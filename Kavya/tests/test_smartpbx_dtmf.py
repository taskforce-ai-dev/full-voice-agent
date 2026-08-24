"""SmartPBX keypad entry end to end: gateway routing, isolation, teardown.

Dialog delivers keypad presses as `dtmf` events on the same socket as the media,
so the digit a guest presses while Kavya is still speaking the "key it in" prompt
arrives before the prompt finishes. These tests pin the two halves of that path:
the gateway hands every validated digit to the session that owns the call (and
only that one), and the session's collector is installed early enough to keep the
digit — and is always torn down.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging

import pytest

import server
from smartpbx_gateway import SmartPBXGateway, SmartPBXSessionRegistry
from tests.test_smartpbx_gateway import (
    START,
    FakeWebSocket,
    _DtmfFactory,
    fixed_diagnostics,
    settings,
)


def dtmf_event(digit, *, call_id="call-1", other_leg="other-1"):
    return {
        "event": "dtmf",
        "dtmf": {"callId": call_id, "otherLegCallId": other_leg, "digit": digit},
    }


async def run_call(messages, *, registry=None, factory=None):
    configuration = settings()
    registry = registry or SmartPBXSessionRegistry(configuration.max_calls)
    socket = FakeWebSocket(messages, token="test-token", header="X-Kavya-SmartPBX-Token")
    factory = factory or _DtmfFactory()
    await SmartPBXGateway(configuration, registry).handle(socket, factory)
    return registry, socket, factory


# --- gateway routing -------------------------------------------------------

@pytest.mark.asyncio
async def test_a_full_keypad_entry_reaches_the_owning_session_in_order():
    digits = [dtmf_event(digit) for digit in "0762560705"] + [dtmf_event("#")]
    _registry, socket, factory = await run_call([START, *digits, {"event": "stop"}])

    assert socket.close_calls == [(1000, "call ended")]
    assert factory.sessions[0].dtmf_digits == list("0762560705") + ["#"]


@pytest.mark.asyncio
async def test_a_context_mismatched_digit_is_still_delivered_and_observed(caplog):
    """A per-leg mismatch is telemetry, never a dropped keypad digit."""
    mismatched = dtmf_event("5", call_id="wrong-leg")

    with caplog.at_level(logging.INFO):
        _registry, socket, factory = await run_call([START, mismatched, {"event": "stop"}])

    assert socket.close_calls == [(1000, "call ended")]
    assert factory.sessions[0].dtmf_digits == ["5"]
    assert ("context_validation", "observed", "context_mismatch") in [
        (row["stage"], row["outcome"], row["failure_class"])
        for row in fixed_diagnostics(caplog)
    ]


@pytest.mark.asyncio
async def test_concurrent_calls_never_receive_each_others_digits():
    configuration = settings()
    registry = SmartPBXSessionRegistry(configuration.max_calls)

    second_start = copy.deepcopy(START)
    second_start["start"]["callId"] = "call-2"
    second_start["start"]["otherLegCallId"] = "other-2"

    first_factory = _DtmfFactory()
    second_factory = _DtmfFactory()
    first_socket = FakeWebSocket(
        [START, *[dtmf_event(d) for d in "111"], {"event": "stop"}],
        token="test-token", header="X-Kavya-SmartPBX-Token",
    )
    second_socket = FakeWebSocket(
        [second_start, *[dtmf_event(d, call_id="call-2", other_leg="other-2") for d in "222"],
         {"event": "stop"}],
        token="test-token", header="X-Kavya-SmartPBX-Token",
    )

    await asyncio.gather(
        SmartPBXGateway(configuration, registry).handle(first_socket, first_factory),
        SmartPBXGateway(configuration, registry).handle(second_socket, second_factory),
    )

    assert first_factory.sessions[0].dtmf_digits == ["1", "1", "1"]
    assert second_factory.sessions[0].dtmf_digits == ["2", "2", "2"]
    assert registry.snapshot()["active_sessions"] == 0


# --- the session collector -------------------------------------------------

def make_session(monkeypatch, on_speak=None):
    session = server.MediaStreamSession(websocket=None, lang="en", media_transport=None)
    session._event_loop = asyncio.get_running_loop()
    session._smartpbx_transfer_context = object()
    spoken: list[str] = []

    async def fake_speak(text, generation=-1):
        spoken.append(text)
        if on_speak is not None:
            await on_speak(session)

    monkeypatch.setattr(session, "_speak", fake_speak)
    return session, spoken


@pytest.mark.asyncio
async def test_early_digits_do_not_interrupt_the_keypad_prompt(monkeypatch):
    """An early press buffers; it must not cut the instruction short."""
    cleared: list[bool] = []

    async def press_early(session):
        for digit in "07":
            assert await session.feed_dtmf(digit) is True

    session, spoken = make_session(monkeypatch, press_early)

    async def record_clear(*_args, **_kwargs):
        cleared.append(True)

    monkeypatch.setattr(session, "_clear_media_audio", record_clear)

    task = asyncio.create_task(session._collect_number_via_keypad({"label": "phone"}))
    for _ in range(4):
        await asyncio.sleep(0)

    for digit in "62560705":
        await session.feed_dtmf(digit)
    await session.feed_dtmf("#")

    result = json.loads(await asyncio.wait_for(task, timeout=1))
    assert result["digits"] == "0762560705", "early and late presses form one number"
    assert len(spoken) == 1, "the instruction is spoken exactly once"
    assert cleared == [], "early DTMF must not interrupt the prompt"


@pytest.mark.asyncio
async def test_session_teardown_resolves_a_collection_started_during_the_prompt(monkeypatch):
    """The SmartPBX session's own teardown path unwinds an in-flight collection."""
    from smartpbx_protocol import CallContext, MediaFormat
    from smartpbx_session import KavyaSmartPBXSession

    prompt_started = asyncio.Event()
    release_prompt = asyncio.Event()

    async def slow_prompt(_session):
        prompt_started.set()
        await release_prompt.wait()

    session, _spoken = make_session(monkeypatch, slow_prompt)
    session._cancel_reprompt = lambda: None
    session._write_audio_dump = lambda: None

    context = CallContext(
        "media", "safe", "0771234567", "0770000000", "account",
        MediaFormat("g711_ulaw", 8000),
    )

    async def post(**_kwargs):
        return None

    smartpbx = KavyaSmartPBXSession(
        context, object(), pipeline=session, post_call_processor=post,
        welcome_text="", llm_provider="openai", model="m",
    )

    task = asyncio.create_task(session._collect_number_via_keypad({"label": "phone"}))
    await asyncio.wait_for(prompt_started.wait(), timeout=1)
    await session.feed_dtmf("7")

    await smartpbx.finish(False)
    release_prompt.set()

    result = json.loads(await asyncio.wait_for(task, timeout=1))
    assert result["status"] == "cancelled"
    assert session._dtmf_collector is None
