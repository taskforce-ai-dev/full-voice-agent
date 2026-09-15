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
import copy
import httpx
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


def _terminal_chunk(finish_reason="STOP"):
    """The terminal metadata emitted by a completed native Gemini round."""
    return _chunk(finish_reason=finish_reason)


def _terminal_chunk_with_output_usage(output_tokens: int):
    """A native Gemini terminal chunk with its SDK output-usage field."""
    chunk = _terminal_chunk()
    chunk.usage_metadata = SimpleNamespace(
        candidates_token_count=output_tokens,
    )
    return chunk


def _usage_only_chunk(output_tokens: int):
    """A native Gemini usage update that carries no candidate payload."""
    return SimpleNamespace(
        candidates=[],
        usage_metadata=SimpleNamespace(candidates_token_count=output_tokens),
    )


def _tool_chunk(
    name: str,
    args: dict | None = None,
    *,
    id: str | None = None,
    thought_signature=None,
):
    return _chunk(
        _part(
            function_call=SimpleNamespace(name=name, args=args or {}, id=id),
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
        snapshot = copy.deepcopy(kwargs)
        self.owner.configs.append(snapshot.get("config"))
        self.owner.contents.append(snapshot.get("contents"))
        self.owner.request_payloads.append(snapshot)
        chunks = self.owner.rounds.pop(0)

        async def stream():
            for index, chunk in enumerate(chunks):
                self.owner.timeline.append(("chunk", index))
                if isinstance(chunk, BaseException):
                    raise chunk
                yield chunk

        return stream()


class FakeGemini:
    def __init__(self, rounds, timeline=None):
        self.rounds = list(rounds)
        self.requests = 0
        self.configs: list[dict] = []
        self.contents: list[list] = []
        self.request_payloads: list[dict] = []
        self.timeline: list[tuple] = timeline if timeline is not None else []
        self.aio = SimpleNamespace(models=FakeGeminiModels(self))


class FakeFlakyGemini:
    """Gemini mock where a turn can raise an exception or return chunks."""

    def __init__(self, turns):
        self._turns = list(turns)
        self.requests = 0
        self.configs: list[dict] = []
        self.contents: list[list] = []
        self.request_payloads: list[dict] = []
        self.timeline: list[tuple] = []
        self.aio = SimpleNamespace(models=self)

    async def generate_content_stream(self, **kwargs):
        self.requests += 1
        snapshot = copy.deepcopy(kwargs)
        self.configs.append(snapshot.get("config"))
        self.contents.append(snapshot.get("contents"))
        self.request_payloads.append(snapshot)

        behavior = self._turns.pop(0)
        if isinstance(behavior, BaseException):
            raise behavior

        turn = SimpleNamespace(chunks=behavior)
        turn_index = self.requests - 1

        async def stream():
            for index, chunk in enumerate(turn.chunks):
                self.timeline.append(("chunk", turn_index, index))
                if isinstance(chunk, BaseException):
                    raise chunk
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


def _session(
    rounds,
    *,
    lang="en",
    smartpbx=False,
    timeline=None,
    model="gemini-2.5-flash",
    terminalize_direct_rounds=True,
):
    """A Gemini MediaStreamSession with TTS captured instead of synthesised."""
    session = server.MediaStreamSession(
        websocket=None,
        lang=lang,
        llm_provider="gemini",
        model=model,
        media_transport=FakeTransport() if smartpbx else None,
    )
    session.tools = []
    # Only direct SmartPBX fixtures acquire the terminal-aware contract. The
    # regular Media Streams / ConversationRelay fixtures intentionally retain
    # their existing terminal-free behavior.
    if smartpbx and terminalize_direct_rounds:
        rounds = [
            list(round_) + [_terminal_chunk()]
            if isinstance(round_, list)
            and not any(
                getattr(chunk.candidates[0], "finish_reason", None)
                for chunk in round_
                if getattr(chunk, "candidates", None)
            )
            else round_
            for round_ in rounds
        ]
    session.gemini_client = FakeGemini(rounds, timeline=timeline)
    if smartpbx:
        session._smartpbx_transfer_context = object()

    spoken: list[str] = []

    async def tts(text, **_kwargs):
        session.gemini_client.timeline.append(("tts", text))
        spoken.append(text)

    session._tts_elevenlabs = tts
    session._tts_openai = tts
    session._tts_gemini_sinhala = tts
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


def test_direct_smartpbx_sinhala_batches_a_no_tool_reply_into_one_tts_request():
    """Avoid a second Gemini synthesis gap inside one caller-facing reply."""
    timeline: list[tuple] = []
    session, spoken = _session(
        [[
            _text_chunk("පළමු වාක්‍යය. "),
            _text_chunk("දෙවන වාක්‍යය."),
        ]],
        lang="si",
        smartpbx=True,
        timeline=timeline,
    )

    result = asyncio.run(session._run_llm_gemini())

    assert spoken == ["පළමු වාක්‍යය. දෙවන වාක්‍යය."]
    assert result == "පළමු වාක්‍යය. දෙවන වාක්‍යය."
    assert next(i for i, entry in enumerate(timeline) if entry[0] == "tts") > max(
        i for i, entry in enumerate(timeline) if entry[0] == "chunk"
    )


def test_direct_smartpbx_sinhala_non_streaming_model_uses_incremental_sentence_tts(
    monkeypatch,
):
    """A non-streaming fallback model (2026-09-04) must not batch the whole
    reply -- each sentence reaches TTS as it completes, same as the English
    per-sentence path, so the first sentence's audio starts in seconds."""
    import server

    monkeypatch.setattr(
        server, "_smartpbx_sinhala_tts_current_model",
        lambda **_kw: "gemini-2.5-flash-preview-tts",
    )
    timeline: list[tuple] = []
    session, spoken = _session(
        [[
            _text_chunk("එක. "),
            _text_chunk("දෙක. "),
            _text_chunk("තුන."),
        ]],
        lang="si",
        smartpbx=True,
        timeline=timeline,
    )

    result = asyncio.run(session._run_llm_gemini())

    assert spoken == ["එක.", "දෙක.", "තුන."]
    assert result == "එක. දෙක. තුන."
    # Load-bearing: the first sentence must reach TTS before the stream
    # finishes, unlike the batched (streaming-model) path above.
    first_tts = next(i for i, entry in enumerate(timeline) if entry[0] == "tts")
    last_chunk = max(i for i, entry in enumerate(timeline) if entry[0] == "chunk")
    assert first_tts < last_chunk


def test_direct_smartpbx_sinhala_non_streaming_model_caps_requests_and_merges_the_tail(
    monkeypatch,
):
    """Per-sentence synthesis on a non-streaming model is capped at 4 live
    TTS requests per turn -- sentences beyond the cap merge into the final
    request instead of each starting a new one."""
    import server

    monkeypatch.setattr(
        server, "_smartpbx_sinhala_tts_current_model",
        lambda **_kw: "gemini-2.5-flash-preview-tts",
    )
    session, spoken = _session(
        [[
            _text_chunk("එක. "),
            _text_chunk("දෙක. "),
            _text_chunk("තුන. "),
            _text_chunk("හතර. "),
            _text_chunk("පහ. "),
            _text_chunk("හය."),
        ]],
        lang="si",
        smartpbx=True,
    )

    asyncio.run(session._run_llm_gemini())

    assert len(spoken) <= 4
    assert spoken[:3] == ["එක.", "දෙක.", "තුන."]
    assert spoken[3] == "හතර. පහ. හය."


def test_direct_smartpbx_sinhala_streaming_primary_still_batches_despite_non_streaming_config(
    monkeypatch,
):
    """The non-streaming-models config must never touch the streaming
    primary -- batching stays exactly as it is when the primary is active."""
    import server

    monkeypatch.setattr(
        server, "SMARTPBX_SINHALA_TTS_NON_STREAMING_MODELS",
        frozenset({"gemini-2.5-flash-preview-tts"}),
    )
    session, spoken = _session(
        [[
            _text_chunk("පළමු වාක්‍යය. "),
            _text_chunk("දෙවන වාක්‍යය."),
        ]],
        lang="si",
        smartpbx=True,
    )

    asyncio.run(session._run_llm_gemini())

    assert spoken == ["පළමු වාක්‍යය. දෙවන වාක්‍යය."]


def test_direct_smartpbx_english_keeps_incremental_sentence_tts():
    """The Sinhala batching boundary must not alter the established English path."""
    session, spoken = _session(
        [[
            _text_chunk("First sentence. "),
            _text_chunk("Second sentence."),
        ]],
        lang="en",
        smartpbx=True,
    )

    asyncio.run(session._run_llm_gemini())

    assert spoken == ["First sentence.", "Second sentence."]


def test_twilio_sinhala_keeps_incremental_sentence_tts():
    """Only the direct SmartPBX Sinhala route trades streaming for continuity."""
    session, spoken = _session(
        [[
            _text_chunk("පළමු වාක්‍යය. "),
            _text_chunk("දෙවන වාක්‍යය."),
        ]],
        lang="si",
    )

    asyncio.run(session._run_llm_gemini())

    assert spoken == ["පළමු වාක්‍යය.", "දෙවන වාක්‍යය."]


def test_direct_smartpbx_sinhala_capture_keeps_incremental_sentence_tts():
    """Capture flows retain their specialised streaming turn behavior."""
    session, spoken = _session(
        [[
            _text_chunk("පළමු වාක්‍යය. "),
            _text_chunk("දෙවන වාක්‍යය."),
        ]],
        lang="si",
        smartpbx=True,
    )
    session._capture_mode_active = True

    asyncio.run(session._run_llm_gemini())

    assert spoken == ["පළමු වාක්‍යය.", "දෙවන වාක්‍යය."]


def test_direct_smartpbx_sinhala_keeps_a_tool_preamble_audible(monkeypatch):
    """Batching a no-tool reply must not discard text that precedes a tool."""
    session, spoken = _session(
        [
            [
                _text_chunk("මම ඒක පරීක්ෂා කරමි. "),
                _text_chunk("කරුණාකර මොහොතක් ඉන්න. "),
                _tool_chunk("check_availability", {"nights": 2}),
            ],
            [_text_chunk("කාමර තිබේ.")],
        ],
        lang="si",
        smartpbx=True,
    )

    async def execute(_name, _arguments):
        return json.dumps({"status": "ok"})

    monkeypatch.setattr(server, "execute_tool", execute)

    asyncio.run(session._run_llm_gemini())

    assert spoken[:2] == [
        "මම ඒක පරීක්ෂා කරමි.",
        "කරුණාකර මොහොතක් ඉන්න.",
    ]
    assert spoken[-1] == "කාමර තිබේ."


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
            [_tool_chunk("check_availability", {"nights": 2}, id="tool-latency-1")],
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
        [
            [_tool_chunk("check_availability", id=f"bounded-{index}")]
            for index in range(server.MAX_TOOL_ROUNDS)
        ],
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
            [
                _tool_chunk(
                    "check_availability",
                    {},
                    id="thought-1",
                    thought_signature=b"sig-123",
                )
            ],
            [_text_chunk("All set.")],
        ],
        smartpbx=True,
        model="gemini-3.7-flash",
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
    assert session._gemini_thinking_unsupported_models == {session.model}
    assert server._gemini_thinking_unsupported is False


def test_direct_sinhala_gemini_uses_its_session_owned_thinking_and_budget():
    session, _spoken = _session(
        [[_text_chunk("හරි.")]],
        lang="si",
        smartpbx=True,
        model="gemini-3.7-flash",
    )
    session._gemini_thinking_level = "low"
    session._smartpbx_gemini_max_tokens = 600

    asyncio.run(session._run_llm_gemini())

    config = session.gemini_client.configs[0]
    assert config["thinking_config"] == {"thinking_level": "low"}
    assert config["max_output_tokens"] == 600


def test_direct_english_gemini_keeps_shared_controls_and_incremental_tts():
    session, spoken = _session(
        [[_text_chunk("All right.")]],
        lang="en",
        smartpbx=True,
        model="gemini-3.7-flash",
    )
    session._gemini_thinking_level = server.GEMINI_THINKING_LEVEL
    session._smartpbx_gemini_max_tokens = 600

    asyncio.run(session._run_llm_gemini())

    config = session.gemini_client.configs[0]
    assert config["thinking_config"] == {
        "thinking_level": server.GEMINI_THINKING_LEVEL
    }
    assert config["max_output_tokens"] == server.SMARTPBX_MAX_TOKENS
    assert spoken == ["All right."]
    assert session._is_direct_smartpbx_english_non_capture() is True


def test_direct_smartpbx_gemini_logs_bounded_reported_output_tokens(caplog):
    session, _spoken = _session(
        [[
            _text_chunk("Ready."),
            _terminal_chunk_with_output_usage(
                server.SMARTPBX_CLAUDE_MAX_LOGGED_OUTPUT_TOKENS + 1,
            ),
        ]],
        smartpbx=True,
        terminalize_direct_rounds=False,
    )

    with caplog.at_level("INFO", logger="server"):
        asyncio.run(session._run_llm_gemini())

    outcomes = [
        record.getMessage()
        for record in caplog.records
        if "event=llm_round_outcome provider=gemini" in record.getMessage()
    ]
    assert outcomes == [
        "smartpbx_media event=llm_round_outcome provider=gemini "
        "outcome=completed stop_reason=end_turn output_tokens=1000000 attempt=1"
    ]


def test_direct_smartpbx_gemini_logs_unknown_only_without_usage_metadata(caplog):
    session, _spoken = _session(
        [[_text_chunk("Ready."), _terminal_chunk()]],
        smartpbx=True,
        terminalize_direct_rounds=False,
    )

    with caplog.at_level("INFO", logger="server"):
        asyncio.run(session._run_llm_gemini())

    outcomes = [
        record.getMessage()
        for record in caplog.records
        if "event=llm_round_outcome provider=gemini" in record.getMessage()
    ]
    assert outcomes == [
        "smartpbx_media event=llm_round_outcome provider=gemini "
        "outcome=completed stop_reason=end_turn output_tokens=unknown attempt=1"
    ]


def test_direct_smartpbx_gemini_consumes_usage_only_chunk_without_capping(caplog):
    session, _spoken = _session(
        [[
            _usage_only_chunk(42),
            _text_chunk("Ready."),
            _terminal_chunk(),
        ]],
        smartpbx=True,
        terminalize_direct_rounds=False,
    )

    with caplog.at_level("INFO", logger="server"):
        asyncio.run(session._run_llm_gemini())

    outcomes = [
        record.getMessage()
        for record in caplog.records
        if "event=llm_round_outcome provider=gemini" in record.getMessage()
    ]
    assert outcomes == [
        "smartpbx_media event=llm_round_outcome provider=gemini "
        "outcome=completed stop_reason=end_turn output_tokens=42 attempt=1"
    ]


def test_direct_smartpbx_gemini_retry_does_not_reuse_prior_attempt_usage(caplog):
    session, _spoken = _session(
        [[_terminal_chunk_with_output_usage(42)], [
            _text_chunk("Ready."),
            _terminal_chunk(),
        ]],
        smartpbx=True,
        terminalize_direct_rounds=False,
    )

    with caplog.at_level("INFO", logger="server"):
        asyncio.run(session._run_llm_gemini())

    outcomes = [
        record.getMessage()
        for record in caplog.records
        if "event=llm_round_outcome provider=gemini" in record.getMessage()
    ]
    assert outcomes == [
        "smartpbx_media event=llm_round_outcome provider=gemini "
        "outcome=true_empty stop_reason=end_turn output_tokens=42 attempt=1",
        "smartpbx_media event=llm_round_outcome provider=gemini "
        "outcome=completed stop_reason=end_turn output_tokens=unknown attempt=2",
    ]


def test_gemini_session_constructor_owns_default_generation_controls():
    session, _spoken = _session([[_text_chunk("Hi.")]], smartpbx=True)

    assert session._gemini_thinking_level == server.GEMINI_THINKING_LEVEL
    assert session._smartpbx_gemini_max_tokens == server.SMARTPBX_MAX_TOKENS
    assert session._gemini_thinking_unsupported_models == set()


def test_non_smartpbx_sinhala_keeps_global_max_tokens_and_legacy_eof_shape():
    rounds = [[_text_chunk("හරි.")]]
    session, _spoken = _session(rounds, lang="si", model="gemini-3.7-flash")

    assert session._provider_max_tokens("gemini") == server.MAX_TOKENS
    assert getattr(rounds[0][-1].candidates[0], "finish_reason", None) is None


def test_gemini_37_multi_tool_followup_preserves_each_provider_call_id(monkeypatch):
    session, _spoken = _session(
        [[
            _tool_chunk(
                "check_availability", {"room": "A"}, id="call-17",
                thought_signature=b"sig-17",
            ),
            _tool_chunk(
                "check_availability", {"room": "B"}, id="call-18",
                thought_signature=b"sig-18",
            ),
        ], [_text_chunk("Both rooms are available.")]],
        smartpbx=True,
        model="gemini-3.7-flash",
    )
    executed: list[dict] = []

    async def execute(name, arguments):
        executed.append({"name": name, "arguments": dict(arguments)})
        return json.dumps({"room": arguments["room"], "available": True})

    monkeypatch.setattr(server, "execute_tool", execute)
    asyncio.run(session._run_llm_gemini())

    assert executed == [
        {"name": "check_availability", "arguments": {"room": "A"}},
        {"name": "check_availability", "arguments": {"room": "B"}},
    ]
    followup = session.gemini_client.request_payloads[1]
    assert followup["model"] == "gemini-3.7-flash"
    model_calls = [
        part["function_call"]
        for content in followup["contents"]
        if content["role"] == "model"
        for part in content["parts"]
        if "function_call" in part
    ]
    responses = [
        part["function_response"]
        for content in followup["contents"]
        if content["role"] == "user"
        for part in content["parts"]
        if "function_response" in part
    ]
    assert [(call["id"], call["name"]) for call in model_calls] == [
        ("call-17", "check_availability"), ("call-18", "check_availability"),
    ]
    assert [part.get("thought_signature") for content in followup["contents"]
            if content["role"] == "model" for part in content["parts"]
            if "function_call" in part] == [b"sig-17", b"sig-18"]
    assert [(response["id"], response["name"], response["response"])
            for response in responses] == [
        ("call-17", "check_availability", {"room": "A", "available": True}),
        ("call-18", "check_availability", {"room": "B", "available": True}),
    ]
    assert "gemini_tc_" not in repr(session.history)


def test_conversation_relay_history_conversion_keeps_function_call_ids_opted_out():
    history = [
        {"role": "assistant", "content": None, "tool_calls": [{
            "id": "provider-id", "type": "function",
            "function": {"name": "check_availability", "arguments": "{}"},
        }]},
        {"role": "tool", "tool_call_id": "provider-id", "content": "{}"},
    ]

    contents = server._history_to_gemini(history)
    parts = [part for content in contents for part in content["parts"]]
    assert "id" not in parts[0]["function_call"]
    assert "id" not in parts[1]["function_response"]


@pytest.mark.parametrize(
    ("id", "name", "args"),
    [
        (None, "check_availability", {"nights": 1}),
        ("call-19", None, {"nights": 1}),
        ("call-20", "check_availability", ["private-malformed-sentinel"]),
    ],
)
def test_gemini_malformed_provider_tool_payload_is_closed_and_privacy_safe(
    monkeypatch, caplog, id, name, args,
):
    session, spoken = _session(
        [[_chunk(_part(function_call=SimpleNamespace(id=id, name=name, args=args)))],
         [_chunk(_part(function_call=SimpleNamespace(id=id, name=name, args=args)))]],
        smartpbx=True,
        terminalize_direct_rounds=True,
    )
    executed: list[tuple] = []

    async def execute(*arguments):
        executed.append(arguments)
        return "{}"

    monkeypatch.setattr(server, "execute_tool", execute)
    with caplog.at_level("INFO", logger="server"):
        asyncio.run(session._run_llm_gemini())

    assert executed == []
    assert not [m for m in session.history if m.get("tool_calls") or m.get("role") == "tool"]
    assert spoken == [server.SMARTPBX_LLM_EMPTY_RETRY_RECOVERY_TEXT]
    assert "private-malformed-sentinel" not in caplog.text


def test_gemini_round_outcome_classifier_distinguishes_terminal_failures():
    cases = [
        ("MAX_TOKENS", True, [], "max_tokens_truncated"),
        ("STOP", True, [{"malformed": True}], "malformed_tool_json"),
        (None, False, [], "stream_aborted"),
        ("STOP", True, [], "true_empty"),
        ("STOP", True, [{"id": "call", "name": "tool", "args": {}}], "completed"),
    ]
    for finish_reason, terminal, calls, expected in cases:
        assert server._classify_gemini_round_outcome(
            text_content="", function_calls=calls, finish_reason=finish_reason,
            saw_terminal_metadata=terminal,
        ).value == expected


def test_thinking_rejection_is_call_local_model_local_and_ordered(monkeypatch, caplog):
    monkeypatch.setattr(server, "_gemini_thinking_unsupported", False)
    a, _ = _session([[_text_chunk("A.")]], smartpbx=True, model="gemini-3.7-flash")
    b, _ = _session([[_text_chunk("B.")]], smartpbx=True, model="gemini-3.7-flash")
    a._gemini_thinking_level = "low"
    b._gemini_thinking_level = "high"
    a_requested = asyncio.Event()
    release_rejection = asyncio.Event()
    a_retried = asyncio.Event()
    calls: list[dict] = []
    inner = a.gemini_client.aio.models.generate_content_stream

    async def reject_a_once(**kwargs):
        calls.append(copy.deepcopy(kwargs))
        if "thinking_config" in kwargs["config"]:
            a_requested.set()
            await release_rejection.wait()
            raise ValueError("thinking_config private rejection text")
        a_retried.set()
        return await inner(**kwargs)

    a.gemini_client.aio.models.generate_content_stream = reject_a_once

    async def scenario():
        a_task = asyncio.create_task(a._run_llm_gemini())
        await asyncio.wait_for(a_requested.wait(), timeout=1)
        release_rejection.set()
        await asyncio.wait_for(a_retried.wait(), timeout=1)
        await b._run_llm_gemini()
        await a_task

    with caplog.at_level("WARNING", logger="server"):
        asyncio.run(scenario())

    assert len(calls) == 2
    assert "thinking_config" not in calls[1]["config"]
    assert len(b.gemini_client.request_payloads) == 1
    assert b.gemini_client.configs[0]["thinking_config"] == {"thinking_level": "high"}
    assert a._gemini_thinking_unsupported_models == {"gemini-3.7-flash"}
    assert b._gemini_thinking_unsupported_models == set()
    assert server._gemini_thinking_unsupported is False
    assert "private rejection text" not in caplog.text


def test_legacy_non_media_gemini_thinking_latch_remains_process_owned(monkeypatch):
    """ConversationRelay callers retain the legacy one-process compatibility latch."""
    monkeypatch.setattr(server, "_gemini_thinking_unsupported", False)
    attempts: list[dict] = []

    class LegacyModels:
        async def generate_content_stream(self, **kwargs):
            attempts.append(copy.deepcopy(kwargs))
            if "thinking_config" in kwargs["config"]:
                raise ValueError("thinking_config is not supported")
            return iter(())

    legacy_client = SimpleNamespace(aio=SimpleNamespace(models=LegacyModels()))

    async def scenario():
        await server._open_gemini_stream(
            legacy_client,
            model="gemini-3.7-flash",
            contents=[],
            config={"thinking_config": {"thinking_level": "low"}},
        )

    asyncio.run(scenario())

    assert len(attempts) == 2
    assert "thinking_config" not in attempts[1]["config"]
    assert server._gemini_thinking_unsupported is True
    assert server._gemini_thinking_config("gemini-3.7-flash") is None


def test_non_thinking_exception_is_not_retried_or_logged_as_config_rejection(monkeypatch, caplog):
    monkeypatch.setattr(server, "_gemini_thinking_unsupported", False)
    monkeypatch.setattr(server, "GEMINI_FAILOVER_TO_CLAUDE", False)
    session, _spoken = _session([[_text_chunk("unreachable")]], smartpbx=True)
    attempts: list[dict] = []
    private_error = "private provider outage while thinking about a retry"

    async def provider_failure(**kwargs):
        attempts.append(copy.deepcopy(kwargs))
        raise RuntimeError(private_error)

    session.gemini_client.aio.models.generate_content_stream = provider_failure

    with caplog.at_level("WARNING", logger="server"):
        with pytest.raises(RuntimeError, match="private provider outage"):
            asyncio.run(session._run_llm_gemini())

    assert len(attempts) == 1
    assert session._gemini_thinking_unsupported_models == set()
    assert server._gemini_thinking_unsupported is False
    assert private_error not in caplog.text


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


# --- a double-empty turn is a provider failure, not an outcome --------------
# Two empty turns in a row sound exactly like a quota error to the caller: dead
# air. So it takes the same route — Claude re-runs the turn and the sticky
# counter advances. The canned apology survives ONLY as the last resort when
# failover is unavailable, which is the one case where silence is the alternative.

def test_media_stream_double_empty_gemini_turn_failsover_to_claude(monkeypatch, caplog):
    session, spoken = _session([[_empty_chunk()], [_empty_chunk()]])
    session.anthropic_client = object()

    async def _run_claude() -> str:
        return "claude answered the turn"

    monkeypatch.setattr(session, "_run_llm_claude", _run_claude)

    with caplog.at_level("WARNING", logger="server"):
        result = asyncio.run(session._run_llm_gemini())

    assert session.gemini_client.requests == 2, "the one empty-turn retry still runs first"
    assert result == "claude answered the turn"
    assert spoken == [], "the canned apology must not be spoken when Claude can answer"
    assert session._gemini_failover_state["consecutive_failovers"] == 1
    assert any(
        "event=llm_provider_failover" in r.getMessage()
        and "reason=empty_response" in r.getMessage()
        for r in caplog.records
    )


def test_media_stream_repeated_double_empty_turns_go_sticky(monkeypatch, caplog):
    session, spoken = _session(
        [[_empty_chunk()], [_empty_chunk()], [_empty_chunk()], [_empty_chunk()]]
    )
    session.anthropic_client = object()
    monkeypatch.setattr(server, "ANTHROPIC_API_KEY", "present")
    claude_calls = 0

    async def _run_claude() -> str:
        nonlocal claude_calls
        claude_calls += 1
        return "claude"

    monkeypatch.setattr(session, "_run_llm_claude", _run_claude)

    with caplog.at_level("INFO", logger="server"):
        for _ in range(3):
            assert asyncio.run(session._run_llm_gemini()) == "claude"

    assert claude_calls == 3
    assert session._gemini_failover_state["degraded"] is True
    # The third turn never touched Gemini: 2 turns x (attempt + retry).
    assert session.gemini_client.requests == 4
    assert spoken == []
    assert sum(
        "event=llm_provider_degraded" in r.getMessage() for r in caplog.records
    ) == 1


def test_media_stream_double_empty_speaks_the_canned_line_when_failover_is_off(monkeypatch):
    session, spoken = _session([[_empty_chunk()], [_empty_chunk()]])
    session.anthropic_client = object()
    monkeypatch.setattr(server, "GEMINI_FAILOVER_TO_CLAUDE", False)

    async def _run_claude() -> str:
        raise AssertionError("failover is disabled — Claude must not be called")

    monkeypatch.setattr(session, "_run_llm_claude", _run_claude)

    result = asyncio.run(session._run_llm_gemini())

    fallback = server.LLM_EMPTY_FALLBACKS["en"]
    assert spoken == [fallback], "silence is still not an acceptable outcome"
    assert result == fallback
    assert session.history[-1] == {"role": "assistant", "content": fallback}
    assert session._gemini_failover_state["consecutive_failovers"] == 0


def test_media_stream_double_empty_speaks_the_canned_line_without_anthropic(monkeypatch):
    session, spoken = _session([[_empty_chunk()], [_empty_chunk()]])
    monkeypatch.setattr(server, "ANTHROPIC_API_KEY", "")
    assert session.anthropic_client is None

    result = asyncio.run(session._run_llm_gemini())

    assert spoken == [server.LLM_EMPTY_FALLBACKS["en"]]
    assert result == server.LLM_EMPTY_FALLBACKS["en"]


def test_conversation_relay_double_empty_gemini_turn_failsover_to_claude(monkeypatch, caplog):
    socket = FakeRelaySocket()
    client = FakeGemini([[_empty_chunk()], [_empty_chunk()]])
    state = server._init_gemini_failover_state()
    history: list[dict] = [{"role": "user", "content": "hi"}]
    monkeypatch.setattr(server, "_get_anthropic_client", lambda: "anthropic")
    monkeypatch.setattr(server, "ANTHROPIC_API_KEY", "present")

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
        return "claude answered the turn"

    monkeypatch.setattr(server, "_run_llm_streaming_claude", _run_claude)

    with caplog.at_level("WARNING", logger="server"):
        result = asyncio.run(
            server._run_llm_streaming_gemini(
                gemini_client=client,
                system="sys",
                conversation_history=history,
                tools=[],
                websocket=socket,
                failover_state=state,
            )
        )

    assert client.requests == 2
    assert result == "claude answered the turn"
    assert socket.tokens() == [], "no canned apology when Claude can answer"
    assert state["consecutive_failovers"] == 1
    assert history == [{"role": "user", "content": "hi"}], (
        "the abandoned Gemini turn must not leave history behind"
    )
    assert any(
        "event=llm_provider_failover" in r.getMessage()
        and "reason=empty_response" in r.getMessage()
        for r in caplog.records
    )


# --- failover must hand Claude CLAUDE-shaped tools ---------------------------
# A Gemini→Claude failover swaps the runner but carries the same tool list.
# Anthropic 400s on Gemini's [{"function_declarations": [...]}] shape, so a
# failover that forgets to convert produces a second failure — the caller hears
# the error line instead of the recovered answer. These tests deliberately do
# NOT monkeypatch the Claude runner: they assert what the Anthropic client (and
# the relay runner) is actually handed.

class _FakeStreamContext:
    def __init__(self, events):
        self._events = events

    async def __aenter__(self):
        async def _stream():
            for event in self._events:
                yield event

        return _stream()

    async def __aexit__(self, *_args):
        return False


class _FakeAnthropicMessages:
    def __init__(self, events):
        self.calls: list[dict] = []
        self._events = events

    def stream(self, **kwargs):
        self.calls.append(kwargs)
        return _FakeStreamContext(self._events)


class FakeAnthropicClient:
    def __init__(self, events=None):
        self.messages = _FakeAnthropicMessages(events or [])


def _anthropic_text_delta(text: str):
    return SimpleNamespace(
        type="content_block_delta",
        delta=SimpleNamespace(type="text_delta", text=text),
    )


GEMINI_SHAPED_TOOLS = [
    {
        "function_declarations": [
            {
                "name": "check_availability",
                "description": "check rooms",
                "parameters": {"type": "object", "properties": {}},
            },
            {
                "name": "capture_spoken_number",
                "description": "parse digits",
                "parameters": {"type": "object", "properties": {}},
            },
        ]
    }
]


def test_gemini_tool_shape_converts_to_anthropic_definitions():
    converted = server._claude_tools_from_gemini(GEMINI_SHAPED_TOOLS)

    assert [tool["name"] for tool in converted] == [
        "check_availability", "capture_spoken_number",
    ]
    assert all("input_schema" in tool for tool in converted)
    assert all("function_declarations" not in tool for tool in converted)


def test_gemini_tool_shape_conversion_preserves_a_restricted_tool_set():
    """The handover failsafe offers ONLY the notify tool — that must survive."""
    converted = server._claude_tools_from_gemini(server.get_handover_tools("gemini"))

    assert [tool["name"] for tool in converted] == ["notify_human_handover"]
    assert converted == server.get_handover_tools("claude")


def test_gemini_tool_shape_conversion_round_trips_the_real_tool_definitions():
    """Converting the production Gemini tools must reproduce get_tools() exactly."""
    import tools as tools_module

    gemini_shaped = [
        {
            "function_declarations": [
                {
                    "name": tool["name"],
                    "description": tool["description"],
                    "parameters": tool["input_schema"],
                }
                for tool in tools_module.TOOL_DEFINITIONS
            ]
        }
    ]
    assert server._claude_tools_from_gemini(gemini_shaped) == tools_module.TOOL_DEFINITIONS


def test_gemini_tool_shape_conversion_passes_anthropic_tools_through():
    claude_tools = [
        {"name": "check_availability", "description": "d", "input_schema": {}}
    ]
    assert server._claude_tools_from_gemini(claude_tools) == claude_tools
    assert server._claude_tools_from_gemini([]) == []
    assert server._claude_tools_from_gemini(None) == []


def test_gemini_to_claude_conversion_deep_copies_mixed_tool_shapes(monkeypatch):
    original_tools = [
        {"function_declarations": [{
            "name": "check_availability",
            "description": "check rooms",
            "parameters": {"type": "object", "properties": {"date": {"type": "string"}}},
        }]},
        {
            "name": "capture_spoken_number",
            "description": "parse digits",
            "input_schema": {"type": "object", "properties": {"spoken": {"type": "string"}}},
        },
    ]
    before = copy.deepcopy(original_tools)
    converted = server._claude_tools_from_gemini(original_tools)
    for tool in converted:
        tool["input_schema"]["properties"]["temporary"] = {"type": "boolean"}
    assert original_tools == before

    session, spoken = _session([], lang="si", smartpbx=True)
    session.tools = original_tools
    original_model = session.model
    monkeypatch.setattr(
        server,
        "_claude_tools_from_gemini",
        lambda _tools: (_ for _ in ()).throw(ValueError("local conversion invariant")),
    )
    # A local invariant failure inside the swap is OURS, not the provider's: the
    # caller gets the localized recovery line rather than the error filler, and
    # the call profile is restored exactly as before.
    recovery = asyncio.run(session._run_claude_failover_turn())
    assert recovery and spoken == [recovery]
    assert "සමාවෙන්න" in recovery
    assert session.llm_provider == "gemini"
    assert session.model == original_model
    assert session.tools is original_tools


def test_direct_sinhala_gemini_failure_uses_claude_then_restores_full_profile(monkeypatch, caplog):
    session, spoken = _session([], lang="si", smartpbx=True, model="gemini-3.7-flash")
    session.gemini_client = FakeFlakyGemini([_QuotaError()])
    session.anthropic_client = object()
    original_tools = [{"function_declarations": [{
        "name": "check_availability",
        "description": "check rooms",
        "parameters": {"type": "object", "properties": {"date": {"type": "string"}}},
    }]}]
    session.tools = original_tools
    session.history = [{"role": "user", "content": "hello"}]
    snapshot = (
        session.lang, session.system_prompt, session.llm_provider, session.model,
        session.tools, copy.deepcopy(session.tools), session.gemini_client,
        session.anthropic_client, session._gemini_thinking_level,
        session._smartpbx_gemini_max_tokens, session._speak_generation,
        copy.deepcopy(session.history), copy.deepcopy(session.full_transcript),
        session._tts_elevenlabs, session._tts_openai, session._tts_gemini_sinhala,
    )
    seen = {}

    async def run_claude():
        seen["profile"] = (session.llm_provider, session.model, session.lang)
        seen["prompt"] = session._active_system_prompt()
        seen["tools"] = session.tools
        assert all("function_declarations" not in tool for tool in session.tools)
        assert [tool["name"] for tool in session.tools] == ["check_availability"]
        session.tools[0]["input_schema"]["properties"]["temporary"] = {"type": "string"}
        await session._speak("සිංහල පිළිතුරක්.")
        return "සිංහල පිළිතුරක්."

    monkeypatch.setattr(server, "ANTHROPIC_API_KEY", "")
    monkeypatch.setattr(session, "_run_llm_claude", run_claude)
    with caplog.at_level("WARNING", logger="server"):
        assert asyncio.run(session._run_llm_gemini()) == "සිංහල පිළිතුරක්."
    assert seen["profile"] == ("claude", server.CLAUDE_MODEL, "si")
    assert "The caller selected Sinhala" in seen["prompt"]
    assert spoken == ["සිංහල පිළිතුරක්."]
    assert (session.lang, session.system_prompt, session.llm_provider, session.model,
            session.tools, copy.deepcopy(session.tools), session.gemini_client,
            session.anthropic_client, session._gemini_thinking_level,
            session._smartpbx_gemini_max_tokens, session._speak_generation,
            session.history, session.full_transcript, session._tts_elevenlabs,
            session._tts_openai, session._tts_gemini_sinhala) == snapshot
    assert session.tools[0]["function_declarations"][0]["parameters"] is not seen["tools"][0]["input_schema"]
    failover_logs = [
        record.getMessage() for record in caplog.records
        if "event=llm_provider_failover" in record.getMessage()
    ]
    assert failover_logs == [
        "smartpbx_media event=llm_provider_failover from=gemini to=claude reason=quota"
    ]
    assert all("hello" not in message and "check_availability" not in message for message in failover_logs)


def test_direct_sinhala_sticky_fallback_uses_injected_client_without_global_key(monkeypatch):
    session, _spoken = _session([], lang="si", smartpbx=True)
    original_tools = copy.deepcopy(GEMINI_SHAPED_TOOLS)
    session.tools = original_tools
    session.anthropic_client = object()
    session._gemini_failover_state["degraded"] = True
    seen = []

    async def run_claude():
        seen.append((session.llm_provider, session.model, session.tools))
        return "සිංහල පිළිතුරක්."

    monkeypatch.setattr(server, "ANTHROPIC_API_KEY", "")
    monkeypatch.setattr(session, "_run_llm_claude", run_claude)
    assert asyncio.run(session._run_llm_gemini()) == "සිංහල පිළිතුරක්."
    assert session.gemini_client.requests == 0
    assert seen[0][0:2] == ("claude", server.CLAUDE_MODEL)
    assert all("function_declarations" not in tool for tool in seen[0][2])
    assert session.tools is original_tools
    assert session.llm_provider == "gemini"


def test_sinhala_fallback_restores_profile_when_claude_fails(monkeypatch):
    """A failed Claude turn restores the profile AND still answers the caller.

    Re-raising here reached `_process_utterance`'s generic handler, which speaks
    the "please wait" filler and then nothing — dead air. The direct SmartPBX
    non-capture path now speaks the shared localized recovery line instead.
    """
    session, spoken = _session([], lang="si", smartpbx=True)
    original_tools = copy.deepcopy(GEMINI_SHAPED_TOOLS)
    session.tools = original_tools
    original_profile = (session.llm_provider, session.model, session.tools)

    async def failing_claude():
        session.tools[0]["input_schema"]["properties"]["temporary"] = {"type": "boolean"}
        raise RuntimeError("claude failure")

    monkeypatch.setattr(session, "_run_llm_claude", failing_claude)
    recovery = asyncio.run(session._run_claude_failover_turn())
    assert recovery and spoken == [recovery]
    assert "සමාවෙන්න" in recovery
    assert (session.llm_provider, session.model, session.tools) == original_profile
    assert session.tools is original_tools
    assert "temporary" not in original_tools[0]["function_declarations"][0]["parameters"]["properties"]


def test_sinhala_fallback_restores_profile_when_claude_is_cancelled(monkeypatch):
    """Cancellation is a lifecycle signal, never a turn outcome: it propagates."""
    session, spoken = _session([], lang="si", smartpbx=True)
    original_tools = copy.deepcopy(GEMINI_SHAPED_TOOLS)
    session.tools = original_tools
    original_profile = (session.llm_provider, session.model, session.tools)

    async def cancelled_claude():
        session.tools[0]["input_schema"]["properties"]["temporary"] = {"type": "boolean"}
        raise asyncio.CancelledError()

    monkeypatch.setattr(session, "_run_llm_claude", cancelled_claude)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(session._run_claude_failover_turn())
    assert spoken == []
    assert (session.llm_provider, session.model, session.tools) == original_profile
    assert session.tools is original_tools
    assert "temporary" not in original_tools[0]["function_declarations"][0]["parameters"]["properties"]


@pytest.mark.parametrize("local_error", [
    ValueError("local parser sentinel"), OSError("local I/O sentinel"), asyncio.TimeoutError(),
])
def test_direct_sinhala_local_error_recovers_without_claude_replay(monkeypatch, local_error):
    session, spoken = _session([], lang="si", smartpbx=True)
    session.gemini_client = FakeFlakyGemini([local_error])
    session.anthropic_client = object()
    claude_calls = 0

    async def run_claude():
        nonlocal claude_calls
        claude_calls += 1
        return "must not run"

    monkeypatch.setattr(session, "_run_llm_claude", run_claude)
    result = asyncio.run(session._run_llm_gemini())
    assert claude_calls == 0
    assert result == spoken[-1]
    assert "sentinel" not in result


def test_direct_sinhala_delivered_partial_audio_never_replays_through_claude(monkeypatch):
    session, spoken = _session(
        [[_text_chunk("පළමු වාක්‍යය. "), _QuotaError()]], lang="si", smartpbx=True,
    )
    session._start_assistant_turn_delivery_tracking()
    claude_calls = 0

    async def delivered_tts(_text, *, sentence=None, turn_generation=None, **_kwargs):
        session._record_delivered_sentence(sentence, turn_generation)
        spoken.append(sentence)

    async def run_claude():
        nonlocal claude_calls
        claude_calls += 1
        return "must not run"

    session._tts_gemini_sinhala = delivered_tts
    session.anthropic_client = object()
    monkeypatch.setattr(session, "_run_llm_claude", run_claude)
    result = asyncio.run(session._run_llm_gemini())

    assert claude_calls == 0
    assert spoken[0] == "පළමු වාක්‍යය."
    assert "පළමු වාක්‍යය." not in spoken[1:]
    assert result.startswith("පළමු වාක්‍යය.")
    assert session.history[0]["content"] == "පළමු වාක්‍යය."


def test_gemini_provider_origin_rejects_boolean_status_codes():
    assert server._gemini_provider_origin_reason(SimpleNamespace(status=True)) is None
    assert server._gemini_provider_origin_reason(SimpleNamespace(status=False)) is None
    assert server._gemini_provider_origin_reason(_QuotaError(status=429)) == "quota"


def test_direct_sinhala_stream_adapter_preserves_smartpbx_timeout():
    """A direct-Sinhala stream timeout must reach its dedicated runner path."""

    async def timed_out_stream():
        raise server._SmartPBXStreamTimeout(
            phase=server._SmartPBXStreamTimeout.PHASE_STALL,
        )
        yield  # pragma: no cover - keeps this an async generator

    async def consume():
        async for _item in server._iter_gemini_provider_deltas(
            timed_out_stream(), mark_provider_errors=True,
        ):
            pass

    with pytest.raises(server._SmartPBXStreamTimeout) as raised:
        asyncio.run(consume())
    assert raised.value.phase == server._SmartPBXStreamTimeout.PHASE_STALL


@pytest.mark.parametrize(
    ("turn", "reason", "private_detail"),
    [
        (httpx.ReadTimeout("private acquisition timeout"), "transport_timeout", "private acquisition timeout"),
        ([httpx.ConnectError("private stream close")], "transport_closed", "private stream close"),
    ],
)
def test_direct_sinhala_sdk_transport_failure_fails_over_with_closed_reason(
    monkeypatch, caplog, turn, reason, private_detail,
):
    session, _spoken = _session([], lang="si", smartpbx=True)
    session.gemini_client = FakeFlakyGemini([turn])
    session.anthropic_client = object()
    seen = []

    async def run_claude():
        seen.append((session.llm_provider, session.model, session.tools))
        return "සිංහල පිළිතුරක්."

    monkeypatch.setattr(session, "_run_llm_claude", run_claude)
    with caplog.at_level("WARNING", logger="server"):
        assert asyncio.run(session._run_llm_gemini()) == "සිංහල පිළිතුරක්."

    assert seen[0][0:2] == ("claude", server.CLAUDE_MODEL)
    failover_logs = [
        record.getMessage() for record in caplog.records
        if "event=llm_provider_failover" in record.getMessage()
    ]
    assert failover_logs == [
        f"smartpbx_media event=llm_provider_failover from=gemini to=claude reason={reason}"
    ]
    assert all(private_detail not in message for message in failover_logs)


def test_direct_sinhala_rejects_an_unapproved_provider_marker(monkeypatch):
    session, spoken = _session([], lang="si", smartpbx=True)
    session.gemini_client = FakeFlakyGemini([
        server._GeminiProviderOriginError("unapproved_reason"),
    ])
    session.anthropic_client = object()
    claude_calls = 0

    async def run_claude():
        nonlocal claude_calls
        claude_calls += 1
        return "must not run"

    monkeypatch.setattr(session, "_run_llm_claude", run_claude)
    result = asyncio.run(session._run_llm_gemini())
    assert claude_calls == 0
    assert result == spoken[-1]


def test_direct_sinhala_local_config_transport_error_does_not_replay_claude(monkeypatch):
    session, spoken = _session([], lang="si", smartpbx=True)
    session.anthropic_client = object()
    claude_calls = 0

    def bad_config(**_kwargs):
        raise httpx.ConnectError("local config transport error")

    async def run_claude():
        nonlocal claude_calls
        claude_calls += 1
        return "must not run"

    monkeypatch.setattr(server, "_build_gemini_config", bad_config)
    monkeypatch.setattr(session, "_run_llm_claude", run_claude)
    result = asyncio.run(session._run_llm_gemini())
    assert claude_calls == 0
    assert result == spoken[-1]


def test_direct_sinhala_local_tts_transport_error_does_not_replay_claude(monkeypatch):
    session, spoken = _session([[_text_chunk("සිංහල වාක්‍යය.")]], lang="si", smartpbx=True)
    session.anthropic_client = object()
    claude_calls = 0

    response_text = "සිංහල වාක්‍යය."

    async def selectively_bad_tts(text, **_kwargs):
        # Model the actual boundary: the response TTS fails, but the
        # independent recovery invocation can still be delivered.  Replacing
        # every TTS call would make recovery impossible and proves nothing
        # about whether this local error replayed via Claude.
        if text == response_text:
            raise httpx.ConnectError("local tts transport error")
        spoken.append(text)

    async def run_claude():
        nonlocal claude_calls
        claude_calls += 1
        return "must not run"

    session._tts_gemini_sinhala = selectively_bad_tts
    monkeypatch.setattr(session, "_run_llm_claude", run_claude)
    result = asyncio.run(session._run_llm_gemini())
    assert claude_calls == 0
    assert response_text not in spoken
    assert spoken
    assert result == spoken[-1]


def test_direct_sinhala_partial_tts_is_cancelled_before_claude_fallback(monkeypatch):
    session, _spoken = _session([], lang="si", smartpbx=True)
    session.gemini_client = FakeFlakyGemini([[
        _text_chunk("පළමු වාක්‍යය. "), _QuotaError(),
    ]])
    session.anthropic_client = object()
    tts_started = asyncio.Event()
    tts_cancelled = False
    tts_tasks = []
    original_start = session._start_smartpbx_round_tts

    async def blocked_tts(*_args, **_kwargs):
        nonlocal tts_cancelled
        tts_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            tts_cancelled = True
            raise

    def start_tts(*args, **kwargs):
        task = original_start(*args, **kwargs)
        if task is not None:
            tts_tasks.append(task)
        return task

    async def run_claude():
        assert tts_started.is_set()
        assert tts_cancelled is True
        assert tts_tasks[0].done() and tts_tasks[0].cancelled()
        assert tts_tasks[0] not in session._smartpbx_deferred_tts_tasks
        return "සිංහල fallback."

    session._tts_gemini_sinhala = blocked_tts
    monkeypatch.setattr(session, "_start_smartpbx_round_tts", start_tts)
    monkeypatch.setattr(session, "_run_llm_claude", run_claude)
    assert asyncio.run(session._run_llm_gemini()) == "සිංහල fallback."


def test_sinhala_fallback_state_is_call_local_while_another_session_stays_gemini(monkeypatch):
    failing, _failing_spoken = _session([], lang="si", smartpbx=True)
    healthy, healthy_spoken = _session([[_text_chunk("healthy Gemini.")]], lang="si", smartpbx=True)
    failing.gemini_client = FakeFlakyGemini([_QuotaError()])
    failing.anthropic_client = object()
    failing_profile = (failing.model, failing.tools, failing.gemini_client, failing.anthropic_client)
    healthy_profile = (healthy.model, healthy.tools, healthy.gemini_client, healthy.anthropic_client)
    claude_calls = 0

    async def run_claude():
        nonlocal claude_calls
        claude_calls += 1
        await asyncio.sleep(0)
        return "සිංහල fallback."

    monkeypatch.setattr(failing, "_run_llm_claude", run_claude)

    async def run_both():
        return await asyncio.gather(failing._run_llm_gemini(), healthy._run_llm_gemini())

    assert asyncio.run(run_both()) == ["සිංහල fallback.", "healthy Gemini."]
    assert claude_calls == 1
    assert healthy.gemini_client.requests == 1
    assert healthy_spoken == ["healthy Gemini."]
    assert failing._gemini_failover_state["consecutive_failovers"] == 1
    assert healthy._gemini_failover_state["consecutive_failovers"] == 0
    assert (failing.model, failing.tools, failing.gemini_client, failing.anthropic_client) == failing_profile
    assert (healthy.model, healthy.tools, healthy.gemini_client, healthy.anthropic_client) == healthy_profile


@pytest.mark.parametrize("failure", [_QuotaError(), ValueError("english local sentinel")])
def test_direct_english_failures_keep_legacy_gemini_failover_behavior(monkeypatch, failure):
    session, spoken = _session([], lang="en", smartpbx=True)
    session.gemini_client = FakeFlakyGemini([failure])
    session.anthropic_client = object()
    claude_calls = 0

    async def run_claude():
        nonlocal claude_calls
        claude_calls += 1
        return "English Claude fallback."

    monkeypatch.setattr(session, "_run_llm_claude", run_claude)
    assert asyncio.run(session._run_llm_gemini()) == "English Claude fallback."
    assert claude_calls == 1
    assert spoken == []
    assert session.lang == "en"
    assert server.LLM_EMPTY_FALLBACKS["si"] not in spoken


def test_direct_sinhala_degraded_event_has_the_closed_schema(monkeypatch, caplog):
    session, _spoken = _session([], lang="si", smartpbx=True)
    session.gemini_client = FakeFlakyGemini([_QuotaError()])
    session.anthropic_client = object()
    monkeypatch.setattr(server, "GEMINI_FAILOVER_STICKY_AFTER", 1)

    async def run_claude():
        return "සිංහල fallback."

    monkeypatch.setattr(session, "_run_llm_claude", run_claude)
    with caplog.at_level("INFO", logger="server"):
        assert asyncio.run(session._run_llm_gemini()) == "සිංහල fallback."
    degraded_logs = [
        record.getMessage() for record in caplog.records
        if "event=llm_provider_degraded" in record.getMessage()
    ]
    assert degraded_logs == ["smartpbx_media event=llm_provider_degraded"]


@pytest.mark.asyncio
async def test_media_stream_sticky_failover_sends_claude_shaped_tools(monkeypatch):
    session, spoken = _session([])
    session.tools = server.get_tools_gemini() or GEMINI_SHAPED_TOOLS
    session.anthropic_client = FakeAnthropicClient([_anthropic_text_delta("recovered.")])
    session.history = [{"role": "user", "content": "hello"}]
    session._gemini_failover_state["degraded"] = True
    monkeypatch.setattr(server, "ANTHROPIC_API_KEY", "present")

    async def _noop_speak(*_args, **_kwargs):
        return None

    session._invoke_speak = _noop_speak
    gemini_tools = list(session.tools)

    result = await session._run_llm_gemini()

    assert result == "recovered."
    call = session.anthropic_client.messages.calls[0]
    assert call["model"] == server.CLAUDE_MODEL
    sent_tools = call["tools"]
    assert sent_tools is not server.NOT_GIVEN
    assert all("function_declarations" not in tool for tool in sent_tools), (
        "Anthropic 400s on Gemini's function_declarations payload"
    )
    assert [tool["name"] for tool in sent_tools] == [
        declaration["name"]
        for entry in gemini_tools
        for declaration in entry["function_declarations"]
    ]
    # The session must be left on its own provider for the next turn.
    assert session.llm_provider == "gemini"
    assert session.tools == gemini_tools
    assert session.model == "gemini-2.5-flash"


@pytest.mark.asyncio
async def test_media_stream_exception_failover_sends_claude_shaped_tools(monkeypatch):
    session, _spoken = _session([])
    session.tools = GEMINI_SHAPED_TOOLS
    session.gemini_client = FakeFlakyGemini([_QuotaError()])
    session.anthropic_client = FakeAnthropicClient([_anthropic_text_delta("recovered.")])
    session.history = [{"role": "user", "content": "hello"}]

    async def _noop_speak(*_args, **_kwargs):
        return None

    session._invoke_speak = _noop_speak

    result = await session._run_llm_gemini()

    assert result == "recovered."
    call = session.anthropic_client.messages.calls[0]
    assert call["model"] == server.CLAUDE_MODEL
    assert [tool["name"] for tool in call["tools"]] == [
        "check_availability", "capture_spoken_number",
    ]
    assert session.tools == GEMINI_SHAPED_TOOLS
    assert session.llm_provider == "gemini"


def test_conversation_relay_sticky_failover_sends_claude_shaped_tools(monkeypatch):
    socket = FakeRelaySocket()
    state = server._init_gemini_failover_state()
    state["degraded"] = True
    monkeypatch.setattr(server, "ANTHROPIC_API_KEY", "present")
    monkeypatch.setattr(server, "_get_anthropic_client", lambda: "anthropic")
    seen: list[list[dict]] = []

    async def _run_claude(*, tools, **_kwargs) -> str:
        seen.append(tools)
        return "claude"

    monkeypatch.setattr(server, "_run_llm_streaming_claude", _run_claude)

    result = asyncio.run(
        server._run_llm_streaming_gemini(
            gemini_client=FakeGemini([]),
            system="sys",
            conversation_history=[{"role": "user", "content": "hi"}],
            tools=GEMINI_SHAPED_TOOLS,
            websocket=socket,
            failover_state=state,
        )
    )

    assert result == "claude"
    assert [tool["name"] for tool in seen[0]] == [
        "check_availability", "capture_spoken_number",
    ]
    assert all("function_declarations" not in tool for tool in seen[0])


def test_conversation_relay_exception_failover_sends_claude_shaped_tools(monkeypatch):
    socket = FakeRelaySocket()
    monkeypatch.setattr(server, "ANTHROPIC_API_KEY", "present")
    monkeypatch.setattr(server, "_get_anthropic_client", lambda: "anthropic")
    seen: list[list[dict]] = []

    async def _run_claude(*, tools, **_kwargs) -> str:
        seen.append(tools)
        return "claude"

    monkeypatch.setattr(server, "_run_llm_streaming_claude", _run_claude)

    result = asyncio.run(
        server._run_llm_streaming_gemini(
            gemini_client=FakeFlakyGemini([_QuotaError()]),
            system="sys",
            conversation_history=[{"role": "user", "content": "hi"}],
            tools=server.get_handover_tools("gemini"),
            websocket=socket,
        )
    )

    assert result == "claude"
    assert [tool["name"] for tool in seen[0]] == ["notify_human_handover"], (
        "the failsafe session must not be handed the full booking tool set"
    )


# --- an empty LATER round must never be replayed on Claude ------------------
# Failover re-runs the whole turn from the truncated history. That is safe only
# while the turn has no side effects yet. If round 1 already executed a tool
# (create_booking!) or spoke, a replay would repeat it — so a later empty round
# takes the canned line instead.

def test_media_stream_empty_round_after_a_tool_ran_does_not_replay_on_claude(monkeypatch):
    session, spoken = _session(
        [
            [_tool_chunk("create_booking", {"guest_name": "Raya"})],
            [_empty_chunk()],
            [_empty_chunk()],
        ]
    )
    session.anthropic_client = FakeAnthropicClient([_anthropic_text_delta("nope")])
    monkeypatch.setattr(server, "ANTHROPIC_API_KEY", "present")
    executed: list[str] = []

    async def execute(name, _arguments):
        executed.append(name)
        return json.dumps({"success": True, "booking_reference": "HH-1"})

    monkeypatch.setattr(server, "execute_tool", execute)

    result = asyncio.run(session._run_llm_gemini())

    fallback = server.LLM_EMPTY_FALLBACKS["en"]
    assert executed == ["create_booking"], "the booking must not be made twice"
    assert session.anthropic_client.messages.calls == [], (
        "a turn with side effects must not be replayed on Claude"
    )
    assert spoken[-1] == fallback
    assert result.endswith(fallback)
    assert session._gemini_failover_state["consecutive_failovers"] == 0


def test_conversation_relay_empty_round_after_a_tool_ran_does_not_replay(monkeypatch):
    socket = FakeRelaySocket()
    client = FakeGemini(
        [
            [_tool_chunk("create_booking", {"guest_name": "Raya"})],
            [_empty_chunk()],
            [_empty_chunk()],
        ]
    )
    monkeypatch.setattr(server, "ANTHROPIC_API_KEY", "present")
    monkeypatch.setattr(server, "_get_anthropic_client", lambda: "anthropic")
    claude_calls = 0

    async def _run_claude(**_kwargs) -> str:
        nonlocal claude_calls
        claude_calls += 1
        return "claude"

    monkeypatch.setattr(server, "_run_llm_streaming_claude", _run_claude)
    executed: list[str] = []

    async def execute(name, _arguments):
        executed.append(name)
        return json.dumps({"success": True, "booking_reference": "HH-1"})

    monkeypatch.setattr(server, "execute_tool", execute)

    result = asyncio.run(
        server._run_llm_streaming_gemini(
            gemini_client=client,
            system="sys",
            conversation_history=[{"role": "user", "content": "book it"}],
            tools=[],
            websocket=socket,
        )
    )

    assert executed == ["create_booking"], "the booking must not be made twice"
    assert claude_calls == 0
    assert result.endswith(server.LLM_EMPTY_FALLBACKS["en"])
    assert socket.tokens()[-1] == server.LLM_EMPTY_FALLBACKS["en"]


def test_media_stream_first_round_empty_still_fails_over(monkeypatch):
    """The safe case must keep working: nothing spoken, no tool, round 1."""
    session, spoken = _session([[_empty_chunk()], [_empty_chunk()]])
    session.anthropic_client = FakeAnthropicClient([_anthropic_text_delta("recovered.")])

    async def _noop_speak(*_args, **_kwargs):
        return None

    session._invoke_speak = _noop_speak

    result = asyncio.run(session._run_llm_gemini())

    assert result == "recovered."
    assert spoken == []
    assert session._gemini_failover_state["consecutive_failovers"] == 1


# --- (e) a successful retry is a clean answer, not a patched-up one --------

_SINHALA_RECOVERY_MARKER = "සමාවෙන්න"  # "sorry"


def test_direct_sinhala_max_tokens_retry_speaks_only_the_retry_answer():
    """A truncated first attempt must leave nothing half-spoken behind it.

    Production saw turn 1 finish `stop_reason=max_tokens` at
    `output_tokens=24` -- the thinking budget ate the visible reply -- and the
    retry then answered normally. Two things must hold across that seam: the
    caller never hears the canned Sinhala recovery line when the retry
    succeeds, and no fragment of the truncated attempt is spoken alongside (or
    in front of) the real answer.
    """
    session, spoken = _session(
        [
            [_text_chunk("පළමු වාක්‍යය. "),
             _empty_chunk("MAX_TOKENS")],
            [_text_chunk("සම්පූර්ණ "
                         "පිළිතුර."),
             _terminal_chunk()],
        ],
        lang="si",
        smartpbx=True,
        terminalize_direct_rounds=False,
    )

    result = asyncio.run(session._run_llm_gemini())

    assert session.gemini_client.requests == 2
    assert spoken == ["සම්පූර්ණ "
                      "පිළිතුර."]
    assert result == "සම්පූර්ණ " \
                     "පිළිතුර."
    # No recovery line, and no surviving fragment of the truncated attempt.
    assert not any(_SINHALA_RECOVERY_MARKER in text for text in spoken)
    assert not any("පළමු" in text for text in spoken)
    # The discarded attempt is not carried into history either.
    assert [message.get("content") for message in session.history] == [
        "සම්පූර්ණ "
        "පිළිතුර."
    ]


def test_direct_sinhala_empty_retry_nudge_is_silent_when_the_retry_answers():
    """The nudge path itself must never speak; only an exhausted retry does."""
    session, spoken = _session(
        [
            [_terminal_chunk()],
            [_text_chunk("පිළිතුර."), _terminal_chunk()],
        ],
        lang="si",
        smartpbx=True,
        terminalize_direct_rounds=False,
    )

    asyncio.run(session._run_llm_gemini())

    assert session.gemini_client.requests == 2
    assert spoken == ["පිළිතුර."]
    assert not any(_SINHALA_RECOVERY_MARKER in text for text in spoken)
    # The nudge reached the retry request only.
    systems = [
        config.get("system_instruction") for config in session.gemini_client.configs
    ]
    assert server.GEMINI_EMPTY_RETRY_NUDGE not in (systems[0] or "")
    assert server.GEMINI_EMPTY_RETRY_NUDGE in (systems[1] or "")


def test_direct_sinhala_exhausted_retry_still_speaks_the_recovery_line():
    """The counterpart: the recovery line is reached when the retry fails too.

    Without this, the tests above would also pass if recovery had been deleted.
    """
    session, spoken = _session(
        [[_empty_chunk("MAX_TOKENS")], [_empty_chunk("MAX_TOKENS")]],
        lang="si",
        smartpbx=True,
        terminalize_direct_rounds=False,
    )

    asyncio.run(session._run_llm_gemini())

    assert session.gemini_client.requests == 2
    assert len(spoken) == 1
    assert _SINHALA_RECOVERY_MARKER in spoken[0]


def test_direct_english_max_tokens_retry_fences_the_truncated_round_audio():
    """The non-batched shape, where the truncated attempt did reach TTS.

    Audio already on the wire cannot be unspoken, so the contract is that the
    stalled generation's queued remainder is cleared and its discarded text
    never becomes history -- the retry's answer stands alone.
    """
    session, spoken = _session(
        [
            [_text_chunk("First sentence. "), _empty_chunk("MAX_TOKENS")],
            [_text_chunk("The real answer."), _terminal_chunk()],
        ],
        lang="en",
        smartpbx=True,
        terminalize_direct_rounds=False,
    )

    from tests.test_smartpbx_server import bind_direct_smartpbx_turn

    async def scenario():
        # The production entrypoint binds the turn before the runner starts;
        # the generation fence is deliberately gated on that ownership token.
        bind_direct_smartpbx_turn(server, session)
        return await session._run_llm_gemini()

    result = asyncio.run(scenario())

    assert session.gemini_client.requests == 2
    assert result == "The real answer."
    assert spoken[-1] == "The real answer."
    # The discarded attempt is fenced off the wire and out of history.
    assert session._media_transport.clears >= 1
    assert [message.get("content") for message in session.history] == [
        "The real answer."
    ]
