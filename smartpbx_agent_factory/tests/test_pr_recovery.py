"""No-network regression checks for durable linked-PR recovery."""

from __future__ import annotations

import stat
import tempfile
import unittest
from pathlib import Path

from smartpbx_agent_factory.prs import (
    GenerationOwnershipEvidence,
    GenerationWorktree,
    PRJournal,
    PRJournalEntry,
    PRJournalError,
    PRCreationFailure,
    RemotePullRequest,
    WorktreeInspection,
    _reserve_or_reuse_journaled_pr,
    open_linked_prs,
)
from smartpbx_agent_factory.readiness import ReadinessAuthority, ReadinessEvidence
from smartpbx_agent_factory.state import GenerationState, Stage
from smartpbx_agent_factory.verify import VerificationReport


class PRJournalContractTests(unittest.TestCase):
    def _worktree(self) -> GenerationWorktree:
        ownership = GenerationOwnershipEvidence(
            generation_id="gen-001",
            generation_root=Path("/tmp/smartpbx-generations/gen-001"),
            handle="backend-handle",
            digest="a" * 64,
        )
        return GenerationWorktree(
            role="backend",
            repository="taskforce/backend",
            branch="smartpbx-agent-factory/gen-001",
            branch_sha="b" * 40,
            path=ownership.generation_root / "backend",
            clean=True,
            ownership=ownership,
        )

    def test_opened_pr_is_durably_reusable_without_creating_a_second_pr(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "state"
            root.mkdir(mode=0o700)
            state = GenerationState.start("gen-001", "a" * 64)
            journal = PRJournal(root)

            journal.record_intent(
                state,
                role="backend",
                repository="taskforce/backend",
                base_branch="main",
                branch="smartpbx-agent-factory/gen-001",
                remote_head_sha="b" * 40,
            )
            journal.record_opened(
                state,
                PRJournalEntry(
                    role="backend",
                    repository="taskforce/backend",
                    base_branch="main",
                    branch="smartpbx-agent-factory/gen-001",
                    remote_head_sha="b" * 40,
                    url="https://github.com/taskforce/backend/pull/42",
                    state="opened",
                ),
            )

            restored = PRJournal(root).load(state)

            self.assertEqual(restored["backend"].url, "https://github.com/taskforce/backend/pull/42")
            self.assertEqual(restored["backend"].remote_head_sha, "b" * 40)
            journal_path = root / "gen-001" / "pull-requests.json"
            self.assertEqual(stat.S_IMODE(journal_path.stat().st_mode), 0o600)

    def test_retry_reuses_only_the_exact_open_remote_pr(self) -> None:
        class Provider:
            def __init__(self) -> None:
                self.pull_requests: tuple[RemotePullRequest, ...] = ()

            def remote_branch_head(self, *, repository: str, branch: str) -> str:
                self.assertEqual((repository, branch), ("taskforce/backend", "smartpbx-agent-factory/gen-001"))
                return "b" * 40

            def find_pull_requests(self, *, repository: str, branch: str) -> tuple[RemotePullRequest, ...]:
                return self.pull_requests

            def assertEqual(self, actual: object, expected: object) -> None:
                if actual != expected:
                    raise AssertionError((actual, expected))

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "state"
            root.mkdir(mode=0o700)
            state = GenerationState.start("gen-001", "a" * 64)
            journal = PRJournal(root)
            provider = Provider()

            self.assertIsNone(_reserve_or_reuse_journaled_pr(
                journal, provider, state, worktree=self._worktree(), base_branch="main"
            ))
            provider.pull_requests = (RemotePullRequest(
                "https://github.com/taskforce/backend/pull/42", "OPEN", "main",
                "smartpbx-agent-factory/gen-001", "b" * 40,
            ),)
            self.assertEqual(
                _reserve_or_reuse_journaled_pr(
                    journal, provider, state, worktree=self._worktree(), base_branch="main"
                ),
                "https://github.com/taskforce/backend/pull/42",
            )
            self.assertEqual(PRJournal(root).load(state)["backend"].state, "opened")

    def test_retry_refuses_a_closed_or_wrong_head_pr(self) -> None:
        class Provider:
            def remote_branch_head(self, *, repository: str, branch: str) -> str:
                return "b" * 40

            def find_pull_requests(self, *, repository: str, branch: str) -> tuple[RemotePullRequest, ...]:
                return (RemotePullRequest(
                    "https://github.com/taskforce/backend/pull/42", "CLOSED", "main",
                    "smartpbx-agent-factory/gen-001", "b" * 40,
                ),)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "state"
            root.mkdir(mode=0o700)
            state = GenerationState.start("gen-001", "a" * 64)
            journal = PRJournal(root)
            journal.record_intent(
                state,
                role="backend",
                repository="taskforce/backend",
                base_branch="main",
                branch="smartpbx-agent-factory/gen-001",
                remote_head_sha="b" * 40,
            )

            with self.assertRaises(PRJournalError):
                _reserve_or_reuse_journaled_pr(
                    journal, Provider(), state, worktree=self._worktree(), base_branch="main"
                )

    def test_retry_never_recreates_a_preexisting_intent_without_remote_proof(self) -> None:
        class Provider:
            def remote_branch_head(self, *, repository: str, branch: str) -> str:
                return "b" * 40

            def find_pull_requests(self, *, repository: str, branch: str) -> tuple[RemotePullRequest, ...]:
                return ()

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "state"
            root.mkdir(mode=0o700)
            state = GenerationState.start("gen-001", "a" * 64)
            journal = PRJournal(root)
            journal.record_intent(
                state,
                role="backend",
                repository="taskforce/backend",
                base_branch="main",
                branch="smartpbx-agent-factory/gen-001",
                remote_head_sha="b" * 40,
            )

            with self.assertRaises(PRJournalError):
                _reserve_or_reuse_journaled_pr(
                    journal, Provider(), state, worktree=self._worktree(), base_branch="main"
                )

    def test_only_a_provider_block_can_reenter_verified_for_pr_recovery(self) -> None:
        state = GenerationState.start("gen-001", "a" * 64)
        state.stage = state.stage.VERIFIED
        state.block_pr_provider("PR provider failed for backend")
        state.recover_pr_provider_block()
        self.assertEqual(state.stage.value, "VERIFIED")

        state.block("ordinary prerequisite failure")
        with self.assertRaises(ValueError):
            state.recover_pr_provider_block()

    def test_visibility_lag_after_create_keeps_opened_url_and_never_recreates(self) -> None:
        class Provider:
            def __init__(self) -> None:
                self.open_calls = 0

            def remote_branch_head(self, *, repository: str, branch: str) -> str:
                return {"taskforce/backend": "f" * 40, "taskforce/operations": "1" * 40, "taskforce/website": "2" * 40}[repository]

            def find_pull_requests(self, *, repository: str, branch: str) -> tuple[RemotePullRequest, ...]:
                return ()  # Simulate GitHub visibility lag after a successful create.

            def open_pull_request(self, *, repository: str, branch: str, title: str, body: str) -> str:
                self.open_calls += 1
                return f"https://github.com/{repository}/pull/42"

            def comment_pull_request(self, *, pull_request_url: str, body: str) -> None:
                self.fail("backlinks must not run while creation is indeterminate")

            def update_pull_request_body(self, *, pull_request_url: str, body: str) -> None:
                self.fail("body updates must not run while creation is indeterminate")

            def inspect_worktree(self, *, path: Path, ownership_handle: str) -> WorktreeInspection:
                worktree = next(item for item in worktrees if item.path == path)
                return WorktreeInspection(path, worktree.repository, worktree.branch, worktree.branch_sha, True, ownership_handle)

            def fail(self, message: str) -> None:
                raise AssertionError(message)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "state"
            root.mkdir(mode=0o700)
            state = self._verified_state()
            readiness = self._readiness()
            worktrees = self._worktrees()
            authority = ReadinessAuthority(root)
            authority.persist(
                state,
                readiness=readiness,
                worktrees=worktrees,
                verification_reports=self._verification_reports(readiness),
            )
            provider = Provider()
            journal = PRJournal(root)

            with self.assertRaises(PRCreationFailure):
                open_linked_prs(
                    provider, state=state, readiness_authority=authority, worktrees=worktrees,
                    inspector=provider, journal=journal,
                    base_branches={"backend": "main", "operations": "main", "website": "main"},
                )
            self.assertEqual(provider.open_calls, 1)
            self.assertEqual(journal.load(state)["backend"].state, "opened")
            self.assertEqual(journal.load(state)["backend"].url, "https://github.com/taskforce/backend/pull/42")
            self.assertTrue(state.pr_provider_recovery)

            state.recover_pr_provider_block()
            with self.assertRaises(PRCreationFailure):
                open_linked_prs(
                    provider, state=state, readiness_authority=authority, worktrees=worktrees,
                    inspector=provider, journal=journal,
                    base_branches={"backend": "main", "operations": "main", "website": "main"},
                )
            self.assertEqual(provider.open_calls, 1)

    def test_pending_backlink_is_not_replayed_after_an_indeterminate_failure(self) -> None:
        class Provider:
            def __init__(self) -> None:
                self.comment_calls = 0

            def remote_branch_head(self, *, repository: str, branch: str) -> str:
                return {"taskforce/backend": "f" * 40, "taskforce/operations": "1" * 40, "taskforce/website": "2" * 40}[repository]

            def find_pull_requests(self, *, repository: str, branch: str) -> tuple[RemotePullRequest, ...]:
                return (RemotePullRequest(
                    f"https://github.com/{repository}/pull/42", "OPEN", "main", branch,
                    self.remote_branch_head(repository=repository, branch=branch),
                ),)

            def open_pull_request(self, *, repository: str, branch: str, title: str, body: str) -> str:
                self.fail("a journaled PR must never be recreated")

            def comment_pull_request(self, *, pull_request_url: str, body: str) -> None:
                self.comment_calls += 1
                raise RuntimeError("connection lost after submit")

            def update_pull_request_body(self, *, pull_request_url: str, body: str) -> None:
                self.fail("body updates must not run")

            def inspect_worktree(self, *, path: Path, ownership_handle: str) -> WorktreeInspection:
                worktree = next(item for item in worktrees if item.path == path)
                return WorktreeInspection(path, worktree.repository, worktree.branch, worktree.branch_sha, True, ownership_handle)

            def fail(self, message: str) -> None:
                raise AssertionError(message)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "state"
            root.mkdir(mode=0o700)
            state = self._verified_state()
            readiness = self._readiness()
            worktrees = self._worktrees()
            authority = ReadinessAuthority(root)
            authority.persist(state, readiness=readiness, worktrees=worktrees, verification_reports=self._verification_reports(readiness))
            journal = PRJournal(root)
            for worktree in worktrees:
                journal.record_intent(
                    state, role=worktree.role, repository=worktree.repository, base_branch="main",
                    branch=worktree.branch, remote_head_sha=worktree.branch_sha,
                )
                journal.record_opened(state, PRJournalEntry(
                    worktree.role, worktree.repository, "main", worktree.branch, worktree.branch_sha,
                    f"https://github.com/{worktree.repository}/pull/42", "opened",
                ))
            provider = Provider()
            options = dict(
                state=state, readiness_authority=authority, worktrees=worktrees, inspector=provider,
                journal=journal, base_branches={"backend": "main", "operations": "main", "website": "main"},
            )

            with self.assertRaises(PRCreationFailure):
                open_linked_prs(provider, **options)
            self.assertEqual(provider.comment_calls, 1)
            self.assertEqual(journal.load(state)["backend"].state, "backlink-pending")

            state.recover_pr_provider_block()
            with self.assertRaises(PRCreationFailure):
                open_linked_prs(provider, **options)
            self.assertEqual(provider.comment_calls, 1)

    def test_pending_body_update_is_not_replayed_after_an_indeterminate_failure(self) -> None:
        class Provider:
            def __init__(self) -> None:
                self.comment_calls = 0
                self.body_calls = 0

            def remote_branch_head(self, *, repository: str, branch: str) -> str:
                return {"taskforce/backend": "f" * 40, "taskforce/operations": "1" * 40, "taskforce/website": "2" * 40}[repository]

            def find_pull_requests(self, *, repository: str, branch: str) -> tuple[RemotePullRequest, ...]:
                return (RemotePullRequest(
                    f"https://github.com/{repository}/pull/42", "OPEN", "main", branch,
                    self.remote_branch_head(repository=repository, branch=branch),
                ),)

            def open_pull_request(self, *, repository: str, branch: str, title: str, body: str) -> str:
                self.fail("a journaled PR must never be recreated")

            def comment_pull_request(self, *, pull_request_url: str, body: str) -> None:
                self.comment_calls += 1

            def update_pull_request_body(self, *, pull_request_url: str, body: str) -> None:
                self.body_calls += 1
                raise RuntimeError("connection lost after body update")

            def inspect_worktree(self, *, path: Path, ownership_handle: str) -> WorktreeInspection:
                worktree = next(item for item in worktrees if item.path == path)
                return WorktreeInspection(path, worktree.repository, worktree.branch, worktree.branch_sha, True, ownership_handle)

            def fail(self, message: str) -> None:
                raise AssertionError(message)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "state"
            root.mkdir(mode=0o700)
            state = self._verified_state()
            readiness = self._readiness()
            worktrees = self._worktrees()
            authority = ReadinessAuthority(root)
            authority.persist(state, readiness=readiness, worktrees=worktrees, verification_reports=self._verification_reports(readiness))
            journal = PRJournal(root)
            for worktree in worktrees:
                journal.record_intent(state, role=worktree.role, repository=worktree.repository, base_branch="main", branch=worktree.branch, remote_head_sha=worktree.branch_sha)
                journal.record_opened(state, PRJournalEntry(worktree.role, worktree.repository, "main", worktree.branch, worktree.branch_sha, f"https://github.com/{worktree.repository}/pull/42", "opened"))
            provider = Provider()
            options = dict(
                state=state, readiness_authority=authority, worktrees=worktrees, inspector=provider,
                journal=journal, base_branches={"backend": "main", "operations": "main", "website": "main"},
                replace_bodies=True,
            )

            with self.assertRaises(PRCreationFailure):
                open_linked_prs(provider, **options)
            self.assertEqual((provider.comment_calls, provider.body_calls), (3, 1))
            self.assertEqual(journal.load(state)["backend"].state, "body-pending")

            state.recover_pr_provider_block()
            with self.assertRaises(PRCreationFailure):
                open_linked_prs(provider, **options)
            self.assertEqual((provider.comment_calls, provider.body_calls), (3, 1))

    @staticmethod
    def _verified_state() -> GenerationState:
        state = GenerationState.start("gen-001", "a" * 64)
        state.transition(Stage.INPUT_COLLECTED)
        state.transition(Stage.SECRETS_RESOLVED)
        state.transition(Stage.KNOWLEDGE_REVIEW_REQUIRED)
        state.record_knowledge_review_digest("b" * 64)
        state.approve_knowledge("b" * 64)
        state.record_plan_digest("c" * 64)
        state.approve_plan("c" * 64)
        readiness = PRJournalContractTests._readiness()
        for name, digest in {
            "readiness": readiness.readiness_digest,
            "secret_scan": readiness.secret_scan_digest,
            "ci_registration": readiness.ci_registration_digest,
            "provenance": readiness.provenance_digest,
            "worktree_ownership": readiness.worktree_ownership_digest,
            "artifact_backend": readiness.artifact_digests["backend"],
            "artifact_operations": readiness.artifact_digests["operations"],
            "artifact_website": readiness.artifact_digests["website"],
        }.items():
            state.record_stage_digest(name, digest)
        state.transition(Stage.VERIFIED)
        return state

    @staticmethod
    def _readiness() -> ReadinessEvidence:
        return ReadinessEvidence(
            readiness_report_path=Path(".smartpbx-generations/gen-001/readiness.json"),
            readiness_verified=True, secret_scan_passed=True, ci_registered=True,
            provenance_source_revision="a" * 40, template_revision="b" * 40,
            artifact_digests={"backend": "c" * 64, "operations": "d" * 64, "website": "e" * 64},
            review_label="Acme inquiry review", wss_url="wss://smartpbx-acme.taskforceai.tech/ws/v1/smartpbx/media",
            expected_wss_hostname="smartpbx-acme.taskforceai.tech", allowed_wss_paths=("/ws/v1/smartpbx/media",),
            readiness_digest="f" * 64, secret_scan_digest="1" * 64, ci_registration_digest="2" * 64,
            provenance_digest="3" * 64, worktree_ownership_digest="4" * 64,
        )

    @staticmethod
    def _worktrees() -> tuple[GenerationWorktree, ...]:
        return tuple(
            GenerationWorktree(
                role, f"taskforce/{role}", "smartpbx-agent-factory/gen-001", sha,
                Path(f"/tmp/{role}-factory/gen-001/checkout"), True,
                GenerationOwnershipEvidence("gen-001", Path(f"/tmp/{role}-factory/gen-001"), f"{role}-handle", "4" * 64),
            )
            for role, sha in (("backend", "f" * 40), ("operations", "1" * 40), ("website", "2" * 40))
        )

    @staticmethod
    def _verification_reports(readiness: ReadinessEvidence) -> dict[str, VerificationReport]:
        return {
            role: VerificationReport(
                agent_slug=f"acme-{role}", artifact_digest=digest, template_version="v1",
                source_revision=readiness.provenance_source_revision, ci_identifier=f"ci-{role}",
                protocol_events=("connected", "start", "media", "stop", "hangup"), static_contracts_passed=True,
                runtime_lifecycle_verified=True, ready_for_pr=True, runtime_status="CI_LIFECYCLE_VERIFIED",
                evidence=("Dockerfile", "server.py", "smartpbx_gateway.py", "smartpbx_protocol.py", "smartpbx_transport.py", "smartpbx_diagnostics.py", "docker-compose.yml", ".github-workflow-fragment.yml", ".smartpbx-factory-provenance.json"),
            )
            for role, digest in readiness.artifact_digests.items()
        }


if __name__ == "__main__":
    unittest.main()
