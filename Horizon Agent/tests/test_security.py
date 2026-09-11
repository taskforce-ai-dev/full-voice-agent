"""Security regressions for the review round: WS ingress auth, dashboard-start
egress suppression, and /kb-reload path containment.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

HORIZON = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HORIZON))

import server  # noqa: E402

client = TestClient(server.app)


# ---------------------------------------------------------------------------
# WebSocket ingress ticket (blocker 3)
# ---------------------------------------------------------------------------

def test_ws_ticket_roundtrip(monkeypatch):
    monkeypatch.setenv("WS_TICKET_SECRET", "unit-secret")
    tkt = server._mint_ws_ticket()
    assert tkt and "." in tkt
    assert server._verify_ws_ticket(tkt) is True


def test_ws_ticket_rejects_forged_and_empty(monkeypatch):
    monkeypatch.setenv("WS_TICKET_SECRET", "unit-secret")
    assert server._verify_ws_ticket("") is False
    assert server._verify_ws_ticket("garbage") is False
    good = server._mint_ws_ticket()
    exp, _, sig = good.partition(".")
    assert server._verify_ws_ticket(f"{exp}.deadbeef") is False          # bad sig
    assert server._verify_ws_ticket(f"9999999999.{sig}") is False        # sig/exp mismatch


def test_ws_ticket_rejects_expired(monkeypatch):
    monkeypatch.setenv("WS_TICKET_SECRET", "unit-secret")
    import hmac, hashlib
    past = "1000000000"  # year 2001
    sig = hmac.new(b"unit-secret", past.encode(), hashlib.sha256).hexdigest()
    assert server._verify_ws_ticket(f"{past}.{sig}") is False


def test_ws_auth_disabled_without_secret(monkeypatch):
    monkeypatch.delenv("WS_TICKET_SECRET", raising=False)
    monkeypatch.setattr(server, "TWILIO_AUTH_TOKEN", "", raising=False)
    # No secret -> enforcement off (local dev), anything verifies.
    assert server._verify_ws_ticket("") is True


def test_ws_conversation_rejects_without_ticket(monkeypatch):
    monkeypatch.setenv("WS_TICKET_SECRET", "unit-secret")
    from starlette.websockets import WebSocketDisconnect
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws/conversation?lang=en") as ws:
            ws.receive_text()


def test_ws_media_stream_rejects_without_ticket(monkeypatch):
    monkeypatch.setenv("WS_TICKET_SECRET", "unit-secret")
    from starlette.websockets import WebSocketDisconnect
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws/media-stream/si") as ws:
            ws.receive_text()


def test_media_stream_ticket_rides_the_path_not_query(monkeypatch):
    """Twilio Media Streams drops query strings, so the ticket must be in the
    URL PATH (/ws/media-stream/si/<ticket>), never ?t=. Regression for the
    Sinhala/Arabic auto-hangup."""
    import re
    monkeypatch.setenv("WS_TICKET_SECRET", "unit-secret")
    body = client.post("/voice/demo-incoming", data={"lang": "si"}).text
    assert "/ws/media-stream/si/" in body          # ticket in the path
    assert "/ws/media-stream/si?t=" not in body     # NOT in the query
    m = re.search(r"/ws/media-stream/si/([^\"\s]+)", body)
    assert m, "no ticket segment found in the Media Streams URL"
    assert server._verify_ws_ticket(m.group(1)) is True


# ---------------------------------------------------------------------------
# Dashboard call-started egress suppression (blocker 1)
# ---------------------------------------------------------------------------

def test_dashboard_call_started_suppressed_by_default(monkeypatch):
    """With egress disabled (default), call-started (which carries caller phone)
    must NOT be dispatched even if a dashboard client is configured."""
    calls = {"n": 0}

    class _Spy:
        def send_call_started(self, *a, **k):
            calls["n"] += 1

    monkeypatch.setattr(server, "dashboard_client", _Spy())
    monkeypatch.setattr(server, "egress_enabled", lambda: False)
    server._dashboard_call_started("CA1", "+94770000000", "en", "2026-09-10T00:00:00")
    assert calls["n"] == 0


# ---------------------------------------------------------------------------
# /kb-reload filename containment (blocker 4)
# ---------------------------------------------------------------------------

def test_kb_filename_accepts_plain_names():
    assert server._safe_kb_filename("horizon_info.txt") == "horizon_info.txt"
    assert server._safe_kb_filename("notes.md") == "notes.md"


@pytest.mark.parametrize("bad", [
    "../secrets.txt",
    "../../etc/passwd",
    "/etc/passwd",
    "sub/dir.txt",
    "back\\slash.txt",
    "..",
    ".",
    "",
    "no_extension",
    "script.py",
    "data.json",
    "weird.txt\x00.py",
])
def test_kb_filename_rejects_hostile_names(bad):
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as ei:
        server._safe_kb_filename(bad)
    assert ei.value.status_code == 400
