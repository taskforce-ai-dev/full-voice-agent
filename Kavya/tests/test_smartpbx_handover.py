"""Parity contracts for the native Dialog handover lifecycle."""

from __future__ import annotations

import asyncio
import json

import pytest

from smartpbx_handover import HandoverPhase, SmartPBXHandoverCoordinator


class _Control:
    def __init__(self, result=None, error: BaseException | None = None):
        self.result = result
        self.error = error
        self.calls = []

    async def transfer_call(self, target):
        self.calls.append(target)
        if self.error:
            raise self.error
        return self.result


class _Pipeline:
    def __init__(self):
        self.entered = 0

    async def enter_transfer_pending(self):
        self.entered += 1


@pytest.mark.asyncio
async def test_acknowledged_transfer_enters_pending_and_reports_privately_once():
    dashboard_calls = []

    async def dashboard(**kwargs):
        dashboard_calls.append(kwargs)

    coordinator = SmartPBXHandoverCoordinator(
        call_control=_Control(result=type("Result", (), {"transferred": True})()),
        pipeline=_Pipeline(),
        call_sid="call-secret",
        caller_phone="+94771234567",
        transcript=lambda: [],
        dashboard_sender=dashboard,
        notification_sender=None,
        human_agent_whatsapp="+94770000000",
    )

    result = await coordinator.attempt("  ask\x00  manager  ")

    assert json.loads(result) == {
        "status": "transferred", "confirmation": "provider_acknowledged"
    }
    assert coordinator.phase is HandoverPhase.ACKNOWLEDGED
    assert coordinator.transfer_pending is True
    assert coordinator._pipeline.entered == 1
    assert len(dashboard_calls) == 1
    assert dashboard_calls[0]["privacy_safe"] is True
    assert dashboard_calls[0]["transfer_target"] == "human_support"
    assert dashboard_calls[0]["transfer_confirmation"] == "provider_acknowledged"


@pytest.mark.asyncio
async def test_immediate_failure_keeps_pipeline_active_and_notifies_once_then_retries_at_finish():
    notifications = []

    async def notify(**kwargs):
        notifications.append(kwargs)
        return {"ok": False}

    pipeline = _Pipeline()
    coordinator = SmartPBXHandoverCoordinator(
        call_control=_Control(result=type("Result", (), {"transferred": False})()),
        pipeline=pipeline,
        call_sid="safe-call",
        caller_phone="0771234567",
        transcript=lambda: [{"role": "user", "text": "latest"}],
        dashboard_sender=None,
        notification_sender=notify,
        human_agent_whatsapp="0770000000",
    )

    result = json.loads(await coordinator.attempt("reason"))
    await coordinator.finalize_notification_retry()
    await coordinator.finalize_notification_retry()

    assert result == {"status": "unavailable", "notification": "failed"}
    assert coordinator.phase is HandoverPhase.IMMEDIATE_FAILED
    assert pipeline.entered == 0
    assert len(notifications) == 2
    assert notifications[0]["privacy_safe"] is True


@pytest.mark.asyncio
async def test_concurrent_attempts_dispatch_and_notify_only_once_per_attempt():
    control = _Control(result=type("Result", (), {"transferred": False})())
    notifications = []

    async def notify(**kwargs):
        notifications.append(kwargs)
        return {"ok": True}

    coordinator = SmartPBXHandoverCoordinator(
        call_control=control,
        pipeline=_Pipeline(), call_sid="safe", caller_phone="0771234567",
        transcript=lambda: [], dashboard_sender=None, notification_sender=notify,
        human_agent_whatsapp="0770000000",
    )
    results = await asyncio.gather(coordinator.attempt("first"), coordinator.attempt("second"))

    assert len(control.calls) == 1
    assert len(notifications) == 1
    assert all(json.loads(value)["status"] == "unavailable" for value in results)
