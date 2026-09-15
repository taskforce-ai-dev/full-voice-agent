"""Dialog transfer dashboard privacy transport regressions."""

from __future__ import annotations

import logging

import pytest


class _Response:
    status = 503

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def text(self):
        raise AssertionError("privacy-safe delivery must not read a response body")


class _Session:
    def __init__(self):
        self.payload = None

    def post(self, _url, *, json, **_kwargs):
        self.payload = json
        return _Response()


@pytest.mark.asyncio
async def test_privacy_safe_transferred_event_omits_human_phone_and_never_reads_failure_body(monkeypatch, caplog):
    import booking_api
    import dashboard_client

    session = _Session()

    async def get_session():
        return session

    monkeypatch.setattr(booking_api, "get_session", get_session)
    monkeypatch.setattr(dashboard_client, "_ENABLED", True)
    monkeypatch.setattr(dashboard_client, "_announced", False)
    monkeypatch.setattr(dashboard_client, "DASHBOARD_API_URL", "https://dashboard.example")
    monkeypatch.setattr(dashboard_client, "DASHBOARD_API_KEY", "key")
    monkeypatch.setattr(dashboard_client, "DASHBOARD_AGENT_ID", "agent")

    with caplog.at_level(logging.INFO):
        await dashboard_client.send_call_transferred(
            call_sid="CALL-SECRET", caller_phone="CALLER-SECRET", reason="REASON-SECRET",
            transfer_target="human_support", transfer_provider="dialog_mcp",
            transfer_confirmation="provider_acknowledged", privacy_safe=True,
        )

    assert "human_phone" not in session.payload["call"]["metadata"]
    assert session.payload["call"]["metadata"]["transfer_confirmation"] == "provider_acknowledged"
    assert "CALL-SECRET" not in caplog.text
    assert "CALLER-SECRET" not in caplog.text
    assert "REASON-SECRET" not in caplog.text
