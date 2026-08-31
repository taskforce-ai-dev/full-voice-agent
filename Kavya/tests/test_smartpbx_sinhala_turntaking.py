"""Direct SmartPBX Sinhala ownership and audible-turn-taking contracts."""

from __future__ import annotations

import asyncio
import base64
from types import SimpleNamespace

import pytest


class _Transport:
    def __init__(self) -> None:
        self.audio: list[bytes] = []

    async def send_audio(self, audio: bytes) -> None:
        self.audio.append(audio)

    async def clear_audio(self) -> int:
        return 0

    async def send_mark(self, _name: str) -> None:
        return None


def _direct_sinhala(server):
    transport = _Transport()
    session = server.MediaStreamSession(
        websocket=None, lang="si", media_transport=transport, llm_provider="claude",
    )
    session._smartpbx_transfer_context = object()
    return session, transport


class _BlockedAudioStream:
    def __init__(self, event) -> None:
        self.event = event
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.sent = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.sent:
            raise StopAsyncIteration
        self.entered.set()
        await self.release.wait()
        self.sent = True
        return self.event


class _GeminiTtsClient:
    def __init__(self, stream) -> None:
        self.stream = stream
        self.aio = SimpleNamespace(
            interactions=SimpleNamespace(create=self.create),
        )

    async def create(self, **_kwargs):
        return self.stream


class _AsyncEvents:
    def __init__(self, events) -> None:
        self._events = iter(events)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._events)
        except StopIteration:
            raise StopAsyncIteration from None


class _ClaudeStream:
    def __init__(self, events) -> None:
        self._events = events

    async def __aenter__(self):
        return _AsyncEvents(self._events)

    async def __aexit__(self, *_args):
        return False


class _ClaudeClient:
    def __init__(self, events) -> None:
        self.requests: list[dict] = []
        self._events = events
        self.messages = SimpleNamespace(stream=self.stream)

    def stream(self, **kwargs):
        self.requests.append(kwargs)
        return _ClaudeStream(self._events)


def _audio_event(pcm: bytes):
    return SimpleNamespace(
        event_type="step.delta",
        delta=SimpleNamespace(
            type="audio",
            data=base64.b64encode(pcm).decode("ascii"),
            mime_type="audio/l16",
            channels=1,
            sample_rate=24000,
        ),
    )


def test_sinhala_direct_smartpbx_gets_the_shared_turn_contract():
    import server

    session, _transport = _direct_sinhala(server)

    assert session._is_direct_smartpbx() is True
    assert session._ensure_smartpbx_turn_telemetry() is not None
    assert "SMARTPBX CALLER RHYTHM" in session._smartpbx_rhythm_rule()


@pytest.mark.asyncio
async def test_sinhala_is_not_speaking_until_gemini_emits_current_generation_audio(monkeypatch):
    import server

    session, transport = _direct_sinhala(server)
    monkeypatch.setattr(server, "GEMINI_API_KEY", "test-key")
    stream = _BlockedAudioStream(_audio_event(bytes(range(256)) * 20))
    session._gemini_tts_client = _GeminiTtsClient(stream)

    task = asyncio.create_task(session._tts_gemini_sinhala("සිංහල පිළිතුර"))
    await stream.entered.wait()

    assert session._is_speaking is False
    stream.release.set()
    await task
    assert transport.audio


@pytest.mark.asyncio
async def test_sinhala_claude_request_keeps_thinking_and_sets_medium_effort(monkeypatch):
    import server

    client = _ClaudeClient([
        SimpleNamespace(
            type="content_block_delta",
            delta=SimpleNamespace(type="text_delta", text="කෙටි පිළිතුර."),
        ),
        SimpleNamespace(
            type="message_delta",
            delta=SimpleNamespace(stop_reason="end_turn"),
            usage=SimpleNamespace(output_tokens=12),
        ),
        SimpleNamespace(type="message_stop"),
    ])
    session, _transport = _direct_sinhala(server)
    session.anthropic_client = client

    async def _no_speak(*_args, **_kwargs):
        return None

    monkeypatch.setattr(server, "retrieve_context", lambda _text: "")
    monkeypatch.setattr(session, "_speak", _no_speak)
    await session._process_utterance_bound("පරීක්ෂණ ප්‍රශ්නය")

    request = client.requests[0]
    assert request["output_config"] == {"effort": "medium"}
    assert "thinking" not in request
