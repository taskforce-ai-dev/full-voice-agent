"""Post-call egress is FAIL-CLOSED: nothing leaves the process unless
HORIZON_POST_CALL_EGRESS is set to EXACTLY 'enabled'.

Covers the whole pipeline (LLM extraction + n8n + dashboard) being suppressed
by default, and the fail-closed semantics of the flag (false/0/typos/unknown
values must NOT enable egress).
"""
from __future__ import annotations

import asyncio
import importlib
import sys
from pathlib import Path

import pytest

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


def test_default_blocks_all_egress(monkeypatch):
    monkeypatch.delenv("HORIZON_POST_CALL_EGRESS", raising=False)
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

    assert calls["extract"] == 0
    assert calls["n8n"] == 0
    assert spy_dash.sent == 0


@pytest.mark.parametrize("value", ["false", "0", "no", "true", "1", "yes",
                                   "ENABLED ", "enable", "enabledx", "", "disabled", "TRUE"])
def test_fail_closed_for_non_exact_values(monkeypatch, value):
    """Only the exact token 'enabled' (case-insensitive, trimmed) turns egress
    on. Everything else — including 'true'/'1' and typos — stays closed."""
    monkeypatch.setenv("HORIZON_POST_CALL_EGRESS", value)
    pc = importlib.reload(post_call)
    # "ENABLED " trims+lowercases to "enabled" -> enabled; that is the ONE
    # affirmative case in this list.
    expected = value.strip().lower() == "enabled"
    assert pc.egress_enabled() is expected
    assert pc._demo_safe() is (not expected)


def test_exact_enabled_opens_egress(monkeypatch):
    monkeypatch.setenv("HORIZON_POST_CALL_EGRESS", "enabled")
    pc = importlib.reload(post_call)
    assert pc.egress_enabled() is True
    assert pc._demo_safe() is False
    monkeypatch.delenv("HORIZON_POST_CALL_EGRESS", raising=False)
    importlib.reload(post_call)
