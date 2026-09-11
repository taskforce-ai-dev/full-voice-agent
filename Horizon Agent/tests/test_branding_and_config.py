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


def test_sinhala_voice_primary_is_rime_arcana():
    """Primary Sinhala voice is Rime Arcana (speaker chandani)."""
    assert server.SI_TTS_PROVIDER == "rime"
    assert server.RIME_ARCANA_SPEAKER  # non-empty (e.g. 'chandani')
    assert server.RIME_ARCANA_URL.startswith("https://")
    assert server._rime_arcana_payload("hi")["modelId"] == "arcana"
    assert server._rime_arcana_payload("hi")["lang"] == "si"


def test_sinhala_voice_fallback_chain_configured():
    """Fallbacks after Arcana: Gemini TTS, then OpenAI TTS — both must be set."""
    assert server.GEMINI_TTS_MODEL.startswith("gemini-")
    assert "tts" in server.GEMINI_TTS_MODEL
    assert server.GEMINI_TTS_VOICE  # non-empty
    assert server.OPENAI_TTS_VOICE  # non-empty (e.g. 'sage')
    assert server.OPENAI_TTS_MODEL.startswith("gpt-")


# --- No legacy runtime content (Winrich/Hatton hotel) -------------------------

def test_kb_collection_is_horizon_not_winrich():
    import knowledge_base
    assert knowledge_base.COLLECTION_NAME == "horizon_kb"


def test_no_winrich_hotel_branding_in_runtime_modules():
    for name in ("server.py", "knowledge_base.py", "post_call.py", "tools.py"):
        text = (HORIZON / name).read_text(encoding="utf-8", errors="ignore")
        assert "Winrich" not in text, f"Winrich branding left in {name}"
        assert "winrich" not in text, f"winrich branding left in {name}"


def test_legacy_reference_files_removed():
    for gone in ("media_stream_server.py", "test_voice.py", "test_voice_elevenlabs.py"):
        assert not (HORIZON / gone).exists(), f"{gone} should have been removed"
