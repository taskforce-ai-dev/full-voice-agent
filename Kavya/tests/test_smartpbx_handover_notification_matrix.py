"""Privacy-safe Dialog manager notification delivery matrix."""
from __future__ import annotations

import logging
import pytest


class Response:
    def __init__(self, status): self.status = status
    async def __aenter__(self): return self
    async def __aexit__(self, *_): return False
    async def text(self): raise AssertionError("privacy mode must not read response body")


@pytest.mark.asyncio
@pytest.mark.parametrize("status,expected", [(201, {"ok": True, "status": 201}), (503, {"ok": False, "status": 503, "error": "delivery_failed"})])
async def test_privacy_safe_notification_sends_operational_payload_without_logging_or_reading_body(monkeypatch, caplog, status, expected):
    import booking_api
    from handover import send_handover_notification
    captured = []
    class Session:
        def post(self, url, json=None, **_kwargs):
            captured.append((url, json)); return Response(status)
    async def get_session(): return Session()
    monkeypatch.setattr(booking_api, "get_session", get_session)
    with caplog.at_level(logging.INFO):
        result = await send_handover_notification(call_sid="CALL-SECRET", customer_name="Unknown", customer_whatsapp="0771234567", call_summary="SUMMARY-SECRET", human_agent_whatsapp="0770000000", privacy_safe=True)
    assert result == expected
    assert captured[0][1]["customer_whatsapp"] == "94771234567"
    assert captured[0][1]["human_agent_whatsapp"] == "94770000000"
    assert "CALL-SECRET" not in caplog.text and "SUMMARY-SECRET" not in caplog.text


@pytest.mark.asyncio
async def test_privacy_safe_notification_redacts_transport_exception(monkeypatch, caplog):
    import booking_api
    from handover import send_handover_notification
    class Session:
        def post(self, *_args, **_kwargs): raise RuntimeError("N8N-SECRET")
    async def get_session(): return Session()
    monkeypatch.setattr(booking_api, "get_session", get_session)
    with caplog.at_level(logging.INFO):
        result = await send_handover_notification(call_sid="CALL-SECRET", customer_name="Unknown", customer_whatsapp="0771234567", call_summary="SUMMARY-SECRET", human_agent_whatsapp="0770000000", privacy_safe=True)
    assert result == {"ok": False, "error": "delivery_failed"}
    assert "N8N-SECRET" not in caplog.text and "CALL-SECRET" not in caplog.text
