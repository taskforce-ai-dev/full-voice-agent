import json
from pathlib import Path

import pytest

from smartpbx_agent_factory.catalogue import CapabilityCatalogue, CatalogueError


CATALOGUE = Path(__file__).parents[1] / "template_v1" / "provider_catalogue.json"


def test_catalogue_rejects_unverified_language_provider_pair():
    catalogue = approved_catalogue(CATALOGUE)
    with pytest.raises(CatalogueError, match="not verified"):
        catalogue.validate_pipeline("si", {"stt": "unverified", "llm": "claude", "tts": "elevenlabs"})


def test_catalogue_accepts_an_explicitly_listed_pipeline():
    catalogue = approved_catalogue(CATALOGUE)
    catalogue.validate_pipeline("en-US", {"stt": "deepgram", "llm": "claude", "tts": "elevenlabs"})


def test_catalogue_rejects_malformed_or_unknown_provider_data(tmp_path):
    malformed = tmp_path / "catalogue.json"
    malformed.write_text(json.dumps({"version": 1}), encoding="utf-8")
    with pytest.raises(CatalogueError, match="languages"):
        CapabilityCatalogue.load(malformed)


def approved_catalogue(path):
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["catalogue_status"] = "approved"
    approved = path.parent / (path.stem + "-approved.json")
    approved.write_text(json.dumps(raw), encoding="utf-8")
    return CapabilityCatalogue.load(approved)


def test_checked_in_fixture_catalogue_is_blocked_until_reviewed():
    with pytest.raises(CatalogueError, match="approved"):
        CapabilityCatalogue.load(CATALOGUE)
