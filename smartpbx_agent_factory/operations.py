"""Render encrypted, reviewable operations artifacts without plaintext leaks."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Iterable

from .gitops import WorktreeHandle, WorktreeManager, WorktreeConflictError, manager_owned_worktree_target
from .model import AgentManifest
from .resources import DerivedResources
from .secrets import SecretAudit, SecretError, SecretPlan, SecretProvider


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
        if candidate.is_symlink():
            raise SecretError("operations artifact path may not traverse a symlink")
        if not candidate.is_file():
            continue
        data = candidate.read_bytes()
        if any(value and value in data for value in encoded_values):
            raise SecretLeakError("secret leak detected in generation artifact")
        if _CREDENTIAL_PATTERN.search(data) or _JWT_PATTERN.search(data) or _PEM_MARKER in data:
            raise SecretLeakError("credential-shaped value detected in generation artifact")
        if candidate.name.endswith(".sops.yaml") and b"sops:" not in data:
            raise SecretLeakError("unencrypted SOPS artifact detected")


def _remove_generation_artifacts(root: Path, agent_dir: Path) -> None:
    """Remove precisely one generated agent directory; never clean a parent tree."""
    agents = agent_dir.parent
    if root.is_symlink() or agents.is_symlink() or agent_dir.is_symlink():
        raise SecretError("operations artifact path may not traverse a symlink")
    if agent_dir.exists() and agent_dir.is_dir():
        for candidate in agent_dir.rglob("*"):
            if candidate.is_symlink():
                raise SecretError("operations artifact cleanup may not traverse a symlink")
        shutil.rmtree(agent_dir)


def render_operations_artifacts(
    manifest: AgentManifest,
    resources: DerivedResources,
    provider: SecretProvider,
    *,
    worktree: WorktreeHandle,
    worktree_manager: WorktreeManager,
    secret_plan: SecretPlan,
) -> SecretAudit:
    """Write non-secret metadata and a single SOPS ciphertext document.

    Validation happens before the destination is made.  Any rendering or leak
    failure removes only ``agents/<slug>`` and never touches a repository,
    worktree, or artifact outside this generation.
    """
    try:
        root = manager_owned_worktree_target(worktree_manager, worktree)
    except WorktreeConflictError as exc:
        raise SecretError(str(exc)) from exc
    if root.is_symlink() or not root.is_dir():
        raise SecretError("operations worktree target must be a real directory")
    provider.validate()
    agents = root / "agents"
    if agents.is_symlink() or (agents.exists() and not agents.is_dir()):
        raise SecretError("operations artifact path may not traverse a symlink")
    agent_dir = agents / resources.slug
    if agent_dir.is_symlink():
        raise SecretError("operations artifact path may not traverse a symlink")
    if agent_dir.exists():
        raise SecretError("operations artifact target already exists")
    secret_path = agent_dir / "secrets.sops.yaml"
    metadata_path = agent_dir / "metadata.yaml"
    if not isinstance(secret_plan, SecretPlan):
        raise SecretError("operations renderer requires an exact approved secret plan")
    plan = secret_plan
    created_agent_dir = False
    try:
        agents.mkdir(mode=0o700, exist_ok=True)
        if agents.is_symlink() or not agents.is_dir():
            raise SecretError("operations artifact directory is unsafe")
        agent_dir.mkdir(exist_ok=False, mode=0o700)
        created_agent_dir = True
        values: dict[str, str] = {}
        for requirement in plan.requirements:
            value = (
                provider.generate(requirement.record_id, length=32)
                if requirement.generated
                else provider.fetch(requirement.record_id)
            )
            if not isinstance(value, str) or not value:
                raise SecretError("secret provider returned an invalid secret value")
            values[requirement.runtime_env] = value
        ciphertext = provider.encrypt_yaml(_plaintext_secret_document(values), path=secret_path)
        if not isinstance(ciphertext, bytes) or not ciphertext:
            raise SecretError("secret provider returned invalid ciphertext")
        metadata_path.write_bytes(_metadata(manifest, resources))
        secret_path.write_bytes(ciphertext)
        metadata_path.chmod(0o600)
        secret_path.chmod(0o600)
        _scan_artifact_tree(agent_dir, values.values())
    except Exception:
        if created_agent_dir:
            _remove_generation_artifacts(root, agent_dir)
        raise
    audit = provider.audit_report() if hasattr(provider, "audit_report") else None
    fetched_names = getattr(audit, "fetched_names", ()) if audit is not None else ()
    generated_names = getattr(audit, "generated_names", ()) if audit is not None else ()
    fetched_names = getattr(audit, "fetched_names", ()) if audit is not None else ()
    expected_fetched = tuple(item.record_id for item in plan.requirements if not item.generated)
    expected_generated = tuple(item.record_id for item in plan.requirements if item.generated)
    if tuple(sorted(set(fetched_names))) != tuple(sorted(expected_fetched)) or tuple(sorted(set(generated_names))) != tuple(sorted(expected_generated)):
        raise SecretError("secret provider audit does not exactly cover the secret plan")
    return SecretAudit(
        fetched_names=fetched_names,
        generated_names=generated_names,
        ciphertext_paths=(secret_path.relative_to(root),),
        plaintext_paths=(),
    )
