"""The prompt's portfolio claims must be derived from the inventory, never stale.

The prompt once hard-coded "our apartments start at three bedrooms" and "we do
not currently have any one-bedroom or two-bedroom apartments". When the inventory
was swapped, Fiona confidently denied the very listings retrieval handed her.
These tests prove the generated block tracks the data, and -- crucially -- that
it says nothing at all when it cannot know.
"""
import re

from kb.facts import portfolio_facts
from kb.schema import Property
from tests.truth_table import TRUTH


def _props(rows=None):
    return [Property(id=r["id"], transaction="rent", property_type=r["type"],
                     zone=r["zone"], area="", bedrooms=r["beds"],
                     bathrooms=float(r["baths"]), rent_amount=float(r["rent"]),
                     rent_period="month", furnishing=r["furnishing"],
                     floor_area_sqft=r["sqft"], description=r["id"])
            for r in (rows if rows is not None else TRUTH)]


def test_empty_inventory_makes_no_claims():
    # Saying nothing is safe; asserting something false is not.
    assert portfolio_facts([]) == ""


def test_counts_match_the_inventory():
    out = portfolio_facts(_props())
    assert "12 listing(s)" in out
    assert "6 apartments" in out and "6 houses" in out


def test_bedroom_range_matches_the_inventory():
    out = portfolio_facts(_props())
    assert "largest is two-bedroom" in out
    assert "smallest is one-bedroom" in out


def test_zones_match_the_inventory_exactly():
    out = portfolio_facts(_props())
    m = re.search(r"Areas: Colombo ([^.]+) only", out)
    assert m, out
    claimed = {int(n) for n in re.findall(r"\d+", m.group(1))}
    assert claimed == {r["zone"] for r in TRUTH}


def test_facts_track_a_changed_inventory():
    """The whole point: add a 3-bedroom listing in a new zone and the claims move
    with it. Under the old hard-coded prompt, Fiona would have denied it."""
    rows = TRUTH + [{"id": "P99", "type": "house", "zone": 4, "beds": 3,
                     "baths": 3, "rent": 500000, "furnishing": "furnished",
                     "sqft": 2000}]
    out = portfolio_facts(_props(rows))
    assert "largest is three-bedroom" in out
    assert "13 listing(s)" in out
    m = re.search(r"Areas: Colombo ([^.]+) only", out)
    assert 4 in {int(n) for n in re.findall(r"\d+", m.group(1))}


def test_never_claims_fixed_rent_when_some_are_on_request():
    props = _props()
    props[0].rent_on_request = True
    props[0].rent_amount = None
    out = portfolio_facts(props)
    assert "Every listing has a fixed" not in out
    assert "on request" in out


def test_all_on_request_is_stated_as_such():
    props = _props()
    for p in props:
        p.rent_on_request, p.rent_amount = True, None
    assert "Every listing's rent is quoted on request" in portfolio_facts(props)


def test_claims_are_consistent_with_the_live_prose_kb():
    """Ties the generated block to the real KB file via the audited truth table,
    so a KB edit that contradicts the prompt fails CI."""
    out = portfolio_facts(_props())
    beds = sorted({r["beds"] for r in TRUTH})
    for b in beds:
        assert {1: "one", 2: "two", 3: "three"}[b] in out
    # Nothing larger than the real maximum may be claimed as available.
    assert "four-bedroom" not in out and "five-bedroom" not in out
