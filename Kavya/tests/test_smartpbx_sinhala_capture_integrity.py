"""Provider-owned Sinhala STT identity and durable capture safety contracts."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

import handover
import server
import tools


class _Scheduled:
    def __init__(self, delay, callback):
        self.delay = delay
        self.callback = callback
        self.cancelled = False

    def cancel(self):
        self.cancelled = True


class _Loop:
    def __init__(self):
        self.scheduled = []

    def call_later(self, delay, callback):
        handle = _Scheduled(delay, callback)
        self.scheduled.append(handle)
        return handle


def _session():
    pipeline = server.MediaStreamSession(
        websocket=None,
        lang="si",
        media_transport=object(),
        llm_provider="gemini",
    )
    pipeline._smartpbx_transfer_context = object()
    pipeline._smartpbx_azure_final_endpointing = True
    pipeline._smartpbx_caller_context = {"_capture_provenance_required": True}
    pipeline._event_loop = _Loop()
    return pipeline


def _metadata(result_id, offset, duration, confidence=0.9):
    return server.AzureFinalMetadata(
        result_id=result_id,
        offset=offset,
        duration=duration,
        confidence=confidence,
    )


def test_azure_metadata_parser_carries_identity_interval_and_confidence():
    result = SimpleNamespace(
        result_id="private-result-id",
        offset=120,
        duration=80,
        json='{"NBest":[{"Confidence":0.42}]}',
    )

    metadata = server._azure_final_metadata(result)

    assert metadata == server.AzureFinalMetadata(
        result_id="private-result-id", offset=120, duration=80, confidence=0.42,
    )


@pytest.mark.asyncio
async def test_duplicate_azure_result_id_is_ignored_without_text_guessing(caplog):
    pipeline = _session()
    metadata = _metadata("private-result-id", 0, 100)

    await pipeline._accumulate_transcript("first final", metadata=metadata)
    await pipeline._accumulate_transcript("different text must still dedupe", metadata=metadata)

    assert pipeline._committed_transcript == "first final"
    assert pipeline._smartpbx_stt_final_events == 1
    assert len(pipeline._event_loop.scheduled) == 1
    assert "action=ignored basis=result_id" in caplog.text
    assert "private-result-id" not in caplog.text
    assert "first final" not in caplog.text


@pytest.mark.asyncio
async def test_partial_azure_overlap_conservatively_appends_without_word_timestamps():
    pipeline = _session()

    await pipeline._accumulate_transcript(
        "old cumulative hypothesis", metadata=_metadata("a", 0, 100),
    )
    await pipeline._accumulate_transcript(
        "authoritative replacement", metadata=_metadata("b", 40, 100),
    )
    await pipeline._accumulate_transcript(
        "next segment", metadata=_metadata("c", 140, 60),
    )

    assert pipeline._committed_transcript == (
        "old cumulative hypothesis authoritative replacement next segment"
    )
    assert pipeline._smartpbx_stt_final_events == 3


def test_low_confidence_capture_persists_pending_until_explicit_confirmation():
    pipeline = _session()
    pipeline._record_capture_tool_completion("capture_spoken_number", {
        "status": "captured",
        "valid": True,
        "normalized": "94771234567",
        "readback": "0 7 7 1 2 3 4 5 6 7",
        "confirmation_required": True,
    })

    assert "guest_phone" not in pipeline._booking_slots
    assert pipeline._pending_capture_confirmation is not None

    assert pipeline._apply_pending_capture_confirmation("ඔව්, හරි") == "confirmed"
    assert pipeline._booking_slots["guest_phone"] == "94771234567"
    assert pipeline._pending_capture_confirmation is None
    assert pipeline._smartpbx_caller_context["_capture_validated_number"] == "94771234567"


def test_rejected_low_confidence_name_clears_candidate_and_reenters_capture():
    pipeline = _session()
    pipeline._record_capture_tool_completion("capture_spoken_name", {
        "status": "captured",
        "name": "Ace Fernando",
        "readback": "Ace Fernando",
        "confirmation_required": True,
    })

    assert pipeline._apply_pending_capture_confirmation("නැහැ, ඒක වැරදියි") == "rejected"
    assert pipeline._pending_capture_confirmation is None
    assert "guest_name" not in pipeline._booking_slots
    assert pipeline._capture_mode_active is True
    assert pipeline._capture_kind == "name"


def test_confirmed_slot_is_immutable_until_a_replacement_is_explicitly_confirmed():
    pipeline = _session()
    pipeline._record_capture_tool_completion(
        "capture_spoken_name", {"status": "captured", "name": "Chris Fernando"},
    )
    pipeline._record_capture_tool_completion(
        "capture_spoken_name", {"status": "captured", "name": "Ace Fernando"},
    )

    assert pipeline._booking_slots["guest_name"] == "Chris Fernando"
    assert pipeline._pending_capture_confirmation.value == "Ace Fernando"

    pipeline._apply_pending_capture_confirmation("yes, that is correct")
    assert pipeline._booking_slots["guest_name"] == "Ace Fernando"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "first", "replacement", "kind"),
    [
        (
            "capture_spoken_number",
            {"status": "captured", "valid": True, "normalized": "94771234567"},
            {
                "status": "captured", "valid": True, "normalized": "94770000000",
                "confirmation_required": True,
            },
            "phone",
        ),
        (
            "capture_spoken_name",
            {"status": "captured", "name": "Chris Fernando"},
            {
                "status": "captured", "name": "Ace Fernando",
                "confirmation_required": True,
            },
            "name",
        ),
    ],
)
async def test_rejected_confirmed_replacement_requires_full_recapture_and_blocks_side_effects(
    monkeypatch, tool_name, first, replacement, kind,
):
    pipeline = _session()
    pipeline._record_capture_tool_completion(tool_name, first)
    pipeline._record_capture_tool_completion(tool_name, replacement)

    assert pipeline._apply_pending_capture_confirmation("no, that is wrong") == "rejected"
    assert pipeline._capture_mode_active is True
    assert pipeline._capture_kind == kind
    assert pipeline._smartpbx_caller_context["_capture_candidate_pending"] == kind

    calls = []

    async def book(**kwargs):
        calls.append(("booking", kwargs))
        return {"success": True}

    async def notify(**kwargs):
        calls.append(("handover", kwargs))
        return {"ok": True}

    monkeypatch.setattr(tools, "create_booking", book)
    monkeypatch.setattr(handover, "send_handover_notification", notify)
    transfer_token = tools.smartpbx_transfer_context.set(
        tools.SmartPBXTransferContext(call_control=None)
    )
    caller_token = handover.handover_context.set(pipeline._smartpbx_caller_context)
    try:
        booking = json.loads(await tools.execute_tool("create_booking", {
            "check_in": "2026-10-10",
            "check_out": "2026-10-11",
            "room_type": tools.ROOM_TYPES_BY_PROPERTY[tools.PROPERTY_HATTON][0],
            "guest_name": "Model Supplied Name",
            "guest_phone": "94770000000",
        }))
        handover_result = json.loads(await tools.execute_tool("notify_human_handover", {
            "customer_name": "Model Supplied Name",
            "customer_whatsapp": "94770000000",
        }))
    finally:
        handover.handover_context.reset(caller_token)
        tools.smartpbx_transfer_context.reset(transfer_token)

    assert booking["status"] == "confirmation_required"
    assert handover_result["status"] == "confirmation_required"
    assert calls == []


@pytest.mark.asyncio
async def test_direct_sinhala_replacement_utterance_fences_same_turn_booking_and_handover(
    monkeypatch,
):
    pipeline = _session()
    pipeline._record_capture_tool_completion("capture_spoken_number", {
        "status": "captured", "valid": True, "normalized": "94771234567",
    })
    pipeline._record_capture_tool_completion("capture_spoken_number", {
        "status": "captured", "valid": True, "normalized": "94770000000",
        "confirmation_required": True,
    })

    assert pipeline._apply_pending_capture_confirmation(
        "actually, use zero seven seven zero zero zero zero zero zero zero"
    ) == "replacement"
    assert pipeline._smartpbx_caller_context["_capture_candidate_pending"] == "phone"
    assert pipeline._capture_mode_active is True

    calls = []

    async def book(**kwargs):
        calls.append(("booking", kwargs))
        return {"success": True}

    async def notify(**kwargs):
        calls.append(("handover", kwargs))
        return {"ok": True}

    monkeypatch.setattr(tools, "create_booking", book)
    monkeypatch.setattr(handover, "send_handover_notification", notify)
    transfer_token = tools.smartpbx_transfer_context.set(
        tools.SmartPBXTransferContext(call_control=None)
    )
    caller_token = handover.handover_context.set(pipeline._smartpbx_caller_context)
    try:
        booking = json.loads(await tools.execute_tool("create_booking", {
            "check_in": "2026-10-10",
            "check_out": "2026-10-11",
            "room_type": tools.ROOM_TYPES_BY_PROPERTY[tools.PROPERTY_HATTON][0],
            "guest_name": "Model Supplied Name",
            "guest_phone": "94770000000",
        }))
        handover_result = json.loads(await tools.execute_tool("notify_human_handover", {
            "customer_name": "Model Supplied Name",
            "customer_whatsapp": "94770000000",
        }))
    finally:
        handover.handover_context.reset(caller_token)
        tools.smartpbx_transfer_context.reset(transfer_token)

    assert booking["status"] == "confirmation_required"
    assert handover_result["status"] == "confirmation_required"
    assert calls == []


def test_rejected_create_booking_input_does_not_enter_sinhala_booking_slots():
    pipeline = _session()
    pipeline._record_booking_tool_completion(
        "create_booking",
        {"guest_name": "Wrong Name", "guest_phone": "94770000000"},
        {"status": "confirmation_required"},
    )

    assert "guest_name" not in pipeline._booking_slots
    assert "guest_phone" not in pipeline._booking_slots


@pytest.mark.asyncio
async def test_direct_sinhala_booking_requires_captured_phone_provenance(monkeypatch):
    calls = []

    async def book(**kwargs):
        calls.append(kwargs)
        return {"success": True}

    monkeypatch.setattr(tools, "create_booking", book)
    transfer_token = tools.smartpbx_transfer_context.set(
        tools.SmartPBXTransferContext(call_control=None)
    )
    caller_token = handover.handover_context.set({
        "_capture_provenance_required": True,
    })
    try:
        result = json.loads(await tools.execute_tool("create_booking", {
            "check_in": "2026-10-10",
            "check_out": "2026-10-11",
            "room_type": tools.ROOM_TYPES_BY_PROPERTY[tools.PROPERTY_HATTON][0],
            "guest_name": "Test Guest",
            "guest_phone": "94771234567",
        }))
    finally:
        handover.handover_context.reset(caller_token)
        tools.smartpbx_transfer_context.reset(transfer_token)

    assert result["status"] == "capture_required"
    assert calls == []


@pytest.mark.asyncio
async def test_sinhala_keypad_result_uses_canonical_normalizer_and_persists_only_when_valid():
    pipeline = _session()

    valid = pipeline._finalize_keypad_capture_result({
        "status": "collected", "digits": "0771234567", "readback": "0 7 7 1 2 3 4 5 6 7",
    })
    assert valid["status"] == "captured"
    assert valid["normalized"] == "94771234567"
    assert pipeline._booking_slots["guest_phone"] == "94771234567"

    pipeline = _session()
    invalid = pipeline._finalize_keypad_capture_result({
        "status": "collected", "digits": "077123", "readback": "0 7 7 1 2 3",
    })
    assert invalid["status"] == "invalid_number"
    assert invalid["normalized"] == ""
    assert "guest_phone" not in pipeline._booking_slots


def test_sinhala_spoken_fallback_becomes_a_durable_keypad_gate():
    pipeline = _session()

    pipeline._record_capture_tool_completion("capture_spoken_number", {
        "status": "needs_more", "fallback_allowed": True,
    })

    assert pipeline._keypad_required is True
    assert pipeline._smartpbx_caller_context["_keypad_required"] is True
    assert pipeline._smartpbx_caller_context["_capture_candidate_pending"] == "phone"
    assert pipeline._capture_mode_active is False
    assert "collect_number_via_keypad now" in pipeline._active_system_prompt()


def test_keypad_no_input_or_invalid_ends_gate_and_allows_one_fresh_spoken_episode():
    pipeline = _session()
    pipeline._set_keypad_required()
    pipeline._smartpbx_caller_context["_capture_spoken_number"] = {"attempts": 2}

    no_input = pipeline._finalize_keypad_capture_result({"status": "no_input"})
    pipeline._record_capture_tool_completion("collect_number_via_keypad", no_input)

    assert pipeline._keypad_required is False
    assert "_keypad_required" not in pipeline._smartpbx_caller_context
    assert "_capture_spoken_number" not in pipeline._smartpbx_caller_context
    assert pipeline._capture_mode_active is True
    assert pipeline._capture_kind == "phone"

    pipeline._set_keypad_required()
    invalid = pipeline._finalize_keypad_capture_result({
        "status": "collected", "digits": "077", "readback": "0 7 7",
    })
    pipeline._record_capture_tool_completion("collect_number_via_keypad", invalid)

    assert invalid["status"] == "invalid_number"
    assert pipeline._keypad_required is False
    assert pipeline._capture_mode_active is True


@pytest.mark.asyncio
async def test_direct_sinhala_keypad_gate_rejects_further_spoken_capture():
    context = {"_capture_provenance_required": True, "_keypad_required": True}
    token = handover.handover_context.set(context)
    try:
        result = json.loads(await tools.execute_tool("capture_spoken_number", {
            "spoken": "zero seven seven one two three four five six seven",
        }))
    finally:
        handover.handover_context.reset(token)

    assert result["status"] == "keypad_required"
    assert "_capture_spoken_number" not in context


@pytest.mark.asyncio
async def test_invalid_direct_sinhala_replacement_blocks_same_batch_booking(monkeypatch):
    calls = []

    async def book(**kwargs):
        calls.append(kwargs)
        return {"success": True}

    monkeypatch.setattr(tools, "create_booking", book)
    context = {
        "_capture_provenance_required": True,
        "_capture_validated_number": "94771234567",
        "_confirmed_capture_phone": True,
    }
    transfer_token = tools.smartpbx_transfer_context.set(
        tools.SmartPBXTransferContext(call_control=None)
    )
    caller_token = handover.handover_context.set(context)
    try:
        invalid = json.loads(await tools.execute_tool("capture_spoken_number", {
            "spoken": "not a phone number",
        }))
        booking = json.loads(await tools.execute_tool("create_booking", {
            "check_in": "2026-10-10",
            "check_out": "2026-10-11",
            "room_type": tools.ROOM_TYPES_BY_PROPERTY[tools.PROPERTY_HATTON][0],
            "guest_name": "Test Guest",
            "guest_phone": "94770000000",
        }))
    finally:
        handover.handover_context.reset(caller_token)
        tools.smartpbx_transfer_context.reset(transfer_token)

    assert invalid["status"] == "needs_more"
    assert context["_capture_candidate_pending"] == "phone"
    assert booking["status"] == "confirmation_required"
    assert calls == []


def test_runner_keeps_retry_marker_after_a_failed_spoken_replacement():
    pipeline = _session()
    pipeline._smartpbx_caller_context.update({
        "_confirmed_capture_phone": True,
        "_capture_candidate_pending": "phone",
        "_capture_spoken_number": {"attempts": 1},
    })

    pipeline._record_capture_tool_completion("capture_spoken_number", {
        "status": "needs_more", "fallback_allowed": False,
    })

    assert pipeline._smartpbx_caller_context["_capture_candidate_pending"] == "phone"
    assert pipeline._smartpbx_caller_context["_capture_spoken_number"] == {"attempts": 1}


@pytest.mark.asyncio
async def test_direct_sinhala_handover_requires_provenance_and_uses_privacy_safe_sender(monkeypatch):
    sent = []

    async def sender(**kwargs):
        sent.append(kwargs)
        return {"ok": True, "status": 200}

    monkeypatch.setattr(handover, "send_handover_notification", sender)
    context = {"_capture_provenance_required": True}
    token = handover.handover_context.set(context)
    try:
        absent = json.loads(await tools.execute_tool("notify_human_handover", {
            "customer_name": "Test Guest",
            "customer_whatsapp": "94770000000",
        }))
    finally:
        handover.handover_context.reset(token)

    assert absent["status"] == "capture_required"
    assert sent == []

    context = {
        "_capture_provenance_required": True,
        "_capture_validated_number": "94771234567",
        "call_sid": "private-call-id",
        "human_agent_whatsapp": "94770000001",
    }
    token = handover.handover_context.set(context)
    try:
        sent_result = json.loads(await tools.execute_tool("notify_human_handover", {
            "customer_name": "Test Guest",
            "customer_whatsapp": "94770000000",
            "call_summary": "private summary",
        }))
    finally:
        handover.handover_context.reset(token)

    assert sent_result["status"] == "sent"
    assert sent == [{
        "call_sid": "private-call-id",
        "customer_name": "Test Guest",
        "customer_whatsapp": "94771234567",
        "call_summary": "private summary",
        "human_agent_whatsapp": "94770000001",
        "privacy_safe": True,
    }]


@pytest.mark.asyncio
async def test_direct_sinhala_confirmed_name_overrides_stale_booking_and_handover_arguments(monkeypatch):
    booking_calls = []
    handover_calls = []

    async def book(**kwargs):
        booking_calls.append(kwargs)
        return {"success": True}

    async def notify(**kwargs):
        handover_calls.append(kwargs)
        return {"ok": True, "status": 200}

    monkeypatch.setattr(tools, "create_booking", book)
    monkeypatch.setattr(handover, "send_handover_notification", notify)
    context = {
        "_capture_provenance_required": True,
        "_capture_validated_number": "94771234567",
        "_confirmed_capture_phone": True,
        "spelled_name": "Confirmed Guest",
        "_confirmed_capture_name": True,
        "human_agent_whatsapp": "94770000001",
    }
    transfer_token = tools.smartpbx_transfer_context.set(
        tools.SmartPBXTransferContext(call_control=None)
    )
    caller_token = handover.handover_context.set(context)
    try:
        booking = json.loads(await tools.execute_tool("create_booking", {
            "check_in": "2026-10-10",
            "check_out": "2026-10-11",
            "room_type": tools.ROOM_TYPES_BY_PROPERTY[tools.PROPERTY_HATTON][0],
            "guest_name": "Stale Model Name",
            "guest_phone": "94770000000",
        }))
        handover_result = json.loads(await tools.execute_tool("notify_human_handover", {
            "customer_name": "Stale Model Name",
            "customer_whatsapp": "94770000000",
        }))
    finally:
        handover.handover_context.reset(caller_token)
        tools.smartpbx_transfer_context.reset(transfer_token)

    assert booking["success"] is True
    assert handover_result["status"] == "sent"
    assert booking_calls[0]["guest_name"] == "Confirmed Guest"
    assert handover_calls[0]["customer_name"] == "Confirmed Guest"


@pytest.mark.asyncio
async def test_english_twilio_capture_and_handover_keep_legacy_paths(monkeypatch):
    sent = []

    async def sender(**kwargs):
        sent.append(kwargs)
        return {"ok": True, "status": 200}

    monkeypatch.setattr(handover, "send_handover_notification", sender)
    context = {"_keypad_required": True}
    token = handover.handover_context.set(context)
    try:
        captured = json.loads(await tools.execute_tool("capture_spoken_number", {
            "spoken": "zero seven seven one two three four five six seven",
        }))
        notified = json.loads(await tools.execute_tool("notify_human_handover", {
            "customer_name": "Test Guest",
            "customer_whatsapp": "94771234567",
        }))
    finally:
        handover.handover_context.reset(token)

    assert captured["status"] == "captured"
    assert notified["status"] == "sent"
    assert "privacy_safe" not in sent[0]
