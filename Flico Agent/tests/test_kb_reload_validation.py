"""The /kb-reload side door must reject corrupt inventory, not serve it.

The admin portal POSTs KB content straight to a live agent, bypassing CI
entirely. These assert the validator, so "a bad KB can never reach prod" is
true through that path too -- not just through git.
"""
import pytest

from kb.migrate import parse_prose

pytest.importorskip("sentence_transformers", reason="knowledge_base_sqlite loads the model")

from knowledge_base_sqlite import _validate  # noqa: E402

_GOOD = ("Rodrigo Realtors has a 1-bedroom, 1-bathroom furnished apartment for rent "
         "on Park Street in Colombo 2 (Slave Island), with a floor area of 620 square "
         "feet, at a monthly rent of one hundred and eighty thousand rupees "
         "(Rs 180,000) per month. (Ref: P51)")


def _v(text):
    rows, skipped, _ = parse_prose(text)
    return _validate(rows, skipped)


def test_good_prose_accepted():
    assert _v(_GOOD) is None


def test_empty_rejected():
    assert _v("Some preamble with no listings at all.") == "no listings parsed"


def test_listing_without_zone_rejected():
    text = _GOOD.replace("in Colombo 2 (Slave Island)", "somewhere nice")
    assert "zone" in (_v(text) or "")


def test_listing_without_rent_rejected():
    text = _GOOD.replace("at a monthly rent of one hundred and eighty thousand "
                         "rupees (Rs 180,000) per month", "at a great price")
    assert "rent" in (_v(text) or "")


def test_duplicate_ids_rejected():
    assert _v(_GOOD + "\n\n" + _GOOD) == "duplicate listing ids"


def test_unreadable_type_is_skipped_not_guessed():
    # "Rodrigo Realtors has a ..." with no recognisable type word must not be
    # silently filed as an apartment.
    text = _GOOD.replace("apartment", "thingamajig")
    rows, skipped, _ = parse_prose(text)
    assert not rows and skipped
    assert _validate(rows, skipped) is not None
