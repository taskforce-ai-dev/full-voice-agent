"""Fail-closed composition seam for separately approved provider lane classes."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


class ProviderCompositionError(RuntimeError):
    """Raised when a rendered provider profile cannot bind concrete lane classes."""


@dataclass(frozen=True)
class ProviderProfile:
    languages: Mapping[str, Mapping[str, str]]
    required_environment: tuple[str, ...]


def load_provider_profile(path: str) -> ProviderProfile:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not isinstance(raw.get("languages"), dict):
        raise ProviderCompositionError("provider profile is invalid")
    required = raw.get("required_environment", [])
    if not isinstance(required, list) or not all(isinstance(name, str) for name in required):
        raise ProviderCompositionError("provider profile environment is invalid")
    return ProviderProfile(languages=raw["languages"], required_environment=tuple(required))


def bind_provider_adapter(profile: ProviderProfile, environ: Mapping[str, str]):
    synthetic = (
        environ.get("SMARTPBX_RUNTIME_MODE") == "synthetic"
        and environ.get("SMARTPBX_ALLOW_SYNTHETIC_FOR_CI") == "1"
    )
    missing = [] if synthetic else [name for name in profile.required_environment if not environ.get(name)]
    if missing:
        raise ProviderCompositionError("missing provider configuration: " + ", ".join(missing))
    _validate_exact_provider_selection(profile)
    return _ComposedProviderAdapter(
        build_stt_adapter(profile, environ), build_llm_adapter(profile, environ), build_tts_adapter(profile, environ)
    )


def _validate_exact_provider_selection(profile: ProviderProfile) -> None:
    supported = {
        "stt": {"google", "azure"},
        "llm": {"claude", "gemini"},
        "tts": {"elevenlabs", "gemini", "rime"},
    }
    for language, lanes in profile.languages.items():
        if not isinstance(language, str) or not isinstance(lanes, Mapping):
            raise ProviderCompositionError("provider language profile is invalid")
        for lane, approved in supported.items():
            provider = lanes.get(lane)
            if provider not in approved:
                raise ProviderCompositionError(f"unsupported {lane} provider for {language}")


def build_stt_adapter(profile: ProviderProfile, environ: Mapping[str, str]):
    """Startup-only STT builder; it never performs a hot-path environment read."""
    from provider_builders import ProviderBuildError, build_stt_adapter as build
    try:
        return build(profile, environ)
    except ProviderBuildError as error:
        raise ProviderCompositionError(str(error)) from error


def build_llm_adapter(profile: ProviderProfile, environ: Mapping[str, str]):
    """Startup-only LLM builder; its result must expose stream_response()."""
    from provider_builders import ProviderBuildError, build_llm_adapter as build
    try:
        return build(profile, environ)
    except ProviderBuildError as error:
        raise ProviderCompositionError(str(error)) from error


def build_tts_adapter(profile: ProviderProfile, environ: Mapping[str, str]):
    """Startup-only TTS builder; its result must expose synthesize_audio()."""
    from provider_builders import ProviderBuildError, build_tts_adapter as build
    try:
        return build(profile, environ)
    except ProviderBuildError as error:
        raise ProviderCompositionError(str(error)) from error


class _ComposedProviderAdapter:
    def __init__(self, stt: object, llm: object, tts: object) -> None:
        self._stt, self._llm, self._tts = stt, llm, tts
        self.active = all(bool(getattr(adapter, "active", False)) for adapter in (stt, llm, tts))

    async def start_recognizer(self, language: str, on_result):
        return await self._stt.start_recognizer(language, on_result)

    async def generate_response(self, transcript: str, language: str, prompt: str):
        return self._llm.stream_response(transcript, language, prompt)

    async def generate_response_with_history(self, transcript: str, language: str, prompt: str, history):
        return self._llm.stream_response_with_history(transcript, language, prompt, history)

    async def synthesize_audio(self, response: str, language: str):
        return await self._tts.synthesize_audio(response, language)

    async def close(self) -> None:
        for adapter in (self._stt, self._llm, self._tts):
            close = getattr(adapter, "close", None)
            if callable(close):
                result = close()
                if hasattr(result, "__await__"):
                    await result
