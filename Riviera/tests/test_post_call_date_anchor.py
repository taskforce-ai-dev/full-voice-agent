"""The post-call extraction must anchor spoken dates to the real current date.

Without an anchor the extraction model guesses the year and picks the past
(a guest saying "27th of September" got "2025-09-27"). The live in-call prompt
already injects `Today's date is <iso>`; the post-call extraction must mirror it,
on both the primary and the retry paths, computed at call time so it is never
frozen. Shared by the Twilio and SmartPBX post-call flows.
"""

from __future__ import annotations

import logging
import re
from datetime import date
from types import SimpleNamespace

import pytest

import post_call


def _extract_with_text_blocks(blocks: list[SimpleNamespace]) -> SimpleNamespace:
    return SimpleNamespace(content=blocks)


class _FakeAnthropicClient:
    def __init__(self, content_blocks: list[SimpleNamespace]):
        self._response = _extract_with_text_blocks(content_blocks)
        self.messages = SimpleNamespace(create=self._create)

    async def _create(self, **_kwargs):
        return self._response


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
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=self._responder(system, messages))]
        )


def _static_json(_system, _messages):
    return '{"guest_name": "Test", "summary": "ok"}'


def _text_block(content: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=content)


def _non_text_block() -> SimpleNamespace:
    return SimpleNamespace(type="tool_use", id="tool-1")


def test_extraction_system_prompt_builder_injects_the_given_date():
    prompt = post_call.build_extraction_system_prompt("2026-08-09")
    assert "Today's date is 2026-08-09." in prompt
    # It must steer the model away from a past year for a bare month/day.
    lowered = prompt.lower()
    assert "year" in lowered
    assert "past" in lowered
    assert "CRITICAL RULES FOR guest_name" in prompt
    assert "create_booking" in prompt
    assert "spelling" in prompt


def test_retry_prompt_builder_injects_the_given_date():
    prompt = post_call.build_retry_prompt("2026-08-09")
    assert "Today's date is 2026-08-09." in prompt
    assert "System marker" in prompt
    assert "copy guest_name from the marker's guest_name field" in prompt


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


@pytest.mark.asyncio
async def test_non_dict_extraction_payload_triggers_retry_path(monkeypatch, caplog):
    client = _FakeAnthropicClient([_text_block("[{\"ok\": false}]")])
    calls = {"retry_count": 0}

    async def _retry(*_args, **_kwargs):
        calls["retry_count"] += 1
        return {"guest_name": "Retry Name", "summary": "Recovered."}

    monkeypatch.setattr(post_call, "_retry_extraction", _retry)
    with caplog.at_level(logging.WARNING):
        result = await post_call.extract_booking_details(
            "Transcript text", lang="en", llm_provider="claude",
            anthropic_client=client, model="m", privacy_safe=True,
        )
    assert calls["retry_count"] == 1
    assert result["guest_name"] == "Retry Name"
    assert "smartpbx_post_call event=extraction_failed" not in caplog.text


@pytest.mark.asyncio
async def test_later_text_block_is_used_for_claude_extraction():
    client = _FakeAnthropicClient([_non_text_block(), _text_block(
        '{"guest_name": "Later Block", "summary": "ok"}',
    )])
    result = await post_call._extract_with_claude(
        client=client, transcript_text="txt", model="m", system_prompt="sys",
    )
    assert result["guest_name"] == "Later Block"


@pytest.mark.asyncio
async def test_privacy_safe_extraction_failure_logs_exception_type(caplog):
    client = _FakeAnthropicClient([_non_text_block()])
    with caplog.at_level(logging.ERROR):
        result = await post_call.extract_booking_details(
            "Transcript text", lang="en", llm_provider="claude",
            anthropic_client=client, model="m", privacy_safe=True,
        )
    assert "smartpbx_post_call event=extraction_failed exc_type=ValueError" in caplog.text
    assert result["_extraction_error"] == "extraction_failed"


class _FrozenDate:
    def __init__(self, value):
        self._value = value

    def today(self):
        return self._value
