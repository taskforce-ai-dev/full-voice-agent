"""Policy invariants for what Tanya (the Hatton Hills / HattonHills demo agent)
is allowed to say about a guest's name during booking.

Ported from Kavya/tests/test_prompt_policy.py (PR #123, reviewed twice — once
adversarially) after HattonHills/server.py:679-688 was found to carry a
byte-identical copy of the buggy name-recovery bullets that caused a real
wrong-name booking on Kavya (booking ref 77, guest_name 'Cha Shawnee' for
Chanya Shehani). HattonHills is the public website demo, so the same failure
there is customer-facing in front of prospective clients.

Two things differ deliberately from the Kavya original, both load-bearing:

1. HattonHills has no post-call WhatsApp confirmation. The only committed n8n
   post-call workflow ("Treehouse Post-Call Processor", webhook path
   `treehouse-call-webhook`) is written for "Treehouse Chalets' voice booking
   agent (Kavya)" specifically. HattonHills/post_call.py posts instead to
   `N8N_POSTCALL_WEBHOOK` (default `/webhook/transcript` per .env.example) —
   a different path with no known workflow listening on it, confirmed against
   the live n8n instance (49 workflows, none scoped to HattonHills/Tanya
   post-call WhatsApp sending). So the LOOP EXIT here must NOT promise a
   WhatsApp confirmation at all — it was reworded to just acknowledge the
   name and move on.
2. The two blocking defects an adversarial review found on Kavya's first pass
   — the hard gate forbidding the very thing the LOOP EXIT above it permits,
   and the LOOP EXIT's own final read-back re-triggering the spelling
   fallback it had just escaped — are asserted against directly here so they
   cannot silently reappear in this copy.

NOTE: a second, pre-existing WhatsApp promise lives elsewhere in this prompt
(the post-create_booking success bullet: "they will also receive a WhatsApp
confirmation shortly with all the details"), also unbacked by any code path
in this repo. That one was deliberately left as-is per product-owner
direction (Aug 2026): for the public try-it-out demo, that promise is
considered cosmetic and out of scope here. Only the LOOP EXIT's promise
below was in scope, because a dangling channel reference there defeats the
purpose of the loop exit itself (it exists to give the guest a way to catch
a wrong name) rather than being merely cosmetic.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

HATTONHILLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HATTONHILLS))

import server  # noqa: E402

PROMPT = server._build_system_prompt("en")


# ---------------------------------------------------------------------------
# Name capture: MACHINE TRANSCRIPTION framing + one-token / two-token branches
# ---------------------------------------------------------------------------

def test_prompt_frames_transcription_as_machine_generated():
    """The model receives no audio, only STT transcript text - it has no
    acoustic or per-token confidence signal. It must judge only the text and
    never claim the audio or line was unclear."""
    assert "MACHINE TRANSCRIPTION" in PROMPT
    assert "you cannot hear audio" in PROMPT


def test_name_confirmation_is_not_gated_on_unperceivable_audio_signal():
    """Positive counterpart to test_prompt_never_blames_unclear_audio_for_a_name_capture
    below: also lock out the "how confident are you" framing some earlier
    drafts of this bug used, so it cannot come back under different wording."""
    assert "how confident you are" not in PROMPT
    assert "you are NOT confident" not in PROMPT


def test_prompt_never_blames_unclear_audio_for_a_name_capture():
    """This exact phrase ("If the audio was unclear or garbled for either
    part, re-ask...") was HattonHills' copy of the branch that caused the
    production wrong-name booking on Kavya (booking ref 77, guest_name
    'Cha Shawnee' for Chanya Shehani): it tells the model to judge a signal
    (audio clarity) it never receives. It is replaced by explicit one-token /
    two-token-mis-transcribed handling, so this phrase must not reappear."""
    assert "If the audio was unclear or garbled" not in PROMPT


def test_prompt_has_a_two_or_more_token_one_part_at_a_time_branch():
    """The missing branch that caused the production failure: two or more
    tokens arrived and at least one looks mis-transcribed. Confirm one part
    at a time rather than re-asking for the whole name. Generalised from
    the original TWO-tokens-only wording to close the three-or-more token
    gap - see test_prompt_has_no_gap_for_three_or_more_tokens below -
    ported from Kavya/server.py, which had the identical hole (Sri Lankan
    names routinely have three parts, and a three-token reply matched no
    branch at all before this fix)."""
    assert "If you received TWO OR MORE tokens" in PROMPT
    assert "confirm ONE PART AT A TIME" in PROMPT


def test_prompt_has_no_gap_for_three_or_more_tokens():
    """Before this fix, the prompt had a branch for exactly ONE token and a
    branch for exactly TWO tokens - nothing for three or more, even though
    Sri Lankan names routinely have three parts (first, middle, last). A
    three-token reply (clean or mis-transcribed) had no matching branch,
    which is the identical "no applicable instruction, fall back to
    something forbidden" shape that produced the original Kavya wrong-name
    booking on a two-token reply - it just had not been hit yet on this
    agent because nobody had tested a long name. The branch is now
    token-count-agnostic: it explicitly says extra tokens are a normal name
    shape, not an error, and gives a deterministic mapping (first token =
    first name, every other token joined = last name) that applies for any
    token count, not just two."""
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
    there are more than two of them. Left implicit, a model asked to fill
    two fields from N confirmed tokens with no stated mapping has no single
    correct move. The prompt must state the same first-token/remaining-
    tokens mapping used in the clean case applies here too."""
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


def test_prompt_forbids_the_generic_full_name_reask():
    """Re-asking for the "full name" (or "first name and last name") in one
    breath is what produces a multi-turn loop - the guest can't tell which
    part is being re-asked. Every re-ask must name exactly one part."""
    assert (
        "NEVER ask the guest to repeat their 'full name', or their "
        "'first name and last name', in one breath"
    ) in PROMPT


# ---------------------------------------------------------------------------
# LOOP EXIT: escape hatch, hard-gate carve-out, no dangling promises
# ---------------------------------------------------------------------------

def test_prompt_has_a_loop_exit_for_repeated_name_mismatches():
    """Without an exit, a persistently mis-transcribed name traps the guest
    in an endless re-ask loop. After the same part has been re-asked twice,
    Tanya must take her best guess and move on."""
    assert "LOOP EXIT" in PROMPT
    assert "if you have re-asked the same part TWICE" in PROMPT


def test_hard_gate_carves_out_the_loop_exit_exception():
    """Blocking defect #1 from Kavya's adversarial review: the hard gate ("do
    NOT proceed to the mobile number ... until you have BOTH a distinct
    first name AND a distinct last name captured and confirmed") otherwise
    flatly forbids the very thing the LOOP EXIT immediately above it permits
    - proceeding on a best-effort guess once repeats and spelling have both
    failed. Without an explicit carve-out here, the LOOP EXIT is forbidden by
    the gate directly beneath it."""
    assert (
        "confirmed — except under the LOOP EXIT rule above, which "
        "explicitly permits proceeding on a best-effort guess"
    ) in PROMPT


def test_loop_exit_final_readback_is_exempt_from_the_spelling_fallback():
    """Blocking defect #2 from Kavya's adversarial review: the LOOP EXIT's own
    closing line ("Read back your single best guess of the whole name once")
    is itself a read-back. The SPELLING FALLBACK section's second trigger
    ("If you read a name back and the guest says it is NOT right ... ask them
    to spell the part that was wrong") is phrased unconditionally, so without
    an explicit carve-out it fires on this exact read-back and re-triggers
    the very spelling fallback the LOOP EXIT had just escaped."""
    assert (
        "this final read-back is an exception to the read-back rules "
        "below: even if the guest says it is still wrong, do NOT ask "
        "them to spell it again"
    ) in PROMPT


def test_loop_exit_promise_is_truthful_about_whatsapp():
    """HattonHills, unlike Kavya, has no verified post-call WhatsApp send: the
    only committed n8n post-call workflow (webhook `treehouse-call-webhook`)
    is written for Kavya by name, and HattonHills/post_call.py posts to a
    different, unmatched webhook path (`N8N_POSTCALL_WEBHOOK`, default
    `/webhook/transcript` per .env.example) — confirmed against the live n8n
    workflow list, which has no HattonHills/Tanya post-call WhatsApp sender.
    The LOOP EXIT must therefore NOT promise a WhatsApp confirmation, nor any
    other verification/follow-up that doesn't happen."""
    assert "WhatsApp" not in PROMPT[PROMPT.index("For full name"):PROMPT.index("SPELLING FALLBACK for names")]
    assert "will confirm the exact spelling" not in PROMPT
    assert "reservations team will" not in PROMPT


def test_loop_exit_only_fires_after_spelling_fallback_has_been_tried():
    """Sequencing: (1) up to TWO repeat attempts on the suspect part, (2)
    THEN the spelling fallback, (3) THEN the loop exit's best-guess escape
    hatch. LOOP EXIT appears earlier in the rendered prompt than SPELLING
    FALLBACK, so it must explicitly point forward to it - otherwise a model
    could exit after two failed repeats without ever trying to spell the
    name. Also assert the forward reference resolves: SPELLING FALLBACK
    actually exists below LOOP EXIT in the rendered text, not just as a
    dangling cross-reference."""
    assert "try the SPELLING FALLBACK below for that part" in PROMPT
    assert "Only if spelling ALSO fails to resolve it" in PROMPT
    assert "the repeat attempts described above have STILL not" in PROMPT
    assert PROMPT.index("LOOP EXIT") < PROMPT.index("SPELLING FALLBACK for names")


# ---------------------------------------------------------------------------
# SPELLING FALLBACK block + read-back confirmation
# ---------------------------------------------------------------------------

def test_prompt_offers_a_spelling_fallback_for_names():
    """Making a guest repeat a name Tanya cannot catch - or that they said
    was wrong - is worse than asking them to spell it once. Spelling is a
    fallback, not the default: only used after a failed catch or a rejected
    read-back, never for a name already heard clearly and confirmed."""
    assert "SPELLING FALLBACK for names" in PROMPT
    assert "could you spell your last name for me" in PROMPT.lower()
    assert "never for a name you already heard clearly and confirmed" in PROMPT


def test_prompt_uses_yes_no_read_back_for_uncertain_names():
    assert "read the full name back and" in PROMPT
    assert "yes/no confirmation" in PROMPT
