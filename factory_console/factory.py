"""Typed adapter to the existing review-only SmartPBX factory."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from smartpbx_agent_factory.bootstrap import FactoryBootstrap, inspect_config
from smartpbx_agent_factory.orchestrator import GenerationBlockedError
from smartpbx_agent_factory.state import GenerationState, Stage

from .domain import CompanyIntake, FactoryConsoleJob, FactoryOperationBlocked


class InternalManifestResolver(Protocol):
    """Server-owned mapping; a browser can never supply a path or Git ref."""

    def manifest_for(self, intake: CompanyIntake) -> Path: ...


class SmartPBXFactoryAdapter:
    """Calls factory Python APIs only; it never constructs a shell command."""

    def __init__(self, bootstrap: FactoryBootstrap, manifests: InternalManifestResolver) -> None:
        self._bootstrap = bootstrap
        self._manifests = manifests

    def inspect(self, job: FactoryConsoleJob) -> None:
        try:
            manifest = self._manifest(job)
            checks = inspect_config(self._bootstrap.config)
            report = self._bootstrap.orchestrator().inspect(manifest)
            if not all(value == "ready" for value in checks.values()) or not report["ok"]:
                raise FactoryOperationBlocked("factory inspection is blocked")
        except (GenerationBlockedError, OSError, ValueError) as error:
            raise FactoryOperationBlocked("factory inspection is blocked") from error

    def plan(self, job: FactoryConsoleJob) -> GenerationState:
        """Create and prepare a digest-bound plan with server-owned credentials only."""
        try:
            orchestrator = self._bootstrap.orchestrator()
            report = orchestrator.plan(self._manifest(job))
            state = orchestrator.record_secrets_resolved(
                report.generation_id, provider=self._bootstrap.secret_provider()
            )
            if state.stage is not Stage.KNOWLEDGE_REVIEW_REQUIRED or not state.knowledge_review_digest:
                raise FactoryOperationBlocked("factory knowledge review is not ready")
            state = orchestrator.resume(
                report.generation_id,
                knowledge_approval=state.knowledge_review_digest,
                secret_provider=self._bootstrap.secret_provider(),
            )
            if state.stage is not Stage.PLAN_REVIEW_REQUIRED:
                raise FactoryOperationBlocked("factory plan review is not ready")
            return state
        except (GenerationBlockedError, OSError, ValueError) as error:
            raise FactoryOperationBlocked("factory plan is blocked") from error

    def approve_plan(self, job: FactoryConsoleJob, generation: GenerationState) -> GenerationState:
        try:
            if generation.stage is not Stage.PLAN_REVIEW_REQUIRED or not generation.plan_digest:
                raise FactoryOperationBlocked("factory plan approval is unavailable")
            return self._bootstrap.orchestrator().resume(
                generation.generation_id, plan_approval=generation.plan_digest
            )
        except (GenerationBlockedError, OSError, ValueError) as error:
            raise FactoryOperationBlocked("factory plan approval is blocked") from error

    def generate(self, job: FactoryConsoleJob, generation: GenerationState) -> GenerationState:
        return self._call_generation("generation", generation, lambda orchestrator: orchestrator.generate(
            generation.generation_id,
            binding=self._bootstrap.binding_for(generation.generation_id),
            secret_provider=self._bootstrap.secret_provider(),
        ))

    def verify(self, job: FactoryConsoleJob, generation: GenerationState) -> GenerationState:
        try:
            orchestrator = self._bootstrap.orchestrator()
            orchestrator.verify_generation(generation.generation_id)
            return orchestrator.generation_state(generation.generation_id)
        except (GenerationBlockedError, OSError, ValueError) as error:
            raise FactoryOperationBlocked("factory verification is blocked") from error

    def open_pr(self, job: FactoryConsoleJob, generation: GenerationState) -> GenerationState:
        return self._call_generation("PR creation", generation, lambda orchestrator: orchestrator.open_pr(generation.generation_id))

    def _call_generation(self, label: str, generation: GenerationState, operation):
        try:
            result = operation(self._bootstrap.orchestrator())
            if not isinstance(result, GenerationState):
                raise FactoryOperationBlocked(f"factory {label} returned invalid state")
            return result
        except (GenerationBlockedError, OSError, ValueError) as error:
            raise FactoryOperationBlocked(f"factory {label} is blocked") from error

    def _manifest(self, job: FactoryConsoleJob) -> Path:
        path = self._manifests.manifest_for(job.intake)
        if not isinstance(path, Path) or not path.is_absolute():
            raise FactoryOperationBlocked("configured factory manifest is unavailable")
        return path
