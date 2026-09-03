"""Dialog SmartPBX's pre-STT English/Sinhala selection menu."""

from __future__ import annotations

import asyncio
import copy
import threading

import pytest

import server
import smartpbx_session
from smartpbx_protocol import CallContext, MediaFormat
from smartpbx_session import KavyaSmartPBXSession


class RecordingTransport:
    def __init__(self) -> None:
        self.clears = 0
        self.audio: list[bytes] = []
        self.marks: list[str] = []

    async def send_audio(self, audio: bytes) -> None:
        self.audio.append(audio)

    async def send_mark(self, name: str) -> None:
        self.marks.append(name)

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


@pytest.mark.parametrize("credential", ["", " \t\n "])
def test_native_gemini_client_rejects_blank_credential_before_sdk_init(
    monkeypatch, credential,
):
    sdk_calls: list[str] = []

    class _SDK:
        @staticmethod
        def Client(*, api_key):
            sdk_calls.append(api_key)
            return object()

    monkeypatch.setattr(server, "GEMINI_API_KEY", credential)
    monkeypatch.setattr(server, "GOOGLE_GENAI_AVAILABLE", True)
    monkeypatch.setattr(server, "google_genai", _SDK)
    monkeypatch.setattr(server, "_gemini_client", None)

    with pytest.raises(RuntimeError, match="GEMINI_API_KEY is not set"):
        server._get_gemini_client()

    assert sdk_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("credential", ["", " \t\n "])
async def test_blank_gemini_credential_ends_before_bilingual_menu_or_activation(
    monkeypatch, credential,
):
    """The preselection menu is unavailable unless its Sinhala TTS key is usable."""
    client_calls: list[str] = []
    session, pipeline, stt = make_session()
    pipeline.anthropic_client = None
    monkeypatch.setattr(server, "GEMINI_API_KEY", credential)
    monkeypatch.setattr(
        server,
        "_get_gemini_client",
        lambda: client_calls.append("gemini") or object(),
    )
    monkeypatch.setattr(
        server,
        "_get_anthropic_client",
        lambda: client_calls.append("claude") or object(),
    )

    await session.start()
    await asyncio.sleep(0)
    await session.feed_dtmf("2")

    assert pipeline.spoken == []
    assert session._language_menu_task is None
    assert session._language_timeout_handle is None
    assert stt.starts == 0
    assert pipeline._stt is None
    assert client_calls == []
    assert session.terminal_future.done()


@pytest.mark.asyncio
async def test_valid_gemini_credential_keeps_bilingual_menu_and_english_activation(
    monkeypatch,
):
    session, pipeline, stt = make_session()
    monkeypatch.setattr(server, "GEMINI_API_KEY", " valid-gemini-key ")

    await session.start()
    await asyncio.sleep(0)

    assert pipeline.spoken == []
    assert len(session._transport.audio) == 1
    assert session._transport.marks == ["language-menu"]
    assert await session.feed_dtmf("1") is True
    assert (pipeline.lang, stt.starts) == ("en", 1)


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
    assert server._resolve_smartpbx_sinhala_gemini_max_tokens(None) == 1024
    assert server._resolve_smartpbx_sinhala_gemini_max_tokens("") == 1024
    assert server._resolve_smartpbx_sinhala_gemini_max_tokens("invalid") == 1024
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
    assert pipeline.spoken == []
    assert len(session._transport.audio) == 1

    assert await session.feed_dtmf("#") is True
    await asyncio.sleep(0)
    assert session._transport.clears == 1
    assert len(session._transport.audio) == 2
    assert session._transport.audio[0] == session._transport.audio[1]
    assert stt.starts == 0

    assert await session.feed_dtmf("*") is True
    assert (pipeline.lang, stt.starts) == ("en", 1)


@pytest.mark.asyncio
async def test_sinhala_removes_only_human_transfer_while_english_keeps_every_tool(
    monkeypatch,
):
    sinhala_session, sinhala_pipeline, _sinhala_stt = make_session()
    # This direct session uses the configured Gemini profile, so provide the
    # native Gemini tool schema rather than relying on unrelated n8n env setup.
    sinhala_pipeline.gemini_client = object()
    monkeypatch.setattr(
        server,
        "get_tools_gemini",
        lambda: [{"function_declarations": [
            {"name": "transfer_to_human"},
            {"name": "check_availability"},
        ]}],
    )
    await sinhala_session.start()
    await sinhala_session.feed_dtmf("2")

    assert sinhala_pipeline.tools == [{"function_declarations": [
        {"name": "check_availability"},
    ]}]

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
    pipeline.gemini_client = object()
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


class _BlockingClearTransport(RecordingTransport):
    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def clear_audio(self) -> None:
        self.clears += 1
        self.entered.set()
        await self.release.wait()


def _controlled_session(transport=None, stt=None):
    pipeline = RecordingPipeline()
    transport = transport or RecordingTransport()
    stt = stt or RecordingStt()
    session = KavyaSmartPBXSession(
        _context(), transport, pipeline=pipeline,
        stt_factory=lambda **_kwargs: stt,
        post_call_processor=lambda **_kwargs: asyncio.sleep(0),
        welcome_text="", llm_provider="claude", model="test-model",
    )
    return session, pipeline, stt, transport


@pytest.mark.asyncio
async def test_digit_and_timeout_serialize_one_preflight_and_one_commit(monkeypatch):
    monkeypatch.setattr(server, "SMARTPBX_SINHALA_LLM_PROVIDER", "claude")
    session, _pipeline, stt, transport = _controlled_session()
    entered = asyncio.Event()
    release = asyncio.Event()
    calls: list[str] = []
    original = session._preflight_language_profile

    async def gated(pipeline, profile):
        calls.append(profile.lang)
        entered.set()
        await release.wait()
        return await original(pipeline, profile)

    monkeypatch.setattr(session, "_preflight_language_profile", gated)
    await session.start()
    digit = asyncio.create_task(session.feed_dtmf("2"))
    await entered.wait()
    timeout = asyncio.create_task(session._activate_language("en", "timeout"))
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(digit, timeout)

    assert calls == ["si"]
    assert stt.starts == 1
    assert transport.clears == 1
    assert session._selected_language == "si"


@pytest.mark.asyncio
async def test_finish_during_preflight_cleans_unstarted_candidate_without_orphan(monkeypatch):
    monkeypatch.setattr(server, "SMARTPBX_SINHALA_LLM_PROVIDER", "claude")
    session, pipeline, stt, transport = _controlled_session()
    entered = asyncio.Event()
    release = asyncio.Event()
    original = session._preflight_language_profile

    async def gated(candidate_pipeline, profile):
        entered.set()
        await release.wait()
        return await original(candidate_pipeline, profile)

    monkeypatch.setattr(session, "_preflight_language_profile", gated)
    await session.start()
    activation = asyncio.create_task(session.feed_dtmf("2"))
    await entered.wait()
    finish = asyncio.create_task(session.finish())
    await asyncio.sleep(0)
    assert session._finish_task is not None
    assert not finish.done()
    release.set()
    await asyncio.gather(activation, finish)

    assert stt.starts == 0
    assert stt.stops == 1
    assert pipeline._stt is None
    assert session._welcome_task is None
    assert transport.clears == 0


@pytest.mark.asyncio
async def test_finish_during_blocked_clear_cleans_candidate_and_releases_finish_lock(monkeypatch):
    monkeypatch.setattr(server, "SMARTPBX_SINHALA_LLM_PROVIDER", "claude")
    transport = _BlockingClearTransport()
    session, pipeline, stt, _ = _controlled_session(transport=transport)
    await session.start()
    activation = asyncio.create_task(session.feed_dtmf("2"))
    await transport.entered.wait()
    finish = asyncio.create_task(session.finish())
    await asyncio.sleep(0)
    assert session._finish_task is not None
    transport.release.set()
    await asyncio.wait_for(asyncio.gather(activation, finish), timeout=1)

    assert stt.starts == 0
    assert stt.stops == 1
    assert pipeline._stt is None
    assert session._welcome_task is None


@pytest.mark.asyncio
async def test_finish_during_cancelled_menu_wait_cleans_candidate_without_late_start(monkeypatch):
    monkeypatch.setattr(server, "SMARTPBX_SINHALA_LLM_PROVIDER", "claude")
    session, pipeline, stt, transport = _controlled_session()
    menu_cancelled = asyncio.Event()
    menu_release = asyncio.Event()

    async def stubborn_menu(_audio):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            menu_cancelled.set()
            await menu_release.wait()

    monkeypatch.setattr(transport, "send_audio", stubborn_menu)
    await session.start()
    await asyncio.sleep(0)
    activation = asyncio.create_task(session.feed_dtmf("2"))
    await asyncio.wait_for(menu_cancelled.wait(), timeout=1)
    finish = asyncio.create_task(session.finish())
    await asyncio.sleep(0)
    assert session._finish_task is not None
    menu_release.set()
    await asyncio.wait_for(asyncio.gather(activation, finish), timeout=1)

    assert stt.starts == 0
    assert stt.stops == 1
    assert pipeline._stt is None
    assert session._welcome_task is None
    assert transport.clears == 1


@pytest.mark.asyncio
async def test_selected_pipeline_stt_owns_audio_and_teardown(monkeypatch):
    monkeypatch.setattr(server, "SMARTPBX_SINHALA_LLM_PROVIDER", "claude")
    session, pipeline, stt, _transport = _controlled_session()
    await session.start()
    await session.feed_dtmf("2")

    await session.feed_audio(b"owned-audio")
    await session.finish()

    assert pipeline._stt is stt
    assert stt.audio == [b"owned-audio"]
    assert stt.stops == 1


@pytest.mark.asyncio
async def test_sync_stt_start_failure_detaches_candidate_and_terminates(monkeypatch):
    monkeypatch.setattr(server, "SMARTPBX_SINHALA_LLM_PROVIDER", "claude")

    class FailingStt(RecordingStt):
        def start(self) -> None:
            self.starts += 1
            raise RuntimeError("synthetic start failure")

    stt = FailingStt()
    session, pipeline, _stt, transport = _controlled_session(stt=stt)
    await session.start()
    await session.feed_dtmf("2")

    assert stt.starts == 1
    assert stt.stops == 1
    assert pipeline._stt is None
    assert session.terminal_future.done()
    assert session._welcome_task is None
    assert transport.clears == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("attribute", "value"),
    (("AZURE_STT_AVAILABLE", False), ("audioop", None), ("AZURE_SPEECH_KEY", "  ")),
)
async def test_sinhala_azure_preflight_failure_is_azure_not_llm_unavailable(
    monkeypatch, attribute, value,
):
    events = _capture_profile_events(monkeypatch)
    pipeline = RecordingPipeline()
    pipeline.gemini_client = object()
    transport = RecordingTransport()
    session = KavyaSmartPBXSession(
        _context(), transport, pipeline=pipeline, stt_factory=server._make_stt,
        post_call_processor=lambda **_kwargs: asyncio.sleep(0), welcome_text="",
        llm_provider="claude", model="test-model",
    )
    monkeypatch.setattr(server, "SMARTPBX_SINHALA_LLM_PROVIDER", "gemini")
    monkeypatch.setattr(server, "get_tools_gemini", lambda: [])
    monkeypatch.setattr(server, attribute, value)
    # The bilingual menu has completed: its temporary Sinhala routing cannot be
    # mistaken for a preflight mutation below.
    await session.start()
    await asyncio.sleep(0)
    timeout_handle = session._language_timeout_handle
    menu_task = session._language_menu_task
    before = {
        "selected": session._selected_language,
        "menu": menu_task,
        "menu_done": menu_task.done() if menu_task is not None else None,
        "speaking": pipeline._is_speaking,
        "generation": pipeline._speak_generation,
        "prompt": pipeline.system_prompt,
        "lang": pipeline.lang,
        "provider": pipeline.llm_provider,
        "model": pipeline.model,
        "anthropic": pipeline.anthropic_client,
        "gemini": pipeline.gemini_client,
        "stt": pipeline._stt,
        "welcome": session._welcome_task,
        "welcome_pending": pipeline._smartpbx_welcome_audio_pending,
    }
    before_tools = copy.deepcopy(pipeline.tools)

    await session.feed_dtmf("2")

    assert session._selected_language == before["selected"]
    assert session._language_timeout_handle is None
    assert timeout_handle is not None and timeout_handle.cancelled()
    assert session._language_menu_task is before["menu"]
    assert (menu_task.done() if menu_task is not None else None) == before["menu_done"]
    assert pipeline._is_speaking == before["speaking"]
    assert pipeline._speak_generation == before["generation"]
    assert pipeline.system_prompt == before["prompt"]
    assert pipeline.lang == before["lang"]
    assert pipeline.llm_provider == before["provider"]
    assert pipeline.model == before["model"]
    assert pipeline.anthropic_client is before["anthropic"]
    assert pipeline.gemini_client is before["gemini"]
    assert pipeline._stt is before["stt"]
    assert pipeline.tools == before_tools
    assert transport.clears == 0
    assert session._welcome_task is before["welcome"]
    assert pipeline._smartpbx_welcome_audio_pending == before["welcome_pending"]
    assert session.terminal_future.done()
    assert events == [
        "smartpbx_media event=language_profile_unavailable lang=si provider=azure"
    ]


@pytest.mark.asyncio
async def test_terminal_language_profile_failure_cancels_timeout_and_rejects_late_activation(monkeypatch):
    monkeypatch.setattr(server, "SMARTPBX_SINHALA_LLM_PROVIDER", "claude")
    english_stt = RecordingStt()

    def profile_factory(**kwargs):
        if kwargs["lang"] == "si":
            raise RuntimeError("synthetic Sinhala STT prerequisite failure")
        return english_stt

    pipeline = RecordingPipeline()
    transport = RecordingTransport()
    session = KavyaSmartPBXSession(
        _context(), transport, pipeline=pipeline, stt_factory=profile_factory,
        post_call_processor=lambda **_kwargs: asyncio.sleep(0), welcome_text="",
        llm_provider="claude", model="test-model",
    )
    await session.start()
    timeout_handle = session._language_timeout_handle

    await session.feed_dtmf("2")
    await session._activate_language("en", "timeout")

    assert session.terminal_future.done()
    assert session._language_timeout_handle is None
    assert timeout_handle is not None and timeout_handle.cancelled()
    assert session._selected_language is None
    assert english_stt.starts == 0
    assert pipeline._stt is None
    assert transport.clears == 0


@pytest.mark.asyncio
async def test_preflight_cleanup_stops_sync_stt_off_loop_and_finishes_once(monkeypatch):
    monkeypatch.setattr(server, "SMARTPBX_SINHALA_LLM_PROVIDER", "claude")

    class ThreadRecordingStt(RecordingStt):
        def __init__(self) -> None:
            super().__init__()
            self.stop_threads: list[int] = []

        def stop(self) -> None:
            self.stop_threads.append(threading.get_ident())
            super().stop()

    stt = ThreadRecordingStt()
    session, pipeline, _stt, _transport = _controlled_session(stt=stt)
    entered = asyncio.Event()
    release = asyncio.Event()
    original = session._preflight_language_profile

    async def gated(candidate_pipeline, profile):
        entered.set()
        await release.wait()
        return await original(candidate_pipeline, profile)

    monkeypatch.setattr(session, "_preflight_language_profile", gated)
    await session.start()
    activation = asyncio.create_task(session.feed_dtmf("2"))
    await entered.wait()
    finish = asyncio.create_task(session.finish())
    await asyncio.sleep(0)
    release.set()
    await asyncio.wait_for(asyncio.gather(activation, finish), timeout=1)

    assert stt.starts == 0
    assert stt.stops == 1
    assert stt.stop_threads != [threading.get_ident()]
    assert pipeline._stt is None


@pytest.mark.asyncio
async def test_invalid_sinhala_provider_is_closed_before_openai_or_state_mutation(monkeypatch):
    events = _capture_profile_events(monkeypatch)
    session, pipeline, stt, transport = _controlled_session()
    original_tools = copy.deepcopy(pipeline.tools)
    monkeypatch.setattr(server, "SMARTPBX_SINHALA_LLM_PROVIDER", "openai")
    monkeypatch.setattr(
        server, "_get_client",
        lambda: (_ for _ in ()).throw(AssertionError("invalid Sinhala provider reached OpenAI")),
    )

    await session.start()
    await asyncio.sleep(0)
    await session.feed_dtmf("2")

    assert stt.starts == 0
    assert session._selected_language is None
    assert session.terminal_future.done()
    assert pipeline.tools == original_tools
    assert pipeline._stt is None
    assert transport.clears == 0
    assert events == [
        "smartpbx_media event=language_profile_unavailable lang=si provider=invalid"
    ]


@pytest.mark.asyncio
async def test_fallback_claude_client_exhaustion_is_the_only_provider_none_case(monkeypatch):
    events = _capture_profile_events(monkeypatch)
    session, pipeline, stt, _transport = _controlled_session()
    pipeline.anthropic_client = None
    monkeypatch.setattr(server, "SMARTPBX_SINHALA_LLM_PROVIDER", "gemini")
    monkeypatch.setattr(server, "_get_gemini_client", _raise_runtime_error)
    monkeypatch.setattr(server, "_get_anthropic_client", _raise_runtime_error)

    await session.start()
    await session.feed_dtmf("2")

    assert stt.starts == 0
    assert events == [
        "smartpbx_media event=language_profile_unavailable lang=si provider=none"
    ]


@pytest.mark.asyncio
async def test_fallback_claude_tool_setup_failure_is_not_client_exhaustion(monkeypatch):
    events = _capture_profile_events(monkeypatch)
    session, pipeline, stt, _transport = _controlled_session()
    pipeline.llm_provider = "gemini"
    monkeypatch.setattr(server, "SMARTPBX_SINHALA_LLM_PROVIDER", "gemini")
    monkeypatch.setattr(server, "_get_gemini_client", _raise_runtime_error)
    monkeypatch.setattr(server, "get_tools", _raise_runtime_error)

    await session.start()
    await session.feed_dtmf("2")

    assert stt.starts == 0
    assert events == [
        "smartpbx_media event=language_profile_unavailable lang=si provider=claude"
    ]


@pytest.mark.asyncio
async def test_gemini_tool_setup_failure_falls_back_without_client_unavailable_reason(monkeypatch):
    events = _capture_profile_events(monkeypatch)
    session, pipeline, stt, _transport = _controlled_session()
    pipeline.gemini_client = object()
    monkeypatch.setattr(server, "SMARTPBX_SINHALA_LLM_PROVIDER", "gemini")
    monkeypatch.setattr(server, "get_tools_gemini", _raise_runtime_error)

    await session.start()
    await session.feed_dtmf("2")

    assert stt.starts == 1
    assert pipeline.llm_provider == "claude"
    assert events == [
        "smartpbx_media event=language_profile_fallback lang=si "
        "from=gemini to=claude reason=provider_setup_unavailable"
    ]


@pytest.mark.asyncio
async def test_timeout_english_profile_preserves_provider_clients_and_tool_copy(monkeypatch):
    session, pipeline, stt = make_session()
    original_tools = pipeline.tools
    expected_tools = copy.deepcopy(original_tools)
    expected = (pipeline.llm_provider, pipeline.model, pipeline.anthropic_client, pipeline.gemini_client)
    monkeypatch.setattr(server, "SMARTPBX_LANGUAGE_SELECTION_TIMEOUT_SECONDS", 0)

    await session.start()
    await asyncio.wait_for(_wait_for_stt_start(stt), timeout=1)

    assert (pipeline.llm_provider, pipeline.model, pipeline.anthropic_client, pipeline.gemini_client) == expected
    assert pipeline.tools == expected_tools
    assert pipeline.tools is not original_tools
    assert pipeline.tools[0] is not original_tools[0]


@pytest.mark.asyncio
async def test_second_invalid_english_profile_preserves_provider_clients_and_tool_copy():
    session, pipeline, stt = make_session()
    original_tools = pipeline.tools
    expected_tools = copy.deepcopy(original_tools)
    expected = (pipeline.llm_provider, pipeline.model, pipeline.anthropic_client, pipeline.gemini_client)

    await session.start()
    await session.feed_dtmf("#")
    await session.feed_dtmf("*")

    assert stt.starts == 1
    assert (pipeline.llm_provider, pipeline.model, pipeline.anthropic_client, pipeline.gemini_client) == expected
    assert pipeline.tools == expected_tools
    assert pipeline.tools is not original_tools
    assert pipeline.tools[0] is not original_tools[0]
