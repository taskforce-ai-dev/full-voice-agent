"""Provider-parity transfer tests for the Dialog SmartPBX media pipeline."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest


class FakeTransport:
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


def _tool(name: str, *, reason: str = "guest asked for a person") -> dict[str, object]:
    return {"name": name, "arguments": {"reason": reason} if name == "transfer_to_human" else {}}


def _openai_chunk(*, text: str | None = None, tools: list[dict[str, object]] | None = None):
    tool_calls = None
    if tools is not None:
        tool_calls = [
            SimpleNamespace(
                index=index,
                id=f"tool-{index}",
                function=SimpleNamespace(
                    name=tool["name"], arguments=json.dumps(tool["arguments"])
                ),
            )
            for index, tool in enumerate(tools)
        ]
    return SimpleNamespace(
        choices=[SimpleNamespace(delta=SimpleNamespace(content=text, tool_calls=tool_calls))]
    )


class FakeOpenAI:
    def __init__(self, rounds: list[list[object]]):
        self.rounds = rounds
        self.requests = 0
        self.chat = SimpleNamespace(completions=self)

    async def create(self, **_kwargs):
        self.requests += 1
        round_events = self.rounds.pop(0)

        async def stream():
            for event in round_events:
                yield event

        return stream()


def _gemini_chunk(*, text: str | None = None, tools: list[dict[str, object]] | None = None):
    parts = []
    if text is not None:
        parts.append(SimpleNamespace(text=text, function_call=None))
    for tool in tools or []:
        parts.append(
            SimpleNamespace(
                text=None,
                function_call=SimpleNamespace(name=tool["name"], args=tool["arguments"]),
            )
        )
    return SimpleNamespace(
        candidates=[
            SimpleNamespace(
                finish_reason=None,
                content=SimpleNamespace(parts=parts),
            )
        ]
    )


class FakeGeminiModels:
    def __init__(self, owner):
        self.owner = owner

    async def generate_content_stream(self, **_kwargs):
        self.owner.requests += 1
        round_events = self.owner.rounds.pop(0)

        async def stream():
            for event in round_events:
                yield event

        return stream()


class FakeGemini:
    def __init__(self, rounds: list[list[object]]):
        self.rounds = rounds
        self.requests = 0
        self.aio = SimpleNamespace(models=FakeGeminiModels(self))


def _claude_terminal_events(stop_reason: str, output_tokens: int) -> list[object]:
    """How every real Claude stream ends. The round classifier treats a stream
    that delivered neither `message_delta` nor `message_stop` as aborted, so a
    fixture that omits these describes a dropped connection rather than the
    ordinary round these tests are about."""
    return [
        SimpleNamespace(
            type="message_delta",
            delta=SimpleNamespace(stop_reason=stop_reason),
            usage=SimpleNamespace(output_tokens=output_tokens),
        ),
        SimpleNamespace(type="message_stop"),
    ]


def _claude_tool_events(tools: list[dict[str, object]]) -> list[object]:
    events = []
    for index, tool in enumerate(tools):
        events.extend(
            [
                SimpleNamespace(
                    type="content_block_start",
                    content_block=SimpleNamespace(
                        type="tool_use", id=f"tool-{index}", name=tool["name"]
                    ),
                ),
                SimpleNamespace(
                    type="content_block_delta",
                    delta=SimpleNamespace(
                        type="input_json_delta",
                        partial_json=json.dumps(tool["arguments"]),
                    ),
                ),
                SimpleNamespace(type="content_block_stop"),
            ]
        )
    events.extend(_claude_terminal_events("tool_use", 180))
    return events


def _claude_text_events(text: str) -> list[object]:
    return [
        SimpleNamespace(
            type="content_block_delta",
            delta=SimpleNamespace(type="text_delta", text=text),
        ),
        *_claude_terminal_events("end_turn", 120),
    ]


class FakeClaudeStream:
    def __init__(self, events: list[object]):
        self.events = events

    async def __aenter__(self):
        async def stream():
            for event in self.events:
                yield event

        return stream()

    async def __aexit__(self, *_args):
        return False


class FakeClaudeMessages:
    def __init__(self, owner):
        self.owner = owner

    def stream(self, **_kwargs):
        self.owner.requests += 1
        return FakeClaudeStream(self.owner.rounds.pop(0))


class FakeClaude:
    def __init__(self, rounds: list[list[object]]):
        self.rounds = rounds
        self.requests = 0
        self.messages = FakeClaudeMessages(self)


def _provider_client(provider: str, rounds: list[list[object]]):
    if provider == "openai":
        return FakeOpenAI(rounds)
    if provider == "gemini":
        return FakeGemini(rounds)
    return FakeClaude(rounds)


def _provider_round(provider: str, *, tools=None, text=None) -> list[object]:
    if provider == "openai":
        return [_openai_chunk(tools=tools)] if tools is not None else [_openai_chunk(text=text)]
    if provider == "gemini":
        return [_gemini_chunk(tools=tools)] if tools is not None else [_gemini_chunk(text=text)]
    return _claude_tool_events(tools) if tools is not None else _claude_text_events(text)


def _make_pipeline(server, provider: str, client):
    transport = FakeTransport()
    pipeline = server.MediaStreamSession(
        websocket=None, lang="en", media_transport=transport
    )
    pipeline._smartpbx_transfer_context = object()
    pipeline.tools = []
    if provider == "openai":
        pipeline.client = client
        runner = pipeline._run_llm
    elif provider == "gemini":
        pipeline.gemini_client = client
        runner = pipeline._run_llm_gemini
    else:
        pipeline.anthropic_client = client
        runner = pipeline._run_llm_claude
    return pipeline, transport, runner


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["openai", "gemini", "claude"])
async def test_acknowledged_transfer_stops_same_batch_without_followup_provider_or_media(
    monkeypatch, provider
):
    import server

    client = _provider_client(
        provider,
        [_provider_round(provider, tools=[_tool("transfer_to_human"), _tool("must_not_run")])],
    )
    pipeline, transport, runner = _make_pipeline(server, provider, client)
    reprompt_started = asyncio.Event()

    async def reprompt():
        reprompt_started.set()
        await asyncio.Event().wait()

    pipeline._reprompt_task = asyncio.create_task(reprompt())
    await reprompt_started.wait()
    executed: list[str] = []

    async def execute(name, _arguments):
        executed.append(name)
        if name == "transfer_to_human":
            await pipeline.enter_transfer_pending()
            return json.dumps({"status": "transfer_requested"})
        raise AssertionError("a later same-batch tool must not run after transfer")

    monkeypatch.setattr(server, "execute_tool", execute)

    result = await runner()

    assert result == ""
    assert executed == ["transfer_to_human"]
    assert client.requests == 1
    assert pipeline.transfer_pending is True
    assert transport.clears == 1
    assert transport.audio == []
    assert transport.marks == []
    assert pipeline._reprompt_task is None


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["openai", "gemini", "claude"])
async def test_immediate_transfer_failure_runs_remaining_tool_and_continues_conversation(
    monkeypatch, provider
):
    import server

    client = _provider_client(
        provider,
        [
            _provider_round(
                provider,
                tools=[_tool("transfer_to_human"), _tool("must_not_run")],
            ),
            _provider_round(provider, text="A human is unavailable. I can still help."),
        ],
    )
    pipeline, transport, runner = _make_pipeline(server, provider, client)
    spoken: list[str] = []

    async def tts(text):
        spoken.append(text)

    pipeline._tts_elevenlabs = tts
    executed: list[str] = []

    async def execute(name, _arguments):
        executed.append(name)
        if name == "transfer_to_human":
            return json.dumps({"status": "unavailable"})
        return json.dumps({"status": "completed"})

    monkeypatch.setattr(server, "execute_tool", execute)

    result = await runner()

    assert result == "A human is unavailable. I can still help."
    assert executed == ["transfer_to_human", "must_not_run"]
    assert client.requests == 2
    assert pipeline.transfer_pending is False
    assert transport.clears == 0
    assert spoken == ["A human is unavailable.", "I can still help."]


@pytest.mark.asyncio
async def test_pending_transfer_suppresses_stt_interim_media_tts_and_reprompt():
    import server

    transport = FakeTransport()
    pipeline = server.MediaStreamSession(
        websocket=None, lang="en", media_transport=transport
    )
    pipeline._smartpbx_transfer_context = object()
    pipeline._event_loop = asyncio.get_running_loop()
    pipeline._pending_transcript = "pre-transfer speech"
    pipeline._latest_interim = "pre-transfer interim"
    spoken: list[str] = []

    async def tts(text):
        spoken.append(text)

    pipeline._tts_elevenlabs = tts

    await pipeline.enter_transfer_pending()
    pipeline._on_stt_result("ignored final")
    pipeline._on_stt_interim("ignored interim")
    await pipeline._send_media_audio(b"audio")
    await pipeline._clear_media_audio()
    await pipeline._send_tts_done()
    pipeline._schedule_reprompt()
    await pipeline._speak("must not speak")
    await pipeline._flush_transcript()
    await asyncio.sleep(0)

    assert pipeline.transfer_pending is True
    assert pipeline._pending_transcript == ""
    assert pipeline._latest_interim == ""
    assert pipeline._reprompt_task is None
    assert transport.clears == 1
    assert transport.audio == []
    assert transport.marks == []
    assert spoken == []
