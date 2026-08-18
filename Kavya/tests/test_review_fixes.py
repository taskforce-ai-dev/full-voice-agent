"""Tests for the consolidated pre-merge review fixes (2026-08-14).

  Fix 1 (BLOCKER): the Gemini->Claude failover in the outer `except Exception`
  handler of both `_run_llm_gemini` (Media Streams) and
  `_run_llm_streaming_gemini` (ConversationRelay) must never truncate history
  and replay a turn once a tool has already executed this turn — replaying
  after e.g. `create_booking` committed would duplicate the booking.

  Fix 2 (MAJOR): capture-name/capture-number/keypad flows are excluded from
  the Phase B empty-retry-nudge / stream-timeout-guard / shared-recovery
  policy (spec section 5 carve-out) via
  `_is_direct_smartpbx_english_non_capture()`.

  Fix 3 (MAJOR): a residual Azure STT SDK-thread callback (or an endpointing
  timer already armed) must not create a new turn after
  `KavyaSmartPBXSession._finish_once` has torn the pipeline down.

  Fix 4 (MAJOR): an in-flight tool call whose result is discarded because
  ownership was lost (barge-in OR the teardown race) must not be a silent
  success — it is now observable via one bounded
  `turn_stage stage="late_tool_completion"` telemetry event.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

import server


# ---------------------------------------------------------------------------
# Shared fakes — small and self-contained, mirroring the pattern already used
# across test_gemini_streaming.py / test_smartpbx_provider_handover.py.
# ---------------------------------------------------------------------------

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


class FakeToolError(Exception):
    """A real (non-empty-response) provider failure, e.g. a 429."""

    def __init__(self, message: str = "rate limited", status: int = 429):
        super().__init__(message)
        self.status = status


def _gemini_part(*, text=None, function_call=None):
    return SimpleNamespace(
        text=text, function_call=function_call, thought=False, thought_signature=None,
    )


def _gemini_chunk(*parts, finish_reason=None):
    return SimpleNamespace(
        candidates=[
            SimpleNamespace(finish_reason=finish_reason, content=SimpleNamespace(parts=list(parts)))
        ]
    )


def _gemini_text(text: str):
    return _gemini_chunk(_gemini_part(text=text))


def _gemini_tool(name: str, args: dict | None = None):
    return _gemini_chunk(_gemini_part(function_call=SimpleNamespace(name=name, args=args or {})))


def _gemini_empty(finish_reason: str = "MAX_TOKENS"):
    return _gemini_chunk(finish_reason=finish_reason)


class FakeFlakyGeminiModels:
    """Each entry in ``owner.turns`` is either an Exception to raise on that
    call, or a list of chunks to stream."""

    def __init__(self, owner):
        self.owner = owner

    async def generate_content_stream(self, **kwargs):
        self.owner.requests += 1
        self.owner.calls_seen.append(kwargs)
        behavior = self.owner.turns.pop(0)
        if isinstance(behavior, BaseException):
            raise behavior

        async def stream():
            for chunk in behavior:
                yield chunk

        return stream()


class FakeFlakyGemini:
    def __init__(self, turns):
        self.turns = list(turns)
        self.requests = 0
        self.calls_seen: list[dict] = []
        self.aio = SimpleNamespace(models=FakeFlakyGeminiModels(self))


class FakeRelaySocket:
    def __init__(self):
        self.sent: list[dict] = []

    async def send_text(self, payload: str) -> None:
        self.sent.append(json.loads(payload))

    def tokens(self) -> list[str]:
        return [msg["token"] for msg in self.sent if msg.get("token")]


def _media_stream_session(*, gemini_turns, capture_mode: bool = False):
    """A direct-SmartPBX-English Gemini MediaStreamSession, TTS captured."""
    session = server.MediaStreamSession(
        websocket=None, lang="en", llm_provider="gemini",
        model="gemini-2.5-flash", media_transport=FakeTransport(),
    )
    session.tools = []
    session.gemini_client = FakeFlakyGemini(gemini_turns)
    session._smartpbx_transfer_context = object()
    session._capture_mode_active = capture_mode

    spoken: list[str] = []

    async def tts(text, **_kwargs):
        spoken.append(text)

    session._tts_elevenlabs = tts
    session._tts_openai = tts
    return session, spoken


def _openai_chunk(*, text=None, tools=None):
    tool_calls = None
    if tools is not None:
        tool_calls = [
            SimpleNamespace(
                index=index,
                id=f"tool-{index}",
                function=SimpleNamespace(name=tool["name"], arguments=json.dumps(tool["arguments"])),
            )
            for index, tool in enumerate(tools)
        ]
    return SimpleNamespace(
        choices=[SimpleNamespace(delta=SimpleNamespace(content=text, tool_calls=tool_calls))]
    )


class FakeOpenAI:
    def __init__(self, rounds):
        self.rounds = list(rounds)
        self.requests = 0
        self.messages_seen: list[list[dict] | None] = []
        self.chat = SimpleNamespace(completions=self)

    async def create(self, **kwargs):
        self.requests += 1
        self.messages_seen.append(kwargs.get("messages"))
        round_events = self.rounds.pop(0)

        async def stream():
            for event in round_events:
                yield event

        return stream()


def _openai_session(rounds, *, capture_mode: bool):
    session = server.MediaStreamSession(
        websocket=None, lang="en", llm_provider="openai",
        model="gpt-4o", media_transport=FakeTransport(),
    )
    session.tools = []
    session.client = FakeOpenAI(rounds)
    session._smartpbx_transfer_context = object()
    session._capture_mode_active = capture_mode

    spoken: list[str] = []

    async def tts(text, **_kwargs):
        spoken.append(text)

    session._tts_elevenlabs = tts
    session._tts_openai = tts
    return session, spoken


# ---------------------------------------------------------------------------
# Fix 1 (BLOCKER) — Gemini failover must never replay a tool-started turn
# ---------------------------------------------------------------------------

def test_fix1_media_stream_gemini_tool_executed_then_exception_skips_replay(monkeypatch):
    """round 0 executes a tool; round 1 raises a real (non-empty) exception.

    History must not be truncated back past the committed tool call, Claude
    must never be asked to replay the turn, and the caller hears the
    post-tool-start recovery line.
    """
    session, spoken = _media_stream_session(
        gemini_turns=[
            [_gemini_tool("create_booking", {"nights": 2})],
            FakeToolError(),
        ],
    )
    executed: list[str] = []

    async def execute(name, _arguments):
        executed.append(name)
        return json.dumps({"status": "booked", "confirmation": "ABC123"})

    monkeypatch.setattr(server, "execute_tool", execute)
    # A configured client is exactly what the pre-fix code needed to replay
    # the turn — present so a regression would actually be exercised.
    session.anthropic_client = object()

    async def _must_not_replay():
        raise AssertionError("Claude must never replay a turn after a tool executed")

    monkeypatch.setattr(session, "_run_llm_claude", _must_not_replay)

    result = asyncio.run(session._run_llm_gemini())

    assert executed == ["create_booking"], "the tool must execute exactly once — no duplicate booking"
    tool_call_entries = [m for m in session.history if m.get("role") == "assistant" and m.get("tool_calls")]
    tool_result_entries = [m for m in session.history if m.get("role") == "tool"]
    assert len(tool_call_entries) == 1, "the committed tool call must survive — no truncation"
    assert len(tool_result_entries) == 1, "the committed tool result must survive — no truncation"
    assert result.endswith(server.SMARTPBX_LLM_TOOL_STARTED_RECOVERY_TEXT)
    assert spoken[-1] == server.SMARTPBX_LLM_TOOL_STARTED_RECOVERY_TEXT


def test_fix1_media_stream_gemini_exception_with_no_tool_still_fails_over(monkeypatch):
    """Preserve existing (load-bearing, tested) behaviour: an exception before
    any tool has executed still fails over to Claude normally."""
    session, _spoken = _media_stream_session(gemini_turns=[FakeToolError()])
    session.anthropic_client = object()

    async def _run_claude():
        return "claude answered the turn"

    monkeypatch.setattr(session, "_run_llm_claude", _run_claude)

    result = asyncio.run(session._run_llm_gemini())

    assert result == "claude answered the turn"
    assert session._gemini_failover_state["consecutive_failovers"] == 1


def test_fix1_conversation_relay_gemini_tool_executed_then_exception_skips_replay(monkeypatch):
    """Same guarantee on the second call site: `_run_llm_streaming_gemini`
    (ConversationRelay). This path has no direct-SmartPBX branch, so the
    non-replay recovery is the existing per-language canned fallback."""
    socket = FakeRelaySocket()
    client = FakeFlakyGemini([
        [_gemini_tool("create_booking", {"nights": 2})],
        FakeToolError(),
    ])
    executed: list[str] = []

    async def execute(name, _arguments):
        executed.append(name)
        return json.dumps({"status": "booked"})

    monkeypatch.setattr(server, "execute_tool", execute)
    monkeypatch.setattr(server, "ANTHROPIC_API_KEY", "present")

    async def _must_not_replay(*_args, **_kwargs):
        raise AssertionError("Claude must never replay a turn after a tool executed")

    monkeypatch.setattr(server, "_run_llm_streaming_claude", _must_not_replay)

    history: list[dict] = [{"role": "user", "content": "book two nights"}]
    result = asyncio.run(
        server._run_llm_streaming_gemini(
            gemini_client=client,
            system="sys",
            conversation_history=history,
            tools=[],
            websocket=socket,
        )
    )

    assert executed == ["create_booking"]
    tool_call_entries = [m for m in history if m.get("role") == "assistant" and m.get("tool_calls")]
    tool_result_entries = [m for m in history if m.get("role") == "tool"]
    assert len(tool_call_entries) == 1, "the committed tool call must survive — no truncation"
    assert len(tool_result_entries) == 1, "the committed tool result must survive — no truncation"
    fallback = server.LLM_EMPTY_FALLBACKS["en"]
    assert result.endswith(fallback)
    assert history[-1] == {"role": "assistant", "content": fallback}


def test_fix1_conversation_relay_gemini_exception_with_no_tool_still_fails_over(monkeypatch):
    socket = FakeRelaySocket()
    client = FakeFlakyGemini([FakeToolError()])
    monkeypatch.setattr(server, "ANTHROPIC_API_KEY", "present")
    monkeypatch.setattr(server, "_get_anthropic_client", lambda: "anthropic")
    claude_calls = 0

    async def _run_claude(**_kwargs):
        nonlocal claude_calls
        claude_calls += 1
        return "claude"

    monkeypatch.setattr(server, "_run_llm_streaming_claude", _run_claude)

    result = asyncio.run(
        server._run_llm_streaming_gemini(
            gemini_client=client,
            system="sys",
            conversation_history=[{"role": "user", "content": "hi"}],
            tools=[],
            websocket=socket,
        )
    )

    assert result == "claude"
    assert claude_calls == 1


# ---------------------------------------------------------------------------
# Fix 2 (MAJOR) — capture mode is excluded from the Phase B gates
# ---------------------------------------------------------------------------

def test_fix2_capture_mode_empty_response_keeps_pre_phase_b_silence():
    """Capture mode must not get the new retry-with-nudge attempt, and an
    exhausted empty response must not speak the new shared recovery line —
    matching whatever a non-"direct" empty response already did.

    Pre-Phase-B (and still true for any non-direct-SmartPBX call today), one
    empty round with no text and no tool call ends the turn immediately —
    the round loop only continues via the tool-call ``continue`` path, so a
    single empty response never advances to a second round. Only the
    SmartPBX-direct retry-nudge policy adds a second attempt (still within
    round 0, via ``max_attempts``); capture mode must not get that either.
    So the whole turn costs exactly one request, not one per
    ``MAX_TOOL_ROUNDS``.
    """
    session, spoken = _openai_session([[_openai_chunk()]], capture_mode=True)

    result = asyncio.run(session._run_llm())

    assert session.client.requests == 1, (
        "capture mode must not get the extra empty-retry attempt, "
        "and a single empty round must not loop for extra rounds"
    )
    assert spoken == [], "capture mode must not speak the new shared recovery line"
    assert result == ""
    for messages in session.client.messages_seen:
        assert not any(
            isinstance(m, dict) and m.get("content") == server.SMARTPBX_EMPTY_RETRY_NUDGE
            for m in (messages or [])
        ), "the retry nudge must never be injected during capture mode"


def test_fix2_control_non_capture_direct_smartpbx_empty_response_gets_new_recovery():
    """Control: without capture mode, the same session DOES get the new
    retry-then-recovery policy — proves the capture-mode test above is
    actually exercising the carve-out, not a no-op."""
    session, spoken = _openai_session([[_openai_chunk()], [_openai_chunk()]], capture_mode=False)

    result = asyncio.run(session._run_llm())

    assert session.client.requests == 2, "direct SmartPBX (non-capture) gets the one retry"
    assert spoken == [server.SMARTPBX_LLM_EMPTY_RETRY_RECOVERY_TEXT]
    assert result.endswith(server.SMARTPBX_LLM_EMPTY_RETRY_RECOVERY_TEXT)


def test_fix2_capture_mode_disables_the_openai_stream_timeout_guard(monkeypatch):
    def _must_not_be_used(*_args, **_kwargs):
        raise AssertionError("the timeout guard must not wrap the stream in capture mode")

    monkeypatch.setattr(server, "_smartpbx_timeout_guarded_stream", _must_not_be_used)

    session, spoken = _openai_session([[_openai_chunk(text="Room seven oh one.")]], capture_mode=True)

    result = asyncio.run(session._run_llm())

    assert spoken == ["Room seven oh one."]
    assert result == "Room seven oh one."


def test_fix2_capture_mode_disables_the_gemini_stream_timeout_guard(monkeypatch):
    def _must_not_be_used(*_args, **_kwargs):
        raise AssertionError("the timeout guard must not wrap the Gemini stream in capture mode")

    monkeypatch.setattr(server, "_smartpbx_timeout_guarded_stream", _must_not_be_used)

    session, spoken = _media_stream_session(
        gemini_turns=[[_gemini_text("Room seven oh one.")]], capture_mode=True,
    )

    result = asyncio.run(session._run_llm_gemini())

    assert spoken == ["Room seven oh one."]
    assert result == "Room seven oh one."


def test_fix2_capture_mode_gemini_empty_response_keeps_canned_fallback(monkeypatch):
    """Gemini's own empty-retry attempt count is unaffected (it already
    applies to every session, not just direct-SmartPBX), but which recovery
    text is ultimately spoken must still respect the capture carve-out."""
    session, spoken = _media_stream_session(
        gemini_turns=[[_gemini_empty()], [_gemini_empty()]], capture_mode=True,
    )
    # Isolate the recovery-text choice from Gemini's separate double-empty
    # failover mechanism (not part of this carve-out).
    monkeypatch.setattr(server, "GEMINI_FAILOVER_TO_CLAUDE", False)

    result = asyncio.run(session._run_llm_gemini())

    fallback = server.LLM_EMPTY_FALLBACKS["en"]
    assert spoken == [fallback]
    assert result.endswith(fallback)


# ---------------------------------------------------------------------------
# Fix 3 (MAJOR) — no new turn/timer after teardown
# ---------------------------------------------------------------------------

def test_fix3_azure_stt_running_guard_blocks_post_stop_callbacks():
    calls: list[tuple[str, str]] = []

    def on_final(text):
        calls.append(("final", text))

    def on_interim(text):
        calls.append(("interim", text))

    stream = server.AzureSTTStream(on_final_result=on_final, on_interim_result=on_interim, lang="en")
    assert stream._running is False, "never started"

    # The guard must be checked before ``evt`` is ever touched.
    stream._on_recognizing(None)
    stream._on_recognized(None)

    assert calls == []


def test_fix3_torn_down_pipeline_drops_transcript_work_silently():
    session = server.MediaStreamSession(websocket=None, lang="en", media_transport=None)
    session._smartpbx_transfer_context = object()

    session._finalize_smartpbx_turns()
    assert session._smartpbx_torn_down is True

    asyncio.run(session._accumulate_transcript("late fragment"))
    asyncio.run(session._set_transcript_interim("late interim"))
    asyncio.run(session._flush_transcript())

    assert session._pending_transcript == ""
    assert session._committed_transcript == ""
    assert session._latest_interim == ""
    assert session._endpointing_handle is None
    assert session._utterance_dispatched is False, "no new turn was armed/dispatched"


def test_fix3_finalize_is_idempotent_and_sets_torn_down_even_without_telemetry():
    """_finalize_smartpbx_turns must set the flag even when no turn telemetry
    was ever constructed (early-return branch) — the guard is defense in
    depth, not conditioned on telemetry having started."""
    session = server.MediaStreamSession(websocket=None, lang="en", media_transport=None)
    assert session._turn_telemetry is None

    session._finalize_smartpbx_turns()
    assert session._smartpbx_torn_down is True

    # Calling it again (a delayed runner's own teardown path) must not raise.
    session._finalize_smartpbx_turns()
    assert session._smartpbx_torn_down is True


# ---------------------------------------------------------------------------
# Fix 4 (MAJOR) — a late tool completion becomes observable, not silent
# ---------------------------------------------------------------------------

def test_fix4_late_tool_completion_after_ownership_loss_emits_bounded_telemetry(monkeypatch):
    session = server.MediaStreamSession(websocket=None, lang="en", media_transport=FakeTransport())
    session._smartpbx_transfer_context = object()
    # Simulate teardown (or a newer turn) having already moved state on:
    # the in-flight runner's captured turn_id no longer matches.
    session._active_smartpbx_turn_id = "turn-new"

    events: list[dict] = []
    monkeypatch.setattr(
        server, "_emit_smartpbx_turn_telemetry",
        # Matches the codebase's established emit convention (see
        # SmartPBXTurnTelemetry.start_turn/mark/finish): callers always pass
        # ``event=`` as a keyword alongside the positional label, so the
        # fake must accept the positional under a different name -- taking
        # it as ``event`` collides with the keyword and raises "multiple
        # values for argument 'event'".
        lambda _event, **fields: events.append(dict(fields)),
    )

    runner = server._SmartPBXRunnerContext(
        turn_id="turn-stale", dropped_frame_baseline=0,
        speak_generation=session._speak_generation, raw_utterance="",
    )
    token = server._smartpbx_runner_context.set(runner)
    try:
        owns = session._current_smartpbx_runner_owns_shared_state(tool_executed=True)
    finally:
        server._smartpbx_runner_context.reset(token)

    assert owns is False, "a stale runner must never be treated as still owning state"
    late_events = [e for e in events if e.get("stage") == "late_tool_completion"]
    assert len(late_events) == 1, "the discarded tool result must be observable, not silent"
    assert late_events[0]["turn_id"] == "turn-stale"
    # Fixed enum shape, no payloads — no transcript/tool-argument/error text.
    assert set(late_events[0]) == {"event", "session_trace_id", "turn_id", "stage"}


def test_fix4_ownership_loss_without_a_tool_stays_silent(monkeypatch):
    """An ordinary barge-in before any tool ran is not newsworthy — only a
    completed tool's discard is."""
    session = server.MediaStreamSession(websocket=None, lang="en", media_transport=FakeTransport())
    session._smartpbx_transfer_context = object()
    session._active_smartpbx_turn_id = "turn-new"

    events: list[dict] = []
    monkeypatch.setattr(
        server, "_emit_smartpbx_turn_telemetry",
        lambda event, **fields: events.append({"event": event, **fields}),
    )

    runner = server._SmartPBXRunnerContext(
        turn_id="turn-stale", dropped_frame_baseline=0,
        speak_generation=session._speak_generation, raw_utterance="",
    )
    token = server._smartpbx_runner_context.set(runner)
    try:
        owns = session._current_smartpbx_runner_owns_shared_state(tool_executed=False)
    finally:
        server._smartpbx_runner_context.reset(token)

    assert owns is False
    assert events == []


def test_fix4_tool_completes_after_teardown_races_ahead_is_discarded_but_visible(monkeypatch):
    """End-to-end: teardown races ahead while a tool is in flight. The result
    is discarded (never committed to history, nothing spoken) exactly as an
    ordinary barge-in already discards a result — but now it is observable."""
    session, spoken = _media_stream_session(
        gemini_turns=[[_gemini_tool("create_booking", {"nights": 1})]],
    )
    turn_id = "turn-1"
    session._active_smartpbx_turn_id = turn_id
    runner = server._SmartPBXRunnerContext(
        turn_id=turn_id, dropped_frame_baseline=0,
        speak_generation=session._speak_generation, raw_utterance="book a room",
    )

    events: list[dict] = []
    monkeypatch.setattr(
        server, "_emit_smartpbx_turn_telemetry",
        # See the matching comment in the fix4_late_tool_completion test above:
        # the codebase's established emit convention always passes ``event=``
        # as a keyword alongside the positional label, so the fake must not
        # name its positional parameter ``event`` or the call collides.
        lambda _event, **fields: events.append(dict(fields)),
    )

    async def execute(_name, _arguments):
        # The teardown race: _finish_once tears the pipeline down while this
        # await is still in flight (see Fix-4's comment in smartpbx_session.py
        # for why this is not cancelled instead).
        session._finalize_smartpbx_turns()
        return json.dumps({"status": "booked", "confirmation": "XYZ"})

    monkeypatch.setattr(server, "execute_tool", execute)

    async def _run():
        token = server._smartpbx_runner_context.set(runner)
        try:
            return await session._run_llm_gemini()
        finally:
            server._smartpbx_runner_context.reset(token)

    result = asyncio.run(_run())

    assert result == "", "a runner that lost ownership must not commit to history or speak"
    assert not any(m.get("role") == "tool" for m in session.history), "the booking result must not be committed"
    # The pre-tool filler ("I'm creating your reservation now.") is spoken
    # while this runner still legitimately owns the turn -- ownership is only
    # lost inside the awaited execute_tool() call, after the filler already
    # went out (see test_direct_english_tool_failure_uses_one_filler_and_
    # opaque_recovery for the same filler-before-tool-await shape). What must
    # NOT happen is any *outcome* text once the result comes back discarded.
    assert spoken == [server.TOOL_FILLERS["create_booking"]], (
        "no recovery/outcome text after the discard -- only the pre-tool filler"
    )
    late_events = [e for e in events if e.get("stage") == "late_tool_completion"]
    assert len(late_events) == 1, "the discarded success must be observable, not silent"
    assert late_events[0]["turn_id"] == turn_id


# ---------------------------------------------------------------------------
# PR-266 review round 2, item 4 — Gemini->Claude failover filler ownership
# ---------------------------------------------------------------------------
#
# When Gemini fails immediately (round 0, no tool) and Claude failover runs,
# the filler started for the Gemini round must not be orphaned (still
# pending, later fires over Claude's speech) or double-started.


class _GatedClaudeStream:
    """Anthropic-shaped streaming context manager whose body doesn't start
    yielding events until ``release`` is set -- lets a test observe the
    (adopted) filler actually firing before any real content arrives."""

    def __init__(self, events: list, release: asyncio.Event):
        self.events = events
        self.release = release

    async def __aenter__(self):
        async def gen():
            await self.release.wait()
            for event in self.events:
                yield event

        return gen()

    async def __aexit__(self, *_args):
        return False


class _GatedClaudeMessages:
    def __init__(self, stream_obj: _GatedClaudeStream):
        self._stream_obj = stream_obj
        self.requests = 0

    def stream(self, **_kwargs):
        self.requests += 1
        return self._stream_obj


class _GatedClaudeClient:
    def __init__(self, events: list, release: asyncio.Event):
        self.messages = _GatedClaudeMessages(_GatedClaudeStream(events, release))


def _claude_text_events(text: str) -> list:
    """One spoken text delta plus the terminal metadata every real Claude
    stream ends with. The round classifier reads a stream that delivered
    neither `message_delta` nor `message_stop` as aborted, so without these the
    failover round would be discarded as a transport failure instead of
    answering the turn."""
    return [
        SimpleNamespace(
            type="content_block_delta",
            delta=SimpleNamespace(type="text_delta", text=text),
        ),
        SimpleNamespace(
            type="message_delta",
            delta=SimpleNamespace(stop_reason="end_turn"),
            usage=SimpleNamespace(output_tokens=120),
        ),
        SimpleNamespace(type="message_stop"),
    ]


@pytest.mark.asyncio
async def test_fix5_gemini_immediate_exception_failover_adopts_filler_fires_once(monkeypatch):
    """Gemini raises immediately at round 0 (no tool) -- the filler it armed
    must transfer to the Claude failover round instead of being orphaned.

    Claude's own content is gated behind ``release`` so the filler has time
    to actually fire (for real, not just get armed) before any real content
    exists. Without the fix, Claude's round 0 starts a SECOND filler timer
    on top of the still-running Gemini one -- both fire independently,
    doubling the filler line. With the fix, there is exactly one controller,
    so it fires at most once and is cleanly cancelled by Claude's first
    delta with no overlap.
    """
    filler_delay = 0.03
    monkeypatch.setattr(server, "SMARTPBX_INITIAL_FILLER_DELAY_SECONDS", filler_delay)

    session, spoken = _media_stream_session(gemini_turns=[FakeToolError()])
    release_claude_content = asyncio.Event()
    session.anthropic_client = _GatedClaudeClient(
        _claude_text_events("Claude answered the turn. "), release_claude_content
    )

    runner_task = asyncio.create_task(session._run_llm_gemini())

    # Give the (adopted) filler time to actually fire -- well past its delay
    # -- while Claude's content is still gated behind release_claude_content.
    await asyncio.sleep(filler_delay * 4)
    assert not runner_task.done(), "Claude's round should still be waiting on gated content"
    assert spoken.count(server.SMARTPBX_INITIAL_FILLER_TEXT) == 1, (
        "the filler must fire at most once -- a second, orphaned Gemini-round "
        "controller would double it"
    )

    release_claude_content.set()
    result = await asyncio.wait_for(runner_task, timeout=5.0)

    assert "Claude answered the turn." in result
    # No overlap: the filler line is fully spoken and gone before Claude's
    # first real sentence appears.
    filler_index = spoken.index(server.SMARTPBX_INITIAL_FILLER_TEXT)
    claude_index = spoken.index("Claude answered the turn.")
    assert filler_index < claude_index
    # Still exactly one filler line in the whole turn.
    assert spoken.count(server.SMARTPBX_INITIAL_FILLER_TEXT) == 1
    # No orphaned filler task left running after the turn completes.
    filler = session._smartpbx_initial_filler
    assert filler is None or filler._task is None or filler._task.done()


class _GatedFailureGeminiModels(FakeFlakyGeminiModels):
    """Like FakeFlakyGeminiModels, but the failing call doesn't raise until
    ``gate`` is set -- lets a test let the Gemini-round filler actually speak
    BEFORE the exception (and therefore the failover decision) happens."""

    def __init__(self, owner, gate: asyncio.Event):
        super().__init__(owner)
        self._gate = gate

    async def generate_content_stream(self, **kwargs):
        await self._gate.wait()
        return await super().generate_content_stream(**kwargs)


@pytest.mark.asyncio
async def test_fix5_gemini_immediate_exception_failover_retires_already_spoken_filler(
    monkeypatch,
):
    """If the Gemini-round filler already spoke by the time the (delayed)
    exception hits, ``_run_claude_failover_turn`` must retire it explicitly
    (``_finish_initial_smartpbx_filler``) rather than silently leave the
    stale, already-spoken controller referenced -- the "cancel/await it
    before Claude starts speaking" branch of item 4, for the case where
    adoption (content hasn't arrived) does not apply.
    """
    filler_delay = 0.01
    monkeypatch.setattr(server, "SMARTPBX_INITIAL_FILLER_DELAY_SECONDS", filler_delay)

    session, spoken = _media_stream_session(gemini_turns=[FakeToolError()])
    gate = asyncio.Event()
    session.gemini_client.aio.models = _GatedFailureGeminiModels(session.gemini_client, gate)
    release_claude_content = asyncio.Event()
    session.anthropic_client = _GatedClaudeClient(
        _claude_text_events("Claude answered the turn. "), release_claude_content
    )

    retired: list[object] = []
    original_finish = session._finish_initial_smartpbx_filler

    async def spy_finish(controller):
        if controller is not None:
            retired.append(controller)
        await original_finish(controller)

    monkeypatch.setattr(session, "_finish_initial_smartpbx_filler", spy_finish)

    runner_task = asyncio.create_task(session._run_llm_gemini())
    # Let the Gemini-round filler actually fire before the exception (still
    # gated behind `gate`) ever reaches the failover decision.
    await asyncio.sleep(filler_delay * 5)
    assert spoken.count(server.SMARTPBX_INITIAL_FILLER_TEXT) == 1
    stale_filler = session._smartpbx_initial_filler
    assert stale_filler is not None and stale_filler.spoke

    gate.set()  # now let the Gemini exception fire -> failover
    release_claude_content.set()
    result = await asyncio.wait_for(runner_task, timeout=5.0)

    assert "Claude answered the turn." in result
    # The already-spoken Gemini-round controller was explicitly retired
    # (cancel-and-await, even though its task was already done) rather than
    # silently dropped -- exactly the "cancel/await before Claude starts
    # speaking" branch item 4 requires for the already-spoken case.
    assert stale_filler in retired
    # Still fired only once -- the retired controller and Claude's own fresh
    # one (if it later fires too) are never the SAME line firing twice.
    assert spoken.count(server.SMARTPBX_INITIAL_FILLER_TEXT) == 1


# ---------------------------------------------------------------------------
# PR-270 — SmartPBX filler handoff
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_smartpbx_pms_tool_runs_while_its_filler_drains_and_result_queues_after(
    monkeypatch,
):
    """A quick PMS result must not chop its already-started wait filler.

    The tool must begin while the short specialized filler is delivering, but
    the next provider round's result cannot speak until that delivery barrier
    has drained.  This models the real SmartPBX Gemini runner; OpenAI and
    Claude use the same direct-path filler branch.
    """
    session, _spoken = _media_stream_session(gemini_turns=[
        [_gemini_tool("check_availability", {"nights": 2})],
        [_gemini_text("Two suites are free.")],
    ])
    # Isolate the PMS filler: a pending initial filler is cancelled by the
    # tool delta before it can speak, exactly as a fast tool request requires.
    monkeypatch.setattr(server, "SMARTPBX_INITIAL_FILLER_DELAY_SECONDS", 60.0)

    filler_started = asyncio.Event()
    filler_release = asyncio.Event()
    tool_started = asyncio.Event()
    timeline: list[str] = []
    filler = server.TOOL_FILLERS["check_availability"]

    async def tts(text, **_kwargs):
        if text == filler:
            timeline.append("filler_started")
            filler_started.set()
            await filler_release.wait()
            timeline.append("filler_drained")
        else:
            timeline.append(f"answer:{text}")

    async def execute(_name, _arguments):
        timeline.append("tool_started")
        tool_started.set()
        return json.dumps({"status": "ok"})

    session._tts_elevenlabs = tts
    session._tts_openai = tts
    monkeypatch.setattr(server, "execute_tool", execute)

    turn = asyncio.create_task(session._run_llm_gemini())
    try:
        await asyncio.wait_for(filler_started.wait(), timeout=1)
        await asyncio.wait_for(tool_started.wait(), timeout=0.2)
        assert "filler_drained" not in timeline
        assert not any(item.startswith("answer:") for item in timeline)

        filler_release.set()
        result = await asyncio.wait_for(turn, timeout=2)

        assert result.endswith("Two suites are free.")
        assert timeline == [
            "filler_started", "tool_started", "filler_drained",
            "answer:Two suites are free.",
        ]
    finally:
        filler_release.set()
        await asyncio.gather(turn, return_exceptions=True)
