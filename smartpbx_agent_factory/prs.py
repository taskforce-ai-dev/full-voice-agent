"""Fail-closed, review-only coordination for linked generated-agent PRs.

This module deliberately owns no Git, GitHub, deployment, or routing action.
Task 8's orchestration boundary supplies immutable readiness evidence and uses
``open_linked_prs`` as its sole PR-creation seam.
"""

from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import json
import os
from pathlib import Path
import re
import stat
import tempfile
from types import MappingProxyType
from typing import Mapping, Protocol, Sequence
from urllib.parse import urlsplit

from .readiness import ReadinessAuthority, ReadinessError, ReadinessEvidence
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
class RemotePullRequest:
    """The minimal remote identity used when resuming a journaled PR."""

    url: str
    state: str
    base_branch: str
    head_branch: str
    head_sha: str


class PRRecoveryProvider(PRProvider, Protocol):
    """Provider reads required before a persisted PR may be reused."""

    def remote_branch_head(self, *, repository: str, branch: str) -> str: ...

    def find_pull_requests(self, *, repository: str, branch: str) -> Sequence[RemotePullRequest]: ...


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


# Compatibility name for record data.  This value is never a capability: only
# ReadinessAuthority.load() may supply it to the provider boundary.
PRReadiness = ReadinessEvidence


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


class PRJournalError(ValueError):
    """A private PR journal is malformed, missing, or does not match the generation."""


@dataclass(frozen=True)
class PRJournalEntry:
    role: str
    repository: str
    base_branch: str
    branch: str
    remote_head_sha: str
    url: str | None
    state: str


class PRJournal:
    """Private, fsync-safe recovery journal for the three review PRs.

    The journal deliberately records only public GitHub references and immutable
    commit identities.  An intent is sealed before every provider create call;
    a retry must query GitHub and prove the exact recorded PR before reusing it.
    """

    _VERSION = 1
    _ENTRY_STATES = frozenset({
        "intent", "opened", "backlink-pending", "backlinked", "body-pending", "body-updated"
    })

    def __init__(self, state_root: Path) -> None:
        if not isinstance(state_root, Path) or not state_root.is_absolute():
            raise PRJournalError("PR journal state root must be absolute")
        self._root_input = state_root
        self._reject_symlinks(state_root)
        self._root = state_root.resolve()

    def load(self, state: GenerationState) -> Mapping[str, PRJournalEntry]:
        path = self._path(state.generation_id)
        if path.is_symlink():
            raise PRJournalError("PR journal may not be a symlink")
        if not path.exists():
            return MappingProxyType({})
        document = self._read(path)
        if (
            document.get("version") != self._VERSION
            or document.get("generation_id") != state.generation_id
            or document.get("manifest_digest") != state.manifest_digest
            or set(document) != {"version", "generation_id", "manifest_digest", "entries", "record_digest"}
        ):
            raise PRJournalError("PR journal does not match its generation")
        expected = _journal_digest({key: value for key, value in document.items() if key != "record_digest"})
        if document.get("record_digest") != expected:
            raise PRJournalError("PR journal digest does not match its contents")
        raw_entries = document.get("entries")
        if not isinstance(raw_entries, dict) or set(raw_entries) - set(_ROLES):
            raise PRJournalError("PR journal entries are invalid")
        entries: dict[str, PRJournalEntry] = {}
        for role, raw in raw_entries.items():
            entry = _journal_entry(raw)
            if entry.role != role:
                raise PRJournalError("PR journal role is invalid")
            entries[role] = entry
        return MappingProxyType(entries)

    def record_intent(
        self,
        state: GenerationState,
        *,
        role: str,
        repository: str,
        base_branch: str,
        branch: str,
        remote_head_sha: str,
    ) -> PRJournalEntry:
        entry = PRJournalEntry(role, repository, base_branch, branch, remote_head_sha, None, "intent")
        self._write_entry(state, entry)
        return entry

    def record_opened(self, state: GenerationState, entry: PRJournalEntry) -> PRJournalEntry:
        if entry.url is None or entry.state != "opened":
            raise PRJournalError("opened PR journal entry is invalid")
        self._write_entry(state, entry, permitted_previous={"intent", "opened"})
        return entry

    def record_backlink_state(self, state: GenerationState, role: str, checkpoint: str) -> PRJournalEntry:
        if checkpoint not in {"backlink-pending", "backlinked", "body-pending", "body-updated"}:
            raise PRJournalError("PR backlink checkpoint is invalid")
        existing = self.load(state).get(role)
        if existing is None or existing.url is None or existing.state not in self._ENTRY_STATES - {"intent"}:
            raise PRJournalError("PR backlink has no opened review request")
        entry = PRJournalEntry(
            existing.role, existing.repository, existing.base_branch, existing.branch,
            existing.remote_head_sha, existing.url, checkpoint,
        )
        self._write_entry(state, entry)
        return entry

    def _write_entry(
        self, state: GenerationState, entry: PRJournalEntry, *, permitted_previous: set[str] | None = None
    ) -> None:
        _validate_journal_entry(entry)
        entries = dict(self.load(state))
        previous = entries.get(entry.role)
        if previous is not None:
            if previous.repository != entry.repository or previous.base_branch != entry.base_branch or previous.branch != entry.branch or previous.remote_head_sha != entry.remote_head_sha:
                raise PRJournalError("PR journal identity drift is not recoverable")
            if permitted_previous is not None and previous.state not in permitted_previous:
                raise PRJournalError("PR journal checkpoint is not recoverable")
        elif permitted_previous is not None:
            raise PRJournalError("opened PR journal entry requires an intent")
        entries[entry.role] = entry
        self._atomic_write(state, entries)

    def _path(self, generation_id: str) -> Path:
        if not isinstance(generation_id, str) or not re.fullmatch(r"gen-[a-f0-9]{32}|gen-[a-z0-9-]+", generation_id):
            raise PRJournalError("PR journal generation id is invalid")
        return self._root / generation_id / "pull-requests.json"

    def _atomic_write(self, state: GenerationState, entries: Mapping[str, PRJournalEntry]) -> None:
        self._ensure_root()
        path = self._path(state.generation_id)
        directory = path.parent
        if directory.is_symlink():
            raise PRJournalError("PR journal directory may not be a symlink")
        directory.mkdir(mode=0o700, exist_ok=True)
        if directory.is_symlink() or not directory.is_dir() or directory.stat().st_mode & 0o777 != 0o700:
            raise PRJournalError("PR journal directory must be private")
        document: dict[str, object] = {
            "version": self._VERSION,
            "generation_id": state.generation_id,
            "manifest_digest": state.manifest_digest,
            "entries": {role: _journal_entry_payload(entry) for role, entry in sorted(entries.items())},
        }
        document["record_digest"] = _journal_digest(document)
        temporary: Path | None = None
        try:
            descriptor, name = tempfile.mkstemp(prefix=".pull-requests.", dir=directory)
            temporary = Path(name)
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(document, handle, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            if path.is_symlink():
                raise PRJournalError("PR journal may not be a symlink")
            os.replace(temporary, path)
            os.chmod(path, 0o600)
            self._require_private_file(path)
            descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except OSError as error:
            raise PRJournalError("cannot persist PR journal") from error
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _read(self, path: Path) -> dict[str, object]:
        self._ensure_root()
        self._require_private_file(path)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
            with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
                details = os.fstat(descriptor)
                if not stat.S_ISREG(details.st_mode) or details.st_mode & 0o777 != 0o600:
                    raise PRJournalError("PR journal must be a mode-0600 regular file")
                raw = json.load(handle)
        except PRJournalError:
            raise
        except (OSError, json.JSONDecodeError) as error:
            raise PRJournalError("cannot read PR journal") from error
        if not isinstance(raw, dict):
            raise PRJournalError("PR journal schema is invalid")
        return raw

    def _ensure_root(self) -> None:
        self._reject_symlinks(self._root_input)
        self._root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self._root.is_symlink() or not self._root.is_dir() or self._root.stat().st_mode & 0o777 != 0o700:
            raise PRJournalError("PR journal state root must be private")

    @staticmethod
    def _require_private_file(path: Path) -> None:
        if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o777 != 0o600:
            raise PRJournalError("PR journal must be a mode-0600 regular file")

    @staticmethod
    def _reject_symlinks(path: Path) -> None:
        current = path
        while current != current.parent:
            if current.is_symlink():
                raise PRJournalError("PR journal state root may not traverse a symlink")
            current = current.parent


def _journal_digest(value: Mapping[str, object]) -> str:
    import hashlib

    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    ).hexdigest()


def _journal_entry_payload(entry: PRJournalEntry) -> dict[str, object]:
    return {
        "role": entry.role,
        "repository": entry.repository,
        "base_branch": entry.base_branch,
        "branch": entry.branch,
        "remote_head_sha": entry.remote_head_sha,
        "url": entry.url,
        "state": entry.state,
    }


def _journal_entry(raw: object) -> PRJournalEntry:
    if not isinstance(raw, dict) or set(raw) != {
        "role", "repository", "base_branch", "branch", "remote_head_sha", "url", "state"
    }:
        raise PRJournalError("PR journal entry schema is invalid")
    entry = PRJournalEntry(
        raw["role"], raw["repository"], raw["base_branch"], raw["branch"],
        raw["remote_head_sha"], raw["url"], raw["state"],
    )
    _validate_journal_entry(entry)
    return entry


def _validate_journal_entry(entry: PRJournalEntry) -> None:
    if (
        entry.role not in _ROLES
        or not _valid_repository(entry.repository)
        or not _valid_branch(entry.base_branch)
        or not _valid_branch(entry.branch)
        or not _is_sha(entry.remote_head_sha)
        or entry.state not in PRJournal._ENTRY_STATES
        or (entry.url is not None and _validate_provider_url(entry.url, entry.repository) != entry.url)
        or (entry.state == "intent") != (entry.url is None)
        or (entry.state != "intent" and not isinstance(entry.url, str))
    ):
        raise PRJournalError("PR journal entry is invalid")


def _remote_pr_matches(
    entry: PRJournalEntry, remote: RemotePullRequest, *, require_url: bool = True
) -> bool:
    return (
        (not require_url or remote.url == entry.url)
        and remote.state == "OPEN"
        and remote.base_branch == entry.base_branch
        and remote.head_branch == entry.branch
        and remote.head_sha == entry.remote_head_sha
    )


def _require_recovery_provider(provider: PRProvider) -> PRRecoveryProvider:
    if not all(callable(getattr(provider, name, None)) for name in ("remote_branch_head", "find_pull_requests")):
        raise PRJournalError("PR recovery provider cannot validate remote state")
    return provider  # type: ignore[return-value]


def _reserve_or_reuse_journaled_pr(
    journal: PRJournal,
    provider: PRProvider,
    state: GenerationState,
    *,
    worktree: GenerationWorktree,
    base_branch: str,
) -> str | None:
    recovery_provider = _require_recovery_provider(provider)
    remote_head = recovery_provider.remote_branch_head(
        repository=worktree.repository, branch=worktree.branch
    )
    if not _is_sha(remote_head) or remote_head != worktree.branch_sha:
        raise PRJournalError("published review branch head differs from the verified worktree")
    entries = journal.load(state)
    existing = entries.get(worktree.role)
    candidates = tuple(recovery_provider.find_pull_requests(
        repository=worktree.repository, branch=worktree.branch
    ))
    if not all(isinstance(candidate, RemotePullRequest) for candidate in candidates):
        raise PRJournalError("remote PR discovery result is invalid")
    if existing is None:
        if candidates:
            raise PRJournalError("unjournaled PR exists for the generated review branch")
        journal.record_intent(
            state,
            role=worktree.role,
            repository=worktree.repository,
            base_branch=base_branch,
            branch=worktree.branch,
            remote_head_sha=remote_head,
        )
        return None
    if (
        existing.repository != worktree.repository
        or existing.base_branch != base_branch
        or existing.branch != worktree.branch
        or existing.remote_head_sha != remote_head
    ):
        raise PRJournalError("journaled PR identity differs from published review branch")
    if existing.state == "intent":
        if not candidates:
            raise PRJournalError("journaled PR creation outcome is indeterminate")
        exact = [candidate for candidate in candidates if _remote_pr_matches(existing, candidate, require_url=False)]
        if len(exact) != 1 or len(candidates) != 1:
            raise PRJournalError("remote PR discovery is ambiguous or does not match its intent")
        opened = PRJournalEntry(
            existing.role, existing.repository, existing.base_branch, existing.branch,
            existing.remote_head_sha, exact[0].url, "opened",
        )
        journal.record_opened(state, opened)
        return opened.url
    if len(candidates) != 1 or not _remote_pr_matches(existing, candidates[0]):
        raise PRJournalError("journaled PR is closed, ambiguous, or has drifted")
    return existing.url


def open_linked_prs(
    provider: PRProvider,
    *,
    state: GenerationState,
    readiness_authority: ReadinessAuthority,
    worktrees: Sequence[GenerationWorktree],
    inspector: WorktreeInspector,
    redactions: Sequence[str] = (),
    replace_bodies: bool = False,
    journal: PRJournal | None = None,
    base_branches: Mapping[str, str] | None = None,
) -> PRSet:
    """Open backend, operations, then website review PRs and add final back-links.

    ``redactions`` is transient scrub data: values are removed from every body
    before any provider call and are never returned or persisted by this module.
    """
    if not isinstance(state, GenerationState) or state.stage is not Stage.VERIFIED:
        raise StateError("linked PR creation requires generation state VERIFIED")
    normalized_redactions = _validate_redactions(state, redactions)
    if not isinstance(readiness_authority, ReadinessAuthority):
        _block(state, "authoritative readiness record is required")
        raise StateError("PR prerequisite failed: authoritative readiness record is required")
    try:
        # Do not accept a caller-built readiness object here.  The persisted
        # record is hash-checked and bound to this generation/worktree set at
        # the last possible point before any provider interaction.
        readiness = readiness_authority.load(state, worktrees=worktrees)
    except ReadinessError as error:
        _block(state, "authoritative readiness record is invalid")
        raise StateError("PR prerequisite failed: authoritative readiness record is invalid") from error
    _require_verified(state, readiness)
    _validate_readiness(state, readiness)
    by_role = _validate_worktrees(state, readiness, worktrees)
    if (journal is None) != (base_branches is None):
        _block(state, "PR journal configuration is incomplete")
        raise StateError("PR prerequisite failed: PR journal configuration is incomplete")
    if base_branches is not None and (
        set(base_branches) != set(_ROLES) or not all(_valid_branch(base_branches[role]) for role in _ROLES)
    ):
        _block(state, "PR journal base branches are invalid")
        raise StateError("PR prerequisite failed: PR journal base branches are invalid")
    urls: dict[str, str] = {}

    for role in _ROLES:
        try:
            _inspect_worktree(state, readiness, by_role[role], inspector)
            reused = (
                _reserve_or_reuse_journaled_pr(
                    journal, provider, state, worktree=by_role[role], base_branch=base_branches[role]
                )
                if journal is not None and base_branches is not None
                else None
            )
        except Exception as exc:
            _block(state, f"PR prerequisite failed for {role}")
            raise PRCreationFailure(role, urls) from exc
        if reused is not None:
            urls[role] = reused
            continue
        try:
            body = _initial_body(role, state, readiness, by_role[role], urls)
            url = _validate_provider_url(
                provider.open_pull_request(
                    repository=by_role[role].repository,
                    branch=by_role[role].branch,
                    title=_redact(_title(role, readiness.review_label), normalized_redactions),
                    body=_redact(body, normalized_redactions),
                ),
                by_role[role].repository,
            )
            urls[role] = url
            if journal is not None:
                intent = journal.load(state).get(role)
                if intent is None or intent.state != "intent":
                    raise PRJournalError("provider-created PR has no durable creation intent")
                journal.record_opened(
                    state,
                    PRJournalEntry(
                        intent.role, intent.repository, intent.base_branch, intent.branch,
                        intent.remote_head_sha, url, "opened",
                    ),
                )
                discovered = _reserve_or_reuse_journaled_pr(
                    journal, provider, state, worktree=by_role[role], base_branch=base_branches[role]
                )
                if discovered != url:
                    raise PRJournalError("provider-created PR does not match its published review branch")
        except Exception as exc:
            _block_provider(state, f"PR provider failed for {role}")
            raise PRCreationFailure(role, urls) from exc

    backlinks = _redact(_backlinks_body(state, readiness, urls), normalized_redactions)
    try:
        for role in _ROLES:
            if journal is not None:
                entry = journal.load(state).get(role)
                if entry is None:
                    raise PRJournalError("opened PR is missing from the recovery journal")
                if entry.state in {"backlinked", "body-pending", "body-updated"}:
                    continue
                if entry.state == "backlink-pending":
                    raise PRJournalError("PR backlink outcome is indeterminate")
                journal.record_backlink_state(state, role, "backlink-pending")
            provider.comment_pull_request(pull_request_url=urls[role], body=backlinks)
            if journal is not None:
                journal.record_backlink_state(state, role, "backlinked")
        if replace_bodies:
            for role in _ROLES:
                if journal is not None and journal.load(state)[role].state == "body-updated":
                    continue
                if journal is not None and journal.load(state)[role].state == "body-pending":
                    raise PRJournalError("PR body update outcome is indeterminate")
                if journal is not None:
                    journal.record_backlink_state(state, role, "body-pending")
                provider.update_pull_request_body(
                    pull_request_url=urls[role],
                    body=_redact(_initial_body(role, state, readiness, by_role[role], urls), normalized_redactions),
                )
                if journal is not None:
                    journal.record_backlink_state(state, role, "body-updated")
    except Exception as exc:
        _block_provider(state, "PR provider backlink update failed")
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
    resolved_paths: set[Path] = set()
    ownership_handles: set[str] = set()
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
            or evidence.generation_id != state.generation_id
            or not _HANDLE.fullmatch(evidence.handle)
            or not _is_digest(evidence.digest)
            or evidence.digest != readiness.worktree_ownership_digest
            or candidate_path == candidate_root
            or candidate_path in resolved_paths
            or evidence.handle in ownership_handles
        ):
            _reject_worktree(state, role)
        resolved_paths.add(candidate_path)
        ownership_handles.add(evidence.handle)
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


def _block_provider(state: GenerationState, reason: str) -> None:
    if state.stage is Stage.VERIFIED:
        state.block_pr_provider(reason)
    elif state.stage is not Stage.THREE_PRS_OPENED:
        state.block(reason)
