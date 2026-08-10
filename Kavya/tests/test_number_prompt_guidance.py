"""The system prompt must make spoken capture primary and keypad the fallback."""

from __future__ import annotations

import server


def test_prompt_directs_verbatim_relay_to_capture_spoken_number():
    prompt = server._build_system_prompt("en")
    low = prompt.lower()
    assert "capture_spoken_number" in prompt
    # Relay verbatim; do not convert in your head.
    assert "exactly as" in low or "verbatim" in low
    assert "read" in low and "back" in low
    # Explicitly forbid mental digit arithmetic.
    assert "in your head" in low or "yourself" in low


def test_prompt_names_treble_and_nought_in_the_shorthand_block():
    prompt = server._build_system_prompt("en")
    low = prompt.lower()
    assert "treble" in low
    assert "nought" in low
    assert "naught" in low


def test_keypad_is_worded_as_a_fallback_not_the_default():
    prompt = server._build_system_prompt("en")
    assert "ALWAYS USE THE KEYPAD" not in prompt, "keypad must no longer be the primary path"
    low = prompt.lower()
    assert "collect_number_via_keypad" in prompt
    capture_idx = low.index("capture_spoken_number")
    # The keypad appears only as a fallback offer.
    keypad_idx = low.index("collect_number_via_keypad")
    assert capture_idx < keypad_idx
    window = low[max(0, keypad_idx - 400): keypad_idx + 400]
    assert "fallback" in window or "if the caller" in window or "only if" in window
    assert "keypad" in window
    assert "fallback only" in window or "only if" in window or "repeated" in window
