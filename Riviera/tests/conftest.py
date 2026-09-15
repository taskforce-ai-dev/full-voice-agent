"""Shared Riya test configuration."""

import pytest


@pytest.fixture(autouse=True)
def canonical_english_voice_profile(monkeypatch):
    """Keep relay-route tests hermetic without weakening production fail-closed behavior."""
    monkeypatch.setenv("RIVIERA_EN_ELEVENLABS_VOICE_ID", "unit-test-canonical-voice")


@pytest.fixture(autouse=True)
def smartpbx_bilingual_menu_credential(monkeypatch):
    """Keep ordinary SmartPBX lifecycle tests on the configured menu path."""
    import server

    monkeypatch.setattr(server, "GEMINI_API_KEY", "test-gemini-key")


# Production defaults N8N_BASE_URL to blank -- fail closed, nothing is POSTed
# until Riviera's own n8n destination is configured (see
# test_external_endpoints_fail_closed.py). Tests that exercise the send path
# stub booking_api.get_session, so give them an explicit, clearly non-shared
# destination; the fail-closed tests override it back to "" themselves.
TEST_N8N_BASE_URL = "https://n8n.riviera.example"


@pytest.fixture(autouse=True)
def explicit_test_n8n_destination(monkeypatch):
    import handover
    import post_call

    monkeypatch.setattr(handover, "N8N_BASE_URL", TEST_N8N_BASE_URL)
    monkeypatch.setattr(post_call, "N8N_BASE_URL", TEST_N8N_BASE_URL)
    monkeypatch.setattr(post_call, "N8N_POSTCALL_WEBHOOK", "/webhook/post-call-data")
