"""Contract tests for the untrusted Dialog SmartPBX protocol boundary."""

import base64
import json

import pytest

from smartpbx_protocol import (
    MESSAGE_TOO_BIG,
    POLICY_VIOLATION,
    ConnectedEvent,
    DtmfEvent,
    HangupEvent,
    MediaEvent,
    ProtocolViolation,
    StartEvent,
    StopEvent,
    UnknownEvent,
    parse_smartpbx_event,
    validate_event_context,
)


START = {
    "event": "start",
    "start": {
        "callId": "call-1",
        "otherLegCallId": "other-1",
        "callerIdNumber": "caller-opaque",
        "calleeIdNumber": "callee-opaque",
        "accountId": "account-1",
        "mediaFormat": {"encoding": "g711_ulaw", "sampleRate": 8000},
    },
}


def parse(payload, *, max_message_chars=4096, max_audio_bytes=1024):
    return parse_smartpbx_event(
        json.dumps(payload),
        max_message_chars=max_message_chars,
        max_audio_bytes=max_audio_bytes,
    )


def test_kavya_start_accepts_only_ulaw_8khz():
    context = parse(START).context

    assert context.call_id == "call-1"
    assert context.media_format.encoding == "g711_ulaw"
    assert context.media_format.sample_rate == 8000


@pytest.mark.parametrize("encoding,sample_rate", [("pcm16", 8000), ("g711_ulaw", 16000)])
def test_kavya_start_rejects_non_ulaw_media(encoding, sample_rate):
    payload = json.loads(json.dumps(START))
    payload["start"]["mediaFormat"] = {"encoding": encoding, "sampleRate": sample_rate}

    with pytest.raises(ProtocolViolation) as raised:
        parse(payload)

    assert raised.value.close_code == POLICY_VIOLATION
    assert encoding not in raised.value.public_reason


@pytest.mark.parametrize("field", ["callId", "otherLegCallId", "callerIdNumber", "calleeIdNumber", "accountId"])
def test_start_requires_nonempty_identifiers(field):
    payload = json.loads(json.dumps(START))
    payload["start"][field] = " "

    with pytest.raises(ProtocolViolation) as raised:
        parse(payload)

    assert raised.value.close_code == POLICY_VIOLATION


def test_media_decodes_canonical_base64_with_a_bounded_size():
    audio = b"\x00\xff\x7f"
    event = parse({"event": "media", "media": {"payload": base64.b64encode(audio).decode()}})

    assert event == MediaEvent(audio=audio)

    with pytest.raises(ProtocolViolation) as raised:
        parse({"event": "media", "media": {"payload": "YQ"}})
    assert raised.value.close_code == POLICY_VIOLATION

    oversized = base64.b64encode(b"a" * 5).decode()
    with pytest.raises(ProtocolViolation) as raised:
        parse({"event": "media", "media": {"payload": oversized}}, max_audio_bytes=4)
    assert raised.value.close_code == MESSAGE_TOO_BIG


def test_parses_closed_event_set_and_bounds_unknown_names():
    assert isinstance(parse({"event": "connected"}), ConnectedEvent)
    assert parse({"event": "dtmf", "dtmf": {"digit": "5", "duration": 10}}) == DtmfEvent("5", 10)
    assert isinstance(parse({"event": "stop"}), StopEvent)
    assert parse({"event": "hangup", "hangup": {"callId": "call-1", "otherLegCallId": "other-1", "accountId": "account-1", "reason": "normal"}}) == HangupEvent("call-1", "other-1", "account-1", "normal")
    unknown = parse({"event": "x" * 100})
    assert isinstance(unknown, UnknownEvent)
    assert len(unknown.name) == 64


def test_rejects_oversized_message_before_decoding_and_mismatched_context():
    with pytest.raises(ProtocolViolation) as raised:
        parse_smartpbx_event("{" + "x" * 32, max_message_chars=16, max_audio_bytes=1024)
    assert raised.value.close_code == MESSAGE_TOO_BIG

    context = parse(START).context
    mismatch = json.loads(json.dumps(START))
    mismatch["start"]["callId"] = "different"
    with pytest.raises(ProtocolViolation) as raised:
        validate_event_context(parse(mismatch), context)
    assert raised.value.close_code == POLICY_VIOLATION



def test_rejects_message_that_exceeds_utf8_byte_limit_even_when_character_count_fits():
    raw = "{\"event\":\"" + "☃" * 8 + "\"}"

    assert len(raw) < len(raw.encode("utf-8"))
    with pytest.raises(ProtocolViolation) as raised:
        parse_smartpbx_event(raw, max_message_chars=len(raw), max_audio_bytes=1024)
    assert raised.value.close_code == MESSAGE_TOO_BIG
