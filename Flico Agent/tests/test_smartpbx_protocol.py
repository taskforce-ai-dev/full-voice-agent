"""Contract tests for the untrusted Dialog SmartPBX event boundary."""

import base64
import json

import pytest

from smartpbx_protocol import (
    MESSAGE_TOO_BIG,
    POLICY_VIOLATION,
    CallContext,
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
        "callId": "call-main",
        "otherLegCallId": "call-other-leg",
        "callerIdNumber": "+94110000000",
        "calleeIdNumber": "+94114698850",
        "accountId": "account-1",
        "mediaFormat": {"encoding": "g711_ulaw", "sampleRate": "8000"},
    },
}


def parse(payload, *, max_message_chars=4096, max_audio_bytes=1024):
    return parse_smartpbx_event(
        json.dumps(payload),
        max_message_chars=max_message_chars,
        max_audio_bytes=max_audio_bytes,
    )


def assert_violation(payload, *, close_code=POLICY_VIOLATION, secret=""):
    with pytest.raises(ProtocolViolation) as raised:
        parse(payload)
    assert raised.value.close_code == close_code
    if secret:
        assert secret not in raised.value.public_reason
    return raised.value


def test_parses_a_valid_start_event_into_immutable_context():
    event = parse(START)

    assert isinstance(event, StartEvent)
    assert event.context == CallContext(
        call_id="call-main",
        other_leg_call_id="call-other-leg",
        caller_id_number="+94110000000",
        callee_id_number="+94114698850",
        account_id="account-1",
        media_format=event.context.media_format,
    )
    assert event.context.media_format.encoding == "g711_ulaw"
    assert event.context.media_format.sample_rate == 8000
    with pytest.raises(AttributeError):
        event.context.call_id = "other-call"


@pytest.mark.parametrize("sample_rate", [8000, "8000"])
def test_normalizes_integer_and_string_sample_rates(sample_rate):
    payload = json.loads(json.dumps(START))
    payload["start"]["mediaFormat"]["sampleRate"] = sample_rate

    assert parse(payload).context.media_format.sample_rate == 8000


@pytest.mark.parametrize(
    "path",
    [
        ("callId",),
        ("otherLegCallId",),
        ("callerIdNumber",),
        ("calleeIdNumber",),
        ("accountId",),
        ("mediaFormat", "encoding"),
        ("mediaFormat", "sampleRate"),
    ],
)
def test_rejects_missing_required_start_fields(path):
    payload = json.loads(json.dumps(START))
    target = payload["start"]
    for key in path[:-1]:
        target = target[key]
    secret = target.pop(path[-1], "missing-value")

    assert_violation(payload, secret=str(secret))


@pytest.mark.parametrize(
    "path",
    [
        ("callId",),
        ("otherLegCallId",),
        ("callerIdNumber",),
        ("calleeIdNumber",),
        ("accountId",),
        ("mediaFormat", "encoding"),
        ("mediaFormat", "sampleRate"),
    ],
)
def test_rejects_blank_required_start_fields(path):
    payload = json.loads(json.dumps(START))
    target = payload["start"]
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = "   "

    assert_violation(payload)


@pytest.mark.parametrize(
    ("encoding", "sample_rate"),
    [("pcm16", "8000"), ("g711_ulaw", "16000")],
)
def test_rejects_unsupported_media_formats(encoding, sample_rate):
    payload = json.loads(json.dumps(START))
    payload["start"]["mediaFormat"] = {
        "encoding": encoding,
        "sampleRate": sample_rate,
    }

    assert_violation(payload, secret=encoding)


def test_decodes_valid_media_base64_into_audio_bytes():
    audio = b"\x00\xff\x7f"
    event = parse({
        "event": "media",
        "media": {"payload": base64.b64encode(audio).decode("ascii")},
    })

    assert isinstance(event, MediaEvent)
    assert event.audio == audio


@pytest.mark.parametrize("payload", ["not-base64", "YQ", "YQ==\n"])
def test_rejects_invalid_or_noncanonical_media_base64(payload):
    assert_violation({"event": "media", "media": {"payload": payload}}, secret=payload)


def test_rejects_decoded_audio_over_the_limit():
    payload = base64.b64encode(b"a" * 5).decode("ascii")

    with pytest.raises(ProtocolViolation) as raised:
        parse({"event": "media", "media": {"payload": payload}}, max_audio_bytes=4)

    assert raised.value.close_code == MESSAGE_TOO_BIG
    assert payload not in raised.value.public_reason


def test_parses_a_valid_dtmf_digit_and_duration():
    event = parse({"event": "dtmf", "dtmf": {"digit": "5", "duration": 120}})

    assert event == DtmfEvent(digit="5", duration=120)


@pytest.mark.parametrize("digit", ["", "12", "x"])
def test_rejects_invalid_dtmf_digits(digit):
    assert_violation({"event": "dtmf", "dtmf": {"digit": digit}}, secret=digit)


def test_parses_valid_hangup_stop_and_connected_events():
    hangup = parse({
        "event": "hangup",
        "hangup": {
            "callId": "call-main",
            "otherLegCallId": "call-other-leg",
            "accountId": "account-1",
            "reason": "normal",
        },
    })

    assert hangup == HangupEvent(
        call_id="call-main",
        other_leg_call_id="call-other-leg",
        account_id="account-1",
        reason="normal",
    )
    assert isinstance(parse({"event": "stop"}), StopEvent)
    assert isinstance(parse({"event": "connected", "protocol": "Call"}), ConnectedEvent)


def test_unknown_event_keeps_only_a_bounded_name():
    event = parse({"event": "custom-" + "x" * 100, "untrusted": {"body": "secret"}})

    assert isinstance(event, UnknownEvent)
    assert event.name == "custom-" + "x" * 57
    assert len(event.name) == 64
    assert not hasattr(event, "raw")


def test_rejects_message_over_the_limit_before_json_decoding():
    raw = "{" + "x" * 32

    with pytest.raises(ProtocolViolation) as raised:
        parse_smartpbx_event(raw, max_message_chars=16, max_audio_bytes=1024)

    assert raised.value.close_code == MESSAGE_TOO_BIG
    assert raw not in raised.value.public_reason


@pytest.mark.parametrize("raw", ["null", "[]", '"text"', "123"])
def test_rejects_non_object_json(raw):
    with pytest.raises(ProtocolViolation) as raised:
        parse_smartpbx_event(raw, max_message_chars=4096, max_audio_bytes=1024)

    assert raised.value.close_code == POLICY_VIOLATION
    assert raw not in raised.value.public_reason


def test_rejects_duplicate_start_when_its_context_id_mismatches():
    context = parse(START).context
    duplicate = json.loads(json.dumps(START))
    duplicate["start"]["callId"] = "attacker-call-id"

    with pytest.raises(ProtocolViolation) as raised:
        validate_event_context(parse(duplicate), context)

    assert raised.value.close_code == POLICY_VIOLATION
    assert "attacker-call-id" not in raised.value.public_reason
