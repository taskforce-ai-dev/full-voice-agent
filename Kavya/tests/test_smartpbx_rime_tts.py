"""RED contracts for the Direct SmartPBX Sinhala Rime Arcana canary."""

from __future__ import annotations

import asyncio
import logging

import pytest


class FakeTransport:
    def __init__(self) -> None:
        self.audio: list[bytes] = []
        self.marks: list[str] = []
        self.first_audio = asyncio.Event()

    async def send_audio(self, audio: bytes) -> None:
        self.audio.append(audio)
        self.first_audio.set()

    async def clear_audio(self) -> int:
        return 0

    async def send_mark(self, name: str) -> None:
        self.marks.append(name)


class FakeRimeResponse:
    status_code = 200

    def __init__(self, chunks: list[bytes], *, failure: BaseException | None = None) -> None:
        self.chunks = chunks
        self.failure = failure
        self.yielded = 0

    def raise_for_status(self) -> None:
        return None

    async def aiter_bytes(self):
        for chunk in self.chunks:
            self.yielded += 1
            yield chunk
        if self.failure is not None:
            raise self.failure


class FakeRimeStream:
    def __init__(self, response: FakeRimeResponse) -> None:
        self.response = response
        self.closed = False

    async def __aenter__(self) -> FakeRimeResponse:
        return self.response

    async def __aexit__(self, *_args) -> bool:
        self.closed = True
        return False


class FakeRimeClient:
    def __init__(self, response: FakeRimeResponse) -> None:
        self.response = response
        self.stream_kwargs: dict[str, object] | None = None
        self.http_stream = FakeRimeStream(response)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args) -> bool:
        return False

    def stream(self, method: str, url: str, **kwargs):
        self.stream_kwargs = {"method": method, "url": url, **kwargs}
        return self.http_stream


def make_direct_sinhala(server):
    transport = FakeTransport()
    pipeline = server.MediaStreamSession(
        websocket=None,
        lang="si",
        media_transport=transport,
        llm_provider="gemini",
    )
    pipeline._smartpbx_transfer_context = object()
    return pipeline, transport


def test_rime_provider_selector_defaults_to_gemini_and_rejects_unknown_values():
    import server

    assert server._resolve_smartpbx_sinhala_tts_provider(None) == "gemini"
    assert server._resolve_smartpbx_sinhala_tts_provider("") == "gemini"
    assert server._resolve_smartpbx_sinhala_tts_provider("RIME") == "rime"
    assert server._resolve_smartpbx_sinhala_tts_provider("other") == "gemini"


def test_rime_arcana_request_payload_uses_the_fixed_sinhala_pcmu_contract():
    import server

    payload = server._rime_arcana_request_payload("සිංහල")

    assert payload["text"] == "සිංහල"
    assert payload["modelId"] == "arcana"
    assert payload["speaker"] == "chandani"
    assert payload["lang"] == "si"
    assert payload["samplingRate"] == 8000


@pytest.mark.asyncio
async def test_rime_stream_uses_pcmu_headers_and_streams_before_provider_eof(monkeypatch):
    import server

    response = FakeRimeResponse([b"first", b"second"])
    client = FakeRimeClient(response)
    monkeypatch.setattr(server, "RIME_API_KEY", "test-rime-key")
    monkeypatch.setattr(server.httpx, "AsyncClient", lambda: client)

    chunks = [chunk async for chunk in server._stream_rime_arcana_mulaw("සිංහල")]

    assert chunks == [b"first", b"second"]
    assert client.stream_kwargs == {
        "method": "POST",
        "url": "https://users.rime.ai/v1/rime-tts",
        "json": server._rime_arcana_request_payload("සිංහල"),
        "headers": {
            "Accept": "audio/PCMU",
            "Authorization": "Bearer test-rime-key",
            "Content-Type": "application/json",
        },
        "timeout": server.SMARTPBX_SINHALA_GEMINI_TTS_TIMEOUT_SECONDS,
    }


@pytest.mark.asyncio
async def test_first_rime_audio_is_accepted_before_provider_eof_and_chunks_are_untouched(
    monkeypatch,
):
    import server

    pipeline, transport = make_direct_sinhala(server)
    provider_can_finish = asyncio.Event()

    async def stream(_text: str):
        yield b"abc"
        await provider_can_finish.wait()
        yield b"defg"

    monkeypatch.setattr(server, "_stream_rime_arcana_mulaw", stream)
    task = asyncio.create_task(pipeline._tts_rime_sinhala("සිංහල", sentence="සිංහල"))
    await asyncio.wait_for(transport.first_audio.wait(), timeout=1)

    assert transport.audio == [b"abc"]
    assert transport.marks == []

    provider_can_finish.set()
    await task
    assert transport.audio == [b"abc", b"defg"]
    assert transport.marks == ["tts_done"]
    assert b"".join(transport.audio) == b"abcdefg"


@pytest.mark.asyncio
async def test_rime_stream_never_invokes_ffmpeg_decode(monkeypatch):
    import server

    pipeline, transport = make_direct_sinhala(server)

    async def stream(_text: str):
        yield b"raw-pcmu"

    async def forbidden_ffmpeg(*_args, **_kwargs):
        raise AssertionError("Rime PCMU streaming must not invoke ffmpeg")

    monkeypatch.setattr(server, "_stream_rime_arcana_mulaw", stream)
    monkeypatch.setattr(server.asyncio, "create_subprocess_exec", forbidden_ffmpeg)

    await pipeline._tts_rime_sinhala("සිංහල", sentence="සිංහල")

    assert transport.audio == [b"raw-pcmu"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [None, RuntimeError("provider body SECRET_EXCEPTION")])
async def test_empty_or_failed_rime_stream_falls_back_to_gemini_once(monkeypatch, failure):
    import server

    pipeline, _transport = make_direct_sinhala(server)
    gemini_calls: list[str] = []

    async def stream(_text: str):
        if failure is not None:
            raise failure
        if False:
            yield b"unreachable"

    async def gemini(text: str, **_kwargs):
        gemini_calls.append(text)

    monkeypatch.setattr(server, "_stream_rime_arcana_mulaw", stream)
    monkeypatch.setattr(pipeline, "_tts_gemini_sinhala", gemini)

    await pipeline._tts_rime_sinhala("සිංහල", sentence="සිංහල")

    assert gemini_calls == ["සිංහල"]


@pytest.mark.asyncio
async def test_rime_failure_after_accepted_audio_never_falls_back_to_gemini(monkeypatch):
    import server

    pipeline, transport = make_direct_sinhala(server)
    gemini_calls: list[str] = []

    async def stream(_text: str):
        yield b"accepted"
        raise server._RimeArcanaTTSFailure("transport_error")

    async def gemini(text: str, **_kwargs):
        gemini_calls.append(text)

    monkeypatch.setattr(server, "_stream_rime_arcana_mulaw", stream)
    monkeypatch.setattr(pipeline, "_tts_gemini_sinhala", gemini)

    await pipeline._tts_rime_sinhala("සිංහල")

    assert transport.audio == [b"accepted"]
    assert gemini_calls == []


@pytest.mark.asyncio
async def test_generation_supersession_drops_later_rime_audio_and_tts_done(monkeypatch):
    import server

    pipeline, transport = make_direct_sinhala(server)

    async def stream(_text: str):
        yield b"first"
        await transport.first_audio.wait()
        pipeline._speak_generation += 1
        yield b"stale"

    monkeypatch.setattr(server, "_stream_rime_arcana_mulaw", stream)

    await pipeline._tts_rime_sinhala("සිංහල", sentence="සිංහල")

    assert transport.audio == [b"first"]
    assert transport.marks == []


@pytest.mark.asyncio
async def test_rime_response_size_is_enforced_incrementally(monkeypatch):
    import server

    response = FakeRimeResponse([b"1234", b"56", b"never-read"])
    client = FakeRimeClient(response)
    monkeypatch.setattr(server, "RIME_API_KEY", "test-rime-key")
    monkeypatch.setattr(server, "_RIME_ARCANA_TTS_MAX_RESPONSE_BYTES", 5)
    monkeypatch.setattr(server.httpx, "AsyncClient", lambda: client)

    with pytest.raises(server._RimeArcanaTTSFailure) as raised:
        _ = [chunk async for chunk in server._stream_rime_arcana_mulaw("සිංහල")]

    assert raised.value.outcome == "response_too_large"
    assert response.yielded == 2


@pytest.mark.asyncio
async def test_rime_cancellation_closes_the_http_stream(monkeypatch):
    import server

    entered = asyncio.Event()
    never = asyncio.Event()

    class Response(FakeRimeResponse):
        async def aiter_bytes(self):
            entered.set()
            await never.wait()
            yield b"unreachable"

    response = Response([])
    client = FakeRimeClient(response)
    monkeypatch.setattr(server, "RIME_API_KEY", "test-rime-key")
    monkeypatch.setattr(server.httpx, "AsyncClient", lambda: client)

    async def consume():
        return [chunk async for chunk in server._stream_rime_arcana_mulaw("සිංහල")]

    task = asyncio.create_task(consume())
    await asyncio.wait_for(entered.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert client.http_stream.closed is True


@pytest.mark.asyncio
async def test_rime_telemetry_is_bounded_and_privacy_safe(monkeypatch, caplog):
    import server

    secret = "rime-secret-should-not-leak"
    caller_text = "සිංහල caller 0771234567"
    response_body = "provider-response-body-123456789"
    exception_text = "exception-text-with-secret-" + secret + "-" + response_body
    response = FakeRimeResponse([], failure=RuntimeError(exception_text))
    client = FakeRimeClient(response)
    monkeypatch.setattr(server, "RIME_API_KEY", secret)
    monkeypatch.setattr(server.httpx, "AsyncClient", lambda: client)
    caplog.set_level(logging.INFO)

    with pytest.raises(server._RimeArcanaTTSFailure):
        _ = [chunk async for chunk in server._stream_rime_arcana_mulaw(caller_text)]

    telemetry = "\n".join(record.getMessage() for record in caplog.records)
    assert telemetry
    assert caller_text not in telemetry
    assert "0771234567" not in telemetry
    assert secret not in telemetry
    assert response_body not in telemetry
    assert exception_text not in telemetry
    assert len(telemetry) <= 1000


@pytest.mark.asyncio
async def test_rime_route_is_limited_to_direct_smartpbx_sinhala(monkeypatch):
    import server

    monkeypatch.setattr(server, "SMARTPBX_SINHALA_TTS_PROVIDER", "rime")
    direct_sinhala, _ = make_direct_sinhala(server)
    direct_english = server.MediaStreamSession(
        websocket=None, lang="en", media_transport=FakeTransport(), llm_provider="claude",
    )
    direct_english._smartpbx_transfer_context = object()
    twilio_sinhala = server.MediaStreamSession(websocket=None, lang="si")
    routes: list[str] = []

    async def rime(*_args, **_kwargs):
        routes.append("rime")

    async def gemini(*_args, **_kwargs):
        routes.append("gemini")

    async def elevenlabs(*_args, **_kwargs):
        routes.append("elevenlabs")

    async def openai(*_args, **_kwargs):
        routes.append("openai")

    for pipeline in (direct_sinhala, direct_english, twilio_sinhala):
        monkeypatch.setattr(pipeline, "_tts_rime_sinhala", rime)
        monkeypatch.setattr(pipeline, "_tts_gemini_sinhala", gemini)
        monkeypatch.setattr(pipeline, "_tts_elevenlabs", elevenlabs)
        monkeypatch.setattr(pipeline, "_tts_openai", openai)

    await direct_sinhala._speak("සිංහල")
    await direct_english._speak("English")
    await twilio_sinhala._speak("සිංහල")

    assert routes == ["rime", "elevenlabs", "openai"]


@pytest.mark.asyncio
async def test_rime_selector_keeps_cached_fixed_phrase_on_existing_gemini_playback_path(
    monkeypatch,
):
    import server

    pipeline, transport = make_direct_sinhala(server)
    text = server.SMARTPBX_SINHALA_INITIAL_FILLER_TEXT
    monkeypatch.setattr(server, "SMARTPBX_SINHALA_TTS_PROVIDER", "rime")
    monkeypatch.setattr(server, "GEMINI_API_KEY", "test-gemini-key")
    monkeypatch.setattr(server, "_SMARTPBX_SINHALA_PHRASE_AUDIO", {})
    server._store_cached_smartpbx_sinhala_phrase_audio(text, b"\x00" * 640)

    async def rime_must_not_run(*_args, **_kwargs):
        raise AssertionError("cached fixed phrase must not request Rime")

    monkeypatch.setattr(pipeline, "_tts_rime_sinhala", rime_must_not_run)

    await pipeline._speak(text, sentence=text)

    assert transport.audio == [b"\x00" * 640]
    assert transport.marks == ["tts_done"]
