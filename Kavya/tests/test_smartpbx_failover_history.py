"""Provider-swap history shape for the direct SmartPBX Sinhala failover.

The advertised Gemini→Claude technical failover could only ever succeed on a
tool-free conversation: ``self.history`` is written in whichever provider's
shape ran the round, and the runners handed it to the other provider verbatim.
After one ``check_availability`` the history holds OpenAI-shaped
``{"role": "assistant", "content": None, "tool_calls": [...]}`` /
``{"role": "tool", ...}`` entries, which the Anthropic Messages API rejects;
the reverse (Anthropic content-block lists handed to Gemini) is equally broken.
The caller heard the "please wait" filler and then nothing, and the sticky
counter kept routing every later turn to the provider that was rejecting our
own payload — a whole-call outage from one transient Gemini error.

These tests pin the seams a caller can hear:
  (a) a renderer that turns ANY internal entry shape into valid Anthropic input,
  (b) ``_history_to_gemini`` consuming Anthropic block lists,
  (c) the real ``_run_llm_claude`` against a fake Anthropic that rejects what
      the API rejects — so the message shape is validated, not stubbed away,
  (d) a failover turn that fails for our own reason speaks the localized
      recovery line and does NOT push the call into sticky Claude routing,
  (e) the replay-safety contract: a tool already executed is never repeated.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

import server
from tests.test_gemini_streaming import (
    FakeFlakyGemini,
    FakeGemini,
    FakeRelaySocket,
    _QuotaError,
    _session,
    _terminal_chunk,
    _text_chunk,
    _tool_chunk,
)


GEMINI_SHAPED_TOOLS = [
    {
        "function_declarations": [
            {
                "name": "check_availability",
                "description": "check rooms",
                "parameters": {"type": "object", "properties": {}},
            }
        ]
    }
]


# --- a fake Anthropic client that rejects what the real API rejects ---------

class StrictAnthropicRejection(Exception):
    """Stand-in for the Messages API 400 on an invalid ``messages`` payload."""

    def __init__(self, reason: str) -> None:
        self.status_code = 400
        super().__init__(reason)


def assert_valid_anthropic_messages(messages) -> None:
    """Reject every payload shape the Messages API itself rejects."""
    if not isinstance(messages, list) or not messages:
        raise StrictAnthropicRejection("messages: expected a non-empty list")
    if messages[0].get("role") != "user":
        raise StrictAnthropicRejection("messages: first message must use role user")
    open_tool_use: set[str] = set()
    for message in messages:
        if not isinstance(message, dict):
            raise StrictAnthropicRejection("messages: entry must be an object")
        unexpected = set(message) - {"role", "content"}
        if unexpected:
            raise StrictAnthropicRejection(
                f"messages: unexpected field {sorted(unexpected)[0]!r}"
            )
        role = message.get("role")
        if role not in {"user", "assistant"}:
            raise StrictAnthropicRejection(f"messages: unsupported role {role!r}")
        content = message.get("content")
        if content is None:
            raise StrictAnthropicRejection("messages: content must not be null")
        blocks: list = []
        if isinstance(content, str):
            if not content.strip():
                raise StrictAnthropicRejection("messages: content must not be empty")
        elif isinstance(content, list):
            if not content:
                raise StrictAnthropicRejection(
                    "messages: content blocks must not be empty"
                )
            blocks = content
        else:
            raise StrictAnthropicRejection("messages: content must be text or blocks")

        answered: set[str] = set()
        for block in blocks:
            if not isinstance(block, dict):
                raise StrictAnthropicRejection("messages: block must be an object")
            block_type = block.get("type")
            if block_type == "text":
                if not str(block.get("text", "")).strip():
                    raise StrictAnthropicRejection(
                        "messages: text block must not be empty"
                    )
            elif block_type == "tool_use":
                if role != "assistant":
                    raise StrictAnthropicRejection(
                        "messages: tool_use must be an assistant block"
                    )
                if not block.get("id") or not block.get("name"):
                    raise StrictAnthropicRejection(
                        "messages: tool_use needs id and name"
                    )
                if not isinstance(block.get("input"), dict):
                    raise StrictAnthropicRejection(
                        "messages: tool_use input must be an object"
                    )
            elif block_type == "tool_result":
                if role != "user":
                    raise StrictAnthropicRejection(
                        "messages: tool_result must be a user block"
                    )
                tool_use_id = block.get("tool_use_id")
                if not tool_use_id:
                    raise StrictAnthropicRejection(
                        "messages: tool_result needs tool_use_id"
                    )
                if tool_use_id not in open_tool_use:
                    raise StrictAnthropicRejection(
                        "messages: tool_result without a preceding tool_use"
                    )
                answered.add(tool_use_id)
            else:
                raise StrictAnthropicRejection(
                    f"messages: unsupported block type {block_type!r}"
                )

        if role == "user":
            if open_tool_use - answered:
                raise StrictAnthropicRejection(
                    "messages: tool_use ids were found without tool_result blocks"
                )
            open_tool_use = set()
        else:
            open_tool_use = {
                block["id"]
                for block in blocks
                if isinstance(block, dict) and block.get("type") == "tool_use"
            }
    if open_tool_use:
        raise StrictAnthropicRejection(
            "messages: tool_use ids were found without tool_result blocks"
        )


class _StrictStreamContext:
    def __init__(self, events):
        self._events = events

    async def __aenter__(self):
        async def _stream():
            for event in self._events:
                yield event

        return _stream()

    async def __aexit__(self, *_args):
        return False


class _StrictAnthropicMessages:
    def __init__(self, owner):
        self._owner = owner

    def stream(self, **kwargs):
        self._owner.calls.append(kwargs)
        assert_valid_anthropic_messages(kwargs.get("messages"))
        return _StrictStreamContext(self._owner.rounds.pop(0))


class StrictAnthropicClient:
    """Validates every request the way the API does, then replays a round."""

    def __init__(self, rounds):
        self.rounds = list(rounds)
        self.calls: list[dict] = []
        self.messages = _StrictAnthropicMessages(self)


def _claude_terminal(stop_reason="end_turn", output_tokens=40):
    return [
        SimpleNamespace(
            type="message_delta",
            delta=SimpleNamespace(stop_reason=stop_reason),
            usage=SimpleNamespace(output_tokens=output_tokens),
        ),
        SimpleNamespace(type="message_stop"),
    ]


def claude_text_round(text: str):
    return [
        SimpleNamespace(type="message_start"),
        SimpleNamespace(
            type="content_block_delta",
            delta=SimpleNamespace(type="text_delta", text=text),
        ),
        *_claude_terminal(),
    ]


def claude_tool_round(name: str, arguments: dict, *, tool_id="toolu_fake_1"):
    return [
        SimpleNamespace(type="message_start"),
        SimpleNamespace(
            type="content_block_start",
            content_block=SimpleNamespace(type="tool_use", id=tool_id, name=name),
        ),
        SimpleNamespace(
            type="content_block_delta",
            delta=SimpleNamespace(
                type="input_json_delta", partial_json=json.dumps(arguments)
            ),
        ),
        SimpleNamespace(type="content_block_stop"),
        *_claude_terminal(stop_reason="tool_use"),
    ]


# --- B1: the renderer ------------------------------------------------------

def test_openai_shaped_tool_round_renders_as_anthropic_tool_use_and_result():
    history = [
        {"role": "user", "content": "කාමර තියෙනවද?"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "call-1",
                "type": "function",
                "function": {
                    "name": "check_availability",
                    "arguments": json.dumps({"nights": 2}),
                },
            }],
            "gemini_thought_signature": "sig-sentinel",
        },
        {"role": "tool", "tool_call_id": "call-1", "content": '{"rooms": 3}'},
    ]

    messages = server._claude_messages_from_history(history)

    assert_valid_anthropic_messages(messages)
    assert messages[1] == {
        "role": "assistant",
        "content": [{
            "type": "tool_use",
            "id": "call-1",
            "name": "check_availability",
            "input": {"nights": 2},
        }],
    }
    assert messages[2] == {
        "role": "user",
        "content": [{
            "type": "tool_result",
            "tool_use_id": "call-1",
            "content": '{"rooms": 3}',
        }],
    }
    # The Gemini thought signature is call-local and must never cross providers.
    assert "sig-sentinel" not in repr(messages)


def test_renderer_keeps_assistant_text_alongside_its_tool_call():
    history = [
        {"role": "user", "content": "hello"},
        {
            "role": "assistant",
            "content": "Let me check.",
            "tool_calls": [{
                "id": "call-2", "type": "function",
                "function": {"name": "check_availability", "arguments": "{}"},
            }],
        },
        {"role": "tool", "tool_call_id": "call-2", "content": "{}"},
    ]

    messages = server._claude_messages_from_history(history)

    assert_valid_anthropic_messages(messages)
    assert messages[1]["content"][0] == {"type": "text", "text": "Let me check."}
    assert messages[1]["content"][1]["type"] == "tool_use"


def test_renderer_merges_a_multi_tool_batch_into_one_user_message():
    history = [
        {"role": "user", "content": "hello"},
        {
            "role": "assistant", "content": None,
            "tool_calls": [
                {"id": "call-a", "type": "function",
                 "function": {"name": "check_availability",
                              "arguments": '{"room": "A"}'}},
                {"id": "call-b", "type": "function",
                 "function": {"name": "check_availability",
                              "arguments": '{"room": "B"}'}},
            ],
        },
        {"role": "tool", "tool_call_id": "call-a", "content": "{}"},
        {"role": "tool", "tool_call_id": "call-b", "content": "{}"},
    ]

    messages = server._claude_messages_from_history(history)

    assert_valid_anthropic_messages(messages)
    assert len(messages) == 3
    assert [block["tool_use_id"] for block in messages[2]["content"]] == [
        "call-a", "call-b",
    ]


def test_renderer_passes_an_anthropic_history_through_untouched():
    history = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": [
            {"type": "text", "text": "One moment."},
            {"type": "tool_use", "id": "toolu_1", "name": "check_availability",
             "input": {"nights": 1}},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_1", "content": "{}"},
        ]},
        {"role": "assistant", "content": "Two rooms are free."},
    ]

    assert server._claude_messages_from_history(history) is history


def test_renderer_drops_an_unanswered_tool_call_instead_of_inventing_a_result():
    """A tool_use with no result is a guaranteed 400 — and we never fake one."""
    history = [
        {"role": "user", "content": "hello"},
        {
            "role": "assistant", "content": "Checking now.",
            "tool_calls": [{
                "id": "call-orphan", "type": "function",
                "function": {"name": "create_booking", "arguments": "{}"},
            }],
        },
    ]

    messages = server._claude_messages_from_history(history)

    assert_valid_anthropic_messages(messages)
    assert messages == [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": [{"type": "text", "text": "Checking now."}]},
    ]
    assert "tool_result" not in repr(messages)


def test_renderer_drops_an_orphan_tool_result_rather_than_400ing():
    history = [
        {"role": "user", "content": "hello"},
        {"role": "tool", "tool_call_id": "call-gone", "content": '{"rooms": 1}'},
        {"role": "assistant", "content": "Two rooms are free."},
    ]

    messages = server._claude_messages_from_history(history)

    assert_valid_anthropic_messages(messages)
    assert all(message["role"] != "tool" for message in messages)


def test_renderer_carries_a_booking_confirmation_marker_as_a_user_turn():
    history = [
        {"role": "user", "content": "book it"},
        {"role": "system",
         "text": "BOOKING CONFIRMED via create_booking: guest_name=A"},
        {"role": "assistant", "content": "Booked."},
    ]

    messages = server._claude_messages_from_history(history)

    assert_valid_anthropic_messages(messages)
    assert any(
        "BOOKING CONFIRMED" in str(message["content"]) for message in messages
    ), "a committed booking must not vanish from the provider's view"


def test_renderer_skips_empty_and_null_assistant_entries():
    history = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": None},
        {"role": "assistant", "content": ""},
        {"role": "assistant", "content": "Answer."},
    ]

    messages = server._claude_messages_from_history(history)

    assert_valid_anthropic_messages(messages)
    assert messages == [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "Answer."},
    ]


def test_renderer_never_leads_with_an_assistant_turn():
    history = [
        {"role": "assistant", "content": "Trimmed-into mid-conversation."},
        {"role": "user", "content": "hello"},
    ]

    messages = server._claude_messages_from_history(history)

    assert_valid_anthropic_messages(messages)
    assert messages[0]["role"] == "user"


# --- B1: the reverse direction --------------------------------------------

def test_history_to_gemini_consumes_anthropic_tool_blocks():
    history = [
        {"role": "user", "content": "කාමර තියෙනවද?"},
        {"role": "assistant", "content": [
            {"type": "text", "text": "One moment."},
            {"type": "tool_use", "id": "toolu_9", "name": "check_availability",
             "input": {"nights": 2}},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_9",
             "content": '{"rooms": 3}'},
        ]},
        {"role": "assistant", "content": "Three rooms are free."},
    ]

    contents = server._history_to_gemini(history, include_function_call_ids=True)

    model_parts = [
        part for content in contents if content["role"] == "model"
        for part in content["parts"]
    ]
    calls = [part["function_call"] for part in model_parts if "function_call" in part]
    responses = [
        part["function_response"]
        for content in contents if content["role"] == "user"
        for part in content["parts"] if "function_response" in part
    ]
    assert [(call["id"], call["name"], call["args"]) for call in calls] == [
        ("toolu_9", "check_availability", {"nights": 2}),
    ]
    assert [(r["id"], r["name"], r["response"]) for r in responses] == [
        ("toolu_9", "check_availability", {"rooms": 3}),
    ]
    assert {"text": "One moment."} in model_parts
    # No Anthropic block dicts leak into the Gemini payload.
    assert "tool_use" not in repr(contents)


def test_history_to_gemini_reads_anthropic_user_text_blocks():
    history = [
        {"role": "user", "content": [{"type": "text", "text": "hello"}]},
    ]

    assert server._history_to_gemini(history) == [
        {"role": "user", "parts": [{"text": "hello"}]},
    ]


# --- B3(a): a tool round then a real Claude failover ------------------------

def _si_session(rounds, *, model="gemini-3.7-flash"):
    session, spoken = _session(rounds, lang="si", smartpbx=True, model=model)
    session.tools = GEMINI_SHAPED_TOOLS
    return session, spoken


def test_failover_after_a_gemini_tool_round_reaches_claude_with_valid_history(
    monkeypatch,
):
    session, spoken = _si_session([
        [_tool_chunk("check_availability", {"nights": 2}, id="gemini-call-1")],
        [_text_chunk("කාමර දෙකක් තියෙනවා.")],
    ])
    executed: list[tuple[str, dict]] = []

    async def execute(name, arguments):
        executed.append((name, dict(arguments)))
        return json.dumps({"rooms": 2})

    monkeypatch.setattr(server, "execute_tool", execute)
    session.history.append({"role": "user", "content": "කාමර තියෙනවද?"})
    asyncio.run(session._run_llm_gemini())
    assert executed == [("check_availability", {"nights": 2})]

    # Turn 2: Gemini throws a transient provider error; Claude must answer.
    session.gemini_client = FakeFlakyGemini([_QuotaError()])
    session.anthropic_client = StrictAnthropicClient([
        claude_text_round("ඔව්, මම වෙන්කරන්නම්."),
    ])
    monkeypatch.setattr(server, "ANTHROPIC_API_KEY", "present")
    session.history.append({"role": "user", "content": "හොඳයි, වෙන්කරන්න."})

    result = asyncio.run(session._run_llm_gemini())

    assert result == "ඔව්, මම වෙන්කරන්නම්."
    assert spoken[-1] == "ඔව්, මම වෙන්කරන්නම්."
    # The tool ran exactly once — the failover replays no side effect.
    assert executed == [("check_availability", {"nights": 2})]
    sent = session.anthropic_client.calls[0]["messages"]
    assert_valid_anthropic_messages(sent)
    assert any(
        isinstance(message.get("content"), list)
        and any(block.get("type") == "tool_use" for block in message["content"])
        for message in sent
    ), "the prior Gemini tool round must survive into the Claude request"
    # The session's own history is untouched by rendering.
    assert session.history[1]["tool_calls"][0]["id"] == "gemini-call-1"
    assert session.llm_provider == "gemini"


def test_failover_history_is_still_gemini_consumable_afterwards(monkeypatch):
    """B3(b): Claude's failover turn runs a tool; the next Gemini turn works."""
    session, _spoken = _si_session([])
    session.gemini_client = FakeFlakyGemini([_QuotaError()])
    session.anthropic_client = StrictAnthropicClient([
        claude_tool_round("check_availability", {"nights": 3}, tool_id="toolu_777"),
        claude_text_round("කාමර තුනක් තියෙනවා."),
    ])
    executed: list[tuple[str, dict]] = []

    async def execute(name, arguments):
        executed.append((name, dict(arguments)))
        return json.dumps({"rooms": 3})

    monkeypatch.setattr(server, "execute_tool", execute)
    monkeypatch.setattr(server, "ANTHROPIC_API_KEY", "present")
    session.history.append({"role": "user", "content": "කාමර තියෙනවද?"})

    assert asyncio.run(session._run_llm_gemini()) == "කාමර තුනක් තියෙනවා."
    assert executed == [("check_availability", {"nights": 3})]
    assert_valid_anthropic_messages(session.anthropic_client.calls[1]["messages"])

    # Turn 3 back on Gemini: the Anthropic blocks Claude left behind must
    # convert, and the tool result must reach Gemini as a function_response.
    session.gemini_client = FakeGemini([[_text_chunk("හරි."), _terminal_chunk()]])
    session.history.append({"role": "user", "content": "ස්තූතියි."})

    assert asyncio.run(session._run_llm_gemini()) == "හරි."
    contents = session.gemini_client.contents[0]
    responses = [
        part["function_response"]
        for content in contents if content["role"] == "user"
        for part in content["parts"] if "function_response" in part
    ]
    assert [(r["name"], r["response"]) for r in responses] == [
        ("check_availability", {"rooms": 3}),
    ]
    assert executed == [("check_availability", {"nights": 3})]


def test_sticky_routing_keeps_working_across_a_tool_round(monkeypatch):
    """B3(c): once degraded, every turn is Claude — including tool rounds."""
    session, _spoken = _si_session([])
    session._gemini_failover_state["degraded"] = True
    session.anthropic_client = StrictAnthropicClient([
        claude_tool_round("check_availability", {"nights": 1}, tool_id="toolu_a"),
        claude_text_round("එක කාමරයක් තියෙනවා."),
        claude_text_round("සුබ දවසක්."),
    ])

    async def execute(_name, _arguments):
        return json.dumps({"rooms": 1})

    monkeypatch.setattr(server, "execute_tool", execute)
    monkeypatch.setattr(server, "ANTHROPIC_API_KEY", "present")

    session.history.append({"role": "user", "content": "කාමර තියෙනවද?"})
    assert asyncio.run(session._run_llm_gemini()) == "එක කාමරයක් තියෙනවා."
    session.history.append({"role": "user", "content": "ස්තූතියි."})
    assert asyncio.run(session._run_llm_gemini()) == "සුබ දවසක්."

    for call in session.anthropic_client.calls:
        assert_valid_anthropic_messages(call["messages"])
    assert session.gemini_client.requests == 0


def test_failover_across_a_tool_round_logs_no_transcript_or_tool_text(
    monkeypatch, caplog,
):
    """B3(d): the privacy contract holds across the whole provider swap."""
    session, _spoken = _si_session([
        [_tool_chunk("check_availability", {"guest_name": "Amara Perera"},
                     id="gemini-call-2")],
        [_text_chunk("කාමර දෙකක් තියෙනවා.")],
    ])

    async def execute(_name, _arguments):
        return json.dumps({"rooms": 2, "guest_name": "Amara Perera"})

    monkeypatch.setattr(server, "execute_tool", execute)
    monkeypatch.setattr(server, "ANTHROPIC_API_KEY", "present")
    session.history.append({"role": "user", "content": "මට කාමරයක් ඕනේ"})

    with caplog.at_level("DEBUG", logger="server"):
        asyncio.run(session._run_llm_gemini())
        session.gemini_client = FakeFlakyGemini([_QuotaError()])
        session.anthropic_client = StrictAnthropicClient([
            claude_text_round("හරි."),
        ])
        session.history.append({"role": "user", "content": "ස්තූතියි."})
        asyncio.run(session._run_llm_gemini())

    logged = "\n".join(record.getMessage() for record in caplog.records)
    for secret in (
        "Amara Perera", "මට කාමරයක් ඕනේ", "ස්තූතියි", "කාමර දෙකක් තියෙනවා.",
        "guest_name", "nights", "gemini-call-2",
    ):
        assert secret not in logged


def test_conversation_relay_claude_accepts_a_gemini_shaped_tool_history():
    """The relay runner carries the identical defect and the identical fix."""
    client = StrictAnthropicClient([claude_text_round("Two rooms are free.")])
    socket = FakeRelaySocket()
    history = [
        {"role": "user", "content": "any rooms?"},
        {"role": "assistant", "content": None, "tool_calls": [{
            "id": "relay-call-1", "type": "function",
            "function": {"name": "check_availability", "arguments": '{"nights": 2}'},
        }]},
        {"role": "tool", "tool_call_id": "relay-call-1", "content": '{"rooms": 2}'},
        {"role": "user", "content": "book it"},
    ]

    result = asyncio.run(server._run_llm_streaming_claude(
        client=client,
        system="sys",
        conversation_history=history,
        tools=[],
        websocket=socket,
    ))

    assert result == "Two rooms are free."
    assert_valid_anthropic_messages(client.calls[0]["messages"])


# --- B2: a failover turn that fails on our side ----------------------------

def test_a_failed_failover_turn_speaks_the_localized_recovery_line(monkeypatch):
    session, spoken = _si_session([])
    session.gemini_client = FakeFlakyGemini([_QuotaError()])
    session.anthropic_client = object()
    monkeypatch.setattr(server, "ANTHROPIC_API_KEY", "present")

    async def exploding_claude():
        raise TypeError("local sentinel")

    monkeypatch.setattr(session, "_run_llm_claude", exploding_claude)
    session.history.append({"role": "user", "content": "කාමර තියෙනවද?"})

    result = asyncio.run(session._run_llm_gemini())

    assert result and spoken == [result], "the caller must not hear dead air"
    assert "සමාවෙන්න" in result
    assert session.history[-1] == {"role": "assistant", "content": result}


def test_our_own_failover_error_does_not_push_the_call_into_sticky_claude(
    monkeypatch,
):
    session, _spoken = _si_session([])
    session.gemini_client = FakeFlakyGemini([_QuotaError()])
    session.anthropic_client = object()
    monkeypatch.setattr(server, "ANTHROPIC_API_KEY", "present")
    monkeypatch.setattr(server, "GEMINI_FAILOVER_STICKY_AFTER", 1)

    async def exploding_claude():
        raise TypeError("local sentinel")

    monkeypatch.setattr(session, "_run_llm_claude", exploding_claude)
    session.history.append({"role": "user", "content": "කාමර තියෙනවද?"})
    asyncio.run(session._run_llm_gemini())

    assert session._gemini_failover_state["consecutive_failovers"] == 0
    assert session._gemini_failover_state["degraded"] is False


def test_a_failed_sticky_turn_releases_the_call_back_to_gemini(monkeypatch):
    session, _spoken = _si_session([[_text_chunk("හරි."), _terminal_chunk()]])
    session._gemini_failover_state["degraded"] = True
    session.anthropic_client = object()
    monkeypatch.setattr(server, "ANTHROPIC_API_KEY", "present")

    async def exploding_claude():
        raise TypeError("local sentinel")

    monkeypatch.setattr(session, "_run_llm_claude", exploding_claude)
    session.history.append({"role": "user", "content": "කාමර තියෙනවද?"})
    asyncio.run(session._run_llm_gemini())

    assert session._gemini_failover_state["degraded"] is False
    # The next turn is free to try Gemini again rather than dead-ending.
    session.history.append({"role": "user", "content": "ස්තූතියි."})
    assert asyncio.run(session._run_llm_gemini()) == "හරි."


def test_a_cancelled_failover_turn_still_propagates(monkeypatch):
    session, _spoken = _si_session([])

    async def cancelled_claude():
        raise asyncio.CancelledError()

    monkeypatch.setattr(session, "_run_llm_claude", cancelled_claude)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(session._run_claude_failover_turn())


def test_a_failed_failover_turn_after_a_tool_uses_the_tool_started_line(
    monkeypatch,
):
    """Replay safety: a committed side effect never invites a repeat booking."""
    session, spoken = _si_session([])
    session.anthropic_client = object()

    async def half_failed_claude():
        session.history.append({
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "toolu_x",
                         "name": "create_booking", "input": {}}],
        })
        session.history.append({
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "toolu_x",
                         "content": "{}"}],
        })
        raise TypeError("local sentinel")

    monkeypatch.setattr(session, "_run_llm_claude", half_failed_claude)
    session.history.append({"role": "user", "content": "වෙන්කරන්න."})

    result = asyncio.run(session._run_claude_failover_turn())

    assert result == spoken[-1]
    assert "යාවත්කාලීනයක්" in result, "the post-tool recovery line, never a retry ask"
