"""Blank-string trap contract for DEMO_RATES_ENABLED (P1-4).

A key present but blank -- e.g. an unset compose ``${DEMO_RATES_ENABLED:-true}``
passthrough -- must resolve exactly like a missing key. Before this fix,
``os.getenv("DEMO_RATES_ENABLED", "true").lower() == "true"`` silently turned
DEMO_RATES_ENABLED off whenever the key was present but empty, dropping the
rate card out of tool results and the system prompt with no crash to notice.
"""
from __future__ import annotations

import importlib

import yanolja_service


def test_blank_demo_rates_enabled_falls_back_to_true_like_absent(monkeypatch):
    monkeypatch.setenv("DEMO_RATES_ENABLED", "")
    module = importlib.reload(yanolja_service)
    try:
        assert module.DEMO_RATES_ENABLED is True
    finally:
        monkeypatch.undo()
        importlib.reload(module)


def test_absent_demo_rates_enabled_still_defaults_to_true(monkeypatch):
    monkeypatch.delenv("DEMO_RATES_ENABLED", raising=False)
    module = importlib.reload(yanolja_service)
    try:
        assert module.DEMO_RATES_ENABLED is True
    finally:
        monkeypatch.undo()
        importlib.reload(module)


def test_explicit_false_still_disables_demo_rates(monkeypatch):
    monkeypatch.setenv("DEMO_RATES_ENABLED", "false")
    module = importlib.reload(yanolja_service)
    try:
        assert module.DEMO_RATES_ENABLED is False
    finally:
        monkeypatch.undo()
        importlib.reload(module)
