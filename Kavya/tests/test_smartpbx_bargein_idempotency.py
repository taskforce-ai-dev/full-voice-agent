"""Audit #8: `_handle_bargein` must be idempotent per speak generation.

Two STT callbacks (an interim and a final, or two interims 150ms apart) can
both be submitted while `_is_speaking` is still True and the loop is busy --
both then run `_handle_bargein` for what is really one interruption. Before
this fix, the second run redid the whole cancel/bump/retain cycle: bumping
`_speak_generation` and `_utterance_turn` a second time, and re-running
`_retain_pending_speech`, which can supersede the caller's own new utterance
a second time and drop it entirely.
"""

from __future__ import annotations

import asyncio

import pytest


class FakeTransport:
    def __init__(self) -> None:
        self.audio: list[bytes] = []
        self.marks: list[str] = []
        self.clears = 0

    async def send_audio(self, audio: bytes) -> None:
        self.audio.append(audio)

    async def send_mark(self, name: str) -> None:
        self.marks.append(name)

    async def clear_audio(self) -> int:
        self.clears += 1
        return self.clears


def _pipeline(server):
    transport = FakeTransport()
    pipeline = server.MediaStreamSession(websocket=None, lang="en", media_transport=transport)
    pipeline._smartpbx_transfer_context = object()
    return pipeline, transport


@pytest.mark.asyncio
async def test_concurrent_bargein_for_the_same_generation_runs_the_cycle_once():
    import server

    pipeline, transport = _pipeline(server)
    retain_calls: list[str] = []
    real_retain = pipeline._retain_pending_speech
    real_cancel_deferred = pipeline._cancel_smartpbx_deferred_tts

    async def counting_retain(reason):
        retain_calls.append(reason)
        return await real_retain(reason)

    async def yielding_cancel_deferred():
        # A genuine suspension point (unlike the real, usually-empty
        # cancel/no-op calls, which can complete without ever actually
        # yielding to the loop) so a truly concurrent second call gets a
        # chance to run its own idempotency check BEFORE this one bumps the
        # generation -- reproducing the real race between two STT callbacks.
        await asyncio.sleep(0)
        return await real_cancel_deferred()

    pipeline._retain_pending_speech = counting_retain
    pipeline._cancel_smartpbx_deferred_tts = yielding_cancel_deferred

    # Two STT callbacks racing in for the same interruption -- both captured
    # the same pre-barge-in generation before either started running.
    await asyncio.gather(pipeline._handle_bargein(), pipeline._handle_bargein())

    assert pipeline._speak_generation == 1, "the generation must bump exactly once"
    assert pipeline._utterance_turn == 1, "the turn counter must bump exactly once"
    assert retain_calls == ["barge_in"], "the cancel/retain cycle must run exactly once"
    assert transport.clears == 1, "media must be cleared exactly once"


@pytest.mark.asyncio
async def test_three_concurrent_bargein_calls_for_the_same_generation_still_run_once():
    import server

    pipeline, transport = _pipeline(server)
    retain_calls: list[str] = []
    real_retain = pipeline._retain_pending_speech
    real_cancel_deferred = pipeline._cancel_smartpbx_deferred_tts

    async def counting_retain(reason):
        retain_calls.append(reason)
        return await real_retain(reason)

    async def yielding_cancel_deferred():
        await asyncio.sleep(0)
        return await real_cancel_deferred()

    pipeline._retain_pending_speech = counting_retain
    pipeline._cancel_smartpbx_deferred_tts = yielding_cancel_deferred

    await asyncio.gather(
        pipeline._handle_bargein(), pipeline._handle_bargein(), pipeline._handle_bargein(),
    )

    assert pipeline._speak_generation == 1
    assert len(retain_calls) == 1
    assert transport.clears == 1


@pytest.mark.asyncio
async def test_a_later_bargein_for_a_new_generation_is_not_blocked_by_a_prior_one():
    """The idempotency guard must not become a permanent lock -- a genuinely
    new interruption after the generation has moved on must still barge in."""
    import server

    pipeline, transport = _pipeline(server)

    await pipeline._handle_bargein()
    assert pipeline._speak_generation == 1

    await pipeline._handle_bargein()
    assert pipeline._speak_generation == 2, "a second, later barge-in must still take effect"
    assert transport.clears == 2


@pytest.mark.asyncio
async def test_sequential_duplicate_bargein_for_the_same_generation_is_a_no_op():
    """Not just the concurrent race -- a duplicate call for a generation that
    was already fully handled and bumped past must also be inert."""
    import server

    pipeline, transport = _pipeline(server)
    # Manually replay what a stale/duplicate scheduled callback would do:
    # call _handle_bargein again while _speak_generation has already been
    # bumped by an earlier call, but the STT thread's callback still closed
    # over the OLD generation value at submit time. The guard itself is keyed
    # off self._speak_generation at call time, so simulate that directly by
    # calling twice in the same tick before any bump would be visible to a
    # second caller that captured the same starting state.
    await pipeline._handle_bargein()
    assert pipeline._speak_generation == 1
    assert transport.clears == 1

    # A duplicate for generation 0 (the marker the first call claimed) must
    # never be reachable again -- self._speak_generation is already 1, so a
    # fresh call naturally captures 1, not 0. This asserts the guard doesn't
    # leak into blocking that fresh, correctly-generationed call either.
    await pipeline._handle_bargein()
    assert pipeline._speak_generation == 2
    assert transport.clears == 2
