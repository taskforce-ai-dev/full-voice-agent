"""Branding + configuration invariants for the Horizon (Vidya) demo.

Locks that the agent presents as Vidya / Horizon Airline & Aviation Academy in
every language, carries no caller-facing hotel/Tanya/Kavya branding, and ships
the Kavya-Dialog Sinhala stack defaults (Gemini brain + Gemini TTS) with an
OpenAI-TTS fallback.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

HORIZON = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HORIZON))

import server  # noqa: E402

_LANGS = ["en", "si", "ar", "ru"]
_FORBIDDEN = ["Tanya", "Hatton", "Treehouse", "Kavya", "hotel", "chalet"]


@pytest.mark.parametrize("lang", _LANGS)
def test_system_prompt_is_vidya_horizon(lang):
    prompt = server._build_system_prompt(lang)
    assert "Vidya" in prompt
    assert "Horizon" in prompt


@pytest.mark.parametrize("lang", _LANGS)
def test_system_prompt_has_no_hotel_branding(lang):
    prompt = server._build_system_prompt(lang)
    for bad in _FORBIDDEN:
        assert bad not in prompt, f"{bad!r} leaked into the {lang} system prompt"


def test_system_prompt_is_inquiry_only():
    """The persona must forbid bookings/payments and ground answers in the KB."""
    prompt = server._build_system_prompt("en")
    assert "INQUIRY" in prompt.upper()


def test_english_and_russian_greetings_are_vidya():
    assert "Vidya" in server.LANGUAGE_CONFIGS["en"]["welcome_greeting"]
    # Russian greeting names her in Cyrillic ("Видья")
    assert "Видья" in server.LANGUAGE_CONFIGS["ru"]["welcome_greeting"]
    assert "Horizon" in server.LANGUAGE_CONFIGS["ru"]["welcome_greeting"]


def test_media_stream_welcomes_cover_ar_and_si():
    for lang in ("ar", "si"):
        assert lang in server.MEDIA_STREAM_WELCOME
        assert "Horizon" in server.MEDIA_STREAM_WELCOME[lang]


def test_no_caller_facing_hotel_branding_in_greetings():
    blobs = [c["welcome_greeting"] for c in server.LANGUAGE_CONFIGS.values()]
    blobs += list(server.MEDIA_STREAM_WELCOME.values())
    for blob in blobs:
        for bad in ("Tanya", "Hatton", "Treehouse", "Kavya"):
            assert bad not in blob


# --- Sinhala = Kavya Dialog stack (Gemini brain + Gemini TTS) defaults --------

def test_sinhala_brain_defaults_to_gemini():
    assert server.SI_LLM_PROVIDER == "gemini"
    assert server.SI_GEMINI_MODEL  # non-empty


def test_sinhala_voice_defaults_to_gemini_tts():
    assert server.GEMINI_TTS_MODEL.startswith("gemini-")
    assert "tts" in server.GEMINI_TTS_MODEL
    assert server.GEMINI_TTS_VOICE  # non-empty


def test_openai_tts_fallback_configured():
    """The graceful-degradation path uses OpenAI TTS; its voice must be set."""
    assert server.OPENAI_TTS_VOICE  # non-empty (e.g. 'sage')
    assert server.OPENAI_TTS_MODEL.startswith("gpt-")
