"""The post-call extraction must anchor spoken dates to the real current date.

Without an anchor the extraction model guesses the year and picks the past
(a guest saying "27th of September" got "2025-09-27"). The live in-call prompt
already injects `Today's date is <iso>`; the post-call extraction must mirror it,
on both the primary and the retry paths, computed at call time so it is never
frozen. Shared by the Twilio and SmartPBX post-call flows.
"""

from __future__ import annotations

import re
from datetime import date
from types import SimpleNamespace

import pytest

import post_call


class RecordingClaude:
    """Fake Anthropic client that records the system prompt and answers with JSON."""

    def __init__(self, responder):
        self._responder = responder
        self.system_prompts: list[str] = []
        self.user_prompts: list[str] = []
        self.messages = SimpleNamespace(create=self._create)

    async def _create(self, *, system=None, messages, **_kwargs):
        if system is not None:
            self.system_prompts.append(system)
        self.user_prompts.append(messages[-1]["content"])
        return SimpleNamespace(content=[SimpleNamespace(text=self._responder(system, messages))])


def _static_json(_system, _messages):
    return '{"guest_name": "Test", "summary": "ok"}'


def test_extraction_system_prompt_builder_injects_the_given_date():
    prompt = post_call.build_extraction_system_prompt("2026-08-09")
    assert "Today's date is 2026-08-09." in prompt
    # It must steer the model away from a past year for a bare month/day.
    lowered = prompt.lower()
    assert "year" in lowered
    assert "past" in lowered


def test_retry_prompt_builder_injects_the_given_date():
    prompt = post_call.build_retry_prompt("2026-08-09")
    assert "Today's date is 2026-08-09." in prompt


@pytest.mark.asyncio
async def test_primary_extraction_anchors_todays_date(monkeypatch):
    client = RecordingClaude(_static_json)
    await post_call.extract_booking_details(
        "Transcript text", lang="en", llm_provider="claude",
        anthropic_client=client, model="m",
    )
    today = date.today().isoformat()
    assert client.system_prompts, "the primary extraction must send a system prompt"
    assert f"Today's date is {today}." in client.system_prompts[0]


@pytest.mark.asyncio
async def test_retry_extraction_anchors_todays_date(monkeypatch):
    calls = {"n": 0}

    def responder(system, _messages):
        calls["n"] += 1
        if calls["n"] == 1:
            return "not json at all"  # force the retry path
        return '{"guest_name": "Test", "summary": "ok"}'

    client = RecordingClaude(responder)
    await post_call.extract_booking_details(
        "Transcript text", lang="en", llm_provider="claude",
        anthropic_client=client, model="m",
    )
    today = date.today().isoformat()
    assert calls["n"] == 2, "the first parse must fail and trigger the retry"
    # The retry sends the prompt as a user message, not a system prompt.
    assert any(f"Today's date is {today}." in u for u in client.user_prompts), (
        "the retry prompt must carry the date anchor too"
    )


@pytest.mark.asyncio
async def test_spoken_date_resolves_to_the_next_occurrence_year(monkeypatch):
    """With the anchor present, a bare month/day resolves to the current/next year."""
    monkeypatch.setattr(post_call, "date", _FrozenDate(date(2026, 8, 9)))

    def responder(system, _messages):
        # Mimic a correctly-anchored model: read the injected date, then map the
        # spoken "27th of September" to the next such date on or after today.
        match = re.search(r"Today's date is (\d{4})-(\d{2})-(\d{2})\.", system)
        year = int(match.group(1))
        anchor = date(year, int(match.group(2)), int(match.group(3)))
        candidate = date(anchor.year, 9, 27)
        if candidate < anchor:
            candidate = date(anchor.year + 1, 9, 27)
        return f'{{"check_in": "{candidate.isoformat()}", "summary": "ok"}}'

    client = RecordingClaude(responder)
    result = await post_call.extract_booking_details(
        "Guest wants a room on the 27th of September.",
        lang="en", llm_provider="claude", anthropic_client=client, model="m",
    )
    assert result["check_in"] == "2026-09-27", "must not pick a past year"


class _FrozenDate:
    def __init__(self, value):
        self._value = value

    def today(self):
        return self._value
