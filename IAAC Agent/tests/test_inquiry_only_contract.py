"""IAAC must remain inquiry-only even under a hostile inherited environment."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import tools


def test_no_provider_can_expose_transactional_tools(monkeypatch):
    """An accidentally configured PMS must not arm hotel or transfer tools."""
    monkeypatch.setattr(tools, "is_configured", lambda: True)

    assert tools.get_tools() == []
    assert tools.get_tools_openai() == []
    assert tools.get_tools_gemini() == []
