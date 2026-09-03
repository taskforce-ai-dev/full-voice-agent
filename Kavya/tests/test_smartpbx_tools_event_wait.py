"""Audit #5: `tools._await_turn_delivery` must block on progress, not spin.

Before this fix it ran `await asyncio.sleep(0)` once per event loop tick for
the full paced delivery of the transfer announcement (~3-5s), contending for
the shared loop with every other concurrent call's paced senders and STT
feeds. `_send_tts_done` (and `_handle_bargein`, when it makes the wait moot)
now set `pipeline._smartpbx_delivery_event`, and the waiter blocks on it.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

import tools


class _DeliveryPipeline:
    def __init__(self) -> None:
        self._assistant_turn_generation = 0
        self._delivered_sentences: list[str] = []
        self._smartpbx_delivery_event = asyncio.Event()

    def deliver(self, sentence: str) -> None:
        self._delivered_sentences.append(sentence)
        self._smartpbx_delivery_event.set()

    def bump_generation(self) -> None:
        self._assistant_turn_generation += 1
        self._smartpbx_delivery_event.set()


@pytest.mark.asyncio
async def test_await_turn_delivery_wakes_on_progress_events():
    pipeline = _DeliveryPipeline()
    task = asyncio.create_task(
        tools._await_turn_delivery(pipeline, generation=0, expected=2, timeout=5)
    )
    # Give the waiter a few event loop ticks to reach its first real await --
    # it must still be pending: no delivery has happened yet.
    for _ in range(5):
        await asyncio.sleep(0)
    assert not task.done()

    pipeline.deliver("first sentence")
    for _ in range(5):
        await asyncio.sleep(0)
    assert not task.done(), "one of two expected sentences must not satisfy the wait"

    pipeline.deliver("second sentence")
    await asyncio.wait_for(task, timeout=1)
    assert task.done()


@pytest.mark.asyncio
async def test_await_turn_delivery_exits_promptly_when_generation_moves_on():
    """A barge-in mid-wait makes the delivery moot; the waiter must notice
    via the same event rather than running out its full timeout."""
    pipeline = _DeliveryPipeline()
    task = asyncio.create_task(
        tools._await_turn_delivery(pipeline, generation=0, expected=2, timeout=5)
    )
    for _ in range(5):
        await asyncio.sleep(0)
    assert not task.done()

    pipeline.bump_generation()
    await asyncio.wait_for(task, timeout=1)
    assert task.done()


@pytest.mark.asyncio
async def test_await_turn_delivery_times_out_when_nothing_ever_progresses(caplog):
    pipeline = _DeliveryPipeline()
    with caplog.at_level(logging.WARNING):
        await asyncio.wait_for(
            tools._await_turn_delivery(pipeline, generation=0, expected=1, timeout=0.05),
            timeout=1,
        )
    assert "transfer_delivery_timeout" in caplog.text


@pytest.mark.asyncio
async def test_await_turn_delivery_falls_back_without_the_event_attribute():
    """A pipeline stand-in without `_smartpbx_delivery_event` (an older or
    lighter test double) must still work, just via the previous busy-spin,
    rather than raising."""

    class _NoEventPipeline:
        def __init__(self) -> None:
            self._assistant_turn_generation = 0
            self._delivered_sentences: list[str] = []

    pipeline = _NoEventPipeline()

    async def _deliver_soon():
        await asyncio.sleep(0.01)
        pipeline._delivered_sentences.append("done")

    deliver_task = asyncio.create_task(_deliver_soon())
    try:
        await asyncio.wait_for(
            tools._await_turn_delivery(pipeline, generation=0, expected=1, timeout=1),
            timeout=1,
        )
        assert pipeline._delivered_sentences == ["done"]
    finally:
        await asyncio.gather(deliver_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_await_turn_delivery_returns_immediately_for_zero_expected():
    pipeline = _DeliveryPipeline()
    await asyncio.wait_for(
        tools._await_turn_delivery(pipeline, generation=0, expected=0, timeout=5),
        timeout=0.2,
    )
