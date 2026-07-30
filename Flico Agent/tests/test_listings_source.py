"""initialize_kb must prefer the structured source, and never break a call.

listings.json is the source of truth; the prose file is a fallback so a rollback
is one file away and cannot be blocked by this code path.
"""
import json
import os
import re

import pytest

import kb.engine
import knowledge_base_sqlite as kbs
from tests.conftest import zero_embedder
from tests.truth_table import TRUTH

_DOCS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "knowledge_docs")


@pytest.fixture
def fresh(tmp_path, monkeypatch):
    def _make():
        with zero_embedder():
            kbs._engine = kb.engine.RealEstateKB(db_path=str(tmp_path / "t.db"))
        return kbs._engine
    engine = _make()
    yield engine
    kbs._engine = None


def test_loads_the_structured_source(fresh):
    assert kbs.initialize_kb(_DOCS) is True
    assert fresh.get_count() == len(TRUTH)


def test_prose_is_generated_not_hand_written(fresh):
    kbs.initialize_kb(_DOCS)
    for p in fresh.all_properties():
        assert p.description, f"{p.id} has no prose"
        assert f"(Ref: {p.id})" in p.description
        assert p.description.startswith("Star Properties has")


# Transcribed from the frozen feed (knowledge_docs/_source_feed_2026-07-30.txt),
# independently of the importer: rows whose features column is "-".
_NO_FEATURES = {"P26", "P28", "P30", "P31", "P32", "P37", "P38"}
# Rows whose feed features include "Swimming Pool".
_POOLS = ["P02", "P06", "P07", "P09", "P10", "P11", "P12", "P13", "P14", "P15",
          "P16", "P17", "P18", "P20", "P21", "P22", "P23", "P24", "P27", "P43",
          "P44", "P45", "P46", "P47", "P48", "P49", "P50", "P54", "P56"]


def test_amenities_are_data_now_not_only_prose(fresh):
    """migrate.py hardcoded key_features=[], so amenities existed ONLY inside the
    paragraph and no filter could ever see them. The inversion fixes that.
    The real feed legitimately lists NO features for seven rows ('-') -- only
    those may be empty; anything else empty means the importer lost data."""
    kbs.initialize_kb(_DOCS)
    for p in fresh.all_properties():
        if p.id in _NO_FEATURES:
            continue
        assert p.key_features, f"{p.id} lost its features"
    pools = [p.id for p in fresh.all_properties()
             if any("pool" in f.lower() for f in p.key_features)]
    assert sorted(pools) == _POOLS


def test_preamble_is_loaded_from_its_own_file(fresh):
    kbs.initialize_kb(_DOCS)
    assert "Star Properties" in fresh.preamble
    assert "AREAS COVERED" in fresh.preamble
    # The preamble must never contain listing paragraphs -- those are generated.
    assert "(Ref: P" not in fresh.preamble


def test_a_corrupt_source_is_rejected_and_falls_back(fresh, tmp_path):
    """A bad listings.json must not take the agent down. It falls back to the
    prose file rather than serving nothing."""
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "listings.json").write_text('{"listings": [{"id": "P1"}]}')  # invalid row
    src = os.path.join(_DOCS, "flico_info.txt")
    (docs / "flico_info.txt").write_text(open(src, encoding="utf-8").read())

    assert kbs.initialize_kb(str(docs)) is True
    # Served from the prose fallback. NOTE (2026-07-30): flico_info.txt was NOT
    # regenerated when the 60-row portfolio landed -- it still holds the 12-row
    # demo snapshot, so a fallback serves stale inventory, not the current 60.
    # The guarantee this test protects is "a bad paste never takes the agent
    # down to zero listings"; the staleness itself is a reported discrepancy.
    assert fresh.get_count() > 0


def test_validation_rejects_a_listing_with_no_zone(fresh, tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    with open(os.path.join(_DOCS, "listings.json"), encoding="utf-8") as fh:
        doc = json.load(fh)
    doc["listings"][0]["zone"] = None
    (docs / "listings.json").write_text(json.dumps(doc))
    # No prose fallback present -> the load must fail rather than serve a listing
    # the zone filter can never match.
    assert kbs.initialize_kb(str(docs)) is False


def test_retrieval_from_the_structured_source_matches_the_tables(fresh):
    kbs.initialize_kb(_DOCS)
    refs = sorted(set(re.findall(
        r"\[(P\d+)\]", kbs.retrieve_context("a two bedroom house", sticky={}))))
    assert refs == ["P60", "P61", "P62"]
