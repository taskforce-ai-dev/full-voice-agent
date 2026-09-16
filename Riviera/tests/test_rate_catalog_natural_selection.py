"""Production-shaped regression contracts for natural room/residency rate turns.

Three live calls were lost because Riya could not turn an ordinary sentence into
a rate she already knows deterministically.  Re-expressed for Riviera Resort's
catalogue, the shapes are:

1. "Uh, the Steel Cabana, please."                -> "I'm a Sri Lankan resident."
2. "I'd like to go with the Family Chalet."       -> "Resident."
3. "I'll go with the Family Cottage. Thank you."  -> "I am a Sri Lankan resident."

None of the three produced a rate.  The rate card proves the rates exist for
those October stays (mid season), so the failure is entirely in recognition:
the exact room recognizer returns ``None`` for all three selections, the
residency recognizer returns ``None`` for a terse "Resident.", and the
room-rate classifier returns ``NONE`` for the natural local/October wordings
guests actually use.

These tests define the recognition contract that must hold *before* any of it is
implemented.  They assert behaviour — which room is captured, which residency is
persisted, which exact record reaches each provider runner — rather than the
wording of the production source.  Semantic knowledge retrieval is deliberately
never allowed to supply the answer: legacy KB retrieval for these queries
returns rate-like prose and booking policy, which is exactly how the wrong
numbers reached callers before the deterministic catalog existed.
"""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

import pytest

import rate_catalog
import server
import yanolja_service


# ── Canonical October stay used by every incident reproduction ────────────────
# October 2026 sits inside Riviera's MID season (1 Sep 2026 – 30 Jun 2027).
OCTOBER_CHECK_IN = "2026-10-05"
OCTOBER_CHECK_OUT = "2026-10-08"

# Rate-card results for that stay (resident, LKR, per room per night, room only).
OCTOBER_RESIDENT_RATE_LKR = {
    "Basic Room Single": 8800,
    "Wooden Cabana": 11000,
    "Lagoon View Steel Cabana": 14600,
    "Double Lagoon or Garden View": 17500,
    "Triple Garden View": 20400,
    "Family Cottage": 24800,
    "Family Chalet": 32100,
}

# The prose legacy semantic retrieval returns for these queries: rate-like
# figures and policy text that must never be the source of a quoted number.
LEGACY_KB_PROSE = (
    "LEGACY_KB: room rates from 8,800 to 46,900 rupees per room per night "
    "depending on season, and the booking cancellation policy."
)

RESIDENCY_ASKS = (
    "Are you a Sri Lankan resident, or a foreign guest?",
    "May I know if you are a Sri Lankan resident or a foreign guest?",
)

TERSE_RESIDENCY_REPLIES = ("Resident.", "Resident", "resident", "Local.", "Local", "local")

NON_RESIDENCY_ASKS = (
    "Which room would you like?",
    "May I have your name, please?",
)


def test_october_rate_table_matches_the_service_mid_season_card():
    """The fixture must track the single source of truth, never drift from it."""
    assert OCTOBER_RESIDENT_RATE_LKR == {
        room: seasons["mid"] for room, seasons in yanolja_service.NIGHTLY_RATE_LKR.items()
    }
    assert set(OCTOBER_RESIDENT_RATE_LKR) == set(yanolja_service.ALL_ROOM_TYPES)


# ── rate_catalog: safe aliases, and nothing looser ───────────────────────────

@pytest.mark.parametrize(
    ("alias", "canonical"),
    (
        ("Steel Cabana", "Lagoon View Steel Cabana"),
        ("Chalet", "Family Chalet"),
        ("Cottage", "Family Cottage"),
        ("Wooden Cabana", "Wooden Cabana"),
        ("Wooden", "Wooden Cabana"),
        ("Basic Room", "Basic Room Single"),
        ("Double room", "Double Lagoon or Garden View"),
        ("Triple", "Triple Garden View"),
    ),
)
def test_safe_room_aliases_resolve_to_their_canonical_room(alias, canonical):
    assert rate_catalog.recognize_selected_room(alias) == canonical


@pytest.mark.parametrize(
    ("alias", "canonical"),
    (
        ("The Steel Cabana, please.", "Lagoon View Steel Cabana"),
        ("I'll go with the Chalet.", "Family Chalet"),
        ("Go with the Cottage.", "Family Cottage"),
        ("I'll go with the double room.", "Double Lagoon or Garden View"),
    ),
)
def test_safe_aliases_work_inside_natural_selection_sentences(alias, canonical):
    assert rate_catalog.recognize_selected_room(alias) == canonical


@pytest.mark.parametrize(
    "fragment",
    (
        "Lagoon",
        "View",
        "Garden",
        "Family",
        "Room",
        "Single",
        "Lagoon View",
        "Garden View",
        "The Lagoon View, please.",
        "I'll go with the Family.",
        "I'll go with the Lagoon.",
    ),
)
def test_room_aliases_never_become_arbitrary_fuzzy_matching(fragment):
    assert rate_catalog.recognize_selected_room(fragment) is None


@pytest.mark.parametrize(
    "utterance",
    (
        "Cabana",
        "the cabana",
        "The cabana, please.",
        "I'll go with the cabana.",
    ),
)
def test_bare_cabana_is_ambiguous_between_both_cabanas(utterance):
    """"cabana" is aliased to BOTH cabanas on purpose: Riya asks which one."""
    assert rate_catalog.recognize_selected_room(utterance) is None


def test_bare_cabana_rate_question_is_never_a_single_room_rate():
    classification = rate_catalog.classify_room_rate_intent("how much is the cabana")

    assert classification.kind in {"NONE", "AMBIGUOUS_RATE"}
    assert len(classification.rooms) != 1


@pytest.mark.parametrize(
    "utterance",
    (
        "Chalet or Cottage?",
        "I'll go with the Chalet or the Cottage.",
        "Is Steel Cabana or Chalet available?",
    ),
)
def test_two_aliased_rooms_in_one_utterance_stay_ambiguous(utterance):
    assert rate_catalog.recognize_selected_room(utterance) is None


# ── rate_catalog: natural selection grammar with bounded filler/punctuation ──

@pytest.mark.parametrize(
    ("utterance", "expected"),
    (
        # The three production incident shapes, re-expressed for Riviera.
        ("Uh, the Steel Cabana, please.", "Lagoon View Steel Cabana"),
        ("I'd like to go with the Family Chalet.", "Family Chalet"),
        ("I'll go with the Family Cottage. Thank you.", "Family Cottage"),
        # The same three shapes over the remaining canonical rooms.
        ("The Basic Room Single, please.", "Basic Room Single"),
        ("Um, the Wooden Cabana please.", "Wooden Cabana"),
        ("Go with the Basic Room Single.", "Basic Room Single"),
        ("I'll go with the Wooden Cabana.", "Wooden Cabana"),
        ("I'd like to go with the Lagoon View Steel Cabana.", "Lagoon View Steel Cabana"),
        ("I'll go with the Family Chalet, thanks.", "Family Chalet"),
        ("I'll go with the Double Lagoon or Garden View.", "Double Lagoon or Garden View"),
        ("The Triple Garden View, please.", "Triple Garden View"),
    ),
)
def test_natural_room_selection_forms_capture_the_canonical_room(utterance, expected):
    assert rate_catalog.recognize_selected_room(utterance) == expected


@pytest.mark.parametrize(
    "utterance",
    (
        "I would like to know what dinner costs at the Family Chalet.",
        "Is dinner available at the Steel Cabana?",
        "The dinner, please.",
        "Go with the spa package, please.",
        "Please go with whatever you recommend.",
        "I'll go with the earlier date. Thank you.",
        "How much is the Family Cottage's dinner, please?",
    ),
)
def test_natural_selection_grammar_stays_bounded(utterance):
    assert rate_catalog.recognize_selected_room(utterance) is None


# ── rate_catalog: terse residency only while the session awaits it ────────────

@pytest.mark.parametrize("utterance", TERSE_RESIDENCY_REPLIES)
def test_terse_resident_is_never_inferred_globally(utterance):
    assert rate_catalog.recognize_residency(utterance) is None


@pytest.mark.parametrize(
    "utterance",
    (
        "I am local but my partner is foreign.",
        "A guest from overseas is arriving too.",
        "My surname sounds Sri Lankan.",
        "Resident parking is included, right?",
    ),
)
def test_terse_residency_context_never_unlocks_ambiguous_or_third_party_statements(
    utterance,
):
    assert rate_catalog.recognize_residency(utterance) is None


@pytest.mark.parametrize(
    ("utterance", "expected"),
    (
        ("I am a Sri Lankan resident.", "resident"),
        ("I'm not local", "foreign"),
        ("foreign guest", "foreign"),
    ),
)
def test_explicit_residency_statements_remain_supported(utterance, expected):
    assert rate_catalog.recognize_residency(utterance) == expected


# ── rate_catalog: natural local / October rate classification ────────────────

@pytest.mark.parametrize(
    "utterance",
    (
        "local rates for October",
        "What are the local rates for October?",
        "what are the local rates for october",
        "What are the resident rates for October?",
        "What is the local rate for October?",
    ),
)
def test_natural_local_october_rate_requests_are_room_rate_intents(utterance):
    classification = rate_catalog.classify_room_rate_intent(utterance)

    assert classification.kind == "RATE"
    assert classification.rooms == ()
    assert not classification.unresolved


@pytest.mark.parametrize(
    ("utterance", "expected"),
    (
        ("rate for Basic Room Single in October", ("Basic Room Single",)),
        (
            "What is the rate for the Family Chalet in October?",
            ("Family Chalet",),
        ),
        ("rate for Steel Cabana in October", ("Lagoon View Steel Cabana",)),
    ),
)
def test_named_room_rate_request_with_a_month_resolves_that_room(utterance, expected):
    classification = rate_catalog.classify_room_rate_intent(utterance)

    assert classification.kind == "RATE"
    assert classification.rooms == expected


@pytest.mark.parametrize(
    "utterance",
    (
        "What are the spa rates for October?",
        "What are the dinner rates for October?",
        "How much does dinner cost in October?",
        "What are the activity rates for October?",
    ),
)
def test_month_scoped_non_room_rate_requests_stay_out_of_the_rate_classifier(utterance):
    classification = rate_catalog.classify_room_rate_intent(utterance)

    assert classification.kind == "NONE"
    assert classification.rooms == ()


# ── rate_catalog: every October resident rate is quotable, never "unavailable" ─

@pytest.mark.parametrize(
    ("room", "expected"), tuple(OCTOBER_RESIDENT_RATE_LKR.items())
)
def test_every_october_resident_rate_is_quotable_and_never_unavailable(room, expected):
    resolution = rate_catalog.resolve_rate(
        room=room,
        residency="resident",
        check_in=OCTOBER_CHECK_IN,
        check_out=OCTOBER_CHECK_OUT,
    )

    assert resolution.is_quotable
    assert resolution.currency == "LKR"
    assert resolution.nightly_rate == expected
    assert resolution.season == "mid"
    record = resolution.authoritative_context()
    assert f"rate_per_room_per_night: {expected}" in record
    assert "season: mid season" in record
    assert "basis: room only, inclusive of all taxes" in record
    assert "no_quote" not in record
    assert "human confirmation" not in record


# ── Provider runner harness (OpenAI / Gemini / Claude) ───────────────────────

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
        model=f"{provider}-natural-rate-test",
    )
    session.tools = []

    async def _no_speak(*_args, **_kwargs):
        return None

    session._invoke_speak = _no_speak
    return session, clients[provider]


def _october_session(provider: str, **slots):
    session, client = _provider_session(provider)
    session._booking_slots.update({
        "check_in": OCTOBER_CHECK_IN,
        "check_out": OCTOBER_CHECK_OUT,
        **slots,
    })
    return session, client


def _seed_delivered_residency_question(session, question: str = RESIDENCY_ASKS[0]):
    """Seed the prior assistant turn that explicitly arms a terse reply.

    Room and date slots are deliberately not enough.  The production boundary
    must carry forward an actual assistant residency question, and stale-runner
    fencing must still prevent that evidence from being committed by an old
    owned turn.
    """
    session.history.append({"role": "assistant", "content": question})
    session.full_transcript.append({"role": "assistant", "text": question})


def _current_turn_request_text(provider: str, client) -> str:
    """Return only the system/current-user input, never prior turn history."""
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
    current_user = next(
        message for message in reversed(request["messages"])
        if message.get("role") == "user"
    )
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


def _legacy_kb(monkeypatch) -> list[str]:
    """Serve the misleading legacy prose and record every query it was asked."""
    queries: list[str] = []

    def retrieve(text: str) -> str:
        queries.append(text)
        return LEGACY_KB_PROSE

    monkeypatch.setattr(server, "retrieve_context", retrieve)
    return queries


def _assert_no_kb_dependency(record: str, turn_text: str, queries: list[str]) -> None:
    assert "LEGACY_KB" not in record
    assert turn_text not in queries, (
        "a deterministic rate turn must not depend on semantic KB retrieval"
    )


# ── The three production incidents, on every provider runner ─────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ("openai", "gemini", "claude"))
async def test_incident_steel_cabana_then_residency_quotes_the_october_rate(
    monkeypatch, provider
):
    session, client = _october_session(provider)
    queries = _legacy_kb(monkeypatch)

    await session._process_utterance_bound("Uh, the Steel Cabana, please.")
    await session._process_utterance_bound("I'm a Sri Lankan resident.")

    assert session._booking_slots["room_type"] == "Lagoon View Steel Cabana"
    assert session._booking_slots["residency"] == "resident"
    record = _current_authoritative_rate_record(provider, client)
    assert "rate_per_room_per_night: 14600" in record
    assert "season: mid season" in record
    assert "no_quote" not in record
    _assert_no_kb_dependency(record, "I'm a Sri Lankan resident.", queries)


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ("openai", "gemini", "claude"))
@pytest.mark.parametrize("residency_ask", RESIDENCY_ASKS)
async def test_incident_family_chalet_then_terse_resident_quotes_the_october_rate(
    monkeypatch, provider, residency_ask
):
    session, client = _october_session(provider)
    queries = _legacy_kb(monkeypatch)

    await session._process_utterance_bound("I'd like to go with the Family Chalet.")
    _seed_delivered_residency_question(session, residency_ask)
    await session._process_utterance_bound("Resident.")

    assert session._booking_slots["room_type"] == "Family Chalet"
    assert session._booking_slots["residency"] == "resident"
    record = _current_authoritative_rate_record(provider, client)
    assert "rate_per_room_per_night: 32100" in record
    assert "no_quote" not in record
    _assert_no_kb_dependency(record, "Resident.", queries)


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ("openai", "gemini", "claude"))
async def test_incident_family_cottage_with_trailing_thanks_quotes_the_october_rate(
    monkeypatch, provider
):
    session, client = _october_session(provider)
    queries = _legacy_kb(monkeypatch)

    await session._process_utterance_bound(
        "I'll go with the Family Cottage. Thank you."
    )
    await session._process_utterance_bound("I am a Sri Lankan resident.")

    assert session._booking_slots["room_type"] == "Family Cottage"
    record = _current_authoritative_rate_record(provider, client)
    assert "rate_per_room_per_night: 24800" in record
    assert "no_quote" not in record
    _assert_no_kb_dependency(record, "I am a Sri Lankan resident.", queries)


# ── A terse "Resident." is inert unless the session actually asked ───────────

@pytest.mark.asyncio
@pytest.mark.parametrize("non_residency_ask", NON_RESIDENCY_ASKS)
@pytest.mark.parametrize("utterance", TERSE_RESIDENCY_REPLIES)
async def test_terse_resident_is_ignored_when_the_session_is_not_awaiting_residency(
    monkeypatch, non_residency_ask, utterance
):
    """A bare session — nothing asked, no room, no dates — must stay inert.

    Deliberately seed a delivered assistant turn that asks about a different
    field.  Even with a live assistant turn in history, that is not evidence
    that Riya asked the residency question.
    """
    session, client = _provider_session("openai")
    _legacy_kb(monkeypatch)
    _seed_delivered_residency_question(session, non_residency_ask)

    await session._process_utterance_bound(utterance)

    assert "residency" not in session._booking_slots
    assert "rate_per_room_per_night" not in _current_turn_request_text(
        "openai", client
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("utterance", ("Resident.", "Local."))
async def test_room_and_dates_alone_do_not_admit_a_terse_resident_reply(
    monkeypatch, utterance
):
    """A slot gap is not evidence that Riya asked the residency question."""
    session, client = _october_session(
        "openai", room_type="Family Chalet",
    )
    _legacy_kb(monkeypatch)

    await session._process_utterance_bound(utterance)

    assert "residency" not in session._booking_slots
    record = _current_authoritative_rate_record("openai", client)
    assert "status: no_quote" not in record
    assert "rate_per_room_per_night" not in record


@pytest.mark.asyncio
@pytest.mark.parametrize("utterance", ("Resident.", "Local."))
async def test_explicit_residency_question_arms_the_terse_reply(
    monkeypatch, utterance
):
    session, client = _october_session(
        "openai", room_type="Family Chalet",
    )
    _legacy_kb(monkeypatch)
    _seed_delivered_residency_question(session)

    await session._process_utterance_bound(utterance)

    assert session._booking_slots["residency"] == "resident"
    record = _current_authoritative_rate_record("openai", client)
    assert "rate_per_room_per_night: 32100" in record


# ── The rate is quoted before identity capture, never handed to a human ──────

@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ("openai", "gemini", "claude"))
async def test_known_october_rate_is_quoted_before_any_name_or_phone_is_captured(
    monkeypatch, provider
):
    session, client = _october_session(provider)
    _legacy_kb(monkeypatch)

    await session._process_utterance_bound("Uh, the Steel Cabana, please.")
    await session._process_utterance_bound("I'm a Sri Lankan resident.")

    record = _current_authoritative_rate_record(provider, client)
    assert "rate_per_room_per_night: 14600" in record
    assert "no_quote" not in record
    assert "human confirmation" not in record
    turn_text = _current_turn_request_text(provider, client)
    assert "I'm a Sri Lankan resident." in turn_text
    assert session.history[-1]["role"] == "assistant"
    assert "capture_spoken_name" not in str(session.history[-1]["content"])
    assert "capture_spoken_number" not in str(session.history[-1]["content"])
    assert session.transfer_pending is False
    assert "guest_name" not in session._booking_slots
    assert "guest_phone" not in session._booking_slots


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ("openai", "gemini", "claude"))
@pytest.mark.parametrize(
    ("room", "utterance"),
    (
        ("Basic Room Single", "The Basic Room Single, please."),
        ("Wooden Cabana", "I'll go with the Wooden Cabana."),
        ("Lagoon View Steel Cabana", "Uh, the Steel Cabana, please."),
        ("Double Lagoon or Garden View", "I'll go with the double room."),
        ("Triple Garden View", "The Triple Garden View, please."),
        ("Family Cottage", "I'll go with the Family Cottage. Thank you."),
        ("Family Chalet", "I'd like to go with the Family Chalet."),
    ),
)
async def test_every_room_selection_reaches_its_exact_october_rate(
    monkeypatch, provider, room, utterance
):
    session, client = _october_session(provider)
    _legacy_kb(monkeypatch)

    await session._process_utterance_bound(utterance)
    await session._process_utterance_bound("I am a Sri Lankan resident.")

    record = _current_authoritative_rate_record(provider, client)
    assert session._booking_slots["room_type"] == room
    assert f"rate_per_room_per_night: {OCTOBER_RESIDENT_RATE_LKR[room]}" in record
    assert "no_quote" not in record


# ── Generic plural local-rate request without a room asks which room ─────────

@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ("openai", "gemini", "claude"))
@pytest.mark.parametrize(
    "utterance",
    (
        "What are the local rates for October?",
        "local rates for October",
        "What are the resident rates for October?",
    ),
)
async def test_generic_plural_local_rate_request_without_a_room_asks_which_room(
    monkeypatch, provider, utterance
):
    session, client = _october_session(provider, residency="resident")
    _legacy_kb(monkeypatch)

    await session._process_utterance_bound(utterance)

    turn_text = _current_turn_request_text(provider, client)
    record = _current_authoritative_rate_record(provider, client)
    assert _has_rate_record(record)
    assert "status: no_quote" in record
    assert "reason: unknown_room" in record
    assert utterance in turn_text
    for room, rate in OCTOBER_RESIDENT_RATE_LKR.items():
        assert room not in record, "a missing room must not be guessed"
        assert str(rate) not in turn_text, "rates must not be enumerated"
    assert "LEGACY_KB" not in turn_text
    assert "room_type" not in session._booking_slots


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ("openai", "gemini", "claude"))
async def test_generic_plural_local_rate_request_uses_the_already_selected_room(
    monkeypatch, provider
):
    session, client = _october_session(
        provider, room_type="Family Cottage", residency="resident",
    )
    _legacy_kb(monkeypatch)

    await session._process_utterance_bound("What are the local rates for October?")

    record = _current_authoritative_rate_record(provider, client)
    assert "rate_per_room_per_night: 24800" in record
    assert "no_quote" not in record


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ("openai", "gemini", "claude"))
async def test_named_room_month_rate_request_quotes_that_room(monkeypatch, provider):
    session, client = _october_session(provider, residency="resident")
    _legacy_kb(monkeypatch)

    await session._process_utterance_bound("rate for Basic Room Single in October")

    record = _current_authoritative_rate_record(provider, client)
    assert "rate_per_room_per_night: 8800" in record
    assert "no_quote" not in record


# ── Preserved behaviour: foreign no-quote, seasons, mixed periods, unknown rooms ─

@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ("openai", "gemini", "claude"))
async def test_natural_selection_then_foreign_residency_is_a_no_quote_to_reservations(
    monkeypatch, provider
):
    """There is no foreign-guest rate card: never fall back to resident LKR."""
    session, client = _october_session(provider)
    _legacy_kb(monkeypatch)

    await session._process_utterance_bound("I'd like to go with the Family Chalet.")
    await session._process_utterance_bound("I am a foreign guest.")

    assert session._booking_slots["room_type"] == "Family Chalet"
    assert session._booking_slots["residency"] == "foreign"
    record = _current_authoritative_rate_record(provider, client)
    assert "status: no_quote" in record
    assert "reason: foreign_rates_unpublished" in record
    assert "reservations" in record.lower()
    assert "rate_per_room_per_night" not in record
    assert "currency: USD" not in record
    assert "LEGACY_KB" not in record


@pytest.mark.parametrize(
    ("check_in", "check_out", "expected", "season"),
    (
        ("2027-07-10", "2027-07-12", 46900, "high"),
        ("2026-12-20", "2026-12-22", 32100, "mid"),
    ),
)
def test_high_season_july_and_mid_season_december_resident_rates_are_preserved(
    check_in, check_out, expected, season
):
    resolution = rate_catalog.resolve_rate(
        room="Family Chalet",
        residency="resident",
        check_in=check_in,
        check_out=check_out,
    )

    assert resolution.is_quotable
    assert resolution.currency == "LKR"
    assert resolution.nightly_rate == expected
    assert resolution.season == season


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ("openai", "gemini", "claude"))
async def test_natural_selection_across_seasons_still_refuses_to_quote(
    monkeypatch, provider
):
    session, client = _provider_session(provider)
    session._booking_slots.update({
        "check_in": "2027-06-29", "check_out": "2027-07-02",
    })
    _legacy_kb(monkeypatch)

    await session._process_utterance_bound(
        "I'll go with the Family Cottage. Thank you."
    )
    await session._process_utterance_bound("I am a Sri Lankan resident.")

    record = _current_authoritative_rate_record(provider, client)
    assert "status: no_quote" in record
    assert "reason: mixed_period" in record
    assert "rate_per_room_per_night" not in record


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ("openai", "gemini", "claude"))
async def test_natural_selection_outside_published_seasons_still_refuses_to_quote(
    monkeypatch, provider
):
    session, client = _provider_session(provider)
    session._booking_slots.update({
        "check_in": "2026-08-20", "check_out": "2026-08-22",
    })
    _legacy_kb(monkeypatch)

    await session._process_utterance_bound(
        "I'll go with the Family Cottage. Thank you."
    )
    await session._process_utterance_bound("I am a Sri Lankan resident.")

    record = _current_authoritative_rate_record(provider, client)
    assert "status: no_quote" in record
    assert "reason: unsupported_period" in record
    assert "rate_per_room_per_night" not in record


@pytest.mark.parametrize(
    "utterance",
    (
        "I'll go with the Royal Villa.",
        "The Royal Villa, please.",
        "Go with the Royal Villa.",
    ),
)
def test_unknown_rooms_are_never_captured_by_the_natural_selection_forms(utterance):
    assert rate_catalog.recognize_selected_room(utterance) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ("openai", "gemini", "claude"))
async def test_unknown_room_month_rate_request_is_an_authoritative_no_quote(
    monkeypatch, provider
):
    session, client = _october_session(provider, residency="resident")
    _legacy_kb(monkeypatch)

    await session._process_utterance_bound("rate for Royal Villa in October")

    turn_text = _current_turn_request_text(provider, client)
    record = _current_authoritative_rate_record(provider, client)
    assert "status: no_quote" in record
    assert "reason: unknown_room" in record
    assert "rate_per_room_per_night" not in record
    assert "LEGACY_KB" not in turn_text


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ("openai", "gemini", "claude"))
@pytest.mark.parametrize(
    "utterance",
    (
        "What are the spa rates for October?",
        "What are the dinner rates for October?",
        "How much does dinner cost in October?",
    ),
)
async def test_non_room_price_questions_keep_the_semantic_knowledge_base(
    monkeypatch, provider, utterance
):
    session, client = _october_session(
        provider, room_type="Family Cottage", residency="resident",
    )
    _legacy_kb(monkeypatch)

    await session._process_utterance_bound(utterance)

    turn_text = _current_turn_request_text(provider, client)
    assert "LEGACY_KB" in turn_text
    assert not _has_rate_record(turn_text)


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ("openai", "gemini", "claude"))
@pytest.mark.parametrize(
    ("utterance", "expected_room"),
    (
        ("Uh, the Steel Cabana, please.", "Lagoon View Steel Cabana"),
        ("I'd like to go with the Family Chalet.", "Family Chalet"),
        ("I'll go with the Family Cottage. Thank you.", "Family Cottage"),
    ),
)
async def test_the_selection_turn_itself_never_quotes_a_rate(
    monkeypatch, provider, utterance, expected_room
):
    session, client = _october_session(provider)
    _legacy_kb(monkeypatch)

    await session._process_utterance_bound(utterance)

    turn_text = _current_turn_request_text(provider, client)
    assert session._booking_slots["room_type"] == expected_room
    assert "LEGACY_KB" in turn_text
    assert "rate_per_room_per_night" not in turn_text


# ── Shared Twilio media path and stale-runner ownership ─────────────────────

@pytest.mark.asyncio
async def test_shared_twilio_media_session_gets_the_same_october_rate(monkeypatch):
    session, client = _october_session("openai")
    _legacy_kb(monkeypatch)
    assert session._smartpbx_transfer_context is None
    assert not session._is_smartpbx_session()

    await session._process_utterance_bound("Uh, the Steel Cabana, please.")
    await session._process_utterance_bound("I'm a Sri Lankan resident.")

    record = _current_authoritative_rate_record("openai", client)
    assert "rate_per_room_per_night: 14600" in record


async def _assert_stale_runner_cannot_commit(
    monkeypatch, utterance: str, slot: str, *, question: str | None = None,
):
    session, _client = _october_session("openai")
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
    if question is not None:
        _seed_delivered_residency_question(session, question)
    session._active_smartpbx_turn_id = "turn-a"
    stale_task = asyncio.create_task(session._process_utterance_bound(utterance))
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
async def test_stale_runner_cannot_commit_an_aliased_natural_selection(monkeypatch):
    assert rate_catalog.recognize_selected_room(
        "Uh, the Steel Cabana, please."
    ) == "Lagoon View Steel Cabana"
    await _assert_stale_runner_cannot_commit(
        monkeypatch, "Uh, the Steel Cabana, please.", "room_type",
    )


@pytest.mark.asyncio
async def test_stale_runner_cannot_commit_a_terse_awaited_residency(monkeypatch):
    await _assert_stale_runner_cannot_commit(
        monkeypatch, "Resident.", "residency", question=RESIDENCY_ASKS[0],
    )
