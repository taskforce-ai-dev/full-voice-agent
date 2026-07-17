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
        assert p.description.startswith("Rodrigo Realtors has")


def test_amenities_are_data_now_not_only_prose(fresh):
    """migrate.py hardcoded key_features=[], so amenities existed ONLY inside the
    paragraph and no filter could ever see them. The inversion fixes that."""
    kbs.initialize_kb(_DOCS)
    for p in fresh.all_properties():
        assert p.key_features, f"{p.id} lost its features"
    pools = [p.id for p in fresh.all_properties()
             if any("pool" in f for f in p.key_features)]
    assert sorted(pools) == ["P54", "P56"]


def test_preamble_is_loaded_from_its_own_file(fresh):
    kbs.initialize_kb(_DOCS)
    assert "Rodrigo Realtors" in fresh.preamble
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
    assert fresh.get_count() == len(TRUTH)  # served from the prose fallback


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
