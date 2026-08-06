"""Cancellation must not reopen a Dialog transfer dispatch boundary."""
from __future__ import annotations
import asyncio, json
import pytest
from smartpbx_handover import SmartPBXHandoverCoordinator

class Control:
    def __init__(self, gate=None, success=False): self.calls=0; self.gate=gate; self.success=success
    async def transfer_call(self, _):
        self.calls += 1
        if self.gate: await self.gate.wait()
        return type("R", (), {"transferred": self.success})()
class Pipeline:
    async def enter_transfer_pending(self): pass

@pytest.mark.asyncio
async def test_cancelled_mcp_attempt_is_not_dispatched_again():
    gate=asyncio.Event(); control=Control(gate)
    c=SmartPBXHandoverCoordinator(call_control=control,pipeline=Pipeline(),call_sid="safe",caller_phone="0771234567",transcript=lambda:[],dashboard_sender=None,notification_sender=None,human_agent_whatsapp="0770000000")
    task=asyncio.create_task(c.attempt("help")); await asyncio.sleep(0); task.cancel()
    with pytest.raises(asyncio.CancelledError): await task
    gate.set()
    assert json.loads(await c.attempt("again"))["status"] == "unavailable"
    assert control.calls == 1

@pytest.mark.asyncio
async def test_cancelled_notification_retries_without_second_mcp_dispatch():
    entered=asyncio.Event(); gate=asyncio.Event(); control=Control()
    async def notify(**_): entered.set(); await gate.wait(); return {"ok":False}
    c=SmartPBXHandoverCoordinator(call_control=control,pipeline=Pipeline(),call_sid="safe",caller_phone="0771234567",transcript=lambda:[],dashboard_sender=None,notification_sender=notify,human_agent_whatsapp="0770000000")
    task=asyncio.create_task(c.attempt("help")); await entered.wait(); task.cancel()
    with pytest.raises(asyncio.CancelledError): await task
    assert json.loads(await c.attempt("again"))["status"] == "unavailable" and control.calls == 1
    gate.set(); await c.finalize_notification_retry(); assert control.calls == 1
