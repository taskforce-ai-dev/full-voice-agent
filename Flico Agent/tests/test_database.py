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
