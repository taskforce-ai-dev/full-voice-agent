"""The capture_spoken_number tool: relay-verbatim in, deterministic digits out."""

from __future__ import annotations

import json

import pytest

import tools
import handover


def _configured(monkeypatch):
    monkeypatch.setattr(tools, "is_configured", lambda: True)


def test_tool_declared_with_a_verbatim_spoken_field():
    tool = next(
        (t for t in tools.TOOL_DEFINITIONS if t["name"] == "capture_spoken_number"), None
    )
    assert tool is not None, "capture_spoken_number must be a declared tool"
    props = tool["input_schema"]["properties"]
    assert "spoken" in props
    assert tool["input_schema"]["required"] == ["spoken"]
    desc = tool["description"].lower()
    assert "primary" in desc and "default" in desc
    assert "first" in desc
    assert "exactly" in desc or "verbatim" in desc
    assert "double" in desc and "triple" in desc  # tells the model NOT to pre-convert


def test_tool_present_in_every_provider_format(monkeypatch):
    _configured(monkeypatch)
    assert any(t["name"] == "capture_spoken_number" for t in tools.get_tools())
    assert any(
        t["function"]["name"] == "capture_spoken_number" for t in tools.get_tools_openai()
    )
    gemini = tools.get_tools_gemini()[0]["function_declarations"]
    assert any(t["name"] == "capture_spoken_number" for t in gemini)
    assert any(t["name"] == "capture_spoken_name" for t in tools.get_tools())
    assert any(
        t["function"]["name"] == "capture_spoken_name" for t in tools.get_tools_openai()
    )
    assert any(
        t["name"] == "capture_spoken_name"
        for t in tools.get_tools_gemini()[0]["function_declarations"]
    )


@pytest.mark.asyncio
async def test_capture_returns_deterministic_digits_and_readback():
    result = json.loads(await tools.execute_tool(
        "capture_spoken_number",
        {"spoken": "oh seven one one seven five four double six eight", "label": "WhatsApp"},
    ))
    assert result["status"] == "captured"
    assert result["digits"] == "0711754668"
    assert result["readback"] == "0 7 1 1 7 5 4 6 6 8"
    assert result["length"] == 10
    assert result["valid"] is True
    assert result["normalized"] == "94711754668"


@pytest.mark.asyncio
async def test_capture_flags_a_wrong_length_number_as_invalid():
    token = handover.handover_context.set({})
    try:
        result = json.loads(await tools.execute_tool(
            "capture_spoken_number",
            {"spoken": "oh seven four two nine four four five one"},  # 8 local digits
        ))
        assert result["valid"] is False
        assert result["status"] == "needs_more"
        assert result["attempts"] == 1
        assert result["fallback_allowed"] is False
        assert result["readback"] == ""
        # It keeps the stream open rather than failing fast.
        assert result["digits"] == "074294451"
    finally:
        handover.handover_context.reset(token)


@pytest.mark.asyncio
async def test_capture_handles_triple_and_nought():
    # 9 digits do not normalize to a valid Sri Lankan number: the accumulation
    # contract keeps collecting (no readback of a wrong number) instead of the
    # old immediate readback.
    result = json.loads(await tools.execute_tool(
        "capture_spoken_number", {"spoken": "nought seven six triple seven double five one"},
    ))
    assert result["digits"] == "076777551"
    assert result["status"] == "needs_more"
    assert result["readback"] == ""


@pytest.mark.asyncio
async def test_capture_full_number_with_triple_and_nought_reads_back():
    result = json.loads(await tools.execute_tool(
        "capture_spoken_number",
        {"spoken": "nought seven six triple seven double five one zero"},
    ))
    assert result["status"] == "captured"
    assert result["digits"] == "0767775510"
    assert result["readback"] == "0 7 6 7 7 7 5 5 1 0"
    assert result["normalized"] == "94767775510"


@pytest.mark.asyncio
async def test_capture_with_no_input_is_invalid_not_a_crash():
    token = handover.handover_context.set({})
    try:
        result = json.loads(await tools.execute_tool("capture_spoken_number", {}))
        assert result["valid"] is False
        assert result["status"] == "needs_more"
        assert result["readback"] == ""
        assert result["digits"] == ""
        assert result["attempts"] == 1
    finally:
        handover.handover_context.reset(token)


def _handover_ctx_token():
    return handover.handover_context.set({})


@pytest.mark.asyncio
async def test_capture_spoken_number_accumulates_three_short_fragments():
    token = _handover_ctx_token()
    try:
        first = json.loads(await tools.execute_tool("capture_spoken_number", {
            "spoken": "zero seven one",
        }))
        assert first["status"] == "needs_more"
        assert first["digits"] == "071"
        assert first["attempts"] == 1
        assert first["fallback_allowed"] is False
        assert first["readback"] == ""

        second = json.loads(await tools.execute_tool("capture_spoken_number", {
            "spoken": "one seven five",
        }))
        assert second["status"] == "needs_more"
        assert second["digits"] == "071175"
        assert second["attempts"] == 2
        assert second["fallback_allowed"] is True
        assert second["readback"] == ""

        third = json.loads(await tools.execute_tool("capture_spoken_number", {
            "spoken": "four six six eight",
        }))
        assert third["status"] == "captured"
        assert third["digits"] == "0711754668"
        assert third["readback"] == "0 7 1 1 7 5 4 6 6 8"
        assert third["fallback_allowed"] is True
    finally:
        handover.handover_context.reset(token)


@pytest.mark.asyncio
async def test_capture_spoken_number_offers_fallback_only_after_two_full_failed_attempts():
    token = _handover_ctx_token()
    try:
        first = json.loads(await tools.execute_tool(
            "capture_spoken_number",
            {"spoken": "three two one"},
        ))
        assert first["fallback_allowed"] is False
        second = json.loads(await tools.execute_tool(
            "capture_spoken_number",
            {"spoken": "four"},
        ))
        assert second["fallback_allowed"] is True
    finally:
        handover.handover_context.reset(token)
