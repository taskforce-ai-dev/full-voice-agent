"""The collect_number_via_keypad tool and its prompt guidance.

Keypad (DTMF) entry is the fallback path when spoken capture fails or the
caller asks to key in the number. The tool is declared in all three provider
formats; its real execution is intercepted at the session level (it needs the
live call), so execute_tool returns a graceful fallback for paths without a
collector.
"""

from __future__ import annotations

import json

import pytest

import tools


def _configured(monkeypatch):
    monkeypatch.setattr(tools, "is_configured", lambda: True)


def test_tool_is_declared_in_the_shared_definitions():
    names = [t["name"] for t in tools.TOOL_DEFINITIONS]
    assert "collect_number_via_keypad" in names
    tool = next(t for t in tools.TOOL_DEFINITIONS if t["name"] == "collect_number_via_keypad")
    props = tool["input_schema"]["properties"]
    assert "label" in props
    assert tool["input_schema"]["required"] == ["label"]
    desc = tool["description"].lower()
    assert "fallback" in desc or "repeated" in desc
    assert "always use" not in desc
    assert "keypad" in desc
    assert "number" in desc


def test_tool_is_present_in_every_provider_format(monkeypatch):
    _configured(monkeypatch)
    assert any(t["name"] == "collect_number_via_keypad" for t in tools.get_tools())
    assert any(
        t["function"]["name"] == "collect_number_via_keypad" for t in tools.get_tools_openai()
    )
    gemini = tools.get_tools_gemini()[0]["function_declarations"]
    assert any(t["name"] == "collect_number_via_keypad" for t in gemini)


@pytest.mark.asyncio
async def test_execute_tool_returns_a_graceful_fallback_without_a_collector():
    # execute_tool is the non-session path (e.g. Twilio, unwired this batch): it
    # must degrade gracefully so the LLM asks the guest to say the number.
    result = json.loads(await tools.execute_tool("collect_number_via_keypad", {"label": "phone"}))
    assert result["status"] in {"unavailable", "no_input"}


def test_system_prompt_guides_keypad_capture_for_numbers():
    import server

    prompt = server._build_system_prompt("en")
    assert "collect_number_via_keypad" in prompt
    low = prompt.lower()
    assert "keypad" in low
    # It must steer away from spoken digits for numbers.
    assert "phone" in low or "whatsapp" in low
