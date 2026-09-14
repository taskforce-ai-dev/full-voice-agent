"""Dialog SmartPBX privacy contracts for the real post-call processor."""

from types import SimpleNamespace

import logging
import pytest

import booking_api
import dashboard_client
import post_call


class FakeResponse:
    status = 503

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def text(self):
        return "N8N-RESPONSE-SECRET"


class FakeSession:
    def __init__(self):
        self.payloads = []

    def post(self, _url, *, json, **_kwargs):
        self.payloads.append(json)
        return FakeResponse()


class FailingOpenAI:
    def __init__(self):
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=self.create),
        )

    async def create(self, **_kwargs):
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="MODEL-RESPONSE-SECRET"))]
        )


class FailingDashboard:
    async def send_call_completed(self, **_kwargs):
        raise RuntimeError("DASHBOARD-EXCEPTION-SECRET")


@pytest.mark.asyncio
async def test_dialog_real_post_call_processor_never_logs_raw_payload_or_failure_details(
    caplog, monkeypatch
):
    session = FakeSession()

    async def get_session():
        return session

    monkeypatch.setattr(booking_api, "get_session", get_session)
    monkeypatch.setattr(post_call, "dashboard_client", FailingDashboard())

    sentinels = (
        "OTHER-LEG-CALL-ID-SECRET",
        "+94770000000",
        "TRANSCRIPT-SECRET",
        "MODEL-RESPONSE-SECRET",
        "N8N-RESPONSE-SECRET",
        "DASHBOARD-EXCEPTION-SECRET",
    )
    with caplog.at_level(logging.INFO):
        await post_call.process_post_call_data(
            call_sid=sentinels[0],
            lang="en",
            caller_phone=sentinels[1],
            full_transcript=[{"role": "user", "text": sentinels[2]}],
            call_start_time="2026-08-06T00:00:00+00:00",
            call_end_time="2026-08-06T00:01:00+00:00",
            llm_provider="openai",
            openai_client=FailingOpenAI(),
            privacy_safe=True,
        )

    assert session.payloads[0]["call_sid"] == sentinels[0]
    assert sentinels[2] in session.payloads[0]["transcript"]
    for sentinel in sentinels:
        assert sentinel not in caplog.text
    assert "smartpbx_post_call event=processing_started" in caplog.text
    assert "smartpbx_post_call event=processing_completed" in caplog.text


@pytest.mark.asyncio
async def test_legacy_post_call_keeps_call_id_logging(monkeypatch, caplog):
    async def extraction(**_kwargs):
        return {"call_outcome": "other", "follow_up_needed": "No"}

    async def post_payload(_payload, **_kwargs):
        return None

    monkeypatch.setattr(post_call, "extract_booking_details", extraction)
    monkeypatch.setattr(post_call, "_post_to_n8n", post_payload)
    monkeypatch.setattr(post_call, "dashboard_client", None)

    with caplog.at_level(logging.INFO):
        await post_call.process_post_call_data(
            call_sid="legacy-call-id",
            lang="en",
            caller_phone="legacy-caller",
            full_transcript=[{"role": "user", "text": "legacy transcript"}],
            call_start_time="2026-08-06T00:00:00+00:00",
            call_end_time="2026-08-06T00:01:00+00:00",
            llm_provider="openai",
        )

    assert "Starting post-call processing for CallSid: legacy-call-id" in caplog.text
    assert "Post-call processing complete for legacy-call-id" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_mode", ["response", "exception"])
async def test_dialog_dashboard_transport_logs_no_response_or_exception_detail(
    failure_mode, caplog, monkeypatch
):
    sentinel = f"DASHBOARD-{failure_mode.upper()}-SECRET"

    class DashboardResponse(FakeResponse):
        async def text(self):
            return sentinel

    class DashboardSession:
        def post(self, *_args, **_kwargs):
            if failure_mode == "exception":
                raise RuntimeError(sentinel)
            return DashboardResponse()

    async def get_session():
        return DashboardSession()

    monkeypatch.setattr(booking_api, "get_session", get_session)
    monkeypatch.setattr(dashboard_client, "_ENABLED", True)
    monkeypatch.setattr(dashboard_client, "_announced", False)
    monkeypatch.setattr(dashboard_client, "DASHBOARD_API_URL", "https://dashboard.example")
    monkeypatch.setattr(dashboard_client, "DASHBOARD_API_KEY", "test-key")
    monkeypatch.setattr(dashboard_client, "DASHBOARD_AGENT_ID", "test-agent")

    with caplog.at_level(logging.INFO):
        await dashboard_client.send_call_completed(
            call_sid="DASHBOARD-CALL-ID-SECRET",
            caller_phone="DASHBOARD-CALLER-SECRET",
            lang="en",
            started_at_iso="2026-08-06T00:00:00+00:00",
            ended_at_iso="2026-08-06T00:01:00+00:00",
            duration_sec=60,
            full_transcript=[{"role": "user", "text": "DASHBOARD-TRANSCRIPT-SECRET"}],
            extracted={"summary": "DASHBOARD-SUMMARY-SECRET"},
            privacy_safe=True,
        )

    assert sentinel not in caplog.text
    assert "DASHBOARD-CALL-ID-SECRET" not in caplog.text
    assert "DASHBOARD-CALLER-SECRET" not in caplog.text
    assert "smartpbx_dashboard event=failed" in caplog.text


# ---------------------------------------------------------------------------
# C1 -- close_reason/close_code/guest_turns/agent_turns/duration_ms/barge_ins
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_post_call_payload_carries_close_diagnostics_and_turn_counts(monkeypatch):
    session = FakeSession()

    async def get_session():
        return session

    monkeypatch.setattr(booking_api, "get_session", get_session)
    monkeypatch.setattr(post_call, "dashboard_client", None)

    async def extraction(**_kwargs):
        return {"call_outcome": "other", "follow_up_needed": "No"}

    monkeypatch.setattr(post_call, "extract_booking_details", extraction)

    await post_call.process_post_call_data(
        call_sid="call-1",
        lang="si",
        caller_phone="+94770000000",
        full_transcript=[
            {"role": "assistant", "text": "hello"},
            {"role": "user", "text": "hi"},
            {"role": "user", "text": "book it"},
            {"role": "assistant", "text": "done"},
            {"role": "system", "text": "note"},
        ],
        call_start_time="2026-08-06T00:00:00+00:00",
        call_end_time="2026-08-06T00:01:00+00:00",
        llm_provider="openai",
        close_reason="hangup",
        close_code=1000,
        duration_ms=60_000,
        barge_ins=2,
        hangup_cause="normal_clearing",
    )

    payload = session.payloads[0]
    assert payload["close_reason"] == "hangup"
    assert payload["close_code"] == 1000
    assert payload["duration_ms"] == 60_000
    assert payload["barge_ins"] == 2
    assert payload["hangup_cause"] == "normal_clearing"
    assert payload["guest_turns"] == 2
    assert payload["agent_turns"] == 2
    assert payload["language_code"] == "si"
    # The existing full-name "language" field must be untouched by the new
    # code, and $json.body.* keeps working for every pre-existing field.
    assert payload["language"] == "Sinhala"
    assert payload["call_sid"] == "call-1"


@pytest.mark.asyncio
async def test_post_call_payload_omits_close_diagnostics_when_not_supplied(monkeypatch):
    """The Twilio call sites don't pass close_reason -- payload shape for them
    must not change (n8n column mapping there is untouched)."""
    session = FakeSession()

    async def get_session():
        return session

    monkeypatch.setattr(booking_api, "get_session", get_session)
    monkeypatch.setattr(post_call, "dashboard_client", None)

    async def extraction(**_kwargs):
        return {"call_outcome": "other", "follow_up_needed": "No"}

    monkeypatch.setattr(post_call, "extract_booking_details", extraction)

    await post_call.process_post_call_data(
        call_sid="legacy-call",
        lang="en",
        caller_phone="+94770000000",
        full_transcript=[{"role": "user", "text": "hi"}],
        call_start_time="2026-08-06T00:00:00+00:00",
        call_end_time="2026-08-06T00:01:00+00:00",
        llm_provider="openai",
    )

    payload = session.payloads[0]
    assert "close_reason" not in payload
    assert "close_code" not in payload
    assert "duration_ms" not in payload
    assert "barge_ins" not in payload
    assert "hangup_cause" not in payload
    assert payload["guest_turns"] == 1
    assert payload["agent_turns"] == 0
    assert payload["language_code"] == "en"


@pytest.mark.asyncio
async def test_dashboard_call_completed_puts_close_diagnostics_under_metadata(monkeypatch):
    sent = []

    async def fake_post(payload, **_kwargs):
        sent.append(payload)

    monkeypatch.setattr(dashboard_client, "_ENABLED", True)
    monkeypatch.setattr(dashboard_client, "_announced", False)
    monkeypatch.setattr(dashboard_client, "DASHBOARD_API_URL", "https://dashboard.example")
    monkeypatch.setattr(dashboard_client, "DASHBOARD_API_KEY", "test-key")
    monkeypatch.setattr(dashboard_client, "DASHBOARD_AGENT_ID", "test-agent")
    monkeypatch.setattr(dashboard_client, "_post", fake_post)

    await dashboard_client.send_call_completed(
        call_sid="call-1",
        caller_phone="+94770000000",
        lang="si",
        started_at_iso="2026-08-06T00:00:00+00:00",
        ended_at_iso="2026-08-06T00:01:00+00:00",
        duration_sec=60,
        full_transcript=[{"role": "user", "text": "hi"}],
        extracted={"summary": "s"},
        close_reason="hangup",
        close_code=1000,
        guest_turns=1,
        agent_turns=1,
        barge_ins=3,
    )

    metadata = sent[0]["call"]["metadata"]
    assert metadata["close_reason"] == "hangup"
    assert metadata["close_code"] == 1000
    assert metadata["guest_turns"] == 1
    assert metadata["agent_turns"] == 1
    assert metadata["barge_ins"] == 3
    # Pre-existing metadata keys are untouched.
    assert metadata["language"] == "si"


@pytest.mark.asyncio
async def test_dashboard_call_completed_omits_close_diagnostics_when_not_supplied(monkeypatch):
    sent = []

    async def fake_post(payload, **_kwargs):
        sent.append(payload)

    monkeypatch.setattr(dashboard_client, "_ENABLED", True)
    monkeypatch.setattr(dashboard_client, "_announced", False)
    monkeypatch.setattr(dashboard_client, "DASHBOARD_API_URL", "https://dashboard.example")
    monkeypatch.setattr(dashboard_client, "DASHBOARD_API_KEY", "test-key")
    monkeypatch.setattr(dashboard_client, "DASHBOARD_AGENT_ID", "test-agent")
    monkeypatch.setattr(dashboard_client, "_post", fake_post)

    await dashboard_client.send_call_completed(
        call_sid="call-1",
        caller_phone="+94770000000",
        lang="en",
        started_at_iso="2026-08-06T00:00:00+00:00",
        ended_at_iso="2026-08-06T00:01:00+00:00",
        duration_sec=60,
        full_transcript=[{"role": "user", "text": "hi"}],
        extracted={"summary": "s"},
    )

    metadata = sent[0]["call"]["metadata"]
    assert "close_reason" not in metadata
    assert "close_code" not in metadata
    assert "guest_turns" not in metadata
    assert "agent_turns" not in metadata
    assert "barge_ins" not in metadata
