"""SmartPBX configuration, admission control, and safe runtime status."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import secrets
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable, Mapping, Protocol

from starlette.websockets import WebSocketDisconnect

from smartpbx_protocol import (
    ConnectedEvent, DtmfEvent, HangupEvent, MediaEvent, POLICY_VIOLATION,
    ProtocolViolation, StartEvent, StopEvent, UnknownEvent,
    parse_smartpbx_event, validate_event_context,
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

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> SmartPBXSettings:
        """Read bounded configuration without retaining a token in repr output."""
        enabled = _parse_enabled(environ.get("ENABLE_SMARTPBX_WSS", "false"))
        token = environ.get("SMARTPBX_WS_TOKEN", "")
        account_id = environ.get("SMARTPBX_ACCOUNT_ID", "")
        if not enabled:
            return cls(
                enabled=False,
                token="",
                account_id="",
                **{
                    attribute: default
                    for _, (attribute, default, _, _) in _INTEGER_SETTINGS.items()
                },
            )
        values = {
            attribute: _parse_bounded_integer(environ.get(name, str(default)), minimum, maximum)
            for name, (attribute, default, minimum, maximum) in _INTEGER_SETTINGS.items()
        }
        if not isinstance(token, str) or not isinstance(account_id, str):
            raise _configuration_error()
        settings = cls(
            enabled=enabled,
            token=token,
            account_id=account_id,
            **values,
        )
        if settings.enabled and not settings.configured:
            raise _configuration_error()
        return settings

    @property
    def configured(self) -> bool:
        return bool(self.token and self.account_id)

    def token_matches(self, candidate: str) -> bool:
        return isinstance(candidate, str) and bool(self.token) and secrets.compare_digest(
            self.token, candidate
        )


class SessionLease:
    """One admitted SmartPBX session, released at most once."""

    def __init__(self, registry: SmartPBXSessionRegistry) -> None:
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
        if isinstance(max_sessions, bool) or max_sessions < 1:
            raise ValueError("max_sessions must be positive")
        self._max_sessions = max_sessions
        self._active_sessions = 0
        self._admitted_total = 0
        self._rejected_capacity_total = 0
        self._released_total = 0
        self._lock = asyncio.Lock()

    async def try_acquire(self) -> SessionLease | None:
        async with self._lock:
            if self._active_sessions >= self._max_sessions:
                self._rejected_capacity_total = _saturating_increment(
                    self._rejected_capacity_total
                )
                return None
            self._active_sessions += 1
            self._admitted_total = _saturating_increment(self._admitted_total)
            return SessionLease(self)

    async def _release(self) -> None:
        async with self._lock:
            if self._active_sessions == 0:
                return
            self._active_sessions -= 1
            self._released_total = _saturating_increment(self._released_total)

    def snapshot(self) -> dict[str, int]:
        return {
            "active_sessions": self._active_sessions,
            "max_sessions": self._max_sessions,
            "admitted_total": self._admitted_total,
            "rejected_capacity_total": self._rejected_capacity_total,
            "released_total": self._released_total,
        }


def smartpbx_status(
    settings: SmartPBXSettings, registry: SmartPBXSessionRegistry
) -> dict[str, bool | int | str]:
    """Return only safe, bounded SmartPBX operational information."""
    return {
        "enabled": settings.enabled,
        "configured": settings.configured,
        **registry.snapshot(),
        "protocol_version": SMARTPBX_PROTOCOL_VERSION,
    }


class _GatewaySession(Protocol):
    @property
    def terminal_future(self) -> asyncio.Future: ...

    async def start(self) -> None: ...

    async def feed_audio(self, audio: bytes) -> None: ...

    async def finish(self, schedule_post_call: bool = False) -> None: ...


SessionFactory = Callable[[Any, SmartPBXMediaTransport], Awaitable[_GatewaySession]]


class _SessionState(Enum):
    NEW = "NEW"
    ACCEPTED = "ACCEPTED"
    STARTED = "STARTED"
    TERMINAL = "TERMINAL"


class SmartPBXGateway:
    """Authenticate and drive one bounded SmartPBX media session."""

    _TOKEN_HEADER = "X-Hutch-SmartPBX-Token"

    def __init__(
        self, settings: SmartPBXSettings, registry: SmartPBXSessionRegistry
    ) -> None:
        self._settings = settings
        self._registry = registry
        self._unknown_events_total = 0

    def snapshot(self) -> dict[str, bool | int | str]:
        return {
            **smartpbx_status(self._settings, self._registry),
            "unknown_events_total": self._unknown_events_total,
        }

    async def handle(self, websocket: Any, session_factory: SessionFactory) -> None:
        session_id = uuid.uuid4().hex
        started_at = time.monotonic()
        state = _SessionState.NEW
        call_fingerprint = ""
        outcome = "rejected"
        failure_class = ""
        close_outcome: tuple[int, str] | None = None
        disconnected = False
        cancellation: asyncio.CancelledError | None = None
        lease: SessionLease | None = None
        transport: SmartPBXMediaTransport | None = None
        session: _GatewaySession | None = None

        token = websocket.headers.get(self._TOKEN_HEADER, "")
        if not self._settings.enabled or not self._settings.configured:
            await _safe_close(websocket, POLICY_VIOLATION, "service unavailable")
            self._log_lifecycle(
                session_id, call_fingerprint, outcome, "disabled", started_at
            )
            return
        if not self._settings.token_matches(token):
            await _safe_close(websocket, POLICY_VIOLATION, "unauthorized")
            self._log_lifecycle(
                session_id, call_fingerprint, outcome, "authentication", started_at
            )
            return

        lease = await self._registry.try_acquire()
        if lease is None:
            await websocket.accept()
            await _safe_close(websocket, 1013, "capacity unavailable")
            self._log_lifecycle(
                session_id, call_fingerprint, outcome, "capacity", started_at
            )
            return

        try:
            await websocket.accept()
            state = _SessionState.ACCEPTED
            context = await self._receive_start(websocket)
            if context.account_id != self._settings.account_id:
                raise ProtocolViolation(
                    POLICY_VIOLATION, "account mismatch", "account_mismatch"
                )
            call_fingerprint = _fingerprint(context.call_id)
            transport = SmartPBXMediaTransport(
                websocket,
                context,
                max_queue_frames=self._settings.max_outbound_frames,
            )
            transport.start()
            session = await session_factory(context, transport)
            await session.start()
            state = _SessionState.STARTED

            while True:
                terminal = getattr(session, "terminal_future", None)
                if terminal is None:
                    raw = await asyncio.wait_for(
                        websocket.receive_text(),
                        timeout=self._settings.idle_timeout_seconds,
                    )
                else:
                    receive_task = asyncio.create_task(websocket.receive_text())
                    done, _ = await asyncio.wait(
                        {receive_task, terminal},
                        timeout=self._settings.idle_timeout_seconds,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if not done:
                        receive_task.cancel()
                        await asyncio.gather(receive_task, return_exceptions=True)
                        raise asyncio.TimeoutError
                    if terminal in done:
                        receive_task.cancel()
                        await asyncio.gather(receive_task, return_exceptions=True)
                        terminal_outcome = terminal.result()
                        transferred = bool(terminal_outcome.get("transferred"))
                        failure_class = _stable_failure_value(
                            terminal_outcome.get("failure_class")
                        )
                        outcome = "transferred" if transferred else "failed"
                        close_outcome = (
                            (1000, "transferred") if transferred
                            else (1011, "internal error")
                        )
                        break
                    raw = receive_task.result()
                event = parse_smartpbx_event(
                    raw,
                    max_message_chars=self._settings.max_message_chars,
                    max_audio_bytes=self._settings.max_audio_bytes,
                )
                if isinstance(event, StartEvent):
                    validate_event_context(event, context)
                    raise ProtocolViolation(
                        POLICY_VIOLATION, "duplicate start", "duplicate_start"
                    )
                if isinstance(event, MediaEvent):
                    await session.feed_audio(event.audio)
                elif isinstance(event, DtmfEvent):
                    self._log_event(
                        "smartpbx_dtmf_observed", session_id, call_fingerprint,
                        "ignored", "", started_at,
                    )
                elif isinstance(event, HangupEvent):
                    validate_event_context(event, context)
                    outcome = "hangup"
                    close_outcome = (1000, "call ended")
                    break
                elif isinstance(event, StopEvent):
                    outcome = "stop"
                    close_outcome = (1000, "call ended")
                    break
                elif isinstance(event, UnknownEvent):
                    self._observe_unknown()
                elif isinstance(event, ConnectedEvent):
                    raise ProtocolViolation(
                        POLICY_VIOLATION, "connected after start", "connected_after_start"
                    )
        except asyncio.TimeoutError:
            is_start_timeout = state is _SessionState.ACCEPTED
            failure_class = "start_timeout" if is_start_timeout else "idle_timeout"
            outcome = "timeout"
            close_outcome = (
                POLICY_VIOLATION,
                "start timeout" if is_start_timeout else "idle timeout",
            )
        except WebSocketDisconnect:
            disconnected = True
            outcome = "disconnect"
        except asyncio.CancelledError as error:
            cancellation = error
            outcome = "cancelled"
        except ProtocolViolation as error:
            outcome = "protocol_error"
            failure_class = error.failure_class
            close_outcome = (error.close_code, error.public_reason)
        except Exception as error:
            outcome = "failed"
            failure_class = _stable_failure_class(error)
            close_outcome = (1011, "internal error")
            self._log_event(
                "smartpbx_session_failed", session_id, call_fingerprint,
                outcome, failure_class, started_at, level=logging.ERROR,
            )
        finally:
            state = _SessionState.TERMINAL
            cleanup_task = asyncio.create_task(self._cleanup(
                session, transport, lease, session_id, call_fingerprint, started_at
            ))
            while not cleanup_task.done():
                try:
                    await asyncio.shield(cleanup_task)
                except asyncio.CancelledError as error:
                    if cancellation is None:
                        cancellation = error
            await cleanup_task
            if close_outcome is not None and not disconnected:
                await _safe_close(websocket, *close_outcome)
            self._log_lifecycle(
                session_id, call_fingerprint, outcome, failure_class, started_at
            )
            if cancellation is not None:
                raise cancellation

    async def _cleanup(
        self, session, transport, lease, session_id, call_fingerprint, started_at
    ):
        if session is not None:
            try:
                await asyncio.wait_for(
                    session.finish(schedule_post_call=True), timeout=5.0
                )
            except (Exception, asyncio.CancelledError):
                self._log_event(
                    "smartpbx_session_cleanup_failed", session_id,
                    call_fingerprint, "degraded", "session_cleanup",
                    started_at, level=logging.ERROR,
                )
        if transport is not None:
            try:
                await asyncio.wait_for(transport.close(), timeout=5.0)
            except (Exception, asyncio.CancelledError):
                self._log_event(
                    "smartpbx_transport_cleanup_failed", session_id,
                    call_fingerprint, "degraded", "transport_cleanup",
                    started_at, level=logging.ERROR,
                )
        if lease is not None:
            try:
                await asyncio.wait_for(lease.release(), timeout=5.0)
            except (Exception, asyncio.CancelledError):
                self._log_event(
                    "smartpbx_lease_cleanup_failed", session_id,
                    call_fingerprint, "degraded", "lease_cleanup",
                    started_at, level=logging.ERROR,
                )

    async def _receive_start(self, websocket: Any):
        deadline = (
            asyncio.get_running_loop().time()
            + self._settings.start_timeout_seconds
        )
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise asyncio.TimeoutError
            raw = await asyncio.wait_for(websocket.receive_text(), timeout=remaining)
            event = parse_smartpbx_event(
                raw,
                max_message_chars=self._settings.max_message_chars,
                max_audio_bytes=self._settings.max_audio_bytes,
            )
            if isinstance(event, StartEvent):
                return event.context
            if isinstance(event, ConnectedEvent):
                continue
            if isinstance(event, UnknownEvent):
                self._observe_unknown()
                continue
            raise ProtocolViolation(
                POLICY_VIOLATION, "start required", "start_required"
            )

    def _observe_unknown(self) -> None:
        self._unknown_events_total = _saturating_increment(
            self._unknown_events_total
        )

    def _log_lifecycle(
        self, session_id: str, call_fingerprint: str, outcome: str,
        failure_class: str, started_at: float,
    ) -> None:
        self._log_event(
            "smartpbx_session_ended", session_id, call_fingerprint,
            outcome, failure_class, started_at,
        )

    def _log_event(
        self, event: str, session_id: str, call_fingerprint: str,
        outcome: str, failure_class: str, started_at: float,
        *, level: int = logging.INFO,
    ) -> None:
        record = {
            "event": event[:64],
            "session_id": session_id,
            "call_fingerprint": call_fingerprint,
            "outcome": outcome[:64],
            "failure_class": failure_class[:64],
            "active_count": self._registry.snapshot()["active_sessions"],
            "duration": round(max(0.0, time.monotonic() - started_at), 3),
        }
        logger.log(level, "%s", json.dumps(record, sort_keys=True))


def _fingerprint(call_id: str) -> str:
    return hashlib.sha256(call_id.encode("utf-8")).hexdigest()[:12]


async def _safe_close(websocket: Any, code: int, reason: str) -> None:
    try:
        await websocket.close(code=code, reason=reason)
    except Exception:
        return


def _parse_enabled(value: str) -> bool:
    if not isinstance(value, str):
        raise _configuration_error()
    normalized = value.lower()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    raise _configuration_error()


def _parse_bounded_integer(value: str, minimum: int, maximum: int) -> int:
    if not isinstance(value, str) or not value.isdecimal():
        raise _configuration_error()
    parsed = int(value)
    if not minimum <= parsed <= maximum:
        raise _configuration_error()
    return parsed


def _saturating_increment(value: int) -> int:
    return min(value + 1, _MAX_COUNTER)


def _configuration_error() -> ValueError:
    return ValueError("invalid SmartPBX configuration")

_FAILURE_CLASSES = frozenset({
    "stt_unavailable", "stt_queue_overflow", "tts_unavailable", "tts_status",
    "tts_timeout", "tts_exception", "pipeline", "internal_error",
})


def _stable_failure_class(error: Exception) -> str:
    return _stable_failure_value(getattr(error, "failure_class", "internal_error"))


def _stable_failure_value(value: Any) -> str:
    return value if value in _FAILURE_CLASSES else "internal_error"
