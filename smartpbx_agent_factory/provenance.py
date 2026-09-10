"""Fail-closed immutable provenance and tokenized-template seams."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping


class ProvenanceError(ValueError):
    """Raised when immutable source/image/template evidence is incomplete."""


@dataclass(frozen=True)
class ProvenanceEvidence:
    image_ref: str
    source_revision: str
    image_digest: str
    oci_revision: str


@dataclass(frozen=True)
class TemplateFile:
    template_path: str
    sha256: str


@dataclass(frozen=True)
class TemplateAllowlist:
    template_version: str
    source_revision: str
    image_digest: str
    oci_revision: str
    protocol_version: str
    environment_schema_version: str
    files: Mapping[str, TemplateFile]

    def __post_init__(self) -> None:
        object.__setattr__(self, "files", MappingProxyType(dict(self.files)))


_REVISION_RE = re.compile(r"^[0-9a-f]{40}$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_VARIABLE_RE = re.compile(r"{{\s*([A-Za-z_][A-Za-z0-9_]*)\s*}}")


def validate_source_revision(revision: str) -> str:
    if not isinstance(revision, str) or not _REVISION_RE.fullmatch(revision):
        raise ProvenanceError("source revision must be a full 40-character revision")
    return revision


def validate_image_digest(digest: str) -> str:
    if not isinstance(digest, str) or not _DIGEST_RE.fullmatch(digest):
        raise ProvenanceError("image digest must be an immutable sha256 digest")
    return digest


def _image_evidence(image_ref: str | Mapping[str, object]) -> tuple[str, str | None, str | None]:
    if isinstance(image_ref, str):
        ref = image_ref
        revision = None
        digest = ref.split("@", 1)[1] if "@" in ref else None
    elif isinstance(image_ref, Mapping):
        raw_ref = image_ref.get("image_ref", image_ref.get("ref"))
        if not isinstance(raw_ref, str):
            raise ProvenanceError("image evidence is missing an immutable image reference")
        ref = raw_ref
        raw_revision = image_ref.get("oci_revision", image_ref.get("revision"))
        revision = raw_revision if isinstance(raw_revision, str) else None
        raw_digest = image_ref.get("image_digest", image_ref.get("digest"))
        digest = raw_digest if isinstance(raw_digest, str) else None
        if digest is None and "@" in ref:
            digest = ref.split("@", 1)[1]
    else:
        raise ProvenanceError("image evidence must be a reference or object")
    return ref, revision, digest


def verify_deployed_image_source(
    image_ref: str | Mapping[str, object], expected_revision: str, expected_digest: str
) -> ProvenanceEvidence:
    expected_revision = validate_source_revision(expected_revision)
    expected_digest = validate_image_digest(expected_digest)
    ref, actual_revision, actual_digest = _image_evidence(image_ref)
    if "@" not in ref:
        raise ProvenanceError("image reference must use an immutable digest")
    if actual_digest is None:
        raise ProvenanceError("image evidence is missing an immutable digest")
    actual_digest = validate_image_digest(actual_digest)
    if actual_revision is None:
        raise ProvenanceError("image evidence is missing OCI revision")
    actual_revision = validate_source_revision(actual_revision)
    if actual_digest != expected_digest:
        raise ProvenanceError("image digest does not match expected provenance")
    if actual_revision != expected_revision:
        raise ProvenanceError("OCI revision does not match expected source revision")
    if ref.rsplit("@", 1)[1] != expected_digest:
        raise ProvenanceError("image reference digest does not match expected provenance")
    return ProvenanceEvidence(ref, expected_revision, expected_digest, actual_revision)


def validate_allowlist_metadata(allowlist: Mapping[str, object]) -> TemplateAllowlist:
    if not isinstance(allowlist, Mapping):
        raise ProvenanceError("template allowlist metadata must be an object")
    status = allowlist.get("status")
    if status != "approved":
        raise ProvenanceError("template allowlist provenance is blocked or not approved")
    template_version = allowlist.get("template_version")
    if not isinstance(template_version, str) or not template_version:
        raise ProvenanceError("template allowlist template_version is required")
    source_revision = validate_source_revision(allowlist.get("source_revision"))
    oci_revision = validate_source_revision(allowlist.get("oci_revision"))
    if oci_revision != source_revision:
        raise ProvenanceError("OCI revision does not match source revision")
    image_digest = validate_image_digest(allowlist.get("image_digest"))
    protocol_version = allowlist.get("protocol_version")
    if protocol_version != "smartpbx-ai-provider-v07":
        raise ProvenanceError("template allowlist protocol version is not verified")
    environment_schema_version = allowlist.get("environment_schema_version")
    if (
        not isinstance(environment_schema_version, str)
        or not environment_schema_version
        or environment_schema_version.lower() == "unverified"
    ):
        raise ProvenanceError("template allowlist environment schema is not verified")
    return TemplateAllowlist(
        template_version=template_version,
        source_revision=source_revision,
        image_digest=image_digest,
        oci_revision=oci_revision,
        protocol_version=protocol_version,
        environment_schema_version=environment_schema_version,
        files=_allowlisted_files(allowlist),
    )


def _safe_relative_path(value: object) -> str:
    if not isinstance(value, str) or not value or "\x00" in value or "\\" in value:
        raise ProvenanceError("template allowlist contains an unsafe path")
    path = Path(value)
    normalized = path.as_posix()
    if (
        normalized == "."
        or value != normalized
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ProvenanceError("template allowlist contains an unsafe path")
    return normalized


def _allowlisted_files(allowlist: Mapping[str, object]) -> Mapping[str, TemplateFile]:
    raw_files = allowlist.get("files") if isinstance(allowlist, Mapping) else None
    if not isinstance(raw_files, Mapping):
        raise ProvenanceError("template allowlist files must be an object")
    normalized: dict[str, TemplateFile] = {}
    targets: set[str] = set()
    for raw_source_path, raw_entry in raw_files.items():
        source_path = _safe_relative_path(raw_source_path)
        if not isinstance(raw_entry, Mapping) or set(raw_entry) != {"template_path", "sha256"}:
            raise ProvenanceError(f"template allowlist entry is incomplete for {source_path}")
        template_path = _safe_relative_path(raw_entry["template_path"])
        sha256 = raw_entry["sha256"]
        if not isinstance(sha256, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", sha256):
            raise ProvenanceError(f"invalid template hash for {source_path}")
        if template_path in targets:
            raise ProvenanceError(f"duplicate template path: {template_path}")
        targets.add(template_path)
        normalized[source_path] = TemplateFile(template_path, sha256)
    return normalized


def verify_template_files(path: Path, allowlist: Mapping[str, object]) -> dict[str, str]:
    metadata = validate_allowlist_metadata(allowlist)
    root = Path(path)
    if not root.is_dir():
        raise ProvenanceError("template root must be a directory")
    if root.is_symlink():
        raise ProvenanceError("template root may not be a symlink")
    expected = dict(metadata.files)
    expected_by_target = {entry.template_path: (source_path, entry) for source_path, entry in expected.items()}
    actual: dict[str, Path] = {}
    for candidate in root.rglob("*"):
        relative = candidate.relative_to(root).as_posix()
        if candidate.is_symlink():
            raise ProvenanceError(f"template symlink is not allowed: {relative}")
        if candidate.is_file():
            actual[relative] = candidate
    extras = sorted(set(actual) - set(expected_by_target))
    if extras:
        raise ProvenanceError(f"template file is not allowlisted: {extras[0]}")
    missing = sorted(set(expected_by_target) - set(actual))
    if missing:
        raise ProvenanceError(f"allowlisted template file is missing: {missing[0]}")
    verified: dict[str, str] = {}
    for relative, candidate in actual.items():
        source_path, entry = expected_by_target[relative]
        digest = "sha256:" + hashlib.sha256(candidate.read_bytes()).hexdigest()
        if digest != entry.sha256:
            raise ProvenanceError(f"template source hash drift: {source_path}")
        verified[source_path] = digest
    return verified


def render_template_text(template: str, variables: Mapping[str, object]) -> str:
    if not isinstance(template, str):
        raise ProvenanceError("template must be text")
    if "\x00" in template:
        raise ProvenanceError("template contains NUL")

    def substitute(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in variables:
            raise ProvenanceError(f"unknown variable: {name}")
        value = variables[name]
        if not isinstance(value, (str, int, float, bool)):
            raise ProvenanceError(f"template variable must be scalar: {name}")
        rendered = str(value)
        if "\x00" in rendered:
            raise ProvenanceError(f"template variable contains NUL: {name}")
        return rendered

    rendered = _VARIABLE_RE.sub(substitute, template)
    if "{{" in rendered or "}}" in rendered:
        raise ProvenanceError("template contains unresolved braces")
    return rendered
