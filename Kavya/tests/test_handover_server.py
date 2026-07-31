"""Server-side tests for the unanswered-handoff failsafe.

Asserts the TwiML branch that routes an unanswered transfer back into Kavya in
failsafe mode, the carry-over state that survives the relay restart, and the
recovery system prompt Kavya runs on.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import server  # noqa: E402


@pytest.fixture
def client():
    # TestClient(...) without the lifespan context manager does not run
    # startup/shutdown, so the KB is never loaded - these routes don't need it.
    return TestClient(server.app)


@pytest.fixture(autouse=True)
def clean_handoff_state():
    server._handoff_state.clear()
    yield
    server._handoff_state.clear()


def _dial_result(client, status: str, call_sid: str = "CA_TEST_1"):
    return client.post(
        "/voice/dial-result",
        data={"DialCallStatus": status, "CallSid": call_sid},
    )


# ---------------------------------------------------------------------------
# /voice/dial-result routing
# ---------------------------------------------------------------------------

def test_answered_call_hangs_up_and_clears_carry_over(client):
    server._remember_handoff("CA_TEST_1", reason="wants a discount", caller_phone="+94771234567")

    resp = _dial_result(client, "completed")

    assert resp.status_code == 200
    assert "<Hangup/>" in resp.text
    assert "handover_failsafe" not in resp.text
    # Human took the call - no failsafe should linger.
    assert "CA_TEST_1" not in server._handoff_state


@pytest.mark.parametrize("status", ["no-answer", "busy", "failed", "canceled"])
def test_unanswered_call_reenters_kavya_in_failsafe_mode(client, status):
    server._remember_handoff("CA_TEST_1", reason="wants a discount", caller_phone="+94771234567")

    resp = _dial_result(client, status)

    assert resp.status_code == 200
    # Back into ConversationRelay, flagged as the failsafe session.
    assert "<Connect" in resp.text
    assert "mode=handover_failsafe" in resp.text
    assert "/ws/conversation?lang=en" in resp.text
    # The apology + details request is spoken by Twilio as the greeting.
    assert "could not pick up" in resp.text
    assert "call you straight back" in resp.text
    # Carry-over survives, tagged with why the dial failed.
    assert server._handoff_state["CA_TEST_1"]["dial_status"] == status
    assert server._handoff_state["CA_TEST_1"]["reason"] == "wants a discount"


def test_failsafe_twiml_escapes_the_query_separator(client):
    """`&` must be `&amp;` inside an XML attribute or Twilio rejects the TwiML."""
    resp = _dial_result(client, "no-answer")
    assert "lang=en&amp;mode=handover_failsafe" in resp.text
    assert "lang=en&mode=" not in resp.text


def test_dial_result_survives_an_unknown_call_sid(client):
    """Twilio can post a status for a call we have no carry-over for."""
    resp = _dial_result(client, "no-answer", call_sid="CA_NEVER_SEEN")
    assert resp.status_code == 200
    assert "mode=handover_failsafe" in resp.text


# ---------------------------------------------------------------------------
# Carry-over state
# ---------------------------------------------------------------------------

def test_remember_handoff_merges_without_dropping_earlier_fields():
    server._remember_handoff("CA1", reason="r", caller_phone="+9477", transcript=[{"role": "user", "text": "hi"}])
    server._remember_handoff("CA1", dial_status="no-answer")
    entry = server._handoff_state["CA1"]
    assert entry["reason"] == "r"
    assert entry["dial_status"] == "no-answer"
    assert entry["transcript"] == [{"role": "user", "text": "hi"}]


def test_remember_handoff_ignores_unknown_call_sids():
    server._remember_handoff("", reason="r")
    server._remember_handoff("unknown", reason="r")
    assert server._handoff_state == {}


def test_handoff_state_is_bounded():
    """Abandoned calls never clean themselves up - the dict must not grow forever."""
    for i in range(server._HANDOFF_STATE_MAX + 50):
        server._remember_handoff(f"CA{i}", reason="r")
    assert len(server._handoff_state) <= server._HANDOFF_STATE_MAX
    # Newest entries are the ones kept.
    assert f"CA{server._HANDOFF_STATE_MAX + 49}" in server._handoff_state


# ---------------------------------------------------------------------------
# Failsafe system prompt
# ---------------------------------------------------------------------------

def test_prompt_offers_the_caller_id_when_we_have_it():
    prompt = server._build_handoff_failsafe_prompt({
        "reason": "wants a group discount",
        "caller_phone": "+94771234567",
        "transcript": [
            {"role": "user", "text": "Hi, I'm Chanya"},
            {"role": "assistant", "text": "Hello Chanya, how can I help?"},
        ],
    })
    assert "+94771234567" in prompt
    assert "number you're calling from" in prompt
    # Prior turns carried over so she confirms the name instead of re-asking.
    assert "Guest: Hi, I'm Chanya" in prompt
    assert "Kavya: Hello Chanya, how can I help?" in prompt
    assert "wants a group discount" in prompt
    assert "notify_human_handover" in prompt


def test_prompt_asks_for_a_number_when_the_caller_id_is_missing():
    prompt = server._build_handoff_failsafe_prompt({"caller_phone": "unknown"})
    assert "do NOT have the guest's number" in prompt
    assert "Ask for the WhatsApp number" in prompt
    assert "calling from" not in prompt  # nothing to offer


def test_prompt_survives_a_completely_empty_carry_over():
    """If the pre-transfer session died before stashing, still run the failsafe."""
    prompt = server._build_handoff_failsafe_prompt({})
    assert "notify_human_handover" in prompt
    assert "asked to speak to a human" in prompt


def test_prompt_forbids_an_extra_confirmation_round_trip():
    """Regression: a read-back-the-number rule made Kavya re-confirm a number the
    guest had already agreed to, so she never called the tool and the guest's
    name was lost to the hang-up fallback."""
    prompt = server._build_handoff_failsafe_prompt({"caller_phone": "+94771234567"})
    assert "MOMENT you have a name and a number" in prompt
    assert "Do NOT ask for one more confirmation first" in prompt
    assert "NEVER ask the same question twice" in prompt
    # Read-back applies only to numbers the guest actually dictated.
    assert "If the guest DICTATED a number" in prompt
    assert "ALREADY\nconfirmed" in prompt or "ALREADY confirmed" in prompt.replace("\n", " ")


def test_prompt_forbids_narrating_internal_state():
    """Regression: on a live call Kavya opened with "I don't have your name from
    our earlier conversation - may I have your name please?", leaking what she
    did and did not have on record."""
    prompt = server._build_handoff_failsafe_prompt({"caller_phone": "+94771234567"})
    assert "NEVER narrate your own records" in prompt
    assert "never 'I don't have your name" in prompt
    # The step-1 wording must not invite the model to explain the condition.
    assert "if you genuinely do not have it" not in prompt


def test_prompt_does_not_reopen_the_booking_flow():
    prompt = server._build_handoff_failsafe_prompt({"caller_phone": "+94771234567"})
    assert "Do NOT restart the booking" in prompt
    assert "do NOT try to" in prompt and "transfer them again" in prompt


def test_transcript_rendering_is_capped_and_skips_blank_turns():
    turns = [{"role": "user", "text": f"line {i}"} for i in range(40)]
    turns.append({"role": "assistant", "text": "   "})
    rendered = server._format_handoff_transcript(turns, limit=5)
    assert len(rendered.splitlines()) == 4  # 5 taken, 1 blank dropped
    assert "line 39" in rendered
    assert "line 0" not in rendered


# ---------------------------------------------------------------------------
# Fallback notification (guest hung up before giving details)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fallback_notifies_the_manager_using_the_caller_id():
    sent = {}

    async def _fake_send(**kwargs):
        sent.update(kwargs)
        return {"ok": True}

    with patch.object(server, "send_handover_notification", _fake_send):
        await server._notify_handover_fallback(
            call_sid="CA_TEST_1",
            state={
                "caller_phone": "+94771234567",
                "reason": "wants a group discount",
                "dial_status": "no-answer",
                "human_agent_whatsapp": "+94711754668",
            },
            caller_phone="+94771234567",
            full_transcript=[{"role": "user", "text": "I want a discount"}],
        )

    assert sent["customer_whatsapp"] == "+94771234567"
    assert sent["human_agent_whatsapp"] == "+94711754668"
    assert "hung up before leaving callback details" in sent["call_summary"]
    assert "wants a group discount" in sent["call_summary"]
    assert "no-answer" in sent["call_summary"]
    assert "I want a discount" in sent["call_summary"]


@pytest.mark.asyncio
async def test_fallback_skips_when_there_is_nothing_actionable():
    """No number anywhere - a notification the manager can't act on is noise."""
    called = False

    async def _fake_send(**kwargs):
        nonlocal called
        called = True
        return {"ok": True}

    with patch.object(server, "send_handover_notification", _fake_send):
        await server._notify_handover_fallback(
            call_sid="CA_TEST_1",
            state={},
            caller_phone="unknown",
            full_transcript=[],
        )

    assert called is False


# ---------------------------------------------------------------------------
# Tool gating in the failsafe session
# ---------------------------------------------------------------------------

def test_relay_twiml_has_no_mode_param_for_normal_calls():
    tag = server._build_conversation_relay_twiml(
        "voice.taskforceai.tech", "en", server.LANGUAGE_CONFIGS["en"],
    )
    assert "mode=" not in tag
    assert "?lang=en" in tag


def test_normal_call_offers_transfer_but_not_notify():
    from tools import TOOL_DEFINITIONS

    names = {t["name"] for t in TOOL_DEFINITIONS}
    assert "transfer_to_human" in names
    assert "notify_human_handover" not in names
