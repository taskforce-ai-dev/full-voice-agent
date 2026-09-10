"""Digest-bound, resumable factory transaction coordination."""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import subprocess
import tempfile
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from .catalogue import CapabilityCatalogue
from .gitops import DirtyWorktreeError, WorktreeConflictError, WorktreeHandle, WorktreeManager
from .knowledge import (
    KnowledgeBuilder,
    KnowledgeBuilderImpl,
    KnowledgeConflict,
    KnowledgeDocument,
    KnowledgeError,
    KnowledgeFact,
    KnowledgeReview,
    recompute_knowledge_review_digest,
)
from .provenance import ProvenanceError, validate_allowlist_metadata
from .readiness import ReadinessAuthority, ReadinessError, ReadinessEvidence
from .render import IncompleteTemplateError, render_backend
from .operations import render_operations_artifacts
from .website import render_website_artifacts
from .resources import AllocationRegistry, DerivedResources, ResourceConflict, derive_resources
from .schema import ManifestError, manifest_digest, parse_manifest
from .secrets import SecretAudit, SecretPlan, SecretProvider, derive_secret_plan
from .state import GenerationState, Stage, StateError
from .verify import VerificationReport


class GenerationError(RuntimeError):
    """Base error for factory transaction failures."""


class GenerationBlockedError(GenerationError):
    """Raised for a fail-closed approval or digest gate."""


class GenerationInfrastructureError(GenerationError):
    """Raised when local factory state cannot be read or written."""


@dataclass(frozen=True)
class PlanReport:
    generation_id: str
    state: GenerationState
    manifest_digest: str
    knowledge_digest: str
    resource_digest: str
    plan_digest: str
    rendered_plan: str


class InventoryProvider(Protocol):
    """Read-only external collision inventory seam; it never allocates."""

    def snapshot(self) -> Mapping[str, object]: ...


@dataclass(frozen=True)
class RepositoryOwnedCIVerificationCoordinator:
    """Concrete capability reserved for the repository-owned CI result adapter.

    It intentionally cannot accept readiness booleans or caller-built reports.
    Until a CI result adapter is installed, verification is pending and the
    generation remains in ``GENERATED``.
    """

    ci_result_adapter: object | None = None

    def verify(self, *, generation_id: str, resources: DerivedResources, lane_records: Mapping[str, Mapping[str, str]]) -> tuple[ReadinessEvidence, Mapping[str, VerificationReport]]:
        adapter = self.ci_result_adapter
        verify = getattr(adapter, "verify", None)
        if not callable(verify):
            raise GenerationBlockedError("repository-owned CI lifecycle result is pending")
        result = verify(generation_id=generation_id, resources=resources, lane_records=lane_records)
        if not isinstance(result, tuple) or len(result) != 2:
            raise GenerationBlockedError("repository-owned CI result is invalid")
        return result

    def worktrees_for(self, *, generation_id: str, inventory: object, readiness: ReadinessEvidence) -> tuple[object, ...]:
        """Ask the same repository-owned adapter to bind CI proof to PR sources."""
        build = getattr(self.ci_result_adapter, "worktrees_for", None)
        if not callable(build):
            raise GenerationBlockedError("repository-owned CI worktree binding is pending")
        worktrees = build(generation_id=generation_id, inventory=inventory, readiness=readiness)
        if not isinstance(worktrees, tuple):
            raise GenerationBlockedError("repository-owned CI worktree binding is invalid")
        return worktrees


@dataclass(frozen=True)
class LaneBinding:
    """One immutable remote/revision/target plus its owning manager."""

    manager: object
    primary: Path
    remote: str
    revision: str
    target: Path

    def __post_init__(self) -> None:
        if self.manager is None or not all(isinstance(item, Path) and item.is_absolute() for item in (self.primary, self.target)):
            raise ValueError("lane binding requires manager and absolute paths")
        if not isinstance(self.remote, str) or not self.remote or not isinstance(self.revision, str) or len(self.revision) != 40 or set(self.revision) - set("0123456789abcdef"):
            raise ValueError("lane binding requires immutable remote revision")


@dataclass(frozen=True)
class GenerationBinding:
    """The sole transaction config for backend, private operations and website."""

    backend: LaneBinding
    operations: LaneBinding
    website: LaneBinding
    release_allowed: bool = False

    def __post_init__(self) -> None:
        if self.release_allowed is not False:
            raise ValueError("factory generation is review-only")
        lanes = (self.backend, self.operations, self.website)
        if not all(isinstance(lane, LaneBinding) for lane in lanes):
            raise ValueError("three lane bindings are required")
        if len({lane.target for lane in lanes}) != 3:
            raise ValueError("lane targets must be distinct")


@dataclass(frozen=True)
class _StoredGeneration:
    state: GenerationState
    manifest_path: Path
    resources: DerivedResources
    knowledge_digest: str
    resource_digest: str
    plan_digest: str
    cleanup_inventory: "CleanupInventory | None"
    knowledge_review: KnowledgeReview | None = None
    plan_artifact: Mapping[str, object] | None = None
    binding: Mapping[str, Mapping[str, str]] | None = None
    sealed_secret: "SealedSecretBundle | None" = None


@dataclass(frozen=True)
class CleanupInventory:
    """Persisted identifiers for generation-owned cleanup, never broad paths."""

    worktrees: tuple[WorktreeHandle, ...] = ()
    plaintext_paths: tuple[Path, ...] = ()
    completed_worktree_targets: tuple[Path, ...] = ()
    completed_plaintext_paths: tuple[Path, ...] = ()
    completed: bool = False
    sealed_ciphertext_paths: tuple[Path, ...] = ()
    completed_sealed_ciphertext_paths: tuple[Path, ...] = ()


@dataclass(frozen=True)
class SealedSecretBundle:
    """Durable non-secret reference to the only ciphertext generated by a run."""

    path: Path
    digest: str
    record_ids: tuple[str, ...]
    runtime_env_names: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.path.is_absolute() or len(self.digest) != 64 or set(self.digest) - set("0123456789abcdef"):
            raise GenerationInfrastructureError("sealed secret bundle is invalid")
        if not self.record_ids or len(set(self.record_ids)) != len(self.record_ids) or len(set(self.runtime_env_names)) != len(self.runtime_env_names):
            raise GenerationInfrastructureError("sealed secret bundle inventory is invalid")


class GenerationOrchestrator:
    """Own state transitions; rendering and PR creation stay separate lanes."""

    def __init__(
        self,
        state_root: Path,
        *,
        catalogue_path: Path | None = None,
        worktree_manager_factory: Callable[[Path], WorktreeManager] = WorktreeManager,
        knowledge_builder_factory: Callable[[Path], KnowledgeBuilder] | None = None,
        inventory_provider: InventoryProvider | None = None,
        verification_coordinator: RepositoryOwnedCIVerificationCoordinator | None = None,
        pr_coordinator: object | None = None,
    ) -> None:
        if not isinstance(state_root, Path) or not state_root.is_absolute():
            raise GenerationInfrastructureError("state root must be an absolute path")
        self._state_root_input = state_root
        self._reject_symlink_traversal(state_root)
        self._state_root = state_root.resolve()
        root = Path(__file__).parent
        self._catalogue_path = (catalogue_path or root / "template_v1" / "provider_catalogue.json").resolve()
        self._worktree_manager_factory = worktree_manager_factory
        self._knowledge_builder_factory = knowledge_builder_factory or (
            lambda approved_root: KnowledgeBuilderImpl(approved_source_roots=(approved_root,))
        )
        self._inventory_provider = inventory_provider
        self._verification_coordinator = verification_coordinator
        self._pr_coordinator = pr_coordinator

    def inspect(self, manifest_path: Path) -> Mapping[str, object]:
        """Report non-mutating prerequisite status; no target checkout is touched."""
        manifest_path = self._manifest_path(manifest_path)
        factory_root = Path(__file__).resolve().parents[1]
        checks = {
            "manifest": _check(manifest_path.is_file(), "manifest unavailable"),
            "catalogue": _check(self._catalogue_path.is_file(), "catalogue unavailable"),
            "source_repository": _git_check(factory_root, ("rev-parse", "--is-inside-work-tree")),
            "origin_main": _git_check(factory_root, ("rev-parse", "--verify", "origin/main^{commit}")),
            "dirty_tree_overlap": _git_clean_check(factory_root),
            "git": _check(_available_binary("git"), "git unavailable"),
            "docker": _check(_available_binary("docker"), "docker unavailable"),
            "gh": _check(_available_binary("gh"), "gh unavailable"),
            "sops": _check(_available_binary("sops"), "sops unavailable"),
            "age": _check(_available_binary("age"), "age unavailable"),
            "template_provenance": self._inspect_provenance(),
            "operations_prerequisites": self._inspect_operations_prerequisites(),
        }
        return {"ok": all(value == "ready" for value in checks.values()), "checks": checks}

    def plan(self, manifest_path: Path) -> PlanReport:
        manifest_path = self._manifest_path(manifest_path)
        try:
            raw = json.loads(manifest_path.read_text(encoding="utf-8"))
            if not isinstance(raw, Mapping):
                raise ManifestError("manifest must be an object")
            catalogue = CapabilityCatalogue.load(self._catalogue_path)
            manifest = parse_manifest(
                raw,
                approved_source_roots=(manifest_path.parent,),
                catalogue=catalogue,
            )
        except (OSError, json.JSONDecodeError, ManifestError) as error:
            raise GenerationBlockedError(str(error)) from error
        digest = manifest_digest(manifest)
        knowledge_digest = _digest_payload({"sources": [asdict(source) for source in manifest.knowledge_sources]})
        with self._allocation_lock():
            try:
                resources = derive_resources(manifest, self._reserved_resources())
            except ResourceConflict as error:
                raise GenerationBlockedError(f"resource allocation conflict: {error}") from error
            resource_digest = _digest_payload(asdict(resources))
            artifact = _canonical_plan_artifact(manifest.slug, resources, digest, knowledge_digest, resource_digest)
            plan_digest = _digest_payload(artifact)
            artifact = {**artifact, "digest": plan_digest}
            state = GenerationState.start(f"gen-{uuid.uuid4().hex}", digest)
            state.transition(Stage.INPUT_COLLECTED)
            stored = _StoredGeneration(
                state, manifest_path, resources, knowledge_digest, resource_digest, plan_digest, CleanupInventory(), None, artifact
            )
            self._save(stored)
        return PlanReport(
            generation_id=state.generation_id,
            state=state,
            manifest_digest=digest,
            knowledge_digest=knowledge_digest,
            resource_digest=resource_digest,
            plan_digest=plan_digest,
            rendered_plan=_redacted_plan(state.generation_id, manifest.slug, resources, digest, knowledge_digest, resource_digest, plan_digest),
        )

    def generate(
        self,
        generation_id: str,
        *,
        binding: GenerationBinding | None = None,
        secret_provider: SecretProvider | None = None,
    ) -> GenerationState:
        """Render/commit/checkpoint each lane; resumed runs skip valid checkpoints."""
        stored = self._load_verified(generation_id)
        if stored.state.stage is Stage.INPUT_COLLECTED:
            if secret_provider is None:
                raise GenerationBlockedError("secret resolution is required before knowledge review")
            self.record_secrets_resolved(generation_id, provider=secret_provider)
            stored = self._load_verified(generation_id)
        if stored.state.stage is Stage.KNOWLEDGE_REVIEW_REQUIRED:
            raise GenerationBlockedError("knowledge approval is required before generation")
        if stored.state.stage is Stage.PLAN_REVIEW_REQUIRED:
            raise GenerationBlockedError("plan approval is required before generation")
        if stored.state.stage is not Stage.GENERATED:
            raise GenerationBlockedError(f"generation cannot start from {stored.state.stage.value}")
        manifest = self._current_manifest(stored)
        review = self._approved_knowledge_review(stored)
        self._require_complete_runtime_template()
        if binding is None:
            raise GenerationBlockedError("an immutable three-lane generation binding is required")
        serialized_binding = _serialize_generation_binding(binding)
        if stored.binding and stored.binding != serialized_binding:
            raise GenerationBlockedError("generation binding differs from its persisted immutable binding")
        if not stored.binding:
            stored = _StoredGeneration(
                stored.state, stored.manifest_path, stored.resources, stored.knowledge_digest,
                stored.resource_digest, stored.plan_digest, stored.cleanup_inventory,
                stored.knowledge_review, stored.plan_artifact, serialized_binding,
                stored.sealed_secret,
            )
            self._save(stored)
        try:
            handles = {
                role: self._create_or_reuse_lane(generation_id, lane)
                for role, lane in (("backend", binding.backend), ("operations", binding.operations), ("website", binding.website))
            }
        except (DirtyWorktreeError, WorktreeConflictError) as error:
            raise GenerationBlockedError("generation worktree creation failed") from error
        # All targets now exist and are manager-owned before any renderer can write.
        try:
            stored = self._load_verified(generation_id)
            if not self._checkpoint_is_valid(stored, "backend", handles["backend"]):
                backend_report = render_backend(
                    manifest, review, stored.resources, handles["backend"],
                    worktree_manager=binding.backend.manager, state=stored.state,
                )
                handles["backend"] = self._commit_and_checkpoint(
                    generation_id, "backend", binding.backend.manager, handles["backend"],
                    (Path("SmartPBX Agents") / stored.resources.slug,), artifact_digest=backend_report.artifact_digest,
                )
            stored = self._load_verified(generation_id)
            backend_digest = stored.state.lane_records["backend"]["artifact_digest"]
            if not self._checkpoint_is_valid(stored, "operations", handles["operations"]):
                secret_plan = derive_secret_plan(manifest, stored.resources, CapabilityCatalogue.load(self._catalogue_path))
                sealed_ciphertext = self._sealed_ciphertext(stored)
                audit = render_operations_artifacts(
                    manifest, stored.resources,
                    worktree=handles["operations"], worktree_manager=binding.operations.manager,
                    secret_plan=secret_plan, sealed_ciphertext=sealed_ciphertext,
                )
                handles["operations"] = self._commit_and_checkpoint(
                    generation_id, "operations", binding.operations.manager, handles["operations"],
                    (Path("agents") / stored.resources.slug,),
                    ciphertext_reference=",".join(
                        sorted(Path(item).as_posix() for item in audit.ciphertext_paths)
                    ),
                )
            stored = self._load_verified(generation_id)
            if not self._checkpoint_is_valid(stored, "website", handles["website"]):
                render_website_artifacts(
                    manifest, stored.resources, backend_artifact_digest=backend_digest,
                    backend_branch_sha=handles["backend"].revision,
                    worktree=handles["website"], worktree_manager=binding.website.manager,
                )
                handles["website"] = self._commit_and_checkpoint(
                    generation_id, "website", binding.website.manager, handles["website"],
                    (Path("data") / "smartpbx-agents.generated.mjs", Path("scripts") / "validate-smartpbx-card.mjs", Path("components") / "pages" / "BookDemo.tsx", Path("package.json")),
                )
        except IncompleteTemplateError as error:
            raise GenerationBlockedError(str(error)) from error
        except GenerationBlockedError:
            raise
        except Exception as error:
            raise GenerationBlockedError("review transaction renderer failed") from error
        return self._load_verified(generation_id).state

    def resume(
        self,
        generation_id: str,
        *,
        knowledge_approval: str | None = None,
        plan_approval: str | None = None,
        binding: GenerationBinding | None = None,
        secret_provider: SecretProvider | None = None,
    ) -> GenerationState:
        stored = self._load_verified(generation_id)
        state = stored.state
        try:
            if state.stage is Stage.INPUT_COLLECTED:
                if secret_provider is None:
                    raise GenerationBlockedError("secret resolution is required before knowledge review")
                self.record_secrets_resolved(generation_id, provider=secret_provider)
                stored = self._load_verified(generation_id)
                state = stored.state
            if knowledge_approval is not None:
                state.approve_knowledge(knowledge_approval)
                state.record_plan_digest(stored.plan_digest)
            if plan_approval is not None:
                state.approve_plan(plan_approval)
        except StateError as error:
            raise GenerationBlockedError(str(error)) from error
        self._save(stored)
        if state.stage is Stage.GENERATED and binding is not None:
            return self.generate(generation_id, binding=binding, secret_provider=secret_provider)
        return state

    def _create_or_reuse_lane(self, generation_id: str, lane: LaneBinding) -> WorktreeHandle:
        """Create once, then revalidate the recorded exact target on resume."""
        stored = self._load_verified(generation_id)
        inventory = stored.cleanup_inventory
        if inventory is None:
            raise GenerationBlockedError("generation cleanup inventory is unavailable")
        existing = next((item for item in inventory.worktrees if item.target == lane.target), None)
        if existing is not None:
            if (existing.primary, existing.target) != (lane.primary.resolve(), lane.target.resolve()):
                raise GenerationBlockedError("recorded lane binding does not match immutable generation binding")
            revalidate = getattr(lane.manager, "reuse_recorded", None)
            if not callable(revalidate):
                raise GenerationBlockedError("recorded worktree requires manager revalidation")
            return revalidate(existing)
        create = getattr(lane.manager, "create", None)
        if not callable(create):
            raise GenerationBlockedError("lane manager cannot create worktrees")
        handle = create(
            primary=lane.primary, remote=lane.remote, revision=lane.revision, target=lane.target,
            branch=f"smartpbx-agent-factory/{generation_id}",
        )
        if not isinstance(handle, WorktreeHandle):
            raise GenerationBlockedError("lane manager returned an invalid worktree handle")
        self.record_owned_worktree(generation_id, lane.manager, handle)
        return handle

    def _checkpoint_is_valid(self, stored: _StoredGeneration, role: str, handle: WorktreeHandle) -> bool:
        record = stored.state.lane_records.get(role)
        if record is None:
            return False
        if record.get("head_sha") != handle.revision or record.get("output_digest") != _tree_digest(handle.target):
            raise GenerationBlockedError("recorded lane checkpoint no longer matches owned worktree")
        return True

    def _commit_and_checkpoint(
        self, generation_id: str, role: str, manager: object, handle: WorktreeHandle,
        allowed_paths: tuple[Path, ...], *, ciphertext_reference: str = "", artifact_digest: str | None = None,
    ) -> WorktreeHandle:
        commit = getattr(manager, "stage_and_commit", None)
        if not callable(commit):
            raise GenerationBlockedError("lane manager cannot create a review commit")
        updated = commit(handle, allowed_paths=allowed_paths, message=f"factory({role}): review generation {generation_id}")
        if not isinstance(updated, WorktreeHandle):
            raise GenerationBlockedError("lane manager did not return an authoritative committed handle")
        self.record_owned_worktree(generation_id, manager, updated)
        stored = self._load_verified(generation_id)
        output_digest = _tree_digest(updated.target)
        canonical_artifact = artifact_digest or output_digest
        if not isinstance(canonical_artifact, str) or len(canonical_artifact) != 64 or set(canonical_artifact) - set("0123456789abcdef"):
            raise GenerationBlockedError("lane renderer did not return a canonical artifact digest")
        stored.state.record_lane(role, output_digest=output_digest, head_sha=updated.revision, artifact_digest=canonical_artifact, ciphertext_reference=ciphertext_reference)
        self._save(stored)
        return updated

    def record_secrets_resolved(
        self, generation_id: str, *, provider: SecretProvider
    ) -> GenerationState:
        """Resolve the exact plan once and persist only a sealed ciphertext bundle."""
        if provider is None or not all(hasattr(provider, name) for name in ("validate", "fetch", "generate", "encrypt_yaml", "audit_report")):
            raise GenerationBlockedError("validated SecretProvider seal capability is required")
        stored = self._load_verified(generation_id)
        state = stored.state
        if state.stage is not Stage.INPUT_COLLECTED:
            raise GenerationBlockedError("secret resolution requires stage INPUT_COLLECTED")
        manifest = self._current_manifest(stored)
        secret_plan = derive_secret_plan(
            manifest, stored.resources, CapabilityCatalogue.load(self._catalogue_path)
        )
        try:
            bundle, audit = self._seal_secret_bundle(stored, secret_plan, provider)
            expected_names = {item.record_id for item in secret_plan.requirements}
            audited_names = audit.fetched_names + audit.generated_names
            if len(audited_names) != len(set(audited_names)) or set(audited_names) != expected_names:
                raise GenerationBlockedError("secret audit names do not exactly match manifest requirements")
            inventory = stored.cleanup_inventory
            if inventory is None or inventory.completed:
                raise GenerationBlockedError("sealed ciphertext cleanup ownership is unavailable")
            stored = _replace_cleanup_inventory(
                stored,
                CleanupInventory(
                    inventory.worktrees,
                    inventory.plaintext_paths,
                    inventory.completed_worktree_targets,
                    inventory.completed_plaintext_paths,
                    sealed_ciphertext_paths=inventory.sealed_ciphertext_paths + (bundle.path,),
                    completed_sealed_ciphertext_paths=inventory.completed_sealed_ciphertext_paths,
                ),
            )
            review = self._build_knowledge_review(stored, manifest)
            stored = _replace_knowledge_review(stored, review, sealed_secret=bundle)
            audit_digest = _digest_payload(
                {
                    "fetched_names": audit.fetched_names,
                    "generated_names": audit.generated_names,
                    "ciphertext_paths": tuple(sorted(Path(item).as_posix() for item in audit.ciphertext_paths)),
                    "secret_plan": secret_plan.digest_payload(),
                    "sealed_ciphertext_digest": bundle.digest,
                }
            )
            state.record_stage_digest("secrets", audit_digest)
            state.transition(Stage.SECRETS_RESOLVED)
            state.transition(Stage.KNOWLEDGE_REVIEW_REQUIRED)
            state.record_knowledge_review_digest(review.digest)
            self._save(stored)
        except (GenerationError, StateError) as error:
            if "bundle" in locals():
                self._rollback_unrecorded_sealed_bundle(generation_id, bundle)
            raise GenerationBlockedError(str(error)) from error
        except Exception as error:
            if "bundle" in locals():
                self._rollback_unrecorded_sealed_bundle(generation_id, bundle)
            raise GenerationBlockedError("secret resolution checkpoint failed") from error
        return state

    def _seal_secret_bundle(
        self, stored: _StoredGeneration, secret_plan: SecretPlan, provider: SecretProvider
    ) -> tuple[SealedSecretBundle, SecretAudit]:
        """Fetch/generate exactly once, immediately encrypt, then drop plaintext."""
        root = self._sealed_root(stored.state.generation_id)
        path = root / "secrets.sops.yaml"
        temporary_path: Path | None = None
        created_root = False
        values: dict[str, str] = {}
        try:
            self._ensure_state_root(create=True)
            base = root.parent
            if base.is_symlink():
                raise GenerationInfrastructureError("sealed secret bundle root may not be a symlink")
            base.mkdir(mode=0o700, exist_ok=True)
            if base.is_symlink() or not base.is_dir() or base.stat().st_mode & 0o777 != 0o700:
                raise GenerationInfrastructureError("sealed secret bundle parent must be private")
            if root.exists():
                self._remove_exact_sealed_bundle(stored.state.generation_id, path)
            root.mkdir(mode=0o700)
            created_root = True
            if root.is_symlink() or root.stat().st_mode & 0o777 != 0o700:
                raise GenerationInfrastructureError("sealed secret bundle directory must be private")
            provider.validate()
            for requirement in secret_plan.requirements:
                value = provider.generate(requirement.record_id, length=32) if requirement.generated else provider.fetch(requirement.record_id)
                if not isinstance(value, str) or not value:
                    raise GenerationBlockedError("secret provider returned no value")
                values[requirement.runtime_env] = value
            plaintext = ("".join(f"{name}: {json.dumps(value, ensure_ascii=True)}\n" for name, value in sorted(values.items()))).encode("utf-8")
            descriptor, temporary_name = tempfile.mkstemp(prefix=".sealed-", dir=root)
            temporary_path = Path(temporary_name)
            os.fchmod(descriptor, 0o600)
            os.close(descriptor)
            ciphertext = provider.encrypt_yaml(plaintext, path=temporary_path)
            values.clear()
            del plaintext
            if not isinstance(ciphertext, bytes) or not ciphertext or b"sops:" not in ciphertext:
                raise GenerationBlockedError("secret provider did not return SOPS ciphertext")
            with temporary_path.open("wb") as sealed_file:
                sealed_file.write(ciphertext)
                sealed_file.flush()
                os.fsync(sealed_file.fileno())
            if temporary_path.is_symlink() or temporary_path.stat().st_mode & 0o777 != 0o600:
                raise GenerationInfrastructureError("sealed secret bundle temporary file must be private")
            os.replace(temporary_path, path)
            temporary_path = None
            self._fsync_directory(root)
            if path.is_symlink() or path.stat().st_mode & 0o777 != 0o600:
                raise GenerationInfrastructureError("sealed secret bundle must be private")
            audit = provider.audit_report()
        except GenerationError:
            if created_root:
                self._remove_exact_sealed_bundle(stored.state.generation_id, path)
            raise
        except Exception as error:
            if created_root:
                self._remove_exact_sealed_bundle(stored.state.generation_id, path)
            raise GenerationBlockedError("secret resolution and sealing failed") from error
        finally:
            values.clear()
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
        if not isinstance(audit, SecretAudit):
            self._remove_exact_sealed_bundle(stored.state.generation_id, path)
            raise GenerationBlockedError("SecretProvider audit is invalid")
        return (
            SealedSecretBundle(
                path, hashlib.sha256(ciphertext).hexdigest(),
                tuple(item.record_id for item in secret_plan.requirements),
                secret_plan.operations_env_names,
            ),
            audit,
        )

    def _sealed_ciphertext(self, stored: _StoredGeneration) -> bytes:
        bundle = stored.sealed_secret
        if not isinstance(bundle, SealedSecretBundle):
            raise GenerationBlockedError("sealed secret bundle is required before operations rendering")
        root = self._state_root / "sealed-secrets" / stored.state.generation_id
        if root.is_symlink() or bundle.path.is_symlink() or bundle.path.parent != root or not bundle.path.is_file():
            raise GenerationBlockedError("sealed secret bundle is missing or unsafe")
        if bundle.path.stat().st_mode & 0o777 != 0o600:
            raise GenerationBlockedError("sealed secret bundle is not private")
        ciphertext = bundle.path.read_bytes()
        if hashlib.sha256(ciphertext).hexdigest() != bundle.digest or b"sops:" not in ciphertext:
            raise GenerationBlockedError("sealed secret bundle digest changed")
        return ciphertext

    def _sealed_root(self, generation_id: str) -> Path:
        return self._state_root / "sealed-secrets" / generation_id

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _remove_exact_sealed_bundle(self, generation_id: str, path: Path) -> None:
        """Remove only the recorded ciphertext file and an empty owned root."""
        root = self._sealed_root(generation_id)
        base = root.parent
        expected = root / "secrets.sops.yaml"
        if path != expected or base.is_symlink() or root.is_symlink() or path.is_symlink():
            raise GenerationBlockedError("recorded sealed ciphertext cleanup is unsafe")
        if not root.exists():
            return
        if not root.is_dir() or root.stat().st_mode & 0o777 != 0o700:
            raise GenerationBlockedError("recorded sealed ciphertext root is unsafe")
        entries = tuple(root.iterdir())
        if any(entry != path or entry.is_symlink() or not entry.is_file() for entry in entries):
            raise GenerationBlockedError("recorded sealed ciphertext root contains an unexpected entry")
        if path.exists():
            if path.stat().st_mode & 0o777 != 0o600:
                raise GenerationBlockedError("recorded sealed ciphertext is not private")
            path.unlink()
            self._fsync_directory(root)
        root.rmdir()
        self._fsync_directory(base)

    def _rollback_unrecorded_sealed_bundle(self, generation_id: str, bundle: SealedSecretBundle) -> None:
        """Keep a bundle only if the state file durably records this exact one."""
        try:
            persisted = self._load(generation_id)
        except GenerationInfrastructureError:
            persisted = None
        if persisted is not None and persisted.sealed_secret == bundle:
            return
        self._remove_exact_sealed_bundle(generation_id, bundle.path)

    def _cleanup_sealed_ciphertext(
        self, stored: _StoredGeneration, inventory: CleanupInventory | None
    ) -> _StoredGeneration:
        if inventory is None:
            raise GenerationBlockedError("cleanup inventory is unavailable; refusing to abandon")
        completed = set(inventory.completed_sealed_ciphertext_paths)
        for path in inventory.sealed_ciphertext_paths:
            if path in completed:
                continue
            self._remove_exact_sealed_bundle(stored.state.generation_id, path)
            completed.add(path)
            inventory = CleanupInventory(
                inventory.worktrees,
                inventory.plaintext_paths,
                inventory.completed_worktree_targets,
                inventory.completed_plaintext_paths,
                sealed_ciphertext_paths=inventory.sealed_ciphertext_paths,
                completed_sealed_ciphertext_paths=tuple(sorted(completed, key=str)),
            )
            stored = _replace_cleanup_inventory(stored, inventory)
            self._save(stored)
        return stored

    def abandon(self, generation_id: str) -> GenerationState:
        stored = self._load(generation_id)
        inventory = stored.cleanup_inventory
        if inventory is None:
            raise GenerationBlockedError("cleanup inventory is unavailable; refusing to abandon")
        if inventory.completed:
            raise GenerationBlockedError("generation cleanup was already completed")
        stored = self._cleanup_worktrees(stored, inventory)
        stored = self._cleanup_plaintext_paths(stored, stored.cleanup_inventory)
        stored = self._cleanup_sealed_ciphertext(stored, stored.cleanup_inventory)
        try:
            stored.state.abandon()
        except StateError as error:
            raise GenerationBlockedError(str(error)) from error
        final_inventory = stored.cleanup_inventory
        if final_inventory is None:
            raise GenerationBlockedError("cleanup inventory is unavailable; refusing to abandon")
        self._save(
            _StoredGeneration(
                stored.state,
                stored.manifest_path,
                stored.resources,
                stored.knowledge_digest,
                stored.resource_digest,
                stored.plan_digest,
                CleanupInventory(
                    final_inventory.worktrees,
                    final_inventory.plaintext_paths,
                    final_inventory.completed_worktree_targets,
                    final_inventory.completed_plaintext_paths,
                    completed=True,
                    sealed_ciphertext_paths=final_inventory.sealed_ciphertext_paths,
                    completed_sealed_ciphertext_paths=final_inventory.completed_sealed_ciphertext_paths,
                ),
                stored.knowledge_review,
                stored.plan_artifact,
                stored.binding,
                stored.sealed_secret,
            )
        )
        return stored.state

    def record_owned_worktree(
        self, generation_id: str, manager: WorktreeManager, handle: WorktreeHandle
    ) -> None:
        """Register a live manager-created worktree for narrow future cleanup."""
        stored = self._load(generation_id)
        inventory = stored.cleanup_inventory
        if inventory is None or inventory.completed or not manager.owns(handle):
            raise GenerationBlockedError("generation worktree ownership cannot be recorded")
        existing = next((item for item in inventory.worktrees if item.target == handle.target), None)
        if existing is not None and existing == handle:
            return
        if existing is not None and existing.ownership_token != handle.ownership_token:
            raise GenerationBlockedError("generation worktree ownership cannot be replaced")
        worktrees = tuple(handle if item.target == handle.target else item for item in inventory.worktrees)
        if existing is None:
            worktrees += (handle,)
        self._save(
            _StoredGeneration(
                stored.state,
                stored.manifest_path,
                stored.resources,
                stored.knowledge_digest,
                stored.resource_digest,
                stored.plan_digest,
                CleanupInventory(
                    worktrees,
                    inventory.plaintext_paths,
                    inventory.completed_worktree_targets,
                    inventory.completed_plaintext_paths,
                    sealed_ciphertext_paths=inventory.sealed_ciphertext_paths,
                    completed_sealed_ciphertext_paths=inventory.completed_sealed_ciphertext_paths,
                ),
                stored.knowledge_review,
                stored.plan_artifact,
                stored.binding,
                stored.sealed_secret,
            )
        )

    def record_owned_plaintext_path(self, generation_id: str, path: Path) -> None:
        """Register one generation-private plaintext file for later narrow removal."""
        stored = self._load(generation_id)
        inventory = stored.cleanup_inventory
        if inventory is None or inventory.completed or not isinstance(path, Path):
            raise GenerationBlockedError("generation plaintext ownership cannot be recorded")
        root = self._state_root / "plaintext" / generation_id
        if root.is_symlink() or path.is_symlink() or not path.is_file():
            raise GenerationBlockedError("generation plaintext ownership cannot be recorded")
        try:
            owned_path = path.resolve()
            owned_path.relative_to(root.resolve())
        except ValueError as error:
            raise GenerationBlockedError("generation plaintext path escapes its owned root") from error
        if owned_path in inventory.plaintext_paths:
            return
        self._save(
            _replace_cleanup_inventory(
                stored,
                CleanupInventory(
                    inventory.worktrees,
                    inventory.plaintext_paths + (owned_path,),
                    inventory.completed_worktree_targets,
                    inventory.completed_plaintext_paths,
                    sealed_ciphertext_paths=inventory.sealed_ciphertext_paths,
                    completed_sealed_ciphertext_paths=inventory.completed_sealed_ciphertext_paths,
                ),
            )
        )

    def record_verified_pr_readiness(
        self,
        generation_id: str,
        *,
        readiness: ReadinessEvidence,
        worktrees: tuple[object, ...],
        verification_reports: Mapping[str, VerificationReport],
    ) -> Path:
        """Persist the sole PR authority after all role reports are truly ready.

        This is deliberately the only state transition into ``VERIFIED`` for
        the PR lane.  It refuses caller booleans unless they agree with reports
        produced by the verification seam and writes durable evidence before
        recording the state transition.
        """
        raise GenerationBlockedError("verification reports are coordinator-owned; call verify_generation")

    def verify_generation(self, generation_id: str) -> Path:
        """Persist readiness only from the injected, authority-owning coordinator."""
        if not isinstance(self._verification_coordinator, RepositoryOwnedCIVerificationCoordinator):
            raise GenerationBlockedError("verification is library-only until a coordinator is configured")
        stored = self._load_verified(generation_id)
        if stored.state.stage is not Stage.GENERATED:
            raise GenerationBlockedError("verification requires exact committed generation lanes")
        try:
            readiness, reports = self._verification_coordinator.verify(
                generation_id=generation_id, resources=stored.resources, lane_records=stored.state.lane_records
            )
        except Exception as error:
            raise GenerationBlockedError("coordinator verification failed") from error
        worktrees = self._verification_coordinator.worktrees_for(
            generation_id=generation_id, inventory=stored.cleanup_inventory, readiness=readiness
        )
        return self._persist_verified_pr_readiness(generation_id, readiness, worktrees, reports)

    def _persist_verified_pr_readiness(
        self, generation_id: str, readiness: ReadinessEvidence, worktrees: tuple[object, ...], verification_reports: Mapping[str, VerificationReport]
    ) -> Path:
        stored = self._load_verified(generation_id)
        state = stored.state
        if state.stage is not Stage.GENERATED:
            raise GenerationBlockedError("verified readiness requires stage GENERATED")
        if not isinstance(readiness, ReadinessEvidence) or not isinstance(verification_reports, Mapping):
            raise GenerationBlockedError("verified readiness evidence is required")
        if set(verification_reports) != {"backend", "operations", "website"}:
            raise GenerationBlockedError("verified readiness requires reports for every role")
        for role, report in verification_reports.items():
            if not isinstance(report, VerificationReport) or not report.ready_for_pr:
                raise GenerationBlockedError(f"{role} verification is not PR-ready")
            if report.artifact_digest != readiness.artifact_digests.get(role):
                raise GenerationBlockedError(f"{role} artifact digest does not match verification")
            if report.source_revision != readiness.provenance_source_revision:
                raise GenerationBlockedError(f"{role} source revision does not match verification")
        if not (readiness.readiness_verified and readiness.secret_scan_passed and readiness.ci_registered):
            raise GenerationBlockedError("real verification, secret scan, and CI registration are required")
        authority = ReadinessAuthority(self._state_root)
        try:
            record_path = authority.persist(
                state,
                readiness=readiness,
                worktrees=worktrees,
                verification_reports=verification_reports,
            )
            for name, digest in {
                "readiness": readiness.readiness_digest,
                "secret_scan": readiness.secret_scan_digest,
                "ci_registration": readiness.ci_registration_digest,
                "provenance": readiness.provenance_digest,
                "worktree_ownership": readiness.worktree_ownership_digest,
                "artifact_backend": readiness.artifact_digests["backend"],
                "artifact_operations": readiness.artifact_digests["operations"],
                "artifact_website": readiness.artifact_digests["website"],
            }.items():
                state.record_stage_digest(name, digest)
            state.transition(Stage.VERIFIED)
        except (ReadinessError, StateError, KeyError) as error:
            raise GenerationBlockedError("verified readiness cannot be persisted") from error
        self._save(stored)
        return record_path

    def open_pr(self, generation_id: str) -> GenerationState:
        stored = self._load_verified(generation_id)
        if stored.state.stage is not Stage.VERIFIED:
            raise GenerationBlockedError("VERIFIED state is required before opening review requests")
        coordinator = self._pr_coordinator
        open_requests = getattr(coordinator, "open", None)
        if not callable(open_requests):
            raise GenerationBlockedError("review request coordinator is unavailable until the PR lane is configured")
        try:
            open_requests(generation_id=generation_id, state=stored.state, inventory=stored.cleanup_inventory)
        except GenerationBlockedError:
            raise
        except Exception as error:
            raise GenerationBlockedError("review request coordinator failed") from error
        self._save(stored)
        return stored.state

    def _load_verified(self, generation_id: str) -> _StoredGeneration:
        stored = self._load(generation_id)
        manifest = self._current_manifest(stored)
        if manifest_digest(manifest) != stored.state.manifest_digest:
            raise GenerationBlockedError("manifest digest changed; create a new generation")
        knowledge_digest = _digest_payload({"sources": [asdict(source) for source in manifest.knowledge_sources]})
        try:
            resources = derive_resources(
                manifest, self._reserved_resources(exclude_generation_id=generation_id)
            )
        except ResourceConflict as error:
            raise GenerationBlockedError("persisted allocation now conflicts with registry") from error
        resource_digest = _digest_payload(asdict(resources))
        plan_digest = _digest_payload(
            _canonical_plan_artifact(
                manifest.slug, resources, stored.state.manifest_digest, knowledge_digest, resource_digest
            )
        )
        if (
            knowledge_digest != stored.knowledge_digest
            or resource_digest != stored.resource_digest
            or plan_digest != stored.plan_digest
        ):
            raise GenerationBlockedError("generation input digest changed; create a new generation")
        return stored

    def _current_manifest(self, stored: _StoredGeneration):
        try:
            raw = json.loads(stored.manifest_path.read_text(encoding="utf-8"))
            if not isinstance(raw, Mapping):
                raise ManifestError("manifest must be an object")
            manifest = parse_manifest(
                raw,
                approved_source_roots=(stored.manifest_path.parent,),
                catalogue=CapabilityCatalogue.load(self._catalogue_path),
            )
        except (OSError, json.JSONDecodeError, ManifestError) as error:
            raise GenerationBlockedError(str(error)) from error
        return manifest

    def _build_knowledge_review(self, stored: _StoredGeneration, manifest: Any) -> KnowledgeReview:
        output_dir = self._state_root / "knowledge-reviews" / stored.state.generation_id
        if output_dir.is_symlink() or output_dir.parent.is_symlink():
            raise GenerationInfrastructureError("knowledge review output may not traverse a symlink")
        try:
            output_dir.parent.mkdir(mode=0o700, exist_ok=True)
            output_dir.mkdir(mode=0o700, exist_ok=True)
            if (
                not output_dir.parent.is_dir()
                or not output_dir.is_dir()
                or output_dir.parent.stat().st_mode & 0o777 != 0o700
                or output_dir.stat().st_mode & 0o777 != 0o700
            ):
                raise GenerationInfrastructureError("knowledge review output must be private")
            builder = self._knowledge_builder_factory(stored.manifest_path.parent)
            review = builder.build(manifest.knowledge_sources, output_dir)
            if type(review) is not KnowledgeReview:
                raise KnowledgeError("knowledge builder returned an invalid review")
            if review.digest != recompute_knowledge_review_digest(review):
                raise KnowledgeError("knowledge review digest is not canonical")
        except (KnowledgeError, OSError, TypeError, ValueError) as error:
            raise GenerationBlockedError(f"knowledge review digest is invalid: {error}") from error
        return review

    @staticmethod
    def _require_complete_runtime_template() -> None:
        path = Path(__file__).parent / "template_v1" / "file_allowlist.json"
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise GenerationBlockedError("INCOMPLETE_TEMPLATE: template provenance is unavailable") from error
        if not isinstance(raw, Mapping) or raw.get("status") != "approved":
            raise GenerationBlockedError("INCOMPLETE_TEMPLATE: a complete client-neutral runtime extraction is required")

    @staticmethod
    def _approved_knowledge_review(stored: _StoredGeneration) -> KnowledgeReview:
        review = stored.knowledge_review
        if type(review) is not KnowledgeReview:
            raise GenerationBlockedError("canonical knowledge review is unavailable")
        try:
            digest = recompute_knowledge_review_digest(review)
        except KnowledgeError as error:
            raise GenerationBlockedError("canonical knowledge review is invalid") from error
        if (
            digest != review.digest
            or review.digest != stored.state.knowledge_review_digest
            or review.digest != stored.state.knowledge_approval_digest
        ):
            raise GenerationBlockedError("canonical knowledge review is not approved for generation")
        return review

    def _manifest_path(self, value: Path) -> Path:
        if not isinstance(value, Path) or not value.is_file():
            raise GenerationBlockedError("manifest path must name an existing file")
        return value.resolve()

    def _state_path(self, generation_id: str) -> Path:
        if not generation_id or any(char not in "abcdefghijklmnopqrstuvwxyz0123456789-" for char in generation_id):
            raise GenerationInfrastructureError("generation id is invalid")
        path = self._state_root / f"{generation_id}.json"
        if path.is_symlink():
            raise GenerationInfrastructureError("generation state file may not be a symlink")
        return path

    @staticmethod
    def _reject_symlink_traversal(path: Path) -> None:
        current = path
        while True:
            if current.is_symlink():
                raise GenerationInfrastructureError("state root may not traverse a symlink")
            if current == current.parent:
                return
            current = current.parent

    def _ensure_state_root(self, *, create: bool) -> None:
        self._reject_symlink_traversal(self._state_root_input)
        if create:
            self._state_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not self._state_root.is_dir() or self._state_root.is_symlink():
            raise GenerationInfrastructureError("state root may not be a symlink")
        if self._state_root.stat().st_mode & 0o777 != 0o700:
            raise GenerationInfrastructureError("state root must not be group or world accessible")

    @contextmanager
    def _allocation_lock(self):
        """Serialize derive-plus-persist so two plans cannot reserve the same set."""
        try:
            import fcntl

            self._ensure_state_root(create=True)
            path = self._state_root / ".allocation.lock"
            if path.is_symlink():
                raise GenerationInfrastructureError("allocation lock may not be a symlink")
            descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
            try:
                os.fchmod(descriptor, 0o600)
                if path.stat().st_mode & 0o777 != 0o600:
                    raise GenerationInfrastructureError("allocation lock must be private")
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                yield
            finally:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                finally:
                    os.close(descriptor)
        except OSError as error:
            raise GenerationInfrastructureError("cannot lock resource allocations") from error

    def _reserved_resources(self, *, exclude_generation_id: str | None = None) -> AllocationRegistry:
        allocations: dict[str, list[object]] = {
            "ports": [], "hostnames": [], "services": [], "containers": [], "slugs": [],
            "folders": [], "wss_headers": [], "ghcr_repositories": [], "ci_identifiers": [],
            "secret_record_keys": [],
        }
        for path in self._state_root.glob("gen-*.json"):
            if exclude_generation_id is not None and path.name == f"{exclude_generation_id}.json":
                continue
            if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o777 != 0o600:
                raise GenerationInfrastructureError("reserved generation state is unsafe")
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                resources = DerivedResources(**dict(raw["resources"]))
            except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError) as error:
                raise GenerationInfrastructureError("reserved generation state is invalid") from error
            allocations["ports"].extend((resources.website_port, resources.smartpbx_port))
            allocations["hostnames"].extend((resources.website_hostname, resources.smartpbx_hostname))
            allocations["services"].extend((resources.website_service, resources.smartpbx_service))
            allocations["containers"].extend((resources.website_service, resources.smartpbx_service))
            allocations["slugs"].append(resources.slug)
            allocations["folders"].append(resources.folder_identity)
            allocations["wss_headers"].append(resources.wss_header)
            allocations["ghcr_repositories"].append(resources.ghcr_repository)
            allocations["ci_identifiers"].append(resources.ci_identifier)
            allocations["secret_record_keys"].append(resources.secret_record_key)
        if self._inventory_provider is not None:
            try:
                snapshot = self._inventory_provider.snapshot
                parameters = inspect.signature(snapshot).parameters.values()
                accepts_exclusion = any(
                    parameter.name == "exclude_generation_id"
                    or parameter.kind is inspect.Parameter.VAR_KEYWORD
                    for parameter in parameters
                )
                external = snapshot(exclude_generation_id=exclude_generation_id) if accepts_exclusion else snapshot()
            except Exception as error:
                raise GenerationInfrastructureError("external allocation inventory is unavailable") from error
            if not isinstance(external, Mapping):
                raise GenerationInfrastructureError("external allocation inventory is invalid")
            allowed = set(allocations) | {"repositories"}
            if set(external) - allowed:
                raise GenerationInfrastructureError("external allocation inventory is invalid")
            for name, values in external.items():
                if name == "repositories":
                    allocations["folders"].extend(str(value) for value in values if isinstance(values, (list, tuple, set)))
                    continue
                if not isinstance(values, (list, tuple, set)):
                    raise GenerationInfrastructureError("external allocation inventory is invalid")
                allocations[name].extend(values)
        return AllocationRegistry(allocations)

    @staticmethod
    def _require_private_state_file(path: Path) -> None:
        if path.is_symlink():
            raise GenerationInfrastructureError("generation state file may not be a symlink")
        if path.exists() and path.stat().st_mode & 0o777 != 0o600:
            raise GenerationInfrastructureError("generation state file must have mode 0600")

    def _save(self, stored: _StoredGeneration) -> None:
        temporary_path: Path | None = None
        try:
            self._ensure_state_root(create=True)
            path = self._state_path(stored.state.generation_id)
            self._require_private_state_file(path)
            payload = {
                "version": 7,
                "state": stored.state.to_dict(),
                "manifest_path": str(stored.manifest_path),
                "resources": asdict(stored.resources),
                "knowledge_digest": stored.knowledge_digest,
                "resource_digest": stored.resource_digest,
                "plan_digest": stored.plan_digest,
                "cleanup_inventory": _serialize_cleanup_inventory(stored.cleanup_inventory),
                "knowledge_review": _serialize_knowledge_review(stored.knowledge_review),
                "plan_artifact": dict(stored.plan_artifact or {}),
                "binding": dict(stored.binding or {}),
                "sealed_secret": _serialize_sealed_secret(stored.sealed_secret),
            }
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{stored.state.generation_id}.", dir=self._state_root, text=True
            )
            temporary_path = Path(temporary_name)
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            if path.is_symlink():
                raise GenerationInfrastructureError("generation state file may not be a symlink")
            os.replace(temporary_path, path)
            path.chmod(0o600)
            self._require_private_state_file(path)
            directory_descriptor = os.open(self._state_root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        except (OSError, TypeError, ValueError) as error:
            raise GenerationInfrastructureError("cannot persist generation state") from error
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    def _load(self, generation_id: str) -> _StoredGeneration:
        try:
            self._ensure_state_root(create=False)
            path = self._state_path(generation_id)
            self._require_private_state_file(path)
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, Mapping) or raw.get("version") not in {1, 2, 3, 4, 5, 6, 7}:
                raise ValueError("state document is invalid")
            state = GenerationState.from_dict(dict(raw["state"]))
            resources = DerivedResources(**dict(raw["resources"]))
            manifest_path = Path(raw["manifest_path"])
            digests = (raw["knowledge_digest"], raw["resource_digest"], raw["plan_digest"])
            if not manifest_path.is_absolute() or any(
                not isinstance(value, str)
                or len(value) != 64
                or any(character not in "0123456789abcdef" for character in value)
                for value in digests
            ):
                raise ValueError("state document is invalid")
            inventory = _parse_cleanup_inventory(raw.get("cleanup_inventory")) if raw["version"] in {4, 5, 6, 7} else None
            review = _parse_knowledge_review(raw.get("knowledge_review")) if raw["version"] in {5, 6, 7} else None
            artifact = raw.get("plan_artifact") if raw["version"] in {6, 7} else None
            binding = raw.get("binding") if raw["version"] in {6, 7} else None
            sealed = _parse_sealed_secret(raw.get("sealed_secret")) if raw["version"] == 7 else None
            if raw["version"] in {6, 7} and (not isinstance(artifact, Mapping) or artifact.get("digest") != state.plan_digest or not isinstance(binding, Mapping)):
                raise ValueError("state document is missing canonical transaction artifacts")
            if state.stage in {Stage.KNOWLEDGE_REVIEW_REQUIRED, Stage.PLAN_REVIEW_REQUIRED, Stage.GENERATED, Stage.VERIFIED, Stage.THREE_PRS_OPENED}:
                if review is None or review.digest != state.knowledge_review_digest:
                    raise ValueError("state document is missing its canonical knowledge review")
            if state.stage in {Stage.KNOWLEDGE_REVIEW_REQUIRED, Stage.PLAN_REVIEW_REQUIRED, Stage.GENERATED, Stage.VERIFIED, Stage.THREE_PRS_OPENED} and sealed is None:
                raise ValueError("state document is missing its sealed secret bundle")
            return _StoredGeneration(state, manifest_path, resources, *digests, inventory, review, artifact, binding, sealed)
        except (OSError, KeyError, TypeError, ValueError, StateError) as error:
            raise GenerationInfrastructureError("cannot load generation state") from error

    def _cleanup_worktrees(
        self, stored: _StoredGeneration, inventory: CleanupInventory | None
    ) -> _StoredGeneration:
        if inventory is None:
            raise GenerationBlockedError("cleanup inventory is unavailable; refusing to abandon")
        completed = set(inventory.completed_worktree_targets)
        for handle in inventory.worktrees:
            if handle.target in completed:
                continue
            manager = self._worktree_manager_factory(handle.temporary_root)
            try:
                manager.remove_recorded(handle)
            except Exception as error:
                raise GenerationBlockedError("recorded worktree cleanup failed authoritative validation") from error
            completed.add(handle.target)
            inventory = CleanupInventory(
                inventory.worktrees,
                inventory.plaintext_paths,
                tuple(sorted(completed, key=str)),
                inventory.completed_plaintext_paths,
                sealed_ciphertext_paths=inventory.sealed_ciphertext_paths,
                completed_sealed_ciphertext_paths=inventory.completed_sealed_ciphertext_paths,
            )
            stored = _replace_cleanup_inventory(stored, inventory)
            self._save(stored)
        return stored

    def _cleanup_plaintext_paths(
        self, stored: _StoredGeneration, inventory: CleanupInventory | None
    ) -> _StoredGeneration:
        if inventory is None:
            raise GenerationBlockedError("cleanup inventory is unavailable; refusing to abandon")
        completed = set(inventory.completed_plaintext_paths)
        root = self._state_root / "plaintext" / stored.state.generation_id
        for path in inventory.plaintext_paths:
            if path in completed:
                continue
            if root.is_symlink() or path.is_symlink():
                raise GenerationBlockedError("recorded plaintext cleanup is unsafe")
            try:
                path.relative_to(root.resolve())
            except ValueError as error:
                raise GenerationBlockedError("recorded plaintext path escapes its owned root") from error
            if path.exists() and not path.is_file():
                raise GenerationBlockedError("recorded plaintext cleanup target is not a file")
            if path.exists():
                path.unlink()
            completed.add(path)
            inventory = CleanupInventory(
                inventory.worktrees,
                inventory.plaintext_paths,
                inventory.completed_worktree_targets,
                tuple(sorted(completed, key=str)),
                sealed_ciphertext_paths=inventory.sealed_ciphertext_paths,
                completed_sealed_ciphertext_paths=inventory.completed_sealed_ciphertext_paths,
            )
            stored = _replace_cleanup_inventory(stored, inventory)
            self._save(stored)
        return stored

    def _inspect_provenance(self) -> str:
        path = Path(__file__).parent / "template_v1" / "file_allowlist.json"
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            validate_allowlist_metadata(raw)
        except (OSError, json.JSONDecodeError, ProvenanceError) as error:
            return f"blocked: {error}"
        return "ready"

    def _inspect_operations_prerequisites(self) -> str:
        required = (
            "SMARTPBX_OPERATIONS_REPOSITORY",
            "SMARTPBX_OPERATIONS_OWNER",
            "SMARTPBX_AGE_RECIPIENT_FILE",
            "SMARTPBX_AGE_RECIPIENT_REVIEW_SOURCE",
            "SMARTPBX_CREDENTIAL_SOURCE_POLICY",
        )
        missing = [name for name in required if not os.environ.get(name)]
        if missing:
            return f"blocked: missing {missing[0]}"
        repository = Path(os.environ["SMARTPBX_OPERATIONS_REPOSITORY"])
        if not repository.is_absolute() or not repository.is_dir() or not (repository / ".git").exists():
            return "blocked: operations repository is not an existing Git checkout"
        return "blocked: operations lane must validate remote, privacy, recipients, and credential policy"


def _digest_payload(payload: object) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _tree_digest(root: Path) -> str:
    """Hash a generated lane without recording contents or following links."""
    if not isinstance(root, Path) or root.is_symlink() or not root.is_dir():
        raise GenerationBlockedError("owned worktree output is unavailable")
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise GenerationBlockedError("owned worktree output may not traverse a symlink")
        if not path.is_file() or ".git" in path.parts:
            continue
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def _available_binary(name: str) -> bool:
    from shutil import which

    return which(name) is not None


def _check(passed: bool, reason: str) -> str:
    return "ready" if passed else f"blocked: {reason}"


def _git_check(root: Path, arguments: tuple[str, ...]) -> str:
    if not (root / ".git").exists():
        return "blocked: source repository is unavailable"
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *arguments],
            capture_output=True,
            check=False,
            shell=False,
            text=True,
        )
    except OSError:
        return "blocked: git unavailable"
    if result.returncode != 0:
        return "blocked: " + (result.stderr.strip() or "Git check failed")
    if arguments[:2] == ("rev-parse", "--is-inside-work-tree") and result.stdout.strip() != "true":
        return "blocked: source path is not a Git checkout"
    return "ready"


def _git_clean_check(root: Path) -> str:
    if not (root / ".git").exists():
        return "blocked: source repository is unavailable"
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain"],
            capture_output=True,
            check=False,
            shell=False,
            text=True,
        )
    except OSError:
        return "blocked: git unavailable"
    if result.returncode != 0:
        return "blocked: " + (result.stderr.strip() or "Git status failed")
    return _check(not result.stdout.strip(), "source repository is dirty")


def _serialize_cleanup_inventory(inventory: CleanupInventory | None) -> object:
    if inventory is None:
        return None
    return {
        "worktrees": [
            {
                "primary": str(handle.primary),
                "target": str(handle.target),
                "revision": handle.revision,
                "temporary_root": str(handle.temporary_root),
                "ownership_token": handle.ownership_token,
                "branch": handle.branch,
            }
            for handle in inventory.worktrees
        ],
        "plaintext_paths": [str(path) for path in inventory.plaintext_paths],
        "completed_worktree_targets": [str(path) for path in inventory.completed_worktree_targets],
        "completed_plaintext_paths": [str(path) for path in inventory.completed_plaintext_paths],
        "completed": inventory.completed,
        "sealed_ciphertext_paths": [str(path) for path in inventory.sealed_ciphertext_paths],
        "completed_sealed_ciphertext_paths": [str(path) for path in inventory.completed_sealed_ciphertext_paths],
    }


def _serialize_sealed_secret(bundle: SealedSecretBundle | None) -> object:
    if bundle is None:
        return None
    return {
        "path": str(bundle.path), "digest": bundle.digest,
        "record_ids": list(bundle.record_ids), "runtime_env_names": list(bundle.runtime_env_names),
    }


def _parse_sealed_secret(raw: object) -> SealedSecretBundle | None:
    if raw is None:
        return None
    if not isinstance(raw, Mapping) or set(raw) != {"path", "digest", "record_ids", "runtime_env_names"}:
        raise ValueError("sealed secret bundle is invalid")
    path, digest, record_ids, env_names = raw["path"], raw["digest"], raw["record_ids"], raw["runtime_env_names"]
    if not isinstance(record_ids, list) or not isinstance(env_names, list):
        raise ValueError("sealed secret bundle is invalid")
    if not isinstance(path, str) or not isinstance(digest, str) or not all(isinstance(item, str) for item in record_ids) or not all(isinstance(item, str) for item in env_names):
        raise ValueError("sealed secret bundle is invalid")
    return SealedSecretBundle(Path(path), digest, tuple(record_ids), tuple(env_names))


def _parse_cleanup_inventory(raw: object) -> CleanupInventory | None:
    if raw is None:
        return None
    legacy_keys = {
        "worktrees",
        "plaintext_paths",
        "completed_worktree_targets",
        "completed_plaintext_paths",
        "completed",
    }
    keys = legacy_keys | {"sealed_ciphertext_paths", "completed_sealed_ciphertext_paths"}
    if not isinstance(raw, Mapping) or set(raw) not in {legacy_keys, keys}:
        raise ValueError("generation cleanup inventory is invalid")
    worktrees = raw["worktrees"]
    plaintext_paths = raw["plaintext_paths"]
    completed_worktree_targets = raw["completed_worktree_targets"]
    completed_plaintext_paths = raw["completed_plaintext_paths"]
    completed = raw["completed"]
    sealed_ciphertext_paths = raw.get("sealed_ciphertext_paths", [])
    completed_sealed_ciphertext_paths = raw.get("completed_sealed_ciphertext_paths", [])
    if not all(
        isinstance(value, list)
        for value in (worktrees, plaintext_paths, completed_worktree_targets, completed_plaintext_paths, sealed_ciphertext_paths, completed_sealed_ciphertext_paths)
    ) or not isinstance(completed, bool):
        raise ValueError("generation cleanup inventory is invalid")
    parsed_worktrees: list[WorktreeHandle] = []
    for item in worktrees:
        if not isinstance(item, Mapping) or set(item) != {
            "primary", "target", "revision", "temporary_root", "ownership_token", "branch"
        }:
            raise ValueError("generation cleanup inventory is invalid")
        primary, target, revision, temporary_root, ownership_token, branch = (
            item["primary"], item["target"], item["revision"], item["temporary_root"], item["ownership_token"], item["branch"]
        )
        if not all(isinstance(value, str) for value in (primary, target, revision, temporary_root, ownership_token)) or (
            branch is not None and not isinstance(branch, str)
        ):
            raise ValueError("generation cleanup inventory is invalid")
        parsed_worktrees.append(
            WorktreeHandle(Path(primary), Path(target), revision, Path(temporary_root), ownership_token, branch)
        )
    parsed_paths = tuple(Path(value) for value in plaintext_paths if isinstance(value, str))
    completed_targets = tuple(Path(value) for value in completed_worktree_targets if isinstance(value, str))
    completed_paths = tuple(Path(value) for value in completed_plaintext_paths if isinstance(value, str))
    sealed_paths = tuple(Path(value) for value in sealed_ciphertext_paths if isinstance(value, str))
    completed_sealed_paths = tuple(Path(value) for value in completed_sealed_ciphertext_paths if isinstance(value, str))
    if (
        len(parsed_paths) != len(plaintext_paths)
        or len(completed_targets) != len(completed_worktree_targets)
        or len(completed_paths) != len(completed_plaintext_paths)
        or len(sealed_paths) != len(sealed_ciphertext_paths)
        or len(completed_sealed_paths) != len(completed_sealed_ciphertext_paths)
        or any(not path.is_absolute() for path in (*parsed_paths, *completed_targets, *completed_paths, *sealed_paths, *completed_sealed_paths))
    ):
        raise ValueError("generation cleanup inventory is invalid")
    return CleanupInventory(tuple(parsed_worktrees), parsed_paths, completed_targets, completed_paths, completed, sealed_paths, completed_sealed_paths)


def _replace_cleanup_inventory(stored: _StoredGeneration, inventory: CleanupInventory) -> _StoredGeneration:
    return _StoredGeneration(
        stored.state,
        stored.manifest_path,
        stored.resources,
        stored.knowledge_digest,
        stored.resource_digest,
        stored.plan_digest,
        inventory,
        stored.knowledge_review,
        stored.plan_artifact,
        stored.binding,
        stored.sealed_secret,
    )


def _replace_knowledge_review(
    stored: _StoredGeneration, review: KnowledgeReview, *, sealed_secret: SealedSecretBundle | None = None
) -> _StoredGeneration:
    return _StoredGeneration(
        stored.state,
        stored.manifest_path,
        stored.resources,
        stored.knowledge_digest,
        stored.resource_digest,
        stored.plan_digest,
        stored.cleanup_inventory,
        review,
        stored.plan_artifact,
        stored.binding,
        sealed_secret if sealed_secret is not None else stored.sealed_secret,
    )


def _serialize_knowledge_review(review: KnowledgeReview | None) -> object:
    if review is None:
        return None
    if type(review) is not KnowledgeReview or review.digest != recompute_knowledge_review_digest(review):
        raise GenerationInfrastructureError("canonical knowledge review cannot be persisted")
    return {
        "facts": [asdict(fact) for fact in review.facts],
        "conflicts": [asdict(conflict) for conflict in review.conflicts],
        "missing_facts": list(review.missing_facts),
        "sensitive_findings": list(review.sensitive_findings),
        "inaccessible_sources": list(review.inaccessible_sources),
        "duplicate_facts": list(review.duplicate_facts),
        "instruction_findings": list(review.instruction_findings),
        "digest": review.digest,
        "documents": [asdict(document) for document in review.documents],
        "executed_instructions": review.executed_instructions,
    }


def _parse_knowledge_review(raw: object) -> KnowledgeReview | None:
    if raw is None:
        return None
    keys = {
        "facts", "conflicts", "missing_facts", "sensitive_findings", "inaccessible_sources",
        "duplicate_facts", "instruction_findings", "digest", "documents", "executed_instructions",
    }
    if not isinstance(raw, Mapping) or set(raw) != keys:
        raise ValueError("state knowledge review is invalid")

    def records(value: object, kind: type[Any], fields: set[str]) -> tuple[Any, ...]:
        if not isinstance(value, list) or any(not isinstance(item, Mapping) or set(item) != fields for item in value):
            raise ValueError("state knowledge review is invalid")
        try:
            return tuple(kind(**dict(item)) for item in value)
        except (TypeError, ValueError) as error:
            raise ValueError("state knowledge review is invalid") from error

    def strings(value: object) -> tuple[str, ...]:
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise ValueError("state knowledge review is invalid")
        return tuple(value)

    try:
        raw_conflicts = raw["conflicts"]
        if (
            not isinstance(raw_conflicts, list)
            or any(
                not isinstance(item, Mapping)
                or set(item) != {"subject", "values", "source_locations"}
                or not isinstance(item["subject"], str)
                or not isinstance(item["values"], list)
                or not isinstance(item["source_locations"], list)
                or not all(isinstance(value, str) for value in item["values"])
                or not all(isinstance(value, str) for value in item["source_locations"])
                for item in raw_conflicts
            )
        ):
            raise ValueError("state knowledge review is invalid")
        review = KnowledgeReview(
            facts=records(raw["facts"], KnowledgeFact, {"text", "source_uri", "location"}),
            conflicts=tuple(
                KnowledgeConflict(item["subject"], tuple(item["values"]), tuple(item["source_locations"]))
                for item in raw_conflicts
            ),
            missing_facts=strings(raw["missing_facts"]),
            sensitive_findings=strings(raw["sensitive_findings"]),
            inaccessible_sources=strings(raw["inaccessible_sources"]),
            duplicate_facts=strings(raw["duplicate_facts"]),
            instruction_findings=strings(raw["instruction_findings"]),
            digest=raw["digest"],
            documents=records(raw["documents"], KnowledgeDocument, {"uri", "owner", "effective_date", "classification", "text"}),
            executed_instructions=raw["executed_instructions"],
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("state knowledge review is invalid") from error
    if review.digest != recompute_knowledge_review_digest(review):
        raise ValueError("state knowledge review digest is invalid")
    return review


def _redacted_plan(
    generation_id: str,
    slug: str,
    resources: DerivedResources,
    manifest_digest_value: str,
    knowledge_digest: str,
    resource_digest: str,
    plan_digest: str,
) -> str:
    return "\n".join(
        (
            f"generation_id={generation_id}",
            f"slug={slug}",
            f"smartpbx_hostname={resources.smartpbx_hostname}",
            f"website_hostname={resources.website_hostname}",
            f"manifest_digest={manifest_digest_value}",
            f"knowledge_digest={knowledge_digest}",
            f"resource_digest={resource_digest}",
            f"plan_digest={plan_digest}",
            "secret_resolution=required",
            "knowledge_review=not-started",
        )
    )


def _canonical_plan_artifact(
    slug: str,
    resources: DerivedResources,
    manifest_digest_value: str,
    knowledge_digest: str,
    resource_digest: str,
) -> dict[str, object]:
    """The exact redacted object whose digest an approval binds."""
    return {
        "schema_version": 1,
        "release_allowed": False,
        "slug": slug,
        "resources": {
            "smartpbx_hostname": resources.smartpbx_hostname,
            "website_hostname": resources.website_hostname,
            "smartpbx_port": resources.smartpbx_port,
            "website_port": resources.website_port,
            "secret_record_key": resources.secret_record_key,
        },
        "manifest_digest": manifest_digest_value,
        "knowledge_digest": knowledge_digest,
        "resource_digest": resource_digest,
    }


def _serialize_generation_binding(binding: GenerationBinding) -> dict[str, dict[str, str]]:
    result: dict[str, dict[str, str]] = {}
    for name, lane in (("backend", binding.backend), ("operations", binding.operations), ("website", binding.website)):
        result[name] = {
            "primary": str(lane.primary.resolve()), "remote": lane.remote,
            "revision": lane.revision, "target": str(lane.target.resolve()),
            "manager_type": f"{type(lane.manager).__module__}.{type(lane.manager).__qualname__}",
        }
    return result
