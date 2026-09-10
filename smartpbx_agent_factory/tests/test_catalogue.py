import json
from pathlib import Path

import pytest

from smartpbx_agent_factory.catalogue import CapabilityCatalogue, CatalogueError


CATALOGUE = Path(__file__).parents[1] / "template_v1" / "provider_catalogue.json"


def test_catalogue_rejects_unverified_language_locale_provider_model_pair():
    catalogue = CapabilityCatalogue.load(CATALOGUE)
    with pytest.raises(CatalogueError, match="not verified"):
        catalogue.validate_pipeline(
            "en",
            {
                "locale": "en-GB",
                "stt": "azure",
                "llm": "claude",
                "llm_model": "claude-sonnet-4-5-20250929",
                "tts": "elevenlabs",
                "tts_model": "eleven_flash_v2_5",
            },
        )


def test_catalogue_accepts_only_deployed_english_and_sinhala_profiles():
    catalogue = CapabilityCatalogue.load(CATALOGUE)
    catalogue.validate_pipeline(
        "en",
        {
            "locale": "en-US",
            "stt": "azure",
            "llm": "claude",
            "llm_model": "claude-sonnet-4-5-20250929",
            "tts": "elevenlabs",
            "tts_model": "eleven_flash_v2_5",
        },
    )
    catalogue.validate_pipeline(
        "si",
        {
            "locale": "si-LK",
            "stt": "azure",
            "llm": "gemini",
            "llm_model": "gemini-3.7-flash",
            "tts": "gemini",
            "tts_model": "gemini-3.1-flash-tts-preview",
        },
    )


@pytest.mark.parametrize(
    ("component", "model"),
    (("stt", "unapproved-stt"), ("llm", "unapproved-llm"), ("tts", "unapproved-tts")),
)
def test_catalogue_rejects_an_unapproved_provider_model_pair(component, model):
    catalogue = CapabilityCatalogue.load(CATALOGUE)
    pipeline = {
        "locale": "en-US",
        "stt": "azure",
        "llm": "claude",
        "llm_model": "claude-sonnet-4-5-20250929",
        "tts": "elevenlabs",
        "tts_model": "eleven_flash_v2_5",
    }
    pipeline[f"{component}_model"] = model
    with pytest.raises(CatalogueError, match="not verified"):
        catalogue.validate_pipeline("en", pipeline)


def test_catalogue_rejects_malformed_or_unknown_provider_data(tmp_path):
    malformed = tmp_path / "catalogue.json"
    malformed.write_text(
        json.dumps(
            {
                "version": 1,
                "catalogue_status": "approved",
                "source_revision": "a" * 40,
                "source_hashes": {"Kavya/server.py": "sha256:" + "b" * 64},
                "runtime_required_secret_identifiers": [],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(CatalogueError, match="languages"):
        CapabilityCatalogue.load(malformed)


def test_catalogue_exposes_named_secret_identifiers_without_secret_values():
    catalogue = CapabilityCatalogue.load(CATALOGUE)
    english = catalogue.required_secret_identifiers_for_pipeline(
        "en",
        {
            "locale": "en-US",
            "stt": "azure",
            "llm": "claude",
            "llm_model": "claude-sonnet-4-5-20250929",
            "tts": "elevenlabs",
            "tts_model": "eleven_flash_v2_5",
        },
    )
    sinhala = catalogue.required_secret_identifiers_for_pipeline(
        "si",
        {
            "locale": "si-LK",
            "stt": "azure",
            "llm": "gemini",
            "llm_model": "gemini-3.7-flash",
            "tts": "gemini",
            "tts_model": "gemini-3.1-flash-tts-preview",
        },
    )
    assert english == frozenset({"ANTHROPIC_API_KEY", "AZURE_SPEECH_KEY", "ELEVENLABS_API_KEY", "KAVYA_EN_ELEVENLABS_VOICE_ID", "SMARTPBX_WS_TOKEN"})
    assert sinhala == frozenset({"AZURE_SPEECH_KEY", "GEMINI_API_KEY", "SMARTPBX_WS_TOKEN"})


def test_catalogue_is_deeply_immutable_after_review():
    catalogue = CapabilityCatalogue.load(CATALOGUE)
    with pytest.raises(TypeError):
        catalogue.languages["fr"] = {}
    with pytest.raises(TypeError):
        catalogue.languages["en"]["en-US"]["stt"] = ()
