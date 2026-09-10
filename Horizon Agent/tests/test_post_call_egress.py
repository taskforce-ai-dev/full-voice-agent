"""Post-call is FAIL-CLOSED by default: no LLM extraction, no n8n webhook, no
dashboard dispatch.

The HattonHills base only skipped the n8n POST under DEMO_SAFE_BOOKINGS; LLM
extraction still ran and a configured dashboard client could still dispatch.
Horizon gates ALL post-call egress on the single demo-safe switch (default on),
so an inherited DASHBOARD/N8N config can never ship a caller transcript.
"""
from __future__ import annotations

import asyncio
import importlib
import sys
from pathlib import Path

HORIZON = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HORIZON))

import post_call  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


def _call_process(pc):
    return _run(pc.process_post_call_data(
        call_sid="CA_test",
        lang="en",
        caller_phone="+94770000000",
        full_transcript=[{"role": "user", "text": "hi"},
                         {"role": "assistant", "text": "Welcome to Horizon."}],
        call_start_time="2026-09-10T00:00:00",
        call_end_time="2026-09-10T00:01:00",
        llm_provider="claude",
    ))


def test_demo_safe_default_blocks_all_egress(monkeypatch):
    """Default env (DEMO_SAFE_BOOKINGS unset -> treated as true): extraction,
    n8n POST and dashboard dispatch must ALL be skipped."""
    monkeypatch.delenv("DEMO_SAFE_BOOKINGS", raising=False)
    pc = importlib.reload(post_call)

    calls = {"extract": 0, "n8n": 0}

    async def _spy_extract(*a, **k):
        calls["extract"] += 1
        return {}

    async def _spy_n8n(*a, **k):
        calls["n8n"] += 1

    class _SpyDashboard:
        def __init__(self):
            self.sent = 0

        async def send_call_completed(self, *a, **k):
            self.sent += 1

    spy_dash = _SpyDashboard()
    monkeypatch.setattr(pc, "extract_booking_details", _spy_extract)
    monkeypatch.setattr(pc, "_post_to_n8n", _spy_n8n)
    monkeypatch.setattr(pc, "dashboard_client", spy_dash)

    _call_process(pc)

    assert calls["extract"] == 0, "LLM extraction must not run in demo-safe mode"
    assert calls["n8n"] == 0, "n8n POST must not run in demo-safe mode"
    assert spy_dash.sent == 0, "dashboard dispatch must not run in demo-safe mode"


def test_demo_safe_helper_defaults_true(monkeypatch):
    monkeypatch.delenv("DEMO_SAFE_BOOKINGS", raising=False)
    pc = importlib.reload(post_call)
    assert pc._demo_safe() is True


def test_demo_safe_can_be_disabled_explicitly(monkeypatch):
    monkeypatch.setenv("DEMO_SAFE_BOOKINGS", "false")
    pc = importlib.reload(post_call)
    assert pc._demo_safe() is False
    # restore default for later modules
    monkeypatch.delenv("DEMO_SAFE_BOOKINGS", raising=False)
    importlib.reload(post_call)
