"""ASGI-level contract tests for the Direct SmartPBX service app.

Before this file, `grep -r "TestClient|websocket_connect" tests/` was zero
hits (audit-tests.md sec 2.2, gap #8 / #13): the `/ws/v1/smartpbx/media`
route handler was only ever reached through hand-built fake sockets whose
`receive_text` blocks forever instead of raising `WebSocketDisconnect`, and
`lifespan()` was never exercised through `build_service_app`, so "SmartPBX
mode skips Twilio startup" was asserted only by reading the source.
"""

from __future__ import annotations

import asyncio
import base64
import time

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect


def smartpbx_env(**overrides) -> dict[str, str]:
    environment = {
        "ENABLE_SMARTPBX_WSS": "true",
        "SMARTPBX_WS_TOKEN": "asgi-test-token",
        "SMARTPBX_ACCOUNT_ID": "asgi-account",
    }
    environment.update(overrides)
    return environment


class _FakeAsgiSession:
    """Minimal stand-in for KavyaSmartPBXSession -- no STT/LLM/TTS clients."""

    def __init__(self, context, transport):
        self.context = context
        self.transport = transport
        self.terminal_future = asyncio.get_running_loop().create_future()

    async def start(self):
        pass

    async def feed_audio(self, audio):
        pass

    async def finish(self, schedule_post_call=False, **close_kwargs):
        pass


def _wait_until(predicate, *, timeout=2.0, interval=0.01):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


# -- bad token rejected before accept ----------------------------------------


def test_media_socket_rejects_bad_token_before_accept(monkeypatch):
    import server

    app = server.build_service_app("smartpbx", smartpbx_env())
    gateway = app.state.smartpbx_gateway

    client = TestClient(app)
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(
            "/ws/v1/smartpbx/media",
            headers={"X-Kavya-SmartPBX-Token": "wrong-token"},
        ):
            pass  # pragma: no cover -- the handshake itself must fail

    snapshot = gateway.snapshot()
    assert snapshot["active_sessions"] == 0
    assert snapshot["admitted_total"] == 0


def test_media_socket_missing_token_header_rejected_before_accept():
    import server

    app = server.build_service_app("smartpbx", smartpbx_env())
    gateway = app.state.smartpbx_gateway

    client = TestClient(app)
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws/v1/smartpbx/media"):
            pass  # pragma: no cover

    assert gateway.snapshot()["admitted_total"] == 0


# -- good token + peer disconnect releases the slot --------------------------


def test_media_socket_peer_disconnect_releases_slot(monkeypatch):
    import server

    async def fake_new_smartpbx_session(context, transport, diagnostic_sink=None):
        return _FakeAsgiSession(context, transport)

    monkeypatch.setattr(server, "_new_smartpbx_session", fake_new_smartpbx_session)

    app = server.build_service_app("smartpbx", smartpbx_env())
    gateway = app.state.smartpbx_gateway

    client = TestClient(app)
    with client.websocket_connect(
        "/ws/v1/smartpbx/media",
        headers={"X-Kavya-SmartPBX-Token": "asgi-test-token"},
    ) as websocket:
        websocket.send_text(
            '{"event":"start","start":{"callId":"call-1",'
            '"otherLegCallId":"other-1","callerIdNumber":"caller-opaque",'
            '"calleeIdNumber":"callee-opaque","accountId":"asgi-account",'
            '"mediaFormat":{"encoding":"g711_ulaw","sampleRate":8000}}}'
        )
        assert _wait_until(lambda: gateway.snapshot()["active_sessions"] == 1), (
            f"session never admitted: {gateway.snapshot()}"
        )
        # The `with` block's exit closes the client side of the socket --
        # this is the real Starlette peer-disconnect path (WebSocketDisconnect
        # raised out of receive_text), not a hand-built fake that blocks
        # forever instead.

    assert _wait_until(lambda: gateway.snapshot()["active_sessions"] == 0), (
        f"slot never released after peer disconnect: {gateway.snapshot()}"
    )
    assert gateway.snapshot()["released_total"] == 1


def test_media_socket_rejects_wrong_codec_at_start(monkeypatch):
    """Bad-shape `start` still fails closed once inside an accepted session."""
    import server

    async def fake_new_smartpbx_session(context, transport, diagnostic_sink=None):
        return _FakeAsgiSession(context, transport)

    monkeypatch.setattr(server, "_new_smartpbx_session", fake_new_smartpbx_session)

    app = server.build_service_app("smartpbx", smartpbx_env())
    gateway = app.state.smartpbx_gateway

    client = TestClient(app)
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(
            "/ws/v1/smartpbx/media",
            headers={"X-Kavya-SmartPBX-Token": "asgi-test-token"},
        ) as websocket:
            websocket.send_text(
                '{"event":"start","start":{"callId":"call-1",'
                '"otherLegCallId":"other-1","callerIdNumber":"caller-opaque",'
                '"calleeIdNumber":"callee-opaque","accountId":"asgi-account",'
                '"mediaFormat":{"encoding":"g722","sampleRate":16000}}}'
            )
            websocket.receive_text()

    assert _wait_until(lambda: gateway.snapshot()["active_sessions"] == 0)


# -- lifespan never constructs the Twilio client in smartpbx mode ------------


def test_smartpbx_lifespan_skips_twilio_client(monkeypatch):
    import server

    monkeypatch.setattr(server, "KAVYA_SERVICE_MODE", "smartpbx")
    monkeypatch.setattr(server, "initialize_kb", lambda *_a, **_kw: True)
    monkeypatch.setattr(server, "prewarm", lambda: None)
    # Truthy credentials so, if the service-mode gate around
    # `_get_twilio_client()` in `lifespan()` were ever removed or inverted,
    # this test would actually observe the call rather than passing
    # vacuously because the credentials were empty.
    monkeypatch.setattr(server, "TWILIO_ACCOUNT_SID", "ACfakefakefakefakefakefakefakefake")
    monkeypatch.setattr(server, "TWILIO_AUTH_TOKEN", "fake-auth-token")

    calls: list[None] = []
    monkeypatch.setattr(server, "_get_twilio_client", lambda: calls.append(None))

    app = server.build_service_app("smartpbx", smartpbx_env())

    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service_mode": "smartpbx"}
    assert calls == [], "smartpbx mode must never construct the Twilio REST client"


def test_twilio_lifespan_still_constructs_twilio_client_when_configured(monkeypatch):
    """Control case: the gate in the test above is real, not a tautology."""
    import server

    monkeypatch.setattr(server, "KAVYA_SERVICE_MODE", "twilio")
    monkeypatch.setattr(server, "initialize_kb", lambda *_a, **_kw: True)
    monkeypatch.setattr(server, "prewarm", lambda: None)
    monkeypatch.setattr(server, "TWILIO_ACCOUNT_SID", "ACfakefakefakefakefakefakefakefake")
    monkeypatch.setattr(server, "TWILIO_AUTH_TOKEN", "fake-auth-token")

    calls: list[None] = []
    monkeypatch.setattr(server, "_get_twilio_client", lambda: calls.append(None))

    with TestClient(server._twilio_app):
        pass

    assert calls == [None]
