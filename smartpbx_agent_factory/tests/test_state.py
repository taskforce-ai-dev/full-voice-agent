import pytest

from smartpbx_agent_factory.state import GenerationState, Stage, StateError


def test_state_approval_is_digest_bound():
    state = GenerationState.start("gen-001", "manifest-digest", "knowledge-digest", "plan-digest")
    state.transition(Stage.INPUT_COLLECTED)
    state.transition(Stage.SECRETS_RESOLVED)
    state.transition(Stage.KNOWLEDGE_REVIEW_REQUIRED)
    with pytest.raises(StateError, match="approval digest"):
        state.approve_knowledge("wrong")


def test_state_advances_only_through_review_approvals():
    state = GenerationState.start("gen-002", "manifest-digest", "knowledge-digest", "plan-digest")
    state.transition(Stage.INPUT_COLLECTED)
    state.transition(Stage.SECRETS_RESOLVED)
    state.transition(Stage.KNOWLEDGE_REVIEW_REQUIRED)
    state.approve_knowledge("knowledge-digest")
    assert state.stage is Stage.PLAN_REVIEW_REQUIRED
    state.approve_plan("plan-digest")
    assert state.stage is Stage.GENERATED


def test_state_rejects_skipping_generation_stages():
    state = GenerationState.start("gen-003", "manifest-digest", "knowledge-digest", "plan-digest")
    with pytest.raises(StateError, match="transition"):
        state.transition(Stage.VERIFIED)


def test_state_can_be_blocked_and_pre_pr_state_abandoned():
    state = GenerationState.start("gen-004", "manifest-digest", "knowledge-digest", "plan-digest")
    state.block("provenance unavailable")
    assert state.stage is Stage.BLOCKED
    assert state.blocked_reason == "provenance unavailable"

    abandoned = GenerationState.start("gen-005", "manifest-digest", "knowledge-digest", "plan-digest")
    abandoned.abandon()
    assert abandoned.stage is Stage.ABANDONED


def test_state_does_not_shortcut_new_to_knowledge_review():
    state = GenerationState.start("gen-006", "manifest-digest", "knowledge-digest", "plan-digest")
    with pytest.raises(StateError, match="transition"):
        state.transition(Stage.KNOWLEDGE_REVIEW_REQUIRED)


def test_state_serializes_expected_and_approved_review_digests():
    state = GenerationState.start("gen-007", "manifest-digest", "knowledge-digest", "plan-digest")
    state.transition(Stage.INPUT_COLLECTED)
    state.transition(Stage.SECRETS_RESOLVED)
    state.transition(Stage.KNOWLEDGE_REVIEW_REQUIRED)
    state.approve_knowledge("knowledge-digest")
    state.approve_plan("plan-digest")
    restored = GenerationState.from_dict(state.to_dict())
    assert restored.knowledge_review_digest == "knowledge-digest"
    assert restored.plan_digest == "plan-digest"


def test_state_rejects_stale_serialized_approval():
    state = GenerationState.start("gen-008", "manifest-digest", "knowledge-digest", "plan-digest")
    raw = state.to_dict()
    raw["knowledge_approval_digest"] = "stale"
    with pytest.raises(StateError, match="approval"):
        GenerationState.from_dict(raw)


def test_state_records_review_digests_at_owned_stages_and_refuses_rebind():
    state = GenerationState.start("gen-009", "manifest-digest")
    state.transition(Stage.INPUT_COLLECTED)
    state.transition(Stage.SECRETS_RESOLVED)
    state.transition(Stage.KNOWLEDGE_REVIEW_REQUIRED)
    state.record_knowledge_review_digest("knowledge-digest")
    with pytest.raises(StateError, match="already recorded"):
        state.record_knowledge_review_digest("knowledge-digest")
    state.approve_knowledge("knowledge-digest")
    state.record_plan_digest("plan-digest")
    with pytest.raises(StateError, match="already recorded"):
        state.record_plan_digest("other-plan-digest")
    state.approve_plan("plan-digest")


def test_state_rejects_approval_without_recorded_artifact_digest():
    state = GenerationState.start("gen-010", "manifest-digest")
    state.transition(Stage.INPUT_COLLECTED)
    state.transition(Stage.SECRETS_RESOLVED)
    state.transition(Stage.KNOWLEDGE_REVIEW_REQUIRED)
    with pytest.raises(StateError, match="expected knowledge review digest"):
        state.approve_knowledge("knowledge-digest")


def test_state_rejects_later_serialized_stage_without_required_approvals():
    raw = GenerationState.start(
        "gen-011", "manifest-digest", "knowledge-digest", "plan-digest"
    ).to_dict()
    raw["stage"] = Stage.GENERATED.value
    with pytest.raises(StateError, match="approval"):
        GenerationState.from_dict(raw)
