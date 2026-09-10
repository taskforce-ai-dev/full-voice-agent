"""Coordinator-owned, durable readiness evidence for review-only PR creation."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping, Sequence

from .state import GenerationState
from .verify import VerificationReport


class ReadinessError(ValueError):
    """Raised when authoritative readiness evidence is unsafe or stale."""


_DIGEST = re.compile(r"[0-9a-f]{64}$")
_SHA = re.compile(r"[0-9a-f]{40}$")
_ROLES = ("backend", "operations", "website")
_RECORD_VERSION = 1
_RECORD_KEYS = {
    "version", "generation_id", "manifest_digest", "readiness_report_path",
    "readiness_verified", "secret_scan_passed", "ci_registered",
    "provenance_source_revision", "template_revision", "artifact_digests",
    "review_label", "wss_url", "expected_wss_hostname", "allowed_wss_paths",
    "readiness_digest", "secret_scan_digest", "ci_registration_digest",
    "provenance_digest", "worktree_ownership_digest", "worktrees", "record_digest",
}


@dataclass(frozen=True)
class ReadinessEvidence:
    """Data included in the coordinator's canonical readiness record."""

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


class ReadinessAuthority:
    """The sole filesystem authority accepted by the PR provider boundary."""

    def __init__(self, state_root: Path) -> None:
        if not isinstance(state_root, Path) or not state_root.is_absolute():
            raise ReadinessError("readiness state root must be absolute")
        self._state_root_input = state_root
        self._reject_symlinks(state_root)
        self._state_root = state_root.resolve()

    def record_path(self, generation_id: str) -> Path:
        self._validate_generation_id(generation_id)
        return self._state_root / generation_id / "readiness.json"

    def persist(
        self,
        state: GenerationState,
        *,
        readiness: ReadinessEvidence,
        worktrees: Sequence[object],
        verification_reports: Mapping[str, VerificationReport],
    ) -> Path:
        """Persist the post-verification record before a state can authorize PRs."""
        if not isinstance(state, GenerationState):
            raise ReadinessError("generation state is required")
        if not isinstance(readiness, ReadinessEvidence):
            raise ReadinessError("canonical readiness evidence is required")
        self._ensure_root(create=True)
        path = self.record_path(state.generation_id)
        self._ensure_generation_dir(state.generation_id, create=True)
        self._require_verified_reports(readiness, verification_reports)
        document = self._document(state, readiness, worktrees)
        self._atomic_write(path, document)
        return path

    def load(
        self, state: GenerationState, *, worktrees: Sequence[object] | None = None
    ) -> ReadinessEvidence:
        """Load, hash-check, and bind evidence immediately before provider use."""
        if not isinstance(state, GenerationState):
            raise ReadinessError("generation state is required")
        self._ensure_root(create=False)
        path = self.record_path(state.generation_id)
        self._ensure_generation_dir(state.generation_id, create=False)
        document = self._read_document(path)
        if document.get("generation_id") != state.generation_id:
            raise ReadinessError("readiness record generation does not match state")
        if document.get("manifest_digest") != state.manifest_digest:
            raise ReadinessError("readiness record manifest digest does not match state")
        expected_digest = _canonical_digest({key: value for key, value in document.items() if key != "record_digest"})
        if document.get("record_digest") != expected_digest:
            raise ReadinessError("readiness record digest does not match its canonical contents")
        readiness = self._readiness_from_document(document)
        self._validate_record_worktrees(document["worktrees"])
        if worktrees is not None and document["worktrees"] != self._worktree_payloads(worktrees):
            raise ReadinessError("readiness record worktree binding changed")
        return readiness

    def _document(
        self, state: GenerationState, readiness: ReadinessEvidence, worktrees: Sequence[object]
    ) -> dict[str, object]:
        report_path = Path(readiness.readiness_report_path)
        expected_report = Path(".smartpbx-generations") / state.generation_id / "readiness.json"
        if report_path != expected_report:
            raise ReadinessError("readiness report path must be generation-contained")
        if not all(isinstance(value, bool) and value for value in (
            readiness.readiness_verified, readiness.secret_scan_passed, readiness.ci_registered
        )):
            raise ReadinessError("real verification, secret scan, and CI registration are required")
        if not _SHA.fullmatch(readiness.provenance_source_revision) or not _SHA.fullmatch(readiness.template_revision):
            raise ReadinessError("readiness source and template revisions must be immutable SHAs")
        if set(readiness.artifact_digests) != set(_ROLES) or not all(
            _DIGEST.fullmatch(value) for value in readiness.artifact_digests.values()
        ):
            raise ReadinessError("readiness must bind every role artifact digest")
        if not all(_DIGEST.fullmatch(value) for value in (
            state.manifest_digest, readiness.readiness_digest, readiness.secret_scan_digest,
            readiness.ci_registration_digest, readiness.provenance_digest,
            readiness.worktree_ownership_digest,
        )):
            raise ReadinessError("readiness evidence digests must be SHA-256 values")
        document: dict[str, object] = {
            "version": _RECORD_VERSION,
            "generation_id": state.generation_id,
            "manifest_digest": state.manifest_digest,
            "readiness_report_path": report_path.as_posix(),
            "readiness_verified": readiness.readiness_verified,
            "secret_scan_passed": readiness.secret_scan_passed,
            "ci_registered": readiness.ci_registered,
            "provenance_source_revision": readiness.provenance_source_revision,
            "template_revision": readiness.template_revision,
            "artifact_digests": dict(sorted(readiness.artifact_digests.items())),
            "review_label": readiness.review_label,
            "wss_url": readiness.wss_url,
            "expected_wss_hostname": readiness.expected_wss_hostname,
            "allowed_wss_paths": list(readiness.allowed_wss_paths),
            "readiness_digest": readiness.readiness_digest,
            "secret_scan_digest": readiness.secret_scan_digest,
            "ci_registration_digest": readiness.ci_registration_digest,
            "provenance_digest": readiness.provenance_digest,
            "worktree_ownership_digest": readiness.worktree_ownership_digest,
            "worktrees": self._worktree_payloads(worktrees),
        }
        document["record_digest"] = _canonical_digest(document)
        return document

    def _readiness_from_document(self, document: Mapping[str, object]) -> ReadinessEvidence:
        if set(document) != _RECORD_KEYS or document.get("version") != _RECORD_VERSION:
            raise ReadinessError("readiness record has an invalid schema")
        try:
            readiness = ReadinessEvidence(
                readiness_report_path=Path(_string(document, "readiness_report_path")),
                readiness_verified=_true(document, "readiness_verified"),
                secret_scan_passed=_true(document, "secret_scan_passed"),
                ci_registered=_true(document, "ci_registered"),
                provenance_source_revision=_immutable_sha(document, "provenance_source_revision"),
                template_revision=_immutable_sha(document, "template_revision"),
                artifact_digests=_artifact_digests(document),
                review_label=_string(document, "review_label"),
                wss_url=_string(document, "wss_url"),
                expected_wss_hostname=_string(document, "expected_wss_hostname"),
                allowed_wss_paths=_paths(document),
                readiness_digest=_digest(document, "readiness_digest"),
                secret_scan_digest=_digest(document, "secret_scan_digest"),
                ci_registration_digest=_digest(document, "ci_registration_digest"),
                provenance_digest=_digest(document, "provenance_digest"),
                worktree_ownership_digest=_digest(document, "worktree_ownership_digest"),
            )
        except (TypeError, ValueError) as error:
            raise ReadinessError("readiness record has invalid evidence") from error
        return readiness

    @staticmethod
    def _worktree_payloads(worktrees: Sequence[object]) -> dict[str, object]:
        if isinstance(worktrees, (str, bytes)) or not isinstance(worktrees, Sequence):
            raise ReadinessError("readiness record requires three worktrees")
        payloads: dict[str, object] = {}
        for worktree in worktrees:
            try:
                ownership = worktree.ownership
                role = worktree.role
                payload = {
                    "repository": worktree.repository,
                    "branch": worktree.branch,
                    "branch_sha": worktree.branch_sha,
                    "path": str(Path(worktree.path)),
                    "clean": worktree.clean,
                    "ownership": {
                        "generation_id": ownership.generation_id,
                        "generation_root": str(Path(ownership.generation_root)),
                        "handle": ownership.handle,
                        "digest": ownership.digest,
                    },
                }
            except (AttributeError, TypeError, ValueError) as error:
                raise ReadinessError("readiness record worktree evidence is invalid") from error
            if role in payloads or role not in _ROLES or not _valid_worktree_payload(payload):
                raise ReadinessError("readiness record worktree evidence is invalid")
            payloads[role] = payload
        if set(payloads) != set(_ROLES):
            raise ReadinessError("readiness record requires backend, operations, and website worktrees")
        return dict(sorted(payloads.items()))

    @staticmethod
    def _require_verified_reports(
        readiness: ReadinessEvidence, verification_reports: Mapping[str, VerificationReport]
    ) -> None:
        if not isinstance(verification_reports, Mapping) or set(verification_reports) != set(_ROLES):
            raise ReadinessError("readiness requires verified reports for every role")
        for role, report in verification_reports.items():
            if not isinstance(report, VerificationReport) or not report.ready_for_pr:
                raise ReadinessError(f"readiness {role} report is not PR-ready")
            if report.artifact_digest != readiness.artifact_digests.get(role):
                raise ReadinessError(f"readiness {role} artifact digest is not verified")
            if report.source_revision != readiness.provenance_source_revision:
                raise ReadinessError(f"readiness {role} source revision is not verified")

    @staticmethod
    def _validate_record_worktrees(value: object) -> None:
        if not isinstance(value, dict) or set(value) != set(_ROLES) or any(
            not _valid_worktree_payload(payload) for payload in value.values()
        ):
            raise ReadinessError("readiness record worktree evidence is invalid")

    def _atomic_write(self, path: Path, document: Mapping[str, object]) -> None:
        temporary: Path | None = None
        try:
            descriptor, name = tempfile.mkstemp(prefix=".readiness.", dir=path.parent)
            temporary = Path(name)
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(document, handle, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            if path.exists() and path.is_symlink():
                raise ReadinessError("readiness record may not be a symlink")
            os.replace(temporary, path)
            os.chmod(path, 0o600)
            self._require_private_file(path)
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except OSError as error:
            raise ReadinessError("cannot persist readiness record") from error
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _read_document(self, path: Path) -> dict[str, object]:
        self._require_private_file(path)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
            with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
                details = os.fstat(descriptor)
                if not stat.S_ISREG(details.st_mode) or details.st_mode & 0o777 != 0o600:
                    raise ReadinessError("readiness record must be a mode-0600 regular file")
                raw = json.load(handle)
        except ReadinessError:
            raise
        except (OSError, json.JSONDecodeError) as error:
            raise ReadinessError("cannot read readiness record") from error
        if not isinstance(raw, dict):
            raise ReadinessError("readiness record has an invalid schema")
        return raw

    def _ensure_root(self, *, create: bool) -> None:
        self._reject_symlinks(self._state_root_input)
        if create:
            self._state_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self._state_root.is_symlink() or not self._state_root.is_dir() or self._state_root.stat().st_mode & 0o777 != 0o700:
            raise ReadinessError("readiness state root must be private and symlink-free")

    def _ensure_generation_dir(self, generation_id: str, *, create: bool) -> Path:
        path = self.record_path(generation_id).parent
        if path.is_symlink():
            raise ReadinessError("readiness record may not traverse a symlink")
        if create:
            path.mkdir(mode=0o700, exist_ok=True)
        if not path.is_dir() or path.is_symlink() or path.stat().st_mode & 0o777 != 0o700:
            raise ReadinessError("readiness generation directory must be private")
        return path

    @staticmethod
    def _require_private_file(path: Path) -> None:
        if path.is_symlink():
            raise ReadinessError("readiness record may not be a symlink")
        if not path.is_file() or path.stat().st_mode & 0o777 != 0o600:
            raise ReadinessError("readiness record must have mode 0600")

    @staticmethod
    def _reject_symlinks(path: Path) -> None:
        current = path
        while current != current.parent:
            if current.is_symlink():
                raise ReadinessError("readiness record may not traverse a symlink")
            current = current.parent

    @staticmethod
    def _validate_generation_id(value: str) -> None:
        if not isinstance(value, str) or not value or any(char not in "abcdefghijklmnopqrstuvwxyz0123456789-" for char in value):
            raise ReadinessError("readiness generation id is invalid")


def _canonical_digest(document: Mapping[str, object]) -> str:
    payload = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _string(document: Mapping[str, object], key: str) -> str:
    value = document[key]
    if not isinstance(value, str) or not value or len(value) > 2048 or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError(key)
    return value


def _true(document: Mapping[str, object], key: str) -> bool:
    if document[key] is not True:
        raise ValueError(key)
    return True


def _digest(document: Mapping[str, object], key: str) -> str:
    value = _string(document, key)
    if not _DIGEST.fullmatch(value):
        raise ValueError(key)
    return value


def _immutable_sha(document: Mapping[str, object], key: str) -> str:
    value = _string(document, key)
    if not _SHA.fullmatch(value):
        raise ValueError(key)
    return value


def _artifact_digests(document: Mapping[str, object]) -> dict[str, str]:
    value = document["artifact_digests"]
    if not isinstance(value, dict) or set(value) != set(_ROLES) or not all(isinstance(digest, str) and _DIGEST.fullmatch(digest) for digest in value.values()):
        raise ValueError("artifact_digests")
    return dict(value)


def _paths(document: Mapping[str, object]) -> tuple[str, ...]:
    value = document["allowed_wss_paths"]
    if not isinstance(value, list) or not value or any(not isinstance(path, str) or not path.startswith("/") for path in value):
        raise ValueError("allowed_wss_paths")
    return tuple(value)


def _valid_worktree_payload(value: object) -> bool:
    if not isinstance(value, dict) or set(value) != {"repository", "branch", "branch_sha", "path", "clean", "ownership"}:
        return False
    ownership = value["ownership"]
    return (
        all(isinstance(value[name], str) and value[name] for name in ("repository", "branch", "path"))
        and isinstance(value["clean"], bool)
        and isinstance(value["branch_sha"], str) and _SHA.fullmatch(value["branch_sha"]) is not None
        and isinstance(ownership, dict) and set(ownership) == {"generation_id", "generation_root", "handle", "digest"}
        and all(isinstance(ownership[name], str) and ownership[name] for name in ("generation_id", "generation_root", "handle"))
        and isinstance(ownership["digest"], str) and _DIGEST.fullmatch(ownership["digest"]) is not None
    )
