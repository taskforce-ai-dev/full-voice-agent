"""Deterministic rate resolution must precede semantic knowledge retrieval.

These tests define the rate-catalog boundary used by the shared MediaStreamSession
path.  They intentionally exercise the session's real slot and provider seams;
no model response is mocked to manufacture a price.

Riviera Resort publishes ONE rate sheet: Sri Lankan resident rates in LKR, per
room per night, room only, inclusive of all taxes, in two DATE-RANGE seasons
(mid 2026-09-01..2027-06-30, high 2027-07-01..2027-08-31).  There is no
foreign-guest sheet, so a foreign guest is always a no-quote that routes to
reservations.
"""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

import pytest

import rate_catalog
import server
import tools
import yanolja_service


MID_STAY = ("2026-09-26", "2026-09-29")
HIGH_STAY = ("2027-07-10", "2027-07-12")


def resolve(room: str, residency: str, check_in: str, check_out: str):
    return rate_catalog.resolve_rate(
        room=room,
        residency=residency,
        check_in=check_in,
        check_out=check_out,
    )


@pytest.mark.parametrize(
    ("room", "mid", "high"),
    (
        ("Basic Room Single", 8800, 12800),
        ("Wooden Cabana", 11000, 16000),
        ("Lagoon View Steel Cabana", 14600, 21300),
        ("Double Lagoon or Garden View", 17500, 25600),
        ("Triple Garden View", 20400, 29800),
        ("Family Cottage", 24800, 36200),
        ("Family Chalet", 32100, 46900),
    ),
)
def test_resident_catalog_covers_every_room_in_each_season(room, mid, high):
    for (check_in, check_out), expected, season in (
        (MID_STAY, mid, "mid"),
        (HIGH_STAY, high, "high"),
    ):
        result = resolve(room, "resident", check_in, check_out)

        assert result.is_quotable
        assert result.currency == "LKR"
        assert result.nightly_rate == expected
        assert result.season == season
        assert result.room == room


def test_resident_catalog_agrees_with_the_service_rate_card():
    """The catalog must never carry a figure the service module does not."""
    assert set(yanolja_service.NIGHTLY_RATE_LKR) == set(yanolja_service.ALL_ROOM_TYPES)
    for room, seasons in yanolja_service.NIGHTLY_RATE_LKR.items():
        assert resolve(room, "resident", *MID_STAY).nightly_rate == seasons["mid"]
        assert resolve(room, "resident", *HIGH_STAY).nightly_rate == seasons["high"]


def test_foreign_guests_are_never_quoted_a_rate():
    """No foreign-guest sheet exists: every room is a no-quote to reservations."""
    for room in yanolja_service.ALL_ROOM_TYPES:
        result = resolve(room, "foreign", *MID_STAY)

        assert not result.is_quotable
        assert result.nightly_rate is None
        assert result.currency is None
        assert result.residency == "foreign"
        assert result.reason == "foreign_rates_unpublished"
        record = result.authoritative_context()
        assert "status: no_quote" in record
        assert "reason: foreign_rates_unpublished" in record
        assert "reservations" in record.lower()
        assert "rate_per_room_per_night" not in record


def test_incident_family_cottage_resident_rate_is_the_mid_season_lkr_rate():
    result = resolve("Family Cottage", "resident", *MID_STAY)

    assert result.is_quotable
    assert result.currency == "LKR"
    assert result.nightly_rate == 24800
    assert result.season == "mid"


def test_family_cottage_july_resident_rate_is_high_season_lkr_rate():
    result = resolve("Family Cottage", "resident", *HIGH_STAY)

    assert result.is_quotable
    assert result.currency == "LKR"
    assert result.nightly_rate == 36200
    assert result.season == "high"


def test_december_is_mid_season_not_a_peak():
    """Riviera's seasons are date ranges: December 2026 sits inside mid season."""
    result = resolve("Wooden Cabana", "resident", "2026-12-20", "2026-12-22")

    assert result.is_quotable
    assert result.currency == "LKR"
    assert result.nightly_rate == 11000
    assert result.season == "mid"


def test_checkout_on_the_first_high_season_day_is_still_a_mid_season_stay():
    result = resolve("Family Cottage", "resident", "2027-06-30", "2027-07-01")

    assert result.is_quotable
    assert result.currency == "LKR"
    assert result.nightly_rate == 24800
    assert result.season == "mid"


def test_stay_crossing_seasons_fails_closed_instead_of_quoting_one_rate():
    result = resolve("Family Cottage", "resident", "2027-06-29", "2027-07-02")

    assert not result.is_quotable
    assert result.reason == "mixed_period"
    assert result.nightly_rate is None
    assert "reservations" in result.authoritative_context().lower()


@pytest.mark.parametrize(
    ("check_in", "check_out"),
    (
        ("2026-08-20", "2026-08-22"),   # entirely before the first published season
        ("2026-08-31", "2026-09-01"),   # one night, the day before mid season opens
        ("2027-08-31", "2027-09-02"),   # runs past the end of high season
        ("2027-09-10", "2027-09-12"),   # entirely after the last published season
    ),
)
def test_dates_outside_every_published_season_are_unsupported(check_in, check_out):
    result = resolve("Family Cottage", "resident", check_in, check_out)

    assert not result.is_quotable
    assert result.reason == "unsupported_period"
    assert result.nightly_rate is None
    assert "reservations" in result.authoritative_context().lower()


@pytest.mark.parametrize(
    ("room", "residency", "check_in", "check_out"),
    (
        ("", "resident", "2026-09-26", "2026-09-29"),
        ("Unknown room", "resident", "2026-09-26", "2026-09-29"),
        ("Family Cottage", "", "2026-09-26", "2026-09-29"),
        ("Family Cottage", "unknown", "2026-09-26", "2026-09-29"),
        ("Family Cottage", "resident", "", "2026-09-29"),
        ("Family Cottage", "resident", "2026-09-26", ""),
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
        "Is Family Cottage available from September 26 to 29?"
    ) == "Family Cottage"
    assert rate_catalog.recognize_selected_room(
        "Is Family Cottage or Family Chalet available?"
    ) is None


@pytest.mark.parametrize(
    "utterance",
    (
        "Is Family Cottages available?",
        "Is Family CottageSuite available?",
    ),
)
def test_room_matching_rejects_suffix_and_prefix_collisions(utterance):
    assert rate_catalog.recognize_selected_room(utterance) is None


def test_bare_cabana_is_ambiguous_between_the_two_cabanas():
    """"cabana" is deliberately aliased to BOTH cabanas so Riya asks which one."""
    assert rate_catalog.recognize_selected_room("the cabana") is None
    assert rate_catalog.recognize_selected_room("Is the cabana available?") is None
    classification = rate_catalog.classify_room_rate_intent("how much is the cabana")
    assert classification.kind in {"NONE", "AMBIGUOUS_RATE"}
    if classification.kind == "AMBIGUOUS_RATE":
        assert set(classification.rooms) == {"Wooden Cabana", "Lagoon View Steel Cabana"}


def test_steel_cabana_beats_the_shared_cabana_alias():
    """The longer mention wins where two rooms overlap on the same word."""
    assert rate_catalog.recognize_selected_room("the steel cabana") == "Lagoon View Steel Cabana"
    assert rate_catalog.recognize_selected_room("the wooden cabana") == "Wooden Cabana"


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


def test_foreign_rate_is_a_no_quote_with_or_without_the_rate_kill_switch(monkeypatch):
    live = resolve("Family Cottage", "foreign", *MID_STAY)
    assert not live.is_quotable
    assert live.nightly_rate is None
    assert live.reason == "foreign_rates_unpublished"

    monkeypatch.setattr("yanolja_service.DEMO_RATES_ENABLED", False)

    dark = resolve("Family Cottage", "foreign", *MID_STAY)

    assert not dark.is_quotable
    assert dark.nightly_rate is None
    assert dark.reason in {"rates_disabled", "foreign_rates_unpublished"}
    assert "status: no_quote" in dark.authoritative_context()


def test_resident_rate_respects_the_existing_demo_rate_kill_switch(monkeypatch):
    monkeypatch.setattr("yanolja_service.DEMO_RATES_ENABLED", False)

    result = resolve("Family Cottage", "resident", *MID_STAY)

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
            "What is the room rate for Family Cottage or Family Chalet?",
            "AMBIGUOUS_RATE",
            ("Family Cottage", "Family Chalet"),
        ),
        (
            "How much are Family Cottage and Family Chalet?",
            "AMBIGUOUS_RATE",
            ("Family Cottage", "Family Chalet"),
        ),
        ("How much does Family Cottage cost?", "RATE", ("Family Cottage",)),
        ("What is the price of Family Cottage?", "RATE", ("Family Cottage",)),
        ("What is the price of the Family Cottage?", "RATE", ("Family Cottage",)),
        ("How much is Family Cottage for one night?", "RATE", ("Family Cottage",)),
        # Guests rarely say the full catalogue name; the safe aliases must
        # reach the same structured result.
        ("How much is the steel cabana?", "RATE", ("Lagoon View Steel Cabana",)),
        ("What is the price of the cottage?", "RATE", ("Family Cottage",)),
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
        "How much is Family Cottage or Family Chalet?",
        "How much are Family Cottage and Family Chalet?",
        "How much is Family Cottage or Family Chalet per night?",
        "How much are Family Cottage and Family Chalet per night?",
        "How much is Family Cottage or Family Chalet per room per night?",
        "How much are Family Cottage and Family Chalet per room per night?",
        "How much is Family Cottage or Family Chalet for one night?",
        "How much are Family Cottage and Family Chalet for one night?",
        "How much is the Family Cottage or Family Chalet?",
        "How much are the Family Cottage and Family Chalet?",
        "How much is the Family Cottage or Family Chalet per night?",
        "How much are the Family Cottage and Family Chalet per night?",
        "How much is the Family Cottage or Family Chalet per room per night?",
        "How much are the Family Cottage and Family Chalet per room per night?",
        "How much is the Family Cottage or Family Chalet for one night?",
        "How much are the Family Cottage and Family Chalet for one night?",
    ),
)
def test_room_rate_classifier_resolves_ambiguous_amount_grammar_before_target_count(
    utterance,
):
    classification = _rate_intent(utterance)

    assert classification.kind == "AMBIGUOUS_RATE"
    assert classification.rooms == ("Family Cottage", "Family Chalet")


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
        ("I would like Family Cottage.", "Family Cottage"),
        ("Is Family Cottage available?", "Family Cottage"),
        ("I would like to know what dinner costs at Family Cottage.", None),
        ("I would like Family Cottage dinner.", None),
        ("Is dinner available at Family Cottage?", None),
        ("How much is Family Cottage's dinner?", None),
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


# The system prompt now mentions "the AUTHORITATIVE RATE RECORD" as an
# instruction, so the bare phrase is no longer proof that a record was injected
# for this turn. Anchor on the two real record headers rate_catalog renders.
_RATE_RECORD_HEADERS = (
    "AUTHORITATIVE RATE RECORD (",   # quotable record
    "AUTHORITATIVE RATE RECORD:",    # no_quote record
)


def _has_rate_record(text: str) -> bool:
    return any(header in text for header in _RATE_RECORD_HEADERS)


def _current_authoritative_rate_record(provider: str, client) -> str:
    """Return the current turn's record, excluding legitimate prior slot history."""
    text = _current_turn_request_text(provider, client)
    starts = [text.find(header) for header in _RATE_RECORD_HEADERS]
    starts = [start for start in starts if start >= 0]
    if not starts:
        return ""
    return text[min(starts):]


def _availability_input() -> dict[str, str]:
    return {"check_in": MID_STAY[0], "check_out": MID_STAY[1]}


def _mid_stay_slots(**slots) -> dict[str, str]:
    return {"check_in": MID_STAY[0], "check_out": MID_STAY[1], **slots}


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
        lambda _text: "CONTRADICTORY_RATE: 1400 USD and 999000 LKR.",
    )

    await session._process_utterance_bound(
        "Is Family Cottage available from September 26 to 29?"
    )
    await session._process_utterance_bound("I am a Sri Lankan resident.")

    assert session._booking_slots["room_type"] == "Family Cottage"
    request_text = _current_turn_request_text(provider, client)
    assert _has_rate_record(request_text)
    assert "rate_per_room_per_night: 24800" in request_text
    assert "currency: LKR" in request_text
    assert "season: mid season" in request_text
    assert "basis: room only, inclusive of all taxes" in request_text
    assert "CONTRADICTORY_RATE" not in request_text
    assert "1400 USD" not in request_text
    assert "999000 LKR" not in request_text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "slots",
    (
        _mid_stay_slots(residency="resident"),
        _mid_stay_slots(room_type="Unknown room", residency="resident"),
        _mid_stay_slots(room_type="Family Cottage"),
        _mid_stay_slots(room_type="Family Cottage", residency="unknown"),
        {"room_type": "Family Cottage", "residency": "resident", "check_in": MID_STAY[0]},
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
    assert _has_rate_record(request_text)
    assert "status: no_quote" in request_text
    assert "CONTRADICTORY_RATE" not in request_text


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ("openai", "gemini", "claude"))
async def test_foreign_session_rate_state_is_a_no_quote_that_routes_to_reservations(
    monkeypatch, provider
):
    session, client = _provider_session(provider)
    session._booking_slots.update(_mid_stay_slots(
        room_type="Family Cottage", residency="foreign",
    ))
    monkeypatch.setattr(
        server, "retrieve_context", lambda _text: "CONTRADICTORY_RATE: 1400 USD."
    )

    await session._process_utterance_bound("What is the room rate?")

    record = _current_authoritative_rate_record(provider, client)
    assert "status: no_quote" in record
    assert "reason: foreign_rates_unpublished" in record
    assert "reservations" in record.lower()
    assert "rate_per_room_per_night" not in record
    assert "USD" not in record
    assert "CONTRADICTORY_RATE" not in _current_turn_request_text(provider, client)


@pytest.mark.asyncio
async def test_completed_rate_state_still_uses_descriptive_kb_for_an_unrelated_turn(monkeypatch):
    session, client = _provider_session("openai")
    session._booking_slots.update(_mid_stay_slots(
        room_type="Family Cottage", residency="resident",
    ))
    monkeypatch.setattr(server, "retrieve_context", lambda _text: "POOL_DETAILS: lagoon-front pool.")

    await session._process_utterance_bound("Please tell me about the pool.")

    request_text = _request_text("openai", client)
    assert "POOL_DETAILS" in request_text
    assert not _has_rate_record(request_text)


@pytest.mark.asyncio
async def test_costume_remains_a_descriptive_kb_turn_after_rate_state_exists(monkeypatch):
    session, client = _provider_session("openai")
    session._booking_slots.update(_mid_stay_slots(
        room_type="Family Cottage", residency="resident",
    ))
    monkeypatch.setattr(server, "retrieve_context", lambda _text: "COSTUME_DETAILS: lagoon theme.")

    await session._process_utterance_bound("Please describe the costume theme.")

    request_text = _request_text("openai", client)
    assert "COSTUME_DETAILS" in request_text
    assert not _has_rate_record(request_text)


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ("openai", "gemini", "claude"))
@pytest.mark.parametrize(
    ("utterance", "kb_marker"),
    (
        (
            "Describe the costume theme in Family Cottage.",
            "COSTUME_DETAILS: lagoon theme.",
        ),
        (
            "What does dinner cost at Family Cottage?",
            "DINNER_DETAILS: dinner is served in the restaurant.",
        ),
    ),
)
async def test_room_scoped_non_price_cost_language_keeps_descriptive_kb_available(
    monkeypatch, provider, utterance, kb_marker
):
    session, client = _provider_session(provider)
    session._booking_slots.update(_mid_stay_slots(
        room_type="Family Cottage", residency="resident",
    ))
    monkeypatch.setattr(server, "retrieve_context", lambda _text: kb_marker)

    await session._process_utterance_bound(utterance)

    request_text = _request_text(provider, client)
    assert kb_marker in request_text
    assert not _has_rate_record(request_text)


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ("openai", "gemini", "claude"))
async def test_indirect_selection_phrase_with_dinner_cost_keeps_descriptive_kb(
    monkeypatch, provider
):
    session, client = _provider_session(provider)
    session._booking_slots.update(_mid_stay_slots(
        room_type="Family Cottage", residency="resident",
    ))
    monkeypatch.setattr(server, "retrieve_context", lambda _text: "DINNER_DETAILS")

    await session._process_utterance_bound(
        "I would like to know what dinner costs at Family Cottage."
    )

    request_text = _request_text(provider, client)
    assert "DINNER_DETAILS" in request_text
    assert not _has_rate_record(request_text)


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ("openai", "gemini", "claude"))
async def test_direct_selection_captures_room_without_inventing_a_rate(
    monkeypatch, provider
):
    session, client = _provider_session(provider)
    monkeypatch.setattr(server, "retrieve_context", lambda _text: "UNUSED_KB")

    await session._process_utterance_bound("I would like Family Cottage.")

    request_text = _request_text(provider, client)
    assert session._booking_slots["room_type"] == "Family Cottage"
    assert "rate_per_room_per_night" not in request_text


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ("openai", "gemini", "claude"))
async def test_direct_selection_then_explicit_room_rate_sends_authoritative_rate(
    monkeypatch, provider
):
    session, client = _provider_session(provider)
    session._booking_slots.update(_mid_stay_slots(residency="resident"))
    monkeypatch.setattr(server, "retrieve_context", lambda _text: "UNUSED_KB")

    await session._process_utterance_bound("I would like Family Cottage.")
    await session._process_utterance_bound("What is the room rate?")

    request_text = _current_turn_request_text(provider, client)
    assert _has_rate_record(request_text)
    assert "rate_per_room_per_night: 24800" in request_text
    assert "UNUSED_KB" not in request_text


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ("openai", "gemini", "claude"))
async def test_explicit_room_rate_wording_still_sends_the_authoritative_rate_record(
    monkeypatch, provider
):
    session, client = _provider_session(provider)
    session._booking_slots.update(_mid_stay_slots(residency="resident"))
    monkeypatch.setattr(server, "retrieve_context", lambda _text: "UNUSED_KB")

    await session._process_utterance_bound(
        "What is the room rate for Family Cottage?"
    )

    request_text = _request_text(provider, client)
    assert _has_rate_record(request_text)
    assert "rate_per_room_per_night: 24800" in request_text
    assert "UNUSED_KB" not in request_text


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ("openai", "gemini", "claude"))
async def test_explicit_canonical_room_amount_question_sends_authoritative_rate(
    monkeypatch, provider
):
    session, client = _provider_session(provider)
    session._booking_slots.update(_mid_stay_slots(residency="resident"))
    monkeypatch.setattr(server, "retrieve_context", lambda _text: "UNUSED_KB")

    await session._process_utterance_bound("How much is Family Cottage?")

    request_text = _request_text(provider, client)
    assert _has_rate_record(request_text)
    assert "rate_per_room_per_night: 24800" in request_text
    assert "UNUSED_KB" not in request_text


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ("openai", "gemini", "claude"))
@pytest.mark.parametrize(
    "utterance",
    (
        "How much is Family Cottage?",
        "How much is Family Cottage per night?",
    ),
)
async def test_canonical_room_amount_controls_remain_authoritative(
    monkeypatch, provider, utterance
):
    session, client = _provider_session(provider)
    session._booking_slots.update(_mid_stay_slots(residency="resident"))
    monkeypatch.setattr(server, "retrieve_context", lambda _text: "UNUSED_KB")

    await session._process_utterance_bound(utterance)

    request_text = _request_text(provider, client)
    assert _has_rate_record(request_text)
    assert "rate_per_room_per_night: 24800" in request_text
    assert "UNUSED_KB" not in request_text


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ("openai", "gemini", "claude"))
async def test_possessive_room_name_dinner_amount_question_remains_descriptive(
    monkeypatch, provider
):
    session, client = _provider_session(provider)
    session._booking_slots.update(_mid_stay_slots(
        room_type="Family Cottage", residency="resident",
    ))
    monkeypatch.setattr(server, "retrieve_context", lambda _text: "DINNER_DETAILS")

    await session._process_utterance_bound(
        "How much is Family Cottage's dinner?"
    )

    request_text = _request_text(provider, client)
    assert "DINNER_DETAILS" in request_text
    assert not _has_rate_record(request_text)


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ("openai", "gemini", "claude"))
async def test_explicit_rate_turn_enables_grounded_amount_pronoun_follow_up(
    monkeypatch, provider
):
    session, client = _provider_session(provider)
    session._booking_slots.update(_mid_stay_slots(
        room_type="Family Cottage", residency="resident",
    ))
    monkeypatch.setattr(server, "retrieve_context", lambda _text: "UNUSED_KB")

    await session._process_utterance_bound("What is the room rate?")
    await session._process_utterance_bound("How much is it?")

    request_text = _request_text(provider, client)
    assert _has_rate_record(request_text)
    assert "rate_per_room_per_night: 24800" in request_text
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
    assert not _has_rate_record(request_text)


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ("openai", "gemini", "claude"))
@pytest.mark.parametrize(
    "utterance",
    (
        "What is the room rate for Family Cottage or Family Chalet?",
        "How much is Family Cottage or Family Chalet?",
        "How much are Family Cottage and Family Chalet?",
        "How much is Family Cottage or Family Chalet per night?",
        "How much are Family Cottage and Family Chalet per night?",
        "How much is Family Cottage or Family Chalet per room per night?",
        "How much are Family Cottage and Family Chalet per room per night?",
        "How much is Family Cottage or Family Chalet for one night?",
        "How much are Family Cottage and Family Chalet for one night?",
        "How much is the Family Cottage or Family Chalet?",
        "How much are the Family Cottage and Family Chalet?",
        "How much is the Family Cottage or Family Chalet per night?",
        "How much are the Family Cottage and Family Chalet per night?",
        "How much is the Family Cottage or Family Chalet per room per night?",
        "How much are the Family Cottage and Family Chalet per room per night?",
        "How much is the Family Cottage or Family Chalet for one night?",
        "How much are the Family Cottage and Family Chalet for one night?",
    ),
)
async def test_ambiguous_multi_room_rate_requests_are_no_quote_without_stale_room(
    monkeypatch, provider, utterance
):
    session, client = _provider_session(provider)
    session._booking_slots.update(_mid_stay_slots(
        room_type="Family Cottage", residency="resident",
    ))
    monkeypatch.setattr(server, "retrieve_context", lambda _text: "AMBIGUOUS_KB")

    await session._process_utterance_bound(utterance)

    request_text = _request_text(provider, client)
    assert _has_rate_record(request_text)
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
    session._booking_slots.update(_mid_stay_slots(
        room_type="Family Cottage", residency="resident",
    ))
    monkeypatch.setattr(server, "retrieve_context", lambda _text: "SEMANTIC_KB")

    await session._process_utterance_bound(utterance)

    request_text = _request_text(provider, client)
    assert "SEMANTIC_KB" in request_text
    assert not _has_rate_record(request_text)


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ("openai", "gemini", "claude"))
@pytest.mark.parametrize(
    ("utterance", "expected_room"),
    (
        ("I would like Family Cottage.", "Family Cottage"),
        ("Is Family Cottage available?", "Family Cottage"),
        ("I would like to know what dinner costs at Family Cottage.", None),
        ("Is dinner available at Family Cottage?", None),
    ),
)
async def test_selection_and_availability_do_not_activate_room_pricing(
    monkeypatch, provider, utterance, expected_room
):
    session, client = _provider_session(provider)
    session._booking_slots.update(_mid_stay_slots(residency="resident"))
    monkeypatch.setattr(server, "retrieve_context", lambda _text: "SELECTION_KB")

    await session._process_utterance_bound(utterance)

    request_text = _request_text(provider, client)
    assert session._booking_slots.get("room_type") == expected_room
    assert "SELECTION_KB" in request_text
    assert not _has_rate_record(request_text)


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ("openai", "gemini", "claude"))
@pytest.mark.parametrize(
    "utterance",
    (
        "How much does Family Cottage cost?",
        "What is the price of Family Cottage?",
        "What is the price of the Family Cottage?",
        "How much is Family Cottage for one night?",
    ),
)
async def test_natural_explicit_room_price_forms_send_authoritative_rate(
    monkeypatch, provider, utterance
):
    session, client = _provider_session(provider)
    session._booking_slots.update(_mid_stay_slots(residency="resident"))
    monkeypatch.setattr(server, "retrieve_context", lambda _text: "UNUSED_KB")

    await session._process_utterance_bound(utterance)

    request_text = _request_text(provider, client)
    assert _has_rate_record(request_text)
    assert "rate_per_room_per_night: 24800" in request_text
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
    assert _has_rate_record(request_text)
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
    session._booking_slots.update(_mid_stay_slots(
        room_type="Family Cottage", residency="resident",
    ))
    monkeypatch.setattr(server, "retrieve_context", lambda _text: "UNKNOWN_ROOM_KB")

    await session._process_utterance_bound(utterance)

    current_turn = _current_turn_request_text(provider, client)
    rate_record = _current_authoritative_rate_record(provider, client)
    assert _has_rate_record(rate_record)
    assert "status: no_quote" in rate_record
    assert "reason: unknown_room" in rate_record
    assert "Family Cottage" not in rate_record
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
    session._booking_slots.update(_mid_stay_slots(
        room_type="Family Cottage", residency="resident",
    ))
    monkeypatch.setattr(server, "retrieve_context", lambda _text: "SUBJECT_KB")

    await session._process_utterance_bound(utterance)

    request_text = _request_text(provider, client)
    assert "SUBJECT_KB" in request_text
    assert not _has_rate_record(request_text)


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ("openai", "gemini", "claude"))
async def test_explicit_rate_follow_up_expires_after_a_non_rate_turn(
    monkeypatch, provider
):
    session, client = _provider_session(provider)
    session._booking_slots.update(_mid_stay_slots(
        room_type="Family Cottage", residency="resident",
    ))

    def retrieve(text: str) -> str:
        return "FOLLOWUP_KB" if text == "How much is it?" else "SPA_KB"

    monkeypatch.setattr(server, "retrieve_context", retrieve)
    await session._process_utterance_bound("What is the room rate?")
    await session._process_utterance_bound("Please tell me about the spa.")
    await session._process_utterance_bound("How much is it?")

    request_text = _request_text(provider, client)
    assert "FOLLOWUP_KB" in request_text
    assert not _has_rate_record(request_text)


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ("openai", "gemini", "claude"))
async def test_incomplete_rate_state_does_not_ground_an_amount_pronoun(
    monkeypatch, provider
):
    session, client = _provider_session(provider)
    session._booking_slots.update({
        "room_type": "Family Cottage",
        "residency": "resident",
    })
    monkeypatch.setattr(server, "retrieve_context", lambda _text: "INCOMPLETE_KB")

    await session._process_utterance_bound("How much is it?")

    request_text = _request_text(provider, client)
    assert "INCOMPLETE_KB" in request_text
    assert not _has_rate_record(request_text)


@pytest.mark.asyncio
async def test_stale_smartpbx_runner_cannot_inherit_a_newer_turn_rate_context(monkeypatch):
    session, client = _provider_session("openai")
    session._smartpbx_transfer_context = object()
    session._media_transport = SimpleNamespace(frames_dropped_total=0)
    session._booking_slots.update(_mid_stay_slots(room_type="Family Cottage"))
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
    assert "rate_per_room_per_night: 24800" in request_text
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
        "I would like Family Cottage."
    ) == "Family Cottage"
    await _assert_stale_runner_cannot_commit_rate_state(
        monkeypatch, "I would like Family Cottage.", "room_type",
    )


@pytest.mark.asyncio
async def test_stale_smartpbx_runner_cannot_commit_explicit_residency(
    monkeypatch,
):
    assert rate_catalog.recognize_residency("I am local.") == "resident"
    await _assert_stale_runner_cannot_commit_rate_state(
        monkeypatch, "I am local.", "residency",
    )
