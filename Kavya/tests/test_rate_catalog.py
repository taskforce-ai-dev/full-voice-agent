"""Deterministic rate resolution must precede semantic knowledge retrieval.

These tests define the rate-catalog boundary used by the shared MediaStreamSession
path.  They intentionally exercise the session's real slot and provider seams;
no model response is mocked to manufacture a price.
"""

from __future__ import annotations

from datetime import date
import inspect

import pytest

import rate_catalog
import server
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


def test_explicit_residency_persists_and_injects_one_authoritative_rate_for_every_provider():
    session = server.MediaStreamSession(websocket=None, lang="en", media_transport=None)
    session._capture_booking_slots(
        "check_availability",
        {
            "check_in": "2026-09-26",
            "check_out": "2026-09-29",
            "room_type": "Mount Monarch Chalet",
        },
    )

    session._capture_explicit_residency("Sri Lankan resident")

    assert session._booking_slots["residency"] == "resident"
    prompt = session._active_system_prompt()
    assert "AUTHORITATIVE RATE RECORD" in prompt
    assert "Mount Monarch Chalet" in prompt
    assert "LKR" in prompt
    assert "315000" in prompt
    assert "1400" not in prompt
    assert "368000" not in prompt

    # The three direct SmartPBX runners must use the shared authoritative prompt,
    # not independently re-resolve a KB answer.
    for runner_name in ("_run_llm", "_run_llm_gemini", "_run_llm_claude"):
        source = inspect.getsource(getattr(server.MediaStreamSession, runner_name))
        assert "_active_system_prompt()" in source or "_booking_slots_note()" in source

    # A deterministic rate turn deliberately excludes a contradictory KB price
    # from the model request.  Descriptive KB retrieval remains available when
    # no complete rate record exists.
    message = session._compose_turn_user_message(
        "Sri Lankan resident",
        "Mount Monarch Chalet costs 1,400 US dollars in April.",
    )
    assert "1,400" not in message
    assert "US dollars" not in message
    assert session._compose_turn_user_message("tell me about the pool", "pool details") == (
        "[Reference context: pool details]\n\nGuest: tell me about the pool"
    )
