"""Carrier-intercept detection on the human-transfer leg.

Regression cover for 2026-08-03: a transfer to the manager was answered in the
same second it was dialled by a carrier intercept, which played a recording at
the guest for 52 seconds and reported DialCallStatus=completed. The handover
code read "completed" as a successful pickup, stood the failsafe down, and
nobody was ever told the guest had called.
"""

from __future__ import annotations

import sys
import time
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
# The actual bug: Twilio's real payload uses CallStatus=in-progress, never
# CallStatus=answered. statusCallbackEvent is *named* "answered" but that is
# a DialCallStatus vocabulary word, not what shows up in the CallStatus field
# of the callback. Every test above builds dial_events by hand with an
# "answered" key or POSTs a literal CallStatus=answered — a payload Twilio
# does not produce — so none of them would catch this. These replay the real
# wire payload and the real production timings from 2026-08-04.
# ---------------------------------------------------------------------------

def test_dial_status_normalizes_in_progress_to_answered(client):
    """Twilio never sends CallStatus=answered — the real callback for a
    picked-up leg carries CallStatus=in-progress. Unless the handler maps
    that onto the canonical "answered" key, _answer_looks_intercepted's
    events.get("answered") lookup is always None and the detector never
    fires. This must fail against unfixed code.
    """
    _seed()
    r = client.post(
        f"/voice/dial-status?parent={CALL}", data={"CallStatus": "in-progress"}
    )
    assert r.status_code == 204
    events = server._handoff_state[CALL]["dial_events"]
    assert "answered" in events, (
        "in-progress must be normalised to the canonical 'answered' key so "
        f"_answer_looks_intercepted can find it (got keys: {sorted(events)})"
    )


def _shift_clock(monkeypatch, offset):
    """Shift server.py's notion of "now" by `offset` seconds, relative to the
    real clock at call time, for every subsequent time.time() call — not
    just the next one.

    A plain fixed-sequence fake (e.g. an iterator of canned timestamps) does
    not work here: the test client's own machinery (httpx's cookie jar) and
    Python's logging module (LogRecord timestamps) also call time.time()
    during the same request, consuming values meant for the handler under
    test. Offsetting the real clock instead means every caller — ours or
    incidental — gets a consistent, still-monotonic answer.
    """
    real_time = time.time
    monkeypatch.setattr(server.time, "time", lambda: real_time() + offset)


def test_call2_production_intercept_is_caught(client, monkeypatch):
    """Reproduces production Call 2 (CA7396bfdc41c0112684942259b2257a9e,
    2026-08-04): initiated -> in-progress 0.598s later, no ringing event —
    the manager's phone never rang, a carrier intercept picked up instantly.
    Posts the real Twilio payload end to end through /voice/dial-status and
    /voice/dial-result and asserts the transfer is treated as intercepted,
    not accepted.
    """
    _seed()
    client.post(f"/voice/dial-status?parent={CALL}", data={"CallStatus": "initiated"})
    _shift_clock(monkeypatch, 0.598)
    client.post(f"/voice/dial-status?parent={CALL}", data={"CallStatus": "in-progress"})

    resp = _dial_result(client, "completed")

    assert "mode=handover_failsafe" in resp.text
    assert "<Hangup/>" not in resp.text
    assert server._handoff_state[CALL]["dial_status"] == "intercepted"


def test_call1_production_genuine_answer_is_accepted(client, monkeypatch):
    """Reproduces production Call 1 (CA2b9355fde83632735ba6cb9d22cf5295,
    2026-08-04): initiated -> ringing -> in-progress ~21s later — the
    manager's phone rang and he answered it, confirmed by hand. Posts the
    real Twilio payload end to end and asserts the transfer is accepted.
    """
    _seed()
    client.post(f"/voice/dial-status?parent={CALL}", data={"CallStatus": "initiated"})
    client.post(f"/voice/dial-status?parent={CALL}", data={"CallStatus": "ringing"})
    _shift_clock(monkeypatch, 21.0)  # ~21s initiated -> in-progress, as observed
    client.post(f"/voice/dial-status?parent={CALL}", data={"CallStatus": "in-progress"})

    resp = _dial_result(client, "completed")

    assert "<Hangup/>" in resp.text
    assert "mode=handover_failsafe" not in resp.text
    assert CALL not in server._handoff_state


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


# ---------------------------------------------------------------------------
# The manager is paged from dial-result, not only from the recovery session
# ---------------------------------------------------------------------------
#
# Live on 2026-08-05 two consecutive intercepted transfers both reached
# "entering failsafe" and notified nobody: the guest had spent the whole dial
# listening to an intercept recording and hung up, so the recovery session's
# WebSocket never opened and neither of its notification paths could run.

@pytest.fixture
def sent(monkeypatch):
    """Capture handover notifications at the n8n boundary."""
    calls: list[dict] = []

    async def _fake_send(**kwargs):
        calls.append(kwargs)
        return {"ok": True, "status": 200}

    monkeypatch.setattr(server, "send_handover_notification", _fake_send)
    return calls


@pytest.mark.asyncio
async def test_intercepted_transfer_pages_the_manager_without_a_recovery_session(
    client, sent, monkeypatch
):
    """The guest hangs up during the intercept. Nobody collects their details.

    The manager must still be told, using the number they rang from. This is
    the whole point of the failsafe and it is the case that was silently
    broken.
    """
    import asyncio

    _seed()
    client.post(f"/voice/dial-status?parent={CALL}", data={"CallStatus": "initiated"})
    client.post(f"/voice/dial-status?parent={CALL}", data={"CallStatus": "in-progress"})

    resp = _dial_result(client, "completed")
    assert "mode=handover_failsafe" in resp.text  # judged NOT answered

    # The notify is fire-and-forget; let the scheduled task run.
    await asyncio.sleep(0)
    for _ in range(10):
        if sent:
            break
        await asyncio.sleep(0.01)

    assert sent, "manager was NOT notified when the transfer was intercepted"
    assert sent[0]["customer_whatsapp"] == "+94776697566"
    assert "NOT answered" in sent[0]["call_summary"]
    assert "call them back" in sent[0]["call_summary"]


@pytest.mark.asyncio
async def test_manager_is_not_paged_twice_for_one_failed_transfer(
    client, sent
):
    """`notified` guards the duplicate. The end-of-session net must stand down."""
    import asyncio

    _seed()
    client.post(f"/voice/dial-status?parent={CALL}", data={"CallStatus": "initiated"})
    client.post(f"/voice/dial-status?parent={CALL}", data={"CallStatus": "in-progress"})
    _dial_result(client, "completed")

    for _ in range(10):
        if sent:
            break
        await asyncio.sleep(0.01)

    assert server._handoff_state[CALL]["notified"] is True

    # Replay the end-of-session fallback the way the WebSocket `finally` does.
    state = server._handoff_state[CALL]
    if not state.get("notified"):  # pragma: no cover - must not fire
        await server._notify_handover_fallback(
            call_sid=CALL, state=state, caller_phone="+94776697566",
            full_transcript=[],
        )

    assert len(sent) == 1


@pytest.mark.asyncio
async def test_genuine_pickup_does_not_page_the_manager(client, sent, monkeypatch):
    """A real human answered. Paging them about a missed call would be noise."""
    import asyncio

    _seed()
    client.post(f"/voice/dial-status?parent={CALL}", data={"CallStatus": "initiated"})
    client.post(f"/voice/dial-status?parent={CALL}", data={"CallStatus": "ringing"})
    _shift_clock(monkeypatch, 21.0)
    client.post(f"/voice/dial-status?parent={CALL}", data={"CallStatus": "in-progress"})

    resp = _dial_result(client, "completed")
    assert "<Hangup/>" in resp.text

    await asyncio.sleep(0.05)
    assert sent == []
