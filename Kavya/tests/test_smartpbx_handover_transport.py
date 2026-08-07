"""Privacy-safe transport actionability contracts for Dialog handover."""

from __future__ import annotations

import pytest

from handover import send_handover_notification


@pytest.mark.asyncio
async def test_privacy_safe_invalid_manager_number_is_not_actionable_without_transport(monkeypatch):
    import booking_api

    called = False

    async def get_session():
        nonlocal called
        called = True
        raise AssertionError("invalid manager number must not reach transport")

    monkeypatch.setattr(booking_api, "get_session", get_session)

    result = await send_handover_notification(
        call_sid="safe-call", customer_name="Unknown", customer_whatsapp="0771234567",
        call_summary="Live transfer could not be started", human_agent_whatsapp="not-a-number",
        privacy_safe=True,
    )

    assert result == {"ok": False, "error": "missing_human_agent_whatsapp"}
    assert called is False
