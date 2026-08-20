"""RED coverage for direct SmartPBX Claude thinking-only stall recovery.

The streams in this module are deterministic async fakes.  They never call a
provider and never wait on wall-clock time; the timeout guard is replaced with
one that raises the production timeout exception after a scripted event list.
"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from types import SimpleNamespace

import pytest


TIMEOUT_LOG_FIELDS = frozenset({
    "event", "provider", "phase", "tool_executed", "progress", "retrying", "attempt",
    "timeout_ms",
})
TIMEOUT_PROVIDER_VALUES = frozenset({"openai", "gemini", "claude", "unknown"})
TIMEOUT_PROGRESS_VALUES = frozenset({"none", "metadata", "thinking", "text", "tool"})
YIELD_TO_LOOP = object()


class IdleFor:
    """A virtual inter-delta pause consumed by the deterministic guard."""

    def __init__(self, seconds: float) -> None:
        self.seconds = seconds


class RecordingTransport:
    def __init__(self) -> None:
        self.clears = 0

    async def clear_audio(self) -> None:
        self.clears += 1


class ScriptedSource:
    def __init__(self, events, *, timeout_phase: str | None = None) -> None:
        self.events = list(events)
        self.timeout_phase = timeout_phase
        self.observed_stall_timeouts: list[float] = []

    def __aiter__(self):
        async def iterate():
            for event in self.events:
                yield event

        return iterate()


class ScriptedClaudeStream:
    def __init__(self, round_script: ScriptedSource) -> None:
        self.round_script = round_script

    async def __aenter__(self):
        return self.round_script

    async def __aexit__(self, *_args) -> bool:
        return False


class ScriptedClaudeMessages:
    def __init__(self, owner: "ScriptedClaude") -> None:
        self.owner = owner

    def stream(self, **kwargs):
        self.owner.requests.append(kwargs)
        return ScriptedClaudeStream(self.owner.rounds.pop(0))


class ScriptedClaude:
    def __init__(self, rounds: list[ScriptedSource]) -> None:
        self.rounds = rounds
        self.requests: list[dict] = []
        self.messages = ScriptedClaudeMessages(self)


def message_start():
    return SimpleNamespace(
        type="message_start",
        message=SimpleNamespace(usage=SimpleNamespace(input_tokens=11)),
    )


def thinking_events(text: str = "PRIVATE_THINKING_SENTINEL_0771234567\nnext line"):
    return [
        SimpleNamespace(
            type="content_block_start",
            content_block=SimpleNamespace(type="thinking"),
        ),
        SimpleNamespace(
            type="content_block_delta",
            delta=SimpleNamespace(type="thinking_delta", thinking=text),
        ),
    ]


def text_event(text: str):
    return SimpleNamespace(
        type="content_block_delta",
        delta=SimpleNamespace(type="text_delta", text=text),
    )


def terminal_events(stop_reason: str = "end_turn"):
    return [
        SimpleNamespace(
            type="message_delta",
            delta=SimpleNamespace(stop_reason=stop_reason),
            usage=SimpleNamespace(output_tokens=42),
        ),
        SimpleNamespace(type="message_stop"),
    ]


def timeout_round(events, phase: str = "stall") -> ScriptedSource:
    return ScriptedSource(events, timeout_phase=phase)


def completed_round(events) -> ScriptedSource:
    return ScriptedSource(events)


def tool_start_events(*, partial_json: str = '{"guest_name":"Jane'):
    return [
        SimpleNamespace(
            type="content_block_start",
            content_block=SimpleNamespace(
                type="tool_use", id="tool-stall-1", name="create_booking",
            ),
        ),
        SimpleNamespace(
            type="content_block_delta",
            delta=SimpleNamespace(type="input_json_delta", partial_json=partial_json),
        ),
    ]


def completed_tool_events():
    return [
        SimpleNamespace(
            type="content_block_start",
            content_block=SimpleNamespace(
                type="tool_use", id="tool-complete-1", name="create_booking",
            ),
        ),
        SimpleNamespace(
            type="content_block_delta",
            delta=SimpleNamespace(
                type="input_json_delta",
                partial_json='{"guest_name":"Jane"}',
            ),
        ),
        SimpleNamespace(type="content_block_stop"),
        *terminal_events("tool_use"),
    ]


async def deterministic_timeout_guard(
    source, *, initial_timeout: float, stall_timeout: float,
    stall_timeout_getter=None,
):
    first = True
    async for event in source:
        if isinstance(event, IdleFor):
            timeout = (
                initial_timeout
                if first
                else (
                    stall_timeout_getter()
                    if stall_timeout_getter is not None
                    else stall_timeout
                )
            )
            source.observed_stall_timeouts.append(timeout)
            if event.seconds > timeout:
                import server

                raise server._SmartPBXStreamTimeout(
                    phase=(
                        server._SmartPBXStreamTimeout.PHASE_INITIAL
                        if first
                        else server._SmartPBXStreamTimeout.PHASE_STALL
                    )
                )
            continue
        if event is YIELD_TO_LOOP:
            await asyncio.sleep(0)
            continue
        yield event
        first = False
    phase = getattr(source, "timeout_phase", None)
    if phase is not None:
        import server

        raise server._SmartPBXStreamTimeout(phase=phase)


def direct_pipeline(server, client):
    pipeline = server.MediaStreamSession(
        websocket=None,
        lang="en",
        anthropic_client=client,
        media_transport=RecordingTransport(),
        llm_provider="claude",
        model="claude-stall-test-model",
    )
    pipeline._smartpbx_transfer_context = object()
    pipeline.tools = []
    telemetry = server.SmartPBXTurnTelemetry(new_id=lambda: "stall-turn")
    pipeline._turn_telemetry = telemetry
    pipeline._active_smartpbx_turn_id = telemetry.start_turn("final")
    pipeline._assistant_turn_generation = pipeline._speak_generation
    return pipeline


def install_deterministic_stream(monkeypatch, server):
    monkeypatch.setattr(server, "_smartpbx_timeout_guarded_stream", deterministic_timeout_guard)


def install_quiet_filler(monkeypatch, pipeline):
    monkeypatch.setattr(
        pipeline,
        "_start_initial_smartpbx_filler",
        lambda **_kwargs: None,
    )


def install_recording_speak(monkeypatch, pipeline):
    spoken: list[str] = []

    async def speak(text, generation=-1, sentence=None):
        spoken.append(text)

    monkeypatch.setattr(pipeline, "_speak", speak)
    return spoken


def assistant_texts(pipeline):
    return [
        message.get("content")
        for message in pipeline.history
        if message.get("role") == "assistant"
        and isinstance(message.get("content"), str)
    ]


def timeout_log_fields(line: str) -> dict[str, str]:
    return {
        key: value
        for token in line.split()
        if "=" in token
        for key, value in [token.split("=", 1)]
    }


def assert_timeout_log_contract(
    line: str, *, progress: str, phase: str, retrying: str, provider: str = "claude"
):
    fields = timeout_log_fields(line)
    assert set(fields) == TIMEOUT_LOG_FIELDS
    assert fields["event"] == "llm_stream_timeout"
    assert fields["provider"] == provider
    assert fields["provider"] in TIMEOUT_PROVIDER_VALUES
    assert fields["phase"] == phase
    assert fields["tool_executed"] in {"true", "false"}
    assert fields["progress"] == progress
    assert fields["progress"] in TIMEOUT_PROGRESS_VALUES
    assert fields["retrying"] == retrying
    assert fields["retrying"] in {"true", "false"}
    assert re.fullmatch(r"[1-9]", fields["attempt"])
    assert re.fullmatch(r"[0-9]{1,6}", fields["timeout_ms"])
    assert 1_000 <= int(fields["timeout_ms"]) <= 30_000


def thinking_idle(seconds: float) -> IdleFor:
    return IdleFor(seconds)


@pytest.mark.asyncio
async def test_thinking_idle_between_general_and_claude_limits_answers_once_without_retry(
    monkeypatch,
):
    """A verified thinking event gets the Claude-only grace window."""
    import server

    first_round = completed_round([
        message_start(),
        *thinking_events("private reasoning"),
        thinking_idle(9.0),
        text_event("Answer after thinking."),
        *terminal_events(),
    ])
    client = ScriptedClaude([first_round])
    pipeline = direct_pipeline(server, client)
    install_deterministic_stream(monkeypatch, server)
    install_quiet_filler(monkeypatch, pipeline)
    spoken = install_recording_speak(monkeypatch, pipeline)

    result = await pipeline._run_llm_claude()

    assert first_round.observed_stall_timeouts == [12.0]
    assert len(client.requests) == 1
    assert result == "Answer after thinking."
    assert spoken == ["Answer after thinking."]
    assert server.SMARTPBX_LLM_EMPTY_RETRY_RECOVERY_TEXT not in spoken


@pytest.mark.asyncio
async def test_thinking_idle_past_claude_limit_retries_with_general_stall_budget(
    monkeypatch,
):
    import server

    first_round = timeout_round([
        message_start(), *thinking_events("private first attempt"), thinking_idle(13.0)
    ])
    retry_round = completed_round([
        message_start(), thinking_idle(7.0), text_event("Retry answer."), *terminal_events()
    ])
    client = ScriptedClaude([first_round, retry_round])
    pipeline = direct_pipeline(server, client)
    install_deterministic_stream(monkeypatch, server)
    install_quiet_filler(monkeypatch, pipeline)
    spoken = install_recording_speak(monkeypatch, pipeline)

    result = await pipeline._run_llm_claude()

    assert first_round.observed_stall_timeouts == [12.0]
    assert retry_round.observed_stall_timeouts == [8.0]
    assert len(client.requests) == 2
    assert result == "Retry answer."
    assert spoken == ["Retry answer."]
    assert server.SMARTPBX_LLM_EMPTY_RETRY_RECOVERY_TEXT not in spoken


@pytest.mark.asyncio
async def test_thinking_attempts_both_past_limits_recover_once_without_third_request(
    monkeypatch,
):
    import server

    first_round = timeout_round([
        message_start(), *thinking_events("private first attempt"), thinking_idle(13.0)
    ])
    retry_round = timeout_round([
        message_start(), *thinking_events("private retry attempt"), thinking_idle(9.0)
    ])
    client = ScriptedClaude([first_round, retry_round])
    pipeline = direct_pipeline(server, client)
    install_deterministic_stream(monkeypatch, server)
    install_quiet_filler(monkeypatch, pipeline)
    spoken = install_recording_speak(monkeypatch, pipeline)

    result = await pipeline._run_llm_claude()

    assert first_round.observed_stall_timeouts == [12.0]
    assert retry_round.observed_stall_timeouts == [8.0]
    assert len(client.requests) == 2
    assert spoken == [server.SMARTPBX_LLM_EMPTY_RETRY_RECOVERY_TEXT]
    assert result == server.SMARTPBX_LLM_EMPTY_RETRY_RECOVERY_TEXT


@pytest.mark.parametrize(
    ("raw", "general_stall", "expected"),
    [
        (None, 8.0, 12.0),
        ("", 8.0, 12.0),
        ("invalid", 8.0, 12.0),
        ("8", 8.0, 8.0),
        ("9", 8.0, 9.0),
        ("12", 8.0, 12.0),
        ("20", 8.0, 20.0),
        ("8", 16.0, 16.0),
        ("12", 16.0, 16.0),
    ],
)
def test_claude_thinking_stall_timeout_is_blank_safe_defaulted_and_maxed(
    raw, general_stall, expected
):
    import server

    assert (
        server._resolve_smartpbx_claude_thinking_stall_timeout_seconds(
            raw, general_stall
        )
        == expected
    )


@pytest.mark.parametrize(
    ("events", "progress"),
    [
        ([message_start(), thinking_idle(9.0)], "metadata"),
        (
            [
                message_start(),
                text_event("Visible text without a terminal stop"),
                thinking_idle(9.0),
            ],
            "text",
        ),
        ([message_start(), *tool_start_events(), thinking_idle(9.0)], "tool"),
    ],
)
@pytest.mark.asyncio
async def test_first_attempt_non_thinking_stalls_keep_the_general_budget(
    monkeypatch, events, progress,
):
    """Only verified thinking may extend an initial Claude stall window."""
    import server

    first_round = timeout_round(events)
    client = ScriptedClaude([first_round])
    pipeline = direct_pipeline(server, client)
    install_deterministic_stream(monkeypatch, server)
    install_quiet_filler(monkeypatch, pipeline)
    spoken = install_recording_speak(monkeypatch, pipeline)

    result = await pipeline._run_llm_claude()

    assert first_round.observed_stall_timeouts == [8.0], progress
    assert len(client.requests) == 1
    assert spoken == [server.SMARTPBX_LLM_EMPTY_RETRY_RECOVERY_TEXT]
    assert result == server.SMARTPBX_LLM_EMPTY_RETRY_RECOVERY_TEXT


@pytest.mark.asyncio
async def test_thinking_metadata_stall_retries_once_and_delivers_only_retry_answer(monkeypatch):
    import server

    client = ScriptedClaude([
        timeout_round([message_start(), *thinking_events()]),
        completed_round([message_start(), text_event("Retry answer."), *terminal_events()]),
    ])
    pipeline = direct_pipeline(server, client)
    install_deterministic_stream(monkeypatch, server)
    install_quiet_filler(monkeypatch, pipeline)
    spoken = install_recording_speak(monkeypatch, pipeline)

    result = await pipeline._run_llm_claude()

    assert len(client.requests) == 2
    assert result == "Retry answer."
    assert spoken == ["Retry answer."]
    assert server.SMARTPBX_LLM_EMPTY_RETRY_RECOVERY_TEXT not in spoken
    assert assistant_texts(pipeline) == ["Retry answer."]


@pytest.mark.asyncio
async def test_thinking_only_stall_twice_speaks_one_empty_recovery_line(monkeypatch):
    import server

    client = ScriptedClaude([
        timeout_round([message_start(), *thinking_events("first PRIVATE_0771234567")]),
        timeout_round([message_start(), *thinking_events("second PRIVATE_0771234567")]),
    ])
    pipeline = direct_pipeline(server, client)
    install_deterministic_stream(monkeypatch, server)
    install_quiet_filler(monkeypatch, pipeline)
    spoken = install_recording_speak(monkeypatch, pipeline)

    result = await pipeline._run_llm_claude()

    assert len(client.requests) == 2
    assert spoken == [server.SMARTPBX_LLM_EMPTY_RETRY_RECOVERY_TEXT]
    assert result == server.SMARTPBX_LLM_EMPTY_RETRY_RECOVERY_TEXT
    assert assistant_texts(pipeline) == [server.SMARTPBX_LLM_EMPTY_RETRY_RECOVERY_TEXT]


@pytest.mark.asyncio
async def test_metadata_only_stall_retries_once_and_logs_metadata_progress(monkeypatch, caplog):
    import server

    client = ScriptedClaude([
        timeout_round([message_start()]),
        completed_round([message_start(), text_event("Metadata retry answer."), *terminal_events()]),
    ])
    pipeline = direct_pipeline(server, client)
    install_deterministic_stream(monkeypatch, server)
    install_quiet_filler(monkeypatch, pipeline)
    spoken = install_recording_speak(monkeypatch, pipeline)

    with caplog.at_level(logging.WARNING):
        result = await pipeline._run_llm_claude()

    assert len(client.requests) == 2
    assert result == "Metadata retry answer."
    assert spoken == ["Metadata retry answer."]
    timeout_lines = [
        line for line in caplog.messages if "event=llm_stream_timeout" in line
    ]
    assert timeout_lines
    assert_timeout_log_contract(
        timeout_lines[0], progress="metadata", phase="stall", retrying="true"
    )


@pytest.mark.asyncio
async def test_thinking_progress_stays_sticky_after_late_message_start(monkeypatch, caplog):
    import server

    client = ScriptedClaude([
        timeout_round([
            *thinking_events("private reasoning"),
            message_start(),
            YIELD_TO_LOOP,
        ]),
        completed_round([message_start(), text_event("Thinking retry answer."), *terminal_events()]),
    ])
    pipeline = direct_pipeline(server, client)
    install_deterministic_stream(monkeypatch, server)
    install_quiet_filler(monkeypatch, pipeline)
    spoken = install_recording_speak(monkeypatch, pipeline)

    with caplog.at_level(logging.WARNING):
        result = await pipeline._run_llm_claude()

    assert len(client.requests) == 2
    assert result == "Thinking retry answer."
    assert spoken == ["Thinking retry answer."]
    timeout_lines = [
        line for line in caplog.messages if "event=llm_stream_timeout" in line
    ]
    assert timeout_lines
    assert_timeout_log_contract(
        timeout_lines[0], progress="thinking", phase="stall", retrying="true"
    )


@pytest.mark.asyncio
async def test_initial_no_event_timeout_stays_single_attempt_and_is_classified_none(monkeypatch, caplog):
    import server

    client = ScriptedClaude([timeout_round([], phase="initial")])
    pipeline = direct_pipeline(server, client)
    install_deterministic_stream(monkeypatch, server)
    install_quiet_filler(monkeypatch, pipeline)
    spoken = install_recording_speak(monkeypatch, pipeline)

    with caplog.at_level(logging.WARNING):
        result = await pipeline._run_llm_claude()

    assert len(client.requests) == 1
    assert spoken == [server.SMARTPBX_LLM_EMPTY_RETRY_RECOVERY_TEXT]
    assert result == server.SMARTPBX_LLM_EMPTY_RETRY_RECOVERY_TEXT
    timeout_lines = [line for line in caplog.messages if "llm_stream_timeout" in line]
    assert timeout_lines
    assert_timeout_log_contract(
        timeout_lines[0], progress="none", phase="initial", retrying="false"
    )


@pytest.mark.asyncio
async def test_visible_text_stall_is_fenced_without_retry_or_partial_replay(monkeypatch):
    import server

    client = ScriptedClaude([
        timeout_round([message_start(), text_event("Partial visible sentence. "), YIELD_TO_LOOP]),
    ])
    pipeline = direct_pipeline(server, client)
    install_deterministic_stream(monkeypatch, server)
    install_quiet_filler(monkeypatch, pipeline)
    spoken = install_recording_speak(monkeypatch, pipeline)
    started = asyncio.Event()
    cancelled = asyncio.Event()
    lifecycle: list[str] = []

    async def blocked_tts(_text, **_kwargs):
        started.set()
        lifecycle.append("started")
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            cancelled.set()
            lifecycle.append("cancelled")
            raise

    def start_tts(text, *, generation, sentence):
        return asyncio.create_task(blocked_tts(text, generation=generation, sentence=sentence))

    generation = pipeline._speak_generation
    monkeypatch.setattr(pipeline, "_start_smartpbx_round_tts", start_tts)
    await pipeline._run_llm_claude()

    assert len(client.requests) == 1
    assert spoken == [server.SMARTPBX_LLM_EMPTY_RETRY_RECOVERY_TEXT]
    assert started.is_set()
    assert cancelled.is_set()
    assert lifecycle == ["started", "cancelled"]
    assert pipeline._speak_generation > generation
    assert not pipeline._smartpbx_deferred_tts_tasks
    assert assistant_texts(pipeline) == [server.SMARTPBX_LLM_EMPTY_RETRY_RECOVERY_TEXT]


@pytest.mark.asyncio
async def test_visible_text_then_late_thinking_stall_is_fenced_without_retry_or_replay(monkeypatch):
    import server

    client = ScriptedClaude([
        timeout_round([
            message_start(),
            text_event("Partial visible sentence. "),
            *thinking_events("late private reasoning"),
            YIELD_TO_LOOP,
        ]),
        completed_round([message_start(), text_event("Unexpected retry answer."), *terminal_events()]),
    ])
    pipeline = direct_pipeline(server, client)
    install_deterministic_stream(monkeypatch, server)
    install_quiet_filler(monkeypatch, pipeline)
    spoken = install_recording_speak(monkeypatch, pipeline)
    started = asyncio.Event()
    cancelled = asyncio.Event()
    lifecycle: list[str] = []

    async def blocked_tts(_text, **_kwargs):
        started.set()
        lifecycle.append("started")
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            cancelled.set()
            lifecycle.append("cancelled")
            raise

    async def immediate_tts(text, *, generation, sentence):
        await pipeline._speak(text, generation=generation, sentence=sentence)

    def start_tts(text, *, generation, sentence):
        if text == "Partial visible sentence.":
            return asyncio.create_task(
                blocked_tts(text, generation=generation, sentence=sentence)
            )
        return asyncio.create_task(
            immediate_tts(text, generation=generation, sentence=sentence)
        )

    generation = pipeline._speak_generation
    monkeypatch.setattr(pipeline, "_start_smartpbx_round_tts", start_tts)
    await pipeline._run_llm_claude()

    assert len(client.requests) == 1
    assert spoken == [server.SMARTPBX_LLM_EMPTY_RETRY_RECOVERY_TEXT]
    assert started.is_set()
    assert cancelled.is_set()
    assert lifecycle == ["started", "cancelled"]
    assert pipeline._speak_generation > generation
    assert not pipeline._smartpbx_deferred_tts_tasks
    assert assistant_texts(pipeline) == [server.SMARTPBX_LLM_EMPTY_RETRY_RECOVERY_TEXT]


@pytest.mark.asyncio
async def test_tool_use_start_stall_never_retries_or_executes_partial_tool(monkeypatch):
    import server

    client = ScriptedClaude([timeout_round([message_start(), *tool_start_events()])])
    pipeline = direct_pipeline(server, client)
    install_deterministic_stream(monkeypatch, server)
    install_quiet_filler(monkeypatch, pipeline)
    spoken = install_recording_speak(monkeypatch, pipeline)
    executed = []

    async def must_not_execute(*args):
        executed.append(args)
        raise AssertionError("partial Claude tool input must not execute")

    monkeypatch.setattr(server, "execute_tool", must_not_execute)
    await pipeline._run_llm_claude()

    assert len(client.requests) == 1
    assert executed == []
    assert spoken == [server.SMARTPBX_LLM_EMPTY_RETRY_RECOVERY_TEXT]
    assert assistant_texts(pipeline) == [server.SMARTPBX_LLM_EMPTY_RETRY_RECOVERY_TEXT]
    assert not any(
        block.get("type") == "tool_use"
        for message in pipeline.history
        if isinstance(message.get("content"), list)
        for block in message["content"]
    )


@pytest.mark.asyncio
async def test_completed_tool_then_later_stall_keeps_post_tool_recovery_without_replay(monkeypatch):
    import json
    import server

    client = ScriptedClaude([
        completed_round([message_start(), *completed_tool_events()]),
        timeout_round([message_start(), *thinking_events("later PRIVATE_0771234567")]),
    ])
    pipeline = direct_pipeline(server, client)
    install_deterministic_stream(monkeypatch, server)
    install_quiet_filler(monkeypatch, pipeline)
    spoken = install_recording_speak(monkeypatch, pipeline)
    executed = []

    async def execute(name, arguments):
        executed.append((name, dict(arguments)))
        return json.dumps({"status": "ok"})

    monkeypatch.setattr(server, "execute_tool", execute)
    await pipeline._run_llm_claude()

    assert len(client.requests) == 2
    assert executed == [("create_booking", {"guest_name": "Jane"})]
    assert spoken == [
        server.TOOL_FILLERS["create_booking"],
        server.SMARTPBX_LLM_TOOL_STARTED_RECOVERY_TEXT,
    ]
    assert spoken.count(server.SMARTPBX_LLM_TOOL_STARTED_RECOVERY_TEXT) == 1
    assert server.SMARTPBX_LLM_EMPTY_RETRY_RECOVERY_TEXT not in spoken
    assert assistant_texts(pipeline) == [server.SMARTPBX_LLM_TOOL_STARTED_RECOVERY_TEXT]
    assistant_tool_messages = [
        message for message in pipeline.history
        if message.get("role") == "assistant"
        and any(
            block.get("type") == "tool_use"
            for block in message.get("content", [])
            if isinstance(block, dict)
        )
    ]
    assert len(assistant_tool_messages) == 1
    tool_use = next(
        block for block in assistant_tool_messages[0]["content"]
        if block.get("type") == "tool_use"
    )
    assert tool_use == {
        "type": "tool_use",
        "id": "tool-complete-1",
        "name": "create_booking",
        "input": {"guest_name": "Jane"},
    }
    tool_result_messages = [
        message for message in pipeline.history
        if message.get("role") == "user"
        and any(
            block.get("type") == "tool_result"
            for block in message.get("content", [])
            if isinstance(block, dict)
        )
    ]
    assert len(tool_result_messages) == 1
    tool_result = next(
        block for block in tool_result_messages[0]["content"]
        if block.get("type") == "tool_result"
    )
    assert tool_result["tool_use_id"] == tool_use["id"]
    assert tool_result["content"] == json.dumps({"status": "ok"})


@pytest.mark.asyncio
async def test_barge_in_during_first_stall_fence_suppresses_retry_speech_history_and_tasks(monkeypatch):
    import server

    client = ScriptedClaude([
        timeout_round([message_start(), *thinking_events("stale PRIVATE_0771234567")]),
        completed_round([message_start(), text_event("stale retry answer."), *terminal_events()]),
    ])
    pipeline = direct_pipeline(server, client)
    install_deterministic_stream(monkeypatch, server)
    install_quiet_filler(monkeypatch, pipeline)
    spoken = install_recording_speak(monkeypatch, pipeline)
    fence_entered = asyncio.Event()
    release_fence = asyncio.Event()
    original_fence = pipeline._smartpbx_fence_stalled_generation

    async def paused_fence(*, tts_tasks, gen):
        fence_entered.set()
        await release_fence.wait()
        return await original_fence(tts_tasks=tts_tasks, gen=gen)

    monkeypatch.setattr(pipeline, "_smartpbx_fence_stalled_generation", paused_fence)
    monkeypatch.setattr(server, "retrieve_context", lambda _text: "")
    task = asyncio.create_task(pipeline._process_utterance_bound("barge-in guest"))
    await fence_entered.wait()
    await pipeline._handle_bargein()
    release_fence.set()
    await task

    assert len(client.requests) == 1
    assert spoken == []
    assert assistant_texts(pipeline) == []
    assert not pipeline._smartpbx_deferred_tts_tasks


@pytest.mark.asyncio
async def test_teardown_during_first_stall_fence_leaves_no_stale_answer_or_owned_tasks(monkeypatch):
    import server

    client = ScriptedClaude([
        timeout_round([message_start(), *thinking_events("stale PRIVATE_0771234567")]),
        completed_round([message_start(), text_event("stale retry answer."), *terminal_events()]),
    ])
    pipeline = direct_pipeline(server, client)
    install_deterministic_stream(monkeypatch, server)
    install_quiet_filler(monkeypatch, pipeline)
    spoken = install_recording_speak(monkeypatch, pipeline)
    fence_entered = asyncio.Event()
    release_fence = asyncio.Event()
    original_fence = pipeline._smartpbx_fence_stalled_generation

    async def paused_fence(*, tts_tasks, gen):
        fence_entered.set()
        await release_fence.wait()
        return await original_fence(tts_tasks=tts_tasks, gen=gen)

    monkeypatch.setattr(pipeline, "_smartpbx_fence_stalled_generation", paused_fence)
    monkeypatch.setattr(server, "retrieve_context", lambda _text: "")
    task = asyncio.create_task(pipeline._process_utterance_bound("teardown guest"))
    await fence_entered.wait()
    pipeline._finalize_smartpbx_turns()
    release_fence.set()
    await task

    assert len(client.requests) == 1
    assert spoken == []
    assert assistant_texts(pipeline) == []
    assert not pipeline._smartpbx_deferred_tts_tasks
    assert pipeline._active_smartpbx_turn_id is None


@pytest.mark.asyncio
async def test_timeout_telemetry_is_closed_bounded_and_cannot_leak_thinking_text(monkeypatch, caplog):
    import server

    sentinel = "RAW_THINKING_PHONE_0771234567\nRAW_NEWLINE_SENTINEL"
    client = ScriptedClaude([
        timeout_round([message_start(), *thinking_events(sentinel)]),
        completed_round([message_start(), text_event("safe answer."), *terminal_events()]),
    ])
    pipeline = direct_pipeline(server, client)
    install_deterministic_stream(monkeypatch, server)
    install_quiet_filler(monkeypatch, pipeline)
    install_recording_speak(monkeypatch, pipeline)

    with caplog.at_level(logging.WARNING):
        await pipeline._run_llm_claude()

    timeout_lines = [line for line in caplog.messages if "llm_stream_timeout" in line]
    assert timeout_lines
    assert_timeout_log_contract(
        timeout_lines[0], progress="thinking", phase="stall", retrying="true"
    )
    assert sentinel not in caplog.text
    assert "RAW_NEWLINE_SENTINEL" not in caplog.text


@pytest.mark.asyncio
async def test_stream_timeout_unknown_provider_is_bounded_and_privacy_safe(caplog):
    import server

    pipeline = direct_pipeline(server, ScriptedClaude([]))
    sentinel = "claude\nPHONE_SENTINEL_0771234567"

    with caplog.at_level(logging.WARNING):
        await pipeline._smartpbx_handle_stream_timeout(
            server._SmartPBXStreamTimeout(phase="stall"),
            provider=sentinel,
            tool_executed=False,
            gen=pipeline._speak_generation,
            full_text="",
            timeout_ms=12_000,
            recover=False,
        )

    timeout_lines = [line for line in caplog.messages if "llm_stream_timeout" in line]
    assert timeout_lines
    assert_timeout_log_contract(
        timeout_lines[0], progress="none", phase="stall", retrying="false", provider="unknown"
    )
    assert sentinel not in caplog.text
    assert "PHONE_SENTINEL_0771234567" not in caplog.text


@pytest.mark.asyncio
async def test_normal_thinking_answer_remains_enabled_by_omitting_thinking_parameter(monkeypatch):
    import server

    client = ScriptedClaude([
        completed_round([
            message_start(),
            *thinking_events("private reasoning"),
            text_event("Normal answer."),
            *terminal_events(),
        ]),
    ])
    pipeline = direct_pipeline(server, client)
    install_deterministic_stream(monkeypatch, server)
    install_quiet_filler(monkeypatch, pipeline)
    spoken = install_recording_speak(monkeypatch, pipeline)

    assert await pipeline._run_llm_claude() == "Normal answer."
    assert len(client.requests) == 1
    assert "thinking" not in client.requests[0]
    assert spoken == ["Normal answer."]


def test_runbook_timeout_contract_declares_closed_progress_and_bounded_fields():
    runbook = Path(__file__).parents[1].joinpath("SMARTPBX_RUNBOOK.md").read_text()

    assert "llm_stream_timeout" in runbook
    assert "event=llm_stream_timeout" in runbook
    for field in TIMEOUT_LOG_FIELDS - {"event"}:
        assert field in runbook
    for value in ("none", "metadata", "thinking", "text", "tool"):
        assert value in runbook
    assert "phase" in runbook
    assert "initial" in runbook
    assert "stall" in runbook
    lowered = runbook.lower()
    assert "thinking text" in lowered
    assert "raw provider" in lowered
