"""Horizon is INQUIRY-ONLY — no booking/availability/cancellation/transfer tools,
BY CONSTRUCTION.

The HattonHills base gates tools on `is_configured()` (a Yanolja/PMS key being
present). Horizon must NOT: its deploy reuses a shared `.env` that may carry a
Yanolja key, and relying on a secret being *absent* would silently arm booking
tools. These tests lock the invariant: even with the PMS "configured", all three
tool exporters return an empty list unless a human explicitly opted in via
`HORIZON_ENABLE_BOOKING_TOOLS=true`.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

HORIZON = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HORIZON))

import tools  # noqa: E402


def _reload_tools(monkeypatch, *, opt_in: str | None, configured: bool):
    """Reload tools.py under a given env so the module-level opt-in constant is
    recomputed, then force is_configured() to the requested value."""
    if opt_in is None:
        monkeypatch.delenv("HORIZON_ENABLE_BOOKING_TOOLS", raising=False)
    else:
        monkeypatch.setenv("HORIZON_ENABLE_BOOKING_TOOLS", opt_in)
    mod = importlib.reload(tools)
    monkeypatch.setattr(mod, "is_configured", lambda: configured)
    return mod


def test_no_tools_when_pms_configured_but_not_opted_in(monkeypatch):
    """The hostile/inherited case: a Yanolja key IS present (is_configured True)
    but nobody opted booking tools in. All exporters must still be empty."""
    t = _reload_tools(monkeypatch, opt_in=None, configured=True)
    assert t.get_tools() == []
    assert t.get_tools_openai() == []
    assert t.get_tools_gemini() == []


def test_no_tools_even_when_opt_in_but_not_configured(monkeypatch):
    """Opt-in alone is not enough — a real PMS must also be configured. This is
    the belt-and-suspenders half; the demo sets neither."""
    t = _reload_tools(monkeypatch, opt_in="true", configured=False)
    assert t.get_tools() == []
    assert t.get_tools_openai() == []
    assert t.get_tools_gemini() == []


def test_default_env_yields_no_tools(monkeypatch):
    """With nothing set at all (the demo's actual state), no tools."""
    t = _reload_tools(monkeypatch, opt_in=None, configured=False)
    assert t.get_tools() == []
    assert t.get_tools_openai() == []
    assert t.get_tools_gemini() == []


def test_tools_require_both_opt_in_and_configured(monkeypatch):
    """Positive control documenting the exact contract: tools appear ONLY when
    a human explicitly opts in AND a PMS is configured — proving inquiry-only is
    a deliberate two-key decision, never an accident of an inherited secret."""
    t = _reload_tools(monkeypatch, opt_in="true", configured=True)
    assert t.get_tools()  # non-empty
    assert t.get_tools_openai()
    assert t.get_tools_gemini()


def test_restore_default_module_state(monkeypatch):
    """Reload once more with a clean env so later test modules import the
    default (inquiry-only) tools module, not a state left armed by the positive
    control above."""
    t = _reload_tools(monkeypatch, opt_in=None, configured=False)
    assert t.get_tools() == []
