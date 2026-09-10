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
    RemotePullRequest,
    _reserve_or_reuse_journaled_pr,
)
from smartpbx_agent_factory.state import GenerationState


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

    def test_only_a_provider_block_can_reenter_verified_for_pr_recovery(self) -> None:
        state = GenerationState.start("gen-001", "a" * 64)
        state.stage = state.stage.VERIFIED
        state.block_pr_provider("PR provider failed for backend")
        state.recover_pr_provider_block()
        self.assertEqual(state.stage.value, "VERIFIED")

        state.block("ordinary prerequisite failure")
        with self.assertRaises(ValueError):
            state.recover_pr_provider_block()


if __name__ == "__main__":
    unittest.main()
