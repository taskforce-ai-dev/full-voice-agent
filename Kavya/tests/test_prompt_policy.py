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


# ---------------------------------------------------------------------------
# Name capture: read-back confirmation, with a spelling fallback
# ---------------------------------------------------------------------------

def test_prompt_offers_a_spelling_fallback_for_names():
    """Policy reversed on request: the earlier build forbade spelling (spelled
    letters like B/P/V/D/E/G are confusable over a phone). In practice, making
    a guest repeat a name Kavya cannot catch — or that they said was wrong — is
    worse than asking them to spell it once. Kavya now falls back to spelling
    when she cannot make out the name after one repeat, or when the guest
    rejects her read-back — and NOT for a name already heard clearly."""
    assert "SPELLING FALLBACK for names" in PROMPT
    assert "could you spell your last name for me" in PROMPT.lower()
    # It must stay a fallback, not the default: still only used after a failed
    # catch or a rejected read-back.
    assert "never for a name you already heard clearly and confirmed" in PROMPT


def test_prompt_uses_yes_no_read_back_for_uncertain_names():
    assert "read the full name back and" in PROMPT
    assert "yes/no confirmation" in PROMPT


def test_name_confirmation_is_not_gated_on_unperceivable_audio_signal():
    """Kavya's model only ever sees STT transcript text - it has no acoustic
    or per-token confidence signal, so it cannot genuinely assess whether
    "the audio was unclear". That criterion was dropped from the name
    read-back trigger; do not reintroduce a self-reported confidence
    judgement there. (Note: "the audio was unclear" still legitimately
    appears elsewhere in the prompt, in the pre-existing first/last-name
    capture logic - this test targets only the confidence-judgement
    framing that was removed, not that unrelated phrase.)"""
    assert "how confident you are" not in PROMPT
    assert "you are NOT confident" not in PROMPT
