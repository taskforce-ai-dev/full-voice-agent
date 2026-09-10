from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path

import pytest

from smartpbx_agent_factory.prs import (
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

    def open_pull_request(self, *, repository: str, branch: str, title: str, body: str) -> str:
        role = branch.split("/", 1)[0]
        if role == self.fail_role:
            raise RuntimeError("provider rejected private request")
        url = f"https://example.test/{repository}/pull/{len(self.open_order) + 1}"
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
    state.stage = stage
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
    )


def fixture_worktrees() -> tuple[GenerationWorktree, ...]:
    return tuple(
        GenerationWorktree(
            role=role,
            repository=f"taskforce/{role}",
            branch=f"{role}/gen-001",
            branch_sha=letter * 40,
            path=Path(f"/tmp/gen-001/{role}"),
            generation_id="gen-001",
            clean=True,
            generation_owned=True,
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
        **{**worktrees[1].__dict__, "clean": False, "generation_owned": False}
    )
    state = fixture_state()

    with pytest.raises(StateError, match="worktree"):
        open_linked_prs(fake_provider, state=state, readiness=fixture_readiness(), worktrees=tuple(worktrees))

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
