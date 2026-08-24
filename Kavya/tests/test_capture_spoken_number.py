"""The capture_spoken_number tool: relay-verbatim in, deterministic digits out.

State policy: an attempt that does not normalise to a usable number is
DISCARDED. No digits survive a tool call, so a mis-heard attempt can never be
concatenated onto the next one and a correction phrase ("double six at the end")
is never applied as a positional edit to stored digits — the tool asks for the
complete number again. Fragments of ONE dictation are still combined by capture
mode before dispatch, so a genuine multi-part number reaches the tool as a
single spoken string (see test_capture_spoken_override_paths.py).
"""

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
async def test_capture_flags_a_wrong_length_number_as_needing_a_fresh_attempt():
    token = handover.handover_context.set({})
    try:
        result = json.loads(await tools.execute_tool(
            "capture_spoken_number",
            {"spoken": "oh seven four two nine four four five one"},  # 8 local digits
        ))
        assert result["valid"] is False
        # Keep the established follow-up status for every runner, while the
        # contents of this attempt are still discarded before the next call.
        assert result["status"] == "needs_more"
        assert result["attempts"] == 1
        assert result["fallback_allowed"] is False
        assert result["readback"] == ""
        # The attempt is reported but not retained: the next call starts clean.
        assert result["digits"] == "074294451"
    finally:
        handover.handover_context.reset(token)


@pytest.mark.asyncio
async def test_capture_handles_triple_and_nought():
    # 9 digits do not normalize to a valid Sri Lankan number: the attempt is
    # discarded and re-asked, never read back as a wrong number.
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
async def test_capture_with_no_input_needs_a_fresh_attempt_not_a_crash():
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


# --- inline double / triple, every digit -----------------------------------

DIGIT_WORDS = [
    ("zero", "0"), ("one", "1"), ("two", "2"), ("three", "3"), ("four", "4"),
    ("five", "5"), ("six", "6"), ("seven", "7"), ("eight", "8"), ("nine", "9"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("word,digit", DIGIT_WORDS)
async def test_inline_double_expands_for_every_digit(word, digit):
    """'double <digit>' inside the current complete number is expanded, not dropped."""
    token = _handover_ctx_token()
    try:
        result = json.loads(await tools.execute_tool("capture_spoken_number", {
            "spoken": f"zero seven seven one two three four five double {word}",
        }))
        assert result["digits"] == "07712345" + digit * 2
        assert result["status"] == "captured"
        assert result["valid"] is True
        assert result["normalized"] == "947712345" + digit * 2
        assert result["readback"] == " ".join("07712345" + digit * 2)
    finally:
        handover.handover_context.reset(token)


@pytest.mark.asyncio
@pytest.mark.parametrize("word,digit", DIGIT_WORDS)
async def test_inline_triple_expands_for_every_digit(word, digit):
    token = _handover_ctx_token()
    try:
        result = json.loads(await tools.execute_tool("capture_spoken_number", {
            "spoken": f"zero seven seven one two three four triple {word}",
        }))
        assert result["digits"] == "0771234" + digit * 3
        assert result["status"] == "captured"
        assert result["normalized"] == "94771234" + digit * 3
    finally:
        handover.handover_context.reset(token)


# --- each attempt stands alone --------------------------------------------

@pytest.mark.asyncio
async def test_a_failed_attempt_contributes_no_digits_to_a_later_tool_call():
    """Production defect: a mis-heard attempt used to be concatenated onto the next."""
    token = _handover_ctx_token()
    try:
        first = json.loads(await tools.execute_tool("capture_spoken_number", {
            "spoken": "zero seven seven one two three",
        }))
        assert first["status"] != "captured"
        assert first["valid"] is False
        assert first["readback"] == "", "an unusable attempt is never read back"

        second = json.loads(await tools.execute_tool("capture_spoken_number", {
            "spoken": "four five six eight",
        }))
        assert second["digits"] == "4568", (
            "the later call must carry only its own digits, never the discarded attempt"
        )
        assert second["status"] != "captured"
        assert second["valid"] is False

        third = json.loads(await tools.execute_tool("capture_spoken_number", {
            "spoken": "zero seven seven one two three four five six eight",
        }))
        assert third["status"] == "captured", "a later valid full number is accepted alone"
        assert third["digits"] == "0771234568"
        assert third["normalized"] == "94771234568"
        assert third["readback"] == "0 7 7 1 2 3 4 5 6 8"
    finally:
        handover.handover_context.reset(token)


@pytest.mark.asyncio
async def test_a_positional_correction_is_never_applied_to_a_stored_attempt():
    """'double six at the end' is a correction phrase, not a patch instruction."""
    token = _handover_ctx_token()
    try:
        attempt = json.loads(await tools.execute_tool("capture_spoken_number", {
            "spoken": "nought seven six triple seven double five one",
        }))
        assert attempt["status"] != "captured"
        assert attempt["readback"] == ""

        correction = json.loads(await tools.execute_tool("capture_spoken_number", {
            "spoken": "double six at the end",
        }))
        assert correction["status"] != "captured"
        assert correction["valid"] is False
        assert correction["normalized"] == ""
        assert correction["digits"] == "66", (
            "the correction is parsed on its own; it must not be spliced into 076777551"
        )
        assert correction["readback"] == ""
        message = str(correction.get("message", "")).lower()
        assert "again" in message
        assert "whole" in message or "complete" in message
    finally:
        handover.handover_context.reset(token)


@pytest.mark.asyncio
async def test_a_failed_attempt_keeps_no_digits_in_the_call_context():
    token = _handover_ctx_token()
    try:
        await tools.execute_tool("capture_spoken_number", {"spoken": "zero seven seven"})
        state = handover.handover_context.get().get("_capture_spoken_number", {})
        assert not str(state.get("digits", "")), (
            "a discarded attempt must leave no digits behind to contaminate the next"
        )
        assert state.get("attempts") == 1, "the attempt counter still drives the fallback offer"
    finally:
        handover.handover_context.reset(token)
