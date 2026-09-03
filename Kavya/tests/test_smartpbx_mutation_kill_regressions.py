"""Regression tests pinned against the surviving mutants recorded in
`audit-tests.md` section 8 / mutation-results.txt (G2, G4, S3, S4).

Each of these guards passed the mutation experiment unchanged (rc=0, all
existing tests green) with its protective logic gutted:

- G2 `release_once_guard_removed` -- `SessionLease.release()`'s own
  "already released" flag was dropped; only the registry's incidental
  `if self._active_sessions:` guard (smartpbx_gateway.py:141) happened to
  still make the existing double-release test pass.
- G4 `constant_time_compare_replaced` -- `token_matches` swapped
  `secrets.compare_digest` for `==`; nothing asserted the constant-time
  call itself.
- S3 `tool_boundary_guard_always_true` -- the pre-execution ownership
  check `_current_smartpbx_runner_can_execute_tools()` was hardcoded to
  `True`; nothing exercised the window between the round-entry ownership
  check and the tool-execution loop.
- S4 `torn_down_gate_removed` -- the `_smartpbx_torn_down` early-return in
  `_accumulate_transcript` was dropped; nothing fed a final STT result
  after teardown.

See the parent task report for RED (against a scratch mutant copy) / GREEN
(against real code) evidence for each test below.
"""

from __future__ import annotations

import asyncio
import json

import pytest

import smartpbx_gateway
from smartpbx_gateway import SmartPBXSessionRegistry

from tests.test_smartpbx_gateway import settings
from tests.test_smartpbx_server import (
    _disable_initial_filler,
    direct_tool_client,
    direct_tool_pipeline,
    direct_tool_round,
)


# -- G2: SessionLease release-twice must count once --------------------------


@pytest.mark.asyncio
async def test_single_lease_released_twice_counts_once():
    """Releasing the SAME lease twice must not double-decrement or
    double-count -- unlike releasing two DIFFERENT admitted leases once
    each, which the pre-existing gateway test conflated (audit gap #1)."""
    registry = SmartPBXSessionRegistry(4)
    lease_a = await registry.try_acquire()
    lease_b = await registry.try_acquire()
    assert lease_a is not None and lease_b is not None
    assert registry.snapshot()["active_sessions"] == 2

    await lease_a.release()
    await lease_a.release()

    snapshot = registry.snapshot()
    assert snapshot["active_sessions"] == 1, "lease_b must still be held"
    assert snapshot["released_total"] == 1, "the second release must not recount"


@pytest.mark.asyncio
async def test_concurrent_release_of_one_lease_is_idempotent():
    """Ten concurrent releases of the SAME lease must behave as exactly
    one release. A second admitted lease is kept held throughout so the
    registry's own `if self._active_sessions:` floor (gateway.py:141)
    cannot single-handedly mask a missing per-lease guard -- with only one
    lease ever admitted that registry-level floor saturates the count on
    its own and this race would pass even with G2's guard removed."""
    registry = SmartPBXSessionRegistry(4)
    lease = await registry.try_acquire()
    other_lease = await registry.try_acquire()
    assert lease is not None and other_lease is not None

    await asyncio.gather(*(lease.release() for _ in range(10)))

    snapshot = registry.snapshot()
    assert snapshot["active_sessions"] == 1, "the held second lease must survive"
    assert snapshot["released_total"] == 1


# -- G4: token_matches must delegate to secrets.compare_digest ---------------


def test_token_matches_delegates_to_secrets_compare_digest(monkeypatch):
    """Pin the constant-time property the only way source-level pinning
    allows: assert the real comparator is actually invoked with both
    operands, rather than asserting a behavioural side effect (reject
    empty / wrong length / non-ASCII) that a non-constant-time `==`
    would satisfy identically and quickly."""
    configuration = settings()
    calls: list[tuple[str, str]] = []

    def fake_compare_digest(a, b):
        calls.append((a, b))
        return a == b

    monkeypatch.setattr(smartpbx_gateway.secrets, "compare_digest", fake_compare_digest)

    result = configuration.token_matches(configuration.token)

    assert result is True
    assert calls == [(configuration.token, configuration.token)]


def test_token_matches_wrong_token_still_delegates_to_compare_digest(monkeypatch):
    configuration = settings()
    calls: list[tuple[str, str]] = []

    def fake_compare_digest(a, b):
        calls.append((a, b))
        return a == b

    monkeypatch.setattr(smartpbx_gateway.secrets, "compare_digest", fake_compare_digest)

    result = configuration.token_matches("wrong-token")

    assert result is False
    assert calls == [(configuration.token, "wrong-token")]


# -- S3: tool-boundary ownership guard --------------------------------------


@pytest.mark.asyncio
async def test_stale_runner_tool_boundary_guard_blocks_execution_after_ownership_loss(
    monkeypatch,
):
    """A runner that loses ownership between the round-entry ownership
    check and the per-tool execution loop must never cross into
    `execute_tool` at all -- the wire-visible tool side-effect boundary
    (server.py `_current_smartpbx_runner_can_execute_tools`) -- even
    though the round-entry check and the post-execution ownership check
    both still pass/fail correctly on their own. This is the narrow
    window S3 (`tool_boundary_guard_always_true`) leaves unguarded."""
    import server

    client = direct_tool_client(
        "openai",
        [direct_tool_round("openai", {"nights": 2}, tool_name="check_availability")],
    )
    pipeline = direct_tool_pipeline(server, "openai", client)
    _disable_initial_filler(monkeypatch, pipeline)
    turn_id = "owner-turn"
    pipeline._active_smartpbx_turn_id = turn_id
    runner = server._SmartPBXRunnerContext(
        turn_id=turn_id, dropped_frame_baseline=0,
        speak_generation=pipeline._speak_generation, raw_utterance="",
    )

    execute_calls: list[tuple[str, dict]] = []

    async def execute(name, arguments):
        execute_calls.append((name, arguments))
        return json.dumps({"status": "ok"})

    def steal_ownership_during_tool_filler(text, *, generation, lease):
        # Simulate a concurrent barge-in landing after the round-entry
        # ownership check has already passed but before the tool-execution
        # loop is reached (the specialized-tool-filler dispatch point).
        pipeline._active_smartpbx_turn_id = "newer-turn"
        return None

    monkeypatch.setattr(server, "execute_tool", execute)
    monkeypatch.setattr(
        pipeline, "_start_smartpbx_tool_filler", steal_ownership_during_tool_filler
    )

    token = server._smartpbx_runner_context.set(runner)
    try:
        result = await pipeline._run_llm()
    finally:
        server._smartpbx_runner_context.reset(token)

    assert result == ""
    assert execute_calls == [], "a stale runner must never cross the tool boundary"
    assert pipeline.history == [], "no tool request/result may be published"


# -- S4: torn-down gate on STT results ---------------------------------------


@pytest.mark.asyncio
async def test_stt_final_after_teardown_is_dropped_and_never_dispatched(monkeypatch):
    """A residual STT final callback landing after `_smartpbx_torn_down`
    was set must be dropped silently -- no endpointing timer armed, no
    transcript state mutated, nothing dispatched into a new turn."""
    import server

    client = direct_tool_client("openai", [])
    pipeline = direct_tool_pipeline(server, "openai", client)
    pipeline._smartpbx_torn_down = True

    armed: list[float] = []
    monkeypatch.setattr(pipeline, "_arm_endpointing", lambda delay: armed.append(delay))

    await pipeline._accumulate_transcript("two nights for two guests please")

    assert pipeline._committed_transcript == ""
    assert pipeline._pending_transcript == ""
    assert armed == [], "no endpointing timer may be armed after teardown"
