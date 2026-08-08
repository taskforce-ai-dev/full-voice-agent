"""Contract tests for the neutral SmartPBX diagnostics boundary."""

import importlib
import importlib.util
import inspect
from enum import Enum


def load_diagnostics_module():
    assert importlib.util.find_spec("smartpbx_diagnostics") is not None
    return importlib.import_module("smartpbx_diagnostics")


def test_diagnostics_enums_are_exact_str_enums_with_stable_values():
    diagnostics = load_diagnostics_module()
    expected = {
        "DiagnosticStage": (
            "SCHEMA_ADMISSION", "CONTEXT_VALIDATION", "SESSION_START", "AUDIO_INGESTION", "TTS", "HANDOVER", "TERMINAL_CLEANUP",
        ),
        "DiagnosticOutcome": (
            "REJECTED", "OBSERVED", "COMPLETED", "DISCONNECTED", "CANCELLED", "FAILED", "DEGRADED",
        ),
        "DiagnosticFailureClass": (
            "NONE", "DISABLED", "AUTHENTICATION", "CAPACITY", "INVALID_MESSAGE", "MESSAGE_TOO_BIG",
            "UNSUPPORTED_MEDIA_FORMAT", "INVALID_MEDIA", "AUDIO_TOO_BIG", "INVALID_DTMF", "UNSUPPORTED_EVENT",
            "START_REQUIRED", "ACCOUNT_MISMATCH", "CONTEXT_MISMATCH", "DUPLICATE_START", "CONNECTED_AFTER_START",
            "START_TIMEOUT", "IDLE_TIMEOUT", "TRANSFER_PENDING_TIMEOUT",
            "SESSION_FACTORY", "SESSION_START", "AUDIO_INGESTION",
            "TTS_MISSING_API_KEY", "TTS_PROFILE_FAILURE", "TTS_HTTP_STATUS", "TTS_TIMEOUT", "TTS_EXCEPTION",
            "HANDOVER_NOT_ACTIONABLE", "TRANSPORT_DISCONNECT", "TRANSPORT_SEND",
            "CANCELLED", "SESSION_CLEANUP", "TRANSPORT_CLEANUP", "LEASE_CLEANUP",
            "WEBSOCKET_CLOSE", "INTERNAL_ERROR",
        ),
    }

    for name, members in expected.items():
        enum_type = getattr(diagnostics, name)
        assert issubclass(enum_type, (str, Enum))
        assert tuple(member.name for member in enum_type) == members
        assert tuple(member.value for member in enum_type) == tuple(member.lower() for member in members)
        assert all(isinstance(member, str) and str(member) == member.value for member in enum_type)


def test_diagnostic_sink_has_only_three_typed_positional_enum_arguments():
    diagnostics = load_diagnostics_module()
    call = diagnostics.SmartPBXDiagnosticSink.__call__
    signature = inspect.signature(call)
    parameters = tuple(signature.parameters.values())

    assert tuple(parameter.name for parameter in parameters) == ("self", "stage", "outcome", "failure_class")
    assert tuple(parameter.kind for parameter in parameters) == (
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
    )
    assert tuple(parameter.annotation for parameter in parameters[1:]) == (
        diagnostics.DiagnosticStage,
        diagnostics.DiagnosticOutcome,
        diagnostics.DiagnosticFailureClass,
    )
    assert signature.return_annotation is None


def test_diagnostics_module_is_import_isolated_and_value_free():
    diagnostics = load_diagnostics_module()
    source = inspect.getsource(diagnostics)

    assert not {"smartpbx_gateway", "server", "CallContext", "payload", "correlation_id", "emit"} & set(diagnostics.__dict__)
    for forbidden in ("smartpbx_gateway", "server", "CallContext", "payload", "correlation_id", "emit"):
        assert forbidden not in source
