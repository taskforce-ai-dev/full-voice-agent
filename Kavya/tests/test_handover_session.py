"""End-to-end test of the failsafe ConversationRelay session.

Drives a real `/ws/conversation?...&mode=handover_failsafe` WebSocket the way
Twilio does (setup → prompt) with only the LLM and the n8n POST stubbed, and
asserts the whole chain: carry-over restored → failsafe prompt → notify-only
tool set → payload on the wire → safety net stands down.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import server  # noqa: E402
import tools as tools_mod  # noqa: E402
from handover import normalize_whatsapp  # noqa: E402

CALL_SID = "CA_E2E_1"
FAILSAFE_URL = "/ws/conversation?lang=en&mode=handover_failsafe"


class _FakeResponse:
    def __init__(self, status=200, body='{"ok":true}'):
        self.status, self._body = status, body

    async def text(self):
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    def __init__(self):
        self.calls = []

    def post(self, url, json=None, **kwargs):  # noqa: A002
        self.calls.append((url, json))
        return _FakeResponse()


@pytest.fixture
def n8n():
    """Capture what would have been POSTed to the n8n handover webhook."""
    session = _FakeSession()

    async def _get_session():
        return session

    import booking_api

    with patch.object(booking_api, "get_session", _get_session):
        yield session


@pytest.fixture(autouse=True)
def isolate(n8n):
    """Stub the LLM client and post-call processing; reset carry-over state."""
    server._handoff_state.clear()

    async def _noop_post_call(**kwargs):
        return None

    with patch.object(server, "_get_anthropic_client", lambda: object()), \
         patch.object(server, "process_post_call_data", _noop_post_call), \
         patch.object(server, "dashboard_client", None):
        yield
    server._handoff_state.clear()


def _seed_carry_over(**overrides):
    state = {
        "reason": "Guest wants a group discount for a buyout.",
        "caller_phone": "+94771234567",
        "transcript": [
            {"role": "user", "text": "Hi, this is Chanya, I need a group rate"},
            {"role": "assistant", "text": "Let me connect you with our team, Chanya."},
        ],
        "dial_status": "no-answer",
        "notified": False,
    }
    state.update(overrides)
    server._remember_handoff(CALL_SID, **state)
    return server._handoff_state[CALL_SID]


def _run_session(fake_llm, url: str = FAILSAFE_URL, prompt: str = "Yes, that's my number"):
    """Open the relay WebSocket, send setup + one prompt, then hang up.

    The stubbed LLM never streams tokens back, so the test cannot wait on a
    WebSocket read. It waits on the stub itself instead, then drops the line -
    which is also what triggers the end-of-session safety net.
    """
    turn_done = threading.Event()

    async def _instrumented(**kwargs):
        try:
            return await fake_llm(**kwargs)
        finally:
            turn_done.set()

    with patch.object(server, "_run_llm_streaming_claude", _instrumented):
        # Bare TestClient(...) - entering it as a context manager would run the
        # lifespan and load the whole ChromaDB knowledge base, which these
        # tests do not touch.
        client = TestClient(server.app)
        with client.websocket_connect(url) as ws:
            ws.send_json({"type": "setup", "callSid": CALL_SID, "from": "+94771234567"})
            ws.send_json({"type": "prompt", "voicePrompt": prompt})
            assert turn_done.wait(20), "the session never processed the prompt"
    # The `finally` block (safety net + post-call) runs as the socket tears
    # down; give the event loop a moment to get there.
    deadline = time.monotonic() + 5
    while CALL_SID in server._handoff_state and time.monotonic() < deadline:
        time.sleep(0.02)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_failsafe_session_collects_details_and_messages_the_manager(n8n):
    state = _seed_carry_over()
    seen = {}

    async def fake_llm(*, client, system, conversation_history, tools, websocket, lang):
        seen["system"] = system
        seen["tools"] = [t["name"] for t in tools]
        # A model can only call a tool it was actually offered. The failsafe
        # session now runs an OPENING turn (to break the silence before the
        # guest speaks) with tools=[], so honour that here — a stub that fires
        # the tool regardless of `tools` would report a double-send that cannot
        # happen in production. This still catches the real regression: offer
        # the notify tool on the opening turn and this fires twice.
        if "notify_human_handover" not in seen["tools"]:
            return "May I have your name please?"
        # This is what the model does once it has the name and the number.
        seen["tool_result"] = await tools_mod.execute_tool("notify_human_handover", {
            "customer_name": "Chanya",
            "customer_whatsapp": "0771234567",
            "call_summary": "Guest wanted a group discount for a buyout, Aug 1-3.",
        })
        return "I've passed your details to our team, they'll call you shortly."

    _run_session(fake_llm)

    # The recovery prompt, not the normal booking prompt.
    assert "YOUR ONLY JOB NOW" in seen["system"]
    assert "group discount" in seen["system"]
    assert "Guest: Hi, this is Chanya" in seen["system"]  # prior turns restored
    assert "+94771234567" in seen["system"]               # caller ID offered

    # Only the notify tool - no wandering back into the booking flow.
    assert seen["tools"] == ["notify_human_handover"]

    # The manager notification actually went out, correctly shaped.
    assert len(n8n.calls) == 1
    url, payload = n8n.calls[0]
    assert url == "https://automation.taskforceai.tech/webhook/kavya-handover"
    assert payload["call_sid"] == CALL_SID
    assert payload["customer_name"] == "Chanya"
    assert payload["customer_whatsapp"] == "94771234567"
    assert payload["human_agent_whatsapp"] == normalize_whatsapp(server.HUMAN_AGENT_PHONE)
    assert "group discount" in payload["call_summary"]

    # Safety net stands down - no duplicate message to the manager.
    assert state["notified"] is True
    assert len(n8n.calls) == 1


# ---------------------------------------------------------------------------
# Safety net
# ---------------------------------------------------------------------------

def test_guest_hangs_up_before_giving_details_still_notifies_the_manager(n8n):
    _seed_carry_over()

    async def fake_llm(**kwargs):
        return "Sure, may I take your name?"  # guest hangs up right after

    _run_session(fake_llm)

    assert len(n8n.calls) == 1, "manager must still hear about the missed call"
    _, payload = n8n.calls[0]
    # Falls back to the number the guest rang from.
    assert payload["customer_whatsapp"] == "94771234567"
    assert "hung up before leaving callback details" in payload["call_summary"]
    assert "no-answer" in payload["call_summary"]


def test_safety_net_does_not_double_send_after_a_successful_tool_call(n8n):
    _seed_carry_over()

    async def fake_llm(**kwargs):
        # Only fire when the tool is actually on offer — the failsafe opening
        # turn deliberately runs with tools=[]. See the note in the happy-path
        # test above.
        if not any(
            t["name"] == "notify_human_handover" for t in kwargs.get("tools", [])
        ):
            return "May I have your name please?"
        await tools_mod.execute_tool("notify_human_handover", {
            "customer_name": "Chanya",
            "customer_whatsapp": "94771234567",
            "call_summary": "x",
        })
        return "Done, they'll call you back."

    _run_session(fake_llm)
    assert len(n8n.calls) == 1


# ---------------------------------------------------------------------------
# Normal calls must be untouched
# ---------------------------------------------------------------------------

def test_a_normal_session_is_not_in_failsafe_mode(n8n):
    seen = {}

    async def fake_llm(*, client, system, conversation_history, tools, websocket, lang):
        seen["system"] = system
        seen["tools"] = [t["name"] for t in tools]
        return "Welcome! How can I help?"

    # This test exercises normal-call tool selection, not PMS credentials. The
    # Yanolja client now correctly fails closed without explicit credentials.
    with patch.object(tools_mod, "is_configured", return_value=True):
        _run_session(fake_llm, url="/ws/conversation?lang=en", prompt="Do you have rooms?")

    assert "YOUR ONLY JOB NOW" not in seen["system"]
    assert "notify_human_handover" not in seen["tools"]
    assert "transfer_to_human" in seen["tools"]
    assert n8n.calls == [], "a normal call must never message the manager"


def test_failsafe_turn_skips_the_knowledge_base(n8n):
    """Hotel facts are noise when all Kavya needs is a name and a number."""
    _seed_carry_over()
    seen = {}
    kb_hits = []

    async def fake_llm(*, client, system, conversation_history, tools, websocket, lang):
        seen["history"] = list(conversation_history)
        return "ok"

    with patch.object(server, "retrieve_context", lambda t: kb_hits.append(t) or "rooms cost x"):
        _run_session(fake_llm, prompt="It's oh seven seven one two three four five six seven")

    assert kb_hits == [], "no KB lookup should happen in the failsafe session"
    assert "Reference context" not in seen["history"][-1]["content"]


def test_failsafe_mode_is_ignored_on_non_english_paths(n8n):
    """Sinhala/Arabic run on Media Streams; there is no transfer to fail there."""
    _seed_carry_over()
    seen = {}

    async def fake_llm(*, client, system, conversation_history, tools, websocket, lang):
        seen["system"] = system
        return "ok"

    _run_session(fake_llm, url="/ws/conversation?lang=si&mode=handover_failsafe")

    assert "YOUR ONLY JOB NOW" not in seen["system"]
    assert n8n.calls == []
