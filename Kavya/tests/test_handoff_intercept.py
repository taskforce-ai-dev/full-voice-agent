"""Carrier-intercept detection on the human-transfer leg.

Regression cover for 2026-08-03: a transfer to the manager was answered in the
same second it was dialled by a carrier intercept, which played a recording at
the guest for 52 seconds and reported DialCallStatus=completed. The handover
code read "completed" as a successful pickup, stood the failsafe down, and
nobody was ever told the guest had called.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import server  # noqa: E402

CALL = "CA_INTERCEPT_TEST"


@pytest.fixture
def client():
    return TestClient(server.app)


@pytest.fixture(autouse=True)
def clean_state():
    server._handoff_state.clear()
    yield
    server._handoff_state.clear()


def _seed(**overrides):
    state = {"reason": "wants a group rate", "caller_phone": "+94776697566"}
    state.update(overrides)
    server._remember_handoff(CALL, **state)
    return server._handoff_state[CALL]


def _dial_result(client, status="completed", call_sid=CALL):
    return client.post(
        "/voice/dial-result",
        data={"DialCallStatus": status, "CallSid": call_sid},
    )


# ---------------------------------------------------------------------------
# _answer_looks_intercepted
# ---------------------------------------------------------------------------

def test_instant_answer_is_flagged():
    """The real incident: initiated and answered in the same second."""
    intercepted, why = server._answer_looks_intercepted(
        {"dial_events": {"initiated": 1000.0, "answered": 1000.0}}
    )
    assert intercepted is True
    assert "too fast" in why


def test_human_pickup_is_not_flagged():
    """The good call the same day: answered ~8s in after real ringing."""
    intercepted, _ = server._answer_looks_intercepted(
        {"dial_events": {"initiated": 1000.0, "ringing": 1001.0, "answered": 1008.0}}
    )
    assert intercepted is False


@pytest.mark.parametrize("gap,expected", [
    (0.0, True), (0.5, True), (1.99, True),
    (2.0, False), (3.0, False), (25.0, False),
])
def test_threshold_boundary(gap, expected):
    intercepted, _ = server._answer_looks_intercepted(
        {"dial_events": {"initiated": 100.0, "answered": 100.0 + gap}}
    )
    assert intercepted is expected


@pytest.mark.parametrize("events", [
    {},                                  # no callbacks arrived
    {"dial_events": {}},                 # endpoint hit but nothing recorded
    {"dial_events": {"initiated": 1.0}}, # never answered
    {"dial_events": {"answered": 1.0}},  # answered but no initiate timestamp
])
def test_fails_open_without_timing(events):
    """Missing timing must NOT bounce a guest who really spoke to a human."""
    intercepted, _ = server._answer_looks_intercepted(events)
    assert intercepted is False


# ---------------------------------------------------------------------------
# /voice/dial-status
# ---------------------------------------------------------------------------

def test_status_callback_records_events(client):
    _seed()
    for ev in ("initiated", "ringing", "answered"):
        r = client.post(f"/voice/dial-status?parent={CALL}", data={"CallStatus": ev})
        assert r.status_code == 204
    assert set(server._handoff_state[CALL]["dial_events"]) == {
        "initiated", "ringing", "answered",
    }


def test_status_callback_retry_keeps_the_first_timestamp(client):
    """Twilio retries callbacks; a retry must not rewrite the original timing."""
    _seed()
    client.post(f"/voice/dial-status?parent={CALL}", data={"CallStatus": "initiated"})
    first = server._handoff_state[CALL]["dial_events"]["initiated"]
    client.post(f"/voice/dial-status?parent={CALL}", data={"CallStatus": "initiated"})
    assert server._handoff_state[CALL]["dial_events"]["initiated"] == first


def test_status_callback_ignores_unknown_calls(client):
    """A stray or replayed callback must not grow the state dict."""
    r = client.post("/voice/dial-status?parent=CA_NEVER_SEEN", data={"CallStatus": "answered"})
    assert r.status_code == 204
    assert "CA_NEVER_SEEN" not in server._handoff_state


# ---------------------------------------------------------------------------
# End to end through /voice/dial-result
# ---------------------------------------------------------------------------

def test_intercepted_completed_runs_the_failsafe(client):
    """The whole point: 'completed' + instant answer must still notify the manager."""
    _seed(dial_events={"initiated": 500.0, "answered": 500.0})

    resp = _dial_result(client, "completed")

    assert resp.status_code == 200
    assert "mode=handover_failsafe" in resp.text
    assert "<Hangup/>" not in resp.text
    # Carry-over must survive so the failsafe session can use it.
    assert CALL in server._handoff_state
    assert server._handoff_state[CALL]["dial_status"] == "intercepted"


def test_genuine_pickup_still_hangs_up(client):
    _seed(dial_events={"initiated": 500.0, "ringing": 501.0, "answered": 512.0})

    resp = _dial_result(client, "completed")

    assert "<Hangup/>" in resp.text
    assert "mode=handover_failsafe" not in resp.text
    assert CALL not in server._handoff_state  # cleared on success


def test_completed_without_timing_still_hangs_up(client):
    """Fail-open: no status callbacks means we trust Twilio's 'completed'."""
    _seed()
    resp = _dial_result(client, "completed")
    assert "<Hangup/>" in resp.text


@pytest.mark.parametrize("status", ["no-answer", "busy", "failed", "canceled"])
def test_ordinary_failures_still_run_the_failsafe(client, status):
    _seed()
    resp = _dial_result(client, status)
    assert "mode=handover_failsafe" in resp.text
    assert server._handoff_state[CALL]["dial_status"] == status


# ---------------------------------------------------------------------------
# Dial TwiML wiring
# ---------------------------------------------------------------------------

def test_transfer_twiml_requests_the_status_callbacks():
    """Without these attributes there is no timing to judge the answer by."""
    import inspect

    src = inspect.getsource(server)
    assert 'statusCallbackEvent="initiated ringing answered completed"' in src
    assert "/voice/dial-status?parent=" in src


def test_caller_id_helper_has_no_hidden_fallback():
    """The old comment claimed unset fell back to an owned number. It does not -
    that wrong comment is why production ran on pass-through for three days."""
    server_module_value = server.TWILIO_CALLER_ID
    assert server._transfer_caller_id("CA_ANY") == server_module_value
