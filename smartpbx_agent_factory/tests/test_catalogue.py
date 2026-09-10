import json
from pathlib import Path

import pytest

from smartpbx_agent_factory.catalogue import CapabilityCatalogue, CatalogueError


CATALOGUE = Path(__file__).parent / "fixtures" / "approved-provider-catalogue.json"
BLOCKED_CATALOGUE = Path(__file__).parents[1] / "template_v1" / "provider_catalogue.json"


def test_catalogue_rejects_unverified_language_locale_provider_model_pair():
    catalogue = CapabilityCatalogue.load(CATALOGUE)
    with pytest.raises(CatalogueError, match="not verified"):
        catalogue.validate_pipeline(
            "en-US",
            {
                "locale": "en-GB",
                "stt": "deepgram",
                "stt_model": "nova-3",
                "llm": "claude",
                "llm_model": "claude-sonnet-4-6",
                "tts": "elevenlabs",
                "tts_model": "eleven_turbo_v2_5",
            },
        )


def test_catalogue_accepts_an_explicitly_listed_pipeline():
    catalogue = CapabilityCatalogue.load(CATALOGUE)
    catalogue.validate_pipeline(
        "en-US",
        {
            "locale": "en-US",
            "stt": "deepgram",
            "stt_model": "nova-3",
            "llm": "claude",
            "llm_model": "claude-sonnet-4-6",
            "tts": "elevenlabs",
            "tts_model": "eleven_turbo_v2_5",
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
        "stt": "deepgram",
        "stt_model": "nova-3",
        "llm": "claude",
        "llm_model": "claude-sonnet-4-6",
        "tts": "elevenlabs",
        "tts_model": "eleven_turbo_v2_5",
    }
    pipeline[f"{component}_model"] = model
    with pytest.raises(CatalogueError, match="not verified"):
        catalogue.validate_pipeline("en-US", pipeline)


def test_catalogue_rejects_malformed_or_unknown_provider_data(tmp_path):
    malformed = tmp_path / "catalogue.json"
    malformed.write_text(json.dumps({"version": 1}), encoding="utf-8")
    with pytest.raises(CatalogueError, match="languages"):
        CapabilityCatalogue.load(malformed)


def test_checked_in_fixture_catalogue_is_blocked_until_reviewed():
    with pytest.raises(CatalogueError, match="approved"):
        CapabilityCatalogue.load(BLOCKED_CATALOGUE)


def test_catalogue_is_deeply_immutable_after_review():
    catalogue = CapabilityCatalogue.load(CATALOGUE)
    with pytest.raises(TypeError):
        catalogue.languages["fr-FR"] = {}
    with pytest.raises(TypeError):
        catalogue.languages["en-US"]["en-US"]["stt"] = ()
