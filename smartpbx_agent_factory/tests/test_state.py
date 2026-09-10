import pytest

from smartpbx_agent_factory.state import GenerationState, Stage, StateError


def test_state_approval_is_digest_bound():
    state = GenerationState.start("gen-001", "manifest-digest")
    state.transition(Stage.KNOWLEDGE_REVIEW_REQUIRED)
    with pytest.raises(StateError, match="approval digest"):
        state.approve_knowledge("wrong")


def test_state_advances_only_through_review_approvals():
    state = GenerationState.start("gen-002", "manifest-digest")
    state.transition(Stage.INPUT_COLLECTED)
    state.transition(Stage.SECRETS_RESOLVED)
    state.transition(Stage.KNOWLEDGE_REVIEW_REQUIRED)
    state.approve_knowledge("manifest-digest")
    assert state.stage is Stage.PLAN_REVIEW_REQUIRED
    state.approve_plan("manifest-digest")
    assert state.stage is Stage.GENERATED


def test_state_rejects_skipping_generation_stages():
    state = GenerationState.start("gen-003", "manifest-digest")
    with pytest.raises(StateError, match="transition"):
        state.transition(Stage.VERIFIED)


def test_state_can_be_blocked_and_pre_pr_state_abandoned():
    state = GenerationState.start("gen-004", "manifest-digest")
    state.block("provenance unavailable")
    assert state.stage is Stage.BLOCKED
    assert state.blocked_reason == "provenance unavailable"

    abandoned = GenerationState.start("gen-005", "manifest-digest")
    abandoned.abandon()
    assert abandoned.stage is Stage.ABANDONED
