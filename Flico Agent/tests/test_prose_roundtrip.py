"""The OLD parser verifies the NEW generator, then earns its own deletion.

Migration bridge. kb/prose.py now renders a listing's paragraph from its fields,
replacing prose that was hand-authored and regex-parsed back into fields by
kb/migrate.py. Trusting a fresh generator on a live agent because it "looks
right" is exactly how the previous bugs shipped.

So: render every row from knowledge_docs/listings.json through the new template,
feed that output through the EXISTING parse_prose, and assert the round-trip
reproduces the fields we started with AND still matches the audited truth table.
Two independent implementations agreeing on 12 listings is real evidence; one
implementation agreeing with itself is not.

When migrate.py's regex layer is deleted, this file goes with it. Until then it is
the thing that makes the inversion safe.
"""
import json
import os

import pytest

from kb.migrate import parse_prose
from kb.prose import render
from kb.schema import Property
from tests.truth_table import TRUTH

_LISTINGS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "knowledge_docs", "listings.json")


def _rows():
    with open(_LISTINGS, encoding="utf-8") as fh:
        return [Property(**r) for r in json.load(fh)["listings"]]


ROWS = _rows()


def test_source_file_matches_the_audited_truth_table():
    """The structured source and the hand-audited table must agree, or the oracle
    is describing something we no longer serve."""
    by_id = {r.id: r for r in ROWS}
    assert sorted(by_id) == sorted(t["id"] for t in TRUTH)
    for want in TRUTH:
        got = by_id[want["id"]]
        assert got.property_type == want["type"], want["id"]
        assert got.zone == want["zone"], want["id"]
        assert got.bedrooms == want["beds"], want["id"]
        assert got.bathrooms == want["baths"], want["id"]
        assert got.rent_amount == want["rent"], want["id"]
        assert got.furnishing == want["furnishing"], want["id"]
        assert got.floor_area_sqft == want["sqft"], want["id"]


@pytest.mark.parametrize("row", ROWS, ids=[r.id for r in ROWS])
def test_generated_prose_parses_back_to_the_same_fields(row):
    """The round-trip. Every field the old regex parser can see must survive
    render() -> parse_prose() unchanged."""
    prose = render(row)
    parsed, skipped, _ = parse_prose(prose)
    assert not skipped, f"{row.id}: generated prose did not parse: {prose}"
    assert len(parsed) == 1, f"{row.id}: expected exactly one listing"
    got = parsed[0]

    assert got.id == row.id
    assert got.property_type == row.property_type, prose
    assert got.zone == row.zone, prose
    assert got.bedrooms == row.bedrooms, prose
    assert got.bathrooms == row.bathrooms, prose
    assert got.rent_amount == row.rent_amount, prose
    assert got.rent_period == row.rent_period, prose
    assert got.rent_on_request == row.rent_on_request, prose
    assert got.furnishing == row.furnishing, prose
    assert got.floor_area_sqft == row.floor_area_sqft, prose
    assert got.parking == row.parking, prose
    assert got.deposit_months == row.deposit_months, prose
    assert got.advance_months == row.advance_months, prose
    assert got.min_lease_months == row.min_lease_months, prose


@pytest.mark.parametrize("row", ROWS, ids=[r.id for r in ROWS])
def test_prose_carries_the_voice_critical_detail(row):
    """The paragraph is what Amaya speaks from. The formatter emits it verbatim,
    so anything missing here is invisible to the caller."""
    prose = render(row)
    assert f"(Ref: {row.id})" in prose
    if row.street:
        assert row.street in prose, "street lost -- callers ask 'where is it?'"
    for feature in row.key_features:
        assert feature in prose, f"feature lost: {feature}"
    if row.rent_amount:
        assert f"Rs {int(row.rent_amount):,}" in prose
        assert f"per {row.rent_period}" in prose, "rent period must never be implied"
    if row.commentary:
        assert row.commentary.strip() in prose


def test_rent_is_spelled_out_for_the_voice():
    """The TTS reads this aloud and the Sinhala prompt forbids digits, so the
    words are not decoration."""
    p = next(r for r in ROWS if r.id == "P51")
    assert "one hundred and eighty thousand rupees" in render(p)


def test_on_request_rent_is_stated_not_omitted():
    p = ROWS[0].model_copy(update={"rent_amount": None, "rent_on_request": True,
                                   "rent_period": None})
    prose = render(p)
    assert "on request" in prose.lower()
    parsed, _, _ = parse_prose(prose)
    assert parsed[0].rent_on_request is True
    assert parsed[0].rent_amount is None
