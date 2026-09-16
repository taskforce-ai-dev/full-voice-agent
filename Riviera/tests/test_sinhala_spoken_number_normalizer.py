"""Sinhala callers say numbers tens+units combined, not digit-by-digit.

Caller feedback (2026-09-02 Sinhala call): digit-by-digit dictation eventually
worked, but a natural two-digit number ("හැට පහ" -- sixty five) was not
recognised at all. Azure si-LK STT returns Sinhala number words for such
utterances, sometimes mixed with ASCII digits, and nothing downstream ever
understood Sinhala digit words -- `handover.py`'s token splitter treats every
Sinhala character as a separator.

`server._normalize_sinhala_spoken_digits` is a pure, word-boundary-matched
text transform: it rewrites Sinhala number words to plain ASCII digits and
leaves everything else -- ordinary words, punctuation, already-ASCII digits --
untouched. It is table-driven tested here in isolation; the wiring into the
Direct SmartPBX Sinhala capture path is covered separately in
`test_sinhala_capture_dictation.py`.
"""

from __future__ import annotations

import pytest

import server


NORMALIZER_CASES: list[tuple[str, str]] = [
    # --- bare units 0-9 --------------------------------------------------
    ("බිංදුව", "0"),
    ("බින්දුව", "0"),
    ("ශුන්‍ය", "0"),
    ("එක", "1"),
    ("එකයි", "1"),
    ("දෙක", "2"),
    ("දෙකයි", "2"),
    ("තුන", "3"),
    ("හතර", "4"),
    ("පහ", "5"),
    ("හය", "6"),
    ("හත", "7"),
    ("අට", "8"),
    ("නවය", "9"),
    # --- teens 11-19 -------------------------------------------------------
    ("එකොළහ", "11"),
    ("දොළහ", "12"),
    ("දහතුන", "13"),
    ("දාහතර", "14"),
    ("පහළොව", "15"),
    ("දහසය", "16"),
    ("දාහත", "17"),
    ("දහඅට", "18"),
    ("දහනවය", "19"),
    # --- ten (10) and bare tens (20-90) -------------------------------------
    ("දහය", "10"),
    ("විස්ස", "20"),
    ("තිස්", "30"),
    ("හතළිස්", "40"),
    ("පනස්", "50"),
    ("හැට", "60"),
    ("හැත්තෑ", "70"),
    ("අසූ", "80"),
    ("අනූ", "90"),
    # --- compound tens+unit --------------------------------------------------
    ("හැට පහ", "65"),
    ("විසි එක", "21"),
    ("තිස් තුන", "33"),
    ("හතළිස් හතර", "44"),
    ("පනස් හත", "57"),
    ("හැත්තෑ අට", "78"),
    ("අනූ නවය", "99"),
    ("විස්ස පහ", "25"),
    # --- mixed ASCII digits + Sinhala words -----------------------------
    ("0 7 7 හැට පහ", "0 7 7 65"),
    ("077 හැට පහ හතර තුන", "077 65 4 3"),
    # --- punctuation / spacing is preserved around a conversion --------
    ("හැට පහ, හතර", "65, 4"),
    # --- negatives: no accidental substring or false-positive match ----
    ("පහත", "පහත"),
    ("පහසුවෙන්", "පහසුවෙන්"),
    ("එකට", "එකට"),
    ("", ""),
    ("මට කාමරයක් ඕන", "මට කාමරයක් ඕන"),
    ("ඔබට කොහොමද", "ඔබට කොහොමද"),
    ("ස්තුතියි", "ස්තුතියි"),
]


@pytest.mark.parametrize("raw, expected", NORMALIZER_CASES)
def test_normalize_sinhala_spoken_digits(raw, expected):
    assert server._normalize_sinhala_spoken_digits(raw) == expected


def test_normalizer_is_idempotent_on_already_normalized_text():
    once = server._normalize_sinhala_spoken_digits("හැට පහ හතර තුන")
    twice = server._normalize_sinhala_spoken_digits(once)
    assert once == twice == "65 4 3"


def test_normalizer_never_raises_on_none_like_falsy_input():
    assert server._normalize_sinhala_spoken_digits("") == ""


def test_sinhala_words_feed_the_capture_dictation_ratio():
    """Defense in depth: even independent of the normaliser running, a
    Sinhala number utterance must read as a dictation for capture mode."""
    ratio = server._capture_dictation_ratio("හැට පහ හතර තුන")
    assert ratio >= server.CAPTURE_DICTATION_MIN_RATIO
    assert server._capture_dictation_ratio("ඔබට කොහොමද") == 0.0
