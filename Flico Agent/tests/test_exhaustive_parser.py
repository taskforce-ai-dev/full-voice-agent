"""Exhaustive proof of the utterance -> QueryFilters layer.

The grammar is DATA: each fragment declares what it must contribute to the
filters. The test takes the cartesian product and asserts the parse equals the
composed expectation. The five shipped bugs and four latent ones were all
"a dimension nobody decided" -- this file is where the decision gets written
down, so an undecided default becomes a test failure instead of a phone call.

Includes the decoys that must contribute NOTHING: occupancy ("four people"),
sizes ("under 1000 square feet"), and counts ("more than 2 bedrooms"). Each of
those was a real bug.
"""
import itertools

import pytest

from kb.query_parser import QueryParser

# (fragment, expected filter contribution)
TYPE_FRAGMENTS = [
    ("", {}),
    ("apartment", {"property_type": "apartment"}),
    ("apartments", {"property_type": "apartment"}),  # plurals: \bhouse\b misses "houses"
    ("flat", {"property_type": "apartment"}),
    ("flats", {"property_type": "apartment"}),
    ("penthouse", {"property_type": "apartment"}),  # contains "house"
    ("house", {"property_type": "house"}),
    ("houses", {"property_type": "house"}),
    ("townhouse", {"property_type": "house"}),
    ("villa", {"property_type": "house"}),
    ("villas", {"property_type": "house"}),
    ("bungalow", {"property_type": "house"}),
    ("office space", {"property_type": "commercial"}),
    ("warehouse", {"property_type": "commercial"}),  # contains "house"
]

ZONE_FRAGMENTS = [
    ("", {}),
    ("in colombo 7", {"zone": 7}),
    ("in colombo seven", {"zone": 7}),
    ("in colombo-7", {"zone": 7}),
    # STT mishearings that reach the parser on real calls
    ("in columbo 5", {"zone": 5}),
    ("in colombus 5", {"zone": 5}),
    ("in wellawatte", {"zone": 6}),
    ("in cinnamon gardens", {"zone": 7}),
    ("in borella", {"zone": 8}),
    ("in kollupitiya", {"zone": 3}),
    ("in colpetty", {"zone": 3}),
    ("in havelock town", {"zone": 5}),
    ("in havoc town", {"zone": 5}),
    # "Slave Island" contains "land" and "Iceland Residence" does too -- both
    # must stay type-free, not classify as property_type=land.
    ("in slave island", {"zone": 2}),
    ("in union place", {"zone": 2}),
]

BEDROOM_FRAGMENTS = [
    ("", {}),
    ("1 bedroom", {"bedrooms": 1}),
    ("one bedroom", {"bedrooms": 1}),
    ("2 bedroom", {"bedrooms": 2}),
    ("two bedroom", {"bedrooms": 2}),
    ("two bedrooms", {"bedrooms": 2}),
    ("2-bedroom", {"bedrooms": 2}),
    ("2br", {"bedrooms": 2}),
    ("at least 2 bedrooms", {"min_bedrooms": 2}),
    ("2 or more bedrooms", {"min_bedrooms": 2}),
    ("2+ bedrooms", {"min_bedrooms": 2}),
    ("minimum two bedrooms", {"min_bedrooms": 2}),
    # Decoys: occupancy is never a bedroom count.
    ("for four people", {}),
    ("for the two of us", {}),
    ("for a family of five", {}),
]

RENT_FRAGMENTS = [
    ("", {}),
    ("under 300k", {"max_rent": 300000.0, "max_rent_exclusive": True}),
    ("below 300k", {"max_rent": 300000.0, "max_rent_exclusive": True}),
    ("less than 300k", {"max_rent": 300000.0, "max_rent_exclusive": True}),
    ("up to 300k", {"max_rent": 300000.0}),
    ("max 300k", {"max_rent": 300000.0}),
    ("maximum 300k", {"max_rent": 300000.0}),
    ("budget of 300k", {"max_rent": 300000.0}),
    ("budget is 300k", {"max_rent": 300000.0}),
    ("not more than 300k", {"max_rent": 300000.0}),
    ("under rs 300,000", {"max_rent": 300000.0, "max_rent_exclusive": True}),
    ("under 3 lakhs", {"max_rent": 300000.0, "max_rent_exclusive": True}),
    ("over 300k", {"min_rent": 300000.0}),
    ("above 300k", {"min_rent": 300000.0}),
    ("between 300k and 500k", {"min_rent": 300000.0, "max_rent": 500000.0}),
    ("from 300 to 500 thousand", {"min_rent": 300000.0, "max_rent": 500000.0}),
    # Decoys: a size is not money.
    ("under 1000 square feet", {}),
    ("over 2000 sq ft", {}),
    ("under 10 perches", {}),
]

_DEFAULTS = {"property_type": None, "zone": None, "bedrooms": None,
             "min_bedrooms": None, "min_rent": None, "max_rent": None,
             "max_rent_exclusive": False}


def _cases():
    for t, z, b, r in itertools.product(
            TYPE_FRAGMENTS, ZONE_FRAGMENTS, BEDROOM_FRAGMENTS, RENT_FRAGMENTS):
        expected = dict(_DEFAULTS)
        for _, contrib in (t, z, b, r):
            expected.update(contrib)
        utterance = " ".join(p for p in ("i need a", b[0], t[0], z[0], r[0]) if p)
        yield utterance, expected


ALL_CASES = list(_cases())


def test_grammar_exhaustive():
    """Sweeps the whole product in one test -- 38k parametrized cases spend all
    their time in pytest overhead. Collects every failure so one run shows the
    full blast radius of a regression instead of the first case that trips."""
    failures = []
    for utterance, expected in ALL_CASES:
        _, f = QueryParser.parse(utterance)
        got = {k: getattr(f, k) for k in _DEFAULTS}
        if got != expected:
            diff = {k: (got[k], expected[k]) for k in expected if got[k] != expected[k]}
            failures.append(f"{utterance!r}: got/want {diff}")
    assert not failures, (
        f"{len(failures)} of {len(ALL_CASES)} utterances mis-parsed:\n  "
        + "\n  ".join(failures[:25]))


def test_grammar_is_actually_large():
    # Guard against the product silently collapsing to a handful of cases.
    assert len(ALL_CASES) > 5000


def test_no_utterance_sets_both_exact_and_floor_bedrooms():
    for utterance, _ in ALL_CASES:
        _, f = QueryParser.parse(utterance)
        assert not (f.bedrooms is not None and f.min_bedrooms is not None), utterance
