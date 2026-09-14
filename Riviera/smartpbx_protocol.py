"""Strict, transport-independent parser for Dialog SmartPBX WebSocket events."""

import base64
import binascii
import json
from dataclasses import dataclass
from typing import Mapping, TypeAlias


POLICY_VIOLATION = 1008
MESSAGE_TOO_BIG = 1009
INTERNAL_ERROR = 1011
TRY_AGAIN_LATER = 1013

_MAX_IDENTIFIER_CHARS = 256
_MAX_PHONE_CHARS = 64
_MAX_HANGUP_REASON_CHARS = 256
_MAX_DTMF_DURATION_MS = 10_000
_ALLOWED_DTMF_DIGITS = frozenset("0123456789*#ABCD")


@dataclass
class ProtocolViolation(Exception):
    """A safe WebSocket close outcome for untrusted protocol input."""

    close_code: int
    public_reason: str
    failure_class: str

    def __str__(self) -> str:
        return self.public_reason


ProtocolError = ProtocolViolation


@dataclass(frozen=True)
class MediaFormat:
    encoding: str
    sample_rate: int


@dataclass(frozen=True)
class CallContext:
    call_id: str
    other_leg_call_id: str
    caller_id_number: str
    callee_id_number: str
    account_id: str
    media_format: MediaFormat

    @property
    def caller_number(self) -> str:
        """Compatibility name for consumers that do not use Dialog field spelling."""
        return self.caller_id_number

    @property
    def callee_number(self) -> str:
        """Compatibility name for consumers that do not use Dialog field spelling."""
        return self.callee_id_number


@dataclass(frozen=True)
class ConnectedEvent:
    pass


@dataclass(frozen=True)
class StartEvent:
    context: CallContext


@dataclass(frozen=True)
class MediaEvent:
    audio: bytes


@dataclass(frozen=True)
class DtmfEvent:
    call_id: str
    other_leg_call_id: str
    digit: str
    duration: int | None


@dataclass(frozen=True)
class HangupEvent:
    call_id: str
    other_leg_call_id: str
    reason: str | None


@dataclass(frozen=True)
class StopEvent:
    pass


@dataclass(frozen=True)
class UnsupportedEvent:
    pass


SmartPBXEvent: TypeAlias = (
    ConnectedEvent | StartEvent | MediaEvent | DtmfEvent | HangupEvent | StopEvent | UnsupportedEvent
)


def parse_start_event(event: Mapping[str, object]) -> CallContext:
    """Validate one decoded Dialog start event and return immutable call context."""
    if not isinstance(event, dict) or event.get("event") != "start":
        raise _invalid_message()
    return _parse_context(event)


def parse_smartpbx_event(raw: str, *, max_message_chars: int, max_audio_bytes: int) -> SmartPBXEvent:
    """Parse one bounded SmartPBX JSON message into the closed event union."""
    try:
        if len(raw.encode("utf-8")) > max_message_chars:
            raise ProtocolViolation(MESSAGE_TOO_BIG, "message too large", "message_too_big")
        message = json.loads(raw)
    except ProtocolViolation:
        raise
    except (TypeError, json.JSONDecodeError, RecursionError, UnicodeEncodeError) as error:
        # RecursionError: deeply nested JSON (e.g. a wall of "[[[[...") blows
        # the C decoder's recursion budget. UnicodeEncodeError: a lone
        # surrogate code point (malformed text frame) cannot be re-encoded to
        # UTF-8 for the size check. Both are protocol abuse, not an internal
        # fault — fail closed the same way a malformed JSON body does, rather
        # than surfacing as the gateway's generic INTERNAL_ERROR/1011 path.
        raise _invalid_message() from error
    if not isinstance(message, dict):
        raise _invalid_message()

    event_name = _event_name(message)
    if event_name == "connected":
        return ConnectedEvent()
    if event_name == "start":
        return StartEvent(_parse_context(message))
    if event_name == "media":
        return _parse_media(message, max_audio_bytes=max_audio_bytes)
    if event_name == "dtmf":
        return _parse_dtmf(message)
    if event_name == "hangup":
        return _parse_hangup(message)
    if event_name == "stop":
        return StopEvent()
    return UnsupportedEvent()


def validate_event_context(event: SmartPBXEvent, context: CallContext) -> None:
    """Reject a duplicate start or control event that belongs to another call."""
    if isinstance(event, StartEvent):
        call_id, other_leg_call_id, account_id = (
            event.context.call_id, event.context.other_leg_call_id, event.context.account_id
        )
    elif isinstance(event, (DtmfEvent, HangupEvent)):
        if (event.call_id, event.other_leg_call_id) != (context.call_id, context.other_leg_call_id):
            raise ProtocolViolation(POLICY_VIOLATION, "event context mismatch", "context_mismatch")
        return
    else:
        return
    if (call_id, other_leg_call_id, account_id) != (
        context.call_id, context.other_leg_call_id, context.account_id
    ):
        raise ProtocolViolation(POLICY_VIOLATION, "event context mismatch", "context_mismatch")


def _parse_context(message: Mapping[object, object]) -> CallContext:
    start = _required_mapping(message, "start")
    media_format = _required_mapping(start, "mediaFormat")
    encoding = _required_text(media_format, "encoding", _MAX_IDENTIFIER_CHARS)
    sample_rate = _sample_rate(media_format)
    if encoding != "g711_ulaw" or sample_rate != 8000:
        raise ProtocolViolation(POLICY_VIOLATION, "unsupported media format", "unsupported_media_format")
    return CallContext(
        call_id=_required_text(start, "callId", _MAX_IDENTIFIER_CHARS),
        other_leg_call_id=_required_text(start, "otherLegCallId", _MAX_IDENTIFIER_CHARS),
        caller_id_number=_required_text(start, "callerIdNumber", _MAX_PHONE_CHARS),
        callee_id_number=_required_text(start, "calleeIdNumber", _MAX_PHONE_CHARS),
        account_id=_required_text(start, "accountId", _MAX_IDENTIFIER_CHARS),
        media_format=MediaFormat(encoding=encoding, sample_rate=sample_rate),
    )


def _parse_media(message: Mapping[object, object], *, max_audio_bytes: int) -> MediaEvent:
    # Hot path: ~50 frames/sec/call, so ~200/sec at the four-call cap. Two costs
    # are accepted here deliberately. The json.dumps below re-serialises the
    # message only to bound the payload length, and the b64 round-trip re-encodes
    # every frame to reject non-canonical base64. Both are kept: the round-trip is
    # real validation, not a formality, and weakening either to save CPU would
    # trade a correctness guarantee for microseconds. Revisit only with a profile
    # showing this is the actual bottleneck, and keep the round-trip if so.
    media = _required_mapping(message, "media")
    payload = _required_text(media, "payload", len(json.dumps(message)))
    try:
        audio = base64.b64decode(payload, validate=True)
    except (ValueError, binascii.Error) as error:
        raise ProtocolViolation(POLICY_VIOLATION, "invalid media payload", "invalid_media") from error
    if base64.b64encode(audio).decode("ascii") != payload:
        raise ProtocolViolation(POLICY_VIOLATION, "invalid media payload", "invalid_media")
    if len(audio) > max_audio_bytes:
        raise ProtocolViolation(MESSAGE_TOO_BIG, "audio too large", "audio_too_big")
    return MediaEvent(audio=audio)


def _parse_dtmf(message: Mapping[object, object]) -> DtmfEvent:
    dtmf = _required_mapping(message, "dtmf")
    call_id = _required_text(dtmf, "callId", _MAX_IDENTIFIER_CHARS)
    other_leg_call_id = _required_text(dtmf, "otherLegCallId", _MAX_IDENTIFIER_CHARS)
    digit = _required_text(dtmf, "digit", 1)
    if digit not in _ALLOWED_DTMF_DIGITS:
        raise ProtocolViolation(POLICY_VIOLATION, "invalid DTMF event", "invalid_dtmf")
    if "duration" not in dtmf:
        return DtmfEvent(call_id=call_id, other_leg_call_id=other_leg_call_id, digit=digit, duration=None)
    duration = dtmf["duration"]
    if isinstance(duration, bool) or not isinstance(duration, int) or not 0 <= duration <= _MAX_DTMF_DURATION_MS:
        raise ProtocolViolation(POLICY_VIOLATION, "invalid DTMF event", "invalid_dtmf")
    return DtmfEvent(call_id=call_id, other_leg_call_id=other_leg_call_id, digit=digit, duration=duration)


def _parse_hangup(message: Mapping[object, object]) -> HangupEvent:
    hangup = _required_mapping(message, "hangup")
    reason = None if "reason" not in hangup else _required_text(hangup, "reason", _MAX_HANGUP_REASON_CHARS)
    return HangupEvent(
        call_id=_required_text(hangup, "callId", _MAX_IDENTIFIER_CHARS),
        other_leg_call_id=_required_text(hangup, "otherLegCallId", _MAX_IDENTIFIER_CHARS),
        reason=reason,
    )


def _event_name(message: Mapping[object, object]) -> str:
    value = message.get("event")
    if not isinstance(value, str) or not value.strip():
        raise _invalid_message()
    return value


def _required_mapping(message: Mapping[object, object], field: str) -> dict[object, object]:
    value = message.get(field)
    if not isinstance(value, dict):
        raise _invalid_message()
    return value


def _required_text(message: Mapping[object, object], field: str, max_chars: int) -> str:
    value = message.get(field)
    if not isinstance(value, str) or not value.strip() or len(value) > max_chars:
        raise _invalid_message()
    return value


def _sample_rate(media_format: Mapping[object, object]) -> int:
    value = media_format.get("sampleRate")
    if isinstance(value, bool):
        raise _invalid_message()
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdecimal():
        return int(value)
    raise _invalid_message()


def _invalid_message() -> ProtocolViolation:
    return ProtocolViolation(POLICY_VIOLATION, "invalid SmartPBX message", "invalid_message")
