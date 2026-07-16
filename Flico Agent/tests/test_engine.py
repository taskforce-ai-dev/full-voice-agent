import pytest

from kb.schema import Property
from tests.conftest import build_kb


def _p(pid, ptype, zone, beds=3, desc="a lovely home", rent=500000.0, req=False, period="month"):
    return Property(
        id=pid, transaction="rent", property_type=ptype, zone=zone,
        area="Area", bedrooms=beds, bathrooms=2.0,
        rent_amount=None if req else rent, rent_period=None if req else period,
        rent_on_request=req, description=desc,
    )


@pytest.fixture(scope="module")
def kb(tmp_path_factory):
    # Descriptions mirror real KB prose. The formatter emits a row's stored prose
    # when it has any, so a fixture whose prose omits the rent tests a shape that
    # migrate.py cannot produce: the parser only sets rent_on_request BECAUSE the
    # prose said so, and _validate rejects a listing with neither rent nor an
    # on-request marker. P4's prose must therefore state its rent, like a real one.
    return build_kb(
        tmp_path_factory.mktemp("kb") / "e.db",
        [
            _p("P1", "apartment", 7, desc="bright apartment with a swimming pool and gym"),
            _p("P2", "house", 7, desc="large family house with a garden"),
            _p("P3", "apartment", 5, desc="cozy flat near the school"),
            _p("P4", "apartment", 2, req=True,
               desc="luxury tower unit. The monthly rent is available on request."),
            _p("P_TWO", "apartment", 7, beds=2, desc="compact two bedroom apartment"),
        ],
        preamble="PREAMBLE.")


def test_type_and_zone_filter_excludes_other_types(kb):
    out = kb.retrieve("a 3 bedroom apartment in colombo 7")
    assert "[P1]" in out
    assert "[P2]" not in out  # house excluded
    assert "[P3]" not in out  # wrong zone


def test_preamble_prepended(kb):
    out = kb.retrieve("apartment in colombo 7")
    assert out.startswith("PREAMBLE.")


def test_relaxation_zone_only_when_type_zone_empty(kb):
    # No commercial in zone 7 -> ladder retries zone 7 (any type)
    out = kb.retrieve("office space in colombo 7")
    assert "[P1]" in out or "[P2]" in out


def test_type_substitution_is_announced_not_silent(kb):
    # No commercial in zone 7, so the ladder must drop the type -- but it may
    # never hand back another type without saying so, or the agent will describe
    # an apartment as the office space the caller asked for.
    out = kb.retrieve("office space in colombo 7")
    assert "DIFFERENT property type" in out


def test_exact_bedroom_count_excludes_other_counts(kb):
    # P1/P3/P4 are 3-bed, P_TWO is 2-bed. "a two bedroom apartment" means two.
    out = kb.retrieve("a two bedroom apartment")
    assert "[P_TWO]" in out
    assert "[P1]" not in out and "[P3]" not in out


def test_at_least_is_a_floor_not_an_exact_match(kb):
    out = kb.retrieve("an apartment with at least 2 bedrooms")
    assert "[P_TWO]" in out and "[P1]" in out


def test_bedroom_relaxation_is_announced(kb):
    # Nothing has 5 bedrooms -> relax, but say the count is wrong.
    out = kb.retrieve("a 5 bedroom apartment")
    assert "DIFFERENT number of bedrooms" in out


def test_exact_match_carries_no_note(kb):
    out = kb.retrieve("a 3 bedroom apartment in colombo 7")
    assert "NOTE:" not in out


def test_requested_zone_never_dropped(kb):
    # Nothing in zone 10 -> honest empty (only preamble), never leak other zones
    out = kb.retrieve("apartment in colombo 10")
    assert "[P1]" not in out and "[P3]" not in out


def test_on_request_listing_surfaces(kb):
    out = kb.retrieve("apartment in colombo 2")
    assert "[P4]" in out
    assert "on request" in out.lower()
