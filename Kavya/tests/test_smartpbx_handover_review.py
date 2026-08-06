"""Regression tests for reviewed Dialog handover state transitions."""

from __future__ import annotations

import asyncio
import json

import pytest

from smartpbx_handover import SmartPBXHandoverCoordinator, bound_transcript_tail
from smartpbx_protocol import CallContext, MediaFormat
from smartpbx_session import KavyaSmartPBXSession


class _Pipeline:
    def __init__(self):
        self.transfer_pending = False

    async def enter_transfer_pending(self):
        self.transfer_pending = True


class _Control:
    def __init__(self):
        self.calls = 0

    async def transfer_call(self, _target):
        self.calls += 1
        return type("Result", (), {"transferred": True})()


def _context():
    return CallContext(
        call_id="media", other_leg_call_id="safe-call", caller_id_number="0771234567",
        callee_id_number="0770000000", account_id="account", media_format=MediaFormat("g711_ulaw", 8000),
    )


@pytest.mark.asyncio
async def test_real_session_exposes_inner_transfer_pending_state():
    pipeline = _Pipeline()
    session = KavyaSmartPBXSession(_context(), object(), pipeline=pipeline)

    assert session.transfer_pending is False
    pipeline.transfer_pending = True
    assert session.transfer_pending is True


@pytest.mark.asyncio
async def test_cancelled_pending_entry_never_redispatches_acknowledged_mcp_transfer():
    entered = asyncio.Event()
    release = asyncio.Event()

    class BlockingPipeline(_Pipeline):
        async def enter_transfer_pending(self):
            entered.set()
            await release.wait()
            self.transfer_pending = True

    control = _Control()
    coordinator = SmartPBXHandoverCoordinator(
        call_control=control, pipeline=BlockingPipeline(), call_sid="safe", caller_phone="0771234567",
        transcript=lambda: [], dashboard_sender=None, notification_sender=None, human_agent_whatsapp="0770000000",
    )
    task = asyncio.create_task(coordinator.attempt("help"))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    release.set()

    result = json.loads(await coordinator.attempt("again"))

    assert control.calls == 1
    assert result == {"status": "transferred", "confirmation": "provider_acknowledged"}


def test_transcript_tail_consumes_only_bounded_recent_items_and_caps_each_item_before_normalizing():
    class HugeHistory:
        def __len__(self):
            return 10000

        def __getitem__(self, index):
            if isinstance(index, slice):
                assert index.start == 9992
                return [{"role": "user", "text": f"{item}-" + ("x" * 100000)} for item in range(index.start, index.stop)]
            raise AssertionError("the full history must not be iterated")

    tail = bound_transcript_tail(HugeHistory())

    assert len(tail) <= 3000
    assert "9999-" in tail
