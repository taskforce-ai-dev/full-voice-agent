"""Provider-bound regression coverage for the one SmartPBX welcome path."""

import asyncio
import audioop

import pytest


class FakeTransport:
    def __init__(self):
        self.audio = []
        self.marks = []

    async def send_audio(self, audio):
        self.audio.append(audio)

    async def send_mark(self, name):
        self.marks.append(name)


class Response:
    status_code = 200

    def __init__(self, chunks):
        self._chunks = chunks

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def aread(self):
        return b"provider failure"

    async def aiter_bytes(self, chunk_size):
        assert chunk_size == 640
        for chunk in self._chunks:
            yield chunk


class Client:
    def __init__(self, response):
        self.response = response
        self.requests = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    def stream(self, method, url, *, json, headers, timeout):
        self.requests.append((method, url, json, headers, timeout))
        return self.response


def _mulaw(samples):
    pcm = b"".join(int(sample).to_bytes(2, "little", signed=True) for sample in samples)
    return audioop.lin2ulaw(pcm, 2)


def _pipeline(server):
    transport = FakeTransport()
    pipeline = server.MediaStreamSession(websocket=None, lang="en", media_transport=transport)
    pipeline._smartpbx_transfer_context = object()
    return pipeline, transport


def _configure_english_voice(monkeypatch, server):
    from english_voice_profile import load_kavya_english_voice_profile

    profile = load_kavya_english_voice_profile(
        {"KAVYA_EN_ELEVENLABS_VOICE_ID": "unit-test-canonical-voice"}
    )
    monkeypatch.setattr(server, "ELEVENLABS_API_KEY", "unit-test-api-key")
    monkeypatch.setattr(server, "load_kavya_english_voice_profile", lambda: profile)


@pytest.mark.asyncio
async def test_direct_welcome_is_faded_cached_and_delivered_once(monkeypatch):
    import server
    server._SMARTPBX_WELCOME_AUDIO_CACHE.clear()
    _configure_english_voice(monkeypatch, server)
    raw = b"\xff" * 17 + _mulaw([12_000] * 600)
    client = Client(Response([raw[:300], raw[300:]]))
    monkeypatch.setattr(server.httpx, "AsyncClient", lambda: client)
    text = "Welcome to Hatton Hills. How may I help you today?"

    first, first_transport = _pipeline(server)
    first._smartpbx_welcome_audio_pending = text
    await first._tts_elevenlabs(text)
    second, second_transport = _pipeline(server)
    second._smartpbx_welcome_audio_pending = text
    await second._tts_elevenlabs(text)

    assert len(client.requests) == 1
    assert first_transport.audio == second_transport.audio
    assert first_transport.audio[0][:17] == raw[:17]
    assert first_transport.audio[0][17 + 480:] == raw[17 + 480:]
    assert first_transport.marks == second_transport.marks == ["tts_done"]


@pytest.mark.asyncio
async def test_normal_smartpbx_sentence_keeps_streaming_chunks_unmodified(monkeypatch):
    import server
    server._SMARTPBX_WELCOME_AUDIO_CACHE.clear()
    _configure_english_voice(monkeypatch, server)
    chunks = [b"normal-one", b"normal-two"]
    client = Client(Response(chunks))
    monkeypatch.setattr(server.httpx, "AsyncClient", lambda: client)
    pipeline, transport = _pipeline(server)

    await pipeline._tts_elevenlabs("A normal mid-call answer.")

    assert len(client.requests) == 1
    assert transport.audio == chunks
    assert not server._SMARTPBX_WELCOME_AUDIO_CACHE
    assert transport.marks == ["tts_done"]


@pytest.mark.asyncio
async def test_interrupted_welcome_neither_sends_stale_audio_nor_completes(monkeypatch):
    import server
    server._SMARTPBX_WELCOME_AUDIO_CACHE.clear()
    _configure_english_voice(monkeypatch, server)
    entered = asyncio.Event()
    release = asyncio.Event()
    raw = _mulaw([12_000] * 600)

    class BlockingResponse(Response):
        async def aiter_bytes(self, chunk_size):
            entered.set()
            await release.wait()
            yield raw

    client = Client(BlockingResponse([]))
    monkeypatch.setattr(server.httpx, "AsyncClient", lambda: client)
    text = "Welcome to Hatton Hills. How may I help you today?"
    pipeline, transport = _pipeline(server)
    pipeline._smartpbx_welcome_audio_pending = text
    task = asyncio.create_task(pipeline._tts_elevenlabs(text))
    await entered.wait()
    pipeline._is_speaking = False
    release.set()
    await task

    assert transport.audio == []
    assert transport.marks == []
    assert server._SMARTPBX_WELCOME_AUDIO_CACHE


@pytest.mark.asyncio
async def test_interrupted_cold_greeting_cannot_cancel_another_callers_welcome(monkeypatch):
    import server

    server._SMARTPBX_WELCOME_AUDIO_CACHE.clear()
    _configure_english_voice(monkeypatch, server)
    entered = asyncio.Event()
    release = asyncio.Event()
    raw = _mulaw([12_000] * 600)

    class BlockingResponse(Response):
        async def aiter_bytes(self, chunk_size):
            entered.set()
            await release.wait()
            yield raw

    first_client = Client(BlockingResponse([]))
    second_client = Client(Response([raw]))
    clients = iter((first_client, second_client))
    monkeypatch.setattr(server.httpx, "AsyncClient", lambda: next(clients))
    text = "Welcome to Hatton Hills. How may I help you today?"
    interrupted, interrupted_transport = _pipeline(server)
    active, active_transport = _pipeline(server)
    interrupted._smartpbx_welcome_audio_pending = text
    active._smartpbx_welcome_audio_pending = text

    first = asyncio.create_task(interrupted._tts_elevenlabs(text))
    await entered.wait()
    second = asyncio.create_task(active._tts_elevenlabs(text))
    await second
    interrupted._is_speaking = False
    release.set()
    await first

    assert interrupted_transport.audio == []
    assert interrupted_transport.marks == []
    assert active_transport.audio
    assert active_transport.marks == ["tts_done"]
    assert len(server._SMARTPBX_WELCOME_AUDIO_CACHE) == 1
    assert next(iter(server._SMARTPBX_WELCOME_AUDIO_CACHE.values())) == active_transport.audio[0]


@pytest.mark.asyncio
async def test_welcome_provider_failure_uses_existing_smartpbx_diagnostic(monkeypatch):
    import server
    from smartpbx_diagnostics import DiagnosticFailureClass, DiagnosticOutcome, DiagnosticStage

    _configure_english_voice(monkeypatch, server)
    response = Response([])
    response.status_code = 503
    client = Client(response)
    monkeypatch.setattr(server.httpx, "AsyncClient", lambda: client)
    text = "Welcome to Hatton Hills. How may I help you today?"
    pipeline, transport = _pipeline(server)
    pipeline._smartpbx_welcome_audio_pending = text
    diagnostics = []
    pipeline._smartpbx_diagnostic_sink = lambda *event: diagnostics.append(event)

    await pipeline._tts_elevenlabs(text)

    assert not pipeline._is_speaking
    assert transport.audio == []
    assert transport.marks == []
    assert diagnostics == [
        (DiagnosticStage.TTS, DiagnosticOutcome.FAILED, DiagnosticFailureClass.TTS_HTTP_STATUS)
    ]


@pytest.mark.asyncio
async def test_empty_welcome_response_is_not_cached_or_completed(monkeypatch):
    import server
    from smartpbx_diagnostics import DiagnosticFailureClass, DiagnosticOutcome, DiagnosticStage

    server._SMARTPBX_WELCOME_AUDIO_CACHE.clear()
    _configure_english_voice(monkeypatch, server)
    client = Client(Response([]))
    monkeypatch.setattr(server.httpx, "AsyncClient", lambda: client)
    text = "Welcome to Hatton Hills. How may I help you today?"
    pipeline, transport = _pipeline(server)
    pipeline._smartpbx_welcome_audio_pending = text
    diagnostics = []
    pipeline._smartpbx_diagnostic_sink = lambda *event: diagnostics.append(event)

    await pipeline._tts_elevenlabs(text)

    assert not pipeline._is_speaking
    assert transport.audio == []
    assert transport.marks == []
    assert not server._SMARTPBX_WELCOME_AUDIO_CACHE
    assert diagnostics == [
        (DiagnosticStage.TTS, DiagnosticOutcome.FAILED, DiagnosticFailureClass.TTS_EXCEPTION)
    ]
