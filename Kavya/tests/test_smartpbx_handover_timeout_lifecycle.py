from __future__ import annotations
import asyncio, json
import pytest
from smartpbx_handover import SmartPBXHandoverCoordinator

class Control:
    def __init__(self, ok=False): self.calls=0; self.ok=ok
    async def transfer_call(self,_): self.calls+=1; return type("R",(),{"transferred":self.ok})()
class Pipeline:
    def __init__(self): self.transfer_pending=False
    async def enter_transfer_pending(self): self.transfer_pending=True

@pytest.mark.asyncio
async def test_notification_timeout_maps_to_failed_and_retries_once_without_second_mcp():
    sends=[]
    async def notify(**_): sends.append(1); raise TimeoutError("BODY-SECRET")
    control=Control(); c=SmartPBXHandoverCoordinator(call_control=control,pipeline=Pipeline(),call_sid="CALL-SECRET",caller_phone="0771234567",transcript=lambda:[],dashboard_sender=None,notification_sender=notify,human_agent_whatsapp="0770000000")
    assert json.loads(await c.attempt("reason")) == {"status":"unavailable","notification":"failed"}
    await c.finalize_notification_retry(); await c.finalize_notification_retry()
    assert control.calls == 1 and len(sends) == 2

@pytest.mark.asyncio
async def test_acknowledgement_does_not_complete_terminal_until_real_session_finish():
    from smartpbx_session import KavyaSmartPBXSession
    from smartpbx_protocol import CallContext, MediaFormat
    class Transport:
        async def clear_audio(self): pass
    pipeline=Pipeline(); pipeline._cancel_reprompt=lambda:None; pipeline._endpointing_handle=None; pipeline._stt=None; pipeline._write_audio_dump=lambda:None; pipeline.full_transcript=[]
    context=CallContext("media","safe","0771234567","0770000000","account",MediaFormat("g711_ulaw",8000))
    async def post(**_): raise AssertionError("empty transcript must not post")
    session=KavyaSmartPBXSession(context,Transport(),pipeline=pipeline,post_call_processor=post,welcome_text="",llm_provider="openai",model="m")
    c=SmartPBXHandoverCoordinator(call_control=Control(True),pipeline=pipeline,call_sid="safe",caller_phone="0771234567",transcript=lambda:[],dashboard_sender=None,notification_sender=None,human_agent_whatsapp="0770000000")
    assert json.loads(await c.attempt("help"))["status"] == "transferred"
    assert not session.terminal_future.done()
    await session.finish(True)
    assert session.terminal_future.done()


@pytest.mark.asyncio
async def test_finalize_notification_retry_is_bounded_against_the_cleanup_budget():
    """finalize runs inside _finish_once, which the gateway gives 5s in total."""
    calls = []

    async def notify(**_):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("fast fail")
        await asyncio.sleep(30)
        return {"ok": True}

    coordinator = SmartPBXHandoverCoordinator(
        call_control=Control(), pipeline=Pipeline(), call_sid="safe",
        caller_phone="0771234567", transcript=lambda: [], dashboard_sender=None,
        notification_sender=notify, human_agent_whatsapp="0770000000",
    )
    assert json.loads(await coordinator.attempt("reason"))["notification"] == "failed"

    loop = asyncio.get_running_loop()
    started = loop.time()
    await coordinator.finalize_notification_retry()
    elapsed = loop.time() - started

    assert elapsed < 2.5, (
        f"the retry took {elapsed:.1f}s of the cleanup's 5s budget"
    )


@pytest.mark.asyncio
async def test_finalize_does_not_block_on_an_in_flight_transfer_attempt():
    """A guest hangup during a slow MCP attempt must not hold the pipeline open."""

    class SlowControl:
        async def transfer_call(self, _target):
            await asyncio.sleep(30)
            return type("R", (), {"transferred": False})()

    coordinator = SmartPBXHandoverCoordinator(
        call_control=SlowControl(), pipeline=Pipeline(), call_sid="safe",
        caller_phone="0771234567", transcript=lambda: [], dashboard_sender=None,
        notification_sender=None, human_agent_whatsapp="0770000000",
    )
    attempt = asyncio.create_task(coordinator.attempt("reason"))
    await asyncio.sleep(0.05)

    loop = asyncio.get_running_loop()
    started = loop.time()
    await coordinator.finalize_notification_retry()
    elapsed = loop.time() - started
    attempt.cancel()
    await asyncio.gather(attempt, return_exceptions=True)

    assert elapsed < 2.5, (
        f"finalize waited {elapsed:.1f}s for the attempt lock; the shielded "
        "_finish_once keeps the whole pipeline alive for that long"
    )
