"""Regression tests for KB chunking.

The previous sliding-window chunker collapsed its forward step to a single
character whenever a paragraph break fell within `overlap` of the window start
— which happens at every ``SECTION HEADER\n----`` underline — exploding the
small Horizon KB into ~550 chunks, ~180 of them pure ``=``/``-`` punctuation.
That shredded the campus table and per-program fee lines so retrieval returned
partial answers ("only Kurunegala", "no course fees"). These tests pin the
paragraph-aware behaviour that replaced it.
"""
from __future__ import annotations

import sys
from pathlib import Path

HORIZON = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HORIZON))

from knowledge_base import chunk_text  # noqa: E402

KB_FILE = HORIZON / "knowledge_docs" / "horizon_info.txt"


def test_empty_and_short():
    assert chunk_text("") == []
    assert chunk_text("   ") == []
    assert chunk_text("Hello world.") == ["Hello world."]


def test_no_pathological_explosion_on_underlines():
    """A header + long underline rule + body must not explode into slivers."""
    text = (
        "SECTION ONE\n"
        "-----------\n\n"
        + ("word " * 300).strip()
        + "\n\nSECTION TWO\n-----------\n\n"
        + ("more " * 300).strip()
    )
    chunks = chunk_text(text, chunk_size=500, overlap=50)
    # Old code produced hundreds; a sane packer produces a handful.
    assert len(chunks) < 20


def test_no_pure_punctuation_chunks():
    chunks = chunk_text(KB_FILE.read_text(encoding="utf-8"))
    for c in chunks:
        assert set(c.strip()) - set("=- \n"), f"pure-rule chunk leaked: {c!r}"


def test_chunks_respect_size_bound():
    chunks = chunk_text(KB_FILE.read_text(encoding="utf-8"), chunk_size=500)
    # Blocks are packed up to chunk_size and long blocks are split under it, so
    # nothing should be wildly over the bound.
    assert all(len(c) <= 500 for c in chunks)


def test_kb_produces_reasonable_chunk_count():
    chunks = chunk_text(KB_FILE.read_text(encoding="utf-8"))
    # ~7 KB doc → tens of chunks, never hundreds.
    assert 15 <= len(chunks) <= 80


def test_campus_table_stays_whole():
    """All three campuses must land in one chunk so "what are your branches"
    surfaces every location, not just Kurunegala."""
    chunks = chunk_text(KB_FILE.read_text(encoding="utf-8"))
    table = [c for c in chunks if "City Academy (Head Office)" in c]
    assert len(table) == 1
    c = table[0]
    assert "Colombo" in c and "Ratmalana" in c and "Kurunegala" in c


def test_each_program_fee_sits_with_its_program():
    """Each diploma's fee sentence must co-locate with the program name so a
    per-program fee question retrieves the fee."""
    chunks = chunk_text(KB_FILE.read_text(encoding="utf-8"))
    for prog in (
        "Airline Cabin Crew",
        "Airport Ground Operations",
        "Airline Ticketing, Reservations and Marketing",
        "Air Cargo and Logistics",
    ):
        needle = f"course fee for the Horizon Diploma in {prog}"
        assert any(needle in c and "LKR" in c for c in chunks), prog
