"""Production-shaped capture-gate tests.

tests/test_smartpbx_capture_gate.py exercises the pre-handover capture gate
directly against tools.execute_tool with hand-built context objects. This
file instead drives the gate through the REAL per-provider SmartPBX runner
(openai/gemini/claude) via server.MediaStreamSession._process_utterance --
the same direct-runner scaffolding test_smartpbx_server.py's own transfer
tests use -- to prove the gate, the capture_first nudge round-trip, and the
capture_outcome retry are wired correctly into the actual conversation loop
for all three providers, not just reachable from a bare unit test.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

import server
import tools

# test_smartpbx_server.py (this directory) is not a package -- pytest's
# per-file rootdir insertion during a single-file run does not reliably put
# this directory on sys.path for a sibling test module's own imports, so add
# it explicitly rather than duplicate its direct-runner test scaffolding.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_smartpbx_server import (  # noqa: E402
    GenerationTrackingTransport,
    bind_direct_smartpbx_turn,
    direct_tool_batch_round,
    direct_tool_client,
    direct_tool_pipeline,
    direct_tool_round,
)


class _FakeCoordinator:
    """Records whether the live-transfer path was ever reached."""

    def __init__(self) -> None:
        self.attempt_calls: list[str] = []

    async def attempt(self, reason: str) -> str:
        self.attempt_calls.append(reason)
        return json.dumps({"status": "transferred", "confirmation": "provider_acknowledged"})


def _wire_pipeline(provider, client, coordinator, monkeypatch):
    pipeline = direct_tool_pipeline(server, provider, client)
    bind_direct_smartpbx_turn(server, pipeline)
    pipeline._media_transport = GenerationTrackingTransport()
    # direct_tool_pipeline() defaults these to True (see its own docstring)
    # since most of test_smartpbx_server.py's transfer tests are about
    # fencing/delivery order, not this gate. These capture-gate tests need
    # the real "nothing attempted yet" starting state; callers that want
    # "already attempted" set them back to True explicitly.
    pipeline._capture_attempted_name = False
    pipeline._capture_attempted_number = False
    pipeline._smartpbx_transfer_context = tools.SmartPBXTransferContext(
        call_control=None, coordinator=coordinator, pipeline=pipeline,
    )
    monkeypatch.setattr(server, "retrieve_context", lambda _text: "")
    monkeypatch.setattr(pipeline, "_start_initial_smartpbx_filler", lambda **_kwargs: None)

    async def tts(_text, **_kwargs):
        return None

    async def delivery_barrier(_pipeline, **_kwargs):
        return None

    monkeypatch.setattr(pipeline, "_tts_elevenlabs", tts)
    monkeypatch.setattr(tools, "_await_turn_delivery", delivery_barrier)
    return pipeline


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["openai", "gemini", "claude"])
async def test_runner_nudges_then_transfers_after_capture(monkeypatch, provider):
    """Round 1: the model calls transfer_to_human with no prior capture and
    is nudged. Round 2: it captures name + number, then retries with
    capture_outcome="captured" -- the coordinator's live-transfer path is
    reached exactly once, on the retry."""
    coordinator = _FakeCoordinator()
    client = direct_tool_client(provider, [
        direct_tool_round(provider, {"reason": "wants a human"}, tool_name="transfer_to_human"),
        direct_tool_batch_round(provider, [
            ("capture_spoken_name", {"spoken": "Ada Lovelace"}),
            ("capture_spoken_number", {"spoken": "oh seven seven one two three four five six seven"}),
            ("transfer_to_human", {"reason": "wants a human", "capture_outcome": "captured"}),
        ]),
    ])
    pipeline = _wire_pipeline(provider, client, coordinator, monkeypatch)

    real_execute = tools.execute_tool
    tool_calls: list[str] = []

    async def execute(name, arguments):
        tool_calls.append(name)
        if name == "capture_spoken_name":
            return json.dumps({"status": "captured", "name": "Ada Lovelace"})
        if name == "capture_spoken_number":
            return json.dumps({"status": "captured", "valid": True, "normalized": "+94771234567"})
        return await real_execute(name, arguments)

    monkeypatch.setattr(server, "execute_tool", execute)

    await asyncio.wait_for(
        pipeline._process_utterance("I want to speak to a human"), timeout=5,
    )

    assert coordinator.attempt_calls == ["wants a human"]
    assert tool_calls.count("transfer_to_human") == 2
    assert pipeline._smartpbx_transfer_context.capture_nudge_sent is True
    assert pipeline._smartpbx_transfer_context.capture_outcome == "captured"


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["openai", "gemini", "claude"])
async def test_runner_transfers_immediately_when_capture_already_attempted(monkeypatch, provider):
    """A caller who already gave name + number earlier in the call (e.g.
    during booking) is transferred on the FIRST transfer_to_human call --
    the gate does not re-ask for what it already has."""
    coordinator = _FakeCoordinator()
    client = direct_tool_client(provider, [
        direct_tool_round(provider, {"reason": "wants a human"}, tool_name="transfer_to_human"),
    ])
    pipeline = _wire_pipeline(provider, client, coordinator, monkeypatch)
    pipeline._capture_attempted_name = True
    pipeline._capture_attempted_number = True

    await asyncio.wait_for(
        pipeline._process_utterance("I want to speak to a human"), timeout=5,
    )

    assert coordinator.attempt_calls == ["wants a human"]
    assert pipeline._smartpbx_transfer_context.capture_nudge_sent is False
    assert pipeline._smartpbx_transfer_context.capture_outcome == "captured"


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["openai", "gemini", "claude"])
async def test_runner_transfers_on_retry_when_caller_declines(monkeypatch, provider):
    """The caller declines to give details; the model retries with
    capture_outcome="declined" and still reaches a human -- the gate is a
    one-time nudge, never a trap."""
    coordinator = _FakeCoordinator()
    client = direct_tool_client(provider, [
        direct_tool_round(provider, {"reason": "wants a human"}, tool_name="transfer_to_human"),
        direct_tool_round(
            provider,
            {"reason": "declined to give details", "capture_outcome": "declined"},
            tool_name="transfer_to_human",
        ),
    ])
    pipeline = _wire_pipeline(provider, client, coordinator, monkeypatch)

    await asyncio.wait_for(
        pipeline._process_utterance("I want to speak to a human"), timeout=5,
    )

    assert coordinator.attempt_calls == ["declined to give details"]
    assert pipeline._smartpbx_transfer_context.capture_outcome == "declined"
