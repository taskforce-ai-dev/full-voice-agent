"""Digest-bound, resumable factory transaction coordination."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping

from .catalogue import CapabilityCatalogue
from .resources import AllocationRegistry, DerivedResources, derive_resources
from .schema import ManifestError, manifest_digest, parse_manifest
from .state import GenerationState, Stage, StateError


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


class GenerationOrchestrator:
    """Own state transitions; rendering and PR creation stay separate lanes."""

    def __init__(self, state_root: Path, *, catalogue_path: Path | None = None) -> None:
        if not isinstance(state_root, Path) or not state_root.is_absolute():
            raise GenerationInfrastructureError("state root must be an absolute path")
        self._state_root = state_root.resolve()
        root = Path(__file__).parent
        self._catalogue_path = (catalogue_path or root / "template_v1" / "provider_catalogue.json").resolve()

    def inspect(self, manifest_path: Path) -> Mapping[str, object]:
        """Report non-mutating prerequisite status; no target checkout is touched."""
        manifest_path = self._manifest_path(manifest_path)
        checks = {
            "manifest": manifest_path.is_file(),
            "catalogue": self._catalogue_path.is_file(),
            "git": _available_binary("git"),
            "docker": _available_binary("docker"),
            "gh": _available_binary("gh"),
            "sops": _available_binary("sops"),
            "age": _available_binary("age"),
            "operations_repository": bool(os.environ.get("SMARTPBX_OPERATIONS_REPOSITORY")),
        }
        return {"ok": all(checks.values()), "checks": checks}

    def plan(self, manifest_path: Path) -> PlanReport:
        manifest_path = self._manifest_path(manifest_path)
        try:
            raw = json.loads(manifest_path.read_text(encoding="utf-8"))
            if not isinstance(raw, Mapping):
                raise ManifestError("manifest must be an object")
            catalogue = CapabilityCatalogue.load(self._catalogue_path)
            manifest = parse_manifest(
                raw,
                approved_source_roots=(manifest_path.parent, Path.cwd()),
                catalogue=catalogue,
            )
        except (OSError, json.JSONDecodeError, ManifestError) as error:
            raise GenerationBlockedError(str(error)) from error
        digest = manifest_digest(manifest)
        resources = derive_resources(manifest, AllocationRegistry())
        knowledge_digest = _digest_payload({"sources": [asdict(source) for source in manifest.knowledge_sources]})
        resource_digest = _digest_payload(asdict(resources))
        plan_digest = _digest_payload(
            {"manifest": digest, "knowledge": knowledge_digest, "resources": resource_digest}
        )
        state = GenerationState.start(f"gen-{uuid.uuid4().hex}", digest)
        state.transition(Stage.INPUT_COLLECTED)
        state.transition(Stage.SECRETS_RESOLVED)
        state.transition(Stage.KNOWLEDGE_REVIEW_REQUIRED)
        state.record_knowledge_review_digest(knowledge_digest)
        stored = _StoredGeneration(state, manifest_path, resources, knowledge_digest, resource_digest, plan_digest)
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

    def generate(self, generation_id: str) -> GenerationState:
        stored = self._load_verified(generation_id)
        if stored.state.stage is Stage.KNOWLEDGE_REVIEW_REQUIRED:
            raise GenerationBlockedError("knowledge approval is required before generation")
        if stored.state.stage is Stage.PLAN_REVIEW_REQUIRED:
            raise GenerationBlockedError("plan approval is required before generation")
        if stored.state.stage is not Stage.GENERATED:
            raise GenerationBlockedError(f"generation cannot start from {stored.state.stage.value}")
        raise GenerationBlockedError(
            "generation renderers and operations prerequisites must be configured before artifact output"
        )

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

    def abandon(self, generation_id: str) -> GenerationState:
        stored = self._load(generation_id)
        try:
            stored.state.abandon()
        except StateError as error:
            raise GenerationBlockedError(str(error)) from error
        self._save(stored)
        return stored.state

    def open_pr(self, generation_id: str) -> GenerationState:
        stored = self._load_verified(generation_id)
        if stored.state.stage is not Stage.VERIFIED:
            raise GenerationBlockedError("VERIFIED state is required before opening review requests")
        raise GenerationBlockedError("review request coordinator is unavailable until the PR lane is configured")

    def _load_verified(self, generation_id: str) -> _StoredGeneration:
        stored = self._load(generation_id)
        try:
            raw = json.loads(stored.manifest_path.read_text(encoding="utf-8"))
            if not isinstance(raw, Mapping):
                raise ManifestError("manifest must be an object")
            manifest = parse_manifest(
                raw,
                approved_source_roots=(stored.manifest_path.parent, Path.cwd()),
                catalogue=CapabilityCatalogue.load(self._catalogue_path),
            )
        except (OSError, json.JSONDecodeError, ManifestError) as error:
            raise GenerationBlockedError(str(error)) from error
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

    def _manifest_path(self, value: Path) -> Path:
        if not isinstance(value, Path) or not value.is_file():
            raise GenerationBlockedError("manifest path must name an existing file")
        return value.resolve()

    def _state_path(self, generation_id: str) -> Path:
        if not generation_id or any(char not in "abcdefghijklmnopqrstuvwxyz0123456789-" for char in generation_id):
            raise GenerationInfrastructureError("generation id is invalid")
        path = (self._state_root / f"{generation_id}.json").resolve()
        try:
            path.relative_to(self._state_root)
        except ValueError as error:
            raise GenerationInfrastructureError("generation state path escapes state root") from error
        return path

    def _save(self, stored: _StoredGeneration) -> None:
        try:
            self._state_root.mkdir(mode=0o700, parents=True, exist_ok=True)
            if self._state_root.stat().st_mode & 0o077:
                raise GenerationInfrastructureError("state root must not be group or world accessible")
            payload = {
                "version": 1,
                "state": stored.state.to_dict(),
                "manifest_path": str(stored.manifest_path),
                "resources": asdict(stored.resources),
                "knowledge_digest": stored.knowledge_digest,
                "resource_digest": stored.resource_digest,
                "plan_digest": stored.plan_digest,
            }
            path = self._state_path(stored.state.generation_id)
            flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
            descriptor = os.open(path, flags, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
                handle.write("\n")
            path.chmod(0o600)
        except (OSError, TypeError, ValueError) as error:
            if isinstance(error, GenerationInfrastructureError):
                raise
            raise GenerationInfrastructureError("cannot persist generation state") from error

    def _load(self, generation_id: str) -> _StoredGeneration:
        try:
            raw = json.loads(self._state_path(generation_id).read_text(encoding="utf-8"))
            if not isinstance(raw, Mapping) or raw.get("version") != 1:
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
            return _StoredGeneration(state, manifest_path, resources, *digests)
        except (OSError, KeyError, TypeError, ValueError, StateError) as error:
            raise GenerationInfrastructureError("cannot load generation state") from error


def _digest_payload(payload: object) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _available_binary(name: str) -> bool:
    from shutil import which

    return which(name) is not None


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
            "knowledge_approval=required",
            "plan_approval=required",
        )
    )
