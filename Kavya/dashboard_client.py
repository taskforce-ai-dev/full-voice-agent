"""Dashboard ingest client — fire-and-forget POSTs to agent-dashboard.

Reuses the shared aiohttp session from booking_api (same pattern as
post_call.py:287-292). When required env vars are missing, every function
becomes a silent no-op. All errors are caught and logged; nothing raises.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level env config (read once at import)
# ---------------------------------------------------------------------------

DASHBOARD_API_URL = os.getenv("DASHBOARD_API_URL", "").rstrip("/")
DASHBOARD_API_KEY = os.getenv("DASHBOARD_API_KEY", "")
DASHBOARD_AGENT_ID = os.getenv("DASHBOARD_AGENT_ID", "")

_ENABLED = bool(DASHBOARD_API_URL and DASHBOARD_API_KEY and DASHBOARD_AGENT_ID)
_INGEST_PATH = "/api/webhooks/agent-events"
# 15s: a cold Vercel function + Neon wake can exceed 5s on the first hit
# after an idle spell — the exact window every post-call dispatch lives in.
_TIMEOUT = aiohttp.ClientTimeout(total=15)
_RETRY_DELAY_SECONDS = 2.0

_announced = False


def _announce_once(privacy_safe: bool = False) -> None:
    """Log enabled/disabled status exactly once, on first public-API call."""
    global _announced
    if _announced:
        return
    _announced = True
    if _ENABLED:
        if privacy_safe:
            logger.info("smartpbx_dashboard event=enabled")
        else:
            logger.info(
                "[dashboard] enabled → %s (agent_id=%s)",
                DASHBOARD_API_URL, DASHBOARD_AGENT_ID,
            )
    else:
        logger.info("[dashboard] disabled (env not set)")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

async def _post(payload: dict[str, Any], privacy_safe: bool = False) -> None:
    """POST payload to the dashboard ingest endpoint. Never raises.

    One retry after a short delay: the first hit after an idle spell often
    lands on a cold serverless instance and times out; the retry hits it warm.
    """
    for attempt in (1, 2):
        try:
            await _post_once(payload, privacy_safe=privacy_safe, attempt=attempt)
            return
        except Exception:
            if attempt == 1:
                await asyncio.sleep(_RETRY_DELAY_SECONDS)


async def _post_once(
    payload: dict[str, Any], *, privacy_safe: bool, attempt: int
) -> None:
    from booking_api import get_session

    url = f"{DASHBOARD_API_URL}{_INGEST_PATH}"
    headers = {"x-aether-secret": DASHBOARD_API_KEY}
    try:
        session = await get_session()
        async with session.post(
            url, json=payload, headers=headers, timeout=_TIMEOUT
        ) as resp:
            if resp.status < 300:
                if privacy_safe:
                    logger.info("smartpbx_dashboard event=sent status=%d", resp.status)
                else:
                    logger.info(
                        "[dashboard] %s ok (%d)",
                        payload.get("eventType"), resp.status,
                    )
            else:
                if privacy_safe:
                    logger.warning("smartpbx_dashboard event=failed status=%d", resp.status)
                else:
                    content = getattr(resp, "content", None)
                    if content is None:
                        body = (await resp.text())[:300]
                    else:
                        body = (await content.read(300)).decode("utf-8", "replace")
                    logger.warning(
                        "[dashboard] send failed: HTTP %d %s", resp.status, body,
                    )
    except Exception as exc:
        # Use %r and include the exception type so empty-string exceptions
        # (e.g. aiohttp ServerDisconnectedError with no args) are visible.
        # The type name carries no payload/PII, so privacy mode logs it too.
        if privacy_safe:
            logger.warning(
                "smartpbx_dashboard event=failed outcome=exception exc_type=%s attempt=%d",
                type(exc).__name__, attempt,
            )
        else:
            logger.warning(
                "[dashboard] send failed (attempt %d): type=%s repr=%r str=%r",
                attempt, type(exc).__name__, exc, str(exc),
            )
        raise


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def send_call_started(
    call_sid: str,
    caller_phone: str,
    lang: str,
    started_at_iso: str,
) -> None:
    """Emit a call.started event for a new inbound call."""
    _announce_once()
    if not _ENABLED:
        return

    payload = {
        "eventType": "call.started",
        "occurredAt": started_at_iso,
        "channel": "voice",
        "contact": caller_phone,
        "agent": {
            "id": DASHBOARD_AGENT_ID,
            "name": "Kavya",
            "type": "booking",
        },
        "call": {
            "id": call_sid,
            "direction": "inbound",
            "status": "active",
            "contact": caller_phone,
            "startedAt": started_at_iso,
            "metadata": {"language": lang},
        },
    }
    await _post(payload)


async def send_call_completed(
    call_sid: str,
    caller_phone: str,
    lang: str,
    started_at_iso: str,
    ended_at_iso: str,
    duration_sec: int,
    full_transcript: list[dict[str, str]],
    extracted: dict[str, Any],
    privacy_safe: bool = False,
    close_reason: str | None = None,
    close_code: int | None = None,
    guest_turns: int | None = None,
    agent_turns: int | None = None,
    barge_ins: int | None = None,
) -> None:
    """Emit a call.completed event with full transcript + extracted summary."""
    _announce_once(privacy_safe=privacy_safe)
    if not _ENABLED:
        return

    follow_up = extracted.get("follow_up_needed") == "Yes"
    outcome = extracted.get("call_outcome")
    summary = extracted.get("summary")

    metadata = {
        "language": lang,
        "guest_name": extracted.get("guest_name"),
        "num_guests": extracted.get("num_guests"),
        "check_in": extracted.get("check_in"),
        "check_out": extracted.get("check_out"),
        "room_preference": extracted.get("room_preference"),
        "availability_result": extracted.get("availability_result"),
    }
    # SmartPBX-only, privacy-safe (enum/count-only) close diagnostics. Omitted
    # entirely for callers (e.g. the Twilio path) that don't supply them, so
    # the existing metadata shape is unchanged there.
    if close_reason is not None:
        metadata["close_reason"] = close_reason
    if close_code is not None:
        metadata["close_code"] = close_code
    if guest_turns is not None:
        metadata["guest_turns"] = guest_turns
    if agent_turns is not None:
        metadata["agent_turns"] = agent_turns
    if barge_ins is not None:
        metadata["barge_ins"] = barge_ins

    payload = {
        "eventType": "call.completed",
        "occurredAt": ended_at_iso,
        "channel": "voice",
        "contact": caller_phone,
        "summary": summary,
        "agent": {"id": DASHBOARD_AGENT_ID},
        "call": {
            "id": call_sid,
            "status": "completed",
            "outcome": outcome,
            "startedAt": started_at_iso,
            "endedAt": ended_at_iso,
            "durationSec": duration_sec,
            "followUpRequired": follow_up,
            "metadata": metadata,
        },
    }
    await _post(payload, privacy_safe=privacy_safe)


async def send_call_transferred(
    call_sid: str,
    caller_phone: str,
    reason: str,
    human_phone: str = "",
    *,
    transfer_target: str | None = None,
    transfer_provider: str | None = None,
    transfer_confirmation: str | None = None,
    privacy_safe: bool = False,
) -> None:
    """Emit a call.transferred event when a call is handed off to a human."""
    from datetime import datetime, timezone

    _announce_once(privacy_safe=privacy_safe)
    if not _ENABLED:
        return

    metadata = {"transfer_reason": reason}
    if human_phone:
        metadata["human_phone"] = human_phone
    if transfer_target:
        metadata["transfer_target"] = transfer_target
    if transfer_provider:
        metadata["transfer_provider"] = transfer_provider
    if transfer_confirmation:
        metadata["transfer_confirmation"] = transfer_confirmation
    payload = {
        "eventType": "call.transferred",
        "occurredAt": datetime.now(timezone.utc).isoformat(),
        "summary": f"Transferred to human: {reason}",
        "severity": "info",
        "channel": "voice",
        "contact": caller_phone,
        "agent": {
            "id": DASHBOARD_AGENT_ID,
            "name": "Kavya",
            "type": "booking",
        },
        "call": {
            "id": call_sid,
            "status": "transferred",
            "contact": caller_phone,
            "metadata": metadata,
        },
    }
    await _post(payload, privacy_safe=privacy_safe)
