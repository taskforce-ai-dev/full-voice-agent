"""Dialog SmartPBX's pre-STT English/Sinhala selection menu."""

from __future__ import annotations

import asyncio
import copy

import pytest

import server
import smartpbx_session
from smartpbx_protocol import CallContext, MediaFormat
from smartpbx_session import KavyaSmartPBXSession


class RecordingTransport:
    def __init__(self) -> None:
        self.clears = 0

    async def clear_audio(self) -> None:
        self.clears += 1


class RecordingStt:
    def __init__(self, snapshot_factory=None) -> None:
        self.starts = 0
        self.stops = 0
        self.audio: list[bytes] = []
        self.snapshot_factory = snapshot_factory
        self.profile_at_start = None

    def start(self) -> None:
        self.starts += 1
        if self.snapshot_factory is not None:
            self.profile_at_start = self.snapshot_factory()

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
        self.llm_provider = "claude"
        self.model = "test-model"
        self._gemini_thinking_level = "global-low"
        self._smartpbx_gemini_max_tokens = 120
        self.anthropic_client = object()
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
    stt = RecordingStt(lambda: {
        "lang": pipeline.lang,
        "llm_provider": pipeline.llm_provider,
        "model": pipeline.model,
        "tools": copy.deepcopy(pipeline.tools),
        "thinking_level": pipeline._gemini_thinking_level,
        "max_tokens": pipeline._smartpbx_gemini_max_tokens,
    })
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


def test_sinhala_llm_provider_defaults_to_gemini_and_invalid_values_fail_to_claude():
    assert server._resolve_smartpbx_sinhala_llm_provider(None) == "gemini"
    assert server._resolve_smartpbx_sinhala_llm_provider("") == "gemini"
    assert server._resolve_smartpbx_sinhala_llm_provider(" GEMINI ") == "gemini"
    assert server._resolve_smartpbx_sinhala_llm_provider("claude") == "claude"
    assert server._resolve_smartpbx_sinhala_llm_provider("openai") == "claude"


def test_sinhala_gemini_thinking_level_is_closed_and_latency_safe():
    assert server._resolve_smartpbx_sinhala_gemini_thinking_level(None) == "low"
    assert server._resolve_smartpbx_sinhala_gemini_thinking_level("") == "low"
    assert server._resolve_smartpbx_sinhala_gemini_thinking_level("medium") == "medium"
    assert server._resolve_smartpbx_sinhala_gemini_thinking_level("HIGH") == "high"
    assert server._resolve_smartpbx_sinhala_gemini_thinking_level("minimal") == "low"


def test_sinhala_gemini_output_budget_defaults_and_clamps():
    assert server._resolve_smartpbx_sinhala_gemini_max_tokens(None) == 600
    assert server._resolve_smartpbx_sinhala_gemini_max_tokens("") == 600
    assert server._resolve_smartpbx_sinhala_gemini_max_tokens("invalid") == 600
    assert server._resolve_smartpbx_sinhala_gemini_max_tokens("199") == 200
    assert server._resolve_smartpbx_sinhala_gemini_max_tokens("600") == 600
    assert server._resolve_smartpbx_sinhala_gemini_max_tokens("1025") == 1024


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
    await asyncio.wait_for(_wait_for_stt_start(timeout_stt), timeout=1)
    assert (timeout_pipeline.lang, timeout_stt.starts) == ("en", 1)


async def _wait_for_stt_start(stt: RecordingStt) -> None:
    while stt.starts != 1:
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_first_invalid_digit_replays_menu_and_second_defaults_to_english():
    session, pipeline, stt = make_session()
    await session.start()
    await asyncio.sleep(0)
    first_menu = list(pipeline.spoken)
    assert [lang for lang, _text in first_menu] == ["en", "si"]

    assert await session.feed_dtmf("#") is True
    await asyncio.sleep(0)
    replayed_menu = pipeline.spoken[len(first_menu):]
    assert [lang for lang, _text in replayed_menu] == ["en", "si"]
    assert stt.starts == 0

    assert await session.feed_dtmf("*") is True
    assert (pipeline.lang, stt.starts) == ("en", 1)


@pytest.mark.asyncio
async def test_sinhala_removes_only_human_transfer_while_english_keeps_every_tool():
    sinhala_session, sinhala_pipeline, _sinhala_stt = make_session()
    original_sinhala_tools = list(sinhala_pipeline.tools)
    await sinhala_session.start()
    await sinhala_session.feed_dtmf("2")

    assert sinhala_pipeline.tools == [
        tool for tool in original_sinhala_tools
        if tool["name"] != "transfer_to_human"
    ]

    english_session, english_pipeline, _english_stt = make_session()
    original_english_tools = list(english_pipeline.tools)
    await english_session.start()
    await english_session.feed_dtmf("1")

    assert english_pipeline.tools == original_english_tools


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
    assert post_calls[0]["llm_provider"] == "gemini"
    assert post_calls[0]["model"] == "gemini-3.7-flash"


@pytest.mark.asyncio
async def test_post_selection_dtmf_reaches_the_active_collector_unchanged():
    session, pipeline, _stt = make_session()
    await session.start()
    await session.feed_dtmf("1")
    pipeline.consume_dtmf = True

    assert await session.feed_dtmf("7") is True
    assert pipeline.dtmf == ["7"]


@pytest.mark.asyncio
async def test_digit_two_applies_gemini_profile_before_sinhala_stt(monkeypatch):
    gemini_tools = [{
        "function_declarations": [
            {"name": "transfer_to_human"},
            {"name": "check_availability"},
            {"name": "create_booking"},
        ]
    }]
    gemini_client = object()
    monkeypatch.setattr(server, "SMARTPBX_SINHALA_LLM_PROVIDER", "gemini")
    monkeypatch.setattr(server, "SMARTPBX_SINHALA_GEMINI_LLM_MODEL", "gemini-3.7-flash")
    monkeypatch.setattr(server, "SMARTPBX_SINHALA_GEMINI_THINKING_LEVEL", "low")
    monkeypatch.setattr(server, "SMARTPBX_SINHALA_GEMINI_MAX_TOKENS", 600)
    monkeypatch.setattr(server, "get_tools_gemini", lambda: gemini_tools)
    monkeypatch.setattr(server, "_get_gemini_client", lambda: gemini_client)

    session, pipeline, stt = make_session()
    await session.start()
    await session.feed_dtmf("2")

    assert stt.starts == 1
    assert pipeline.llm_provider == "gemini"
    assert pipeline.gemini_client is gemini_client
    assert pipeline.model == "gemini-3.7-flash"
    assert pipeline._gemini_thinking_level == "low"
    assert pipeline._smartpbx_gemini_max_tokens == 600
    assert pipeline.tools == [{
        "function_declarations": [
            {"name": "check_availability"},
            {"name": "create_booking"},
        ]
    }]
    assert stt.profile_at_start == {
        "lang": "si",
        "llm_provider": "gemini",
        "model": "gemini-3.7-flash",
        "tools": pipeline.tools,
        "thinking_level": "low",
        "max_tokens": 600,
    }


@pytest.mark.asyncio
async def test_english_selection_keeps_existing_provider_model_tools_and_clients(monkeypatch):
    session, pipeline, _stt = make_session()
    monkeypatch.setattr(
        server,
        "load_kavya_english_voice_profile",
        lambda: (_ for _ in ()).throw(AssertionError("IVR must not load TTS secrets")),
    )
    original_tools = pipeline.tools
    expected_tools = copy.deepcopy(original_tools)
    expected = (
        pipeline.llm_provider,
        pipeline.model,
        pipeline.anthropic_client,
        pipeline.gemini_client,
    )
    monkeypatch.setattr(
        server,
        "get_tools_gemini",
        lambda: (_ for _ in ()).throw(AssertionError("English must not rebuild Gemini tools")),
    )

    await session.start()
    await session.feed_dtmf("1")

    assert (
        pipeline.llm_provider,
        pipeline.model,
        pipeline.anthropic_client,
        pipeline.gemini_client,
    ) == expected
    assert pipeline.tools == expected_tools
    assert pipeline.tools is not original_tools
    assert pipeline.tools[0] is not original_tools[0]
    assert session._resolve_language_profile("en").lang == "en"


@pytest.mark.asyncio
async def test_concurrent_english_and_sinhala_profiles_never_cross_mutate(monkeypatch):
    gemini_client = object()
    monkeypatch.setattr(server, "SMARTPBX_SINHALA_LLM_PROVIDER", "gemini")
    monkeypatch.setattr(server, "SMARTPBX_SINHALA_GEMINI_LLM_MODEL", "gemini-3.7-flash")
    monkeypatch.setattr(server, "SMARTPBX_SINHALA_GEMINI_THINKING_LEVEL", "low")
    monkeypatch.setattr(server, "SMARTPBX_SINHALA_GEMINI_MAX_TOKENS", 600)
    monkeypatch.setattr(
        server,
        "get_tools_gemini",
        lambda: [{"function_declarations": [{"name": "check_availability"}]}],
    )
    monkeypatch.setattr(server, "_get_gemini_client", lambda: gemini_client)
    english, english_pipeline, english_stt = make_session()
    sinhala, sinhala_pipeline, sinhala_stt = make_session()

    await asyncio.gather(english.start(), sinhala.start())
    await asyncio.gather(english.feed_dtmf("1"), sinhala.feed_dtmf("2"))

    assert (english_pipeline.lang, english_pipeline.llm_provider, english_pipeline.model) == (
        "en", "claude", "test-model"
    )
    assert (sinhala_pipeline.lang, sinhala_pipeline.llm_provider, sinhala_pipeline.model) == (
        "si", "gemini", "gemini-3.7-flash"
    )
    assert english_stt.profile_at_start["llm_provider"] == "claude"
    assert sinhala_stt.profile_at_start["llm_provider"] == "gemini"
    assert english_pipeline.tools is not sinhala_pipeline.tools
    sinhala_pipeline.tools[0]["function_declarations"][0]["name"] = "changed"
    assert english_pipeline.tools == [{"name": "transfer_to_human"}, {"name": "check_availability"}]


@pytest.mark.asyncio
async def test_sinhala_claude_rollback_profile_stays_transfer_free(monkeypatch):
    monkeypatch.setattr(server, "SMARTPBX_SINHALA_LLM_PROVIDER", "claude")
    claude_client = object()
    monkeypatch.setattr(server, "_get_anthropic_client", lambda: claude_client)
    session, pipeline, stt = make_session()
    pipeline.anthropic_client = None

    await session.start()
    await session.feed_dtmf("2")

    assert stt.starts == 1
    assert pipeline.llm_provider == "claude"
    assert pipeline.model == server.CLAUDE_MODEL
    assert pipeline.anthropic_client is claude_client
    assert all("function_declarations" not in tool for tool in pipeline.tools)
    assert "transfer_to_human" not in {tool["name"] for tool in pipeline.tools}


def test_without_transfer_tool_keeps_provider_native_tool_shapes():
    filter_tools = getattr(smartpbx_session, "_without_transfer_tool")
    assert filter_tools(
        [{"function_declarations": [{"name": "transfer_to_human"}]}], "gemini"
    ) == []
    assert filter_tools([{
        "function_declarations": [
            {"name": "transfer_to_human"}, {"name": "check_availability"},
        ]
    }], "gemini") == [{
        "function_declarations": [{"name": "check_availability"}],
    }]


def _raise_runtime_error():
    raise RuntimeError("synthetic client failure")


def _capture_profile_events(monkeypatch):
    events: list[str] = []

    def capture(message, *args, **_kwargs):
        rendered = message % args if args else message
        if "event=language_profile_" in rendered:
            events.append(rendered)

    monkeypatch.setattr(smartpbx_session.logger, "warning", capture)
    return events


@pytest.mark.asyncio
async def test_sinhala_gemini_client_init_failure_falls_back_before_stt(monkeypatch):
    events = _capture_profile_events(monkeypatch)
    session, pipeline, stt = make_session()
    claude_client = pipeline.anthropic_client
    monkeypatch.setattr(server, "_get_gemini_client", _raise_runtime_error)

    await session.start()
    await session.feed_dtmf("2")

    assert stt.starts == 1
    assert pipeline.anthropic_client is claude_client
    assert stt.profile_at_start["llm_provider"] == "claude"
    assert stt.profile_at_start["model"] == server.CLAUDE_MODEL
    assert all("function_declarations" not in tool for tool in stt.profile_at_start["tools"])
    assert "transfer_to_human" not in {tool["name"] for tool in stt.profile_at_start["tools"]}
    assert events == [
        "smartpbx_media event=language_profile_fallback lang=si from=gemini to=claude reason=client_unavailable"
    ]


@pytest.mark.asyncio
async def test_sinhala_activation_fails_closed_when_no_llm_client_is_usable(monkeypatch):
    events = _capture_profile_events(monkeypatch)
    session, pipeline, stt = make_session()
    pipeline.anthropic_client = None
    monkeypatch.setattr(server, "_get_gemini_client", _raise_runtime_error)
    monkeypatch.setattr(server, "_get_anthropic_client", _raise_runtime_error)

    await session.start()
    await session.feed_dtmf("2")

    assert stt.starts == 0
    assert session.terminal_future.done()
    assert session._welcome_task is None
    assert events == [
        "smartpbx_media event=language_profile_unavailable lang=si provider=none"
    ]
