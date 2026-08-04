"""SmartPBX configuration, admission control, and safe runtime status."""

from __future__ import annotations

import asyncio
import secrets
from dataclasses import dataclass, field
from typing import Mapping


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
        values = {
            attribute: _parse_bounded_integer(environ.get(name, str(default)), minimum, maximum)
            for name, (attribute, default, minimum, maximum) in _INTEGER_SETTINGS.items()
        }
        token = environ.get("SMARTPBX_WS_TOKEN", "")
        account_id = environ.get("SMARTPBX_ACCOUNT_ID", "")
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

    async def release(self) -> None:
        if self._released:
            return
        self._released = True
        await self._registry._release()


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
