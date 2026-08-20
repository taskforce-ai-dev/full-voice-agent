"""Deterministic rate resolution must precede semantic knowledge retrieval.

These tests define the rate-catalog boundary used by the shared MediaStreamSession
path.  They intentionally exercise the session's real slot and provider seams;
no model response is mocked to manufacture a price.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import rate_catalog
import server
import tools
from yanolja_service import DEMO_NIGHTLY_RATE_USD


def resolve(room: str, residency: str, check_in: str, check_out: str):
    return rate_catalog.resolve_rate(
        room=room,
        residency=residency,
        check_in=check_in,
        check_out=check_out,
    )


@pytest.mark.parametrize(
    ("room", "off_peak", "peak"),
    (
        ("Forest Escape Suite", 158000, 185000),
        ("Eco Harmony Suite", 180000, 211000),
        ("Sunrise Vista Premium Suite", 214000, 250000),
        ("Mount Luxe Chalet", 259000, 303000),
        ("Mount Monarch Chalet", 315000, 368000),
    ),
)
def test_resident_catalog_covers_every_room_in_each_season(room, off_peak, peak):
    for check_in, check_out, expected in (
        ("2026-09-26", "2026-09-29", off_peak),
        ("2026-04-10", "2026-04-12", peak),
    ):
        result = resolve(room, "resident", check_in, check_out)

        assert result.is_quotable
        assert result.currency == "LKR"
        assert result.nightly_rate == expected
        assert result.room == room


def test_foreign_catalog_reuses_the_existing_usd_rate_source():
    for room, expected in DEMO_NIGHTLY_RATE_USD.items():
        result = resolve(room, "foreign", "2026-09-26", "2026-09-29")

        assert result.is_quotable
        assert result.currency == "USD"
        assert result.nightly_rate == expected


def test_incident_mount_monarch_resident_rate_is_the_off_peak_lkr_rate():
    result = resolve(
        "Mount Monarch Chalet", "resident", "2026-09-26", "2026-09-29"
    )

    assert result.is_quotable
    assert result.currency == "LKR"
    assert result.nightly_rate == 315000


def test_mount_monarch_april_resident_rate_is_peak_lkr_rate():
    result = resolve(
        "Mount Monarch Chalet", "resident", "2026-04-10", "2026-04-12"
    )

    assert result.is_quotable
    assert result.currency == "LKR"
    assert result.nightly_rate == 368000


def test_december_uses_the_resident_peak_rate():
    result = resolve("Eco Harmony Suite", "resident", "2026-12-20", "2026-12-22")

    assert result.is_quotable
    assert result.currency == "LKR"
    assert result.nightly_rate == 211000


def test_checkout_on_the_first_peak_day_is_still_an_off_peak_stay():
    result = resolve(
        "Mount Monarch Chalet", "resident", "2026-03-31", "2026-04-01"
    )

    assert result.is_quotable
    assert result.currency == "LKR"
    assert result.nightly_rate == 315000


def test_stay_crossing_seasons_fails_closed_instead_of_quoting_one_rate():
    result = resolve(
        "Mount Monarch Chalet", "resident", "2026-03-31", "2026-04-02"
    )

    assert not result.is_quotable
    assert result.reason == "mixed_period"
    assert result.nightly_rate is None


@pytest.mark.parametrize(
    ("room", "residency", "check_in", "check_out"),
    (
        ("", "resident", "2026-09-26", "2026-09-29"),
        ("Unknown room", "resident", "2026-09-26", "2026-09-29"),
        ("Mount Monarch Chalet", "", "2026-09-26", "2026-09-29"),
        ("Mount Monarch Chalet", "unknown", "2026-09-26", "2026-09-29"),
        ("Mount Monarch Chalet", "resident", "", "2026-09-29"),
        ("Mount Monarch Chalet", "resident", "2026-09-26", ""),
    ),
)
def test_missing_or_unknown_rate_inputs_never_invent_a_quote(
    room, residency, check_in, check_out
):
    result = resolve(room, residency, check_in, check_out)

    assert not result.is_quotable
    assert result.nightly_rate is None


@pytest.mark.parametrize(
    ("utterance", "expected"),
    (
        ("I am a Sri Lankan resident", "resident"),
        ("I am local", "resident"),
        ("I am a foreign guest", "foreign"),
        ("I am not a Sri Lankan resident", "foreign"),
        ("I am not local", "foreign"),
        ("My surname sounds Sri Lankan", None),
    ),
)
def test_residency_recognition_requires_an_explicit_safe_statement(utterance, expected):
    assert rate_catalog.recognize_residency(utterance) == expected


@pytest.mark.parametrize(
    ("utterance", "expected"),
    (
        ("Sri Lankan resident", "resident"),
        ("foreign guest", "foreign"),
    ),
)
def test_terse_residency_answers_are_safe_direct_replies(utterance, expected):
    assert rate_catalog.recognize_residency(utterance) == expected


def test_one_canonical_room_in_an_availability_request_is_captured_but_two_are_ambiguous():
    assert rate_catalog.recognize_selected_room(
        "Is Mount Monarch Chalet available from September 26 to 29?"
    ) == "Mount Monarch Chalet"
    assert rate_catalog.recognize_selected_room(
        "Is Mount Monarch Chalet or Mount Luxe Chalet available?"
    ) is None


@pytest.mark.parametrize(
    ("utterance", "expected"),
    (
        ("A guest from overseas is arriving too.", None),
        ("I am local but my partner is foreign.", None),
        ("Foreign guests have different prices, right?", None),
    ),
)
def test_residency_recognition_rejects_other_people_and_conflicting_statements(
    utterance, expected
):
    assert rate_catalog.recognize_residency(utterance) == expected


def test_explicit_residency_correction_replaces_the_prior_call_state():
    session = server.MediaStreamSession(websocket=None, lang="en", media_transport=None)

    session._capture_explicit_residency("I am local")
    session._capture_explicit_residency("Actually, I am not local")

    assert session._booking_slots["residency"] == "foreign"


def test_foreign_rate_respects_the_existing_demo_rate_kill_switch(monkeypatch):
    monkeypatch.setattr("yanolja_service.DEMO_RATES_ENABLED", False)

    result = resolve("Mount Monarch Chalet", "foreign", "2026-09-26", "2026-09-29")

    assert not result.is_quotable
    assert result.nightly_rate is None
    assert result.reason == "rates_disabled"


async def _one_event_stream(event):
    yield event


class _RecordingOpenAI:
    def __init__(self):
        self.requests: list[dict] = []
        self.chat = SimpleNamespace(completions=self)

    async def create(self, **kwargs):
        self.requests.append(kwargs)
        return _one_event_stream(
            SimpleNamespace(
                choices=[SimpleNamespace(
                    delta=SimpleNamespace(content="Acknowledged.", tool_calls=None)
                )]
            )
        )


class _RecordingGeminiModels:
    def __init__(self, owner):
        self.owner = owner

    async def generate_content_stream(self, **kwargs):
        self.owner.requests.append(kwargs)
        return _one_event_stream(
            SimpleNamespace(candidates=[SimpleNamespace(
                finish_reason=None,
                content=SimpleNamespace(parts=[SimpleNamespace(
                    text="Acknowledged.", function_call=None,
                )]),
            )])
        )


class _RecordingGemini:
    def __init__(self):
        self.requests: list[dict] = []
        self.aio = SimpleNamespace(models=_RecordingGeminiModels(self))


class _RecordingClaudeStream:
    async def __aenter__(self):
        return _one_event_stream(
            SimpleNamespace(
                type="content_block_delta",
                delta=SimpleNamespace(type="text_delta", text="Acknowledged."),
            )
        )

    async def __aexit__(self, *_args):
        return False


class _RecordingClaudeMessages:
    def __init__(self, owner):
        self.owner = owner

    def stream(self, **kwargs):
        self.owner.requests.append(kwargs)
        return _RecordingClaudeStream()


class _RecordingClaude:
    def __init__(self):
        self.requests: list[dict] = []
        self.messages = _RecordingClaudeMessages(self)


def _provider_session(provider: str):
    clients = {
        "openai": _RecordingOpenAI(),
        "gemini": _RecordingGemini(),
        "claude": _RecordingClaude(),
    }
    session = server.MediaStreamSession(
        websocket=None,
        lang="en",
        openai_client=clients["openai"],
        gemini_client=clients["gemini"],
        anthropic_client=clients["claude"],
        media_transport=None,
        llm_provider=provider,
        model=f"{provider}-rate-test",
    )
    session.tools = []

    async def _no_speak(*_args, **_kwargs):
        return None

    session._invoke_speak = _no_speak
    return session, clients[provider]


def _request_text(provider: str, client) -> str:
    request = client.requests[-1]
    if provider == "openai":
        return str(request["messages"])
    if provider == "gemini":
        return str(request["config"]) + str(request["contents"])
    return str(request["system"]) + str(request["messages"])


def _availability_input() -> dict[str, str]:
    return {"check_in": "2026-09-26", "check_out": "2026-09-29"}


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ("openai", "gemini", "claude"))
async def test_real_availability_then_selected_room_then_residency_sends_one_exact_rate_record(
    monkeypatch, provider
):
    availability = next(
        tool for tool in tools.TOOL_DEFINITIONS if tool["name"] == "check_availability"
    )
    assert "room_type" not in availability["input_schema"]["properties"]

    session, client = _provider_session(provider)
    session._capture_booking_slots("check_availability", _availability_input())
    monkeypatch.setattr(
        server,
        "retrieve_context",
        lambda _text: "CONTRADICTORY_RATE: 1400 USD and 368000 LKR.",
    )

    await session._process_utterance_bound(
        "Is Mount Monarch Chalet available from September 26 to 29?"
    )
    await session._process_utterance_bound("I am a Sri Lankan resident.")

    assert session._booking_slots["room_type"] == "Mount Monarch Chalet"
    request_text = _request_text(provider, client)
    assert "AUTHORITATIVE RATE RECORD" in request_text
    assert "rate_per_room_per_night: 315000" in request_text
    assert "CONTRADICTORY_RATE" not in request_text
    assert "1400 USD" not in request_text
    assert "368000 LKR" not in request_text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "slots",
    (
        {"residency": "resident", "check_in": "2026-09-26", "check_out": "2026-09-29"},
        {"room_type": "Unknown room", "residency": "resident", "check_in": "2026-09-26", "check_out": "2026-09-29"},
        {"room_type": "Mount Monarch Chalet", "check_in": "2026-09-26", "check_out": "2026-09-29"},
        {"room_type": "Mount Monarch Chalet", "residency": "unknown", "check_in": "2026-09-26", "check_out": "2026-09-29"},
        {"room_type": "Mount Monarch Chalet", "residency": "resident", "check_in": "2026-09-26"},
    ),
)
async def test_incomplete_or_unknown_session_rate_state_is_an_authoritative_no_quote_turn(
    monkeypatch, slots
):
    session, client = _provider_session("openai")
    session._booking_slots.update(slots)
    monkeypatch.setattr(
        server, "retrieve_context", lambda _text: "CONTRADICTORY_RATE: 1400 USD."
    )

    await session._process_utterance_bound("What is the nightly rate?")

    request_text = _request_text("openai", client)
    assert "AUTHORITATIVE RATE RECORD" in request_text
    assert "status: no_quote" in request_text
    assert "CONTRADICTORY_RATE" not in request_text


@pytest.mark.asyncio
async def test_completed_rate_state_still_uses_descriptive_kb_for_an_unrelated_turn(monkeypatch):
    session, client = _provider_session("openai")
    session._booking_slots.update({
        "room_type": "Mount Monarch Chalet",
        "residency": "resident",
        "check_in": "2026-09-26",
        "check_out": "2026-09-29",
    })
    monkeypatch.setattr(server, "retrieve_context", lambda _text: "POOL_DETAILS: private plunge pool.")

    await session._process_utterance_bound("Please tell me about the pool.")

    request_text = _request_text("openai", client)
    assert "POOL_DETAILS" in request_text
    assert "AUTHORITATIVE RATE RECORD" not in request_text
