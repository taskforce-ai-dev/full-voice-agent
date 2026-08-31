"""Direct SmartPBX Sinhala ownership and audible-turn-taking contracts."""

from __future__ import annotations

import asyncio
import base64
from types import SimpleNamespace

import pytest


class _Transport:
    def __init__(self) -> None:
        self.audio: list[bytes] = []
        self.marks: list[str] = []
        self.session = None
        self.speaking_at_send: list[bool] = []

    async def send_audio(self, audio: bytes) -> None:
        if self.session is not None:
            self.speaking_at_send.append(self.session._is_speaking)
        self.audio.append(audio)

    async def clear_audio(self) -> int:
        return 0

    async def send_mark(self, name: str) -> None:
        self.marks.append(name)


def _direct_sinhala(server):
    transport = _Transport()
    session = server.MediaStreamSession(
        websocket=None, lang="si", media_transport=transport, llm_provider="claude",
    )
    session._smartpbx_transfer_context = object()
    transport.session = session
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
    assert transport.speaking_at_send[0] is False
    assert transport.marks == ["tts_done"]
    assert session._is_speaking is False
    assert session._tts_synthesis_in_flight is False
    assert session._tts_synthesis_generation is None
    assert task.done()
    assert session._smartpbx_deferred_tts_tasks == set()


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


@pytest.mark.asyncio
async def test_english_claude_request_keeps_its_existing_effort_shape(monkeypatch):
    import server

    client = _ClaudeClient([
        SimpleNamespace(
            type="content_block_delta",
            delta=SimpleNamespace(type="text_delta", text="Short reply."),
        ),
        SimpleNamespace(
            type="message_delta",
            delta=SimpleNamespace(stop_reason="end_turn"),
            usage=SimpleNamespace(output_tokens=12),
        ),
        SimpleNamespace(type="message_stop"),
    ])
    session = server.MediaStreamSession(
        websocket=None, lang="en", media_transport=_Transport(),
        llm_provider="claude", anthropic_client=client,
    )
    session._smartpbx_transfer_context = object()

    async def _no_speak(*_args, **_kwargs):
        return None

    monkeypatch.setattr(server, "retrieve_context", lambda _text: "")
    monkeypatch.setattr(session, "_speak", _no_speak)
    await session._process_utterance_bound("guest question")

    assert "output_config" not in client.requests[0]
    assert "thinking" not in client.requests[0]


@pytest.mark.asyncio
async def test_single_pre_audio_stt_tail_does_not_cancel_sinhala_synthesis(monkeypatch):
    import server

    session, _transport = _direct_sinhala(server)
    admitted: list[str] = []

    async def _record(text):
        admitted.append(text)

    monkeypatch.setattr(session, "_accumulate_transcript", _record)
    session._tts_synthesis_in_flight = True
    session._tts_synthesis_generation = session._speak_generation

    await session._handle_pre_audio_stt("final", "late provider tail")

    assert session._speak_generation == 0
    assert admitted == []
    await session._flush_pre_audio_stt()
    assert admitted == ["late provider tail"]


@pytest.mark.asyncio
async def test_sustained_pre_audio_stt_cancels_sinhala_synthesis_and_admits_once(monkeypatch):
    import server

    session, _transport = _direct_sinhala(server)
    admitted: list[str] = []
    ticks = iter((10.0, 10.3, 10.3))

    async def _record(text):
        admitted.append(text)

    async def _no_clear(*_args, **_kwargs):
        return 0

    monkeypatch.setattr(server.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(session, "_accumulate_transcript", _record)
    monkeypatch.setattr(session, "_clear_media_audio", _no_clear)
    session._tts_synthesis_in_flight = True
    session._tts_synthesis_generation = session._speak_generation

    await session._handle_pre_audio_stt("interim", "caller continues")
    await session._handle_pre_audio_stt("final", "with more detail")

    assert session._speak_generation == 1
    assert admitted == ["caller continues with more detail"]


@pytest.mark.asyncio
async def test_sinhala_recovery_is_never_the_english_fallback(monkeypatch):
    import server

    session, _transport = _direct_sinhala(server)
    spoken: list[str] = []

    async def _speak(text, **_kwargs):
        spoken.append(text)

    monkeypatch.setattr(session, "_speak", _speak)
    await session._smartpbx_speak_recovery_and_finish(
        tool_executed=False, gen=0, full_text=""
    )

    assert len(spoken) == 1
    assert "I'm sorry" not in spoken[0]


def test_sinhala_effort_knob_is_allowlisted_and_has_a_high_safety_fallback():
    import server

    assert server._resolve_smartpbx_sinhala_claude_effort(None) == "medium"
    assert server._resolve_smartpbx_sinhala_claude_effort("") == "medium"
    assert server._resolve_smartpbx_sinhala_claude_effort("   ") == "medium"
    assert server._resolve_smartpbx_sinhala_claude_effort("medium") == "medium"
    assert server._resolve_smartpbx_sinhala_claude_effort("high") == "high"
    assert server._resolve_smartpbx_sinhala_claude_effort("invalid") == "high"


@pytest.mark.asyncio
async def test_pre_audio_callbacks_keep_latest_cumulative_hypothesis_once(monkeypatch):
    import server

    session, _transport = _direct_sinhala(server)
    session._event_loop = asyncio.get_running_loop()
    session._tts_synthesis_in_flight = True
    session._tts_synthesis_generation = session._speak_generation
    monkeypatch.setattr(server, "SMARTPBX_PRE_AUDIO_STT_MIN_SECONDS", 0.0)

    session._on_stt_interim("I need")
    await asyncio.sleep(0)
    session._on_stt_result("I need a room")
    await asyncio.sleep(0.02)

    assert session._speak_generation == 1
    assert session._pending_transcript == "I need a room"
    assert "I need I need" not in session._pending_transcript
    if session._endpointing_handle is not None:
        session._endpointing_handle.cancel()


class _HangingClaudeStream:
    async def __aenter__(self):
        await asyncio.Event().wait()

    async def __aexit__(self, *_args):
        return False


class _HangingClaudeClient:
    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.messages = SimpleNamespace(stream=self.stream)

    def stream(self, **kwargs):
        self.requests.append(kwargs)
        return _HangingClaudeStream()


@pytest.mark.asyncio
@pytest.mark.parametrize("lang", ["si", "en"])
async def test_direct_claude_initial_timeout_recovers_in_the_selected_language(
    monkeypatch, lang,
):
    import server

    client = _HangingClaudeClient()
    transport = _Transport()
    session = server.MediaStreamSession(
        websocket=None, lang=lang, media_transport=transport,
        llm_provider="claude", anthropic_client=client,
    )
    transport.session = session
    session._smartpbx_transfer_context = object()
    spoken: list[str] = []

    async def _speak(text, **_kwargs):
        spoken.append(text)

    monkeypatch.setattr(server, "retrieve_context", lambda _text: "")
    monkeypatch.setattr(server, "SMARTPBX_LLM_INITIAL_RESPONSE_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(server, "SMARTPBX_LLM_STALL_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(session, "_speak", _speak)

    await asyncio.wait_for(
        session._process_utterance_bound("caller question"), timeout=1.0
    )

    assert len(client.requests) == 1
    assert len(spoken) == 1
    if lang == "si":
        assert "I'm sorry" not in spoken[0]
    else:
        assert spoken == [server.SMARTPBX_LLM_EMPTY_RETRY_RECOVERY_TEXT]
