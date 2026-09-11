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
    Stage.NEW: frozenset({Stage.INPUT_COLLECTED}),
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
    knowledge_review_digest: str | None = None
    plan_digest: str | None = None
    knowledge_approval_digest: str | None = None
    plan_approval_digest: str | None = None
    stage_digests: dict[str, str] = field(default_factory=dict)
    lane_records: dict[str, dict[str, str]] = field(default_factory=dict)
    blocked_reason: str | None = None
    pr_provider_recovery: bool = False

    @classmethod
    def start(cls, generation_id: str, manifest_digest: str) -> "GenerationState":
        if not generation_id or not manifest_digest:
            raise StateError("generation_id and manifest_digest are required")
        return cls(generation_id=generation_id, manifest_digest=manifest_digest)

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

    def record_knowledge_review_digest(self, digest: str) -> None:
        self._record_review_digest(Stage.KNOWLEDGE_REVIEW_REQUIRED, "knowledge", digest)

    def approve_plan(self, digest: str) -> None:
        self._require_approval(Stage.PLAN_REVIEW_REQUIRED, digest, "plan")
        self.plan_approval_digest = digest
        self.transition(Stage.GENERATED)

    def record_plan_digest(self, digest: str) -> None:
        self._record_review_digest(Stage.PLAN_REVIEW_REQUIRED, "plan", digest)

    def _record_review_digest(self, expected_stage: Stage, name: str, digest: str) -> None:
        if self.stage is not expected_stage:
            raise StateError(f"{name} review digest requires stage {expected_stage.value}")
        if not isinstance(digest, str) or not digest:
            raise StateError(f"{name} review digest must not be empty")
        attribute = "knowledge_review_digest" if name == "knowledge" else "plan_digest"
        if getattr(self, attribute) is not None:
            raise StateError(f"{name} review digest already recorded")
        setattr(self, attribute, digest)

    def _require_approval(self, expected: Stage, digest: str, name: str) -> None:
        if self.stage is not expected:
            raise StateError(f"{name} approval requires stage {expected.value}")
        expected_digest = (
            self.knowledge_review_digest if name == "knowledge" else self.plan_digest
        )
        if expected_digest is None:
            raise StateError(f"expected {name} review digest is required")
        if digest != expected_digest:
            raise StateError(f"{name} approval digest does not match expected reviewed artifact")

    def record_stage_digest(self, name: str, digest: str) -> None:
        if not name or not digest:
            raise StateError("stage digest name and value are required")
        self.stage_digests[name] = digest

    def record_lane(self, lane: str, *, output_digest: str, head_sha: str, artifact_digest: str, ciphertext_reference: str = "", published_remote_sha: str = "") -> None:
        """Persist only bounded non-secret resume evidence for one owned lane."""
        if lane not in {"backend", "operations", "website"}:
            raise StateError("lane is invalid")
        values = (output_digest, artifact_digest)
        if any(not isinstance(value, str) or len(value) != 64 or set(value) - set("0123456789abcdef") for value in values) or (
            not isinstance(head_sha, str) or len(head_sha) != 40 or set(head_sha) - set("0123456789abcdef")
        ):
            raise StateError("lane digest is invalid")
        if ciphertext_reference and (not isinstance(ciphertext_reference, str) or len(ciphertext_reference) > 240 or "\x00" in ciphertext_reference):
            raise StateError("ciphertext reference is invalid")
        if published_remote_sha and (
            not isinstance(published_remote_sha, str)
            or len(published_remote_sha) != 40
            or set(published_remote_sha) - set("0123456789abcdef")
        ):
            raise StateError("published remote SHA is invalid")
        self.lane_records[lane] = {
            "output_digest": output_digest,
            "head_sha": head_sha,
            "artifact_digest": artifact_digest,
            "ciphertext_reference": ciphertext_reference,
            "published_remote_sha": published_remote_sha,
        }

    def block(self, reason: str) -> None:
        if self.stage is Stage.THREE_PRS_OPENED:
            raise StateError("cannot block a generation after three PRs opened")
        if not reason or "\x00" in reason:
            raise StateError("blocked reason is required")
        self.blocked_reason = reason
        self.pr_provider_recovery = False
        self.stage = Stage.BLOCKED

    def block_pr_provider(self, reason: str) -> None:
        """Block a provider-side PR interruption, preserving the sole retry path."""
        if self.stage is not Stage.VERIFIED:
            raise StateError("PR provider failure requires stage VERIFIED")
        if not reason or "\x00" in reason:
            raise StateError("blocked reason is required")
        self.blocked_reason = reason
        self.pr_provider_recovery = True
        self.stage = Stage.BLOCKED

    def recover_pr_provider_block(self) -> None:
        """Re-open only a durably recorded PR-provider interruption for validation."""
        if self.stage is not Stage.BLOCKED or not self.pr_provider_recovery:
            raise StateError("only a PR-provider-blocked generation may recover")
        self.stage = Stage.VERIFIED
        self.blocked_reason = None
        self.pr_provider_recovery = False

    def abandon(self) -> None:
        if self.stage is Stage.THREE_PRS_OPENED:
            raise StateError("cannot abandon after three PRs opened")
        self.stage = Stage.ABANDONED

    def to_dict(self) -> dict[str, Any]:
        return {
            "generation_id": self.generation_id,
            "manifest_digest": self.manifest_digest,
            "knowledge_review_digest": self.knowledge_review_digest,
            "plan_digest": self.plan_digest,
            "stage": self.stage.value,
            "knowledge_approval_digest": self.knowledge_approval_digest,
            "plan_approval_digest": self.plan_approval_digest,
            "stage_digests": dict(self.stage_digests),
            "lane_records": {name: dict(value) for name, value in self.lane_records.items()},
            "blocked_reason": self.blocked_reason,
            "pr_provider_recovery": self.pr_provider_recovery,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "GenerationState":
        try:
            state = cls(
                generation_id=raw["generation_id"],
                manifest_digest=raw["manifest_digest"],
                knowledge_review_digest=raw.get("knowledge_review_digest"),
                plan_digest=raw.get("plan_digest"),
                stage=Stage(raw["stage"]),
                knowledge_approval_digest=raw.get("knowledge_approval_digest"),
                plan_approval_digest=raw.get("plan_approval_digest"),
                stage_digests=dict(raw.get("stage_digests", {})),
                lane_records={str(name): dict(value) for name, value in dict(raw.get("lane_records", {})).items()},
                blocked_reason=raw.get("blocked_reason"),
                pr_provider_recovery=raw.get("pr_provider_recovery", False),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise StateError("invalid serialized generation state") from exc
        _require_stage_artifacts(state)
        for lane, record in state.lane_records.items():
            if not isinstance(record, dict):
                raise StateError("serialized lane record is invalid")
            state.record_lane(lane, **record)
        if (
            state.knowledge_approval_digest is not None
            and state.knowledge_approval_digest != state.knowledge_review_digest
        ):
            raise StateError("knowledge approval digest does not match expected review digest")
        if state.plan_approval_digest is not None and state.plan_approval_digest != state.plan_digest:
            raise StateError("plan approval digest does not match expected review digest")
        if not isinstance(state.pr_provider_recovery, bool):
            raise StateError("serialized PR recovery marker is invalid")
        if state.pr_provider_recovery and state.stage is not Stage.BLOCKED:
            raise StateError("serialized PR recovery marker requires stage BLOCKED")
        return state


def _require_stage_artifacts(state: GenerationState) -> None:
    if state.stage in {
        Stage.PLAN_REVIEW_REQUIRED,
        Stage.GENERATED,
        Stage.VERIFIED,
        Stage.THREE_PRS_OPENED,
    }:
        if state.knowledge_review_digest is None or state.knowledge_approval_digest is None:
            raise StateError("serialized stage requires knowledge approval")
    if state.stage in {Stage.GENERATED, Stage.VERIFIED, Stage.THREE_PRS_OPENED}:
        if state.plan_digest is None or state.plan_approval_digest is None:
            raise StateError("serialized stage requires plan approval")
