"""Anthropic system block layout for prompt caching.

The production fix is to keep the stable base prompt cacheable by separating it
into its own cached text block and appending the booking-slot note (if any) without
cache_control.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import server


class _FakeWebSocket:
    def __init__(self) -> None:
        self.payloads: list[dict] = []

    async def send_text(self, payload: str) -> None:
        self.payloads.append(json.loads(payload))


class _FakeStreamContext:
    def __init__(self, events: list[SimpleNamespace]):
        self._events = events

    async def __aenter__(self):
        async def _stream():
            for event in self._events:
                yield event

        return _stream()

    async def __aexit__(self, *_args):  # noqa: B027
        return False


class _FakeMessages:
    def __init__(self, events: list[SimpleNamespace]):
        self.calls: list[dict] = []
        self._events = events

    def stream(self, **kwargs) -> _FakeStreamContext:
        self.calls.append(kwargs)
        return _FakeStreamContext(self._events)


class _FakeAnthropicClient:
    def __init__(self, events: list[SimpleNamespace]):
        self.messages = _FakeMessages(events)


def _text_delta(text: str) -> SimpleNamespace:
    return SimpleNamespace(
        type="content_block_delta",
        delta=SimpleNamespace(type="text_delta", text=text),
    )


@pytest.mark.asyncio
async def test_media_stream_claude_cache_control_blocks_split_slot_note():
    session = server.MediaStreamSession(websocket=None, lang="en", media_transport=None)
    session.anthropic_client = _FakeAnthropicClient([_text_delta("done")])
    session.tools = []
    session.history = [{"role": "user", "content": "Hello"}]
    session._booking_slots = {"guest_name": "Raya", "check_in": "2026-09-27"}

    async def _noop_speak(*_args, **_kwargs):
        return None

    session._invoke_speak = _noop_speak

    await session._run_llm_claude()

    system_blocks = session.anthropic_client.messages.calls[0]["system"]
    assert len(system_blocks) == 2
    assert system_blocks[0] == {
        "type": "text",
        "text": session.system_prompt,
        "cache_control": {"type": "ephemeral"},
    }
    assert system_blocks[1]["type"] == "text"
    assert system_blocks[1]["text"] == session._booking_slots_note()
    assert "cache_control" not in system_blocks[1]


@pytest.mark.asyncio
async def test_media_stream_claude_cache_control_blocks_skip_empty_slot_note():
    session = server.MediaStreamSession(websocket=None, lang="en", media_transport=None)
    session.anthropic_client = _FakeAnthropicClient([_text_delta("done")])
    session.tools = []
    session.history = [{"role": "user", "content": "Hello"}]

    async def _noop_speak(*_args, **_kwargs):
        return None

    session._invoke_speak = _noop_speak

    await session._run_llm_claude()

    system_blocks = session.anthropic_client.messages.calls[0]["system"]
    assert len(system_blocks) == 1
    assert system_blocks[0] == {
        "type": "text",
        "text": session.system_prompt,
        "cache_control": {"type": "ephemeral"},
    }


@pytest.mark.asyncio
async def test_ws_conversation_claude_cache_control_blocks_split_slots_note(monkeypatch):
    base_prompt = "Base prompt for ws conversation."
    note = f"{server._BOOKING_SLOTS_NOTE_PREFIX}- guest name: Example"
    websocket = _FakeWebSocket()

    client = _FakeAnthropicClient([_text_delta("reply")])
    monkeypatch.setattr(server, "LLM_PROVIDER", "claude")

    await server._stream_llm_turn(
        system=base_prompt + note,
        conversation_history=[{"role": "user", "content": "hello"}],
        tools=[],
        websocket=websocket,
        lang="en",
        anthropic_client=client,
        gemini_client=None,
        openai_client=None,
        transcript_sink=None,
    )

    system_blocks = client.messages.calls[0]["system"]
    assert len(system_blocks) == 2
    assert system_blocks[0] == {
        "type": "text",
        "text": base_prompt,
        "cache_control": {"type": "ephemeral"},
    }
    assert system_blocks[1] == {
        "type": "text",
        "text": note,
    }


@pytest.mark.asyncio
async def test_ws_conversation_claude_cache_control_blocks_skip_empty_slots_note(monkeypatch):
    base_prompt = "Base prompt for ws conversation."
    websocket = _FakeWebSocket()

    client = _FakeAnthropicClient([_text_delta("reply")])
    monkeypatch.setattr(server, "LLM_PROVIDER", "claude")

    await server._stream_llm_turn(
        system=base_prompt,
        conversation_history=[{"role": "user", "content": "hello"}],
        tools=[],
        websocket=websocket,
        lang="en",
        anthropic_client=client,
        gemini_client=None,
        openai_client=None,
        transcript_sink=None,
    )

    system_blocks = client.messages.calls[0]["system"]
    assert len(system_blocks) == 1
    assert system_blocks[0] == {
        "type": "text",
        "text": base_prompt,
        "cache_control": {"type": "ephemeral"},
    }
