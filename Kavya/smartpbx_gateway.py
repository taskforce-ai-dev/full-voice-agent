"""Kavya Dialog SmartPBX configuration, admission control, and lifecycle boundary."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import secrets
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping, Protocol

from starlette.websockets import WebSocketDisconnect

from smartpbx_protocol import (
    ConnectedEvent, DtmfEvent, HangupEvent, MediaEvent, POLICY_VIOLATION,
    ProtocolViolation, StartEvent, StopEvent, UnknownEvent, parse_smartpbx_event,
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


SessionFactory = Callable[[Any, SmartPBXMediaTransport], Awaitable[_GatewaySession]]


class SmartPBXGateway:
    """Authenticate and drive a single bounded Kavya SmartPBX media session."""

    def __init__(self, settings: SmartPBXSettings, registry: SmartPBXSessionRegistry) -> None:
        self._settings = settings
        self._registry = registry
        self._unknown_events_total = 0

    def snapshot(self) -> dict[str, bool | int | str]:
        return {**smartpbx_status(self._settings, self._registry), "unknown_events_total": self._unknown_events_total}

    async def handle(self, websocket: Any, session_factory: SessionFactory) -> None:
        session_id, started_at = uuid.uuid4().hex, time.monotonic()
        lease: SessionLease | None = None
        transport: SmartPBXMediaTransport | None = None
        session: _GatewaySession | None = None
        close_outcome: tuple[int, str] | None = None
        outcome, failure_class, disconnected, cancellation, call_fingerprint = "rejected", "", False, None, ""

        token = websocket.headers.get(self._settings.auth_header_name, "")
        if not self._settings.enabled or not self._settings.configured:
            await _safe_close(websocket, POLICY_VIOLATION, "service unavailable")
            self._log_lifecycle(session_id, call_fingerprint, outcome, "disabled", started_at)
            return
        if not self._settings.token_matches(token):
            await _safe_close(websocket, POLICY_VIOLATION, "unauthorized")
            self._log_lifecycle(session_id, call_fingerprint, outcome, "authentication", started_at)
            return

        lease = await self._registry.try_acquire()
        if lease is None:
            await _safe_close(websocket, 1013, "capacity unavailable")
            self._log_lifecycle(session_id, call_fingerprint, outcome, "capacity", started_at)
            return

        accepted = False
        try:
            await websocket.accept()
            accepted = True
            context = await self._receive_start(websocket)
            if context.account_id != self._settings.account_id:
                raise ProtocolViolation(POLICY_VIOLATION, "account mismatch", "account_mismatch")
            call_fingerprint = _fingerprint(context.call_id)
            transport = SmartPBXMediaTransport(websocket, context, max_queue_frames=self._settings.max_outbound_frames)
            transport.start()
            session = await session_factory(context, transport)
            await session.start()

            while True:
                raw = await self._receive_or_terminal(websocket, session)
                if raw is None:
                    outcome, close_outcome = "terminal", (1000, "call ended")
                    break
                event = parse_smartpbx_event(raw, max_message_chars=self._settings.max_message_chars, max_audio_bytes=self._settings.max_audio_bytes)
                if isinstance(event, StartEvent):
                    validate_event_context(event, context)
                    raise ProtocolViolation(POLICY_VIOLATION, "duplicate start", "duplicate_start")
                if isinstance(event, MediaEvent):
                    await session.feed_audio(event.audio)
                elif isinstance(event, DtmfEvent):
                    self._log_event("smartpbx_dtmf_observed", session_id, call_fingerprint, "ignored", "", started_at)
                elif isinstance(event, HangupEvent):
                    validate_event_context(event, context)
                    outcome, close_outcome = "hangup", (1000, "call ended")
                    break
                elif isinstance(event, StopEvent):
                    outcome, close_outcome = "stop", (1000, "call ended")
                    break
                elif isinstance(event, UnknownEvent):
                    self._unknown_events_total = _saturating_increment(self._unknown_events_total)
                elif isinstance(event, ConnectedEvent):
                    raise ProtocolViolation(POLICY_VIOLATION, "connected after start", "connected_after_start")
        except asyncio.TimeoutError:
            failure_class, outcome = ("start_timeout", "timeout") if not session else ("idle_timeout", "timeout")
            close_outcome = (POLICY_VIOLATION, "start timeout" if not session else "idle timeout")
        except WebSocketDisconnect:
            disconnected, outcome = True, "disconnect"
        except asyncio.CancelledError as error:
            cancellation, outcome = error, "cancelled"
        except ProtocolViolation as error:
            outcome, failure_class, close_outcome = "protocol_error", error.failure_class, (error.close_code, error.public_reason)
        except Exception as error:
            outcome, failure_class, close_outcome = "failed", _stable_failure_class(error), (1011, "internal error")
            self._log_event("smartpbx_session_failed", session_id, call_fingerprint, outcome, failure_class, started_at, level=logging.ERROR)
        finally:
            cleanup_task = asyncio.create_task(
                self._cleanup(session, transport, lease, session_id, call_fingerprint, started_at)
            )
            while not cleanup_task.done():
                try:
                    await asyncio.shield(cleanup_task)
                except asyncio.CancelledError as error:
                    if cancellation is None:
                        cancellation = error
            await cleanup_task
            if accepted and close_outcome is not None and not disconnected:
                await _safe_close(websocket, *close_outcome)
            self._log_lifecycle(session_id, call_fingerprint, outcome, failure_class, started_at)
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
                max_message_chars=self._settings.max_message_chars, max_audio_bytes=self._settings.max_audio_bytes,
            )
            if isinstance(event, StartEvent):
                return event.context
            if isinstance(event, UnknownEvent):
                self._unknown_events_total = _saturating_increment(self._unknown_events_total)
                continue
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
            done, _ = await asyncio.wait(
                {receive_task, terminal}, timeout=None, return_when=asyncio.FIRST_COMPLETED
            )
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

    async def _cleanup(self, session, transport, lease, session_id, call_fingerprint, started_at) -> None:
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
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                self._log_event(f"smartpbx_{name}_cleanup_failed", session_id, call_fingerprint, "degraded", f"{name}_cleanup", started_at, level=logging.ERROR)

    def _log_lifecycle(self, session_id, call_fingerprint, outcome, failure_class, started_at) -> None:
        self._log_event("smartpbx_session_ended", session_id, call_fingerprint, outcome, failure_class, started_at)

    def _log_event(self, event, session_id, call_fingerprint, outcome, failure_class, started_at, *, level=logging.INFO) -> None:
        logger.log(level, "%s", json.dumps({
            "event": event[:64], "session_id": session_id, "call_fingerprint": call_fingerprint,
            "outcome": outcome[:64], "failure_class": failure_class[:64],
            "active_count": self._registry.snapshot()["active_sessions"],
            "duration": round(max(0.0, time.monotonic() - started_at), 3),
        }, sort_keys=True))


async def _safe_close(websocket: Any, code: int, reason: str) -> None:
    try:
        await websocket.close(code=code, reason=reason)
    except Exception:
        pass


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


_FAILURE_CLASSES = frozenset({"stt_unavailable", "stt_queue_overflow", "tts_unavailable", "tts_status", "tts_timeout", "tts_exception", "pipeline", "internal_error"})


def _stable_failure_class(error: Exception) -> str:
    value = getattr(error, "failure_class", "internal_error")
    return value if value in _FAILURE_CLASSES else "internal_error"


def _fingerprint(call_id: str) -> str:
    return hashlib.sha256(call_id.encode("utf-8")).hexdigest()[:12]
