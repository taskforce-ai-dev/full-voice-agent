"""Fail-closed configuration contract for the Yanolja HTTP client."""

import asyncio
import importlib

import pytest

import yanolja_client


def test_missing_yanolja_credentials_disable_login_without_a_network_session(monkeypatch):
    monkeypatch.delenv("YANOLJA_USERNAME", raising=False)
    monkeypatch.delenv("YANOLJA_PASSWORD", raising=False)
    client = importlib.reload(yanolja_client)
    try:
        assert client.is_configured() is False
        with pytest.raises(client.YanoljaError, match="not configured"):
            asyncio.run(client.login())
        assert client._session is None
    finally:
        monkeypatch.undo()
        importlib.reload(client)
