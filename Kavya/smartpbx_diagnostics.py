"""Neutral type contracts for privacy-safe SmartPBX diagnostics."""

from enum import StrEnum
from typing import Protocol


class DiagnosticStage(StrEnum):
    SCHEMA_ADMISSION = "schema_admission"
    CONTEXT_VALIDATION = "context_validation"
    SESSION_START = "session_start"
    AUDIO_INGESTION = "audio_ingestion"
    TTS = "tts"
    HANDOVER = "handover"
    TERMINAL_CLEANUP = "terminal_cleanup"


class DiagnosticOutcome(StrEnum):
    REJECTED = "rejected"
    OBSERVED = "observed"
    COMPLETED = "completed"
    DISCONNECTED = "disconnected"
    CANCELLED = "cancelled"
    FAILED = "failed"
    DEGRADED = "degraded"


class DiagnosticFailureClass(StrEnum):
    NONE = "none"
    DISABLED = "disabled"
    AUTHENTICATION = "authentication"
    CAPACITY = "capacity"
    INVALID_MESSAGE = "invalid_message"
    MESSAGE_TOO_BIG = "message_too_big"
    UNSUPPORTED_MEDIA_FORMAT = "unsupported_media_format"
    INVALID_MEDIA = "invalid_media"
    AUDIO_TOO_BIG = "audio_too_big"
    INVALID_DTMF = "invalid_dtmf"
    UNSUPPORTED_EVENT = "unsupported_event"
    START_REQUIRED = "start_required"
    ACCOUNT_MISMATCH = "account_mismatch"
    CONTEXT_MISMATCH = "context_mismatch"
    DUPLICATE_START = "duplicate_start"
    CONNECTED_AFTER_START = "connected_after_start"
    START_TIMEOUT = "start_timeout"
    IDLE_TIMEOUT = "idle_timeout"
    TRANSFER_PENDING_TIMEOUT = "transfer_pending_timeout"
    MAX_CALL_DURATION = "max_call_duration"
    SESSION_FACTORY = "session_factory"
    SESSION_START = "session_start"
    AUDIO_INGESTION = "audio_ingestion"
    STT_UNAVAILABLE = "stt_unavailable"
    GEMINI_API_KEY_MISSING = "gemini_api_key_missing"
    PROFILE_UNAVAILABLE = "profile_unavailable"
    TTS_MISSING_API_KEY = "tts_missing_api_key"
    TTS_PROFILE_FAILURE = "tts_profile_failure"
    TTS_HTTP_STATUS = "tts_http_status"
    TTS_TIMEOUT = "tts_timeout"
    TTS_EXCEPTION = "tts_exception"
    HANDOVER_NOT_ACTIONABLE = "handover_not_actionable"
    TRANSPORT_DISCONNECT = "transport_disconnect"
    TRANSPORT_SEND = "transport_send"
    CANCELLED = "cancelled"
    SESSION_CLEANUP = "session_cleanup"
    TRANSPORT_CLEANUP = "transport_cleanup"
    LEASE_CLEANUP = "lease_cleanup"
    WEBSOCKET_CLOSE = "websocket_close"
    INTERNAL_ERROR = "internal_error"


class SmartPBXDiagnosticSink(Protocol):
    def __call__(
        self,
        stage: DiagnosticStage,
        outcome: DiagnosticOutcome,
        failure_class: DiagnosticFailureClass,
    ) -> None: ...


class SmartPBXCloseReason(StrEnum):
    """Closed vocabulary for why a SmartPBX call ended.

    Carried into the session summary (`outcome`) and the post-call record
    (`close_reason`/`close_code`) so a call is never recorded as a plain
    "completed" call when it actually failed closed.
    """

    HANGUP = "hangup"
    STOP = "stop"
    PEER_DISCONNECT = "peer_disconnect"
    IDLE_TIMEOUT = "idle_timeout"
    START_TIMEOUT = "start_timeout"
    TRANSFER_PENDING_TIMEOUT = "transfer_pending_timeout"
    MAX_CALL_DURATION = "max_call_duration"
    TRANSPORT_FAILURE = "transport_failure"
    STT_FATAL = "stt_fatal"
    PROTOCOL_VIOLATION = "protocol_violation"
    PROFILE_UNAVAILABLE = "profile_unavailable"
    INTERNAL_ERROR = "internal_error"
