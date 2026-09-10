"""Digest-bound resumable generation state."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class StateError(ValueError):
    """Raised when a generation state transition or approval is invalid."""


class Stage(str, Enum):
    NEW = "NEW"
    INPUT_COLLECTED = "INPUT_COLLECTED"
    SECRETS_RESOLVED = "SECRETS_RESOLVED"
    KNOWLEDGE_REVIEW_REQUIRED = "KNOWLEDGE_REVIEW_REQUIRED"
    PLAN_REVIEW_REQUIRED = "PLAN_REVIEW_REQUIRED"
    GENERATED = "GENERATED"
    VERIFIED = "VERIFIED"
    THREE_PRS_OPENED = "THREE_PRS_OPENED"
    BLOCKED = "BLOCKED"
    ABANDONED = "ABANDONED"


_NEXT: dict[Stage, frozenset[Stage]] = {
    Stage.NEW: frozenset({Stage.INPUT_COLLECTED, Stage.KNOWLEDGE_REVIEW_REQUIRED}),
    Stage.INPUT_COLLECTED: frozenset({Stage.SECRETS_RESOLVED}),
    Stage.SECRETS_RESOLVED: frozenset({Stage.KNOWLEDGE_REVIEW_REQUIRED}),
    Stage.KNOWLEDGE_REVIEW_REQUIRED: frozenset({Stage.PLAN_REVIEW_REQUIRED}),
    Stage.PLAN_REVIEW_REQUIRED: frozenset({Stage.GENERATED}),
    Stage.GENERATED: frozenset({Stage.VERIFIED}),
    Stage.VERIFIED: frozenset({Stage.THREE_PRS_OPENED}),
    Stage.THREE_PRS_OPENED: frozenset(),
    Stage.BLOCKED: frozenset(),
    Stage.ABANDONED: frozenset(),
}


@dataclass
class GenerationState:
    generation_id: str
    manifest_digest: str
    stage: Stage = Stage.NEW
    knowledge_approval_digest: str | None = None
    plan_approval_digest: str | None = None
    stage_digests: dict[str, str] = field(default_factory=dict)
    blocked_reason: str | None = None

    @classmethod
    def start(cls, generation_id: str, manifest_digest: str) -> "GenerationState":
        if not generation_id or not manifest_digest:
            raise StateError("generation_id and manifest_digest are required")
        return cls(generation_id, manifest_digest)

    def transition(self, target: Stage) -> None:
        if not isinstance(target, Stage):
            raise StateError("transition target must be a Stage")
        if target not in _NEXT[self.stage]:
            raise StateError(f"invalid transition {self.stage.value} -> {target.value}")
        self.stage = target

    def approve_knowledge(self, digest: str) -> None:
        self._require_approval(Stage.KNOWLEDGE_REVIEW_REQUIRED, digest, "knowledge")
        self.knowledge_approval_digest = digest
        self.transition(Stage.PLAN_REVIEW_REQUIRED)

    def approve_plan(self, digest: str) -> None:
        self._require_approval(Stage.PLAN_REVIEW_REQUIRED, digest, "plan")
        self.plan_approval_digest = digest
        self.transition(Stage.GENERATED)

    def _require_approval(self, expected: Stage, digest: str, name: str) -> None:
        if self.stage is not expected:
            raise StateError(f"{name} approval requires stage {expected.value}")
        if digest != self.manifest_digest:
            raise StateError(f"{name} approval digest does not match manifest digest")

    def record_stage_digest(self, name: str, digest: str) -> None:
        if not name or not digest:
            raise StateError("stage digest name and value are required")
        self.stage_digests[name] = digest

    def block(self, reason: str) -> None:
        if self.stage is Stage.THREE_PRS_OPENED:
            raise StateError("cannot block a generation after three PRs opened")
        if not reason or "\x00" in reason:
            raise StateError("blocked reason is required")
        self.blocked_reason = reason
        self.stage = Stage.BLOCKED

    def abandon(self) -> None:
        if self.stage is Stage.THREE_PRS_OPENED:
            raise StateError("cannot abandon after three PRs opened")
        self.stage = Stage.ABANDONED

    def to_dict(self) -> dict[str, Any]:
        return {
            "generation_id": self.generation_id,
            "manifest_digest": self.manifest_digest,
            "stage": self.stage.value,
            "knowledge_approval_digest": self.knowledge_approval_digest,
            "plan_approval_digest": self.plan_approval_digest,
            "stage_digests": dict(self.stage_digests),
            "blocked_reason": self.blocked_reason,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "GenerationState":
        try:
            state = cls(
                generation_id=raw["generation_id"],
                manifest_digest=raw["manifest_digest"],
                stage=Stage(raw["stage"]),
                knowledge_approval_digest=raw.get("knowledge_approval_digest"),
                plan_approval_digest=raw.get("plan_approval_digest"),
                stage_digests=dict(raw.get("stage_digests", {})),
                blocked_reason=raw.get("blocked_reason"),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise StateError("invalid serialized generation state") from exc
        return state
