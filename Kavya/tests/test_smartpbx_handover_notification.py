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


@pytest.mark.asyncio
async def test_unnotifiable_caller_emits_a_diagnostic_instead_of_going_silent():
    """A withheld or landline CLI means nobody is told the guest wanted a human."""
    from smartpbx_diagnostics import DiagnosticFailureClass, DiagnosticOutcome, DiagnosticStage
    from smartpbx_handover import SmartPBXHandoverCoordinator

    emitted = []

    class Pipeline:
        transfer_pending = False
        _smartpbx_diagnostic_sink = staticmethod(
            lambda stage, outcome, failure: emitted.append((stage, outcome, failure))
        )

        async def enter_transfer_pending(self):
            self.transfer_pending = True

    class Control:
        async def transfer_call(self, _target):
            return type("R", (), {"transferred": False})()

    async def notify(**_):
        raise AssertionError("an unnotifiable number must not reach the sender")

    coordinator = SmartPBXHandoverCoordinator(
        call_control=Control(), pipeline=Pipeline(), call_sid="safe",
        caller_phone="",  # withheld CLI normalises to empty
        transcript=lambda: [], dashboard_sender=None,
        notification_sender=notify, human_agent_whatsapp="0770000000",
    )
    result = json.loads(await coordinator.attempt("caller wants a human"))

    assert result == {"status": "unavailable", "notification": "not_actionable"}
    assert emitted == [(
        DiagnosticStage.HANDOVER,
        DiagnosticOutcome.DEGRADED,
        DiagnosticFailureClass.HANDOVER_NOT_ACTIONABLE,
    )], "the highest-cost silent failure must be visible in the diagnostic stream"


@pytest.mark.asyncio
async def test_a_notifiable_caller_emits_no_handover_diagnostic():
    from smartpbx_handover import SmartPBXHandoverCoordinator

    emitted = []

    class Pipeline:
        transfer_pending = False
        _smartpbx_diagnostic_sink = staticmethod(
            lambda stage, outcome, failure: emitted.append((stage, outcome, failure))
        )

        async def enter_transfer_pending(self):
            self.transfer_pending = True

    class Control:
        async def transfer_call(self, _target):
            return type("R", (), {"transferred": False})()

    async def notify(**_):
        return {"ok": True}

    coordinator = SmartPBXHandoverCoordinator(
        call_control=Control(), pipeline=Pipeline(), call_sid="safe",
        caller_phone="0771234567", transcript=lambda: [], dashboard_sender=None,
        notification_sender=notify, human_agent_whatsapp="0770000000",
    )
    assert json.loads(await coordinator.attempt("x"))["notification"] == "sent"
    assert emitted == []
