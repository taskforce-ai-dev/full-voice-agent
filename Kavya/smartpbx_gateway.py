"""Kavya Dialog SmartPBX configuration, admission control, and lifecycle boundary."""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping, Protocol

from starlette.websockets import WebSocketDisconnect

from smartpbx_diagnostics import (
    DiagnosticFailureClass, DiagnosticOutcome, DiagnosticStage, SmartPBXDiagnosticSink,
)
from smartpbx_protocol import (
    ConnectedEvent, DtmfEvent, HangupEvent, MediaEvent, POLICY_VIOLATION,
    ProtocolViolation, StartEvent, StopEvent, UnsupportedEvent, parse_smartpbx_event,
    validate_event_context,
)
from smartpbx_transport import SmartPBXMediaTransport


logger = logging.getLogger(__name__)

SMARTPBX_PROTOCOL_VERSION = "smartpbx-ai-provider-v06"
_MAX_COUNTER = (1 << 63) - 1
_INTEGER_SETTINGS = {
    "SMARTPBX_MAX_CALLS": ("max_calls", 4, 1, 4),
    "SMARTPBX_MAX_MESSAGE_CHARS": ("max_message_chars", 65536, 1024, 65536),
    "SMARTPBX_MAX_AUDIO_BYTES": ("max_audio_bytes", 32768, 160, 32768),
    "SMARTPBX_MAX_OUTBOUND_FRAMES": ("max_outbound_frames", 128, 1, 128),
    "SMARTPBX_START_TIMEOUT_SECONDS": ("start_timeout_seconds", 10, 1, 30),
    "SMARTPBX_IDLE_TIMEOUT_SECONDS": ("idle_timeout_seconds", 90, 10, 300),
}


@dataclass(frozen=True)
class SmartPBXSettings:
    enabled: bool
    token: str = field(repr=False)
    account_id: str
    max_calls: int
    max_message_chars: int
    max_audio_bytes: int
    max_outbound_frames: int
    start_timeout_seconds: int
    idle_timeout_seconds: int
    auth_header_name: str = "X-Kavya-SmartPBX-Token"

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> "SmartPBXSettings":
        enabled = _parse_enabled(environ.get("ENABLE_SMARTPBX_WSS", "false"))
        header = environ.get("SMARTPBX_AUTH_HEADER_NAME", "X-Kavya-SmartPBX-Token")
        if not isinstance(header, str) or not header.strip() or len(header) > 128:
            raise _configuration_error()
        if not enabled:
            return cls(False, "", "", **_default_integer_settings(), auth_header_name=header)
        token, account_id = environ.get("SMARTPBX_WS_TOKEN", ""), environ.get("SMARTPBX_ACCOUNT_ID", "")
        values = {
            attribute: _parse_bounded_integer(environ.get(name, str(default)), minimum, maximum)
            for name, (attribute, default, minimum, maximum) in _INTEGER_SETTINGS.items()
        }
        if not isinstance(token, str) or not isinstance(account_id, str):
            raise _configuration_error()
        settings = cls(True, token, account_id, **values, auth_header_name=header)
        if not settings.configured:
            raise _configuration_error()
        return settings

    @property
    def configured(self) -> bool:
        return bool(self.token and self.account_id)

    def token_matches(self, candidate: str) -> bool:
        return isinstance(candidate, str) and bool(self.token) and secrets.compare_digest(self.token, candidate)


class SessionLease:
    """One admitted SmartPBX session, released at most once."""

    def __init__(self, registry: "SmartPBXSessionRegistry") -> None:
        self._registry = registry
        self._released = False
        self._release_lock = asyncio.Lock()

    async def release(self) -> None:
        async with self._release_lock:
            if self._released:
                return
            await self._registry._release()
            self._released = True


class SmartPBXSessionRegistry:
    """Bound active sessions and retain saturating admission counters."""

    def __init__(self, max_sessions: int) -> None:
        if isinstance(max_sessions, bool) or not 1 <= max_sessions <= 4:
            raise ValueError("max_sessions must be between 1 and 4")
        self._max_sessions = max_sessions
        self._active_sessions = self._admitted_total = self._rejected_capacity_total = self._released_total = 0
        self._lock = asyncio.Lock()

    async def try_acquire(self) -> SessionLease | None:
        async with self._lock:
            if self._active_sessions >= self._max_sessions:
                self._rejected_capacity_total = _saturating_increment(self._rejected_capacity_total)
                return None
            self._active_sessions += 1
            self._admitted_total = _saturating_increment(self._admitted_total)
            return SessionLease(self)

    async def _release(self) -> None:
        async with self._lock:
            if self._active_sessions:
                self._active_sessions -= 1
                self._released_total = _saturating_increment(self._released_total)

    def snapshot(self) -> dict[str, int]:
        return {
            "active_sessions": self._active_sessions, "max_sessions": self._max_sessions,
            "admitted_total": self._admitted_total, "rejected_capacity_total": self._rejected_capacity_total,
            "released_total": self._released_total,
        }


def smartpbx_status(settings: SmartPBXSettings, registry: SmartPBXSessionRegistry) -> dict[str, bool | int | str]:
    """Return only safe bounded operational values."""
    return {"enabled": settings.enabled, "configured": settings.configured, **registry.snapshot(), "protocol_version": SMARTPBX_PROTOCOL_VERSION}


class _GatewaySession(Protocol):
    @property
    def terminal_future(self) -> asyncio.Future[None]: ...

    async def start(self) -> None: ...
    async def feed_audio(self, audio: bytes) -> None: ...
    async def finish(self, schedule_post_call: bool = False) -> None: ...


SessionFactory = Callable[[Any, SmartPBXMediaTransport, SmartPBXDiagnosticSink], Awaitable[_GatewaySession]]


class SmartPBXGateway:
    """Authenticate and drive a single bounded Kavya SmartPBX media session."""

    def __init__(self, settings: SmartPBXSettings, registry: SmartPBXSessionRegistry) -> None:
        self._settings = settings
        self._registry = registry

    def snapshot(self) -> dict[str, bool | int | str]:
        return smartpbx_status(self._settings, self._registry)

    async def handle(self, websocket: Any, session_factory: SessionFactory) -> None:
        correlation_id = f"spx-{secrets.token_hex(16)}"
        started_at = time.monotonic()
        sink_enabled = True

        def sink(
            stage: DiagnosticStage,
            outcome: DiagnosticOutcome,
            failure_class: DiagnosticFailureClass,
        ) -> None:
            if sink_enabled:
                self._emit_diagnostic(correlation_id, started_at, stage, outcome, failure_class)

        lease: SessionLease | None = None
        transport: SmartPBXMediaTransport | None = None
        session: _GatewaySession | None = None
        accepted = False
        disconnected = False
        cancellation: asyncio.CancelledError | None = None
        close_outcome: tuple[int, str] | None = None
        completed_normally = False

        token = websocket.headers.get(self._settings.auth_header_name, "")
        if not self._settings.enabled or not self._settings.configured:
            await _safe_close(websocket, POLICY_VIOLATION, "service unavailable")
            sink(DiagnosticStage.SCHEMA_ADMISSION, DiagnosticOutcome.REJECTED, DiagnosticFailureClass.DISABLED)
            sink_enabled = False
            return
        if not self._settings.token_matches(token):
            await _safe_close(websocket, POLICY_VIOLATION, "unauthorized")
            sink(DiagnosticStage.SCHEMA_ADMISSION, DiagnosticOutcome.REJECTED, DiagnosticFailureClass.AUTHENTICATION)
            sink_enabled = False
            return

        lease = await self._registry.try_acquire()
        if lease is None:
            await _safe_close(websocket, 1013, "capacity unavailable")
            sink(DiagnosticStage.SCHEMA_ADMISSION, DiagnosticOutcome.REJECTED, DiagnosticFailureClass.CAPACITY)
            sink_enabled = False
            return

        try:
            await websocket.accept()
            accepted = True
            context = await self._receive_start(websocket)
            if context.account_id != self._settings.account_id:
                raise ProtocolViolation(POLICY_VIOLATION, "account mismatch", "account_mismatch")
            transport = SmartPBXMediaTransport(
                websocket, context, max_queue_frames=self._settings.max_outbound_frames
            )
            transport.start()
            try:
                session = await session_factory(context, transport, sink)
            except Exception:
                sink(DiagnosticStage.SESSION_START, DiagnosticOutcome.FAILED, DiagnosticFailureClass.SESSION_FACTORY)
                close_outcome = (1011, "internal error")
                return
            try:
                await session.start()
            except Exception:
                sink(DiagnosticStage.SESSION_START, DiagnosticOutcome.FAILED, DiagnosticFailureClass.SESSION_START)
                close_outcome = (1011, "internal error")
                return
            sink(DiagnosticStage.SESSION_START, DiagnosticOutcome.COMPLETED, DiagnosticFailureClass.NONE)

            while True:
                raw = await self._receive_or_terminal(websocket, session)
                if raw is None:
                    close_outcome = (1000, "call ended")
                    completed_normally = True
                    break
                event = parse_smartpbx_event(
                    raw,
                    max_message_chars=self._settings.max_message_chars,
                    max_audio_bytes=self._settings.max_audio_bytes,
                )
                if isinstance(event, StartEvent):
                    validate_event_context(event, context)
                    raise ProtocolViolation(POLICY_VIOLATION, "duplicate start", "duplicate_start")
                if isinstance(event, MediaEvent):
                    try:
                        await session.feed_audio(event.audio)
                    except Exception:
                        sink(DiagnosticStage.AUDIO_INGESTION, DiagnosticOutcome.FAILED, DiagnosticFailureClass.AUDIO_INGESTION)
                        close_outcome = (1011, "internal error")
                        return
                elif isinstance(event, DtmfEvent):
                    validate_event_context(event, context)
                    sink(DiagnosticStage.CONTEXT_VALIDATION, DiagnosticOutcome.OBSERVED, DiagnosticFailureClass.NONE)
                elif isinstance(event, HangupEvent):
                    validate_event_context(event, context)
                    close_outcome = (1000, "call ended")
                    completed_normally = True
                    break
                elif isinstance(event, StopEvent):
                    close_outcome = (1000, "call ended")
                    completed_normally = True
                    break
                elif isinstance(event, UnsupportedEvent):
                    raise ProtocolViolation(POLICY_VIOLATION, "unsupported event", "unsupported_event")
                elif isinstance(event, ConnectedEvent):
                    raise ProtocolViolation(POLICY_VIOLATION, "connected after start", "connected_after_start")
        except asyncio.TimeoutError:
            if session is None:
                sink(DiagnosticStage.SCHEMA_ADMISSION, DiagnosticOutcome.REJECTED, DiagnosticFailureClass.START_TIMEOUT)
                close_outcome = (POLICY_VIOLATION, "start timeout")
            else:
                sink(DiagnosticStage.AUDIO_INGESTION, DiagnosticOutcome.FAILED, DiagnosticFailureClass.IDLE_TIMEOUT)
                close_outcome = (POLICY_VIOLATION, "idle timeout")
        except WebSocketDisconnect:
            disconnected = True
            sink(DiagnosticStage.TERMINAL_CLEANUP, DiagnosticOutcome.DISCONNECTED, DiagnosticFailureClass.TRANSPORT_DISCONNECT)
        except asyncio.CancelledError as error:
            cancellation = error
            sink(DiagnosticStage.TERMINAL_CLEANUP, DiagnosticOutcome.CANCELLED, DiagnosticFailureClass.CANCELLED)
        except ProtocolViolation as error:
            stage, outcome, failure = _protocol_diagnostic(error.failure_class)
            sink(stage, outcome, failure)
            close_outcome = (error.close_code, error.public_reason)
        except Exception:
            sink(DiagnosticStage.TERMINAL_CLEANUP, DiagnosticOutcome.FAILED, DiagnosticFailureClass.INTERNAL_ERROR)
            close_outcome = (1011, "internal error")
        finally:
            cleanup_task = asyncio.create_task(self._cleanup(session, transport, lease, sink))
            while not cleanup_task.done():
                try:
                    await asyncio.shield(cleanup_task)
                except asyncio.CancelledError as error:
                    if cancellation is None:
                        cancellation = error
                        sink(DiagnosticStage.TERMINAL_CLEANUP, DiagnosticOutcome.CANCELLED, DiagnosticFailureClass.CANCELLED)
            cleanup_degraded = await cleanup_task
            close_failed = False
            if accepted and close_outcome is not None and not disconnected:
                close_failed = not await _safe_close(websocket, *close_outcome)
                if close_failed:
                    sink(DiagnosticStage.TERMINAL_CLEANUP, DiagnosticOutcome.DEGRADED, DiagnosticFailureClass.WEBSOCKET_CLOSE)
            if completed_normally and cancellation is None and not cleanup_degraded and not close_failed:
                sink(DiagnosticStage.TERMINAL_CLEANUP, DiagnosticOutcome.COMPLETED, DiagnosticFailureClass.NONE)
            sink_enabled = False
            if cancellation is not None:
                raise cancellation

    async def _receive_start(self, websocket: Any):
        deadline = asyncio.get_running_loop().time() + self._settings.start_timeout_seconds
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise asyncio.TimeoutError
            event = parse_smartpbx_event(
                await asyncio.wait_for(websocket.receive_text(), timeout=remaining),
                max_message_chars=self._settings.max_message_chars,
                max_audio_bytes=self._settings.max_audio_bytes,
            )
            if isinstance(event, StartEvent):
                return event.context
            if isinstance(event, UnsupportedEvent):
                raise ProtocolViolation(POLICY_VIOLATION, "unsupported event", "unsupported_event")
            if isinstance(event, ConnectedEvent):
                continue
            raise ProtocolViolation(POLICY_VIOLATION, "start required", "start_required")

    async def _receive_or_terminal(self, websocket: Any, session: _GatewaySession) -> str | None:
        timeout = None if getattr(session, "transfer_pending", False) else self._settings.idle_timeout_seconds
        terminal = getattr(session, "terminal_future", None)
        if terminal is None:
            return await asyncio.wait_for(websocket.receive_text(), timeout=timeout)
        receive_task = asyncio.create_task(websocket.receive_text())
        done, _ = await asyncio.wait({receive_task, terminal}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
        if not done and getattr(session, "transfer_pending", False):
            done, _ = await asyncio.wait({receive_task, terminal}, return_when=asyncio.FIRST_COMPLETED)
        if not done:
            receive_task.cancel()
            await asyncio.gather(receive_task, return_exceptions=True)
            raise asyncio.TimeoutError
        if terminal in done:
            receive_task.cancel()
            await asyncio.gather(receive_task, return_exceptions=True)
            terminal.result()
            return None
        return receive_task.result()

    async def _cleanup(
        self,
        session: _GatewaySession | None,
        transport: SmartPBXMediaTransport | None,
        lease: SessionLease | None,
        sink: SmartPBXDiagnosticSink,
    ) -> bool:
        degraded = False
        failures = {
            "session": DiagnosticFailureClass.SESSION_CLEANUP,
            "transport": DiagnosticFailureClass.TRANSPORT_CLEANUP,
            "lease": DiagnosticFailureClass.LEASE_CLEANUP,
        }
        for name, operation in (
            ("session", None if session is None else session.finish(schedule_post_call=True)),
            ("transport", None if transport is None else transport.close()),
            ("lease", None if lease is None else lease.release()),
        ):
            if operation is None:
                continue
            task = asyncio.create_task(operation)
            try:
                await asyncio.wait_for(task, timeout=5)
            except (Exception, asyncio.CancelledError):
                degraded = True
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                sink(DiagnosticStage.TERMINAL_CLEANUP, DiagnosticOutcome.DEGRADED, failures[name])
        return degraded

    def _emit_diagnostic(
        self,
        correlation_id: str,
        started_at: float,
        stage: DiagnosticStage,
        outcome: DiagnosticOutcome,
        failure_class: DiagnosticFailureClass,
    ) -> None:
        logger.info("%s", json.dumps({
            "event": "smartpbx_protocol_diagnostic",
            "correlation_id": correlation_id,
            "stage": stage.value,
            "outcome": outcome.value,
            "failure_class": failure_class.value,
            "active_sessions": self._registry.snapshot()["active_sessions"],
            "duration_ms": round(max(0.0, time.monotonic() - started_at) * 1000),
        }, sort_keys=True))


def _protocol_diagnostic(failure_class: str) -> tuple[DiagnosticStage, DiagnosticOutcome, DiagnosticFailureClass]:
    try:
        failure = DiagnosticFailureClass(failure_class)
    except ValueError:
        return (DiagnosticStage.SCHEMA_ADMISSION, DiagnosticOutcome.FAILED, DiagnosticFailureClass.INTERNAL_ERROR)
    if failure in {
        DiagnosticFailureClass.ACCOUNT_MISMATCH,
        DiagnosticFailureClass.CONTEXT_MISMATCH,
        DiagnosticFailureClass.DUPLICATE_START,
        DiagnosticFailureClass.CONNECTED_AFTER_START,
    }:
        return (DiagnosticStage.CONTEXT_VALIDATION, DiagnosticOutcome.REJECTED, failure)
    return (DiagnosticStage.SCHEMA_ADMISSION, DiagnosticOutcome.REJECTED, failure)


async def _safe_close(websocket: Any, code: int, reason: str) -> bool:
    try:
        await websocket.close(code=code, reason=reason)
    except Exception:
        return False
    return True


def _default_integer_settings() -> dict[str, int]:
    return {attribute: default for _, (attribute, default, _, _) in _INTEGER_SETTINGS.items()}


def _parse_enabled(value: str) -> bool:
    if isinstance(value, str) and value.lower() in {"true", "false"}:
        return value.lower() == "true"
    raise _configuration_error()


def _parse_bounded_integer(value: str, minimum: int, maximum: int) -> int:
    if not isinstance(value, str) or not value.isdecimal() or not minimum <= int(value) <= maximum:
        raise _configuration_error()
    return int(value)


def _saturating_increment(value: int) -> int:
    return min(value + 1, _MAX_COUNTER)


def _configuration_error() -> ValueError:
    return ValueError("invalid SmartPBX configuration")
