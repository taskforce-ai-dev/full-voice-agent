"""Exactly-once utterance dispatch across the shared endpointing state machine.

A production call fired two llm_round sequences ~0.5s apart for one guest turn
(Kavya talking over herself). Root cause: a stale endpointing timer dispatches,
then a re-arm (a late final/interim of the same utterance) dispatches again. The
endpointing change exposed it — the short final grace makes the two dispatches
tightly spaced and near-identical. There must be exactly one _process_utterance
per guest turn regardless of timer/callback ordering, and a barge-in must be
able to start a fresh turn.
"""

from __future__ import annotations

import asyncio

import pytest

import server


class _Scheduled:
    def __init__(self, delay, callback):
        self.delay = delay
        self.callback = callback
        self.cancelled = False

    def cancel(self):
        self.cancelled = True


class FakeLoop:
    def __init__(self):
        self.scheduled: list[_Scheduled] = []

    def call_later(self, delay, callback):
        handle = _Scheduled(delay, callback)
        self.scheduled.append(handle)
        return handle

    @property
    def last(self) -> _Scheduled:
        return self.scheduled[-1]


def make_session(monkeypatch, lang="en", hold=None):
    session = server.MediaStreamSession(websocket=None, lang=lang, media_transport=None)
    session._event_loop = FakeLoop()
    calls: list[str] = []

    async def process(text):
        calls.append(text)
        if hold is not None:
            await hold.wait()

    async def noop():
        return None

    monkeypatch.setattr(session, "_process_utterance", process)
    # No websocket/transport in this harness; barge-in's media clear is exercised
    # elsewhere and is not what these tests assert.
    monkeypatch.setattr(session, "_clear_media_audio", noop)
    return session, session._event_loop, calls


@pytest.mark.asyncio
async def test_single_utterance_dispatches_exactly_once(monkeypatch):
    session, loop, calls = make_session(monkeypatch)

    await session._accumulate_transcript("i'd like a room")
    loop.last.callback()
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert calls == ["i'd like a room"]


@pytest.mark.asyncio
async def test_late_final_after_a_flush_does_not_start_a_second_round(monkeypatch):
    # The reported bug: an interim timer fires and dispatches, then a final of the
    # same utterance arrives after the flush and arms the short grace. Only one
    # llm_round may run while the first turn is still in flight.
    hold = asyncio.Event()
    session, loop, calls = make_session(monkeypatch, hold=hold)

    await session._set_transcript_interim("hello")
    loop.last.callback()
    await asyncio.sleep(0)  # flush #1 dispatches and holds the turn in flight
    assert calls == ["hello"]

    # A late final for the same utterance arrives and re-arms.
    await session._accumulate_transcript("hello there")
    loop.last.callback()
    await asyncio.sleep(0)

    assert calls == ["hello"], "a re-arm during an in-flight turn must not dispatch again"
    hold.set()
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_two_stale_timers_firing_dispatch_once(monkeypatch):
    hold = asyncio.Event()
    session, loop, calls = make_session(monkeypatch, hold=hold)

    await session._set_transcript_interim("one")
    first = loop.last
    await session._accumulate_transcript("one two")
    second = loop.last

    # Both timers fire (e.g. the interim timer had already fired when the final
    # re-armed): whichever runs first wins; the other must be suppressed.
    first.callback()
    second.callback()
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert len(calls) == 1, f"exactly one dispatch expected, got {calls}"
    hold.set()
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_the_next_turn_dispatches_after_the_first_completes(monkeypatch):
    session, loop, calls = make_session(monkeypatch)

    await session._accumulate_transcript("first turn")
    loop.last.callback()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert calls == ["first turn"]

    await session._accumulate_transcript("second turn")
    loop.last.callback()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert calls == ["first turn", "second turn"], "the guard must release for the next turn"


@pytest.mark.asyncio
async def test_barge_in_resets_the_guard_for_a_fresh_turn(monkeypatch):
    hold = asyncio.Event()
    session, loop, calls = make_session(monkeypatch, hold=hold)

    # Turn 1 dispatches and is still in flight (agent responding).
    await session._accumulate_transcript("what rooms do you have")
    loop.last.callback()
    await asyncio.sleep(0)
    assert calls == ["what rooms do you have"]

    # Guest interrupts while the agent is speaking.
    session._is_speaking = True
    await session._handle_bargein()

    # The barge-in utterance must be able to dispatch even though turn 1 has not
    # unwound yet.
    session._is_speaking = False
    await session._accumulate_transcript("actually never mind")
    loop.last.callback()
    await asyncio.sleep(0)

    assert calls == ["what rooms do you have", "actually never mind"]
    hold.set()
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_stale_timer_after_barge_in_does_not_dispatch(monkeypatch):
    hold = asyncio.Event()
    session, loop, calls = make_session(monkeypatch, hold=hold)

    await session._accumulate_transcript("first")
    stale = loop.last
    loop.last.callback()
    await asyncio.sleep(0)
    assert calls == ["first"]

    session._is_speaking = True
    await session._handle_bargein()
    session._is_speaking = False

    # A timer armed before the barge-in fires late; it must not dispatch the old
    # utterance again.
    stale.callback()
    await asyncio.sleep(0)
    assert calls == ["first"]
    hold.set()
    await asyncio.sleep(0)
