"""Fix #2 regression: the WhatsApp fallback notification must carry a
captured guest name when one is available, instead of always hard-coding
"Unknown" — the diagnosed bug was that `customer_name="Unknown"` was
unconditional even when `capture_spoken_name` had already succeeded
earlier in the call.
"""

from __future__ import annotations

import json

import pytest

from smartpbx_handover import SmartPBXHandoverCoordinator


class _Control:
    def __init__(self, result=None):
        self.result = result

    async def transfer_call(self, _target):
        return self.result


class _Pipeline:
    async def enter_transfer_pending(self):
        pass


def _make_coordinator(*, guest_name, notifications, result=None):
    async def notify(**kwargs):
        notifications.append(kwargs)
        return {"ok": True}

    return SmartPBXHandoverCoordinator(
        call_control=_Control(result=result),
        pipeline=_Pipeline(),
        call_sid="call-name-fix",
        caller_phone="+94771234567",
        transcript=lambda: [],
        dashboard_sender=None,
        notification_sender=notify,
        human_agent_whatsapp="+94770000000",
        guest_name=guest_name,
    )


@pytest.mark.asyncio
async def test_captured_name_reaches_the_fallback_notification():
    notifications: list[dict] = []
    coordinator = _make_coordinator(
        guest_name=lambda: "Nimal Perera",
        notifications=notifications,
        result=None,  # transfer_call unavailable -> transfer_failed fallback
    )

    await coordinator.attempt("Caller asked for a human.")

    assert len(notifications) == 1
    assert notifications[0]["customer_name"] == "Nimal Perera"


@pytest.mark.asyncio
async def test_missing_guest_name_still_defaults_to_unknown():
    notifications: list[dict] = []
    coordinator = _make_coordinator(guest_name=None, notifications=notifications)

    await coordinator.attempt("Caller asked for a human.")

    assert notifications[0]["customer_name"] == "Unknown"


@pytest.mark.asyncio
async def test_empty_captured_name_defaults_to_unknown():
    notifications: list[dict] = []
    coordinator = _make_coordinator(
        guest_name=lambda: "", notifications=notifications
    )

    await coordinator.attempt("Caller asked for a human.")

    assert notifications[0]["customer_name"] == "Unknown"


@pytest.mark.asyncio
async def test_raising_guest_name_accessor_never_breaks_the_notification():
    def boom():
        raise RuntimeError("booking_slots not ready")

    notifications: list[dict] = []
    coordinator = _make_coordinator(guest_name=boom, notifications=notifications)

    result = await coordinator.attempt("Caller asked for a human.")

    assert json.loads(result)["status"] == "unavailable"
    assert notifications[0]["customer_name"] == "Unknown"


@pytest.mark.asyncio
async def test_captured_name_is_control_char_stripped_and_bounded():
    notifications: list[dict] = []
    coordinator = _make_coordinator(
        guest_name=lambda: "  Nimal\x00 Perera  " + ("x" * 200),
        notifications=notifications,
    )

    await coordinator.attempt("Caller asked for a human.")

    name = notifications[0]["customer_name"]
    assert "\x00" not in name
    assert len(name) <= 120
