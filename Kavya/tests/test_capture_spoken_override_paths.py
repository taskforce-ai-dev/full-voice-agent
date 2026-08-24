"""The capture tools must parse what the CALLER said, on every runner.

`capture_spoken_number` / `capture_spoken_name` are deterministic parsers over
the caller's own words. The model routinely paraphrases or partially re-types
what it heard into the `spoken` argument, so every runner overrides that
argument with the raw utterance before executing the tool
(`_override_capture_spoken_argument`).

This matters twice over now that capture mode combines a fragmented dictation
into ONE utterance: the combined text only reaches the parser through this
override. The media-Claude runner was missing it — and `LLM_PROVIDER` defaults
to claude, so that was the production path. These tests pin the override at the
tool-execution boundary for the media-Claude runner and for the
ConversationRelay-Claude runner.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

import server


RAW_DICTATION = "zero seven seven one two three four five six eight"
MODEL_PARAPHRASE = "0771234568"


# --- fake Anthropic streaming client ---------------------------------------

def _text_delta(text: str) -> SimpleNamespace:
    return SimpleNamespace(
        type="content_block_delta",
        delta=SimpleNamespace(type="text_delta", text=text),
    )


def _tool_use_events(name: str, tool_input: dict) -> list[SimpleNamespace]:
    return [
        SimpleNamespace(
            type="content_block_start",
            content_block=SimpleNamespace(type="tool_use", id="tu_1", name=name),
        ),
        SimpleNamespace(
            type="content_block_delta",
            delta=SimpleNamespace(
                type="input_json_delta", partial_json=json.dumps(tool_input)
            ),
        ),
        SimpleNamespace(type="content_block_stop"),
    ]


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


class _FakeMessages:
    def __init__(self, rounds):
        self._rounds = list(rounds)
        self.calls: list[dict] = []

    def stream(self, **kwargs):
        self.calls.append(kwargs)
        events = self._rounds.pop(0) if self._rounds else [_text_delta("All set.")]
        return _FakeStreamContext(events)


class FakeAnthropicClient:
    def __init__(self, rounds):
        self.messages = _FakeMessages(rounds)


class FakeRelaySocket:
    def __init__(self):
        self.sent: list[dict] = []

    async def send_text(self, payload: str) -> None:
        self.sent.append(json.loads(payload))


# --- media-Claude runner (the production path) -----------------------------

@pytest.mark.asyncio
async def test_media_claude_capture_tool_parses_the_raw_combined_utterance(monkeypatch):
    session = server.MediaStreamSession(websocket=None, lang="en", media_transport=None)
    session._smartpbx_transfer_context = object()
    session.tools = []
    session.anthropic_client = FakeAnthropicClient([
        _tool_use_events("capture_spoken_number", {"spoken": MODEL_PARAPHRASE}),
        [_text_delta("Thank you.")],
    ])
    session.history = [{"role": "user", "content": "zero seven seven..."}]
    # What capture mode buffered and dispatched as one utterance.
    session._last_guest_utterance_raw = RAW_DICTATION

    async def _noop_speak(*_args, **_kwargs):
        return None

    session._invoke_speak = _noop_speak
    seen: list[dict] = []

    async def execute(name, arguments):
        seen.append({"name": name, "spoken": arguments.get("spoken")})
        return json.dumps({"status": "captured", "digits": "0771234568", "valid": True})

    monkeypatch.setattr(server, "execute_tool", execute)

    await session._run_llm_claude()

    assert seen == [{"name": "capture_spoken_number", "spoken": RAW_DICTATION}], (
        "the parser must see the caller's words, not the model's paraphrase"
    )


@pytest.mark.asyncio
async def test_media_claude_capture_override_also_covers_spelled_names(monkeypatch):
    session = server.MediaStreamSession(websocket=None, lang="en", media_transport=None)
    session.tools = []
    session.anthropic_client = FakeAnthropicClient([
        _tool_use_events("capture_spoken_name", {"spoken": "Kavya"}),
        [_text_delta("Got it.")],
    ])
    session.history = [{"role": "user", "content": "k a v y a"}]
    session._last_guest_utterance_raw = "k a v y a"

    async def _noop_speak(*_args, **_kwargs):
        return None

    session._invoke_speak = _noop_speak
    seen: list[dict] = []

    async def execute(name, arguments):
        seen.append({"name": name, "spoken": arguments.get("spoken")})
        return json.dumps({"status": "captured", "name": "Kavya"})

    monkeypatch.setattr(server, "execute_tool", execute)

    await session._run_llm_claude()

    assert seen == [{"name": "capture_spoken_name", "spoken": "k a v y a"}]


@pytest.mark.asyncio
async def test_media_claude_leaves_non_capture_tool_arguments_alone(monkeypatch):
    session = server.MediaStreamSession(websocket=None, lang="en", media_transport=None)
    session.tools = []
    session.anthropic_client = FakeAnthropicClient([
        _tool_use_events("check_availability", {"check_in": "2026-09-27"}),
        [_text_delta("Checking.")],
    ])
    session.history = [{"role": "user", "content": "any rooms?"}]
    session._last_guest_utterance_raw = "any rooms on the twenty seventh"

    async def _noop_speak(*_args, **_kwargs):
        return None

    session._invoke_speak = _noop_speak
    seen: list[dict] = []

    async def execute(name, arguments):
        seen.append({"name": name, "arguments": dict(arguments)})
        return json.dumps({"status": "ok"})

    monkeypatch.setattr(server, "execute_tool", execute)

    await session._run_llm_claude()

    assert seen == [
        {"name": "check_availability", "arguments": {"check_in": "2026-09-27"}}
    ], "only the capture tools take the raw-utterance override"


@pytest.mark.asyncio
async def test_media_claude_history_keeps_the_models_own_tool_arguments(monkeypatch):
    """The override feeds the parser; the transcript of the turn stays honest."""
    session = server.MediaStreamSession(websocket=None, lang="en", media_transport=None)
    session.tools = []
    session.anthropic_client = FakeAnthropicClient([
        _tool_use_events("capture_spoken_number", {"spoken": MODEL_PARAPHRASE}),
        [_text_delta("Thank you.")],
    ])
    session.history = [{"role": "user", "content": "zero seven seven..."}]
    session._last_guest_utterance_raw = RAW_DICTATION

    async def _noop_speak(*_args, **_kwargs):
        return None

    session._invoke_speak = _noop_speak

    async def execute(_name, _arguments):
        return json.dumps({"status": "captured", "digits": "0771234568", "valid": True})

    monkeypatch.setattr(server, "execute_tool", execute)

    await session._run_llm_claude()

    tool_use_blocks = [
        block
        for message in session.history
        if isinstance(message.get("content"), list)
        for block in message["content"]
        if isinstance(block, dict) and block.get("type") == "tool_use"
    ]
    assert tool_use_blocks, "the assistant turn must still record its tool call"
    assert tool_use_blocks[0]["input"] == {"spoken": MODEL_PARAPHRASE}


# --- ConversationRelay-Claude runner ---------------------------------------

def test_conversation_relay_claude_capture_tool_parses_the_raw_utterance(monkeypatch):
    socket = FakeRelaySocket()
    client = FakeAnthropicClient([
        _tool_use_events("capture_spoken_number", {"spoken": MODEL_PARAPHRASE}),
        [_text_delta("Thank you.")],
    ])
    seen: list[dict] = []

    async def execute(name, arguments):
        seen.append({"name": name, "spoken": arguments.get("spoken")})
        return json.dumps({"status": "captured", "digits": "0771234568", "valid": True})

    monkeypatch.setattr(server, "execute_tool", execute)

    asyncio.run(
        server._run_llm_streaming_claude(
            client=client,
            system="sys",
            conversation_history=[
                {
                    "role": "user",
                    "content": (
                        f"[Reference context: KB snippets]\n\nGuest: {RAW_DICTATION}"
                    ),
                }
            ],
            tools=[],
            websocket=socket,
        )
    )

    assert seen == [{"name": "capture_spoken_number", "spoken": RAW_DICTATION}], (
        "the reference-context prefix must be stripped and the raw words passed"
    )


# --- one capture turn, one tool invocation ---------------------------------

class _Scheduled:
    def __init__(self, delay, callback):
        self.delay = delay
        self.callback = callback
        self.cancelled = False

    def cancel(self):
        self.cancelled = True


class _RecordingLoop:
    """call_later recorder so the combining window is fired deterministically."""

    def __init__(self):
        self.scheduled: list[_Scheduled] = []

    def call_later(self, delay, callback):
        handle = _Scheduled(delay, callback)
        self.scheduled.append(handle)
        return handle

    @property
    def last(self) -> _Scheduled:
        return self.scheduled[-1]


@pytest.mark.asyncio
async def test_fragments_of_one_dictation_reach_the_real_tool_as_a_single_call(monkeypatch):
    """Genuine STT fragments still combine INSIDE the turn: one dispatch, one tool call.

    Each tool call now stands alone, so a number split across recognizer finals
    can only succeed if capture mode combines the fragments before dispatch and
    the raw-utterance override hands that combined text to the parser.
    """
    import handover
    import tools

    session = server.MediaStreamSession(websocket=None, lang="en", media_transport=None)
    session._smartpbx_transfer_context = object()
    session._event_loop = _RecordingLoop()
    session.tools = []
    session.anthropic_client = FakeAnthropicClient([
        _tool_use_events("capture_spoken_number", {"spoken": MODEL_PARAPHRASE}),
        [_text_delta("Thank you.")],
    ])

    async def _noop_speak(*_args, **_kwargs):
        return None

    session._invoke_speak = _noop_speak
    dispatched: list[str] = []
    executed: list[dict] = []

    async def run_turn(text: str) -> None:
        dispatched.append(text)
        session._last_guest_utterance_raw = text
        session.history = [{"role": "user", "content": text}]
        await session._run_llm_claude()

    session._process_utterance = run_turn

    real_execute = tools.execute_tool

    async def execute(name, arguments):
        result = await real_execute(name, arguments)
        executed.append({"name": name, "spoken": arguments.get("spoken"), "result": json.loads(result)})
        return result

    monkeypatch.setattr(server, "execute_tool", execute)

    token = handover.handover_context.set({})
    try:
        session._enter_capture_mode()
        for fragment in ("zero seven seven", "one two three", "four five six eight"):
            await session._accumulate_transcript(fragment)
            assert dispatched == [], "a fragment must refresh the window, not dispatch"
        session._event_loop.last.callback()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
    finally:
        handover.handover_context.reset(token)

    assert dispatched == ["zero seven seven one two three four five six eight"]
    assert len(executed) == 1, "the combined dictation must cost exactly one tool call"
    assert executed[0]["spoken"] == "zero seven seven one two three four five six eight"
    assert executed[0]["result"]["status"] == "captured"
    assert executed[0]["result"]["digits"] == "0771234568"
    assert executed[0]["result"]["normalized"] == "94771234568"
