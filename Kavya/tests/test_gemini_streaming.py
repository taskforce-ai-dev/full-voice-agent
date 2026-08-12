"""Streaming parity for the native Gemini LLM path.

The Gemini runners used to reach TTS only once the whole response had been
drained, which is why a Gemini evaluation measured dead air rather than model
latency: the first spoken word waited for the last token. Worse, a turn whose
output budget was consumed by thinking finished with no text and no tool call
and the agent simply said nothing.

These tests pin the four behaviours that made the path usable, at the seams a
caller can actually hear:
  (a) each completed sentence reaches TTS while the stream is still arriving,
  (b) tool rounds loop and the pre-tool filler still fires,
  (c) a fully empty stream is retried once and then speaks a fallback line,
  (d) a barge-in mid-stream stops further TTS from being scheduled.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

import server


# --- fake native-SDK stream ------------------------------------------------

def _part(*, text=None, function_call=None, thought=False, thought_signature=None):
    return SimpleNamespace(
        text=text,
        function_call=function_call,
        thought=thought,
        thought_signature=thought_signature,
    )


def _chunk(*parts, finish_reason=None):
    return SimpleNamespace(
        candidates=[
            SimpleNamespace(
                finish_reason=finish_reason,
                content=SimpleNamespace(parts=list(parts)),
            )
        ]
    )


def _text_chunk(text: str):
    return _chunk(_part(text=text))


def _tool_chunk(name: str, args: dict | None = None, *, thought_signature=None):
    return _chunk(
        _part(
            function_call=SimpleNamespace(name=name, args=args or {}),
            thought_signature=thought_signature,
        )
    )


def _empty_chunk(finish_reason="MAX_TOKENS"):
    """What a thinking-budget blowout actually looks like: a finish and no parts."""
    return _chunk(finish_reason=finish_reason)


class FakeGeminiModels:
    def __init__(self, owner):
        self.owner = owner

    async def generate_content_stream(self, **kwargs):
        self.owner.requests += 1
        self.owner.configs.append(kwargs.get("config"))
        self.owner.contents.append(kwargs.get("contents"))
        chunks = self.owner.rounds.pop(0)

        async def stream():
            for index, chunk in enumerate(chunks):
                self.owner.timeline.append(("chunk", index))
                yield chunk

        return stream()


class FakeGemini:
    def __init__(self, rounds, timeline=None):
        self.rounds = list(rounds)
        self.requests = 0
        self.configs: list[dict] = []
        self.contents: list[list] = []
        self.timeline: list[tuple] = timeline if timeline is not None else []
        self.aio = SimpleNamespace(models=FakeGeminiModels(self))


class FakeFlakyGemini:
    """Gemini mock where a turn can raise an exception or return chunks."""

    def __init__(self, turns):
        self._turns = list(turns)
        self.requests = 0
        self.configs: list[dict] = []
        self.contents: list[list] = []
        self.timeline: list[tuple] = []
        self.aio = SimpleNamespace(models=self)

    async def generate_content_stream(self, **kwargs):
        self.requests += 1
        self.configs.append(kwargs.get("config"))
        self.contents.append(kwargs.get("contents"))

        behavior = self._turns.pop(0)
        if isinstance(behavior, BaseException):
            raise behavior

        turn = SimpleNamespace(chunks=behavior)
        turn_index = self.requests - 1

        async def stream():
            for index, chunk in enumerate(turn.chunks):
                self.timeline.append(("chunk", turn_index, index))
                yield chunk

        return stream()


class _QuotaError(Exception):
    def __init__(self, message: str = "quota exceeded", status: int = 429):
        super().__init__(message)
        self.status = status


class FakeTransport:
    """Stand-in for SmartPBXMediaTransport (present only on the direct path)."""

    def __init__(self):
        self.audio: list[bytes] = []
        self.clears = 0
        self.marks: list[str] = []

    async def send_audio(self, audio: bytes) -> None:
        self.audio.append(audio)

    async def clear_audio(self) -> None:
        self.clears += 1

    async def send_mark(self, name: str) -> None:
        self.marks.append(name)


def _session(rounds, *, lang="en", smartpbx=False, timeline=None, model="gemini-2.5-flash"):
    """A Gemini MediaStreamSession with TTS captured instead of synthesised."""
    session = server.MediaStreamSession(
        websocket=None,
        lang=lang,
        llm_provider="gemini",
        model=model,
        media_transport=FakeTransport() if smartpbx else None,
    )
    session.tools = []
    session.gemini_client = FakeGemini(rounds, timeline=timeline)
    if smartpbx:
        session._smartpbx_transfer_context = object()

    spoken: list[str] = []

    async def tts(text, **_kwargs):
        session.gemini_client.timeline.append(("tts", text))
        spoken.append(text)

    session._tts_elevenlabs = tts
    session._tts_openai = tts
    return session, spoken


# --- (a) sentences reach TTS incrementally --------------------------------

def test_each_sentence_reaches_tts_while_the_stream_is_still_arriving():
    timeline: list[tuple] = []
    session, spoken = _session(
        [[
            _text_chunk("Good morning. "),
            _text_chunk("We have two suites free. "),
            _text_chunk("Shall I hold one?"),
        ]],
        timeline=timeline,
    )

    result = asyncio.run(session._run_llm_gemini())

    assert spoken == [
        "Good morning.",
        "We have two suites free.",
        "Shall I hold one?",
    ]
    assert result == "Good morning. We have two suites free. Shall I hold one?"

    # The load-bearing assertion: speech started before the stream finished.
    # Buffering the whole response first is exactly the regression this path had.
    first_tts = next(i for i, entry in enumerate(timeline) if entry[0] == "tts")
    last_chunk = max(i for i, entry in enumerate(timeline) if entry[0] == "chunk")
    assert first_tts < last_chunk, (
        f"TTS must be scheduled mid-stream, not after the last chunk: {timeline}"
    )


def test_thought_parts_are_never_spoken():
    session, spoken = _session(
        [[
            _chunk(_part(text="The caller wants a rate. I should check.", thought=True)),
            _text_chunk("Rooms start at seven hundred dollars."),
        ]]
    )

    result = asyncio.run(session._run_llm_gemini())

    assert spoken == ["Rooms start at seven hundred dollars."]
    assert "should check" not in result


# --- (b) tool rounds ------------------------------------------------------

def test_tool_round_loops_and_the_filler_covers_the_tool_latency(monkeypatch):
    session, spoken = _session(
        [
            [_tool_chunk("check_availability", {"nights": 2})],
            [_text_chunk("Two suites are free.")],
        ],
        smartpbx=True,
    )
    executed: list[tuple[str, dict]] = []

    async def execute(name, arguments):
        executed.append((name, arguments))
        return json.dumps({"status": "ok"})

    monkeypatch.setattr(server, "execute_tool", execute)

    result = asyncio.run(session._run_llm_gemini())

    assert executed == [("check_availability", {"nights": 2})]
    assert session.gemini_client.requests == 2
    assert spoken[0] == server.TOOL_FILLERS["check_availability"]
    assert spoken[-1] == "Two suites are free."
    assert result.endswith("Two suites are free.")


def test_the_twilio_media_streams_path_still_gets_its_language_filler(monkeypatch):
    """Sinhala rides the same runner and takes fillers from MEDIA_STREAM_FILLERS."""
    session, spoken = _session(
        [
            [_tool_chunk("check_availability", {})],
            [_text_chunk("ඔව්.")],
        ],
        lang="si",
    )

    async def execute(_name, _arguments):
        return json.dumps({"status": "ok"})

    monkeypatch.setattr(server, "execute_tool", execute)

    asyncio.run(session._run_llm_gemini())

    expected = server.MEDIA_STREAM_FILLERS["si"]["check_availability"]
    assert spoken[0] == expected


def test_tool_rounds_are_bounded_by_max_tool_rounds(monkeypatch):
    session, _spoken = _session(
        [[_tool_chunk("check_availability")] for _ in range(server.MAX_TOOL_ROUNDS)],
        smartpbx=True,
    )

    async def execute(_name, _arguments):
        return json.dumps({"status": "ok"})

    monkeypatch.setattr(server, "execute_tool", execute)

    asyncio.run(session._run_llm_gemini())

    assert session.gemini_client.requests == server.MAX_TOOL_ROUNDS


def test_streamed_thought_signature_is_echoed_back_to_gemini(monkeypatch):
    """Gemini 3.x rejects the follow-up round if the signature is not returned."""
    session, _spoken = _session(
        [
            [_tool_chunk("check_availability", {}, thought_signature=b"sig-123")],
            [_text_chunk("All set.")],
        ],
        smartpbx=True,
    )

    async def execute(_name, _arguments):
        return json.dumps({"status": "ok"})

    monkeypatch.setattr(server, "execute_tool", execute)

    asyncio.run(session._run_llm_gemini())

    followup_contents = session.gemini_client.contents[1]
    model_parts = [
        part
        for content in followup_contents
        if content["role"] == "model"
        for part in content["parts"]
    ]
    signatures = [p.get("thought_signature") for p in model_parts if "function_call" in p]
    assert signatures == [b"sig-123"]


# --- (c) empty responses --------------------------------------------------

def test_empty_stream_retries_once_with_a_nudge_then_speaks_a_fallback():
    session, spoken = _session([[_empty_chunk()], [_empty_chunk()]])

    result = asyncio.run(session._run_llm_gemini())

    assert session.gemini_client.requests == 2, "an empty turn must be retried once"
    first, second = session.gemini_client.configs
    assert server.GEMINI_EMPTY_RETRY_NUDGE not in first["system_instruction"]
    assert server.GEMINI_EMPTY_RETRY_NUDGE in second["system_instruction"]

    fallback = server.LLM_EMPTY_FALLBACKS["en"]
    assert spoken == [fallback], "silence is not an acceptable outcome"
    assert result == fallback
    assert session.history[-1] == {"role": "assistant", "content": fallback}
    # The nudge is a system-side prod: it must never surface to the caller.
    assert all(
        server.GEMINI_EMPTY_RETRY_NUDGE not in str(msg) for msg in session.history
    )


def test_empty_stream_is_retried_only_once_per_turn():
    session, spoken = _session([[_empty_chunk()], [_empty_chunk()], [_empty_chunk()]])

    asyncio.run(session._run_llm_gemini())

    assert session.gemini_client.requests == 2
    assert spoken == [server.LLM_EMPTY_FALLBACKS["en"]]


def test_a_successful_retry_speaks_the_model_not_the_fallback():
    session, spoken = _session([[_empty_chunk()], [_text_chunk("Sorry, I mean yes.")]])

    result = asyncio.run(session._run_llm_gemini())

    assert session.gemini_client.requests == 2
    assert spoken == ["Sorry, I mean yes."]
    assert result == "Sorry, I mean yes."
    assert server.LLM_EMPTY_FALLBACKS["en"] not in spoken


def test_a_safety_blocked_turn_also_speaks_the_fallback():
    session, spoken = _session(
        [[_empty_chunk(finish_reason="SAFETY")], [_empty_chunk(finish_reason="SAFETY")]]
    )

    asyncio.run(session._run_llm_gemini())

    assert spoken == [server.LLM_EMPTY_FALLBACKS["en"]]


@pytest.mark.parametrize("lang", ["si", "ta", "ar"])
def test_the_fallback_is_language_specific(lang):
    """The Sinhala/Twilio Media Streams path shares this runner."""
    session, spoken = _session([[_empty_chunk()], [_empty_chunk()]], lang=lang)

    asyncio.run(session._run_llm_gemini())

    assert spoken == [server.LLM_EMPTY_FALLBACKS[lang]]


def test_an_empty_turn_logs_a_diagnostic(caplog):
    session, _spoken = _session([[_empty_chunk()], [_empty_chunk()]])

    with caplog.at_level("WARNING", logger="server"):
        asyncio.run(session._run_llm_gemini())

    empties = [r for r in caplog.records if "event=empty_response" in r.getMessage()]
    assert empties, "an inaudible failure must leave a greppable trace"
    assert any("retrying=true" in r.getMessage() for r in empties)
    assert any("event=empty_response_fallback" in r.getMessage() for r in caplog.records)


# --- (d) barge-in ---------------------------------------------------------

def test_bargein_mid_stream_stops_scheduling_further_tts():
    session, spoken = _session([[
        _text_chunk("First sentence here. "),
        _text_chunk("Second sentence here. "),
        _text_chunk("Third sentence here."),
    ]])

    original = session._tts_elevenlabs

    async def tts_then_barge_in(text, **kwargs):
        await original(text, **kwargs)
        # The caller talks over her right after the first sentence.
        session._speak_generation += 1

    session._tts_elevenlabs = tts_then_barge_in

    asyncio.run(session._run_llm_gemini())

    assert spoken == ["First sentence here."], (
        "no sentence may be scheduled after the barge-in bumped the generation"
    )


def test_stale_generation_sentences_are_dropped_by_the_speak_fence():
    session, spoken = _session([[_text_chunk("Hello there. "), _text_chunk("Bye.")]])
    session._speak_generation = 7

    async def scenario():
        # A barge-in that lands before the round starts speaking at all.
        session._speak_generation = 8
        await session._speak("stale", generation=7, sentence="stale")

    asyncio.run(scenario())
    assert spoken == []


# --- thinking controls ----------------------------------------------------

def test_thinking_is_disabled_for_a_25_voice_turn():
    session, _spoken = _session([[_text_chunk("Hi.")]], model="gemini-2.5-flash")

    asyncio.run(session._run_llm_gemini())

    config = session.gemini_client.configs[0]
    assert config["thinking_config"] == {"thinking_budget": 0}
    assert config["max_output_tokens"] == server.MAX_TOKENS


def test_thinking_is_floored_for_a_3x_model():
    session, _spoken = _session([[_text_chunk("Hi.")]], model="gemini-3.5-flash")

    asyncio.run(session._run_llm_gemini())

    assert session.gemini_client.configs[0]["thinking_config"] == {
        "thinking_level": server.GEMINI_THINKING_LEVEL
    }


def test_a_model_that_rejects_thinking_controls_degrades_instead_of_failing(monkeypatch):
    monkeypatch.setattr(server, "_gemini_thinking_unsupported", False)
    session, spoken = _session([[_text_chunk("Hi there.")]])
    inner = session.gemini_client.aio.models.generate_content_stream
    attempts: list[dict] = []

    async def picky(**kwargs):
        attempts.append(kwargs["config"])
        if "thinking_config" in kwargs["config"]:
            raise ValueError("thinking_config is not supported for this model")
        return await inner(**kwargs)

    session.gemini_client.aio.models.generate_content_stream = picky

    asyncio.run(session._run_llm_gemini())

    assert len(attempts) == 2
    assert "thinking_config" not in attempts[1]
    assert spoken == ["Hi there."]
    assert server._gemini_thinking_unsupported is True


# --- ConversationRelay runner --------------------------------------------

class FakeRelaySocket:
    def __init__(self):
        self.sent: list[dict] = []

    async def send_text(self, payload: str) -> None:
        self.sent.append(json.loads(payload))

    def tokens(self) -> list[str]:
        return [msg["token"] for msg in self.sent if msg.get("token")]


def test_conversation_relay_gemini_streams_tokens_as_they_arrive():
    socket = FakeRelaySocket()
    client = FakeGemini([[_text_chunk("Good "), _text_chunk("morning.")]])

    result = asyncio.run(
        server._run_llm_streaming_gemini(
            gemini_client=client,
            system="sys",
            conversation_history=[{"role": "user", "content": "hi"}],
            tools=[],
            websocket=socket,
        )
    )

    assert socket.tokens() == ["Good ", "morning."]
    assert result == "Good morning."
    assert socket.sent[-1] == {"type": "text", "token": "", "last": True}


def test_conversation_relay_gemini_empty_turn_retries_then_apologises():
    socket = FakeRelaySocket()
    client = FakeGemini([[_empty_chunk()], [_empty_chunk()]])
    history: list[dict] = [{"role": "user", "content": "hi"}]

    result = asyncio.run(
        server._run_llm_streaming_gemini(
            gemini_client=client,
            system="sys",
            conversation_history=history,
            tools=[],
            websocket=socket,
        )
    )

    fallback = server.LLM_EMPTY_FALLBACKS["en"]
    assert client.requests == 2
    assert socket.tokens() == [fallback]
    assert result == fallback
    assert history[-1] == {"role": "assistant", "content": fallback}


def test_conversation_relay_gemini_never_speaks_thoughts():
    socket = FakeRelaySocket()
    client = FakeGemini([[
        _chunk(_part(text="Internal plan: quote the rate.", thought=True)),
        _text_chunk("Seven hundred dollars a night."),
    ]])

    asyncio.run(
        server._run_llm_streaming_gemini(
            gemini_client=client,
            system="sys",
            conversation_history=[{"role": "user", "content": "hi"}],
            tools=[],
            websocket=socket,
        )
    )

    assert socket.tokens() == ["Seven hundred dollars a night."]


def test_conversation_relay_gemini_stops_sending_after_a_bargein():
    socket = FakeRelaySocket()
    generation_ref = [3]

    class BargingSocket(FakeRelaySocket):
        async def send_text(self, payload: str) -> None:
            await super().send_text(payload)
            generation_ref[0] = 4  # the caller interrupts

    socket = BargingSocket()
    client = FakeGemini([[_text_chunk("One. "), _text_chunk("Two. "), _text_chunk("Three.")]])

    asyncio.run(
        server._run_llm_streaming_gemini(
            gemini_client=client,
            system="sys",
            conversation_history=[{"role": "user", "content": "hi"}],
            tools=[],
            websocket=socket,
            generation_ref=generation_ref,
        )
    )

    assert socket.tokens() == ["One. "]


def test_conversation_relay_gemini_tool_round_sends_a_filler(monkeypatch):
    socket = FakeRelaySocket()
    client = FakeGemini([
        [_tool_chunk("check_availability", {"nights": 1})],
        [_text_chunk("One suite is free.")],
    ])
    executed: list[str] = []

    async def execute(name, _arguments):
        executed.append(name)
        return json.dumps({"status": "ok"})

    monkeypatch.setattr(server, "execute_tool", execute)

    asyncio.run(
        server._run_llm_streaming_gemini(
            gemini_client=client,
            system="sys",
            conversation_history=[{"role": "user", "content": "hi"}],
            tools=[],
            websocket=socket,
        )
    )

    assert executed == ["check_availability"]
    assert client.requests == 2
    assert any(
        token in server.TOOL_FILLER_VARIANTS["check_availability"]
        for token in socket.tokens()
    )
    assert "One suite is free." in socket.tokens()


def test_conversation_relay_capture_tool_gets_no_filler_but_still_runs(monkeypatch):
    """A capture tool is instant and local — a 'let me check' would be a lie."""
    socket = FakeRelaySocket()
    client = FakeGemini([
        [_tool_chunk("capture_spoken_number", {"spoken": "oh seven one"})],
        [_text_chunk("Got it.")],
    ])
    executed: list[str] = []

    async def execute(name, _arguments):
        executed.append(name)
        return json.dumps({"status": "ok", "digits": "071"})

    monkeypatch.setattr(server, "execute_tool", execute)

    asyncio.run(
        server._run_llm_streaming_gemini(
            gemini_client=client,
            system="sys",
            conversation_history=[{"role": "user", "content": "hi"}],
            tools=[],
            websocket=socket,
        )
    )

    assert executed == ["capture_spoken_number"], "the tool must still execute"
    assert socket.tokens() == ["Got it."]
    assert server.DEFAULT_FILLER not in socket.tokens()


def test_conversation_relay_capture_tool_uses_reference_context_prefix_raw_utterance(monkeypatch):
    socket = FakeRelaySocket()
    client = FakeGemini([
        [_tool_chunk("capture_spoken_number", {"spoken": "bad-model-spoken"})],
        [_text_chunk("Thanks, I have the number.")],
    ])
    captured: dict[str, str] = {}

    async def execute(name, arguments):
        captured["name"] = name
        captured["spoken"] = arguments.get("spoken", "")
        return json.dumps({"status": "ok"})

    monkeypatch.setattr(server, "execute_tool", execute)

    asyncio.run(
        server._run_llm_streaming_gemini(
            gemini_client=client,
            system="sys",
            conversation_history=[
                {"role": "user", "content": "[Reference context: KB snippets]\n\nGuest: triple seven"},
            ],
            tools=server.get_tools(),
            websocket=socket,
        )
    )

    assert captured == {
        "name": "capture_spoken_number",
        "spoken": "triple seven",
    }


# --- Gemini failover -> Claude (per-session + per-call sticky) ---------------

def test_session_media_stream_gemini_quota_error_failsover_to_claude(monkeypatch, caplog):
    session, spoken = _session([[_empty_chunk()]])
    session.anthropic_client = object()
    session.history.append({"role": "user", "content": "hello"})
    base_model = session.model

    calls: list[dict] = []

    async def _run_claude() -> str:
        calls.append(
            {
                "model": session.model,
                "provider": session.llm_provider,
                "tools_len": len(session.tools),
            }
        )
        return "claude fallback"

    monkeypatch.setattr(session, "_run_llm_claude", _run_claude)
    session.gemini_client = FakeFlakyGemini([_QuotaError()])

    with caplog.at_level("WARNING", logger="server"):
        result = asyncio.run(session._run_llm_gemini())

    assert result == "claude fallback"
    assert calls == [
        {"model": server.CLAUDE_MODEL, "provider": "claude", "tools_len": len(server.get_tools())}
    ]
    assert session.model == base_model
    assert any(
        "event=llm_provider_failover" in r.getMessage() and "reason=quota" in r.getMessage()
        for r in caplog.records
    )
    assert session._gemini_failover_state["consecutive_failovers"] == 1


def test_gemini_failover_can_be_disabled_for_conversation_relay(monkeypatch):
    socket = FakeRelaySocket()
    client = FakeFlakyGemini([_QuotaError()])
    monkeypatch.setattr(server, "GEMINI_FAILOVER_TO_CLAUDE", False)

    with pytest.raises(_QuotaError):
        asyncio.run(
            server._run_llm_streaming_gemini(
                gemini_client=client,
                system="sys",
                conversation_history=[{"role": "user", "content": "hi"}],
                tools=[],
                websocket=socket,
            )
        )


def test_conversation_relay_gemini_sticky_failover_threshold_blocks_gemini(monkeypatch, caplog):
    socket = FakeRelaySocket()
    state = server._init_gemini_failover_state()
    client = FakeFlakyGemini([_QuotaError(), _QuotaError(), _empty_chunk(), _empty_chunk()])
    monkeypatch.setattr(server, "_get_anthropic_client", lambda: "anthropic")
    monkeypatch.setattr(server, "ANTHROPIC_API_KEY", "present")
    claude_calls = 0

    async def _run_claude(
        client: object,
        system: str,
        conversation_history: list[dict],
        tools: list[dict],
        websocket: FakeRelaySocket,
        lang: str = "en",
        generation_ref=None,
        transcript_sink=None,
        model=server.MODEL,
    ) -> str:
        nonlocal claude_calls
        claude_calls += 1
        return "claude"

    monkeypatch.setattr(server, "_run_llm_streaming_claude", _run_claude)

    with caplog.at_level("INFO", logger="server"):
        for _ in range(3):
            result = asyncio.run(
                server._run_llm_streaming_gemini(
                    gemini_client=client,
                    system="sys",
                    conversation_history=[{"role": "user", "content": "hi"}],
                    tools=[],
                    websocket=socket,
                    failover_state=state,
                )
            )
            assert result == "claude"

    degraded_logs = [
        r.getMessage()
        for r in caplog.records
        if "smartpbx_media event=llm_provider_degraded" in r.getMessage()
    ]

    assert len(degraded_logs) == 1
    assert client.requests == 2
    assert claude_calls == 3
    assert state["degraded"] is True


def test_conversation_relay_gemini_recovery_resets_failover_counter(monkeypatch):
    socket = FakeRelaySocket()
    state = server._init_gemini_failover_state()
    client = FakeFlakyGemini([
        _QuotaError(),
        [_text_chunk("ok from gemini")],
        _QuotaError(),
        _empty_chunk(),
    ])

    monkeypatch.setattr(server, "_get_anthropic_client", lambda: "anthropic")
    monkeypatch.setattr(server, "ANTHROPIC_API_KEY", "present")
    claude_calls = 0

    async def _run_claude(
        client: object,
        system: str,
        conversation_history: list[dict],
        tools: list[dict],
        websocket: FakeRelaySocket,
        lang: str = "en",
        generation_ref=None,
        transcript_sink=None,
        model=server.MODEL,
    ) -> str:
        nonlocal claude_calls
        claude_calls += 1
        return "claude"

    monkeypatch.setattr(server, "_run_llm_streaming_claude", _run_claude)

    first = asyncio.run(
        server._run_llm_streaming_gemini(
            gemini_client=client,
            system="sys",
            conversation_history=[{"role": "user", "content": "hi"}],
            tools=[],
            websocket=socket,
            failover_state=state,
        )
    )
    assert first == "claude"

    second = asyncio.run(
        server._run_llm_streaming_gemini(
            gemini_client=client,
            system="sys",
            conversation_history=[{"role": "user", "content": "again"}],
            tools=[],
            websocket=socket,
            failover_state=state,
        )
    )
    assert second == "ok from gemini"

    third = asyncio.run(
        server._run_llm_streaming_gemini(
            gemini_client=client,
            system="sys",
            conversation_history=[{"role": "user", "content": "again"}],
            tools=[],
            websocket=socket,
            failover_state=state,
        )
    )
    assert third == "claude"

    assert state["degraded"] is False
    assert state["consecutive_failovers"] == 1
    assert claude_calls == 2
    # Three Gemini attempts is the desired shape: the mid-sequence success
    # reset the counter, so the third turn tries Gemini again before failing
    # over — recovery must re-earn trust, not stay degraded.
    assert client.requests == 3


def test_conversation_relay_gemini_fails_over_only_when_anthropic_configured(monkeypatch):
    socket = FakeRelaySocket()
    client = FakeFlakyGemini([_QuotaError()])
    monkeypatch.setattr(server, "ANTHROPIC_API_KEY", "")
    claude_calls = 0

    async def _run_claude(*_args, **_kwargs):
        nonlocal claude_calls
        claude_calls += 1
        return "claude"

    monkeypatch.setattr(server, "_run_llm_streaming_claude", _run_claude)

    with pytest.raises(_QuotaError):
        asyncio.run(
            server._run_llm_streaming_gemini(
                gemini_client=client,
                system="sys",
                conversation_history=[{"role": "user", "content": "hi"}],
                tools=[],
                websocket=socket,
            )
        )

    assert claude_calls == 0
