"""Transport-agnostic DTMF keypad collector core.

Buffers digits, '#' finalizes, '*' clears the current entry, an inter-digit
pause finalizes with what is buffered, an overall timeout fails, and a max
length guards runaway input. Terminating resolves an asyncio future so the
awaiting tool can read the result.
"""

from __future__ import annotations

import asyncio

import pytest

from smartpbx_dtmf import DtmfCollector


class _Handle:
    def __init__(self, delay, callback):
        self.delay = delay
        self.callback = callback
        self.cancelled = False

    def cancel(self):
        self.cancelled = True


class FakeLoop:
    """Records call_later so timeouts are fired deterministically; real futures."""

    def __init__(self, real):
        self._real = real
        self.timers: list[_Handle] = []

    def call_later(self, delay, callback):
        handle = _Handle(delay, callback)
        self.timers.append(handle)
        return handle

    def create_future(self):
        return self._real.create_future()

    def fire(self, delay):
        """Fire the most recent live timer armed at ``delay``."""
        for handle in reversed(self.timers):
            if handle.delay == delay and not handle.cancelled:
                handle.cancelled = True
                handle.callback()
                return
        raise AssertionError(f"no live timer at delay {delay}")


def make(**kwargs):
    loop = FakeLoop(asyncio.get_running_loop())
    collector = DtmfCollector(
        loop=loop,
        interdigit_timeout=kwargs.get("interdigit_timeout", 6.0),
        overall_timeout=kwargs.get("overall_timeout", 30.0),
        max_digits=kwargs.get("max_digits", 15),
    )
    collector.start()
    return collector, loop


@pytest.mark.asyncio
async def test_hash_finalizes_the_buffered_digits():
    collector, _loop = make()
    for key in "0762560705":
        collector.feed(key)
    collector.feed("#")
    result = await asyncio.wait_for(collector.future, timeout=1)
    assert result["status"] == "collected"
    assert result["digits"] == "0762560705"
    assert result["readback"] == "0 7 6 2 5 6 0 7 0 5"
    assert result["length"] == 10


@pytest.mark.asyncio
async def test_star_clears_the_current_entry():
    collector, _loop = make()
    for key in "999":
        collector.feed(key)
    collector.feed("*")
    for key in "0771234567":
        collector.feed(key)
    collector.feed("#")
    result = await asyncio.wait_for(collector.future, timeout=1)
    assert result["digits"] == "0771234567", "the cleared digits must not survive"


@pytest.mark.asyncio
async def test_interdigit_timeout_finalizes_with_what_is_buffered():
    collector, loop = make(interdigit_timeout=6.0)
    for key in "0761":
        collector.feed(key)
    loop.fire(6.0)  # guest paused
    result = await asyncio.wait_for(collector.future, timeout=1)
    assert result["status"] == "collected"
    assert result["digits"] == "0761"


@pytest.mark.asyncio
async def test_overall_timeout_with_no_input_is_a_failure():
    collector, loop = make(overall_timeout=30.0)
    loop.fire(30.0)
    result = await asyncio.wait_for(collector.future, timeout=1)
    assert result["status"] == "no_input"
    assert result["reason"] == "overall_timeout"


@pytest.mark.asyncio
async def test_max_length_guards_a_runaway_and_finalizes():
    collector, _loop = make(max_digits=6)
    for key in "1234567890":  # more than max
        collector.feed(key)
    result = await asyncio.wait_for(collector.future, timeout=1)
    assert result["status"] == "collected"
    assert result["digits"] == "123456"
    assert result["length"] == 6


@pytest.mark.asyncio
async def test_stray_hash_with_empty_buffer_is_ignored():
    collector, loop = make()
    collector.feed("#")  # mis-press before entering anything
    assert not collector.future.done(), "a lone # must not end collection with nothing"
    for key in "0771234567":
        collector.feed(key)
    collector.feed("#")
    result = await asyncio.wait_for(collector.future, timeout=1)
    assert result["digits"] == "0771234567"


@pytest.mark.asyncio
async def test_non_numeric_keys_are_ignored():
    collector, _loop = make()
    for key in ("A", "B", "C", "D"):
        collector.feed(key)
    for key in "076":
        collector.feed(key)
    collector.feed("#")
    result = await asyncio.wait_for(collector.future, timeout=1)
    assert result["digits"] == "076"


@pytest.mark.asyncio
async def test_cancel_resolves_the_future_cleanly_for_teardown():
    collector, loop = make()
    collector.feed("0")
    collector.feed("7")
    collector.cancel()
    result = await asyncio.wait_for(collector.future, timeout=1)
    assert result["status"] == "cancelled"
    # Timers must be cancelled so nothing fires after teardown.
    assert all(h.cancelled for h in loop.timers)
    # Feeding after cancel is a no-op and must not raise or re-resolve.
    collector.feed("9")
    assert collector.future.result()["status"] == "cancelled"


@pytest.mark.asyncio
async def test_feeding_after_finalize_is_a_noop():
    collector, _loop = make()
    for key in "076":
        collector.feed(key)
    collector.feed("#")
    await asyncio.wait_for(collector.future, timeout=1)
    collector.feed("5")  # late digit after the future resolved
    assert collector.future.result()["digits"] == "076"


@pytest.mark.asyncio
async def test_overall_timer_is_cancelled_once_collected():
    collector, loop = make()
    for key in "076":
        collector.feed(key)
    collector.feed("#")
    await asyncio.wait_for(collector.future, timeout=1)
    assert all(h.cancelled for h in loop.timers), "no timer may fire after finalize"
