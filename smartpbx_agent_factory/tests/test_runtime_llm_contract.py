"""Production-shaped behavioral specification for the candidate LLM lane.

These tests intentionally load the generated-template source directly so the
candidate stays independently executable before any renderer approval.
"""

from __future__ import annotations

import asyncio
import importlib.machinery
import importlib.util
from contextlib import AbstractAsyncContextManager
from pathlib import Path

import pytest


def _lane():
    template = Path(__file__).parents[1] / "template_v1" / "runtime" / "llm_adapters.py.tmpl"
    loader = importlib.machinery.SourceFileLoader("candidate_llm_lane", str(template))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class _ClaudeContext(AbstractAsyncContextManager):
    def __init__(self, events):
        self._events = events

    async def __aenter__(self):
        return _events(self._events)

    async def __aexit__(self, *_args):
        return False


class _ClaudeClient:
    def __init__(self, attempts):
        self.attempts = iter(attempts)

    def open_stream(self, _request):
        return _ClaudeContext(next(self.attempts))


class _GeminiClient:
    def __init__(self, attempts):
        self.attempts = iter(attempts)

    async def open_stream(self, _request):
        return _events(next(self.attempts))


async def _events(events):
    for item in events:
        if isinstance(item, tuple):
            await asyncio.sleep(item[0])
            item = item[1]
        yield item


def _adapter(module, *, claude=(), gemini=(), timeouts=None):
    config = module.LLMRuntimeConfig(
        claude_model="claude-fixture",
        gemini_model="gemini-fixture",
        max_output_tokens=256,
        timeouts=timeouts or module.StreamTimeouts(),
    )
    return module.InquiryOnlyLLMAdapter(
        claude_client=_ClaudeClient(claude), gemini_client=_GeminiClient(gemini), config=config
    )


async def _events_for(adapter, request):
    return [event async for event in adapter.stream_response(request)]


def _request(module, provider):
    return module.InquiryRequest(provider, "Answer only reviewed inquiries.", (module.InquiryMessage("user", "Hello"),))


def _claude_normal(text="One sentence."):
    return [
        {"type": "message_start"},
        {"type": "content_block_delta", "delta": {"type": "text_delta", "text": text}},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 12}},
        {"type": "message_stop"},
    ]


def _gemini_normal(text="One sentence."):
    return [{"kind": "text", "text": text}, {"kind": "finish", "finish_reason": "STOP", "output_tokens": 12}]


def test_claude_and_gemini_normal_terminal_reasons_commit_after_provisional_sentence():
    module = _lane()
    for provider, kwargs in (("claude", {"claude": [_claude_normal()]}), ("gemini", {"gemini": [_gemini_normal()]})):
        events = asyncio.run(_events_for(_adapter(module, **kwargs), _request(module, provider)))
        assert isinstance(events[0], module.ProvisionalSentence)
        assert isinstance(events[-1], module.TerminalCommit)
        assert events[0].generation == events[-1].generation
        assert events[-1].metadata.finish_reason in {"end_turn", "stop"}


@pytest.mark.parametrize(
    ("provider", "attempts"),
    (
        ("claude", [_claude_normal("Preamble."), _claude_normal("Recovered.")]),
        ("gemini", [_gemini_normal("Preamble."), _gemini_normal("Recovered.")]),
    ),
)
def test_max_tokens_after_a_preamble_fences_that_generation_and_retries_once(provider, attempts):
    module = _lane()
    if provider == "claude":
        attempts[0][-2]["delta"]["stop_reason"] = "max_tokens"
        adapter = _adapter(module, claude=attempts)
    else:
        attempts[0][-1]["finish_reason"] = "MAX_TOKENS"
        adapter = _adapter(module, gemini=attempts)
    events = asyncio.run(_events_for(adapter, _request(module, provider)))
    fences = [event for event in events if isinstance(event, module.GenerationFence)]
    commits = [event for event in events if isinstance(event, module.TerminalCommit)]
    assert len(fences) == 1
    assert fences[0].reason is module.RoundOutcome.MAX_TOKENS_TRUNCATED
    assert fences[0].retrying is True
    assert len(commits) == 1
    assert commits[0].generation != fences[0].generation


def test_dropped_stream_discards_preamble_then_recovers_after_one_retry_without_a_partial_commit():
    module = _lane()
    dropped = [{"type": "message_start"}, {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Preamble."}}]
    events = asyncio.run(_events_for(_adapter(module, claude=[dropped, dropped]), _request(module, "claude")))
    assert len([event for event in events if isinstance(event, module.GenerationFence)]) == 2
    assert not any(isinstance(event, module.TerminalCommit) for event in events)
    assert isinstance(events[-1], module.RecoveryBoundary)
    assert events[-1].reason is module.RoundOutcome.STREAM_ABORTED


@pytest.mark.parametrize("provider", ("claude", "gemini"))
def test_initial_and_stall_timeouts_take_the_same_retry_then_recovery_boundary(provider):
    module = _lane()
    timeouts = module.StreamTimeouts(initial_seconds=1, stall_seconds=1, claude_thinking_stall_seconds=1)
    initial = [[(1.1, {"type": "message_start"})]] if provider == "claude" else [[(1.1, {"kind": "text", "text": "late"})]]
    kwargs = {provider: initial}
    events = asyncio.run(_events_for(_adapter(module, timeouts=timeouts, **kwargs), _request(module, provider)))
    assert isinstance(events[-1], module.RecoveryBoundary)
    assert events[-1].reason is module.RoundOutcome.TIMEOUT
    assert events[-1].retrying is False

    stalled = [
        [{"type": "message_start"}, (1.1, {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "late"}})],
        [{"type": "message_start"}, (1.1, {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "late"}})],
    ] if provider == "claude" else [
        [{"kind": "usage", "output_tokens": 1}, (1.1, {"kind": "text", "text": "late"})],
        [{"kind": "usage", "output_tokens": 1}, (1.1, {"kind": "text", "text": "late"})],
    ]
    events = asyncio.run(_events_for(_adapter(module, timeouts=timeouts, **{provider: stalled}), _request(module, provider)))
    assert len([event for event in events if isinstance(event, module.GenerationFence)]) == 2
    assert isinstance(events[-1], module.RecoveryBoundary)


def test_inquiry_only_lane_never_commits_or_runs_a_provider_tool_event():
    module = _lane()
    tool_event = [{"type": "content_block_start", "content_block": {"type": "tool_use"}}]
    with pytest.raises(module.LLMProtocolError, match="inquiry-only"):
        asyncio.run(_events_for(_adapter(module, claude=[tool_event]), _request(module, "claude")))


def test_thinking_stays_enabled_in_both_injected_provider_request_shapes():
    module = _lane()
    config = module.LLMRuntimeConfig("claude-fixture", "gemini-fixture", 256)
    request = _request(module, "claude")
    assert "tools" not in module._claude_request(request, config)
    assert config.thinking.enabled is True
    assert "thinking_config" in module._gemini_request(request, config)["config"]
