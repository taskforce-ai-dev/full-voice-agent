"""Session-level DTMF collection: prompt ordering, success, and fallback.

The keypad instruction is always spoken and awaited in full, so the guest never
hears dead air — but the collector is installed BEFORE it, because guests key in
over the prompt and those digits used to be dropped. The entry window itself is
armed only once the prompt has been delivered, and no collector, timer or
awaiting task may survive success, timeout, cancellation, a failed prompt, a
transfer or a hangup.
"""

from __future__ import annotations

import asyncio
import json

import pytest

import server
from smartpbx_dtmf import DtmfCollector


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

    # Digits keyed in after the instruction still collect normally.
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


@pytest.mark.asyncio
async def test_hangup_during_collection_resolves_the_future_without_leaking(monkeypatch):
    session, _spoken = make_smartpbx_session(monkeypatch)
    task = asyncio.create_task(session._collect_number_via_keypad({"label": "phone"}))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert session._dtmf_collector is not None

    # Teardown mid-collection (e.g. the guest hung up).
    session._cancel_dtmf_collection()

    result = json.loads(await asyncio.wait_for(task, timeout=1))
    assert result["status"] == "cancelled"
    assert session._dtmf_collector is None
    assert task.done(), "the awaiting collection task must not leak"


@pytest.mark.asyncio
async def test_cancel_dtmf_collection_is_safe_when_not_collecting(monkeypatch):
    session, _spoken = make_smartpbx_session(monkeypatch)
    assert session._dtmf_collector is None
    session._cancel_dtmf_collection()  # must not raise
    assert session._dtmf_collector is None


@pytest.mark.asyncio
async def test_smartpbx_finish_cancels_active_dtmf_collection():
    from smartpbx_session import KavyaSmartPBXSession
    from smartpbx_protocol import CallContext, MediaFormat

    cancelled = []

    class Pipeline:
        transfer_pending = False
        _endpointing_handle = None
        _stt = None
        full_transcript = []

        def _cancel_reprompt(self):
            pass

        def _write_audio_dump(self):
            pass

        def _cancel_dtmf_collection(self):
            cancelled.append(True)

    context = CallContext("media", "safe", "0771234567", "0770000000", "account", MediaFormat("g711_ulaw", 8000))

    async def post(**_):
        return None

    session = KavyaSmartPBXSession(
        context, object(), pipeline=Pipeline(), post_call_processor=post,
        welcome_text="", llm_provider="openai", model="m",
    )
    await session.finish(False)
    assert cancelled == [True], "finish must cancel any active keypad collection"


# --- early keypad input during the spoken instruction ----------------------

class RecordingLoop:
    """Wraps the running loop so every DTMF timer can be inspected for leaks."""

    def __init__(self, loop):
        self._loop = loop
        self.timers: list[dict] = []

    def call_later(self, delay, callback):
        record: dict = {"delay": delay, "fired": False}

        def fire():
            record["fired"] = True
            callback()

        record["handle"] = self._loop.call_later(delay, fire)
        self.timers.append(record)
        return record["handle"]

    def create_future(self):
        return self._loop.create_future()

    def delays(self) -> list[float]:
        return [timer["delay"] for timer in self.timers]

    def live(self) -> list[float]:
        """Delays of timers that neither fired nor were cancelled — i.e. leaks."""
        return [
            timer["delay"]
            for timer in self.timers
            if not timer["fired"] and not timer["handle"].cancelled()
        ]


@pytest.mark.asyncio
async def test_collector_start_is_idempotent_after_buffering_early_digits():
    """A duplicate post-prompt start cannot orphan its first timer handles."""
    loop = RecordingLoop(asyncio.get_running_loop())
    collector = DtmfCollector(
        loop=loop,
        interdigit_timeout=server.DTMF_INTERDIGIT_TIMEOUT_SECONDS,
        overall_timeout=server.DTMF_OVERALL_TIMEOUT_SECONDS,
    )

    collector.feed("0")
    collector.start()
    collector.start()

    assert loop.delays() == [
        server.DTMF_INTERDIGIT_TIMEOUT_SECONDS,
        server.DTMF_OVERALL_TIMEOUT_SECONDS,
    ]
    collector.cancel()
    assert loop.live() == []


def make_recording_session(monkeypatch, on_speak=None):
    """A SmartPBX session whose keypad prompt runs `on_speak` while it speaks."""
    session = server.MediaStreamSession(websocket=None, lang="en", media_transport=None)
    loop = RecordingLoop(asyncio.get_running_loop())
    session._event_loop = loop
    session._smartpbx_transfer_context = object()
    spoken: list[str] = []

    async def fake_speak(text, generation=-1):
        spoken.append(text)
        if on_speak is not None:
            await on_speak(session)

    monkeypatch.setattr(session, "_speak", fake_speak)
    return session, loop, spoken


@pytest.mark.asyncio
async def test_digits_pressed_during_the_prompt_are_retained(monkeypatch):
    """The guest keys in while the instruction is still playing; nothing is lost."""
    early: list[bool] = []

    async def press_during_prompt(session):
        for digit in "0762560705":
            early.append(await session.feed_dtmf(digit))

    session, loop, spoken = make_recording_session(monkeypatch, press_during_prompt)

    task = asyncio.create_task(
        session._collect_number_via_keypad({"label": "WhatsApp number for your confirmation"})
    )
    for _ in range(4):
        await asyncio.sleep(0)
    assert spoken and "hash" in spoken[0].lower()
    assert early == [True] * 10, "a collector must be installed before the prompt is spoken"

    await session.feed_dtmf("#")
    result = json.loads(await asyncio.wait_for(task, timeout=1))

    assert result["status"] == "collected"
    assert result["digits"] == "0762560705"
    assert result["readback"] == "0 7 6 2 5 6 0 7 0 5"
    assert session._dtmf_collector is None
    assert loop.live() == [], "no DTMF timer may outlive the collection"


@pytest.mark.asyncio
async def test_early_hash_completes_the_entry_without_arming_a_stale_timer(monkeypatch):
    """A full entry finished during the prompt must not arm the overall timeout."""

    async def key_it_all_in(session):
        for digit in "0762560705":
            await session.feed_dtmf(digit)
        await session.feed_dtmf("#")

    session, loop, _spoken = make_recording_session(monkeypatch, key_it_all_in)

    result = json.loads(
        await asyncio.wait_for(
            session._collect_number_via_keypad({"label": "callback number"}), timeout=1
        )
    )

    assert result["status"] == "collected"
    assert result["digits"] == "0762560705"
    assert server.DTMF_OVERALL_TIMEOUT_SECONDS not in loop.delays(), (
        "the entry was already complete; arming the overall timeout would be stale"
    )
    assert session._dtmf_collector is None
    assert loop.live() == []


@pytest.mark.asyncio
async def test_the_overall_timeout_is_armed_only_after_the_prompt(monkeypatch):
    """The guest gets the full entry window, measured from the end of the prompt."""
    during_prompt: list[list[float]] = []

    async def observe(session):
        during_prompt.append(list(session._event_loop.delays()))

    session, loop, _spoken = make_recording_session(monkeypatch, observe)

    task = asyncio.create_task(session._collect_number_via_keypad({"label": "phone"}))
    for _ in range(4):
        await asyncio.sleep(0)

    assert during_prompt == [[]] or server.DTMF_OVERALL_TIMEOUT_SECONDS not in during_prompt[0], (
        "the overall timeout must not run while the instruction is still playing"
    )
    assert loop.delays().count(server.DTMF_OVERALL_TIMEOUT_SECONDS) == 1, (
        "the full entry window is armed once, after the prompt"
    )

    for digit in "0762560705":
        await session.feed_dtmf(digit)
    await session.feed_dtmf("#")
    result = json.loads(await asyncio.wait_for(task, timeout=1))
    assert result["status"] == "collected"
    assert loop.live() == []


@pytest.mark.asyncio
async def test_a_failed_prompt_leaves_no_collector_or_timer(monkeypatch):
    session, loop, _spoken = make_recording_session(monkeypatch)

    async def failing_speak(_text, generation=-1):
        await session.feed_dtmf("7")  # an early press that must be dropped with the entry
        raise RuntimeError("tts unavailable")

    monkeypatch.setattr(session, "_speak", failing_speak)

    with pytest.raises(RuntimeError):
        await session._collect_number_via_keypad({"label": "phone"})

    assert session._dtmf_collector is None
    assert loop.live() == []
    assert await session.feed_dtmf("5") is False


@pytest.mark.asyncio
async def test_cancellation_during_the_prompt_unwinds_the_collection(monkeypatch):
    """Teardown mid-prompt must resolve the collection, not strand its awaiter."""
    prompt_started = asyncio.Event()
    release_prompt = asyncio.Event()

    async def slow_prompt(_session):
        prompt_started.set()
        await release_prompt.wait()

    session, loop, _spoken = make_recording_session(monkeypatch, slow_prompt)

    task = asyncio.create_task(session._collect_number_via_keypad({"label": "phone"}))
    await asyncio.wait_for(prompt_started.wait(), timeout=1)
    await session.feed_dtmf("4")
    session._cancel_dtmf_collection()
    release_prompt.set()

    result = json.loads(await asyncio.wait_for(task, timeout=1))
    assert result["status"] == "cancelled"
    assert session._dtmf_collector is None
    assert loop.live() == []
    assert task.done(), "the awaiting collection task must not leak"


@pytest.mark.asyncio
async def test_external_cancellation_after_the_prompt_cancels_collector_timers(monkeypatch):
    """Cancelling the tool task after prompt delivery must not orphan timers."""
    session, loop, _spoken = make_recording_session(monkeypatch)

    task = asyncio.create_task(session._collect_number_via_keypad({"label": "phone"}))
    for _ in range(4):
        await asyncio.sleep(0)

    assert session._dtmf_collector is not None
    assert loop.live() == [server.DTMF_OVERALL_TIMEOUT_SECONDS]

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert session._dtmf_collector is None
    assert loop.live() == []


@pytest.mark.asyncio
async def test_transfer_resolves_an_in_flight_keypad_collection(monkeypatch):
    session, loop, _spoken = make_recording_session(monkeypatch)

    async def noop(*_args, **_kwargs):
        return None

    session._clear_media_audio = noop

    task = asyncio.create_task(session._collect_number_via_keypad({"label": "phone"}))
    for _ in range(4):
        await asyncio.sleep(0)
    assert session._dtmf_collector is not None

    await session.enter_transfer_pending()

    result = json.loads(await asyncio.wait_for(task, timeout=1))
    assert result["status"] == "cancelled"
    assert session._dtmf_collector is None
    assert loop.live() == []


@pytest.mark.asyncio
async def test_a_successful_collection_leaves_no_timer_behind(monkeypatch):
    session, loop, _spoken = make_recording_session(monkeypatch)

    task = asyncio.create_task(session._collect_number_via_keypad({"label": "phone"}))
    for _ in range(4):
        await asyncio.sleep(0)
    for digit in "0762560705":
        await session.feed_dtmf(digit)
    await session.feed_dtmf("#")

    result = json.loads(await asyncio.wait_for(task, timeout=1))
    assert result["status"] == "collected"
    assert session._dtmf_collector is None
    assert loop.live() == []


@pytest.mark.asyncio
async def test_an_overall_timeout_leaves_no_collector_or_timer(monkeypatch):
    monkeypatch.setattr(server, "DTMF_OVERALL_TIMEOUT_SECONDS", 0.02)
    session, loop, _spoken = make_recording_session(monkeypatch)

    result = json.loads(
        await asyncio.wait_for(
            session._collect_number_via_keypad({"label": "phone"}), timeout=1
        )
    )
    assert result["status"] == "no_input"
    assert result["reason"] == "overall_timeout"
    assert session._dtmf_collector is None
    assert loop.live() == []


@pytest.mark.asyncio
async def test_a_pause_during_the_prompt_does_not_finalize_a_partial_entry(monkeypatch):
    """The inter-digit pause is entry timing, so it too starts after the prompt.

    A guest who taps the first digits and then listens to the rest of the
    instruction must not have those digits finalized as the whole number.
    """
    during_prompt: list[list[float]] = []

    async def press_then_listen(session):
        for digit in "076":
            await session.feed_dtmf(digit)
        during_prompt.append(list(session._event_loop.delays()))

    session, loop, _spoken = make_recording_session(monkeypatch, press_then_listen)

    task = asyncio.create_task(session._collect_number_via_keypad({"label": "phone"}))
    for _ in range(4):
        await asyncio.sleep(0)

    assert server.DTMF_INTERDIGIT_TIMEOUT_SECONDS not in during_prompt[0], (
        "an inter-digit pause must not run against a prompt the guest is still hearing"
    )
    assert not task.done(), "a partial early entry must not finalize the collection"
    assert server.DTMF_INTERDIGIT_TIMEOUT_SECONDS in loop.delays(), (
        "once the prompt is delivered the buffered entry gets its pause window"
    )

    for digit in "2560705":
        await session.feed_dtmf(digit)
    await session.feed_dtmf("#")

    result = json.loads(await asyncio.wait_for(task, timeout=1))
    assert result["digits"] == "0762560705"
    assert loop.live() == []
