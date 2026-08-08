"""Call-local, privacy-safe Dialog handover lifecycle coordination."""

from __future__ import annotations

import asyncio
import json
import re
from collections import deque
from collections.abc import Sequence
from enum import Enum
from typing import Any, Awaitable, Callable


class HandoverPhase(str, Enum):
    IDLE = "idle"
    ACKNOWLEDGED = "acknowledged"
    IMMEDIATE_FAILED = "immediate_failed"


_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")
_MAX_REASON = 500
_MAX_TRANSCRIPT_MESSAGES = 8
_MAX_TRANSCRIPT_CHARS = 3000
_MAX_TRANSCRIPT_MESSAGE_CHARS = 512
# The gateway gives the whole session cleanup 5s; leave room for the rest.
_FINALIZE_RETRY_TIMEOUT = 2.0


def bound_reason(value: Any) -> str:
    value = _CONTROL.sub(" ", str(value or ""))
    value = " ".join(value.split())[:_MAX_REASON].strip()
    return value or "Caller requested human assistance."


def bound_transcript_tail(messages: Any) -> str:
    items: list[str] = []
    if isinstance(messages, Sequence) or (
        hasattr(messages, "__len__") and hasattr(messages, "__getitem__")
    ):
        size = len(messages)
        tail = messages[max(0, size - _MAX_TRANSCRIPT_MESSAGES):size]
    else:
        tail = deque(messages or (), maxlen=_MAX_TRANSCRIPT_MESSAGES)
    for message in tail:
        if not isinstance(message, dict) or message.get("role") not in {"user", "assistant"}:
            continue
        text = message.get("text")
        if not isinstance(text, str):
            continue
        cleaned = " ".join(_CONTROL.sub(" ", text[:_MAX_TRANSCRIPT_MESSAGE_CHARS]).split())
        if cleaned:
            items.append(f"{message['role'].title()}: {cleaned}")
    return "\n".join(items)[-_MAX_TRANSCRIPT_CHARS:]


class SmartPBXHandoverCoordinator:
    """Serialize one Dialog transfer and its bounded immediate-failure fallback."""

    def __init__(self, *, call_control: Any | None, pipeline: Any, call_sid: str,
                 caller_phone: str, transcript: Callable[[], Any],
                 dashboard_sender: Callable[..., Awaitable[None]] | None,
                 notification_sender: Callable[..., Awaitable[dict[str, Any]]] | None,
                 human_agent_whatsapp: str) -> None:
        self._call_control, self._pipeline = call_control, pipeline
        self._call_sid, self._caller_phone = call_sid, caller_phone
        self._transcript = transcript
        self._dashboard_sender, self._notification_sender = dashboard_sender, notification_sender
        self._human_agent_whatsapp = human_agent_whatsapp
        self._phase = HandoverPhase.IDLE
        self._lock = asyncio.Lock()
        self._result: str | None = None
        self._reason = "Caller requested human assistance."
        self._notification_state: str | None = None
        self._notification_attempts = 0

    @property
    def phase(self) -> HandoverPhase:
        return self._phase

    @property
    def transfer_pending(self) -> bool:
        return self._phase is HandoverPhase.ACKNOWLEDGED

    async def attempt(self, reason: Any) -> str:
        async with self._lock:
            if self._result is not None:
                return self._result
            self._reason = bound_reason(reason)
            self._result = json.dumps({"status": "unavailable", "notification": "failed"})
            try:
                result = await self._call_control.transfer_call("human_support") if self._call_control else None
            except asyncio.CancelledError:
                raise
            except Exception:
                result = None
            if getattr(result, "transferred", False):
                self._phase = HandoverPhase.ACKNOWLEDGED
                self._result = json.dumps({"status": "transferred", "confirmation": "provider_acknowledged"})
                try:
                    await self._enter_pending()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    pass
                await self._send_dashboard()
                return self._result
            self._phase = HandoverPhase.IMMEDIATE_FAILED
            notification = await self._deliver_notification()
            self._result = json.dumps({"status": "unavailable", "notification": notification})
            return self._result

    async def finalize_notification_retry(self) -> None:
        """Best-effort last retry, bounded against the caller's cleanup budget.

        This runs inside the shielded _finish_once, which the gateway allows 5s in
        total. Acquiring the lock is inside the timeout on purpose: an in-flight
        attempt() can hold it through its MCP retries, and waiting that out would
        keep the whole pipeline alive long after the guest hung up.
        """
        try:
            async with asyncio.timeout(_FINALIZE_RETRY_TIMEOUT):
                async with self._lock:
                    if self._phase is HandoverPhase.IMMEDIATE_FAILED and self._notification_state == "failed":
                        await self._deliver_notification()
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            return

    async def _enter_pending(self) -> None:
        entered = getattr(self._pipeline, "enter_transfer_pending", None)
        if entered is not None:
            result = entered()
            if hasattr(result, "__await__"):
                await result

    async def _send_dashboard(self) -> None:
        if self._dashboard_sender is None:
            return
        try:
            await self._dashboard_sender(call_sid=self._call_sid, caller_phone=self._caller_phone,
                reason=self._reason, human_phone="", transfer_target="human_support",
                transfer_provider="dialog_mcp", transfer_confirmation="provider_acknowledged",
                privacy_safe=True)
        except asyncio.CancelledError:
            raise
        except Exception:
            return

    async def _deliver_notification(self) -> str:
        if self._notification_state == "sent":
            return "sent"
        if self._notification_attempts >= 2:
            return self._notification_state or "failed"
        if self._notification_sender is None:
            self._notification_state = "not_actionable"
            return self._notification_state
        from handover import normalize_whatsapp

        if not normalize_whatsapp(self._caller_phone) or not normalize_whatsapp(self._human_agent_whatsapp):
            self._notification_state = "not_actionable"
            return self._notification_state
        self._notification_attempts += 1
        self._notification_state = "failed"
        summary = "Live transfer could not be started; please call the guest back. " + self._reason
        tail = bound_transcript_tail(self._transcript())
        if tail:
            summary += "\n\nLast exchanges:\n" + tail
        try:
            async with asyncio.timeout(5):
                outcome = await self._notification_sender(call_sid=self._call_sid, customer_name="Unknown",
                    customer_whatsapp=self._caller_phone, call_summary=summary[:3628],
                    human_agent_whatsapp=self._human_agent_whatsapp, privacy_safe=True)
        except asyncio.CancelledError:
            raise
        except Exception:
            self._notification_state = "failed"
            return self._notification_state
        if outcome.get("ok"):
            self._notification_state = "sent"
        elif outcome.get("error") == "missing_customer_whatsapp":
            self._notification_state = "not_actionable"
        else:
            self._notification_state = "failed"
        return self._notification_state
