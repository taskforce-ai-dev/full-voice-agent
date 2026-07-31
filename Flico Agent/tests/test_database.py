import numpy as np
import pytest
from kb.database import KBDatabase
from kb.schema import Property, QueryFilters


def _prop(pid, ptype="apartment", zone=7, beds=3, rent=500000.0):
    return Property(
        id=pid, transaction="rent", property_type=ptype, zone=zone,
        area="Cinnamon Gardens", bedrooms=beds, bathrooms=2.0,
        rent_amount=rent, rent_period="month", rent_on_request=False,
        description=f"listing {pid}",
    )


@pytest.fixture
def db(tmp_path):
    return KBDatabase(str(tmp_path / "t.db"))


def _vec():
    v = np.ones(384, dtype=np.float32)
    return v / np.linalg.norm(v)


def test_insert_and_count(db):
    db.insert_properties_batch([(_prop("P1"), _vec()), (_prop("P2"), _vec())])
    assert db.get_count() == 2


def test_filter_by_type_and_zone(db):
    db.insert_properties_batch([
        (_prop("P1", ptype="apartment", zone=7), _vec()),
        (_prop("P2", ptype="house", zone=7), _vec()),
        (_prop("P3", ptype="apartment", zone=5), _vec()),
    ])
    rows = db.query_properties(QueryFilters(property_type="apartment", zone=7))
    assert [p.id for p, _ in rows] == ["P1"]


def test_filter_by_max_rent(db):
    db.insert_properties_batch([
        (_prop("P1", rent=300000.0), _vec()),
        (_prop("P2", rent=900000.0), _vec()),
    ])
    rows = db.query_properties(QueryFilters(max_rent=500000.0))
    assert [p.id for p, _ in rows] == ["P1"]


def test_reconcile_removes_absent_ids(db):
    db.insert_properties_batch([(_prop("P1"), _vec()), (_prop("P2"), _vec())])
    deleted = db.reconcile(keep_ids={"P1"})
    assert deleted == 1
    assert db.get_count() == 1


def test_embedding_roundtrip(db):
    v = _vec()
    db.insert_properties_batch([(_prop("P1"), v)])
    _, got = db.query_properties(QueryFilters())[0]
    assert got.dtype == np.float32
    assert np.allclose(got, v)


def test_availability_survives_the_db_roundtrip(db):
    """The table used to have no `available` column at all, so every listing
    came back claiming the schema default "now" -- including the one listing
    that is only available soon. The prose carried the truth, but the
    portfolio-facts aggregation (built from these round-tripped rows) was
    structurally blind to it."""
    p = _prop("P1")
    p.available = "soon"
    db.insert_properties_batch([(p, _vec()), (_prop("P2"), _vec())])
    got = {q.id: q.available for q, _ in db.query_properties(QueryFilters())}
    assert got == {"P1": "soon", "P2": "now"}


def test_terms_survive_the_db_roundtrip(db):
    p = _prop("P1")
    p.deposit_months, p.advance_months, p.min_lease_months = 2, 1, 12
    db.insert_properties_batch([(p, _vec()), (_prop("P2"), _vec())])
    got = {q.id: q for q, _ in db.query_properties(QueryFilters())}
    assert (got["P1"].deposit_months, got["P1"].advance_months,
            got["P1"].min_lease_months) == (2, 1, 12)
    # Unstated terms must come back as "not recorded", never a number.
    assert got["P2"].deposit_months is None
    assert got["P2"].advance_months is None
    assert got["P2"].min_lease_months is None


def test_a_pre_migration_db_file_gains_the_available_column(tmp_path):
    """data/ persists across restarts, so existing DB files predate the
    column. Opening one must migrate it in place; failing the insert would
    silently drop the structured source and fall back to the prose pipeline."""
    import sqlite3
    path = str(tmp_path / "old.db")
    # Build a DB, then drop the column to simulate a file created before it.
    KBDatabase(path)
    with sqlite3.connect(path) as conn:
        conn.execute("ALTER TABLE properties DROP COLUMN available")
    db = KBDatabase(path)  # re-open: must add the column back
    p = _prop("P1")
    p.available = "soon"
    db.insert_properties_batch([(p, _vec())])
    got, _ = db.query_properties(QueryFilters())[0]
    assert got.available == "soon"
