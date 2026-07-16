"""Acceptance tests: the live KB must reproduce the demo portfolio's own
lookup tables exactly (DEMO PROPERTY DETAIL SECTION, P51-P62).

These run against the real prose in knowledge_docs/flico_info.txt, so they fail
loudly if the KB content and the retrieval logic ever drift apart. If the real
portfolio is restored, replace the expectations below with that portfolio's.
"""
import os
import re

import pytest

from kb.migrate import parse_prose

pytest.importorskip("sentence_transformers")

from kb.engine import RealEstateKB  # noqa: E402  (needs the optional model)

_KB_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "knowledge_docs", "flico_info.txt")


@pytest.fixture(scope="module")
def kb(tmp_path_factory):
    with open(_KB_FILE, encoding="utf-8") as fh:
        rows, skipped, _ = parse_prose(fh.read())
    assert not skipped, f"KB prose failed to parse: {skipped}"
    k = RealEstateKB(db_path=str(tmp_path_factory.mktemp("kb") / "demo.db"), preamble="")
    k.add_properties(rows)
    return k


def _refs(kb, query):
    return sorted(set(re.findall(r"\[(P\d+)\]", kb.retrieve(query, n_results=12, sticky={}))))


def test_all_twelve_demo_rows_present(kb):
    assert kb.get_count() == 12


@pytest.mark.parametrize("query,expected", [
    ("I need a one bedroom apartment", ["P51", "P52", "P53"]),
    ("a two bedroom apartment", ["P54", "P55", "P56"]),
    ("a one bedroom house", ["P57", "P58", "P59"]),
    ("a two bedroom house", ["P60", "P61", "P62"]),
])
def test_by_category(kb, query, expected):
    assert _refs(kb, query) == expected


@pytest.mark.parametrize("zone,expected", [
    (2, ["P51", "P56"]),
    (3, ["P52"]),
    (5, ["P54", "P57", "P60"]),
    (6, ["P53", "P59"]),
    (7, ["P55", "P61"]),
    (8, ["P58", "P62"]),
])
def test_by_colombo_zone(kb, zone, expected):
    assert _refs(kb, f"anything in Colombo {zone}") == expected


def test_by_price_under_300k(kb):
    # P56 is exactly Rs 300,000 and belongs to the 300k-500k band, not this one.
    assert _refs(kb, "anything under 300k") == [
        "P51", "P52", "P53", "P54", "P57", "P58", "P59", "P60", "P62"]


def test_by_price_300k_to_500k(kb):
    assert _refs(kb, "between 300k and 500k") == ["P55", "P56", "P61"]


@pytest.mark.parametrize("query,expected", [
    ("a one bedroom apartment in Colombo 2", ["P51"]),
    ("a two bedroom house in Colombo 7", ["P61"]),
    ("a one bedroom in Colombo 6", ["P53", "P59"]),
    ("a two bedroom apartment under 300k", ["P54"]),
])
def test_combined_constraints(kb, query, expected):
    assert _refs(kb, query) == expected


def test_wrong_type_is_never_offered_silently(kb):
    # Colombo 8 has houses only. Asking for an apartment there must not hand back
    # a house without saying it is a different type.
    out = kb.retrieve("an apartment in Colombo 8", sticky={})
    assert "DIFFERENT property type" in out


def test_occupancy_is_not_a_bedroom_filter(kb):
    # "four people" must not filter to 4 bedrooms (none exist) -- all 12 remain.
    assert len(_refs(kb, "somewhere for four people")) == 12
