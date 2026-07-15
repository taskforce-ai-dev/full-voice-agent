import pytest
from kb.engine import RealEstateKB
from kb.schema import Property

pytest.importorskip("sentence_transformers")


def _p(pid, ptype, zone, beds=3, desc="a lovely home", rent=500000.0, req=False, period="month"):
    return Property(
        id=pid, transaction="rent", property_type=ptype, zone=zone,
        area="Area", bedrooms=beds, bathrooms=2.0,
        rent_amount=None if req else rent, rent_period=None if req else period,
        rent_on_request=req, description=desc,
    )


@pytest.fixture(scope="module")
def kb(tmp_path_factory):
    path = tmp_path_factory.mktemp("kb") / "e.db"
    k = RealEstateKB(db_path=str(path), preamble="PREAMBLE.")
    k.add_properties([
        _p("P1", "apartment", 7, desc="bright apartment with a swimming pool and gym"),
        _p("P2", "house", 7, desc="large family house with a garden"),
        _p("P3", "apartment", 5, desc="cozy flat near the school"),
        _p("P4", "apartment", 2, desc="luxury tower unit", req=True),
    ])
    return k


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


def test_requested_zone_never_dropped(kb):
    # Nothing in zone 10 -> honest empty (only preamble), never leak other zones
    out = kb.retrieve("apartment in colombo 10")
    assert "[P1]" not in out and "[P3]" not in out


def test_on_request_listing_surfaces(kb):
    out = kb.retrieve("apartment in colombo 2")
    assert "[P4]" in out
    assert "on request" in out.lower()
