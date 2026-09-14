"""Client-neutral streaming TTS for SmartPBX g711_ulaw/8000 media.

This module has no environment reads.  Its startup owner supplies immutable
configuration and provider clients before any call begins.  It deliberately
returns framed bytes to ``SmartPBXMediaTransport`` instead of writing a socket:
the transport remains the one authority for paced delivery, bounded
backpressure, and barge-in generation clearing.
"""

from __future__ import annotations

import audioop
from collections.abc import AsyncIterable, AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping, Protocol


SAMPLE_RATE_HZ = 8_000
FRAME_BYTES = 160  # 20 ms of g711_ulaw/8 kHz, matching SmartPBX wire frames.
SILENCE_ULAW = b"\xff"
ELEVENLABS_STREAM_ENDPOINT = "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream"
RIME_ARCANA_ENDPOINT = "https://users.rime.ai/v1/rime-tts"
MAX_PROVIDER_RESPONSE_BYTES = 10 * 1024 * 1024


class TTSConfigurationError(ValueError):
    """Raised before calls when startup-injected TTS configuration is invalid."""


class TTSProviderError(RuntimeError):
    """A bounded provider outcome; it intentionally carries no caller text."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class ElevenLabsSettings:
    api_key: str
    voice_id: str
    model_id: str = "eleven_flash_v2_5"
    optimize_streaming_latency: int = 3
    timeout_seconds: float = 15.0
    max_response_bytes: int = MAX_PROVIDER_RESPONSE_BYTES

    def __post_init__(self) -> None:
        if not self.api_key or not self.voice_id:
            raise TTSConfigurationError("ElevenLabs key and voice are required")
        if not 0 <= self.optimize_streaming_latency <= 4:
            raise TTSConfigurationError("ElevenLabs latency must be between 0 and 4")
        if not 3 <= self.timeout_seconds <= 30:
            raise TTSConfigurationError("ElevenLabs timeout must be between 3 and 30 seconds")
        if not 1 <= self.max_response_bytes <= MAX_PROVIDER_RESPONSE_BYTES:
            raise TTSConfigurationError("ElevenLabs response bound is invalid")


@dataclass(frozen=True)
class SinhalaGeminiSettings:
    api_key: str
    model: str = "gemini-3.1-flash-tts-preview"
    voice: str = "Vindemiatrix"
    fallback_models: tuple[str, ...] = (
        "gemini-2.5-flash-preview-tts", "gemini-2.5-pro-preview-tts",
    )
    timeout_seconds: float = 15.0

    def __post_init__(self) -> None:
        if not self.api_key or not self.model or not self.voice:
            raise TTSConfigurationError("Gemini key, model, and voice are required")
        if not 3 <= self.timeout_seconds <= 30:
            raise TTSConfigurationError("Gemini timeout must be between 3 and 30 seconds")

    @property
    def models(self) -> tuple[str, ...]:
        return (self.model, *self.fallback_models)


@dataclass(frozen=True)
class SinhalaRimeSettings:
    api_key: str
    endpoint: str = RIME_ARCANA_ENDPOINT
    timeout_seconds: float = 15.0
    max_response_bytes: int = MAX_PROVIDER_RESPONSE_BYTES

    def __post_init__(self) -> None:
        if not self.api_key or not self.endpoint.startswith("https://"):
            raise TTSConfigurationError("Rime key and HTTPS endpoint are required")
        if not 3 <= self.timeout_seconds <= 30:
            raise TTSConfigurationError("Rime timeout must be between 3 and 30 seconds")
        if not 1 <= self.max_response_bytes <= MAX_PROVIDER_RESPONSE_BYTES:
            raise TTSConfigurationError("Rime response bound is invalid")


@dataclass(frozen=True)
class TTSLanguageRoute:
    """One exact profile code selected during startup; no locale guessing occurs."""

    provider: str

    def __post_init__(self) -> None:
        if self.provider not in {"elevenlabs", "gemini", "rime"}:
            raise TTSConfigurationError("unsupported TTS language route")


@dataclass(frozen=True)
class TTSStartupConfig:
    """Immutable startup-only credentials and provider choices, never tenant data."""

    english: ElevenLabsSettings | None
    sinhala_gemini: SinhalaGeminiSettings | None
    language_routes: Mapping[str, TTSLanguageRoute]
    sinhala_rime: SinhalaRimeSettings | None = None

    def __post_init__(self) -> None:
        routes = dict(self.language_routes)
        if not routes or any(
            not isinstance(code, str) or not code or not isinstance(route, TTSLanguageRoute)
            for code, route in routes.items()
        ):
            raise TTSConfigurationError("language routes must use explicit profile codes")
        object.__setattr__(self, "language_routes", MappingProxyType(routes))
        selected = {route.provider for route in routes.values()}
        if "elevenlabs" in selected and self.english is None:
            raise TTSConfigurationError("ElevenLabs settings are required")
        if "gemini" in selected and self.sinhala_gemini is None:
            raise TTSConfigurationError("Gemini settings are required")
        if "rime" in selected and self.sinhala_rime is None:
            raise TTSConfigurationError("Rime settings are required")


class HTTPResponse(Protocol):
    status_code: int
    headers: Any

    async def aiter_bytes(self, chunk_size: int = ...) -> AsyncIterator[bytes]: ...


class HTTPStreamingClient(Protocol):
    def stream(
        self, method: str, url: str, *, headers: dict[str, str], json: dict[str, Any], timeout: float
    ) -> AbstractAsyncContextManager[HTTPResponse]: ...


@dataclass(frozen=True)
class PCM24kChunk:
    data: bytes
    mime_type: str = "audio/l16"
    channels: int = 1
    sample_rate: int = 24_000


class GeminiStreamingClient(Protocol):
    async def stream_audio(
        self, *, api_key: str, model: str, text: str, voice: str, timeout: float
    ) -> AsyncIterable[PCM24kChunk]: ...


@dataclass(frozen=True)
class TTSProviderClients:
    http: HTTPStreamingClient | None
    gemini: GeminiStreamingClient | None


def _elevenlabs_url(settings: ElevenLabsSettings) -> str:
    url = ELEVENLABS_STREAM_ENDPOINT.format(voice_id=settings.voice_id)
    url += "?output_format=ulaw_8000"
    if settings.optimize_streaming_latency:
        url += f"&optimize_streaming_latency={settings.optimize_streaming_latency}"
    return url


async def _frame_ulaw(
    chunks: AsyncIterable[bytes], *, generation: int, generation_is_current: Callable[[int], bool]
) -> AsyncIterator[bytes]:
    """Preserve stream order and stop before a stale generation reaches transport."""
    residual = b""
    async for chunk in chunks:
        if not generation_is_current(generation):
            return
        if not isinstance(chunk, bytes):
            raise TTSProviderError("malformed_audio")
        residual += chunk
        while len(residual) >= FRAME_BYTES:
            if not generation_is_current(generation):
                return
            frame, residual = residual[:FRAME_BYTES], residual[FRAME_BYTES:]
            yield frame
    if residual and generation_is_current(generation):
        yield residual + (SILENCE_ULAW * (FRAME_BYTES - len(residual)))


async def _bounded_provider_audio(chunks: AsyncIterable[bytes], maximum: int) -> AsyncIterator[bytes]:
    """Reject an oversized provider body before it can fill the paced transport."""
    total_bytes = 0
    async for chunk in chunks:
        total_bytes += len(chunk)
        if total_bytes > maximum:
            raise TTSProviderError("response_too_large")
        yield chunk


class SmartPBXTTSAdapter:
    """Concrete provider selection that yields only framed PCMU to the caller."""

    active = True

    def __init__(self, config: TTSStartupConfig, clients: TTSProviderClients) -> None:
        self._config = config
        self._clients = clients

    async def close(self) -> None:
        for client in (self._clients.http, self._clients.gemini):
            close = getattr(client, "aclose", None)
            if callable(close):
                result = close()
                if hasattr(result, "__await__"):
                    await result

    async def synthesize_audio(self, response: str, language: str) -> AsyncIterable[bytes]:
        """ConversationProviderAdapter-compatible stream for a non-interrupted call."""
        return self.synthesize_for_generation(response, language, generation=0, generation_is_current=lambda _: True)

    async def synthesize_for_generation(
        self, response: str, language: str, *, generation: int, generation_is_current: Callable[[int], bool]
    ) -> AsyncIterable[bytes]:
        """Yield 160-byte PCMU frames; callers still send them through SmartPBXMediaTransport."""
        text = response.strip()
        if not text:
            return _empty_audio_stream()
        try:
            route = self._config.language_routes[language]
        except KeyError as exc:
            raise TTSConfigurationError("language profile has no TTS route") from exc
        if route.provider == "elevenlabs":
            return _frame_ulaw(
                self._stream_elevenlabs(text), generation=generation, generation_is_current=generation_is_current
            )
        if route.provider == "gemini":
            return self._stream_gemini(text, generation=generation, generation_is_current=generation_is_current)
        if route.provider == "rime":
            return _frame_ulaw(self._stream_rime(text), generation=generation, generation_is_current=generation_is_current)
        raise TTSConfigurationError("unsupported TTS language route")

    async def _stream_elevenlabs(self, text: str) -> AsyncIterator[bytes]:
        settings = self._config.english
        if settings is None or self._clients.http is None:
            raise TTSConfigurationError("ElevenLabs startup client is unavailable")
        headers = {"xi-api-key": settings.api_key, "Content-Type": "application/json"}
        payload = {
            "text": text,
            "model_id": settings.model_id,
            "voice_settings": {"stability": 0.5, "similarity_boost": 0.75, "style": 0.0, "use_speaker_boost": True},
        }
        async with self._clients.http.stream(
            "POST", _elevenlabs_url(settings), headers=headers, json=payload, timeout=settings.timeout_seconds
        ) as response:
            if response.status_code != 200:
                raise TTSProviderError("http_status")
            async for chunk in _bounded_provider_audio(
                response.aiter_bytes(chunk_size=640), settings.max_response_bytes
            ):
                if chunk:
                    yield chunk

    async def _stream_rime(self, text: str) -> AsyncIterator[bytes]:
        settings = self._config.sinhala_rime
        if settings is None or self._clients.http is None:
            raise TTSConfigurationError("Rime settings are unavailable")
        headers = {
            "Accept": "audio/PCMU",
            "Authorization": f"Bearer {settings.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "text": text, "modelId": "arcana", "speaker": "chandani", "lang": "si",
            "max_tokens": 1200, "repetition_penalty": 1.6, "samplingRate": SAMPLE_RATE_HZ,
            "speedAlpha": 1, "temperature": 0.5, "top_p": 1,
        }
        async with self._clients.http.stream(
            "POST", settings.endpoint, headers=headers, json=payload, timeout=settings.timeout_seconds
        ) as response:
            media_type = str(response.headers.get("content-type", "")).split(";", 1)[0].lower()
            if response.status_code != 200:
                raise TTSProviderError("http_status")
            if media_type not in {"audio/pcmu", "audio/basic"}:
                raise TTSProviderError("invalid_audio_metadata")
            emitted = False
            async for chunk in _bounded_provider_audio(
                response.aiter_bytes(chunk_size=640), settings.max_response_bytes
            ):
                if chunk:
                    emitted = True
                    yield chunk
            if not emitted:
                raise TTSProviderError("empty_audio")

    async def _stream_gemini(
        self, text: str, *, generation: int, generation_is_current: Callable[[int], bool]
    ) -> AsyncIterator[bytes]:
        settings = self._config.sinhala_gemini
        if settings is None or self._clients.gemini is None:
            raise TTSConfigurationError("Gemini startup client is unavailable")
        emitted = False
        for model in settings.models:
            try:
                pcm = await self._clients.gemini.stream_audio(
                    api_key=settings.api_key, model=model, text=text, voice=settings.voice, timeout=settings.timeout_seconds
                )
                async for frame in _frame_ulaw(
                    _pcm24k_to_ulaw(pcm), generation=generation, generation_is_current=generation_is_current
                ):
                    emitted = True
                    yield frame
                if not emitted:
                    raise TTSProviderError("empty_audio")
                return
            except TTSProviderError as error:
                if emitted or error.code not in {"quota_exceeded", "rate_limited"}:
                    raise
        raise TTSProviderError("quota_exceeded")


async def _pcm24k_to_ulaw(chunks: AsyncIterable[PCM24kChunk]) -> AsyncIterator[bytes]:
    """Convert only validated mono 24 kHz PCM16 into g711_ulaw/8 kHz bytes."""
    ratecv_state: Any = None
    pcm_tail = b""
    async for chunk in chunks:
        if chunk.mime_type != "audio/l16" or chunk.channels != 1 or chunk.sample_rate != 24_000:
            raise TTSProviderError("invalid_audio_metadata")
        data = pcm_tail + bytes(chunk.data)
        if len(data) % 2:
            data, pcm_tail = data[:-1], data[-1:]
        else:
            pcm_tail = b""
        if data:
            pcm8k, ratecv_state = audioop.ratecv(data, 2, 1, 24_000, SAMPLE_RATE_HZ, ratecv_state)
            yield audioop.lin2ulaw(pcm8k, 2)
    if pcm_tail:
        raise TTSProviderError("malformed_audio")


async def _empty_audio_stream() -> AsyncIterator[bytes]:
    if False:
        yield b""
