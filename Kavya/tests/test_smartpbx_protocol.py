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


def test_parses_closed_event_set_and_uses_value_free_unsupported_events():
    assert isinstance(parse({"event": "connected"}), ConnectedEvent)
    dtmf = parse({"event": "dtmf", "dtmf": {"callId": "call-1", "otherLegCallId": "other-1", "digit": "5", "duration": 10}})
    assert isinstance(dtmf, DtmfEvent)
    assert (dtmf.call_id, dtmf.other_leg_call_id, dtmf.digit, dtmf.duration) == ("call-1", "other-1", "5", 10)
    assert isinstance(parse({"event": "stop"}), StopEvent)
    hangup = parse({"event": "hangup", "hangup": {"callId": "call-1", "otherLegCallId": "other-1", "accountId": "ignored", "reason": "normal"}})
    assert isinstance(hangup, HangupEvent)
    assert (hangup.call_id, hangup.other_leg_call_id, hangup.reason) == ("call-1", "other-1", "normal")
    assert not hasattr(hangup, "account_id")
    unknown = parse({"event": "x" * 100})
    assert type(unknown).__name__ == "UnsupportedEvent"
    assert vars(unknown) == {}


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


def hangup_payload(**fields):
    return {"event": "hangup", "hangup": {"callId": "call", "otherLegCallId": "other", **fields}}


@pytest.mark.parametrize(
    "field,value",
    [
        ("callId", None), ("callId", ""), ("callId", 7), ("callId", "x" * 257),
        ("otherLegCallId", None), ("otherLegCallId", ""), ("otherLegCallId", 7), ("otherLegCallId", "x" * 257),
    ],
)
def test_v06_hangup_rejects_missing_blank_non_text_and_oversized_legs(field, value):
    payload = hangup_payload()
    if value is None:
        del payload["hangup"][field]
    else:
        payload["hangup"][field] = value

    with pytest.raises(ProtocolViolation):
        parse(payload)


@pytest.mark.parametrize("field", ["callId", "otherLegCallId"])
def test_v06_hangup_accepts_max_length_legs(field):
    payload = hangup_payload(reason="normal")
    payload["hangup"][field] = "x" * 256

    event = parse(payload)

    assert getattr(event, "call_id" if field == "callId" else "other_leg_call_id") == "x" * 256


def test_v06_hangup_accepts_absent_reason_and_ignores_account_and_unrelated_fields():
    event = parse(hangup_payload(accountId="ignored", unrelated={"untrusted": True}))

    assert (event.call_id, event.other_leg_call_id, event.reason) == ("call", "other", None)
    assert not hasattr(event, "account_id")


@pytest.mark.parametrize("reason", [None, "", 7, ["reason"], {"reason": "nested"}, "x" * 257])
def test_v06_hangup_rejects_present_null_empty_non_text_and_oversized_reason(reason):
    with pytest.raises(ProtocolViolation):
        parse(hangup_payload(reason=reason))


def test_v06_hangup_accepts_max_length_reason():
    assert parse(hangup_payload(reason="x" * 256)).reason == "x" * 256


def dtmf_payload(**fields):
    return {"event": "dtmf", "dtmf": {"callId": "call", "otherLegCallId": "other", "digit": "5", **fields}}


@pytest.mark.parametrize(
    "field,value",
    [
        ("callId", None), ("callId", ""), ("callId", 7), ("callId", "x" * 257),
        ("otherLegCallId", None), ("otherLegCallId", ""), ("otherLegCallId", 7), ("otherLegCallId", "x" * 257),
    ],
)
def test_v06_dtmf_rejects_missing_blank_non_text_and_oversized_legs(field, value):
    payload = dtmf_payload()
    if value is None:
        del payload["dtmf"][field]
    else:
        payload["dtmf"][field] = value

    with pytest.raises(ProtocolViolation):
        parse(payload)


@pytest.mark.parametrize("field", ["callId", "otherLegCallId"])
def test_v06_dtmf_accepts_max_length_legs_and_ignores_unrelated_fields(field):
    payload = dtmf_payload(unrelated="ignored")
    payload["dtmf"][field] = "x" * 256

    event = parse(payload)

    assert getattr(event, "call_id" if field == "callId" else "other_leg_call_id") == "x" * 256


@pytest.mark.parametrize("digit", list("0123456789*#ABCD"))
def test_v06_dtmf_accepts_all_protocol_digits(digit):
    event = parse(dtmf_payload(digit=digit))

    assert event.digit == digit


@pytest.mark.parametrize("duration", [None, 0, 10_000])
def test_v06_dtmf_accepts_absent_or_bounded_duration(duration):
    payload = dtmf_payload()
    if duration is not None:
        payload["dtmf"]["duration"] = duration

    event = parse(payload)

    assert event.duration == duration


@pytest.mark.parametrize("duration", [True, -1, 1.5, 10_001])
def test_v06_dtmf_rejects_invalid_duration(duration):
    with pytest.raises(ProtocolViolation):
        parse(dtmf_payload(duration=duration))


@pytest.mark.parametrize("event_name", ["", "   ", None, 7, []])
def test_event_name_must_be_nonblank_text(event_name):
    with pytest.raises(ProtocolViolation):
        parse({"event": event_name})


@pytest.mark.parametrize("event_type,field", [("dtmf", "callId"), ("dtmf", "otherLegCallId"), ("hangup", "callId"), ("hangup", "otherLegCallId")])
def test_v06_dtmf_and_hangup_reject_context_mismatch_for_either_leg(event_type, field):
    context = parse(START).context
    payload = dtmf_payload() if event_type == "dtmf" else hangup_payload()
    payload[event_type][field] = "different"

    with pytest.raises(ProtocolViolation) as raised:
        validate_event_context(parse(payload), context)

    assert raised.value.close_code == POLICY_VIOLATION
