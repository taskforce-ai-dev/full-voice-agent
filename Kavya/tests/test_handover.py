"""Tests for the unanswered-handoff failsafe.

Covers the whole path: a human agent fails to pick up a transferred call →
Kavya re-enters in failsafe mode → she collects the guest's name and WhatsApp
number → the property manager gets a WhatsApp via n8n.

No network. The n8n POST is stubbed at the aiohttp-session boundary.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import handover  # noqa: E402
from handover import (  # noqa: E402
    build_payload,
    normalize_whatsapp,
    send_handover_notification,
)

# The exact payload the operator specified, used as the contract fixture.
SAMPLE_PAYLOAD_KEYS = {
    "call_sid",
    "customer_name",
    "customer_whatsapp",
    "call_summary",
    "human_agent_whatsapp",
    "timestamp",
}


# ---------------------------------------------------------------------------
# Fake aiohttp session
# ---------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, status: int, body: str) -> None:
        self.status = status
        self._body = body

    async def text(self) -> str:
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    """Records every POST so tests can assert on the exact wire payload."""

    def __init__(self, status: int = 200, body: str = '{"ok":true}') -> None:
        self.status = status
        self.body = body
        self.calls: list[tuple[str, dict]] = []

    def post(self, url, json=None, **kwargs):  # noqa: A002 - aiohttp's kwarg name
        self.calls.append((url, json))
        return _FakeResponse(self.status, self.body)


def _patch_session(session: _FakeSession):
    """Patch the aiohttp session `handover` imports lazily from booking_api."""
    async def _get_session():
        return session

    import booking_api

    return patch.object(booking_api, "get_session", _get_session)


# ---------------------------------------------------------------------------
# normalize_whatsapp
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("+94771234567", "94771234567"),      # international, plus sign
        ("94771234567", "94771234567"),       # already normalised
        ("0771234567", "94771234567"),        # local trunk form
        ("771234567", "94771234567"),         # bare national number
        ("+94 77 123 4567", "94771234567"),   # spaces
        ("077-123 4567", "94771234567"),      # punctuation
        ("+94711754668", "94711754668"),      # the real manager number
        ("+1 415 555 0132", "14155550132"),   # foreign guest, left intact
        ("", ""),
        (None, ""),
        ("   ", ""),
        ("abc", ""),
        ("0", ""),                            # only a trunk prefix - unusable
    ],
)
def test_normalize_whatsapp(raw, expected):
    assert normalize_whatsapp(raw) == expected


def test_normalize_whatsapp_never_double_prefixes():
    """The bug that bit the post-call processor: 0711754668 -> 947171754668."""
    assert normalize_whatsapp("0711754668") == "94711754668"
    assert normalize_whatsapp(normalize_whatsapp("0711754668")) == "94711754668"


# ---------------------------------------------------------------------------
# build_payload
# ---------------------------------------------------------------------------

def test_build_payload_matches_the_specified_contract():
    payload = build_payload(
        call_sid="CA123...",
        customer_name="Chanya",
        customer_whatsapp="0771234567",
        call_summary="Guest asked about availability Aug 1-3, 2 guests, Deluxe room.",
        human_agent_whatsapp="+94770000000",
    )
    assert set(payload) == SAMPLE_PAYLOAD_KEYS
    assert payload["call_sid"] == "CA123..."
    assert payload["customer_name"] == "Chanya"
    assert payload["customer_whatsapp"] == "94771234567"
    assert payload["human_agent_whatsapp"] == "94770000000"
    # 2026-07-31T08:30:00Z
    assert payload["timestamp"].endswith("Z")
    assert len(payload["timestamp"]) == 20


def test_build_payload_defaults_missing_name_and_sid():
    payload = build_payload(
        call_sid="",
        customer_name="",
        customer_whatsapp="94771234567",
        call_summary="",
        human_agent_whatsapp="94770000000",
    )
    assert payload["call_sid"] == "unknown"
    assert payload["customer_name"] == "Unknown"


# ---------------------------------------------------------------------------
# send_handover_notification
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_send_posts_normalised_payload_to_the_handover_webhook():
    session = _FakeSession()
    with _patch_session(session):
        result = await send_handover_notification(
            call_sid="CA999",
            customer_name="Chanya",
            customer_whatsapp="0771234567",
            call_summary="Wanted a group discount.",
            human_agent_whatsapp="+94711754668",
        )

    assert result["ok"] is True
    assert len(session.calls) == 1
    url, body = session.calls[0]
    assert url == "https://automation.taskforceai.tech/webhook/kavya-handover"
    assert body["customer_whatsapp"] == "94771234567"
    assert body["human_agent_whatsapp"] == "94711754668"
    assert body["call_sid"] == "CA999"
    assert set(body) == SAMPLE_PAYLOAD_KEYS


@pytest.mark.asyncio
async def test_send_refuses_without_a_usable_customer_number():
    """Never fire a notification the manager cannot act on."""
    session = _FakeSession()
    with _patch_session(session):
        result = await send_handover_notification(
            call_sid="CA999",
            customer_name="Chanya",
            customer_whatsapp="not a number",
            call_summary="x",
            human_agent_whatsapp="+94711754668",
        )

    assert result["ok"] is False
    assert result["error"] == "missing_customer_whatsapp"
    assert session.calls == []


@pytest.mark.asyncio
async def test_send_reports_non_2xx_without_raising():
    session = _FakeSession(status=500, body="boom")
    with _patch_session(session):
        result = await send_handover_notification(
            call_sid="CA999",
            customer_name="Chanya",
            customer_whatsapp="94771234567",
            call_summary="x",
            human_agent_whatsapp="94711754668",
        )
    assert result["ok"] is False
    assert result["status"] == 500


@pytest.mark.asyncio
async def test_send_swallows_transport_errors():
    """A dead n8n must never take down a live call."""
    async def _boom():
        raise ConnectionError("n8n down")

    import booking_api

    with patch.object(booking_api, "get_session", _boom):
        result = await send_handover_notification(
            call_sid="CA999",
            customer_name="Chanya",
            customer_whatsapp="94771234567",
            call_summary="x",
            human_agent_whatsapp="94711754668",
        )
    assert result["ok"] is False


# ---------------------------------------------------------------------------
# Tool layer
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_notify_tool_sends_and_marks_the_call_notified():
    from tools import execute_tool

    ctx = {"call_sid": "CA555", "human_agent_whatsapp": "+94711754668", "notified": False}
    token = handover.handover_context.set(ctx)
    session = _FakeSession()
    try:
        with _patch_session(session):
            raw = await execute_tool("notify_human_handover", {
                "customer_name": "Chanya",
                "customer_whatsapp": "0771234567",
                "call_summary": "Wanted a group discount for Aug 1-3.",
            })
    finally:
        handover.handover_context.reset(token)

    assert json.loads(raw)["status"] == "sent"
    # The session's end-of-call safety net must now stand down.
    assert ctx["notified"] is True
    _, body = session.calls[0]
    assert body["call_sid"] == "CA555"
    assert body["customer_whatsapp"] == "94771234567"
    assert body["human_agent_whatsapp"] == "94711754668"


@pytest.mark.asyncio
async def test_notify_tool_asks_for_a_rerun_on_an_unusable_number():
    from tools import execute_tool

    ctx = {"call_sid": "CA555", "human_agent_whatsapp": "+94711754668", "notified": False}
    token = handover.handover_context.set(ctx)
    session = _FakeSession()
    try:
        with _patch_session(session):
            raw = await execute_tool("notify_human_handover", {
                "customer_name": "Chanya",
                "customer_whatsapp": "",
                "call_summary": "x",
            })
    finally:
        handover.handover_context.reset(token)

    assert json.loads(raw)["status"] == "invalid_number"
    assert ctx["notified"] is False  # must retry, not silently give up


@pytest.mark.asyncio
async def test_notify_tool_does_not_claim_success_when_n8n_fails():
    from tools import execute_tool

    ctx = {"call_sid": "CA555", "human_agent_whatsapp": "+94711754668", "notified": False}
    token = handover.handover_context.set(ctx)
    try:
        with _patch_session(_FakeSession(status=503, body="nope")):
            raw = await execute_tool("notify_human_handover", {
                "customer_name": "Chanya",
                "customer_whatsapp": "94771234567",
                "call_summary": "x",
            })
    finally:
        handover.handover_context.reset(token)

    assert json.loads(raw)["status"] == "failed"
    assert ctx["notified"] is False


def test_handover_tool_is_not_offered_during_a_normal_call():
    """Otherwise Kavya could promise a callback instead of transferring."""
    from tools import TOOL_DEFINITIONS

    assert "notify_human_handover" not in {t["name"] for t in TOOL_DEFINITIONS}


@pytest.mark.parametrize("fmt,extract", [
    ("claude", lambda t: [x["name"] for x in t]),
    ("openai", lambda t: [x["function"]["name"] for x in t]),
    ("gemini", lambda t: [f["name"] for x in t for f in x["function_declarations"]]),
])
def test_get_handover_tools_returns_only_the_notify_tool(fmt, extract):
    from tools import get_handover_tools

    assert extract(get_handover_tools(fmt)) == ["notify_human_handover"]
