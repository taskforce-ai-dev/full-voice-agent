from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path

import pytest

from smartpbx_agent_factory.prs import (
    GenerationOwnershipEvidence,
    GenerationWorktree,
    PRCreationFailure,
    PRReadiness,
    open_linked_prs,
)
from smartpbx_agent_factory.state import GenerationState, Stage, StateError


@dataclass
class FakePRProvider:
    open_order: list[str] = field(default_factory=list)
    calls: list[str] = field(default_factory=list)
    bodies: dict[str, str] = field(default_factory=dict)
    titles: dict[str, str] = field(default_factory=dict)
    comments: dict[str, list[str]] = field(default_factory=dict)
    body_updates: list[str] = field(default_factory=list)
    release_allowed: dict[str, bool] = field(default_factory=dict)
    fail_role: str | None = None
    returned_urls: dict[str, str] = field(default_factory=dict)

    def open_pull_request(self, *, repository: str, branch: str, title: str, body: str) -> str:
        role = branch.split("/", 1)[0]
        if role == self.fail_role:
            raise RuntimeError("provider rejected private request")
        url = self.returned_urls.get(
            role, f"https://example.test/{repository}/pull/{len(self.open_order) + 1}"
        )
        self.open_order.append(role)
        self.calls.append(f"open:{role}")
        self.bodies[url] = body
        self.titles[url] = title
        self.comments[url] = []
        self.release_allowed[url] = 'releaseState: "pending"' not in body
        return url

    def comment_pull_request(self, *, pull_request_url: str, body: str) -> None:
        self.calls.append(f"comment:{pull_request_url}")
        self.comments[pull_request_url].append(body)

    def update_pull_request_body(self, *, pull_request_url: str, body: str) -> None:
        self.calls.append(f"update:{pull_request_url}")
        self.body_updates.append(pull_request_url)
        self.bodies[pull_request_url] = body


@pytest.fixture
def fake_provider() -> FakePRProvider:
    return FakePRProvider()


def fixture_state(stage: Stage = Stage.VERIFIED) -> GenerationState:
    state = GenerationState.start("gen-001", "a" * 64)
    state.transition(Stage.INPUT_COLLECTED)
    state.transition(Stage.SECRETS_RESOLVED)
    state.transition(Stage.KNOWLEDGE_REVIEW_REQUIRED)
    state.record_knowledge_review_digest("b" * 64)
    state.approve_knowledge("b" * 64)
    state.record_plan_digest("c" * 64)
    state.approve_plan("c" * 64)
    if stage is Stage.GENERATED:
        return state
    for name, digest in fixture_stage_digests().items():
        state.record_stage_digest(name, digest)
    state.transition(Stage.VERIFIED)
    return state


def fixture_readiness() -> PRReadiness:
    return PRReadiness(
        readiness_report_path=Path(".smartpbx-generations/gen-001/readiness.json"),
        readiness_verified=True,
        secret_scan_passed=True,
        ci_registered=True,
        provenance_source_revision="a" * 40,
        template_revision="b" * 40,
        artifact_digests={"backend": "c" * 64, "operations": "d" * 64, "website": "e" * 64},
        review_label="Acme inquiry review",
        wss_url="wss://smartpbx-acme.example.test/ws/v1/smartpbx/media",
        allowed_wss_paths=("/ws/v1/smartpbx/media",),
        readiness_digest="f" * 64,
        secret_scan_digest="1" * 64,
        ci_registration_digest="2" * 64,
        provenance_digest="3" * 64,
        worktree_ownership_digest="4" * 64,
    )


def fixture_stage_digests() -> dict[str, str]:
    readiness = fixture_readiness()
    return {
        "readiness": readiness.readiness_digest,
        "secret_scan": readiness.secret_scan_digest,
        "ci_registration": readiness.ci_registration_digest,
        "provenance": readiness.provenance_digest,
        "worktree_ownership": readiness.worktree_ownership_digest,
        "artifact_backend": readiness.artifact_digests["backend"],
        "artifact_operations": readiness.artifact_digests["operations"],
        "artifact_website": readiness.artifact_digests["website"],
    }


def fixture_ownership() -> GenerationOwnershipEvidence:
    return GenerationOwnershipEvidence(
        generation_id="gen-001",
        generation_root=Path("/tmp/smartpbx-generations/gen-001"),
        handle="worktree-handle-gen-001",
        digest="4" * 64,
    )


def fixture_worktrees() -> tuple[GenerationWorktree, ...]:
    ownership = fixture_ownership()
    return tuple(
        GenerationWorktree(
            role=role,
            repository=f"taskforce/{role}",
            branch=f"{role}/gen-001",
            branch_sha=letter * 40,
            path=ownership.generation_root / role,
            clean=True,
            ownership=ownership,
        )
        for role, letter in (("backend", "f"), ("operations", "1"), ("website", "2"))
    )


def test_pr_creation_requires_verified_state(fake_provider: FakePRProvider) -> None:
    with pytest.raises(StateError, match="VERIFIED"):
        open_linked_prs(
            fake_provider,
            state=fixture_state(Stage.GENERATED),
            readiness=fixture_readiness(),
            worktrees=fixture_worktrees(),
        )


def test_three_prs_are_opened_sequentially_with_immutable_digests(fake_provider: FakePRProvider) -> None:
    state = fixture_state()
    result = open_linked_prs(
        fake_provider,
        state=state,
        readiness=fixture_readiness(),
        worktrees=fixture_worktrees(),
    )

    assert result.backend_url
    assert result.operations_url
    assert result.website_url
    assert fake_provider.open_order == ["backend", "operations", "website"]
    assert fake_provider.calls[:3] == ["open:backend", "open:operations", "open:website"]
    assert all(len(digest) == 64 for digest in result.artifact_digests.values())
    assert fake_provider.comments[result.backend_url]
    assert fake_provider.comments[result.operations_url]
    assert fake_provider.comments[result.website_url]
    assert result.manifest_digest == "a" * 64
    assert result.readiness_report_path.name == "readiness.json"
    assert state.stage is Stage.THREE_PRS_OPENED
    assert fake_provider.body_updates == []
    for url in (result.backend_url, result.operations_url, result.website_url):
        comment = fake_provider.comments[url][0]
        assert result.backend_url in comment
        assert result.operations_url in comment
        assert result.website_url in comment
        for digest in result.artifact_digests.values():
            assert digest in comment


def test_initial_bodies_link_only_previously_opened_prs(fake_provider: FakePRProvider) -> None:
    result = open_linked_prs(
        fake_provider,
        state=fixture_state(),
        readiness=fixture_readiness(),
        worktrees=fixture_worktrees(),
    )

    assert result.operations_url not in fake_provider.bodies[result.backend_url]
    assert result.website_url not in fake_provider.bodies[result.backend_url]
    assert result.backend_url in fake_provider.bodies[result.operations_url]
    assert result.website_url not in fake_provider.bodies[result.operations_url]
    assert result.backend_url in fake_provider.bodies[result.website_url]
    assert result.operations_url in fake_provider.bodies[result.website_url]


def test_pr_body_is_redacted_without_losing_public_wss_metadata(fake_provider: FakePRProvider) -> None:
    secret = "marker"
    readiness = replace(fixture_readiness(), review_label=secret)
    result = open_linked_prs(
        fake_provider,
        state=fixture_state(),
        readiness=readiness,
        worktrees=fixture_worktrees(),
        redactions=(secret,),
    )

    assert secret not in fake_provider.bodies[result.backend_url]
    assert secret not in fake_provider.titles[result.backend_url]
    assert "wss://" in fake_provider.bodies[result.backend_url]


def test_website_pr_is_review_only_until_routing_activation(fake_provider: FakePRProvider) -> None:
    result = open_linked_prs(
        fake_provider,
        state=fixture_state(),
        readiness=fixture_readiness(),
        worktrees=fixture_worktrees(),
    )

    body = fake_provider.bodies[result.website_url]
    assert "pending approved routing activation" in body
    assert "backend health" in body
    assert 'releaseState: "pending"' in body
    assert "DEMO_AGENT_HOSTS" in body
    assert fake_provider.release_allowed[result.website_url] is False


@pytest.mark.parametrize(
    "field",
    ("readiness_verified", "secret_scan_passed", "ci_registered"),
)
def test_missing_readiness_gate_fails_closed_and_blocks_generation(field: str, fake_provider: FakePRProvider) -> None:
    readiness = fixture_readiness()
    object.__setattr__(readiness, field, False)
    state = fixture_state()

    with pytest.raises(StateError, match="prerequisite"):
        open_linked_prs(fake_provider, state=state, readiness=readiness, worktrees=fixture_worktrees())

    assert state.stage is Stage.BLOCKED
    assert fake_provider.open_order == []


def test_dirty_or_non_owned_worktree_fails_closed_before_provider_call(fake_provider: FakePRProvider) -> None:
    worktrees = list(fixture_worktrees())
    worktrees[1] = GenerationWorktree(
        **{**worktrees[1].__dict__, "clean": False}
    )
    state = fixture_state()

    with pytest.raises(StateError, match="worktree"):
        open_linked_prs(fake_provider, state=state, readiness=fixture_readiness(), worktrees=tuple(worktrees))

    assert state.stage is Stage.BLOCKED
    assert fake_provider.open_order == []


@pytest.mark.parametrize(
    "url",
    (
        "http://example.test/pull/1",
        "https://user:password@example.test/pull/1",
        "https://example.test/pull/1?token=leak",
        "https://example.test/pull/1#fragment",
        "https://example.test/pull/1\nnext",
    ),
)
def test_provider_url_must_be_bounded_credential_free_https_before_storage(
    url: str, fake_provider: FakePRProvider
) -> None:
    fake_provider.returned_urls["backend"] = url
    state = fixture_state()

    with pytest.raises(PRCreationFailure) as raised:
        open_linked_prs(fake_provider, state=state, readiness=fixture_readiness(), worktrees=fixture_worktrees())

    assert raised.value.failed_role == "backend"
    assert dict(raised.value.opened_urls) == {}
    assert fake_provider.open_order == ["backend"]
    assert state.stage is Stage.BLOCKED


@pytest.mark.parametrize(
    "wss_url",
    (
        "https://smartpbx-acme.example.test/ws/v1/smartpbx/media",
        "wss://user:password@smartpbx-acme.example.test/ws/v1/smartpbx/media",
        "wss://smartpbx-acme.example.test/ws/v1/smartpbx/media?api_key=leak",
        "wss://smartpbx-acme.example.test/other",
        "wss://smartpbx-acme.example.test/ws/v1/smartpbx/media#fragment",
    ),
)
def test_public_wss_url_must_match_allowed_media_path_without_credentials_or_parameters(
    wss_url: str, fake_provider: FakePRProvider
) -> None:
    state = fixture_state()

    with pytest.raises(StateError, match="public WSS URL"):
        open_linked_prs(
            fake_provider,
            state=state,
            readiness=replace(fixture_readiness(), wss_url=wss_url),
            worktrees=fixture_worktrees(),
        )

    assert state.stage is Stage.BLOCKED
    assert fake_provider.open_order == []


def test_non_string_redaction_fails_closed_without_provider_call(fake_provider: FakePRProvider) -> None:
    state = fixture_state()

    with pytest.raises(StateError, match="redaction"):
        open_linked_prs(
            fake_provider,
            state=state,
            readiness=fixture_readiness(),
            worktrees=fixture_worktrees(),
            redactions=(object(),),
        )

    assert state.stage is Stage.BLOCKED
    assert fake_provider.open_order == []


def test_readiness_report_must_be_generation_owned_repo_relative_metadata(fake_provider: FakePRProvider) -> None:
    state = fixture_state()

    with pytest.raises(StateError, match="readiness report"):
        open_linked_prs(
            fake_provider,
            state=state,
            readiness=replace(fixture_readiness(), readiness_report_path=Path("/tmp/private/readiness.json")),
            worktrees=fixture_worktrees(),
        )

    assert state.stage is Stage.BLOCKED
    assert fake_provider.open_order == []


def test_worktree_must_be_contained_by_bound_generation_ownership_evidence(fake_provider: FakePRProvider) -> None:
    state = fixture_state()
    worktrees = list(fixture_worktrees())
    worktrees[0] = GenerationWorktree(
        **{**worktrees[0].__dict__, "path": Path("/tmp/smartpbx-generations/gen-001/../escape")}
    )

    with pytest.raises(StateError, match="worktree"):
        open_linked_prs(fake_provider, state=state, readiness=fixture_readiness(), worktrees=tuple(worktrees))

    assert state.stage is Stage.BLOCKED
    assert fake_provider.open_order == []


def test_verified_state_requires_bound_readiness_and_ownership_digests(fake_provider: FakePRProvider) -> None:
    state = fixture_state()
    state.stage_digests.pop("worktree_ownership")

    with pytest.raises(StateError, match="state digest"):
        open_linked_prs(fake_provider, state=state, readiness=fixture_readiness(), worktrees=fixture_worktrees())

    assert state.stage is Stage.BLOCKED
    assert fake_provider.open_order == []


def test_missing_provenance_record_fails_closed_before_provider_call(fake_provider: FakePRProvider) -> None:
    state = fixture_state()
    readiness = replace(fixture_readiness(), provenance_source_revision="main")

    with pytest.raises(StateError, match="provenance"):
        open_linked_prs(fake_provider, state=state, readiness=readiness, worktrees=fixture_worktrees())

    assert state.stage is Stage.BLOCKED
    assert fake_provider.open_order == []


def test_later_pr_failure_preserves_earlier_pr_and_reports_partial_state(fake_provider: FakePRProvider) -> None:
    fake_provider.fail_role = "operations"
    state = fixture_state()

    with pytest.raises(PRCreationFailure) as raised:
        open_linked_prs(fake_provider, state=state, readiness=fixture_readiness(), worktrees=fixture_worktrees())

    failure = raised.value
    assert failure.failed_role == "operations"
    assert tuple(failure.opened_urls) == ("backend",)
    assert state.stage is Stage.BLOCKED
    assert fake_provider.open_order == ["backend"]
