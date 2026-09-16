"""Legacy-versus-explicit SmartPBX STT factory compatibility contracts."""

import pytest

import server


def test_omitted_provider_keeps_legacy_azure_construction_with_blank_key(monkeypatch):
    """Task 2 strictness applies only when a provider is explicitly requested."""
    monkeypatch.setattr(server, "STT_PROVIDER", "azure")
    monkeypatch.setattr(server, "AZURE_STT_AVAILABLE", True)
    monkeypatch.setattr(server, "audioop", object())
    monkeypatch.setattr(server, "AZURE_SPEECH_KEY", "  ")

    stream = server._make_stt(lambda _text: None, lambda _text: None, "en")

    assert isinstance(stream, server.AzureSTTStream)


def test_explicit_azure_fail_closed_remains_strict_with_blank_key(monkeypatch):
    monkeypatch.setattr(server, "AZURE_STT_AVAILABLE", True)
    monkeypatch.setattr(server, "audioop", object())
    monkeypatch.setattr(server, "AZURE_SPEECH_KEY", "  ")

    with pytest.raises(RuntimeError, match="requested STT provider unavailable"):
        server._make_stt(
            lambda _text: None, lambda _text: None, "si",
            provider="azure", fail_closed=True,
        )


def test_explicit_azure_non_fail_closed_keeps_google_fallback_with_blank_key(monkeypatch):
    monkeypatch.setattr(server, "AZURE_STT_AVAILABLE", True)
    monkeypatch.setattr(server, "audioop", object())
    monkeypatch.setattr(server, "AZURE_SPEECH_KEY", "  ")

    stream = server._make_stt(
        lambda _text: None, lambda _text: None, "en",
        provider="azure", fail_closed=False,
    )

    assert isinstance(stream, server.GoogleSTTStream)
