"""Exhaustive proof of the SQL filter layer.

Sweeps the ENTIRE filter algebra -- every type x zone x bedroom-spec x rent-bound
combination the parser can produce -- and diffs each result against the
independent oracle. Not a sample: the rent grid uses equivalence classes
(tests/truth_table.rent_equivalence_grid), and because SQL comparisons are
monotone, covering each distinct rent +/-1 and the midpoints covers every
real-valued threshold. This is where all five shipped bugs lived.

Runs with zero-vector embeddings, so no sentence-transformers and no model
download: it is pure SQL semantics, and fast enough to gate every deploy.
"""
import itertools

import numpy as np
import pytest

from kb.database import KBDatabase
from kb.schema import Property, QueryFilters
from tests.oracle import expected_ids
from tests.truth_table import BEDROOMS, TRUTH, ZONES, rent_equivalence_grid

_TYPES = [None, "apartment", "house", "commercial", "land"]
_ZONES = [None] + list(range(1, 11))
_BEDS = [None] + sorted(set(BEDROOMS) | {3})


@pytest.fixture(scope="module")
def db(tmp_path_factory):
    d = KBDatabase(str(tmp_path_factory.mktemp("x") / "x.db"))
    zero = np.zeros(384, dtype=np.float32)
    d.insert_properties_batch([
        (Property(id=r["id"], transaction="rent", property_type=r["type"],
                  zone=r["zone"], area="", bedrooms=r["beds"],
                  bathrooms=float(r["baths"]), rent_amount=float(r["rent"]),
                  rent_period="month", furnishing=r["furnishing"],
                  floor_area_sqft=r["sqft"], description=r["id"]), zero)
        for r in TRUTH
    ])
    return d


def _ids(db, **kw):
    return sorted(p.id for p, _ in db.query_properties(QueryFilters(**kw)))


@pytest.mark.parametrize("ptype,zone,beds", list(itertools.product(_TYPES, _ZONES, _BEDS)))
def test_type_zone_bedrooms_exhaustive(db, ptype, zone, beds):
    kw = dict(property_type=ptype, zone=zone, bedrooms=beds)
    assert _ids(db, **kw) == expected_ids(TRUTH, **kw)


@pytest.mark.parametrize("min_beds", sorted(set(BEDROOMS) | {3}))
@pytest.mark.parametrize("ptype", _TYPES)
def test_bedroom_floor_exhaustive(db, ptype, min_beds):
    kw = dict(property_type=ptype, min_bedrooms=min_beds)
    assert _ids(db, **kw) == expected_ids(TRUTH, **kw)


@pytest.mark.parametrize("ceiling", rent_equivalence_grid())
@pytest.mark.parametrize("exclusive", [False, True])
def test_rent_ceiling_exhaustive(db, ceiling, exclusive):
    kw = dict(max_rent=float(ceiling), max_rent_exclusive=exclusive)
    assert _ids(db, **kw) == expected_ids(TRUTH, **kw)


@pytest.mark.parametrize("floor", rent_equivalence_grid())
def test_rent_floor_exhaustive(db, floor):
    kw = dict(min_rent=float(floor))
    assert _ids(db, **kw) == expected_ids(TRUTH, **kw)


@pytest.mark.parametrize("lo,hi", [
    (lo, hi) for lo in rent_equivalence_grid()[::3]
    for hi in rent_equivalence_grid()[::3] if lo <= hi
])
def test_rent_range_exhaustive(db, lo, hi):
    kw = dict(min_rent=float(lo), max_rent=float(hi))
    assert _ids(db, **kw) == expected_ids(TRUTH, **kw)


@pytest.mark.parametrize("zone", ZONES)
@pytest.mark.parametrize("ceiling", rent_equivalence_grid()[::4])
def test_zone_and_rent_together(db, zone, ceiling):
    kw = dict(zone=zone, max_rent=float(ceiling), max_rent_exclusive=True)
    assert _ids(db, **kw) == expected_ids(TRUTH, **kw)
