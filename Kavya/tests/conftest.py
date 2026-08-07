"""Shared Kavya test configuration."""

import pytest


@pytest.fixture(autouse=True)
def canonical_english_voice_profile(monkeypatch):
    """Keep relay-route tests hermetic without weakening production fail-closed behavior."""
    monkeypatch.setenv("KAVYA_EN_ELEVENLABS_VOICE_ID", "unit-test-canonical-voice")
