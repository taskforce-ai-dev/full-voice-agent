"""Proves kb.migrate's prose regexes agree with the hand-audited truth table.

One assertion pins two things at once: that the parser reads the prose correctly,
and that the prose still says what a human signed off on. Either drifting fails
CI, which is what stops a KB edit from silently changing the inventory.
"""
import os

from kb.migrate import parse_prose
from tests.truth_table import TRUTH

_KB_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "knowledge_docs", "flico_info.txt")


def _parsed():
    with open(_KB_FILE, encoding="utf-8") as fh:
        rows, skipped, _ = parse_prose(fh.read())
    assert not skipped, f"prose paragraphs failed to parse: {skipped}"
    return {r.id: r for r in rows}


def test_row_set_matches_truth():
    assert sorted(_parsed()) == sorted(r["id"] for r in TRUTH)


def test_every_field_matches_truth():
    parsed = _parsed()
    for want in TRUTH:
        got = parsed[want["id"]]
        assert got.property_type == want["type"], want["id"]
        assert got.zone == want["zone"], want["id"]
        assert got.bedrooms == want["beds"], want["id"]
        assert got.bathrooms == want["baths"], want["id"]
        assert got.rent_amount == want["rent"], want["id"]
        assert got.furnishing == want["furnishing"], want["id"]
        assert got.floor_area_sqft == want["sqft"], want["id"]
        # Not hardcoded "month": P03 is priced per DAY, and price-on-request
        # rows carry no period at all. Quoting a per-day rate as monthly, or
        # inventing a period for a POR listing, are both wrong answers.
        assert got.rent_period == want["period"], want["id"]
        assert got.transaction == "rent", want["id"]


def test_no_listing_is_silently_misfiled():
    # _classify falls through to "apartment"; make sure nothing relies on that.
    for row in _parsed().values():
        assert row.property_type in {"apartment", "house", "commercial", "land"}
        assert row.zone is not None, f"{row.id} has no zone -> zone filter can't work"
        assert row.rent_amount is not None or row.rent_on_request, row.id
