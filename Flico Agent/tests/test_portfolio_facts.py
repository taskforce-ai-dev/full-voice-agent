"""The prompt's portfolio claims must be derived from the inventory, never stale.

The prompt once hard-coded "our apartments start at three bedrooms" and "we do
not currently have any one-bedroom or two-bedroom apartments". When the inventory
was swapped, Amaya confidently denied the very listings retrieval handed her.
These tests prove the generated block tracks the data, and -- crucially -- that
it says nothing at all when it cannot know.

The 2026-07-30 portfolio (60 rows) brings real nulls: 24 price-on-request rents,
7 commercial rows with no bedroom count, one per-DAY price (P03). The facts
block must stay literally true across all of them.
"""
import re

from kb.facts import portfolio_facts
from tests.conftest import truth_property
from tests.truth_table import (AVAILABLE_NOT_NOW, ADVANCE_MONTHS,
                               DEPOSIT_MONTHS, MIN_LEASE_MONTHS, TRUTH)

_MONTHLY_PRICED = [r for r in TRUTH
                   if r["rent"] is not None and r["period"] == "month"]


def _props(rows=None):
    return [truth_property(r) for r in (rows if rows is not None else TRUTH)]


def test_empty_inventory_makes_no_claims():
    # Saying nothing is safe; asserting something false is not.
    assert portfolio_facts([]) == ""


def test_counts_match_the_inventory():
    out = portfolio_facts(_props())
    by_type = {}
    for r in TRUTH:
        by_type[r["type"]] = by_type.get(r["type"], 0) + 1
    assert f"{len(TRUTH)} listing(s)" in out
    assert f"{by_type['apartment']} apartments" in out
    assert f"{by_type['house']} houses" in out
    assert f"{by_type['commercial']} commercial and office spaces" in out


def test_bedroom_range_matches_the_inventory():
    out = portfolio_facts(_props())
    assert "largest is five-bedroom" in out
    assert "smallest is one-bedroom" in out


def test_zones_match_the_inventory_exactly():
    out = portfolio_facts(_props())
    m = re.search(r"Areas: Colombo ([^.]+) only", out)
    assert m, out
    claimed = {int(n) for n in re.findall(r"\d+", m.group(1))}
    assert claimed == {r["zone"] for r in TRUTH}


def test_facts_track_a_changed_inventory():
    """The whole point: add a 6-bedroom listing in a new zone and the claims move
    with it. Under the old hard-coded prompt, Amaya would have denied it."""
    rows = TRUTH + [{"id": "P99", "type": "house", "zone": 9, "beds": 6,
                     "baths": 6, "rent": 900000, "period": "month",
                     "furnishing": "furnished", "sqft": 6000}]
    out = portfolio_facts(_props(rows))
    assert "largest is six-bedroom" in out
    assert f"{len(rows)} listing(s)" in out
    m = re.search(r"Areas: Colombo ([^.]+) only", out)
    assert 9 in {int(n) for n in re.findall(r"\d+", m.group(1))}


def test_cheapest_and_dearest_are_computed_not_left_to_the_llm():
    """Asked in Sinhala for the cheapest listing, Amaya named a Rs 150,000 unit
    when a Rs 130,000 one exists -- she was scanning the listings and comparing.
    Aggregations get computed here, like the deposit figures."""
    out = portfolio_facts(_props())
    lo = min(_MONTHLY_PRICED, key=lambda r: r["rent"])
    hi = max(_MONTHLY_PRICED, key=lambda r: r["rent"])
    assert f"CHEAPEST listing: {lo['id']} at Rs {lo['rent']:,}" in out
    assert f"MOST EXPENSIVE: {hi['id']} at Rs {hi['rent']:,}" in out


def test_per_day_pricing_is_never_ranked_as_a_monthly_rent():
    """P03 is Rs 15,000 PER DAY -- numerically the smallest rent in the table,
    but calling it the cheapest listing would undercut every genuine monthly
    rent by an order of magnitude. Only per-month figures may be ranked."""
    daily = [r for r in TRUTH if r["period"] == "day"]
    assert daily, "the portfolio no longer has a per-day listing; retire this test"
    out = portfolio_facts(_props())
    for r in daily:
        assert f"CHEAPEST listing: {r['id']}" not in out


def test_cheapest_tracks_a_changed_inventory():
    rows = TRUTH + [{"id": "P99", "type": "house", "zone": 4, "beds": 1,
                     "baths": 1, "rent": 50000, "period": "month",
                     "furnishing": "unfurnished", "sqft": 400}]
    out = portfolio_facts(_props(rows))
    assert "CHEAPEST listing: P99 at Rs 50,000" in out


def test_no_cheapest_claim_for_a_single_listing():
    """One listing is not 'the cheapest and the most expensive' -- that reads as
    two options to a caller."""
    out = portfolio_facts(_props(_MONTHLY_PRICED[:1]))
    assert "CHEAPEST" not in out


def test_on_request_listings_are_not_ranked_by_price():
    props = _props()
    for p in props:
        p.rent_on_request, p.rent_amount = True, None
    assert "CHEAPEST" not in portfolio_facts(props)


def test_mixed_pricing_is_stated_as_a_split_never_as_all_fixed():
    """24 of the 60 real rows are price-on-request. The block must say so --
    'every listing has a fixed rent' would be a confident lie."""
    out = portfolio_facts(_props())
    on_request = sum(1 for r in TRUTH if r["rent"] is None)
    assert "Every listing has a fixed" not in out
    assert f"{len(TRUTH) - on_request} listing(s) have a fixed quoted rent" in out
    assert f"{on_request} are quoted on request" in out


def test_all_on_request_is_stated_as_such():
    props = _props()
    for p in props:
        p.rent_on_request, p.rent_amount = True, None
    assert "Every listing's rent is quoted on request" in portfolio_facts(props)


def test_all_fixed_is_only_claimed_when_true():
    assert "Every listing has a fixed" in portfolio_facts(_props(_MONTHLY_PRICED))


def test_terms_coverage_is_counted_not_left_to_the_llm():
    """Asked "is there a deposit?" with no property named, Amaya answered "for
    most of our properties, yes" -- 44 of 60 record NO deposit. The block never
    stated the coverage, so she extrapolated from whatever sample she held.
    Anything countable is counted here; expectations derive from the audited
    terms tables, never hardcoded."""
    out = portfolio_facts(_props())
    n = len(TRUTH)
    assert (f"{len(DEPOSIT_MONTHS)} of {n} state a deposit, "
            f"{len(ADVANCE_MONTHS)} an advance, "
            f"{len(MIN_LEASE_MONTHS)} a minimum lease") in out
    assert "consultant confirms terms" in out


def test_terms_line_tracks_a_changed_inventory():
    props = _props()
    for p in props:
        p.deposit_months, p.advance_months, p.min_lease_months = 2, 1, 12
    out = portfolio_facts(props)
    # Full coverage carries no overgeneralisation trap: no warning line.
    assert "state a deposit" not in out


def test_zero_terms_coverage_is_still_stated_as_a_count():
    props = _props()
    for p in props:
        p.deposit_months = p.advance_months = p.min_lease_months = None
    out = portfolio_facts(props)
    assert f"0 of {len(TRUTH)} state a deposit" in out


def test_availability_split_is_computed_not_left_to_the_llm():
    """Amaya told a caller all 60 properties are available right now; P26 is
    only available soon. The block's own headline count fed that answer, so
    the block must carry the split itself."""
    out = portfolio_facts(_props())
    n = len(TRUTH)
    n_now = n - len(AVAILABLE_NOT_NOW)
    assert f"{n_now} of {n} listings are available right now" in out
    for pid, when in AVAILABLE_NOT_NOW.items():
        assert pid in out and f"available {when}" in out


def test_all_available_now_needs_no_availability_line():
    props = _props()
    for p in props:
        p.available = "now"
    assert "available right now" not in portfolio_facts(props)


def test_many_unavailable_listings_are_counted_not_enumerated():
    # Naming each exception is only useful while they are few; past that the
    # block would bloat every single turn.
    props = _props()
    for p in props:
        p.available = "now"
    for p in props[:10]:
        p.available = "soon"
    out = portfolio_facts(props)
    assert f"{len(props) - 10} of {len(props)} listings are available right now" in out
    assert "P02 (" not in out


def test_uncounted_attributes_get_the_generalisation_guard():
    """The open class: parking (35/60 recorded), furnishing (51/60) and any
    future attribute carry the same trap as the deposit. One guard line covers
    what no one thought to count."""
    out = portfolio_facts(_props())
    assert "Never characterise the portfolio as a whole" in out
    assert "parking, furnishing" in out
    # The empty inventory still says nothing at all.
    assert portfolio_facts([]) == ""


def test_agency_name_is_parameterized_not_hardcoded():
    # The agency must come from the caller, not a literal baked into the facts
    # text. Probe with a name that appears nowhere in the codebase: if the
    # parameter were ignored, the default would leak through instead.
    out = portfolio_facts(_props(), agency="Probe Lettings Ltd")
    assert "Probe Lettings Ltd" in out
    assert "Star Properties" not in out


def test_default_agency_is_still_star_properties():
    assert "Star Properties" in portfolio_facts(_props())


def test_claims_are_consistent_with_the_live_structured_kb():
    """Ties the generated block to the real KB via the audited truth table,
    so a KB edit that contradicts the prompt fails CI."""
    out = portfolio_facts(_props())
    words = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five",
             6: "six", 7: "seven", 8: "eight", 9: "nine", 10: "ten"}
    beds = sorted({r["beds"] for r in TRUTH if r["beds"] is not None})
    for b in beds:
        assert words[b] in out
    # Nothing larger than the real maximum may be claimed as available.
    for b in range(max(beds) + 1, 11):
        assert f"{words.get(b, str(b))}-bedroom" not in out
