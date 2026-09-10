"""Fail-closed immutable provenance and tokenized-template seams."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


class ProvenanceError(ValueError):
    """Raised when immutable source/image/template evidence is incomplete."""


@dataclass(frozen=True)
class ProvenanceEvidence:
    image_ref: str
    source_revision: str
    image_digest: str
    oci_revision: str


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


def _allowlisted_files(allowlist: Mapping[str, object]) -> Mapping[str, str]:
    raw_files = allowlist.get("files", allowlist) if isinstance(allowlist, Mapping) else None
    if not isinstance(raw_files, Mapping):
        raise ProvenanceError("template allowlist files must be an object")
    normalized: dict[str, str] = {}
    for raw_path, raw_hash in raw_files.items():
        if not isinstance(raw_path, str) or not raw_path or Path(raw_path).is_absolute() or ".." in Path(raw_path).parts:
            raise ProvenanceError("template allowlist contains an unsafe path")
        if not isinstance(raw_hash, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", raw_hash):
            raise ProvenanceError(f"invalid template hash for {raw_path}")
        normalized[raw_path.replace("\\", "/")] = raw_hash
    return normalized


def verify_template_files(path: Path, allowlist: Mapping[str, object]) -> dict[str, str]:
    root = Path(path)
    if not root.is_dir():
        raise ProvenanceError("template root must be a directory")
    if root.is_symlink():
        raise ProvenanceError("template root may not be a symlink")
    expected = dict(_allowlisted_files(allowlist))
    actual: dict[str, Path] = {}
    for candidate in root.rglob("*"):
        relative = candidate.relative_to(root).as_posix()
        if candidate.is_symlink():
            raise ProvenanceError(f"template symlink is not allowed: {relative}")
        if candidate.is_file():
            actual[relative] = candidate
    extras = sorted(set(actual) - set(expected))
    if extras:
        raise ProvenanceError(f"template file is not allowlisted: {extras[0]}")
    missing = sorted(set(expected) - set(actual))
    if missing:
        raise ProvenanceError(f"allowlisted template file is missing: {missing[0]}")
    verified: dict[str, str] = {}
    for relative, candidate in actual.items():
        digest = "sha256:" + hashlib.sha256(candidate.read_bytes()).hexdigest()
        if digest != expected[relative]:
            raise ProvenanceError(f"template hash drift: {relative}")
        verified[relative] = digest
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
