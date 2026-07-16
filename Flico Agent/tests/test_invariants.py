"""Invariants the relaxation ladder must satisfy for EVERY reachable query.

The exhaustive filter sweep proves the SQL is right. This proves the layer above
it -- the ladder -- can never hand the LLM something misleading: a wrong-type
listing without a label, a listing outside the requested zone, or a silent
substitution. These are properties, not examples: they are asserted over the
whole reachable filter space rather than a few cases.

Uses the shared zero-vector embedder (tests/conftest.py) so no model download is
needed. Ranking only permutes, so it cannot affect any property asserted here.
"""
import itertools

import pytest

from kb.schema import Property, QueryFilters
from tests.conftest import build_kb
from tests.oracle import satisfies
from tests.truth_table import TRUTH, rent_equivalence_grid

_TYPE_NOTE = "DIFFERENT property type"
_BEDS_NOTE = "DIFFERENT number of bedrooms"
_RENT_NOTE = "cost MORE than they asked"


@pytest.fixture(scope="module")
def store(tmp_path_factory):
    return build_kb(tmp_path_factory.mktemp("inv") / "i.db", [
        Property(id=r["id"], transaction="rent", property_type=r["type"],
                 zone=r["zone"], area="", bedrooms=r["beds"],
                 bathrooms=float(r["baths"]), rent_amount=float(r["rent"]),
                 rent_period="month", furnishing=r["furnishing"],
                 floor_area_sqft=r["sqft"], description=r["id"])
        for r in TRUTH
    ])


def _filter_space():
    rents = rent_equivalence_grid()[::5]
    for ptype, zone, beds, rent in itertools.product(
            [None, "apartment", "house", "commercial", "land"],
            [None] + list(range(1, 11)),
            [None, 1, 2, 3],
            [None] + [float(r) for r in rents]):
        yield QueryFilters(property_type=ptype, zone=zone, bedrooms=beds,
                           max_rent=rent, max_rent_exclusive=True)


SPACE = list(_filter_space())


def _constraints(f):
    return dict(property_type=f.property_type, zone=f.zone, bedrooms=f.bedrooms,
                min_bedrooms=f.min_bedrooms, max_rent=f.max_rent,
                max_rent_exclusive=f.max_rent_exclusive, min_rent=f.min_rent)


def _rows_of(store, f):
    rows, note = store._candidates(f)
    return [p for p, _ in rows], note


def test_space_is_large():
    assert len(SPACE) > 1000


def test_zone_is_never_relaxed(store):
    """The strongest guarantee: a caller who names an area is NEVER offered
    another area. Silently widening the zone was a real production bug."""
    bad = []
    for f in SPACE:
        if f.zone is None:
            continue
        for p in _rows_of(store, f)[0]:
            if p.zone != f.zone:
                bad.append(f"{f!r} -> {p.id} in zone {p.zone}")
    assert not bad, bad[:10]


def test_no_note_means_exact_satisfaction(store):
    """If nothing was relaxed, every row must satisfy every stated constraint."""
    bad = []
    for f in SPACE:
        rows, note = _rows_of(store, f)
        if note:
            continue
        for p in rows:
            row = {"id": p.id, "type": p.property_type, "zone": p.zone,
                   "beds": p.bedrooms, "rent": p.rent_amount}
            if not satisfies(row, **_constraints(f)):
                bad.append(f"{f!r} -> {p.id} violates a stated constraint with no NOTE")
    assert not bad, bad[:10]


def test_substitution_is_always_announced(store):
    """A row that violates a stated constraint may only appear alongside a NOTE
    naming that exact dimension. This is what stops the LLM describing a house
    as the apartment the caller asked for."""
    bad = []
    for f in SPACE:
        rows, note = _rows_of(store, f)
        for p in rows:
            if f.property_type and p.property_type != f.property_type and _TYPE_NOTE not in note:
                bad.append(f"{f!r} -> {p.id} wrong type, unannounced")
            if f.bedrooms and p.bedrooms != f.bedrooms and _BEDS_NOTE not in note:
                bad.append(f"{f!r} -> {p.id} wrong bedrooms, unannounced")
            if (f.max_rent and p.rent_amount and p.rent_amount >= f.max_rent
                    and _RENT_NOTE not in note):
                bad.append(f"{f!r} -> {p.id} over budget, unannounced")
    assert not bad, bad[:10]


def test_note_implies_something_was_actually_relaxed(store):
    """A NOTE that fires when nothing was given up teaches the LLM to distrust
    NOTEs, which would undermine every guarantee above."""
    bad = []
    for f in SPACE:
        rows, note = _rows_of(store, f)
        if not note or not rows:
            continue
        if _TYPE_NOTE in note and all(p.property_type == f.property_type for p in rows):
            bad.append(f"{f!r}: type note but every row matches the type")
        if _BEDS_NOTE in note and f.bedrooms and all(p.bedrooms == f.bedrooms for p in rows):
            bad.append(f"{f!r}: bedroom note but every row matches the count")
    assert not bad, bad[:10]


def test_never_fabricates_a_listing(store):
    known = {r["id"] for r in TRUTH}
    for f in SPACE:
        rows, _ = _rows_of(store, f)
        assert {p.id for p in rows} <= known, f


def test_exact_matches_are_never_diluted(store):
    """If exact matches exist, the ladder must return exactly those and stop --
    never pad the results with relaxed listings the caller did not ask for."""
    bad = []
    for f in SPACE:
        exact = sorted(r["id"] for r in TRUTH if satisfies(r, **_constraints(f)))
        if not exact:
            continue
        rows, note = _rows_of(store, f)
        if sorted(p.id for p in rows) != exact or note:
            bad.append(f"{f!r}: expected exactly {exact}, got {sorted(p.id for p in rows)} note={bool(note)}")
    assert not bad, bad[:10]
