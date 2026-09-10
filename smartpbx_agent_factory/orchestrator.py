"""Digest-bound, resumable factory transaction coordination."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

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
from .resources import AllocationRegistry, DerivedResources, ResourceConflict, derive_resources
from .schema import ManifestError, manifest_digest, parse_manifest
from .secrets import SecretAudit, SecretProvider
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


@dataclass(frozen=True)
class CleanupInventory:
    """Persisted identifiers for generation-owned cleanup, never broad paths."""

    worktrees: tuple[WorktreeHandle, ...] = ()
    plaintext_paths: tuple[Path, ...] = ()
    completed_worktree_targets: tuple[Path, ...] = ()
    completed_plaintext_paths: tuple[Path, ...] = ()
    completed: bool = False


@dataclass(frozen=True)
class BackendWorktreeBinding:
    """One caller-supplied, manager-owned destination for backend rendering."""

    manager: WorktreeManager
    primary: Path
    remote: str
    revision: str
    target: Path


class GenerationOrchestrator:
    """Own state transitions; rendering and PR creation stay separate lanes."""

    def __init__(
        self,
        state_root: Path,
        *,
        catalogue_path: Path | None = None,
        worktree_manager_factory: Callable[[Path], WorktreeManager] = WorktreeManager,
        knowledge_builder_factory: Callable[[Path], KnowledgeBuilder] | None = None,
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
            plan_digest = _digest_payload(
                {"manifest": digest, "knowledge": knowledge_digest, "resources": resource_digest}
            )
            state = GenerationState.start(f"gen-{uuid.uuid4().hex}", digest)
            state.transition(Stage.INPUT_COLLECTED)
            stored = _StoredGeneration(
                state, manifest_path, resources, knowledge_digest, resource_digest, plan_digest, CleanupInventory()
            )
            self._save(stored)
        return PlanReport(
            generation_id=state.generation_id,
            state=state,
            manifest_digest=digest,
            knowledge_digest=knowledge_digest,
            resource_digest=resource_digest,
            plan_digest=plan_digest,
            rendered_plan=_redacted_plan(state.generation_id, manifest.slug, resources, digest, knowledge_digest, resource_digest),
        )

    def generate(
        self, generation_id: str, *, backend_worktree: BackendWorktreeBinding | None = None
    ) -> GenerationState:
        stored = self._load_verified(generation_id)
        if stored.state.stage is Stage.INPUT_COLLECTED:
            raise GenerationBlockedError("secret resolution is required before knowledge review")
        if stored.state.stage is Stage.KNOWLEDGE_REVIEW_REQUIRED:
            raise GenerationBlockedError("knowledge approval is required before generation")
        if stored.state.stage is Stage.PLAN_REVIEW_REQUIRED:
            raise GenerationBlockedError("plan approval is required before generation")
        if stored.state.stage is not Stage.GENERATED:
            raise GenerationBlockedError(f"generation cannot start from {stored.state.stage.value}")
        manifest = self._current_manifest(stored)
        review = self._approved_knowledge_review(stored)
        self._require_complete_runtime_template()
        if not isinstance(backend_worktree, BackendWorktreeBinding):
            raise GenerationBlockedError("a manager-owned backend worktree binding is required")
        try:
            handle = backend_worktree.manager.create(
                primary=backend_worktree.primary,
                remote=backend_worktree.remote,
                revision=backend_worktree.revision,
                target=backend_worktree.target,
                branch=f"smartpbx-agent-factory/{stored.state.generation_id}",
            )
            self.record_owned_worktree(generation_id, backend_worktree.manager, handle)
        except (DirtyWorktreeError, WorktreeConflictError) as error:
            raise GenerationBlockedError("backend worktree creation failed") from error
        # Never render below the factory state directory.  The target is the
        # exact handle just created and recorded by WorktreeManager.
        try:
            render_backend(
                manifest,
                review,
                stored.resources,
                handle,
                worktree_manager=backend_worktree.manager,
                state=stored.state,
            )
        except IncompleteTemplateError as error:
            raise GenerationBlockedError(str(error)) from error
        raise GenerationBlockedError("generated backend requires configured operations, website, verification, and PR bindings")

    def resume(
        self,
        generation_id: str,
        *,
        knowledge_approval: str | None = None,
        plan_approval: str | None = None,
    ) -> GenerationState:
        stored = self._load_verified(generation_id)
        state = stored.state
        try:
            if knowledge_approval is not None:
                state.approve_knowledge(knowledge_approval)
                state.record_plan_digest(stored.plan_digest)
            if plan_approval is not None:
                state.approve_plan(plan_approval)
        except StateError as error:
            raise GenerationBlockedError(str(error)) from error
        self._save(stored)
        return state

    def record_secrets_resolved(
        self, generation_id: str, *, provider: SecretProvider
    ) -> GenerationState:
        """Advance only from a validated, redacted SecretProvider audit artifact."""
        if not hasattr(provider, "validate") or not hasattr(provider, "audit_report"):
            raise GenerationBlockedError("validated SecretProvider audit is required")
        stored = self._load_verified(generation_id)
        state = stored.state
        if state.stage is not Stage.INPUT_COLLECTED:
            raise GenerationBlockedError("secret resolution requires stage INPUT_COLLECTED")
        try:
            provider.validate()
            audit = provider.audit_report()
        except Exception as error:
            raise GenerationBlockedError("SecretProvider validation or audit retrieval failed") from error
        if not isinstance(audit, SecretAudit):
            raise GenerationBlockedError("SecretProvider audit is invalid")
        expected_names = {f"{stored.resources.slug}/wss_token"}
        audited_names = audit.fetched_names + audit.generated_names
        if len(audited_names) != len(set(audited_names)) or set(audited_names) != expected_names:
            raise GenerationBlockedError("secret audit names do not exactly match manifest requirements")
        manifest = self._current_manifest(stored)
        review = self._build_knowledge_review(stored, manifest)
        stored = _replace_knowledge_review(stored, review)
        audit_digest = _digest_payload(
            {
                "fetched_names": audit.fetched_names,
                "generated_names": audit.generated_names,
                "ciphertext_paths": audit.ciphertext_paths,
            }
        )
        try:
            state.record_stage_digest("secrets", audit_digest)
            state.transition(Stage.SECRETS_RESOLVED)
            state.transition(Stage.KNOWLEDGE_REVIEW_REQUIRED)
            state.record_knowledge_review_digest(review.digest)
        except StateError as error:
            raise GenerationBlockedError(str(error)) from error
        self._save(stored)
        return state

    def abandon(self, generation_id: str) -> GenerationState:
        stored = self._load(generation_id)
        inventory = stored.cleanup_inventory
        if inventory is None:
            raise GenerationBlockedError("cleanup inventory is unavailable; refusing to abandon")
        if inventory.completed:
            raise GenerationBlockedError("generation cleanup was already completed")
        stored = self._cleanup_worktrees(stored, inventory)
        stored = self._cleanup_plaintext_paths(stored, stored.cleanup_inventory)
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
                ),
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
                ),
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
        try:
            ReadinessAuthority(self._state_root).load(stored.state)
        except ReadinessError as error:
            raise GenerationBlockedError("authoritative readiness record is unavailable or invalid") from error
        # The actual provider lane receives its authority below through
        # open_linked_prs(), which always invokes ReadinessAuthority.load().
        # This CLI-only placeholder has no provider or worktree seams and can
        # therefore never turn caller-provided flags into a provider action.
        raise GenerationBlockedError("review request coordinator is unavailable until the PR lane is configured")

    def _load_verified(self, generation_id: str) -> _StoredGeneration:
        stored = self._load(generation_id)
        manifest = self._current_manifest(stored)
        if manifest_digest(manifest) != stored.state.manifest_digest:
            raise GenerationBlockedError("manifest digest changed; create a new generation")
        knowledge_digest = _digest_payload({"sources": [asdict(source) for source in manifest.knowledge_sources]})
        resources = derive_resources(manifest, AllocationRegistry())
        resource_digest = _digest_payload(asdict(resources))
        plan_digest = _digest_payload(
            {
                "manifest": stored.state.manifest_digest,
                "knowledge": knowledge_digest,
                "resources": resource_digest,
            }
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

    def _reserved_resources(self) -> AllocationRegistry:
        allocations: dict[str, list[object]] = {
            "ports": [], "hostnames": [], "services": [], "containers": [], "slugs": [],
            "folders": [], "wss_headers": [], "ghcr_repositories": [], "ci_identifiers": [],
            "secret_record_keys": [],
        }
        for path in self._state_root.glob("gen-*.json"):
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
                "version": 5,
                "state": stored.state.to_dict(),
                "manifest_path": str(stored.manifest_path),
                "resources": asdict(stored.resources),
                "knowledge_digest": stored.knowledge_digest,
                "resource_digest": stored.resource_digest,
                "plan_digest": stored.plan_digest,
                "cleanup_inventory": _serialize_cleanup_inventory(stored.cleanup_inventory),
                "knowledge_review": _serialize_knowledge_review(stored.knowledge_review),
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
            if not isinstance(raw, Mapping) or raw.get("version") not in {1, 2, 3, 4, 5}:
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
            inventory = _parse_cleanup_inventory(raw.get("cleanup_inventory")) if raw["version"] in {4, 5} else None
            review = _parse_knowledge_review(raw.get("knowledge_review")) if raw["version"] == 5 else None
            if state.stage in {Stage.KNOWLEDGE_REVIEW_REQUIRED, Stage.PLAN_REVIEW_REQUIRED, Stage.GENERATED, Stage.VERIFIED, Stage.THREE_PRS_OPENED}:
                if review is None or review.digest != state.knowledge_review_digest:
                    raise ValueError("state document is missing its canonical knowledge review")
            return _StoredGeneration(state, manifest_path, resources, *digests, inventory, review)
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
    }


def _parse_cleanup_inventory(raw: object) -> CleanupInventory | None:
    if raw is None:
        return None
    if not isinstance(raw, Mapping) or set(raw) != {
        "worktrees",
        "plaintext_paths",
        "completed_worktree_targets",
        "completed_plaintext_paths",
        "completed",
    }:
        raise ValueError("generation cleanup inventory is invalid")
    worktrees = raw["worktrees"]
    plaintext_paths = raw["plaintext_paths"]
    completed_worktree_targets = raw["completed_worktree_targets"]
    completed_plaintext_paths = raw["completed_plaintext_paths"]
    completed = raw["completed"]
    if not all(
        isinstance(value, list)
        for value in (worktrees, plaintext_paths, completed_worktree_targets, completed_plaintext_paths)
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
    if (
        len(parsed_paths) != len(plaintext_paths)
        or len(completed_targets) != len(completed_worktree_targets)
        or len(completed_paths) != len(completed_plaintext_paths)
        or any(not path.is_absolute() for path in (*parsed_paths, *completed_targets, *completed_paths))
    ):
        raise ValueError("generation cleanup inventory is invalid")
    return CleanupInventory(tuple(parsed_worktrees), parsed_paths, completed_targets, completed_paths, completed)


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
    )


def _replace_knowledge_review(stored: _StoredGeneration, review: KnowledgeReview) -> _StoredGeneration:
    return _StoredGeneration(
        stored.state,
        stored.manifest_path,
        stored.resources,
        stored.knowledge_digest,
        stored.resource_digest,
        stored.plan_digest,
        stored.cleanup_inventory,
        review,
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
            "secret_resolution=required",
            "knowledge_review=not-started",
        )
    )
