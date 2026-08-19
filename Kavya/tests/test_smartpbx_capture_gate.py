"""Fix #1 regression: transfer_to_human must not reach a live transfer
attempt on a Dialog/SmartPBX call before the caller has at least been
ASKED for a name and a callback number.

This exercises the dispatch layer directly (`tools.execute_tool`), not the
LLM — the diagnosed bug was that Kavya could run zero or partial capture
tools before calling transfer_to_human, so the guarantee under test is
event-sequence, not prompt wording.
"""

from __future__ import annotations

import json

import pytest

import tools


class _FakeCoordinator:
    """Records whether the live-transfer path was ever reached."""

    def __init__(self) -> None:
        self.attempt_calls: list[str] = []

    async def attempt(self, reason: str) -> str:
        self.attempt_calls.append(reason)
        return json.dumps({"status": "transferred", "confirmation": "provider_acknowledged"})


class _FakePipeline:
    def __init__(self, *, name_attempted: bool, number_attempted: bool) -> None:
        self._capture_attempted_name = name_attempted
        self._capture_attempted_number = number_attempted


def _make_context(pipeline: _FakePipeline, coordinator: _FakeCoordinator) -> tools.SmartPBXTransferContext:
    return tools.SmartPBXTransferContext(call_control=None, coordinator=coordinator, pipeline=pipeline)


@pytest.mark.asyncio
async def test_transfer_deflected_when_no_capture_ever_attempted():
    coordinator = _FakeCoordinator()
    pipeline = _FakePipeline(name_attempted=False, number_attempted=False)
    context = _make_context(pipeline, coordinator)

    token = tools.smartpbx_transfer_context.set(context)
    try:
        result = await tools.execute_tool("transfer_to_human", {"reason": "wants a human"})
    finally:
        tools.smartpbx_transfer_context.reset(token)

    assert json.loads(result)["status"] == "capture_first"
    assert coordinator.attempt_calls == []


@pytest.mark.asyncio
async def test_transfer_deflected_when_only_one_of_name_or_number_attempted():
    coordinator = _FakeCoordinator()
    pipeline = _FakePipeline(name_attempted=True, number_attempted=False)
    context = _make_context(pipeline, coordinator)

    token = tools.smartpbx_transfer_context.set(context)
    try:
        result = await tools.execute_tool("transfer_to_human", {"reason": "wants a human"})
    finally:
        tools.smartpbx_transfer_context.reset(token)

    assert json.loads(result)["status"] == "capture_first"
    assert coordinator.attempt_calls == []


@pytest.mark.asyncio
async def test_transfer_proceeds_on_second_call_after_one_time_nudge():
    """Not a hard block: a caller who declines still reaches a human."""
    coordinator = _FakeCoordinator()
    pipeline = _FakePipeline(name_attempted=False, number_attempted=False)
    context = _make_context(pipeline, coordinator)

    token = tools.smartpbx_transfer_context.set(context)
    try:
        first = await tools.execute_tool("transfer_to_human", {"reason": "wants a human"})
        assert json.loads(first)["status"] == "capture_first"
        assert coordinator.attempt_calls == []

        second = await tools.execute_tool("transfer_to_human", {"reason": "declined to give details"})
    finally:
        tools.smartpbx_transfer_context.reset(token)

    assert json.loads(second)["status"] == "transferred"
    assert coordinator.attempt_calls == ["declined to give details"]


@pytest.mark.asyncio
async def test_transfer_proceeds_immediately_when_both_already_attempted():
    coordinator = _FakeCoordinator()
    pipeline = _FakePipeline(name_attempted=True, number_attempted=True)
    context = _make_context(pipeline, coordinator)

    token = tools.smartpbx_transfer_context.set(context)
    try:
        result = await tools.execute_tool("transfer_to_human", {"reason": "wants a human"})
    finally:
        tools.smartpbx_transfer_context.reset(token)

    assert json.loads(result)["status"] == "transferred"
    assert coordinator.attempt_calls == ["wants a human"]


@pytest.mark.asyncio
async def test_transfer_proceeds_when_pipeline_is_none():
    """No session to gate on (e.g. legacy call shape) — never blocks blind."""
    coordinator = _FakeCoordinator()
    context = tools.SmartPBXTransferContext(call_control=None, coordinator=coordinator, pipeline=None)

    token = tools.smartpbx_transfer_context.set(context)
    try:
        result = await tools.execute_tool("transfer_to_human", {"reason": "wants a human"})
    finally:
        tools.smartpbx_transfer_context.reset(token)

    assert json.loads(result)["status"] == "transferred"
    assert coordinator.attempt_calls == ["wants a human"]
