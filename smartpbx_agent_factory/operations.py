"""Render encrypted, reviewable operations artifacts without plaintext leaks."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Iterable

from .model import AgentManifest
from .resources import DerivedResources
from .secrets import SecretAudit, SecretError, SecretProvider


class SecretLeakError(SecretError):
    """Raised after removing generation-owned files that contained secret material."""


_CREDENTIAL_PATTERN = re.compile(
    rb"(?:api[_-]?key|authorization|password|secret)\s*[:=]\s*[^\s#]{8,}", re.IGNORECASE
)
_JWT_PATTERN = re.compile(rb"eyJ[a-zA-Z0-9_-]{8,}\.[a-zA-Z0-9_-]{8,}\.[a-zA-Z0-9_-]{8,}")
_PEM_MARKER = b"-----BEGIN"


def _yaml_scalar(value: object) -> str:
    """JSON quoting is valid YAML scalar syntax and keeps metadata deterministic."""
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"))


def _metadata(manifest: AgentManifest, resources: DerivedResources) -> bytes:
    lines = (
        ("schema_version", 1),
        ("slug", resources.slug),
        ("smartpbx_hostname", resources.smartpbx_hostname),
        ("website_hostname", resources.website_hostname),
        ("smartpbx_port", resources.smartpbx_port),
        ("website_port", resources.website_port),
        ("image_repository", resources.ghcr_repository),
        ("ci_identifier", resources.ci_identifier),
        ("wss_header_name", resources.wss_header),
        ("smartpbx_account_id_present", bool(manifest.smartpbx.account_id)),
        ("rotation_due", manifest.operations.rotation_due),
    )
    return ("".join(f"{key}: {_yaml_scalar(value)}\n" for key, value in lines)).encode("utf-8")


def _plaintext_secret_document(values: dict[str, str]) -> bytes:
    return ("".join(f"{key}: {_yaml_scalar(value)}\n" for key, value in sorted(values.items()))).encode("utf-8")


def _scan_artifact_tree(root: Path, secret_values: Iterable[str]) -> None:
    encoded_values = tuple(value.encode("utf-8") for value in secret_values)
    for candidate in root.rglob("*"):
        if not candidate.is_file():
            continue
        data = candidate.read_bytes()
        if any(value and value in data for value in encoded_values):
            raise SecretLeakError("secret leak detected in generation artifact")
        if _CREDENTIAL_PATTERN.search(data) or _JWT_PATTERN.search(data) or _PEM_MARKER in data:
            raise SecretLeakError("credential-shaped value detected in generation artifact")
        if candidate.name.endswith(".sops.yaml") and b"sops:" not in data:
            raise SecretLeakError("unencrypted SOPS artifact detected")


def _remove_generation_artifacts(agent_dir: Path) -> None:
    """Remove precisely one generated agent directory; never clean a parent tree."""
    if agent_dir.exists():
        shutil.rmtree(agent_dir)


def render_operations_artifacts(
    manifest: AgentManifest,
    resources: DerivedResources,
    provider: SecretProvider,
    output_dir: Path,
) -> SecretAudit:
    """Write non-secret metadata and a single SOPS ciphertext document.

    Validation happens before the destination is made.  Any rendering or leak
    failure removes only ``agents/<slug>`` and never touches a repository,
    worktree, or artifact outside this generation.
    """
    provider.validate()
    if not isinstance(output_dir, Path):
        raise SecretError("output directory must be a Path")
    root = output_dir.resolve()
    agent_dir = root / "agents" / resources.slug
    secret_path = agent_dir / "secrets.sops.yaml"
    metadata_path = agent_dir / "metadata.yaml"
    secret_name = f"{resources.slug}/wss_token"
    try:
        agent_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
        value = provider.generate(secret_name, length=32)
        if not isinstance(value, str) or not value:
            raise SecretError("secret provider returned an invalid generated value")
        ciphertext = provider.encrypt_yaml(_plaintext_secret_document({"wss_token": value}), path=secret_path)
        if not isinstance(ciphertext, bytes) or not ciphertext:
            raise SecretError("secret provider returned invalid ciphertext")
        metadata_path.write_bytes(_metadata(manifest, resources))
        secret_path.write_bytes(ciphertext)
        metadata_path.chmod(0o600)
        secret_path.chmod(0o600)
        _scan_artifact_tree(agent_dir, (value,))
    except Exception:
        _remove_generation_artifacts(agent_dir)
        raise
    audit = provider.audit_report() if hasattr(provider, "audit_report") else None
    fetched_names = getattr(audit, "fetched_names", ()) if audit is not None else ()
    generated_names = getattr(audit, "generated_names", (secret_name,)) if audit is not None else (secret_name,)
    if not generated_names:
        generated_names = (secret_name,)
    return SecretAudit(
        fetched_names=fetched_names,
        generated_names=generated_names,
        ciphertext_paths=(secret_path.relative_to(root),),
        plaintext_paths=(),
    )
