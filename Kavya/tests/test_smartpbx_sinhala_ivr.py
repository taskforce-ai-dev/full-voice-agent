"""Dialog SmartPBX's pre-STT English/Sinhala selection menu."""

from __future__ import annotations

import asyncio

import pytest

import server
from smartpbx_protocol import CallContext, MediaFormat
from smartpbx_session import KavyaSmartPBXSession


class RecordingTransport:
    def __init__(self) -> None:
        self.clears = 0

    async def clear_audio(self) -> None:
        self.clears += 1


class RecordingStt:
    def __init__(self) -> None:
        self.starts = 0
        self.stops = 0
        self.audio: list[bytes] = []

    def start(self) -> None:
        self.starts += 1

    def stop(self) -> None:
        self.stops += 1

    def feed(self, audio: bytes) -> None:
        self.audio.append(audio)


class RecordingPipeline:
    def __init__(self) -> None:
        self.lang = "en"
        self.system_prompt = ""
        self.tools = [{"name": "transfer_to_human"}, {"name": "check_availability"}]
        self._stt = None
        self._is_speaking = False
        self._speak_generation = 0
        self._endpointing_handle = None
        self._smartpbx_transfer_context = None
        self._smartpbx_welcome_audio_pending = None
        self._event_loop = None
        self._dtmf_collector = None
        self.transfer_pending = False
        self.full_transcript = [{"role": "user", "text": "test"}]
        self.anthropic_client = None
        self.client = None
        self.gemini_client = None
        self.spoken: list[tuple[str, str]] = []
        self.dtmf: list[str] = []
        self.consume_dtmf = False

    def _on_stt_result(self, _text: str) -> None:
        return None

    def _on_stt_interim(self, _text: str) -> None:
        return None

    async def _speak(self, text: str) -> None:
        self.spoken.append((self.lang, text))

    async def feed_dtmf(self, digit: str) -> bool:
        self.dtmf.append(digit)
        return self.consume_dtmf

    def _cancel_reprompt(self) -> None:
        return None

    def _write_audio_dump(self) -> None:
        return None


def _context() -> CallContext:
    return CallContext(
        call_id="media-leg",
        other_leg_call_id="dialog-call",
        caller_id_number="+94000000000",
        callee_id_number="+94110000000",
        account_id="dialog-account",
        media_format=MediaFormat(encoding="g711_ulaw", sample_rate=8000),
    )


def make_session():
    pipeline = RecordingPipeline()
    stt = RecordingStt()
    session = KavyaSmartPBXSession(
        _context(),
        RecordingTransport(),
        pipeline=pipeline,
        stt_factory=lambda **_kwargs: stt,
        post_call_processor=lambda **_kwargs: asyncio.sleep(0),
        welcome_text="",
        llm_provider="claude",
        model="test-model",
    )
    return session, pipeline, stt


@pytest.mark.asyncio
async def test_digit_two_starts_sinhala_stt_once_and_consumes_only_the_menu_digit():
    session, pipeline, stt = make_session()
    await session.start()
    assert stt.starts == 0

    assert await session.feed_dtmf("2") is True
    assert stt.starts == 1
    assert pipeline.lang == "si"
    assert pipeline.system_prompt == server._build_system_prompt("si")
    assert await session.feed_dtmf("7") is False


@pytest.mark.asyncio
async def test_digit_one_and_timeout_start_english_once(monkeypatch):
    session, pipeline, stt = make_session()
    await session.start()
    assert await session.feed_dtmf("1") is True
    assert (pipeline.lang, stt.starts) == ("en", 1)

    timeout_session, timeout_pipeline, timeout_stt = make_session()
    monkeypatch.setattr(server, "SMARTPBX_LANGUAGE_SELECTION_TIMEOUT_SECONDS", 0)
    await timeout_session.start()
    await asyncio.sleep(0)
    assert (timeout_pipeline.lang, timeout_stt.starts) == ("en", 1)


@pytest.mark.asyncio
async def test_first_invalid_digit_replays_menu_and_second_defaults_to_english():
    session, pipeline, stt = make_session()
    await session.start()
    await asyncio.sleep(0)
    first_menu = list(pipeline.spoken)

    assert await session.feed_dtmf("#") is True
    await asyncio.sleep(0)
    assert len(pipeline.spoken) == len(first_menu) * 2
    assert stt.starts == 0

    assert await session.feed_dtmf("*") is True
    assert (pipeline.lang, stt.starts) == ("en", 1)


@pytest.mark.asyncio
async def test_audio_before_selection_never_reaches_stt_feed():
    session, _pipeline, stt = make_session()
    await session.start()

    await session.feed_audio(b"pre-selection")
    assert stt.audio == []


@pytest.mark.asyncio
async def test_selected_sinhala_uses_sinhala_welcome_and_post_call_language():
    post_calls: list[dict[str, object]] = []
    pipeline = RecordingPipeline()
    stt = RecordingStt()

    async def post_call(**metadata):
        post_calls.append(metadata)

    session = KavyaSmartPBXSession(
        _context(), RecordingTransport(), pipeline=pipeline,
        stt_factory=lambda **_kwargs: stt, post_call_processor=post_call,
        welcome_text=None, llm_provider="claude", model="test-model",
    )
    await session.start()
    await session.feed_dtmf("2")
    await asyncio.sleep(0)
    await session.finish(True)
    await asyncio.sleep(0)

    assert ("si", server.LANGUAGE_CONFIGS["si"]["welcome_greeting"]) in pipeline.spoken
    assert post_calls[0]["lang"] == "si"


@pytest.mark.asyncio
async def test_post_selection_dtmf_reaches_the_active_collector_unchanged():
    session, pipeline, _stt = make_session()
    await session.start()
    await session.feed_dtmf("1")
    pipeline.consume_dtmf = True

    assert await session.feed_dtmf("7") is True
    assert pipeline.dtmf == ["7"]
