"""Non-secret intake and a facade over the factory's generation state."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import re
from typing import Mapping, Protocol
from uuid import uuid4

from smartpbx_agent_factory.state import GenerationState, Stage


class IntakeValidationError(ValueError):
    pass


class InvalidStateTransition(ValueError):
    pass


class FactoryOperationBlocked(RuntimeError):
    pass


_INTAKE_FIELDS = frozenset({
    "company_name", "industry", "purpose", "primary_contact", "supported_languages",
})
_TEXT = re.compile(r"[^\x00-\x1f]{1,240}\Z")
_LANGUAGE = re.compile(r"[a-z]{2,8}(?:-[A-Z]{2})?\Z")


@dataclass(frozen=True)
class CompanyIntake:
    company_name: str
    industry: str
    purpose: str
    primary_contact: str
    supported_languages: tuple[str, ...]

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> "CompanyIntake":
        if not isinstance(payload, Mapping) or set(payload) != _INTAKE_FIELDS:
            raise IntakeValidationError("intake must contain only the supported non-secret fields")
        values: dict[str, str] = {}
        for field in ("company_name", "industry", "purpose", "primary_contact"):
            value = payload[field]
            if not isinstance(value, str) or not _TEXT.fullmatch(value.strip()):
                raise IntakeValidationError(f"{field} must be bounded text")
            normalized = value.strip()
            if "://" in normalized or normalized.startswith(("/", "~")):
                raise IntakeValidationError(f"{field} must not be a URL or filesystem path")
            values[field] = normalized
        languages = payload["supported_languages"]
        if not isinstance(languages, list) or not 1 <= len(languages) <= 8:
            raise IntakeValidationError("supported_languages must be a non-empty short list")
        parsed_languages = tuple(languages)
        if any(not isinstance(value, str) or not _LANGUAGE.fullmatch(value) for value in parsed_languages):
            raise IntakeValidationError("supported_languages contains an invalid language code")
        if len(set(parsed_languages)) != len(parsed_languages):
            raise IntakeValidationError("supported_languages must not contain duplicates")
        return cls(supported_languages=parsed_languages, **values)


class JobState(str, Enum):
    DRAFT = "draft"
    INSPECTED = "inspected"
    KNOWLEDGE_REVIEW_REQUIRED = "knowledge_review_required"
    PLAN_REVIEW_REQUIRED = "plan_review_required"
    APPROVED_FOR_GENERATION = "approved_for_generation"
    GENERATED = "generated"
    VERIFIED = "verified"
    PR_READY = "pr_ready"
    BLOCKED = "blocked"


class ConsoleOperation(str, Enum):
    INSPECT = "inspect"
    PLAN = "plan"
    APPROVE_KNOWLEDGE = "approve_knowledge"
    APPROVE_PLAN = "approve_plan"
    GENERATE = "generate"
    VERIFY = "verify"
    OPEN_PR = "open_pr"


@dataclass(frozen=True)
class FactoryConsoleJob:
    job_id: str
    intake: CompanyIntake
    inspected: bool = False
    generation: GenerationState | None = None
    generation_dispatched: bool = False
    blocked_reason: str | None = None

    @property
    def state(self) -> JobState:
        if self.blocked_reason:
            return JobState.BLOCKED
        if self.generation is None:
            return JobState.INSPECTED if self.inspected else JobState.DRAFT
        if self.generation.stage is Stage.KNOWLEDGE_REVIEW_REQUIRED:
            return JobState.KNOWLEDGE_REVIEW_REQUIRED
        if self.generation.stage is Stage.PLAN_REVIEW_REQUIRED:
            return JobState.PLAN_REVIEW_REQUIRED
        if self.generation.stage is Stage.GENERATED:
            return JobState.GENERATED if self.generation_dispatched else JobState.APPROVED_FOR_GENERATION
        if self.generation.stage is Stage.VERIFIED:
            return JobState.VERIFIED
        if self.generation.stage is Stage.THREE_PRS_OPENED:
            return JobState.PR_READY
        return JobState.BLOCKED


class FactoryOperations(Protocol):
    """The only console-to-factory seam; no command strings cross it."""

    def inspect(self, job: FactoryConsoleJob) -> None: ...
    def plan(self, job: FactoryConsoleJob) -> GenerationState: ...
    def approve_knowledge(self, job: FactoryConsoleJob, generation: GenerationState, digest: str) -> GenerationState: ...
    def approve_plan(self, job: FactoryConsoleJob, generation: GenerationState, digest: str) -> GenerationState: ...
    def generate(self, job: FactoryConsoleJob, generation: GenerationState) -> GenerationState: ...
    def verify(self, job: FactoryConsoleJob, generation: GenerationState) -> GenerationState: ...
    def open_pr(self, job: FactoryConsoleJob, generation: GenerationState) -> GenerationState: ...


class ConsoleJobService:
    """Stores façade metadata; actual generation transitions remain factory-owned."""

    def __init__(self, factory: FactoryOperations) -> None:
        self._factory = factory
        self._jobs: dict[str, FactoryConsoleJob] = {}

    def create(self, payload: Mapping[str, object]) -> FactoryConsoleJob:
        intake = CompanyIntake.from_payload(payload)
        job = FactoryConsoleJob(job_id=f"fc-{uuid4().hex}", intake=intake)
        self._jobs[job.job_id] = job
        return job

    def get(self, job_id: str) -> FactoryConsoleJob:
        try:
            return self._jobs[job_id]
        except KeyError as error:
            raise KeyError("factory console job was not found") from error

    def inspect(self, job_id: str) -> FactoryConsoleJob:
        job = self._require(job_id, JobState.DRAFT)
        try:
            self._factory.inspect(job)
        except FactoryOperationBlocked:
            return self._block(job)
        return self._save(FactoryConsoleJob(job.job_id, job.intake, inspected=True))

    def prepare_plan(self, job_id: str) -> FactoryConsoleJob:
        job = self._require(job_id, JobState.INSPECTED)
        try:
            return self._with_generation(job, self._factory.plan(job))
        except FactoryOperationBlocked:
            return self._block(job)

    def approve_knowledge(self, job_id: str, digest: str) -> FactoryConsoleJob:
        job = self._require(job_id, JobState.KNOWLEDGE_REVIEW_REQUIRED)
        generation = self._generation(job)
        self._require_digest(generation.knowledge_review_digest, digest, "knowledge")
        try:
            return self._with_generation(job, self._factory.approve_knowledge(job, generation, digest))
        except FactoryOperationBlocked:
            return self._block(job)

    def approve_plan(self, job_id: str, digest: str) -> FactoryConsoleJob:
        job = self._require(job_id, JobState.PLAN_REVIEW_REQUIRED)
        generation = self._generation(job)
        self._require_digest(generation.plan_digest, digest, "plan")
        try:
            return self._with_generation(job, self._factory.approve_plan(job, generation, digest))
        except FactoryOperationBlocked:
            return self._block(job)

    def generate(self, job_id: str) -> FactoryConsoleJob:
        job = self._require(job_id, JobState.APPROVED_FOR_GENERATION)
        try:
            generation = self._factory.generate(job, self._generation(job))
        except FactoryOperationBlocked:
            return self._block(job)
        return self._save(FactoryConsoleJob(job.job_id, job.intake, True, generation, True))

    def verify(self, job_id: str) -> FactoryConsoleJob:
        job = self._require(job_id, JobState.GENERATED)
        try:
            return self._with_generation(job, self._factory.verify(job, self._generation(job)), dispatched=True)
        except FactoryOperationBlocked:
            return self._block(job)

    def open_pr(self, job_id: str) -> FactoryConsoleJob:
        job = self._require(job_id, JobState.VERIFIED)
        try:
            return self._with_generation(job, self._factory.open_pr(job, self._generation(job)), dispatched=True)
        except FactoryOperationBlocked:
            return self._block(job)

    def _require(self, job_id: str, expected: JobState) -> FactoryConsoleJob:
        job = self.get(job_id)
        if job.state is not expected:
            raise InvalidStateTransition(f"{expected.value} is required; current state is {job.state.value}")
        return job

    @staticmethod
    def _generation(job: FactoryConsoleJob) -> GenerationState:
        if job.generation is None:
            raise InvalidStateTransition("factory generation is required")
        return job.generation

    @staticmethod
    def _require_digest(expected: str | None, actual: str, label: str) -> None:
        if not isinstance(actual, str) or not expected or actual != expected:
            raise InvalidStateTransition(f"explicit {label} digest confirmation is required")

    def _with_generation(self, job: FactoryConsoleJob, generation: GenerationState, *, dispatched: bool = False) -> FactoryConsoleJob:
        if not isinstance(generation, GenerationState):
            raise FactoryOperationBlocked("factory adapter returned no generation state")
        return self._save(FactoryConsoleJob(job.job_id, job.intake, True, generation, dispatched))

    def _save(self, job: FactoryConsoleJob) -> FactoryConsoleJob:
        self._jobs[job.job_id] = job
        return job

    def _block(self, job: FactoryConsoleJob) -> FactoryConsoleJob:
        """Keep the console fail-closed without copying sensitive factory errors."""
        return self._save(FactoryConsoleJob(
            job.job_id, job.intake, job.inspected, job.generation,
            job.generation_dispatched, "factory operation blocked",
        ))
