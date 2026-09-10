"""Fail-closed, review-only coordination for linked generated-agent PRs.

This module deliberately owns no Git, GitHub, deployment, or routing action.
Task 8's orchestration boundary supplies immutable readiness evidence and uses
``open_linked_prs`` as its sole PR-creation seam.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from types import MappingProxyType
from typing import Mapping, Protocol, Sequence

from .state import GenerationState, Stage, StateError


_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_SHA = re.compile(r"[0-9a-f]{40}\Z")
_ROLES = ("backend", "operations", "website")


class PRProvider(Protocol):
    """The only provider calls the coordinator may make."""

    def open_pull_request(self, *, repository: str, branch: str, title: str, body: str) -> str: ...

    def comment_pull_request(self, *, pull_request_url: str, body: str) -> None: ...

    def update_pull_request_body(self, *, pull_request_url: str, body: str) -> None: ...


@dataclass(frozen=True)
class GenerationWorktree:
    """Read-only evidence that a PR source belongs to this generation."""

    role: str
    repository: str
    branch: str
    branch_sha: str
    path: Path
    generation_id: str
    clean: bool
    generation_owned: bool


@dataclass(frozen=True)
class PRReadiness:
    """Immutable prerequisites supplied by the verification/orchestration seam."""

    readiness_report_path: Path
    readiness_verified: bool
    secret_scan_passed: bool
    ci_registered: bool
    provenance_source_revision: str
    template_revision: str
    artifact_digests: Mapping[str, str]
    review_label: str
    wss_url: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "readiness_report_path", Path(self.readiness_report_path))
        object.__setattr__(self, "artifact_digests", MappingProxyType(dict(self.artifact_digests)))


@dataclass(frozen=True)
class PRSet:
    backend_url: str
    operations_url: str
    website_url: str
    readiness_report_path: Path
    manifest_digest: str
    artifact_digests: Mapping[str, str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "readiness_report_path", Path(self.readiness_report_path))
        object.__setattr__(self, "artifact_digests", MappingProxyType(dict(self.artifact_digests)))


class PRCreationFailure(RuntimeError):
    """Reports a later provider failure without implying earlier PR rollback."""

    def __init__(self, failed_role: str, opened_urls: Mapping[str, str]) -> None:
        self.failed_role = failed_role
        self.opened_urls = MappingProxyType(dict(opened_urls))
        super().__init__(f"PR creation failed for {failed_role}; earlier PRs remain open")


def open_linked_prs(
    provider: PRProvider,
    *,
    state: GenerationState,
    readiness: PRReadiness,
    worktrees: Sequence[GenerationWorktree],
    redactions: Sequence[str] = (),
    replace_bodies: bool = False,
) -> PRSet:
    """Open backend, operations, then website review PRs and add final back-links.

    ``redactions`` is transient scrub data: values are removed from every body
    before any provider call and are never returned or persisted by this module.
    """
    _require_verified(state)
    _validate_readiness(state, readiness)
    by_role = _validate_worktrees(state, worktrees)
    urls: dict[str, str] = {}

    try:
        for role in _ROLES:
            body = _initial_body(role, state, readiness, by_role[role], urls)
            url = provider.open_pull_request(
                repository=by_role[role].repository,
                branch=by_role[role].branch,
                title=_redact(_title(role, readiness.review_label), redactions),
                body=_redact(body, redactions),
            )
            if not url:
                raise ValueError("provider returned an empty pull request URL")
            urls[role] = url
    except Exception as exc:
        failed_role = next(role for role in _ROLES if role not in urls)
        _block(state, f"PR creation failed for {failed_role}")
        raise PRCreationFailure(failed_role, urls) from exc

    backlinks = _redact(_backlinks_body(state, readiness, urls), redactions)
    try:
        for role in _ROLES:
            provider.comment_pull_request(pull_request_url=urls[role], body=backlinks)
        if replace_bodies:
            for role in _ROLES:
                provider.update_pull_request_body(
                    pull_request_url=urls[role],
                    body=_redact(_initial_body(role, state, readiness, by_role[role], urls), redactions),
                )
    except Exception as exc:
        _block(state, "PR backlink update failed")
        raise PRCreationFailure("backlinks", urls) from exc

    state.transition(Stage.THREE_PRS_OPENED)
    return PRSet(
        backend_url=urls["backend"],
        operations_url=urls["operations"],
        website_url=urls["website"],
        readiness_report_path=readiness.readiness_report_path,
        manifest_digest=state.manifest_digest,
        artifact_digests=readiness.artifact_digests,
    )


def _require_verified(state: GenerationState) -> None:
    if state.stage is not Stage.VERIFIED:
        raise StateError("linked PR creation requires generation state VERIFIED")
    if not _DIGEST.fullmatch(state.manifest_digest):
        _block(state, "invalid manifest digest")
        raise StateError("PR prerequisite failed: manifest digest must be a SHA-256 digest")


def _validate_readiness(state: GenerationState, readiness: PRReadiness) -> None:
    failures: list[str] = []
    if not readiness.readiness_verified or not readiness.readiness_report_path.name:
        failures.append("readiness report")
    if not readiness.secret_scan_passed:
        failures.append("secret scan")
    if not readiness.ci_registered:
        failures.append("CI registration")
    if not _SHA.fullmatch(readiness.provenance_source_revision):
        failures.append("provenance source revision")
    if not _SHA.fullmatch(readiness.template_revision):
        failures.append("template revision")
    if set(readiness.artifact_digests) != set(_ROLES) or any(
        not _DIGEST.fullmatch(value) for value in readiness.artifact_digests.values()
    ):
        failures.append("artifact digests")
    if not readiness.review_label.strip():
        failures.append("review label")
    if not readiness.wss_url.startswith("wss://") or any(
        marker in readiness.wss_url.lower() for marker in ("token", "secret", "password", "@")
    ):
        failures.append("public WSS URL")
    if failures:
        reason = "PR prerequisite failed: " + ", ".join(failures)
        _block(state, reason)
        raise StateError(reason)


def _validate_worktrees(
    state: GenerationState, worktrees: Sequence[GenerationWorktree]
) -> Mapping[str, GenerationWorktree]:
    by_role = {worktree.role: worktree for worktree in worktrees}
    if len(by_role) != len(worktrees) or set(by_role) != set(_ROLES):
        _block(state, "PR prerequisite failed: three generation worktrees are required")
        raise StateError("PR prerequisite failed: backend, operations, and website worktrees are required")
    for role in _ROLES:
        worktree = by_role[role]
        if (
            not worktree.clean
            or not worktree.generation_owned
            or worktree.generation_id != state.generation_id
            or not worktree.repository
            or not worktree.branch
            or not _SHA.fullmatch(worktree.branch_sha)
            or not str(worktree.path)
        ):
            _block(state, f"PR prerequisite failed: {role} worktree is not clean and generation-owned")
            raise StateError(f"PR prerequisite failed: {role} worktree must be clean and generation-owned")
    return MappingProxyType(by_role)


def _title(role: str, review_label: str) -> str:
    if role == "website":
        return f"Review-only website demo: {review_label}"
    return f"Review-only {role} artifact: {review_label}"


def _initial_body(
    role: str,
    state: GenerationState,
    readiness: PRReadiness,
    worktree: GenerationWorktree,
    urls: Mapping[str, str],
) -> str:
    lines = [
        f"# {role.title()} review-only PR",
        "",
        f"Review label: {readiness.review_label}",
        f"Manifest digest: `{state.manifest_digest}`",
        f"Source revision: `{readiness.provenance_source_revision}`",
        f"Template revision: `{readiness.template_revision}`",
        f"Branch SHA: `{worktree.branch_sha}`",
        f"Artifact digest: `{readiness.artifact_digests[role]}`",
        f"Readiness report: `{readiness.readiness_report_path}`",
        f"Public WSS endpoint: {readiness.wss_url}",
    ]
    if role == "operations":
        lines.append(f"Backend PR: {urls['backend']}")
    elif role == "website":
        lines.extend(
            (
                f"Backend PR: {urls['backend']}",
                f"Operations PR: {urls['operations']}",
                "Release gate: pending approved routing activation after backend health proof.",
                'releaseState: "pending"',
                "Do not edit DEMO_AGENT_HOSTS in this review PR; see demo-routing-activation.md.",
            )
        )
    return "\n".join(lines) + "\n"


def _backlinks_body(
    state: GenerationState, readiness: PRReadiness, urls: Mapping[str, str]
) -> str:
    return "\n".join(
        (
            "# Linked review order",
            f"1. Backend: {urls['backend']}",
            f"2. Operations: {urls['operations']}",
            f"3. Website: {urls['website']}",
            f"Manifest digest: `{state.manifest_digest}`",
            f"Backend artifact digest: `{readiness.artifact_digests['backend']}`",
            f"Operations artifact digest: `{readiness.artifact_digests['operations']}`",
            f"Website artifact digest: `{readiness.artifact_digests['website']}`",
            f"Source revision: `{readiness.provenance_source_revision}`",
            f"Template revision: `{readiness.template_revision}`",
            "Website remains pending approved routing activation until backend health is proven.",
        )
    ) + "\n"


def _redact(body: str, values: Sequence[str]) -> str:
    redacted = body
    for value in sorted({value for value in values if value}, key=len, reverse=True):
        redacted = redacted.replace(value, "[REDACTED]")
    return redacted


def _block(state: GenerationState, reason: str) -> None:
    if state.stage is not Stage.THREE_PRS_OPENED:
        state.block(reason)
