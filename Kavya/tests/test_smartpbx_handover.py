"""Parity contracts for the native Dialog handover lifecycle."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

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
        call_control=_Control(result=type("Result", (), {"acknowledged": True})()),
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
        "status": "transfer_requested", "confirmation": "provider_acknowledged"
    }
    assert coordinator.phase is HandoverPhase.ACKNOWLEDGED
    assert coordinator.transfer_pending is True
    assert coordinator._pipeline.entered == 1
    assert len(dashboard_calls) == 1
    assert dashboard_calls[0]["privacy_safe"] is True
    assert dashboard_calls[0]["transfer_target"] == "human_support"
    assert dashboard_calls[0]["transfer_confirmation"] == "provider_acknowledged"


@pytest.mark.asyncio
async def test_acknowledged_transfer_is_requested_not_connected_and_notifies_neutrally():
    """An MCP acknowledgement never asserts that a human answered the call."""
    notifications = []

    async def notify(**kwargs):
        notifications.append(kwargs)
        return {"ok": True}

    pipeline = _Pipeline()
    coordinator = SmartPBXHandoverCoordinator(
        call_control=_Control(result=SimpleNamespace(acknowledged=True, failure=None)),
        pipeline=pipeline,
        call_sid="safe-call",
        caller_phone="0771234567",
        transcript=lambda: [],
        dashboard_sender=None,
        notification_sender=notify,
        human_agent_whatsapp="0770000000",
    )

    result = json.loads(await coordinator.attempt("guest asked for a manager"))

    assert result == {
        "status": "transfer_requested",
        "confirmation": "provider_acknowledged",
    }
    assert coordinator.phase is HandoverPhase.ACKNOWLEDGED
    assert coordinator.transfer_pending is True
    assert pipeline.entered == 1
    assert len(notifications) == 1
    assert notifications[0]["outcome"] == "transfer_acknowledged"
    assert "transfer_succeeded" not in notifications[0].values()
    assert notifications[0]["call_summary"].startswith("Live transfer was requested.")
    assert "answered" not in notifications[0]["call_summary"].lower()


@pytest.mark.asyncio
async def test_immediate_failure_keeps_pipeline_active_and_notifies_once_then_retries_at_finish():
    notifications = []

    async def notify(**kwargs):
        notifications.append(kwargs)
        return {"ok": False}

    pipeline = _Pipeline()
    coordinator = SmartPBXHandoverCoordinator(
        call_control=_Control(result=type("Result", (), {"acknowledged": False})()),
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
    assert notifications[0]["outcome"] == "transfer_failed"
    assert notifications[1]["outcome"] == "transfer_failed"
    assert notifications[0]["privacy_safe"] is True


def _real_control(session_factory):
    """Wire the genuine MCP client in, so the wire contract is under test too."""
    from smartpbx_mcp import DialogMCPCallControl, DialogMCPSettings
    from smartpbx_protocol import CallContext, MediaFormat

    settings = DialogMCPSettings.from_env({
        "SMARTPBX_MCP_URL": "https://dialog.example:9443/ucp/v2/mcp",
        "SMARTPBX_API_KEY": "api-key-marker",
        "SMARTPBX_ACCOUNT_ID": "account-1",
        "SMARTPBX_MCP_ACCOUNT_HEADER": "account_id",
        "SMARTPBX_TRANSFER_DESTINATIONS_JSON": '{"human_support":"tel:+94711754668"}',
    })
    context = CallContext(
        call_id="media-leg", other_leg_call_id="dialog-leg", caller_id_number="+94771234567",
        callee_id_number="+94110000000", account_id="account-1",
        media_format=MediaFormat("g711_ulaw", 8000),
    )
    return DialogMCPCallControl(settings, context, session_factory)


def _session_factory(outcome):
    from contextlib import asynccontextmanager

    calls = []

    class _Session:
        async def initialize(self):
            return None

        async def call_tool(self, name, arguments):
            calls.append((name, arguments))
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

    @asynccontextmanager
    async def factory(**_kwargs):
        yield _Session()

    return factory, calls


@pytest.mark.asyncio
async def test_dialog_acknowledgement_requests_transfer_and_notifies_the_manager():
    """A provider acknowledgement still sends one neutral WhatsApp heads-up."""
    acknowledged = type("Result", (), {"content": [type("T", (), {"text": "ok"})()]})()
    factory, calls = _session_factory(acknowledged)
    notifications = []

    async def notify(**kwargs):
        notifications.append(kwargs)
        return {"ok": True}

    coordinator = SmartPBXHandoverCoordinator(
        call_control=_real_control(factory), pipeline=_Pipeline(), call_sid="dialog-leg",
        caller_phone="+94771234567", transcript=lambda: [], dashboard_sender=None,
        notification_sender=notify, human_agent_whatsapp="94711754668",
    )

    result = json.loads(await coordinator.attempt("guest asked for a human"))

    assert calls == [(
        "transfer_call",
        {"destination number": "94711754668", "tier": "BYPASS"},
    )]
    assert result == {"status": "transfer_requested", "confirmation": "provider_acknowledged"}
    assert coordinator.transfer_pending is True
    assert len(notifications) == 1
    assert notifications[0]["outcome"] == "transfer_acknowledged"


@pytest.mark.asyncio
async def test_rejected_request_still_falls_back_to_the_manager_whatsapp():
    """A protocol-level rejection is a genuine failure: fail closed, notify."""

    class _McpError(Exception):
        __module__ = "mcp.shared.exceptions"

        def __init__(self):
            self.error = type("E", (), {"code": -32603, "message": "bad argument"})()
            super().__init__("bad argument")

    factory, _calls = _session_factory(_McpError())
    notifications = []

    async def notify(**kwargs):
        notifications.append(kwargs)
        return {"ok": True}

    coordinator = SmartPBXHandoverCoordinator(
        call_control=_real_control(factory), pipeline=_Pipeline(), call_sid="dialog-leg",
        caller_phone="+94771234567", transcript=lambda: [], dashboard_sender=None,
        notification_sender=notify, human_agent_whatsapp="94711754668",
    )

    result = json.loads(await coordinator.attempt("guest asked for a human"))

    assert result == {"status": "unavailable", "notification": "sent"}
    assert coordinator.transfer_pending is False
    assert len(notifications) == 1
    assert notifications[0]["outcome"] == "transfer_failed"


@pytest.mark.asyncio
async def test_concurrent_attempts_dispatch_and_notify_only_once_per_attempt():
    control = _Control(result=type("Result", (), {"acknowledged": False})())
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
    assert notifications[0]["outcome"] == "transfer_failed"
    assert all(json.loads(value)["status"] == "unavailable" for value in results)


@pytest.mark.asyncio
async def test_transfer_acknowledged_and_failed_outcomes_are_in_payload():
    notifications = []

    async def notify(**kwargs):
        notifications.append((kwargs["call_sid"], kwargs.get("outcome")))
        return {"ok": True}

    acknowledged = SmartPBXHandoverCoordinator(
        call_control=_Control(result=type("Result", (), {"acknowledged": True})()),
        pipeline=_Pipeline(), call_sid="acknowledged-call", caller_phone="0771234567", transcript=lambda: [],
        dashboard_sender=None, notification_sender=notify, human_agent_whatsapp="0770000000",
    )
    failure = SmartPBXHandoverCoordinator(
        call_control=_Control(result=type("Result", (), {"acknowledged": False})()),
        pipeline=_Pipeline(), call_sid="failed-call", caller_phone="0771234567", transcript=lambda: [],
        dashboard_sender=None, notification_sender=notify, human_agent_whatsapp="0770000000",
    )

    await acknowledged.attempt("first")
    await failure.attempt("second")

    assert notifications == [("acknowledged-call", "transfer_acknowledged"), ("failed-call", "transfer_failed")]
