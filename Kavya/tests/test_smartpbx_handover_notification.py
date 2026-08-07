"""Actionability gates for Dialog immediate-failure notification."""

from __future__ import annotations

import json

import pytest

from smartpbx_handover import SmartPBXHandoverCoordinator


class _Control:
    async def transfer_call(self, _target):
        return type("Result", (), {"transferred": False})()


class _Pipeline:
    async def enter_transfer_pending(self):
        raise AssertionError("immediate failure must leave the AI active")


@pytest.mark.asyncio
@pytest.mark.parametrize("caller,manager", [("not-a-number", "0770000000"), ("0771234567", "not-a-number")])
async def test_unactionable_callback_or_manager_number_never_dispatches_notification(caller, manager):
    calls = []

    async def notify(**kwargs):
        calls.append(kwargs)
        return {"ok": True}

    coordinator = SmartPBXHandoverCoordinator(
        call_control=_Control(), pipeline=_Pipeline(), call_sid="safe-call",
        caller_phone=caller, transcript=lambda: [], dashboard_sender=None,
        notification_sender=notify, human_agent_whatsapp=manager,
    )

    result = json.loads(await coordinator.attempt("Need help"))

    assert result == {"status": "unavailable", "notification": "not_actionable"}
    assert calls == []
