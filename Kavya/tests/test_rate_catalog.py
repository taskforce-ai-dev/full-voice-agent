"""Deterministic rate resolution must precede semantic knowledge retrieval.

These tests define the rate-catalog boundary used by the shared MediaStreamSession
path.  They intentionally exercise the session's real slot and provider seams;
no model response is mocked to manufacture a price.
"""

from __future__ import annotations

import asyncio
import threading
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
    "utterance",
    (
        "Is Mount Monarch Chalets available?",
        "Is Mount Monarch ChaletSuite available?",
    ),
)
def test_room_matching_rejects_suffix_and_prefix_collisions(utterance):
    assert rate_catalog.recognize_selected_room(utterance) is None


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


def test_resident_rate_respects_the_existing_demo_rate_kill_switch(monkeypatch):
    monkeypatch.setattr("yanolja_service.DEMO_RATES_ENABLED", False)

    result = resolve("Mount Monarch Chalet", "resident", "2026-09-26", "2026-09-29")

    assert not result.is_quotable
    assert result.nightly_rate is None
    assert result.reason == "rates_disabled"


@pytest.mark.parametrize(
    ("utterance", "expected"),
    (
        ("I'm local", "resident"),
        ("I'm not local", "foreign"),
        ("I'm a foreign guest", "foreign"),
    ),
)
def test_contracted_explicit_residency_statements_persist(utterance, expected):
    session = server.MediaStreamSession(websocket=None, lang="en", media_transport=None)

    session._capture_explicit_residency(utterance)

    assert session._booking_slots["residency"] == expected


def _rate_intent(utterance: str):
    return rate_catalog.classify_room_rate_intent(utterance)


@pytest.mark.parametrize(
    ("utterance", "kind", "rooms"),
    (
        (
            "What is the room rate for Mount Monarch Chalet or Mount Luxe Chalet?",
            "AMBIGUOUS_RATE",
            ("Mount Monarch Chalet", "Mount Luxe Chalet"),
        ),
        (
            "How much are Mount Monarch Chalet and Mount Luxe Chalet?",
            "AMBIGUOUS_RATE",
            ("Mount Monarch Chalet", "Mount Luxe Chalet"),
        ),
        ("How much does Mount Monarch Chalet cost?", "RATE", ("Mount Monarch Chalet",)),
        ("What is the price of Mount Monarch Chalet?", "RATE", ("Mount Monarch Chalet",)),
        ("What is the price of the Mount Monarch Chalet?", "RATE", ("Mount Monarch Chalet",)),
        ("How much is Mount Monarch Chalet for one night?", "RATE", ("Mount Monarch Chalet",)),
    ),
)
def test_room_rate_classifier_has_structured_ambiguous_and_natural_price_results(
    utterance, kind, rooms
):
    classification = _rate_intent(utterance)

    assert classification.kind == kind
    assert classification.rooms == rooms


@pytest.mark.parametrize(
    "utterance",
    (
        "How much is Mount Monarch Chalet or Mount Luxe Chalet?",
        "How much are Mount Monarch Chalet and Mount Luxe Chalet?",
        "How much is Mount Monarch Chalet or Mount Luxe Chalet per night?",
        "How much are Mount Monarch Chalet and Mount Luxe Chalet per night?",
        "How much is Mount Monarch Chalet or Mount Luxe Chalet per room per night?",
        "How much are Mount Monarch Chalet and Mount Luxe Chalet per room per night?",
        "How much is Mount Monarch Chalet or Mount Luxe Chalet for one night?",
        "How much are Mount Monarch Chalet and Mount Luxe Chalet for one night?",
        "How much is the Mount Monarch Chalet or Mount Luxe Chalet?",
        "How much are the Mount Monarch Chalet and Mount Luxe Chalet?",
        "How much is the Mount Monarch Chalet or Mount Luxe Chalet per night?",
        "How much are the Mount Monarch Chalet and Mount Luxe Chalet per night?",
        "How much is the Mount Monarch Chalet or Mount Luxe Chalet per room per night?",
        "How much are the Mount Monarch Chalet and Mount Luxe Chalet per room per night?",
        "How much is the Mount Monarch Chalet or Mount Luxe Chalet for one night?",
        "How much are the Mount Monarch Chalet and Mount Luxe Chalet for one night?",
    ),
)
def test_room_rate_classifier_resolves_ambiguous_amount_grammar_before_target_count(
    utterance,
):
    classification = _rate_intent(utterance)

    assert classification.kind == "AMBIGUOUS_RATE"
    assert classification.rooms == ("Mount Monarch Chalet", "Mount Luxe Chalet")


@pytest.mark.parametrize(
    "utterance",
    (
        "How much is Royal Villa?",
        "How much is Royal Villa per night?",
        "How much is Royal Villa per room per night?",
        "How much is Royal Villa for one night?",
    ),
)
def test_room_rate_classifier_marks_unknown_room_noun_amount_forms_unresolved(utterance):
    classification = _rate_intent(utterance)

    assert classification.kind == "RATE"
    assert classification.rooms == ()
    assert classification.unresolved


@pytest.mark.parametrize(
    "utterance",
    (
        "What are the spa rates?",
        "How much does dinner cost?",
        "What is the activity price in USD?",
        "What is the spa cost in LKR per night?",
        "Does the room cost include dinner?",
        "How much is dinner?",
    ),
)
def test_room_rate_classifier_rejects_non_room_price_subjects(utterance):
    classification = _rate_intent(utterance)

    assert classification.kind == "NONE"
    assert classification.rooms == ()


@pytest.mark.parametrize(
    ("utterance", "expected"),
    (
        ("I would like Mount Monarch Chalet.", "Mount Monarch Chalet"),
        ("Is Mount Monarch Chalet available?", "Mount Monarch Chalet"),
        ("I would like to know what dinner costs at Mount Monarch Chalet.", None),
        ("I would like Mount Monarch Chalet dinner.", None),
        ("Is dinner available at Mount Monarch Chalet?", None),
        ("How much is Mount Monarch Chalet's dinner?", None),
    ),
)
def test_room_selection_is_direct_and_separate_from_availability_and_price_subjects(
    utterance, expected
):
    assert rate_catalog.recognize_selected_room(utterance) == expected


@pytest.mark.parametrize(
    ("utterance", "expected"),
    (
        ("We're local", "resident"),
        ("I am an international guest", "foreign"),
        ("Were local residents welcome to the spa?", None),
    ),
)
def test_residency_recognition_handles_contractions_without_were_false_positives(
    utterance, expected
):
    assert rate_catalog.recognize_residency(utterance) == expected


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


def _current_turn_request_text(provider: str, client) -> str:
    """Return only the system/current-user request scope, not prior turn history."""
    request = client.requests[-1]
    if provider == "openai":
        messages = request["messages"]
        current_user = next(
            message for message in reversed(messages) if message["role"] == "user"
        )
        return str(messages[0]["content"]) + str(current_user["content"])
    if provider == "gemini":
        current_user = request["contents"][-1]
        return str(request["config"]["system_instruction"]) + str(current_user)
    current_user = request["messages"][-1]
    return str(request["system"]) + str(current_user["content"])


def _current_authoritative_rate_record(provider: str, client) -> str:
    """Return the current turn's record, excluding legitimate prior slot history."""
    _before, marker, record = _current_turn_request_text(provider, client).partition(
        "AUTHORITATIVE RATE RECORD"
    )
    return marker + record


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
    request_text = _current_turn_request_text(provider, client)
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


@pytest.mark.asyncio
async def test_costume_remains_a_descriptive_kb_turn_after_rate_state_exists(monkeypatch):
    session, client = _provider_session("openai")
    session._booking_slots.update({
        "room_type": "Mount Monarch Chalet",
        "residency": "resident",
        "check_in": "2026-09-26",
        "check_out": "2026-09-29",
    })
    monkeypatch.setattr(server, "retrieve_context", lambda _text: "COSTUME_DETAILS: forest theme.")

    await session._process_utterance_bound("Please describe the costume theme.")

    request_text = _request_text("openai", client)
    assert "COSTUME_DETAILS" in request_text
    assert "AUTHORITATIVE RATE RECORD" not in request_text


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ("openai", "gemini", "claude"))
@pytest.mark.parametrize(
    ("utterance", "kb_marker"),
    (
        (
            "Describe the costume theme in Mount Monarch Chalet.",
            "COSTUME_DETAILS: forest theme.",
        ),
        (
            "What does dinner cost at Mount Monarch Chalet?",
            "DINNER_DETAILS: dinner is served in the restaurant.",
        ),
    ),
)
async def test_room_scoped_non_price_cost_language_keeps_descriptive_kb_available(
    monkeypatch, provider, utterance, kb_marker
):
    session, client = _provider_session(provider)
    session._booking_slots.update({
        "room_type": "Mount Monarch Chalet",
        "residency": "resident",
        "check_in": "2026-09-26",
        "check_out": "2026-09-29",
    })
    monkeypatch.setattr(server, "retrieve_context", lambda _text: kb_marker)

    await session._process_utterance_bound(utterance)

    request_text = _request_text(provider, client)
    assert kb_marker in request_text
    assert "AUTHORITATIVE RATE RECORD" not in request_text


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ("openai", "gemini", "claude"))
async def test_indirect_selection_phrase_with_dinner_cost_keeps_descriptive_kb(
    monkeypatch, provider
):
    session, client = _provider_session(provider)
    session._booking_slots.update({
        "room_type": "Mount Monarch Chalet",
        "residency": "resident",
        "check_in": "2026-09-26",
        "check_out": "2026-09-29",
    })
    monkeypatch.setattr(server, "retrieve_context", lambda _text: "DINNER_DETAILS")

    await session._process_utterance_bound(
        "I would like to know what dinner costs at Mount Monarch Chalet."
    )

    request_text = _request_text(provider, client)
    assert "DINNER_DETAILS" in request_text
    assert "AUTHORITATIVE RATE RECORD" not in request_text


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ("openai", "gemini", "claude"))
async def test_direct_selection_captures_room_without_inventing_a_rate(
    monkeypatch, provider
):
    session, client = _provider_session(provider)
    monkeypatch.setattr(server, "retrieve_context", lambda _text: "UNUSED_KB")

    await session._process_utterance_bound("I would like Mount Monarch Chalet.")

    request_text = _request_text(provider, client)
    assert session._booking_slots["room_type"] == "Mount Monarch Chalet"
    assert "rate_per_room_per_night" not in request_text


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ("openai", "gemini", "claude"))
async def test_direct_selection_then_explicit_room_rate_sends_authoritative_rate(
    monkeypatch, provider
):
    session, client = _provider_session(provider)
    session._booking_slots.update({
        "residency": "resident",
        "check_in": "2026-09-26",
        "check_out": "2026-09-29",
    })
    monkeypatch.setattr(server, "retrieve_context", lambda _text: "UNUSED_KB")

    await session._process_utterance_bound("I would like Mount Monarch Chalet.")
    await session._process_utterance_bound("What is the room rate?")

    request_text = _current_turn_request_text(provider, client)
    assert "AUTHORITATIVE RATE RECORD" in request_text
    assert "rate_per_room_per_night: 315000" in request_text
    assert "UNUSED_KB" not in request_text


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ("openai", "gemini", "claude"))
async def test_explicit_room_rate_wording_still_sends_the_authoritative_rate_record(
    monkeypatch, provider
):
    session, client = _provider_session(provider)
    session._booking_slots.update({
        "residency": "resident",
        "check_in": "2026-09-26",
        "check_out": "2026-09-29",
    })
    monkeypatch.setattr(server, "retrieve_context", lambda _text: "UNUSED_KB")

    await session._process_utterance_bound(
        "What is the room rate for Mount Monarch Chalet?"
    )

    request_text = _request_text(provider, client)
    assert "AUTHORITATIVE RATE RECORD" in request_text
    assert "rate_per_room_per_night: 315000" in request_text
    assert "UNUSED_KB" not in request_text


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ("openai", "gemini", "claude"))
async def test_explicit_canonical_room_amount_question_sends_authoritative_rate(
    monkeypatch, provider
):
    session, client = _provider_session(provider)
    session._booking_slots.update({
        "residency": "resident",
        "check_in": "2026-09-26",
        "check_out": "2026-09-29",
    })
    monkeypatch.setattr(server, "retrieve_context", lambda _text: "UNUSED_KB")

    await session._process_utterance_bound("How much is Mount Monarch Chalet?")

    request_text = _request_text(provider, client)
    assert "AUTHORITATIVE RATE RECORD" in request_text
    assert "rate_per_room_per_night: 315000" in request_text
    assert "UNUSED_KB" not in request_text


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ("openai", "gemini", "claude"))
@pytest.mark.parametrize(
    "utterance",
    (
        "How much is Mount Monarch Chalet?",
        "How much is Mount Monarch Chalet per night?",
    ),
)
async def test_canonical_room_amount_controls_remain_authoritative(
    monkeypatch, provider, utterance
):
    session, client = _provider_session(provider)
    session._booking_slots.update({
        "residency": "resident",
        "check_in": "2026-09-26",
        "check_out": "2026-09-29",
    })
    monkeypatch.setattr(server, "retrieve_context", lambda _text: "UNUSED_KB")

    await session._process_utterance_bound(utterance)

    request_text = _request_text(provider, client)
    assert "AUTHORITATIVE RATE RECORD" in request_text
    assert "rate_per_room_per_night: 315000" in request_text
    assert "UNUSED_KB" not in request_text


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ("openai", "gemini", "claude"))
async def test_possessive_room_name_dinner_amount_question_remains_descriptive(
    monkeypatch, provider
):
    session, client = _provider_session(provider)
    session._booking_slots.update({
        "room_type": "Mount Monarch Chalet",
        "residency": "resident",
        "check_in": "2026-09-26",
        "check_out": "2026-09-29",
    })
    monkeypatch.setattr(server, "retrieve_context", lambda _text: "DINNER_DETAILS")

    await session._process_utterance_bound(
        "How much is Mount Monarch Chalet's dinner?"
    )

    request_text = _request_text(provider, client)
    assert "DINNER_DETAILS" in request_text
    assert "AUTHORITATIVE RATE RECORD" not in request_text


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ("openai", "gemini", "claude"))
async def test_explicit_rate_turn_enables_grounded_amount_pronoun_follow_up(
    monkeypatch, provider
):
    session, client = _provider_session(provider)
    session._booking_slots.update({
        "room_type": "Mount Monarch Chalet",
        "residency": "resident",
        "check_in": "2026-09-26",
        "check_out": "2026-09-29",
    })
    monkeypatch.setattr(server, "retrieve_context", lambda _text: "UNUSED_KB")

    await session._process_utterance_bound("What is the room rate?")
    await session._process_utterance_bound("How much is it?")

    request_text = _request_text(provider, client)
    assert "AUTHORITATIVE RATE RECORD" in request_text
    assert "rate_per_room_per_night: 315000" in request_text
    assert "UNUSED_KB" not in request_text


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ("openai", "gemini", "claude"))
async def test_ungrounded_amount_pronoun_remains_a_descriptive_kb_turn(
    monkeypatch, provider
):
    session, client = _provider_session(provider)
    monkeypatch.setattr(server, "retrieve_context", lambda _text: "GENERAL_DETAILS")

    await session._process_utterance_bound("How much is it?")

    request_text = _request_text(provider, client)
    assert "GENERAL_DETAILS" in request_text
    assert "AUTHORITATIVE RATE RECORD" not in request_text


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ("openai", "gemini", "claude"))
@pytest.mark.parametrize(
    "utterance",
    (
        "What is the room rate for Mount Monarch Chalet or Mount Luxe Chalet?",
        "How much is Mount Monarch Chalet or Mount Luxe Chalet?",
        "How much are Mount Monarch Chalet and Mount Luxe Chalet?",
        "How much is Mount Monarch Chalet or Mount Luxe Chalet per night?",
        "How much are Mount Monarch Chalet and Mount Luxe Chalet per night?",
        "How much is Mount Monarch Chalet or Mount Luxe Chalet per room per night?",
        "How much are Mount Monarch Chalet and Mount Luxe Chalet per room per night?",
        "How much is Mount Monarch Chalet or Mount Luxe Chalet for one night?",
        "How much are Mount Monarch Chalet and Mount Luxe Chalet for one night?",
        "How much is the Mount Monarch Chalet or Mount Luxe Chalet?",
        "How much are the Mount Monarch Chalet and Mount Luxe Chalet?",
        "How much is the Mount Monarch Chalet or Mount Luxe Chalet per night?",
        "How much are the Mount Monarch Chalet and Mount Luxe Chalet per night?",
        "How much is the Mount Monarch Chalet or Mount Luxe Chalet per room per night?",
        "How much are the Mount Monarch Chalet and Mount Luxe Chalet per room per night?",
        "How much is the Mount Monarch Chalet or Mount Luxe Chalet for one night?",
        "How much are the Mount Monarch Chalet and Mount Luxe Chalet for one night?",
    ),
)
async def test_ambiguous_multi_room_rate_requests_are_no_quote_without_stale_room(
    monkeypatch, provider, utterance
):
    session, client = _provider_session(provider)
    session._booking_slots.update({
        "room_type": "Mount Monarch Chalet",
        "residency": "resident",
        "check_in": "2026-09-26",
        "check_out": "2026-09-29",
    })
    monkeypatch.setattr(server, "retrieve_context", lambda _text: "AMBIGUOUS_KB")

    await session._process_utterance_bound(utterance)

    request_text = _request_text(provider, client)
    assert "AUTHORITATIVE RATE RECORD" in request_text
    assert "status: no_quote" in request_text
    assert "reason: ambiguous_room" in request_text
    assert "rate_per_room_per_night" not in request_text
    assert "AMBIGUOUS_KB" not in request_text


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ("openai", "gemini", "claude"))
@pytest.mark.parametrize(
    "utterance",
    (
        "What are the spa rates?",
        "How much does dinner cost?",
        "What is the activity price in USD?",
        "What is the spa cost in LKR per night?",
        "Does the room cost include dinner?",
        "How much is dinner?",
    ),
)
async def test_non_room_price_subjects_keep_semantic_kb(
    monkeypatch, provider, utterance
):
    session, client = _provider_session(provider)
    session._booking_slots.update({
        "room_type": "Mount Monarch Chalet",
        "residency": "resident",
        "check_in": "2026-09-26",
        "check_out": "2026-09-29",
    })
    monkeypatch.setattr(server, "retrieve_context", lambda _text: "SEMANTIC_KB")

    await session._process_utterance_bound(utterance)

    request_text = _request_text(provider, client)
    assert "SEMANTIC_KB" in request_text
    assert "AUTHORITATIVE RATE RECORD" not in request_text


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ("openai", "gemini", "claude"))
@pytest.mark.parametrize(
    ("utterance", "expected_room"),
    (
        ("I would like Mount Monarch Chalet.", "Mount Monarch Chalet"),
        ("Is Mount Monarch Chalet available?", "Mount Monarch Chalet"),
        ("I would like to know what dinner costs at Mount Monarch Chalet.", None),
        ("Is dinner available at Mount Monarch Chalet?", None),
    ),
)
async def test_selection_and_availability_do_not_activate_room_pricing(
    monkeypatch, provider, utterance, expected_room
):
    session, client = _provider_session(provider)
    session._booking_slots.update({
        "residency": "resident",
        "check_in": "2026-09-26",
        "check_out": "2026-09-29",
    })
    monkeypatch.setattr(server, "retrieve_context", lambda _text: "SELECTION_KB")

    await session._process_utterance_bound(utterance)

    request_text = _request_text(provider, client)
    assert session._booking_slots.get("room_type") == expected_room
    assert "SELECTION_KB" in request_text
    assert "AUTHORITATIVE RATE RECORD" not in request_text


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ("openai", "gemini", "claude"))
@pytest.mark.parametrize(
    "utterance",
    (
        "How much does Mount Monarch Chalet cost?",
        "What is the price of Mount Monarch Chalet?",
        "What is the price of the Mount Monarch Chalet?",
        "How much is Mount Monarch Chalet for one night?",
    ),
)
async def test_natural_explicit_room_price_forms_send_authoritative_rate(
    monkeypatch, provider, utterance
):
    session, client = _provider_session(provider)
    session._booking_slots.update({
        "residency": "resident",
        "check_in": "2026-09-26",
        "check_out": "2026-09-29",
    })
    monkeypatch.setattr(server, "retrieve_context", lambda _text: "UNUSED_KB")

    await session._process_utterance_bound(utterance)

    request_text = _request_text(provider, client)
    assert "AUTHORITATIVE RATE RECORD" in request_text
    assert "rate_per_room_per_night: 315000" in request_text
    assert "UNUSED_KB" not in request_text


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ("openai", "gemini", "claude"))
async def test_unknown_room_explicit_rate_request_is_authoritative_no_quote(
    monkeypatch, provider
):
    session, client = _provider_session(provider)
    monkeypatch.setattr(server, "retrieve_context", lambda _text: "UNKNOWN_ROOM_KB")

    await session._process_utterance_bound(
        "What is the room rate for Royal Villa?"
    )

    request_text = _request_text(provider, client)
    assert "AUTHORITATIVE RATE RECORD" in request_text
    assert "status: no_quote" in request_text
    assert "reason: unknown_room" in request_text
    assert "UNKNOWN_ROOM_KB" not in request_text


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ("openai", "gemini", "claude"))
@pytest.mark.parametrize(
    "utterance",
    (
        "What is the price of Royal Villa?",
        "How much does Royal Villa cost?",
        "How much is Royal Villa?",
        "How much is Royal Villa per night?",
        "How much is Royal Villa per room per night?",
        "How much is Royal Villa for one night?",
    ),
)
async def test_unknown_room_price_grammar_cannot_reuse_a_persisted_room(
    monkeypatch, provider, utterance
):
    session, client = _provider_session(provider)
    session._booking_slots.update({
        "room_type": "Mount Monarch Chalet",
        "residency": "resident",
        "check_in": "2026-09-26",
        "check_out": "2026-09-29",
    })
    monkeypatch.setattr(server, "retrieve_context", lambda _text: "UNKNOWN_ROOM_KB")

    await session._process_utterance_bound(utterance)

    current_turn = _current_turn_request_text(provider, client)
    rate_record = _current_authoritative_rate_record(provider, client)
    assert "AUTHORITATIVE RATE RECORD" in rate_record
    assert "status: no_quote" in rate_record
    assert "reason: unknown_room" in rate_record
    assert "Mount Monarch Chalet" not in rate_record
    assert "rate_per_room_per_night" not in rate_record
    assert "UNKNOWN_ROOM_KB" not in current_turn


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ("openai", "gemini", "claude"))
@pytest.mark.parametrize(
    "utterance",
    (
        "What does a costume cost?",
        "What are the breakfast rates?",
        "What is the airport-transfer price?",
    ),
)
async def test_non_room_price_grammar_stays_semantic_without_a_denylist(
    monkeypatch, provider, utterance
):
    session, client = _provider_session(provider)
    session._booking_slots.update({
        "room_type": "Mount Monarch Chalet",
        "residency": "resident",
        "check_in": "2026-09-26",
        "check_out": "2026-09-29",
    })
    monkeypatch.setattr(server, "retrieve_context", lambda _text: "SUBJECT_KB")

    await session._process_utterance_bound(utterance)

    request_text = _request_text(provider, client)
    assert "SUBJECT_KB" in request_text
    assert "AUTHORITATIVE RATE RECORD" not in request_text


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ("openai", "gemini", "claude"))
async def test_explicit_rate_follow_up_expires_after_a_non_rate_turn(
    monkeypatch, provider
):
    session, client = _provider_session(provider)
    session._booking_slots.update({
        "room_type": "Mount Monarch Chalet",
        "residency": "resident",
        "check_in": "2026-09-26",
        "check_out": "2026-09-29",
    })

    def retrieve(text: str) -> str:
        return "FOLLOWUP_KB" if text == "How much is it?" else "SPA_KB"

    monkeypatch.setattr(server, "retrieve_context", retrieve)
    await session._process_utterance_bound("What is the room rate?")
    await session._process_utterance_bound("Please tell me about the spa.")
    await session._process_utterance_bound("How much is it?")

    request_text = _request_text(provider, client)
    assert "FOLLOWUP_KB" in request_text
    assert "AUTHORITATIVE RATE RECORD" not in request_text


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ("openai", "gemini", "claude"))
async def test_incomplete_rate_state_does_not_ground_an_amount_pronoun(
    monkeypatch, provider
):
    session, client = _provider_session(provider)
    session._booking_slots.update({
        "room_type": "Mount Monarch Chalet",
        "residency": "resident",
    })
    monkeypatch.setattr(server, "retrieve_context", lambda _text: "INCOMPLETE_KB")

    await session._process_utterance_bound("How much is it?")

    request_text = _request_text(provider, client)
    assert "INCOMPLETE_KB" in request_text
    assert "AUTHORITATIVE RATE RECORD" not in request_text


@pytest.mark.asyncio
async def test_stale_smartpbx_runner_cannot_inherit_a_newer_turn_rate_context(monkeypatch):
    session, client = _provider_session("openai")
    session._smartpbx_transfer_context = object()
    session._media_transport = SimpleNamespace(frames_dropped_total=0)
    session._booking_slots.update({
        "room_type": "Mount Monarch Chalet",
        "check_in": "2026-09-26",
        "check_out": "2026-09-29",
    })
    entered_kb = threading.Event()
    release_kb = threading.Event()
    compositions: list[tuple[str, str, str]] = []
    original_compose = session._compose_turn_user_message

    def retrieve(text: str) -> str:
        if text == "Please describe the pool.":
            entered_kb.set()
            assert release_kb.wait(timeout=5), "test must release the blocked KB call"
            return "STALE_POOL_DETAILS"
        return "UNEXPECTED_KB"

    def record_compose(text: str, kb_context: str) -> str:
        message = original_compose(text, kb_context)
        compositions.append((text, kb_context, message))
        return message

    monkeypatch.setattr(server, "retrieve_context", retrieve)
    monkeypatch.setattr(session, "_compose_turn_user_message", record_compose)

    session._active_smartpbx_turn_id = "turn-a"
    stale_task = asyncio.create_task(
        session._process_utterance_bound("Please describe the pool.")
    )
    assert await asyncio.to_thread(entered_kb.wait, 1)

    session._active_smartpbx_turn_id = "turn-b"
    session._speak_generation += 1
    await session._process_utterance_bound("Sri Lankan resident")

    release_kb.set()
    await stale_task

    assert len(client.requests) == 1
    request_text = _request_text("openai", client)
    assert "rate_per_room_per_night: 315000" in request_text
    assert "STALE_POOL_DETAILS" not in request_text
    assert not any(text == "Please describe the pool." for text, _kb, _message in compositions)
    assert not any(
        message.get("content") == "Please describe the pool."
        for message in session.history
    )


async def _assert_stale_runner_cannot_commit_rate_state(
    monkeypatch, utterance: str, slot: str,
):
    session, _client = _provider_session("openai")
    session._smartpbx_transfer_context = object()
    session._media_transport = SimpleNamespace(frames_dropped_total=0)
    entered_kb = threading.Event()
    release_kb = threading.Event()

    def retrieve(text: str) -> str:
        if text == utterance:
            entered_kb.set()
            assert release_kb.wait(timeout=5), "test must release the blocked KB call"
        return "KB_DETAILS"

    monkeypatch.setattr(server, "retrieve_context", retrieve)
    session._active_smartpbx_turn_id = "turn-a"
    stale_task = asyncio.create_task(
        session._process_utterance_bound(utterance)
    )
    try:
        assert await asyncio.to_thread(entered_kb.wait, 1)
        session._active_smartpbx_turn_id = "turn-b"
        session._speak_generation += 1
        await session._process_utterance_bound("Please tell me about the pool.")
    finally:
        release_kb.set()
        await stale_task

    assert slot not in session._booking_slots


@pytest.mark.asyncio
async def test_stale_smartpbx_runner_cannot_commit_a_recognized_direct_selection(
    monkeypatch,
):
    assert rate_catalog.recognize_selected_room(
        "I would like Mount Monarch Chalet."
    ) == "Mount Monarch Chalet"
    await _assert_stale_runner_cannot_commit_rate_state(
        monkeypatch, "I would like Mount Monarch Chalet.", "room_type",
    )


@pytest.mark.asyncio
async def test_stale_smartpbx_runner_cannot_commit_explicit_residency(
    monkeypatch,
):
    assert rate_catalog.recognize_residency("I am local.") == "resident"
    await _assert_stale_runner_cannot_commit_rate_state(
        monkeypatch, "I am local.", "residency",
    )
