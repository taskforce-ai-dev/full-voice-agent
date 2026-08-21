"""Production-shaped regression contracts for natural room/residency rate turns.

Three live calls were lost because Kavya could not turn an ordinary sentence into
a rate she already knows deterministically:

1. "Uh, the Sunrise Vista, please."            -> "I'm a Sri Lankan resident."
2. "I'd like to go with the Mount Luxe Chalet." -> "Resident."
3. "I'll go with the Mount Monarch Chalet. Thank you." -> "I am a Sri Lankan resident."

None of the three produced a rate.  Production probes prove the rates exist for
those October stays, so the failure is entirely in recognition: the exact room
recognizer returns ``None`` for all three selections, the residency recognizer
returns ``None`` for a terse "Resident.", and the room-rate classifier returns
``NONE`` for the natural local/October wordings guests actually use.

These tests define the recognition contract that must hold *before* any of it is
implemented.  They assert behaviour — which room is captured, which residency is
persisted, which exact record reaches each provider runner — rather than the
wording of the production source.  Semantic knowledge retrieval is deliberately
never allowed to supply the answer: legacy KB retrieval for these queries
returns foreign-rate and booking prose, which is exactly how the wrong numbers
reached callers before the deterministic catalog existed.
"""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

import pytest

import rate_catalog
import server
from yanolja_service import DEMO_NIGHTLY_RATE_USD


# ── Canonical October stay used by every incident reproduction ────────────────
OCTOBER_CHECK_IN = "2026-10-05"
OCTOBER_CHECK_OUT = "2026-10-08"

# Production probe results for that stay (resident, LKR, per room per night).
OCTOBER_RESIDENT_RATE_LKR = {
    "Forest Escape Suite": 158000,
    "Eco Harmony Suite": 180000,
    "Sunrise Vista Premium Suite": 214000,
    "Mount Luxe Chalet": 259000,
    "Mount Monarch Chalet": 315000,
}

# The prose legacy semantic retrieval actually returned for these queries.
LEGACY_KB_PROSE = (
    "LEGACY_KB: foreign rates from 700 USD to 1400 USD per room per night, "
    "and the booking cancellation policy."
)

RESIDENCY_ASKS = (
    "Are you a Sri Lankan resident, or a foreign guest?",
    "May I know if you are a Sri Lankan resident or a foreign guest?",
)

NON_RESIDENCY_ASKS = (
    "Which room would you like?",
    "May I have your name, please?",
)


# ── rate_catalog: safe aliases, and nothing looser ───────────────────────────

@pytest.mark.parametrize(
    ("alias", "canonical"),
    (
        ("Sunrise Vista", "Sunrise Vista Premium Suite"),
        ("Mount Luxe", "Mount Luxe Chalet"),
        ("Mount Monarch", "Mount Monarch Chalet"),
    ),
)
def test_safe_room_aliases_resolve_to_their_canonical_room(alias, canonical):
    assert rate_catalog.recognize_selected_room(alias) == canonical


@pytest.mark.parametrize(
    ("alias", "canonical"),
    (
        ("The Sunrise Vista, please.", "Sunrise Vista Premium Suite"),
        ("I'll go with the Mount Luxe.", "Mount Luxe Chalet"),
        ("Go with Mount Monarch.", "Mount Monarch Chalet"),
    ),
)
def test_safe_aliases_work_inside_natural_selection_sentences(alias, canonical):
    assert rate_catalog.recognize_selected_room(alias) == canonical


@pytest.mark.parametrize(
    "fragment",
    (
        "Vista",
        "Sunrise",
        "Mount",
        "Luxe",
        "Monarch",
        "Chalet",
        "Premium Suite",
        "The Mount Chalet, please.",
        "I'll go with the Vista.",
        "I'll go with the Mount.",
    ),
)
def test_room_aliases_never_become_arbitrary_fuzzy_matching(fragment):
    assert rate_catalog.recognize_selected_room(fragment) is None


@pytest.mark.parametrize(
    "utterance",
    (
        "Mount Luxe or Mount Monarch?",
        "I'll go with the Mount Luxe or the Mount Monarch.",
        "Is Sunrise Vista or Mount Luxe available?",
    ),
)
def test_two_aliased_rooms_in_one_utterance_stay_ambiguous(utterance):
    assert rate_catalog.recognize_selected_room(utterance) is None


# ── rate_catalog: natural selection grammar with bounded filler/punctuation ──

@pytest.mark.parametrize(
    ("utterance", "expected"),
    (
        # The three production incidents, verbatim.
        ("Uh, the Sunrise Vista, please.", "Sunrise Vista Premium Suite"),
        ("I'd like to go with the Mount Luxe Chalet.", "Mount Luxe Chalet"),
        ("I'll go with the Mount Monarch Chalet. Thank you.", "Mount Monarch Chalet"),
        # The same three shapes over the remaining canonical rooms.
        ("The Forest Escape Suite, please.", "Forest Escape Suite"),
        ("Um, the Eco Harmony Suite please.", "Eco Harmony Suite"),
        ("Go with the Forest Escape Suite.", "Forest Escape Suite"),
        ("I'll go with the Eco Harmony Suite.", "Eco Harmony Suite"),
        ("I'd like to go with the Sunrise Vista Premium Suite.", "Sunrise Vista Premium Suite"),
        ("I'll go with the Mount Luxe Chalet, thanks.", "Mount Luxe Chalet"),
    ),
)
def test_natural_room_selection_forms_capture_the_canonical_room(utterance, expected):
    assert rate_catalog.recognize_selected_room(utterance) == expected


@pytest.mark.parametrize(
    "utterance",
    (
        "I would like to know what dinner costs at the Mount Luxe Chalet.",
        "Is dinner available at the Sunrise Vista?",
        "The dinner, please.",
        "Go with the spa package, please.",
        "Please go with whatever you recommend.",
        "I'll go with the earlier date. Thank you.",
        "How much is the Mount Monarch Chalet's dinner, please?",
    ),
)
def test_natural_selection_grammar_stays_bounded(utterance):
    assert rate_catalog.recognize_selected_room(utterance) is None


# ── rate_catalog: terse residency only while the session awaits it ────────────

@pytest.mark.parametrize("utterance", ("Resident.", "Resident", "resident"))
def test_terse_resident_is_recognized_only_while_awaiting_residency(utterance):
    assert rate_catalog.recognize_residency(
        utterance, awaiting_residency=True
    ) == "resident"


@pytest.mark.parametrize("utterance", ("Resident.", "Resident", "resident"))
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
def test_awaiting_residency_does_not_unlock_ambiguous_or_third_party_statements(
    utterance,
):
    assert rate_catalog.recognize_residency(
        utterance, awaiting_residency=True
    ) is None


@pytest.mark.parametrize(
    ("utterance", "expected"),
    (
        ("I am a Sri Lankan resident.", "resident"),
        ("I'm not local", "foreign"),
        ("foreign guest", "foreign"),
    ),
)
def test_awaiting_residency_preserves_the_existing_explicit_statements(
    utterance, expected
):
    assert rate_catalog.recognize_residency(utterance) == expected
    assert rate_catalog.recognize_residency(
        utterance, awaiting_residency=True
    ) == expected


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
        ("rate for Forest Escape Suite in October", ("Forest Escape Suite",)),
        (
            "What is the rate for the Mount Luxe Chalet in October?",
            ("Mount Luxe Chalet",),
        ),
        ("rate for Sunrise Vista in October", ("Sunrise Vista Premium Suite",)),
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
    record = resolution.authoritative_context()
    assert f"rate_per_room_per_night: {expected}" in record
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


def _request_text(provider: str, client) -> str:
    """Return the whole recorded request.

    The Claude runner hands ``messages.stream()`` the live history list, so the
    assistant reply is appended to the very object the recorder captured.  Any
    assertion that a marker is PRESENT must therefore scan the whole request
    rather than index the last message, or it reads the reply back instead of
    the guest turn.
    """
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
async def test_incident_sunrise_vista_then_residency_quotes_the_october_rate(
    monkeypatch, provider
):
    session, client = _october_session(provider)
    queries = _legacy_kb(monkeypatch)

    await session._process_utterance_bound("Uh, the Sunrise Vista, please.")
    await session._process_utterance_bound("I'm a Sri Lankan resident.")

    assert session._booking_slots["room_type"] == "Sunrise Vista Premium Suite"
    assert session._booking_slots["residency"] == "resident"
    record = _current_authoritative_rate_record(provider, client)
    assert "rate_per_room_per_night: 214000" in record
    assert "no_quote" not in record
    _assert_no_kb_dependency(record, "I'm a Sri Lankan resident.", queries)


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ("openai", "gemini", "claude"))
@pytest.mark.parametrize("residency_ask", RESIDENCY_ASKS)
async def test_incident_mount_luxe_then_terse_resident_quotes_the_october_rate(
    monkeypatch, provider, residency_ask
):
    session, client = _october_session(provider)
    queries = _legacy_kb(monkeypatch)

    await session._process_utterance_bound("I'd like to go with the Mount Luxe Chalet.")
    session._delivered_sentences = [residency_ask]
    await session._process_utterance_bound("Resident.")

    assert session._booking_slots["room_type"] == "Mount Luxe Chalet"
    assert session._booking_slots["residency"] == "resident"
    record = _current_authoritative_rate_record(provider, client)
    assert "rate_per_room_per_night: 259000" in record
    assert "no_quote" not in record
    _assert_no_kb_dependency(record, "Resident.", queries)


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ("openai", "gemini", "claude"))
async def test_incident_mount_monarch_with_trailing_thanks_quotes_the_october_rate(
    monkeypatch, provider
):
    session, client = _october_session(provider)
    queries = _legacy_kb(monkeypatch)

    await session._process_utterance_bound(
        "I'll go with the Mount Monarch Chalet. Thank you."
    )
    await session._process_utterance_bound("I am a Sri Lankan resident.")

    assert session._booking_slots["room_type"] == "Mount Monarch Chalet"
    record = _current_authoritative_rate_record(provider, client)
    assert "rate_per_room_per_night: 315000" in record
    assert "no_quote" not in record
    _assert_no_kb_dependency(record, "I am a Sri Lankan resident.", queries)


# ── A terse "Resident." is inert unless the session actually asked ───────────

@pytest.mark.asyncio
@pytest.mark.parametrize("non_residency_ask", NON_RESIDENCY_ASKS)
async def test_terse_resident_is_ignored_when_the_session_is_not_awaiting_residency(
    monkeypatch, non_residency_ask
):
    """A bare session — nothing asked, no room, no dates — must stay inert.

    Deliberately scoped to a session that is unambiguously NOT awaiting
    residency under any arming rule.  Whether a slot state of "room and dates
    captured, residency the only gap" also counts as an explicit await is a
    separate question, settled by the pending-slot contract in
    ``test_rate_catalog.py``; this test must not pre-empt it.
    """
    session, client = _provider_session("openai")
    _legacy_kb(monkeypatch)
    session._delivered_sentences = [non_residency_ask]

    await session._process_utterance_bound("Resident.")

    assert "residency" not in session._booking_slots
    assert "rate_per_room_per_night" not in _request_text("openai", client)


# ── The rate is quoted before identity capture, never handed to a human ──────

@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ("openai", "gemini", "claude"))
async def test_known_october_rate_is_quoted_before_any_name_or_phone_is_captured(
    monkeypatch, provider
):
    session, client = _october_session(provider)
    _legacy_kb(monkeypatch)

    await session._process_utterance_bound("Uh, the Sunrise Vista, please.")
    await session._process_utterance_bound("I'm a Sri Lankan resident.")

    record = _current_authoritative_rate_record(provider, client)
    assert "rate_per_room_per_night: 214000" in record
    assert "no_quote" not in record
    assert "human confirmation" not in record
    assert "guest_name" not in session._booking_slots
    assert "guest_phone" not in session._booking_slots


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ("openai", "gemini", "claude"))
@pytest.mark.parametrize(
    ("room", "utterance"),
    (
        ("Forest Escape Suite", "The Forest Escape Suite, please."),
        ("Eco Harmony Suite", "I'll go with the Eco Harmony Suite."),
        ("Sunrise Vista Premium Suite", "Uh, the Sunrise Vista, please."),
        ("Mount Luxe Chalet", "I'd like to go with the Mount Luxe Chalet."),
        ("Mount Monarch Chalet", "I'll go with the Mount Monarch Chalet. Thank you."),
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

    turn_text = _request_text(provider, client)
    record = _current_authoritative_rate_record(provider, client)
    assert "AUTHORITATIVE RATE RECORD" in record
    assert "status: no_quote" in record
    assert "reason: unknown_room" in record
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
        provider, room_type="Mount Monarch Chalet", residency="resident",
    )
    _legacy_kb(monkeypatch)

    await session._process_utterance_bound("What are the local rates for October?")

    record = _current_authoritative_rate_record(provider, client)
    assert "rate_per_room_per_night: 315000" in record
    assert "no_quote" not in record


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ("openai", "gemini", "claude"))
async def test_named_room_month_rate_request_quotes_that_room(monkeypatch, provider):
    session, client = _october_session(provider, residency="resident")
    _legacy_kb(monkeypatch)

    await session._process_utterance_bound("rate for Forest Escape Suite in October")

    record = _current_authoritative_rate_record(provider, client)
    assert "rate_per_room_per_night: 158000" in record
    assert "no_quote" not in record


# ── Preserved behaviour: foreign USD, peaks, mixed periods, unknown rooms ────

@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ("openai", "gemini", "claude"))
async def test_natural_selection_then_foreign_residency_keeps_the_usd_rate(
    monkeypatch, provider
):
    session, client = _october_session(provider)
    _legacy_kb(monkeypatch)

    await session._process_utterance_bound("I'd like to go with the Mount Luxe Chalet.")
    await session._process_utterance_bound("I am a foreign guest.")

    record = _current_authoritative_rate_record(provider, client)
    expected = DEMO_NIGHTLY_RATE_USD["Mount Luxe Chalet"]
    assert "currency: USD" in record
    assert f"rate_per_room_per_night: {expected}" in record


@pytest.mark.parametrize(
    ("check_in", "check_out", "expected"),
    (
        ("2026-04-10", "2026-04-12", 303000),
        ("2026-12-20", "2026-12-22", 303000),
    ),
)
def test_april_and_december_peak_resident_rates_are_preserved(
    check_in, check_out, expected
):
    resolution = rate_catalog.resolve_rate(
        room="Mount Luxe Chalet",
        residency="resident",
        check_in=check_in,
        check_out=check_out,
    )

    assert resolution.is_quotable
    assert resolution.currency == "LKR"
    assert resolution.nightly_rate == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ("openai", "gemini", "claude"))
async def test_natural_selection_across_seasons_still_refuses_to_quote(
    monkeypatch, provider
):
    session, client = _provider_session(provider)
    session._booking_slots.update({
        "check_in": "2026-03-31", "check_out": "2026-04-02",
    })
    _legacy_kb(monkeypatch)

    await session._process_utterance_bound(
        "I'll go with the Mount Monarch Chalet. Thank you."
    )
    await session._process_utterance_bound("I am a Sri Lankan resident.")

    record = _current_authoritative_rate_record(provider, client)
    assert "status: no_quote" in record
    assert "reason: mixed_period" in record
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

    turn_text = _request_text(provider, client)
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
        provider, room_type="Mount Monarch Chalet", residency="resident",
    )
    _legacy_kb(monkeypatch)

    await session._process_utterance_bound(utterance)

    turn_text = _request_text(provider, client)
    assert "LEGACY_KB" in turn_text
    assert "AUTHORITATIVE RATE RECORD" not in turn_text


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ("openai", "gemini", "claude"))
@pytest.mark.parametrize(
    ("utterance", "expected_room"),
    (
        ("Uh, the Sunrise Vista, please.", "Sunrise Vista Premium Suite"),
        ("I'd like to go with the Mount Luxe Chalet.", "Mount Luxe Chalet"),
        ("I'll go with the Mount Monarch Chalet. Thank you.", "Mount Monarch Chalet"),
    ),
)
async def test_the_selection_turn_itself_never_quotes_a_rate(
    monkeypatch, provider, utterance, expected_room
):
    session, client = _october_session(provider)
    _legacy_kb(monkeypatch)

    await session._process_utterance_bound(utterance)

    turn_text = _request_text(provider, client)
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

    await session._process_utterance_bound("Uh, the Sunrise Vista, please.")
    await session._process_utterance_bound("I'm a Sri Lankan resident.")

    record = _current_authoritative_rate_record("openai", client)
    assert "rate_per_room_per_night: 214000" in record


async def _assert_stale_runner_cannot_commit(
    monkeypatch, utterance: str, slot: str, *, delivered: list[str] | None = None,
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
    if delivered is not None:
        session._delivered_sentences = list(delivered)
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
        "Uh, the Sunrise Vista, please."
    ) == "Sunrise Vista Premium Suite"
    await _assert_stale_runner_cannot_commit(
        monkeypatch, "Uh, the Sunrise Vista, please.", "room_type",
    )


@pytest.mark.asyncio
async def test_stale_runner_cannot_commit_a_terse_awaited_residency(monkeypatch):
    assert rate_catalog.recognize_residency(
        "Resident.", awaiting_residency=True
    ) == "resident"
    await _assert_stale_runner_cannot_commit(
        monkeypatch, "Resident.", "residency", delivered=[RESIDENCY_ASKS[0]],
    )
