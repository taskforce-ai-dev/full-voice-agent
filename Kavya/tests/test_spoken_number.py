"""Deterministic spoken-number parsing is the single authority for phone digits.

Azure transcribes the caller's words correctly ("nought", "triple seven"); the
failure was the LLM doing digit arithmetic in its head. The code — not the model
— converts the dictated words to digits, at one chokepoint, so the live readback,
the stored booking phone, and the WhatsApp number can never diverge.
"""

from __future__ import annotations

import doctest

import pytest

import handover
from handover import normalize_whatsapp, spoken_number_to_digits


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("triple seven", "777"),
        ("treble two", "222"),
        ("double five", "55"),
        ("double oh", "00"),
        ("double o", "00"),
        ("nought", "0"),
        ("naught", "0"),
        ("oh", "0"),
        ("o", "0"),
        ("zero", "0"),
        ("nought seven six", "076"),
        # A fully-spoken number with mixed shorthand.
        ("oh seven one one seven five four double six eight", "0711754668"),
        ("nought seven six triple seven double five one", "076777551"),
        # Numerals pass straight through; already-expanded input is a no-op.
        ("0771234567", "0771234567"),
        ("0771 754 double 6 8", "0771754668"),
        ("", ""),
    ],
)
def test_spoken_number_to_digits(raw, expected):
    assert spoken_number_to_digits(raw) == expected


def test_normalize_whatsapp_now_understands_fully_spoken_numbers():
    # The parser strips word-digits no longer: a wholly-spoken local number
    # normalises to the same canonical E.164 form as its dialled equivalent.
    assert normalize_whatsapp(
        "oh seven one one seven five four double six eight"
    ) == "94711754668"
    assert normalize_whatsapp("0711754668") == "94711754668"
    # Wrong length is still rejected, never padded into a plausible wrong number.
    assert normalize_whatsapp("nought seven four two nine four four five one") == ""


def test_length_validation_rejects_wrong_counts():
    # Nine local digits after the trunk zero is valid; eight is not.
    assert normalize_whatsapp("oh seven seven four two nine four four five one") == "94774294451"
    assert normalize_whatsapp("oh seven four two nine four four five one") == ""


def test_handover_parser_doctests_still_pass():
    results = doctest.testmod(handover, verbose=False)
    assert results.failed == 0, f"{results.failed} handover doctest(s) failed"


def test_stored_booking_phone_derives_from_the_same_parser():
    # create_booking -> _resolve_stored_phone -> normalize_whatsapp, so the
    # booking phone is the SAME deterministic conversion as the live readback.
    from yanolja_service import _resolve_stored_phone

    assert _resolve_stored_phone(
        "oh seven one one seven five four double six eight"
    ) == "94711754668"
    # A wrong-length spoken number is not stored as a plausible wrong number;
    # it falls back to the caller's own line.
    assert _resolve_stored_phone(
        "oh seven four two nine four four five one", "+94711754668"
    ) == "94711754668"
    assert _resolve_stored_phone("oh seven four two nine four four five one", "") == ""
