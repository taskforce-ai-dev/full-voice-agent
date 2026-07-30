"""Acceptance tests: the live KB must reproduce the real 60-row agency
portfolio's lookup behaviour exactly, end to end through kb.retrieve().

These run against the real prose in knowledge_docs/flico_info.txt (parsed by
kb.migrate.parse_prose, the same path production's fallback uses), so they
fail loudly if the KB content and the retrieval logic ever drift apart.

Formerly test_demo_portfolio_lookups.py, written against a 12-row synthetic
demo portfolio (P51-P62). That portfolio was replaced by the real 60-row
agency inventory (P02-P62: 39 apartments, 14 houses, 7 commercial/office;
1-5 bedrooms; Colombo zones 1,2,3,4,5,6,7,8,10; 24 of 60 price-on-request;
P03 priced per DAY) -- renamed to match, and every expectation below is
DERIVED from tests/truth_table.py + tests/oracle.py rather than a second
hand-maintained copy of 60 rows, so it cannot drift the way the old
hardcoded ID lists did. What still can't be derived -- which utterance maps
to which filters -- is pinned by hand against kb/query_parser.py and noted
per test.
"""
import os
import re

import pytest

from kb.migrate import parse_prose
from tests.conftest import build_kb
from tests.oracle import expected_ids
from tests.truth_table import ALL_IDS, BEDROOMS, TRUTH, ZONES

_KB_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "knowledge_docs", "flico_info.txt")

_NUM_WORD = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five"}


@pytest.fixture(scope="module")
def kb(tmp_path_factory):
    with open(_KB_FILE, encoding="utf-8") as fh:
        rows, skipped, _ = parse_prose(fh.read())
    assert not skipped, f"KB prose failed to parse: {skipped}"
    return build_kb(tmp_path_factory.mktemp("kb") / "portfolio.db", rows)


def _refs(kb, query):
    # NO n_results: production (server.py) calls retrieve_context(text, sticky=...)
    # and takes the default. These tests once passed at n_results=12 while prod ran
    # at 6 and silently showed 6 of the 9 "under 300k" matches -- certifying a
    # configuration prod never executed. Always assert at production parameters.
    return sorted(set(re.findall(r"\[(P\d+)\]", kb.retrieve(query, sticky={}))))


def test_all_sixty_rows_present(kb):
    assert kb.get_count() == len(TRUTH) == 60


# ---------------------------------------------------------------------------
# Exact bedroom matching (never a floor) across every type/count the real
# portfolio actually has. Generated from the truth table so it covers all of
# apartment x {1,2,3,4} and house x {1,2,3,4,5} -- not a hand-picked diagonal.
# (5-bedroom apartments don't exist, so that combination is excluded: an
# empty exact filter would trip the relaxation ladder, which is tested
# separately below.)
# ---------------------------------------------------------------------------
_BED_TYPE_CASES = [
    (ptype, beds)
    for ptype in ("apartment", "house")
    for beds in BEDROOMS
    if expected_ids(TRUTH, property_type=ptype, bedrooms=beds)
]


@pytest.mark.parametrize("ptype,beds", _BED_TYPE_CASES)
def test_bedroom_count_is_exact_not_a_floor(kb, ptype, beds):
    query = f"a {_NUM_WORD[beds]} bedroom {ptype}"
    assert _refs(kb, query) == expected_ids(TRUTH, property_type=ptype, bedrooms=beds)


def test_bedroom_floor_is_a_floor_not_exact(kb):
    # Contrast case for the exact-match sweep above: "at least three bedrooms"
    # must pick up every count from 3 up to the portfolio max (5), unlike a
    # plain "three bedroom" which would be exact.
    assert _refs(kb, "at least three bedrooms") == expected_ids(TRUTH, min_bedrooms=3)


def test_bedroom_filter_never_returns_commercial_rows(kb):
    # Commercial rows state no bedroom count at all (beds=None in the truth
    # table). A bedroom filter -- even with no property_type stated -- must
    # never match one. The old 12-row demo portfolio had no commercial rows,
    # so this case never existed before.
    refs = _refs(kb, "somewhere with two bedrooms")
    assert refs == expected_ids(TRUTH, bedrooms=2)
    commercial_ids = {r["id"] for r in TRUTH if r["type"] == "commercial"}
    assert not (commercial_ids & set(refs))


@pytest.mark.parametrize("zone", ZONES)
def test_by_colombo_zone(kb, zone):
    assert _refs(kb, f"anything in Colombo {zone}") == expected_ids(TRUTH, zone=zone)


def test_by_price_under_300k_is_exclusive(kb):
    # P56 sits exactly at Rs 300,000 -- "under" must exclude it.
    refs = _refs(kb, "anything under 300k")
    assert refs == expected_ids(TRUTH, max_rent=300000, max_rent_exclusive=True)
    assert "P56" not in refs


def test_by_price_up_to_300k_is_inclusive(kb):
    # "up to", unlike "under", includes the boundary listing.
    refs = _refs(kb, "anything up to 300k")
    assert refs == expected_ids(TRUTH, max_rent=300000, max_rent_exclusive=False)
    assert "P56" in refs


def test_price_ceiling_includes_price_on_request(kb):
    # New territory the old, fully-priced 12-row portfolio never exercised:
    # 24 of 60 rows are price-on-request. The shipped SQL is
    # `rent_amount <= ? OR rent_amount IS NULL`, so a budget CEILING must
    # still surface them (the caller is told to ask, not shown nothing).
    refs = _refs(kb, "anything under 300k")
    por_ids = {r["id"] for r in TRUTH if r["rent"] is None}
    assert por_ids & set(refs), "expected at least one price-on-request row under a ceiling"
    assert refs == expected_ids(TRUTH, max_rent=300000, max_rent_exclusive=True)


def test_price_floor_excludes_price_on_request(kb):
    # The documented asymmetry, encoded as-is (not fixed here): min_rent
    # EXCLUDES price-on-request rows, unlike max_rent. A caller filtering for
    # premium stock ("over a million") only sees rows PROVEN to qualify.
    refs = _refs(kb, "anything over 1 million")
    assert refs == expected_ids(TRUTH, min_rent=1_000_000)
    por_ids = {r["id"] for r in TRUTH if r["rent"] is None}
    assert not (por_ids & set(refs))


def test_by_price_300k_to_500k(kb):
    assert _refs(kb, "between 300k and 500k") == expected_ids(
        TRUTH, min_rent=300000, max_rent=500000)


@pytest.mark.parametrize("query,constraints", [
    ("a two bedroom apartment in Colombo 2",
     dict(property_type="apartment", zone=2, bedrooms=2)),
    ("a two bedroom house in Colombo 7",
     dict(property_type="house", zone=7, bedrooms=2)),
    ("a one bedroom in Colombo 6",
     dict(zone=6, bedrooms=1)),
    ("a one bedroom apartment under 200k",
     dict(property_type="apartment", bedrooms=1, max_rent=200000, max_rent_exclusive=True)),
])
def test_combined_constraints(kb, query, constraints):
    assert _refs(kb, query) == expected_ids(TRUTH, **constraints)


def test_requested_type_is_honoured_never_drifts(kb):
    # Colombo 1 is apartment-only in the real portfolio (no houses, no
    # commercial) -- asking for a house there must not silently hand back an
    # apartment without saying it is a different type.
    out = kb.retrieve("a house in Colombo 1", sticky={})
    assert "DIFFERENT property type" in out
    assert sorted(set(re.findall(r"\[(P\d+)\]", out))) == expected_ids(TRUTH, zone=1)


def test_relaxation_ladder_drops_rent_before_bedrooms_or_type(kb):
    # No 1-bedroom apartment is under Rs 100,000 -- the ladder must relax
    # ONLY rent (bedrooms=1 and type=apartment both still hold for every
    # returned row) and say so, without ever claiming a bedroom or type
    # mismatch that isn't real.
    out = kb.retrieve("a one bedroom apartment under 100k", sticky={})
    assert "MORE than they asked" in out
    assert "DIFFERENT number of bedrooms" not in out
    assert "DIFFERENT property type" not in out
    ids = sorted(set(re.findall(r"\[(P\d+)\]", out)))
    assert ids == expected_ids(TRUTH, property_type="apartment", bedrooms=1)


def test_relaxation_ladder_drops_bedrooms_before_type(kb):
    # No 5-bedroom apartment exists anywhere in the portfolio -- the ladder
    # must relax bedrooms only (every returned row is still an apartment),
    # never silently substitute a house.
    out = kb.retrieve("a five bedroom apartment", sticky={})
    assert "DIFFERENT number of bedrooms" in out
    assert "DIFFERENT property type" not in out
    ids = sorted(set(re.findall(r"\[(P\d+)\]", out)))
    assert ids == expected_ids(TRUTH, property_type="apartment")


def test_relaxation_ladder_cascades_and_zone_is_never_dropped(kb):
    # Nothing in Colombo 1 is a 5-bedroom house under Rs 50,000 -- rent, then
    # bedrooms, then property_type must ALL be relaxed in turn (Colombo 1 has
    # apartments only), but the zone constraint itself is never dropped: the
    # relaxation ladder (kb/engine.py::_RELAXATIONS) has no zone entry.
    out = kb.retrieve("a five bedroom house in Colombo 1 under 50k", sticky={})
    assert "MORE than they asked" in out
    assert "DIFFERENT number of bedrooms" in out
    assert "DIFFERENT property type" in out
    ids = sorted(set(re.findall(r"\[(P\d+)\]", out)))
    assert ids == expected_ids(TRUTH, zone=1)


def test_occupancy_is_not_a_bedroom_filter(kb):
    # "N people/family" must never become a bedroom-count filter -- every
    # phrasing below must return the complete, unfiltered inventory (60 rows).
    for query in ("somewhere for four people", "a place for four people",
                  "a family of four"):
        assert _refs(kb, query) == ALL_IDS


def test_per_day_listing_is_labelled_per_day_not_per_month(kb):
    # P03 (Colombo 4 -- the only listing in that zone) is priced Rs 15,000
    # PER DAY. Nothing about retrieval may present that as a monthly figure:
    # the prose must self-label the period so the LLM (and anything scanning
    # this context for "cheapest") can never mistake it for a rate an order
    # of magnitude cheaper than every genuinely monthly listing.
    out = kb.retrieve("an apartment in Colombo 4", sticky={})
    assert "[P03]" in out
    assert expected_ids(TRUTH, property_type="apartment", zone=4) == ["P03"]
    assert "per day" in out
    assert "per month" not in out


def test_ranking_never_truncates_the_matched_set(kb):
    # Historical bug: n_results defaulted to 6 in production while the SQL
    # filter matched 9 rows for "under 300k" in the old 12-row portfolio,
    # silently hiding qualifying listings. n_results=None must return the
    # WHOLE matched set -- ranking may only ORDER it. At 60 rows the matched
    # set for this query is far larger (40+), making truncation obvious if
    # it ever regresses.
    refs = _refs(kb, "anything under 300k")
    expected = expected_ids(TRUTH, max_rent=300000, max_rent_exclusive=True)
    assert len(refs) == len(expected)
    assert len(expected) > 10
    assert refs == expected
