"""Real-adapter regression for an acknowledged Dialog handover lifecycle."""

from __future__ import annotations

import asyncio
import base64
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from smartpbx_gateway import SmartPBXGateway, SmartPBXSessionRegistry, SmartPBXSettings
from smartpbx_session import KavyaSmartPBXSession


START = {
    "event": "start",
    "start": {
        "callId": "dialog-media-leg",
        "otherLegCallId": "dialog-safe-call",
        "callerIdNumber": "+94770000000",
        "calleeIdNumber": "+94114698850",
        "accountId": "dialog-account",
        "mediaFormat": {"encoding": "g711_ulaw", "sampleRate": 8000},
    },
}


class QueueWebSocket:
    def __init__(self) -> None:
        self.headers = {"X-Kavya-SmartPBX-Token": "test-token"}
        self.messages: asyncio.Queue[str] = asyncio.Queue()
        self.messages.put_nowait(json.dumps(START))
        self.close_calls: list[tuple[int, str]] = []
        self.sent: list[str] = []

    async def accept(self) -> None:
        return None

    async def receive_text(self) -> str:
        return await self.messages.get()

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.close_calls.append((code, reason))

    async def send_text(self, message: str) -> None:
        self.sent.append(message)


class RecordingSTT:
    def __init__(self, started: asyncio.Event) -> None:
        self.started = started
        self.audio: list[bytes] = []
        self.stops = 0

    def start(self) -> None:
        self.started.set()

    def feed(self, audio: bytes) -> None:
        self.audio.append(audio)

    def stop(self) -> None:
        self.stops += 1


class CountingSession(KavyaSmartPBXSession):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.finish_calls = 0

    async def finish(
        self,
        schedule_post_call: bool = False,
        close_reason: str | None = None,
        close_code: int | None = None,
    ) -> None:
        self.finish_calls += 1
        await super().finish(schedule_post_call, close_reason=close_reason, close_code=close_code)


class SizedTranscriptSentinel:
    """Sized history that fails if the session eagerly copies or iterates it."""

    def __len__(self) -> int:
        return 10_000

    def __getitem__(self, index):
        if isinstance(index, slice):
            return []
        raise AssertionError("the transcript boundary must not index individual items")

    def __iter__(self):
        raise AssertionError("the transcript boundary must not copy the full history")


@pytest.mark.asyncio
async def test_real_acknowledged_transfer_stays_open_until_dialog_stop_and_posts_once(
    monkeypatch,
):
    import dashboard_client
    import server
    import smartpbx_mcp

    transfer_calls: list[str] = []

    async def transfer_call(_self, target: str):
        transfer_calls.append(target)
        return SimpleNamespace(acknowledged=True)

    async def dashboard_ack(**_kwargs) -> None:
        return None

    monkeypatch.setattr(smartpbx_mcp.DialogMCPCallControl, "transfer_call", transfer_call)
    monkeypatch.setattr(dashboard_client, "send_call_transferred", dashboard_ack)

    started = asyncio.Event()
    stt = RecordingSTT(started)
    post_calls: list[dict] = []
    sessions: list[CountingSession] = []

    async def post_call(**metadata) -> None:
        post_calls.append(metadata)

    async def session_factory(context, transport, sink=None):
        pipeline = server.MediaStreamSession(
            websocket=None,
            lang="en",
            media_transport=transport,
        )
        pipeline.full_transcript = SizedTranscriptSentinel()
        session = CountingSession(
            context,
            transport,
            pipeline=pipeline,
            stt_factory=lambda **_kwargs: stt,
            post_call_processor=post_call,
            welcome_text="",
            llm_provider="openai",
            model="test-model",
        )
        assert sink is None or callable(sink)
        sessions.append(session)
        return session

    configuration = replace(
        SmartPBXSettings.from_env(
            {
                "ENABLE_SMARTPBX_WSS": "true",
                "SMARTPBX_WS_TOKEN": "test-token",
                "SMARTPBX_ACCOUNT_ID": "dialog-account",
            }
        ),
        idle_timeout_seconds=0.03,
    )
    registry = SmartPBXSessionRegistry(configuration.max_calls)
    gateway = SmartPBXGateway(configuration, registry)
    socket = QueueWebSocket()
    gateway_task = asyncio.create_task(gateway.handle(socket, session_factory))
    socket.messages.put_nowait(
        json.dumps(
            {
                "event": "dtmf",
                "dtmf": {
                    "callId": "dialog-media-leg",
                    "otherLegCallId": "dialog-safe-call",
                    "digit": "1",
                },
            }
        )
    )

    await asyncio.wait_for(started.wait(), timeout=1)
    session = sessions[0]
    pipeline = session._pipeline
    coordinator = pipeline._smartpbx_transfer_context.coordinator

    sentinel = pipeline.full_transcript
    assert coordinator._transcript() is sentinel
    pipeline.full_transcript = [{"role": "user", "text": "Please connect me."}]

    result = json.loads(await coordinator.attempt("Guest requested a person."))
    assert result == {
        "status": "transfer_requested",
        "confirmation": "provider_acknowledged",
    }
    assert transfer_calls == ["human_support"]
    assert session.transfer_pending is True
    assert not session.terminal_future.done()

    socket.messages.put_nowait(
        json.dumps(
            {
                "event": "media",
                "media": {
                    "payload": base64.b64encode(b"post-ack-audio").decode("ascii")
                },
            }
        )
    )
    await asyncio.sleep(0.05)
    assert not gateway_task.done(), "acknowledged transfer must outlive AI idle timeout"
    assert stt.audio == [], "post-ack carrier audio must not reach Kavya STT"

    socket.messages.put_nowait(json.dumps({"event": "stop"}))
    await asyncio.wait_for(gateway_task, timeout=1)
    if session._post_call_task is not None:
        await asyncio.wait_for(session._post_call_task, timeout=1)

    assert socket.close_calls == [(1000, "call ended")]
    assert session.finish_calls == 1
    assert stt.stops == 1
    assert session.terminal_future.done()
    assert registry.snapshot()["active_sessions"] == 0
    assert registry.snapshot()["released_total"] == 1
    assert len(post_calls) == 1
    assert post_calls[0]["privacy_safe"] is True
    assert post_calls[0]["call_sid"] == "dialog-safe-call"
