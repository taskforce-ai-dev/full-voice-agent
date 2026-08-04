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
    judgement there. (Note: this used to also flag a second, unrelated
    "the audio was unclear" phrase in the first/last-name capture logic as
    a legitimate survivor. That occurrence was itself removed - see
    test_prompt_never_blames_unclear_audio_for_a_name_capture below - so
    the phrase no longer appears anywhere in the prompt.)"""
    assert "how confident you are" not in PROMPT
    assert "you are NOT confident" not in PROMPT


def test_prompt_frames_transcription_as_machine_generated():
    """Positive counterpart to test_prompt_never_blames_unclear_audio_for_a_name_capture
    below, which only locks the MACHINE TRANSCRIPTION bullet in by an absence
    assertion (that the old "audio was unclear" phrase is gone). An
    absence-only test would stay green even if the whole framing bullet were
    deleted, so also assert the bullet's actual content is present: the
    model is told it receives a machine transcription, not audio, and must
    judge only the text."""
    assert "MACHINE TRANSCRIPTION" in PROMPT
    assert "you cannot hear audio" in PROMPT


def test_prompt_never_blames_unclear_audio_for_a_name_capture():
    """Production call CA464ae445b9b20813a0f8316e6ad5dbfb (2026-08-04, guest
    Chanya Shehani): the guest gave two tokens ('cha Shawnee') and both were
    mis-transcribed, but the only applicable branch told the model to judge
    whether "the audio was unclear or garbled" - a signal the model never
    receives (Twilio ConversationRelay's transcript message carries no audio
    or confidence field). The model fell back to asking for the guest's
    "full name" again, which the prompt elsewhere forbids. That branch is
    replaced by explicit one-token / two-token-mis-transcribed handling, so
    this exact phrase must not reappear anywhere in the prompt."""
    assert "If the audio was unclear or garbled" not in PROMPT


def test_prompt_forbids_the_generic_full_name_reask():
    """Re-asking for the "full name" (or "first name and last name") in one
    breath is what produced the seven-turn loop on the call above - the
    guest can't tell which part is being re-asked. Every re-ask must name
    exactly one part."""
    assert (
        "NEVER ask the guest to repeat their 'full name', or their "
        "'first name and last name', in one breath"
    ) in PROMPT


def test_prompt_has_a_two_token_one_part_at_a_time_branch():
    """The missing branch that caused the production failure: two tokens
    arrived and at least one looks mis-transcribed. Confirm one part at a
    time rather than re-asking for the whole name."""
    assert "If you received TWO tokens but either looks mis-transcribed" in PROMPT
    assert "confirm ONE PART AT A TIME" in PROMPT


def test_prompt_has_a_loop_exit_for_repeated_name_mismatches():
    """Without an exit, a persistently mis-transcribed name (common for Sri
    Lankan names over telephony) traps the guest in an endless re-ask loop,
    as happened on the production call above (seven turns). After two
    attempts on the same part, Kavya must take her best guess, promise a
    WhatsApp follow-up, and move on."""
    assert "LOOP EXIT" in PROMPT
    assert "after TWO attempts on the same part" in PROMPT
    assert (
        "our reservations team will confirm the exact spelling with you "
        "on WhatsApp together with your booking confirmation"
    ) in PROMPT


def test_hard_gate_carves_out_the_loop_exit_exception():
    """The hard gate ("do NOT proceed to the mobile number ... until you
    have BOTH a distinct first name AND a distinct last name captured and
    confirmed") otherwise flatly contradicts the LOOP EXIT immediately
    above it, which fires precisely when the name is NOT confirmed - "the
    guest still says no" is one of its two triggers - and tells the model
    to proceed to the mobile number anyway. Without an explicit exception,
    the model has to arbitrate between two absolute directives at exactly
    the moment the escape hatch should release it: the same "no applicable
    instruction, fall back to something forbidden" shape that produced the
    original seven-turn call."""
    assert (
        "confirmed — except under the LOOP EXIT rule above, which "
        "explicitly permits proceeding on a best-effort guess"
    ) in PROMPT


def test_loop_exit_only_fires_after_spelling_fallback_has_been_tried():
    """Sequencing fix: the intended flow is (1) up to TWO repeat attempts on
    the suspect part, (2) THEN the spelling fallback (PR #122), (3) THEN the
    loop exit's best-guess-plus-WhatsApp escape hatch. Before this fix,
    LOOP EXIT - which appears earlier in the rendered prompt than the
    SPELLING FALLBACK block - never mentioned spelling at all, so a model
    could exit after two failed repeats without ever trying to spell the
    name. A same-strings-exist assertion would not catch that: it checks
    the actual cross-referencing phrases that wire the two blocks together,
    plus that LOOP EXIT (which points forward to spelling) still precedes
    SPELLING FALLBACK (which points back to the repeat attempts) in the
    rendered text, so both forward- and backward-references resolve to
    real content."""
    assert "try the SPELLING FALLBACK below for that part" in PROMPT
    assert "Only if spelling ALSO fails to resolve it" in PROMPT
    assert "repeat attempts described above have not resolved it" in PROMPT
    assert PROMPT.index("LOOP EXIT") < PROMPT.index("SPELLING FALLBACK for names")


def test_mobile_number_is_read_back_in_local_form_not_plus94():
    """When Kavya repeats the guest's mobile number to confirm it, she must
    say it in the natural local form the guest gave (e.g. "zero seven seven,
    ..."), NOT prefixed with the +94 country code spoken as "plus nine four".
    The +94 canonicalisation is a backend concern (WhatsApp/PMS); the guest
    should only ever hear their own local number. (Note: "plus nine four"
    still legitimately appears elsewhere in the prompt for the hotel's own
    reservations hotline - this test targets the mobile read-back rule.)"""
    assert "READING THE MOBILE NUMBER BACK" in PROMPT
    assert "NEVER speak the country code" in PROMPT
