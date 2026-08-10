"""Session-level DTMF collection: TTS ordering, success, and fallback.

The keypad instruction must be spoken (and delivered) BEFORE collection begins,
or the guest hears dead air. Collection then routes DTMF digits from the gateway
into the collector and returns the number for the model to read back.
"""

from __future__ import annotations

import asyncio
import json

import pytest

import server


def make_smartpbx_session(monkeypatch):
    session = server.MediaStreamSession(websocket=None, lang="en", media_transport=None)
    session._event_loop = asyncio.get_running_loop()
    # Mark this as a SmartPBX call so DTMF is enabled.
    session._smartpbx_transfer_context = object()
    spoken: list[str] = []

    async def fake_speak(text, generation=-1):
        spoken.append(text)

    monkeypatch.setattr(session, "_speak", fake_speak)
    return session, spoken


@pytest.mark.asyncio
async def test_instruction_is_spoken_before_collection_begins(monkeypatch):
    session, spoken = make_smartpbx_session(monkeypatch)

    task = asyncio.create_task(
        session._collect_number_via_keypad({"label": "WhatsApp number for your confirmation"})
    )
    # Let the coroutine run until it parks on the collector future.
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert spoken, "the keypad instruction must be spoken"
    assert "hash" in spoken[0].lower(), "the instruction must tell the guest to press hash"
    assert "whatsapp number" in spoken[0].lower()
    assert session._dtmf_collector is not None, "collection must be active after the instruction"

    # Digits only arrive AFTER the instruction was spoken — proving the order.
    for digit in "0762560705":
        assert await session.feed_dtmf(digit) is True
    await session.feed_dtmf("#")

    result = json.loads(await asyncio.wait_for(task, timeout=1))
    assert result["status"] == "collected"
    assert result["digits"] == "0762560705"
    assert result["readback"] == "0 7 6 2 5 6 0 7 0 5"
    assert session._dtmf_collector is None, "the collector must be cleared after collection"


@pytest.mark.asyncio
async def test_feed_dtmf_is_a_noop_when_not_collecting(monkeypatch):
    session, _spoken = make_smartpbx_session(monkeypatch)
    assert session._dtmf_collector is None
    assert await session.feed_dtmf("5") is False


@pytest.mark.asyncio
async def test_non_smartpbx_session_reports_keypad_unavailable(monkeypatch):
    session = server.MediaStreamSession(websocket=None, lang="en", media_transport=None)
    session._event_loop = asyncio.get_running_loop()
    # No _smartpbx_transfer_context → not a SmartPBX call → keypad not wired.
    spoken: list[str] = []
    monkeypatch.setattr(session, "_speak", lambda *a, **k: spoken.append(a))

    result = json.loads(await session._collect_number_via_keypad({"label": "phone"}))
    assert result["status"] == "unavailable"
    assert spoken == [], "an unavailable keypad must not speak an instruction"


@pytest.mark.asyncio
async def test_overall_timeout_returns_a_failure_for_spoken_fallback(monkeypatch):
    session, spoken = make_smartpbx_session(monkeypatch)
    monkeypatch.setattr(server, "DTMF_OVERALL_TIMEOUT_SECONDS", 0.02)

    result = json.loads(
        await asyncio.wait_for(
            session._collect_number_via_keypad({"label": "callback number"}), timeout=1
        )
    )
    assert result["status"] == "no_input"
    assert result["reason"] == "overall_timeout"
    assert session._dtmf_collector is None


def test_dtmf_knob_defaults_and_clamping():
    # Floats via the endpointing parser, the digit count via the int parser.
    assert server._parse_endpointing_seconds({}, "X", 6.0, 1.0, 30.0) == 6.0
    assert server._parse_endpointing_seconds({"X": "999"}, "X", 6.0, 1.0, 30.0) == 30.0
    assert server._parse_endpointing_seconds({"X": "0"}, "X", 30.0, 5.0, 120.0) == 5.0
    assert server._parse_clamped_int({}, "X", 15, 1, 40) == 15
    assert server._parse_clamped_int({"X": "999"}, "X", 15, 1, 40) == 40
    assert server._parse_clamped_int({"X": "0"}, "X", 15, 1, 40) == 1
    assert server._parse_clamped_int({"X": "bad"}, "X", 15, 1, 40) == 15
    # The module constants are within their documented bounds.
    assert 1.0 <= server.DTMF_INTERDIGIT_TIMEOUT_SECONDS <= 30.0
    assert 5.0 <= server.DTMF_OVERALL_TIMEOUT_SECONDS <= 120.0
    assert 1 <= server.DTMF_MAX_DIGITS <= 40
