"""Horizon is INQUIRY-ONLY — it exposes NO tools, as a hard invariant.

There is no environment variable, PMS credential, or other runtime state that
can turn booking/availability/cancellation/transfer tools on: `tools.py`'s
exporters are hard-wired to return `[]`. These tests lock that so a future edit
(or an inherited Yanolja key, or a stray env flag) cannot re-expose the
hotel-shaped booking tools on this aviation-academy line.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

HORIZON = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HORIZON))

import tools  # noqa: E402


def test_all_tool_exporters_return_empty():
    assert tools.get_tools() == []
    assert tools.get_tools_openai() == []
    assert tools.get_tools_gemini() == []


def test_no_env_flag_can_enable_tools(monkeypatch):
    """Set every plausible enabling flag AND a configured PMS; still no tools."""
    for var in ("HORIZON_ENABLE_BOOKING_TOOLS", "ENABLE_BOOKING_TOOLS",
                "HORIZON_TOOLS", "DEMO_SAFE_BOOKINGS"):
        monkeypatch.setenv(var, "true")
    t = importlib.reload(tools)
    # Even if the booking API reports configured, exporters stay empty.
    monkeypatch.setattr(t, "is_configured", lambda: True, raising=False)
    assert t.get_tools() == []
    assert t.get_tools_openai() == []
    assert t.get_tools_gemini() == []
    # restore clean module for later tests
    for var in ("HORIZON_ENABLE_BOOKING_TOOLS", "ENABLE_BOOKING_TOOLS",
                "HORIZON_TOOLS", "DEMO_SAFE_BOOKINGS"):
        monkeypatch.delenv(var, raising=False)
    importlib.reload(tools)


def test_exporters_take_no_arguments_and_are_pure():
    """Calling repeatedly always yields the same empty result — no hidden state."""
    for _ in range(3):
        assert tools.get_tools() == []
