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
SERVER_TEXT = (KAVYA / "server.py").read_text(encoding="utf-8")

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
    assert "NET AND INCLUSIVE OF ALL TAXES" in PROMPT
    assert "net and inclusive of all taxes and service charge" in PROMPT
    assert "Every room-rate sentence must include the meal plan" in PROMPT


def test_prompt_forbids_adding_a_service_charge_on_top():
    """Before this change Kavya quoted "54,450 rupees, plus a 10% service
    charge" on a live call. The quoted room rate is now the final rate."""
    assert "The room rate you quote is the FINAL room rate" in PROMPT
    assert "'plus service charge'" in PROMPT


def test_kb_room_rates_are_inclusive():
    assert "NET AND INCLUSIVE OF ALL TAXES" in KB.upper()
    assert "service charge" in KB.lower()
    assert "half board with breakfast and dinner included" in KB.lower()


def test_prompt_includes_residency_question_once_before_rate_quote():
    assert "RESIDENCY QUESTION — ASK ONLY WHEN QUOTING PRICES" in PROMPT
    assert "Do NOT ask whether the guest is a Sri Lankan resident or a foreign guest" not in PROMPT
    # The prompt still asks for one-time residency capture and re-use.
    assert (
        "once the guest has picked a room, ask whether they are a Sri Lankan "
        "resident or a foreign guest" in PROMPT
    )
    assert "Ask it once and once only per booking" in PROMPT


def test_kb_has_both_foreign_and_resident_rate_sheets():
    assert "FOREIGN GUEST RATES" in KB
    assert "SRI LANKAN RESIDENT RATES" in KB
    assert "FOREIGN GUEST RATES — in US dollars" in KB
    assert "Sri Lankan resident rates in rupees" in KB
    assert "peak months of April and December" in KB
    # Keep each sheet self-describing so single-sheet retrieval is unambiguous.
    assert "guided nature walk" in KB  # existing complimentary listing retained


def test_prompt_mentions_complimentary_experiences_during_booking():
    assert "If the guest has told you this is a two-night or longer stay" in PROMPT
    assert "guided nature walk" in PROMPT
    assert "guided birding tour" in PROMPT
    assert "stargazing" in PROMPT
    assert PROMPT.count("proactively mention once") == 1


def test_prompt_includes_warm_voice_rule_with_no_stage_directions():
    low = PROMPT.lower()
    assert (
        "never emit bracketed stage directions or audio tags" in low
    )
    assert "asterisk actions" in low
    assert "[warmly]" in PROMPT
    assert "[laughs]" in PROMPT


def test_hardcoded_branding_not_present_in_kavya_prompt_or_kb():
    assert "Treehouse" not in PROMPT
    assert "Mosvold" not in PROMPT
    assert "Belihuloya" not in PROMPT
    assert "Treehouse" not in KB
    assert "Mosvold" not in KB
    assert "Belihuloya" not in KB
    assert "Treehouse" not in SERVER_TEXT
    assert "Mosvold" not in SERVER_TEXT
    assert "Belihuloya" not in SERVER_TEXT


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


def test_prompt_has_a_two_or_more_token_one_part_at_a_time_branch():
    """The missing branch that caused the production failure: two or more
    tokens arrived and at least one looks mis-transcribed. Confirm one part
    at a time rather than re-asking for the whole name. Generalised from
    the original TWO-tokens-only wording (#123) to close the three-or-more
    token gap - see test_prompt_has_no_gap_for_three_or_more_tokens below -
    without duplicating this branch for every possible token count."""
    assert "If you received TWO OR MORE tokens" in PROMPT
    assert "confirm ONE PART AT A TIME" in PROMPT


def test_prompt_has_no_gap_for_three_or_more_tokens():
    """Before this fix, the prompt had a branch for exactly ONE token and a
    branch for exactly TWO tokens - nothing for three or more, even though
    Sri Lankan names routinely have three parts (first, middle, last). A
    three-token reply (clean or mis-transcribed) had no matching branch,
    which is the identical "no applicable instruction, fall back to
    something forbidden" shape that produced the original seven-turn loop
    on a two-token reply (#123) - it just had not been hit yet because
    nobody had tested a long name. The branch is now token-count-agnostic:
    it explicitly says extra tokens are a normal name shape, not an error,
    and gives a deterministic mapping (first token = first name, every
    other token joined = last name) that applies for any token count, not
    just two."""
    assert "more than two tokens" in PROMPT
    assert "Chanya Malsha Shehani" in PROMPT
    assert "whether the guest gave two tokens or more" in PROMPT
    assert (
        "take the FIRST token as the first name and join every remaining "
        "token together as the last name"
    ) in PROMPT


def test_suspect_token_resolution_maps_back_to_first_and_last_name():
    """The suspect-token branch resolves tokens one at a time by position
    (first suspect part, then the next), but never explicitly said how the
    resolved tokens map back onto the first-name/last-name fields once
    there are more than two of them. Left implicit, that is exactly the
    kind of ambiguity the #123 history warns about: a model asked to fill
    two fields from N confirmed tokens with no stated mapping has no
    single correct move. The prompt must state the same first-token/
    remaining-tokens mapping used in the clean case applies here too."""
    assert (
        "Once every token is resolved, map them the same way as the clean "
        "case above"
    ) in PROMPT


def test_three_token_branch_sits_before_spelling_fallback_and_loop_exit():
    """The three-or-more-token branch must feed INTO the existing repeat ->
    SPELLING FALLBACK -> LOOP EXIT sequence, not sit after it or duplicate
    it as a second, competing escape hatch. Confirms rendering order: the
    TWO-OR-MORE-tokens branch appears before both LOOP EXIT (its own
    re-ask logic funnels into the same LOOP EXIT already gated above) and
    SPELLING FALLBACK (which the LOOP EXIT tries before giving up), so
    there is exactly one escape hatch in the whole section, reached the
    same way regardless of how many tokens the guest gave."""
    two_or_more = PROMPT.index("If you received TWO OR MORE tokens")
    loop_exit = PROMPT.index("LOOP EXIT")
    spelling_fallback = PROMPT.index("SPELLING FALLBACK for names")
    assert two_or_more < loop_exit < spelling_fallback


def test_suspect_token_criterion_does_not_flag_real_english_names():
    """Read literally, "is an ordinary English word sitting where a name
    should be" would flag real first names that are also ordinary English
    words - Joy, Rose, Mark, Grace, Shane - common among Sri Lankan
    Christian and Burgher callers, adding a needless re-ask to a clean
    call. The criterion must be narrowed to words that are CLEARLY not a
    name. 'cha' also moved out of this example pair - it is a fragment
    (already covered by the lone-syllable-or-fragment criterion), not an
    ordinary English word."""
    assert "is an ordinary English word that is clearly not a name ('car')" in PROMPT
    assert "ordinary English word sitting where a name should be" not in PROMPT


def test_prompt_has_a_loop_exit_for_repeated_name_mismatches():
    """Without an exit, a persistently mis-transcribed name (common for Sri
    Lankan names over telephony) traps the guest in an endless re-ask loop,
    as happened on the production call above (seven turns). After the same
    part has been re-asked twice, Kavya must take her best guess and move
    on. The wording counts RE-ASKS explicitly ("re-asked ... TWICE") rather
    than "TWO attempts", which was ambiguous about whether the guest's
    original answer counted as the first attempt."""
    assert "LOOP EXIT" in PROMPT
    assert "if you have re-asked the same part TWICE" in PROMPT


def test_loop_exit_promise_is_truthful_about_the_whatsapp_message():
    """The n8n post-call processor's customer WhatsApp message is a one-way
    automated template populated with the LLM-extracted name - the very
    name the agent just admitted it is unsure of - and nothing in that
    workflow reads replies or flags the name as unverified for a human
    follow-up (verified against n8n-workflows/treehouse-post-call-processor.json
    and post_call.py). So the prompt must NOT claim anyone will confirm,
    verify, follow up, call, or reply about the spelling - it may only
    promise that the guest receives a booking confirmation on WhatsApp and
    should check the name spelling on it themselves."""
    assert (
        "You'll get your booking confirmation on WhatsApp shortly, so "
        "please check the spelling of your name on it"
    ) in PROMPT
    assert "will confirm the exact spelling" not in PROMPT
    assert "reservations team will" not in PROMPT


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


def test_loop_exit_final_readback_is_exempt_from_the_spelling_fallback():
    """The LOOP EXIT's own closing line ("Read back your single best guess
    of the whole name once") is itself a read-back. The SPELLING FALLBACK
    section's second trigger ("If you read a name back and the guest says
    it is NOT right ... ask them to spell the part that was wrong") is
    phrased unconditionally, so without an explicit carve-out it fires on
    this exact read-back and sends the model back into spelling immediately
    after LOOP EXIT told it to stop - defeating the escape hatch it was
    meant to be. The prompt must say plainly that this one read-back is an
    exception: even a rejected answer here does not trigger another spell
    request."""
    assert (
        "this final read-back is an exception to the read-back rules "
        "below: even if the guest says it is still wrong, do NOT ask "
        "them to spell it again"
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
    assert "the repeat attempts described above have STILL not" in PROMPT
    assert PROMPT.index("LOOP EXIT") < PROMPT.index("SPELLING FALLBACK for names")


def test_spelling_fallback_trigger_is_grammatical():
    """The pre-fix trigger sentence read "If you STILL cannot make out the
    name after the repeat attempts described above have not resolved it" -
    an ungrammatical double condition (STILL ... have not) produced by
    splicing PR #122's spelling fallback onto the sequencing fix above it.
    This is the load-bearing trigger for the spelling fallback, so the
    grammar bug matters beyond cosmetics. Locks the corrected single-clause
    phrasing and the absence of the old broken text."""
    assert (
        "If the repeat attempts described above have STILL not resolved "
        "the name, politely ask them to spell it"
    ) in PROMPT
    assert "If you STILL cannot make out the name after the repeat" not in PROMPT


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


# ---------------------------------------------------------------------------
# Mobile number: validate LENGTH, re-ask, fall back to caller ID (never block)
# ---------------------------------------------------------------------------

def test_prompt_states_the_sri_lankan_mobile_length_rule():
    """A concrete count (nine local digits) is what makes a fumbled number
    DETECTABLE. Without it Kavya cannot tell a complete number from a
    dropped-digit one - the Booking 80 defect, where 074294451 was accepted
    and stored as a real-looking but wrong 9474294451."""
    assert "MOBILE NUMBER LENGTH CHECK" in PROMPT
    assert "exactly NINE digits after the leading zero" in PROMPT


def test_prompt_accepts_natural_number_formats_and_only_rejects_wrong_length():
    """The rule must not refuse a valid number for HOW it was said (leading
    zero or not, with or without the country code, double/triple shorthand) -
    only when the digit count is genuinely wrong."""
    assert "never reject a number because of HOW it was said" in PROMPT
    assert "ONLY when the count is genuinely WRONG" in PROMPT
    assert "do NOT re-ask" in PROMPT


def test_prompt_escalates_to_digit_by_digit_then_keeps_trying():
    """Digit-by-digit is not a one-shot before giving up: Kavya reads the
    number back and keeps patiently re-asking for several attempts. She must
    NOT jump to the calling-number fallback after a single failed attempt."""
    assert "DIGIT BY DIGIT" in PROMPT
    assert "one digit at a time" in PROMPT
    assert "READ IT BACK AND KEEP TRYING" in PROMPT
    assert "keep patiently re-asking" in PROMPT
    assert (
        "Do NOT announce that you will use the number they are calling from "
        "after just one failed digit-by-digit attempt"
    ) in PROMPT


def test_prompt_asks_permission_for_caller_id_only_as_a_last_resort():
    """After SEVERAL failed attempts, Kavya asks PERMISSION to use the calling
    number (a question), not a unilateral 'we'll use your number'. She uses it
    only on an explicit yes, and never reaches this step early."""
    assert "LAST RESORT — ASK PERMISSION" in PROMPT
    assert "may I send your confirmation to the number you're calling from" in PROMPT
    assert "If the guest says YES" in PROMPT
    assert "Never jump straight to this last-resort step" in PROMPT


def test_prompt_cannot_confirm_the_booking_if_the_guest_refuses_caller_id_too():
    """If the guest refuses the calling number AND still can't give a workable
    one, Kavya does not force the booking through - she says she can't confirm
    it without a contact number."""
    assert "If the guest says NO" in PROMPT
    assert "can't confirm the booking without a number" in PROMPT
    assert "Do NOT force the booking through in this case" in PROMPT


def test_prompt_exempts_foreign_numbers_from_the_nine_digit_rule():
    assert "FOREIGN NUMBERS" in PROMPT
    assert "nine-digit rule does NOT apply" in PROMPT
