"""Validate and stage non-secret Factory Console input for later review.

The console boundary intentionally stops before the factory knowledge builder:
it has no filesystem path, URL-fetch, generation-state, or deployment API.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Iterable, Mapping, Sequence
from urllib.parse import urlsplit

from smartpbx_agent_factory.catalogue import CapabilityCatalogue
from smartpbx_agent_factory.model import AgentManifest
from smartpbx_agent_factory.schema import ManifestError, manifest_digest, parse_manifest


_SOURCE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_INSTRUCTION = re.compile(
    r"(?im)^\s*(?:ignore|disregard|override|system\s+message|assistant\s*:|developer\s*:|"
    r"upload|exfiltrate)\b"
)
_SECRET_FIELD_NAMES = frozenset(
    {
        "apikey",
        "secret",
        "clientsecret",
        "password",
        "credential",
        "credentials",
        "accesstoken",
        "token",
        "authorization",
        "privatekey",
    }
)
_CREDENTIAL_VALUE = re.compile(
    r"(?i)(?:\b(?:bearer|basic)\s+[a-z0-9._~+/-]{12,}|\b(?:api[_-]?key|secret|password|token)\s*[:=]|"
    r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----|\b(?:sk|pk)_[a-z0-9_-]{16,}|\bAIza[\w-]{20,})"
)
_TEXT_TYPES = {".txt": "text/plain", ".md": "text/markdown", ".markdown": "text/markdown"}


@dataclass(frozen=True)
class ValidationIssue:
    """Stable, content-free validation feedback for the console."""

    code: str
    field: str


class IntakeValidationError(ValueError):
    """Raised with structured errors that never include submitted values."""

    def __init__(self, issues: Sequence[ValidationIssue]) -> None:
        self.issues = tuple(issues)
        super().__init__("factory console intake validation failed")

    def as_dict(self) -> dict[str, object]:
        return {"error": "invalid intake", "issues": [issue.__dict__ for issue in self.issues]}


@dataclass(frozen=True)
class KnowledgeUpload:
    """An in-memory text attachment; paths and file objects are not accepted."""

    name: str
    content_type: str
    content: bytes = field(repr=False)
    is_symlink: bool = False


@dataclass(frozen=True)
class StagedKnowledge:
    """Validated bytes retained only for the later approved knowledge handoff."""

    name: str
    content_type: str
    content: bytes = field(repr=False)
    sha256: str = ""

    @property
    def byte_count(self) -> int:
        return len(self.content)


@dataclass(frozen=True)
class ReviewSource:
    """Non-sensitive source evidence bound into the review digest."""

    kind: str
    byte_count: int
    sha256: str


@dataclass(frozen=True)
class IntakeReviewArtifact:
    """Redacted evidence for owner review; source text and PII are excluded."""

    manifest_digest: str
    sources: tuple[ReviewSource, ...]
    enabled_capabilities: tuple[str, ...]
    language_codes: tuple[str, ...]
    digest: str

    def as_dict(self) -> dict[str, object]:
        return {
            "digest": self.digest,
            "manifest_digest": self.manifest_digest,
            "sources": [source.__dict__ for source in self.sources],
            "enabled_capabilities": list(self.enabled_capabilities),
            "language_codes": list(self.language_codes),
        }


@dataclass(frozen=True)
class StagedIntake:
    """The only output of intake, ready for a later explicit review flow."""

    manifest: AgentManifest
    knowledge: tuple[StagedKnowledge, ...]
    manifest_digest: str
    review: IntakeReviewArtifact


class FactoryConsoleIntake:
    """Fail-closed validation before the factory schema and knowledge boundaries."""

    def __init__(
        self,
        *,
        catalogue_path: Path,
        allowed_url_origins: Iterable[str] = (),
        max_source_bytes: int = 256 * 1024,
        max_total_bytes: int = 1024 * 1024,
    ) -> None:
        if not isinstance(max_source_bytes, int) or not isinstance(max_total_bytes, int):
            raise ValueError("intake byte limits must be integers")
        if max_source_bytes <= 0 or max_total_bytes <= 0 or max_source_bytes > max_total_bytes:
            raise ValueError("intake byte limits are invalid")
        self._catalogue = CapabilityCatalogue.load(Path(catalogue_path))
        self._allowed_url_origins = frozenset(_canonical_origin(value) for value in allowed_url_origins)
        self._max_source_bytes = max_source_bytes
        self._max_total_bytes = max_total_bytes

    def stage(self, raw_manifest: Mapping[str, object], uploads: Sequence[KnowledgeUpload]) -> StagedIntake:
        """Return validated, in-memory material without side effects or network access."""
        issues = self._prevalidate_manifest(raw_manifest)
        staged_uploads, upload_issues = self._validate_uploads(uploads)
        issues.extend(upload_issues)
        issues.extend(self._validate_source_declarations(raw_manifest, staged_uploads))
        if issues:
            raise IntakeValidationError(_unique_issues(issues))
        try:
            manifest = parse_manifest(
                raw_manifest,
                approved_source_roots=(Path("/factory-console-intake"),),
                catalogue=self._catalogue,
            )
        except ManifestError:
            raise IntakeValidationError((ValidationIssue("MANIFEST_SCHEMA", "manifest"),)) from None
        digest = manifest_digest(manifest)
        review = self._review(manifest, staged_uploads, digest)
        return StagedIntake(manifest, staged_uploads, digest, review)

    def _prevalidate_manifest(self, raw_manifest: object) -> list[ValidationIssue]:
        if not isinstance(raw_manifest, Mapping):
            return [ValidationIssue("MANIFEST_TYPE", "manifest")]
        issues: list[ValidationIssue] = []
        for key, value in _walk(raw_manifest):
            if _looks_like_secret_field(key):
                issues.append(ValidationIssue("SECRET_FIELD", "manifest"))
            if isinstance(value, str) and _CREDENTIAL_VALUE.search(value):
                issues.append(ValidationIssue("CREDENTIAL_VALUE", "manifest"))
        return issues

    def _validate_uploads(self, uploads: object) -> tuple[tuple[StagedKnowledge, ...], list[ValidationIssue]]:
        if not isinstance(uploads, Sequence) or isinstance(uploads, (str, bytes)):
            return (), [ValidationIssue("UPLOADS_TYPE", "knowledge")]
        total = 0
        names: set[str] = set()
        staged: list[StagedKnowledge] = []
        issues: list[ValidationIssue] = []
        for upload in uploads:
            if not isinstance(upload, KnowledgeUpload):
                issues.append(ValidationIssue("UPLOAD_TYPE", "knowledge"))
                continue
            if not _SOURCE_NAME.fullmatch(upload.name):
                issues.append(ValidationIssue("SOURCE_NAME", "knowledge"))
                continue
            if upload.is_symlink:
                issues.append(ValidationIssue("SYMLINK_SOURCE", "knowledge"))
                continue
            suffix = Path(upload.name).suffix.lower()
            if _TEXT_TYPES.get(suffix) != upload.content_type:
                issues.append(ValidationIssue("UNSUPPORTED_CONTENT", "knowledge"))
                continue
            if not isinstance(upload.content, bytes):
                issues.append(ValidationIssue("CONTENT_TYPE", "knowledge"))
                continue
            if len(upload.content) > self._max_source_bytes:
                issues.append(ValidationIssue("SOURCE_TOO_LARGE", "knowledge"))
                continue
            total += len(upload.content)
            if total > self._max_total_bytes:
                issues.append(ValidationIssue("TOTAL_TOO_LARGE", "knowledge"))
                continue
            if upload.name in names:
                issues.append(ValidationIssue("DUPLICATE_SOURCE", "knowledge"))
                continue
            names.add(upload.name)
            try:
                text = upload.content.decode("utf-8")
            except UnicodeDecodeError:
                issues.append(ValidationIssue("INVALID_TEXT", "knowledge"))
                continue
            if _CREDENTIAL_VALUE.search(text):
                issues.append(ValidationIssue("CREDENTIAL_CONTENT", "knowledge"))
                continue
            if _INSTRUCTION.search(text):
                issues.append(ValidationIssue("INSTRUCTION_CONTENT", "knowledge"))
                continue
            staged.append(StagedKnowledge(upload.name, upload.content_type, upload.content, sha256(upload.content).hexdigest()))
        return tuple(staged), issues

    def _validate_source_declarations(
        self, raw_manifest: Mapping[str, object], uploads: tuple[StagedKnowledge, ...]
    ) -> list[ValidationIssue]:
        if not isinstance(raw_manifest, Mapping):
            return []
        sources = raw_manifest.get("knowledge_sources")
        if not isinstance(sources, Sequence) or isinstance(sources, (str, bytes)):
            return []
        expected_uploads: set[str] = set()
        issues: list[ValidationIssue] = []
        for source in sources:
            if not isinstance(source, Mapping):
                continue
            kind = source.get("kind")
            if kind == "local":
                name = source.get("path")
                if not isinstance(name, str) or not _SOURCE_NAME.fullmatch(name):
                    issues.append(ValidationIssue("SOURCE_PATH", "knowledge_sources"))
                elif name in expected_uploads:
                    issues.append(ValidationIssue("DUPLICATE_SOURCE", "knowledge_sources"))
                else:
                    expected_uploads.add(name)
            elif kind == "url":
                if not self._url_is_allowlisted(source):
                    issues.append(ValidationIssue("URL_ORIGIN", "knowledge_sources"))
        actual_uploads = {item.name for item in uploads}
        if expected_uploads != actual_uploads:
            issues.append(ValidationIssue("SOURCE_BINDING", "knowledge_sources"))
        return issues

    def _url_is_allowlisted(self, source: Mapping[str, object]) -> bool:
        value = source.get("url")
        origins = source.get("approved_origins")
        if not isinstance(value, str) or not isinstance(origins, Sequence) or isinstance(origins, (str, bytes)):
            return False
        try:
            origin = _canonical_origin(value, allow_path=True)
            approved = {_canonical_origin(item) for item in origins if isinstance(item, str)}
        except ValueError:
            return False
        return origin in self._allowed_url_origins and approved and approved <= self._allowed_url_origins and origin in approved

    @staticmethod
    def _review(manifest: AgentManifest, uploads: tuple[StagedKnowledge, ...], digest: str) -> IntakeReviewArtifact:
        by_name = {upload.name: upload for upload in uploads}
        sources = tuple(
            ReviewSource(
                kind=source.kind,
                byte_count=by_name[source.path].byte_count if source.kind == "local" else 0,
                sha256=by_name[source.path].sha256 if source.kind == "local" else sha256((source.url or "").encode()).hexdigest(),
            )
            for source in manifest.knowledge_sources
        )
        payload = {
            "manifest_digest": digest,
            "sources": [source.__dict__ for source in sources],
            "enabled_capabilities": manifest.capabilities.enabled_names,
            "language_codes": tuple(language.code for language in manifest.languages),
        }
        review_digest = sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        return IntakeReviewArtifact(
            manifest_digest=digest,
            sources=sources,
            enabled_capabilities=manifest.capabilities.enabled_names,
            language_codes=tuple(language.code for language in manifest.languages),
            digest=review_digest,
        )


def _walk(value: Mapping[str, object], prefix: str = "") -> Iterable[tuple[str, object]]:
    for key, item in value.items():
        label = f"{prefix}.{key}" if prefix else str(key)
        yield label, item
        if isinstance(item, Mapping):
            yield from _walk(item, label)
        elif isinstance(item, Sequence) and not isinstance(item, (str, bytes)):
            for entry in item:
                if isinstance(entry, Mapping):
                    yield from _walk(entry, label)


def _unique_issues(issues: Iterable[ValidationIssue]) -> tuple[ValidationIssue, ...]:
    return tuple(dict.fromkeys(issues))


def _looks_like_secret_field(label: str) -> bool:
    return any(
        re.sub(r"[^a-z0-9]", "", segment.casefold()) in _SECRET_FIELD_NAMES
        for segment in label.split(".")
    )


def _canonical_origin(value: str, *, allow_path: bool = False) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or (not allow_path and (parsed.path not in {"", "/"} or parsed.query or parsed.fragment))
    ):
        raise ValueError("invalid URL origin")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("invalid URL origin") from exc
    if parsed.query or parsed.fragment:
        raise ValueError("invalid URL origin")
    host = parsed.hostname.lower().rstrip(".")
    return f"https://{host}" if port in {None, 443} else f"https://{host}:{port}"
