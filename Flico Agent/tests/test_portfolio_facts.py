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


def test_cheapest_and_dearest_are_computed_not_left_to_the_llm():
    """Asked in Sinhala for the cheapest listing, Fiona named a Rs 150,000 unit
    when a Rs 130,000 one exists -- she was scanning 12 listings and comparing.
    Aggregations get computed here, like the deposit figures."""
    out = portfolio_facts(_props())
    lo = min(TRUTH, key=lambda r: r["rent"])
    hi = max(TRUTH, key=lambda r: r["rent"])
    assert f"CHEAPEST listing: {lo['id']} at Rs {lo['rent']:,}" in out
    assert f"MOST EXPENSIVE: {hi['id']} at Rs {hi['rent']:,}" in out


def test_cheapest_tracks_a_changed_inventory():
    rows = TRUTH + [{"id": "P99", "type": "house", "zone": 4, "beds": 1,
                     "baths": 1, "rent": 50000, "furnishing": "unfurnished",
                     "sqft": 400}]
    out = portfolio_facts(_props(rows))
    assert "CHEAPEST listing: P99 at Rs 50,000" in out


def test_no_cheapest_claim_for_a_single_listing():
    """One listing is not 'the cheapest and the most expensive' -- that reads as
    two options to a caller."""
    out = portfolio_facts(_props(TRUTH[:1]))
    assert "CHEAPEST" not in out


def test_on_request_listings_are_not_ranked_by_price():
    props = _props()
    for p in props:
        p.rent_on_request, p.rent_amount = True, None
    assert "CHEAPEST" not in portfolio_facts(props)


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


def test_agency_name_is_parameterized_not_hardcoded():
    # Rodrigo Realtors is the safety-net default for direct callers, but
    # server.py always passes the active brand's agency explicitly -- a demo
    # brand's prompt must never contain the real client's agency name.
    out = portfolio_facts(_props(), agency="Start Property")
    assert "Start Property" in out
    assert "Rodrigo Realtors" not in out


def test_default_agency_is_still_rodrigo_realtors():
    assert "Rodrigo Realtors" in portfolio_facts(_props())


def test_claims_are_consistent_with_the_live_prose_kb():
    """Ties the generated block to the real KB file via the audited truth table,
    so a KB edit that contradicts the prompt fails CI."""
    out = portfolio_facts(_props())
    beds = sorted({r["beds"] for r in TRUTH})
    for b in beds:
        assert {1: "one", 2: "two", 3: "three"}[b] in out
    # Nothing larger than the real maximum may be claimed as available.
    assert "four-bedroom" not in out and "five-bedroom" not in out
