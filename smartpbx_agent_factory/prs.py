"""Fail-closed, review-only coordination for linked generated-agent PRs.

This module deliberately owns no Git, GitHub, deployment, or routing action.
Task 8's orchestration boundary supplies immutable readiness evidence and uses
``open_linked_prs`` as its sole PR-creation seam.
"""

from __future__ import annotations

from dataclasses import dataclass
import ipaddress
from pathlib import Path
import re
from types import MappingProxyType
from typing import Mapping, Protocol, Sequence
from urllib.parse import urlsplit

from .state import GenerationState, Stage, StateError


_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_SHA = re.compile(r"[0-9a-f]{40}\Z")
_ROLES = ("backend", "operations", "website")
_MAX_URL_LENGTH = 2048
_MAX_REDACTION_LENGTH = 4096
_MAX_REPOSITORY_LENGTH = 200
_MAX_BRANCH_LENGTH = 255
_MAX_REVIEW_LABEL_LENGTH = 160
_CONTROL_PLANE_SUFFIX = ".taskforceai.tech"
_HANDLE = re.compile(r"[A-Za-z0-9._:-]{1,128}\Z")
_REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
_HOST_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\Z")
_CREDENTIAL_LIKE = re.compile(
    r"(?:authorization\s*:\s*bearer\s+\S+|(?:api[_-]?key|secret|token|password)\s*[:=]\s*\S+|(?:sk|ghp)_[A-Za-z0-9_-]{8,})",
    re.IGNORECASE,
)


def _is_digest(value: object) -> bool:
    return isinstance(value, str) and _DIGEST.fullmatch(value) is not None


def _is_sha(value: object) -> bool:
    return isinstance(value, str) and _SHA.fullmatch(value) is not None


class PRProvider(Protocol):
    """The only provider calls the coordinator may make."""

    def open_pull_request(self, *, repository: str, branch: str, title: str, body: str) -> str: ...

    def comment_pull_request(self, *, pull_request_url: str, body: str) -> None: ...

    def update_pull_request_body(self, *, pull_request_url: str, body: str) -> None: ...


@dataclass(frozen=True)
class WorktreeInspection:
    """A just-in-time Task 8 inspection bound to an opaque ownership handle."""

    path: Path
    repository: str
    branch: str
    head_sha: str
    clean: bool
    ownership_handle: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", Path(self.path))


class WorktreeInspector(Protocol):
    """Task 8 implementation seam; this module never executes Git itself."""

    def inspect_worktree(self, *, path: Path, ownership_handle: str) -> WorktreeInspection: ...


@dataclass(frozen=True)
class GenerationOwnershipEvidence:
    """Task 8's externally verified handle for a generation-owned worktree root."""

    generation_id: str
    generation_root: Path
    handle: str
    digest: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "generation_root", Path(self.generation_root))


@dataclass(frozen=True)
class GenerationWorktree:
    """Read-only evidence that a PR source belongs to this generation."""

    role: str
    repository: str
    branch: str
    branch_sha: str
    path: Path
    clean: bool
    ownership: GenerationOwnershipEvidence

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", Path(self.path))


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
    expected_wss_hostname: str
    allowed_wss_paths: tuple[str, ...]
    readiness_digest: str
    secret_scan_digest: str
    ci_registration_digest: str
    provenance_digest: str
    worktree_ownership_digest: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "readiness_report_path", Path(self.readiness_report_path))
        object.__setattr__(self, "artifact_digests", MappingProxyType(dict(self.artifact_digests)))
        object.__setattr__(self, "allowed_wss_paths", tuple(self.allowed_wss_paths))


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
    inspector: WorktreeInspector,
    redactions: Sequence[str] = (),
    replace_bodies: bool = False,
) -> PRSet:
    """Open backend, operations, then website review PRs and add final back-links.

    ``redactions`` is transient scrub data: values are removed from every body
    before any provider call and are never returned or persisted by this module.
    """
    normalized_redactions = _validate_redactions(state, redactions)
    _require_verified(state, readiness)
    _validate_readiness(state, readiness)
    by_role = _validate_worktrees(state, readiness, worktrees)
    urls: dict[str, str] = {}

    try:
        for role in _ROLES:
            _inspect_worktree(state, readiness, by_role[role], inspector)
            body = _initial_body(role, state, readiness, by_role[role], urls)
            url = provider.open_pull_request(
                repository=by_role[role].repository,
                branch=by_role[role].branch,
                title=_redact(_title(role, readiness.review_label), normalized_redactions),
                body=_redact(body, normalized_redactions),
            )
            urls[role] = _validate_provider_url(url, by_role[role].repository)
    except Exception as exc:
        failed_role = next(role for role in _ROLES if role not in urls)
        _block(state, f"PR creation failed for {failed_role}")
        raise PRCreationFailure(failed_role, urls) from exc

    backlinks = _redact(_backlinks_body(state, readiness, urls), normalized_redactions)
    try:
        for role in _ROLES:
            provider.comment_pull_request(pull_request_url=urls[role], body=backlinks)
        if replace_bodies:
            for role in _ROLES:
                provider.update_pull_request_body(
                    pull_request_url=urls[role],
                    body=_redact(_initial_body(role, state, readiness, by_role[role], urls), normalized_redactions),
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


def _require_verified(state: GenerationState, readiness: PRReadiness) -> None:
    if state.stage is not Stage.VERIFIED:
        raise StateError("linked PR creation requires generation state VERIFIED")
    if (
        not _is_digest(state.knowledge_review_digest)
        or state.knowledge_approval_digest != state.knowledge_review_digest
        or not _is_digest(state.plan_digest)
        or state.plan_approval_digest != state.plan_digest
    ):
        _block(state, "invalid approved knowledge or plan digest")
        raise StateError("PR prerequisite failed: VERIFIED state requires bound approval digests")
    if not _is_digest(state.manifest_digest):
        _block(state, "invalid manifest digest")
        raise StateError("PR prerequisite failed: manifest digest must be a SHA-256 digest")
    expected = {
        "readiness": readiness.readiness_digest,
        "secret_scan": readiness.secret_scan_digest,
        "ci_registration": readiness.ci_registration_digest,
        "provenance": readiness.provenance_digest,
        "worktree_ownership": readiness.worktree_ownership_digest,
        "artifact_backend": readiness.artifact_digests.get("backend"),
        "artifact_operations": readiness.artifact_digests.get("operations"),
        "artifact_website": readiness.artifact_digests.get("website"),
    }
    if any(state.stage_digests.get(name) != digest for name, digest in expected.items()):
        _block(state, "state digest is missing or does not match immutable readiness evidence")
        raise StateError("PR prerequisite failed: state digest is missing or does not match readiness evidence")


def _validate_readiness(state: GenerationState, readiness: PRReadiness) -> None:
    failures: list[str] = []
    if not readiness.readiness_verified or not _generation_metadata_path(state, readiness.readiness_report_path):
        failures.append("readiness report")
    if not readiness.secret_scan_passed:
        failures.append("secret scan")
    if not readiness.ci_registered:
        failures.append("CI registration")
    if not _is_sha(readiness.provenance_source_revision):
        failures.append("provenance source revision")
    if not _is_sha(readiness.template_revision):
        failures.append("template revision")
    if set(readiness.artifact_digests) != set(_ROLES) or any(
        not _is_digest(value) for value in readiness.artifact_digests.values()
    ):
        failures.append("artifact digests")
    if any(
        not _is_digest(digest)
        for digest in (
            readiness.readiness_digest,
            readiness.secret_scan_digest,
            readiness.ci_registration_digest,
            readiness.provenance_digest,
            readiness.worktree_ownership_digest,
        )
    ):
        failures.append("readiness evidence digests")
    if not _valid_review_label(readiness.review_label):
        failures.append("review label")
    if not _valid_wss_url(
        readiness.wss_url, readiness.expected_wss_hostname, readiness.allowed_wss_paths
    ):
        failures.append("public WSS URL")
    if failures:
        reason = "PR prerequisite failed: " + ", ".join(failures)
        _block(state, reason)
        raise StateError(reason)


def _contains_control(value: str) -> bool:
    return any(ord(character) < 32 or ord(character) == 127 for character in value)


def _contains_credential_like(value: str) -> bool:
    return _CREDENTIAL_LIKE.search(value) is not None


def _generation_metadata_path(state: GenerationState, path: Path) -> bool:
    if path.is_absolute() or ".." in path.parts:
        return False
    return path.parts == (".smartpbx-generations", state.generation_id, "readiness.json")


def _valid_public_hostname(value: object) -> bool:
    if not isinstance(value, str) or not value or len(value) > 253 or _contains_control(value):
        return False
    hostname = value.lower()
    labels = hostname.split(".")
    try:
        ipaddress.ip_address(hostname)
        return False
    except ValueError:
        pass
    if (
        "." not in hostname
        or all(label.isdigit() for label in labels)
        or hostname.endswith((".localhost", ".local", ".internal", ".lan", ".home"))
        or not hostname.startswith("smartpbx-")
        or not hostname.endswith(_CONTROL_PLANE_SUFFIX)
    ):
        return False
    return all(_HOST_LABEL.fullmatch(label) is not None for label in labels)


def _valid_wss_url(value: object, expected_hostname: object, allowed_paths: object) -> bool:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > _MAX_URL_LENGTH
        or _contains_control(value)
        or not _valid_public_hostname(expected_hostname)
        or not isinstance(allowed_paths, tuple)
        or not allowed_paths
        or any(
            not isinstance(path, str)
            or not path.startswith("/")
            or _contains_control(path)
            or "?" in path
            or "#" in path
            for path in allowed_paths
        )
    ):
        return False
    try:
        parsed = urlsplit(value)
        _ = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "wss"
        and parsed.hostname == str(expected_hostname).lower()
        and not parsed.username
        and not parsed.password
        and not parsed.query
        and not parsed.fragment
        and parsed.path in allowed_paths
    )


def _validate_provider_url(value: object, repository: str) -> str:
    if not isinstance(value, str) or not value or len(value) > _MAX_URL_LENGTH or _contains_control(value):
        raise ValueError("provider returned an invalid pull request URL")
    try:
        parsed = urlsplit(value)
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("provider returned an invalid pull request URL") from exc
    if (
        parsed.scheme != "https"
        or parsed.netloc != "github.com"
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path != f"/{repository}/pull/{_pull_number(parsed.path, repository)}"
    ):
        raise ValueError("provider returned an invalid pull request URL")
    return value


def _pull_number(path: str, repository: str) -> str:
    prefix = f"/{repository}/pull/"
    if not path.startswith(prefix):
        return ""
    number = path[len(prefix):]
    return number if re.fullmatch(r"[1-9][0-9]{0,9}", number) else ""


def _valid_repository(value: object) -> bool:
    return (
        isinstance(value, str)
        and 1 <= len(value) <= _MAX_REPOSITORY_LENGTH
        and not _contains_control(value)
        and not _contains_credential_like(value)
        and _REPOSITORY.fullmatch(value) is not None
    )


def _valid_branch(value: object) -> bool:
    return (
        isinstance(value, str)
        and 1 <= len(value) <= _MAX_BRANCH_LENGTH
        and not _contains_control(value)
        and not _contains_credential_like(value)
        and not any(marker in value for marker in (" ", "\t", "\\", "..", "@{", "//"))
        and not value.startswith(("-", "/", "."))
        and not value.endswith(("/", "."))
    )


def _valid_review_label(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value.strip())
        and len(value) <= _MAX_REVIEW_LABEL_LENGTH
        and not _contains_control(value)
        and not _contains_credential_like(value)
    )


def _validate_redactions(state: GenerationState, values: Sequence[str]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        _block(state, "invalid PR redaction input")
        raise StateError("PR prerequisite failed: redactions must be a sequence of strings")
    if any(
        not isinstance(value, str)
        or len(value) > _MAX_REDACTION_LENGTH
        or _contains_control(value)
        for value in values
    ):
        _block(state, "invalid PR redaction input")
        raise StateError("PR prerequisite failed: redactions must be bounded strings")
    return tuple(value for value in values if value)


def _validate_worktrees(
    state: GenerationState, readiness: PRReadiness, worktrees: Sequence[GenerationWorktree]
) -> Mapping[str, GenerationWorktree]:
    try:
        by_role = {worktree.role: worktree for worktree in worktrees}
    except (AttributeError, TypeError):
        _block(state, "PR prerequisite failed: invalid generation worktree evidence")
        raise StateError("PR prerequisite failed: generation worktree evidence is required")
    if len(by_role) != len(worktrees) or set(by_role) != set(_ROLES):
        _block(state, "PR prerequisite failed: three generation worktrees are required")
        raise StateError("PR prerequisite failed: backend, operations, and website worktrees are required")
    root: Path | None = None
    ownership: GenerationOwnershipEvidence | None = None
    resolved_paths: set[Path] = set()
    for role in _ROLES:
        worktree = by_role[role]
        if not isinstance(worktree, GenerationWorktree) or not isinstance(
            worktree.ownership, GenerationOwnershipEvidence
        ):
            _reject_worktree(state, role)
        evidence = worktree.ownership
        try:
            candidate_root = evidence.generation_root.resolve(strict=False)
            candidate_path = worktree.path.resolve(strict=False)
            candidate_path.relative_to(candidate_root)
        except (AttributeError, OSError, ValueError):
            _reject_worktree(state, role)
        if (
            not worktree.clean
            or not _valid_repository(worktree.repository)
            or not _valid_branch(worktree.branch)
            or not _is_sha(worktree.branch_sha)
            or not worktree.path.is_absolute()
            or not candidate_root.is_absolute()
            or candidate_root.name != state.generation_id
            or evidence.generation_id != state.generation_id
            or not _HANDLE.fullmatch(evidence.handle)
            or not _is_digest(evidence.digest)
            or evidence.digest != readiness.worktree_ownership_digest
            or candidate_path == candidate_root
            or candidate_path in resolved_paths
        ):
            _reject_worktree(state, role)
        if ownership is None:
            ownership, root = evidence, candidate_root
        elif evidence != ownership or candidate_root != root:
            _reject_worktree(state, role)
        resolved_paths.add(candidate_path)
    return MappingProxyType(by_role)


def _reject_worktree(state: GenerationState, role: str) -> None:
    _block(state, f"PR prerequisite failed: {role} worktree is not clean and generation-owned")
    raise StateError(f"PR prerequisite failed: {role} worktree must be clean and generation-owned")


def _inspect_worktree(
    state: GenerationState,
    readiness: PRReadiness,
    worktree: GenerationWorktree,
    inspector: WorktreeInspector,
) -> None:
    """Re-read Task 8's verified worktree handle immediately before provider use."""
    try:
        inspection = inspector.inspect_worktree(
            path=worktree.path, ownership_handle=worktree.ownership.handle
        )
        inspected_path = inspection.path.resolve(strict=False)
        expected_path = worktree.path.resolve(strict=False)
        root = worktree.ownership.generation_root.resolve(strict=False)
        inspected_path.relative_to(root)
    except (AttributeError, OSError, TypeError, ValueError):
        _reject_worktree(state, worktree.role)
    if (
        not isinstance(inspection, WorktreeInspection)
        or inspected_path != expected_path
        or inspection.repository != worktree.repository
        or inspection.branch != worktree.branch
        or inspection.head_sha != worktree.branch_sha
        or not inspection.clean
        or inspection.ownership_handle != worktree.ownership.handle
        or not _valid_repository(inspection.repository)
        or not _valid_branch(inspection.branch)
        or not _is_sha(inspection.head_sha)
        or inspection.ownership_handle != worktree.ownership.handle
        or worktree.ownership.digest != readiness.worktree_ownership_digest
    ):
        _reject_worktree(state, worktree.role)


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
