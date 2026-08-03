"""Policy invariants for what Kavya is allowed to say.

These guard two management-approved decisions that are easy to undo by accident:
no individual is ever named in a data-security answer, and a quoted room rate
is final. Both live in two places at once (system prompt + knowledge base), so
each is asserted against both.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

KAVYA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KAVYA))

import server  # noqa: E402

KB = (KAVYA / "knowledge_docs" / "hotel_info.txt").read_text(encoding="utf-8")
PROMPT = server._build_system_prompt("en")

# Named in the pre-approval wording; management asked for both to be removed.
FORMER_NAMES = ["Rakesh", "Mr. Chrys"]


@pytest.mark.parametrize("name", FORMER_NAMES)
def test_no_employee_is_named_in_the_prompt(name):
    assert name not in PROMPT


@pytest.mark.parametrize("name", FORMER_NAMES)
def test_no_employee_is_named_in_the_knowledge_base(name):
    assert name not in KB


@pytest.mark.parametrize("source,text", [("prompt", PROMPT), ("kb", KB)])
def test_access_headcount_is_never_stated(source, text):
    """"Only two people can access it" was dropped: it is the claim that breaks
    the moment a third person is granted access."""
    lowered = text.lower()
    for phrase in ("only two people", "two people can access", "only two authorised"):
        assert phrase not in lowered, f"{source} still states a headcount: {phrase}"


@pytest.mark.parametrize("source,text", [("prompt", PROMPT), ("kb", KB)])
def test_rbac_is_still_the_answer(source, text):
    """Removing the names must not remove the substance."""
    lowered = text.lower()
    assert "role" in lowered and "access control" in lowered
    assert "never by default" in lowered
    assert "no shared administrative account" in lowered


@pytest.mark.parametrize("source,text", [("prompt", PROMPT), ("kb", KB)])
def test_data_is_never_sold_or_shared(source, text):
    assert "never sold" in text.lower()
    assert "never shared with anyone outside the service of those reservations" in text.lower()


def test_no_certification_is_claimed():
    """The approved wording deliberately claims no audit or standard."""
    lowered = (PROMPT + KB).lower()
    for claim in ("iso 27001", "iso27001", "soc 2", "soc2", "pci-dss", "pci dss",
                  "gdpr compliant", "gdpr-compliant", "certified"):
        assert claim not in lowered, f"unverified compliance claim present: {claim}"


# ---------------------------------------------------------------------------
# Room rates are inclusive and final
# ---------------------------------------------------------------------------

def test_prompt_states_rates_include_tax_and_service_charge():
    assert "INCLUSIVE of all taxes and service charge" in PROMPT


def test_prompt_forbids_adding_a_service_charge_on_top():
    """Before this change Kavya quoted "54,450 rupees, plus a 10% service
    charge" on a live call. The quoted room rate is now the final rate."""
    assert "The room rate you quote is the FINAL room rate" in PROMPT
    assert "'plus service charge'" in PROMPT


def test_kb_room_rates_are_inclusive():
    assert "all taxes and service charge included" in KB
    assert "include all taxes and service charge" in KB
    assert "nothing further is added to it" in KB


def test_paid_experiences_still_carry_their_service_charge():
    """Experiences are priced exclusive of service charge and were deliberately
    NOT changed - only room rates are inclusive. If this ever flips, it must be
    a decision, not a stray find-and-replace."""
    assert "plus ten percent service charge" in KB
    assert "additional ten percent service charge" in KB
