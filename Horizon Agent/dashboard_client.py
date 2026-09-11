"""Dashboard ingest client — fire-and-forget POSTs to agent-dashboard.

Reuses the shared aiohttp session from booking_api (same pattern as
post_call.py:287-292). When required env vars are missing, every function
becomes a silent no-op. All errors are caught and logged; nothing raises.
"""

from __future__ import annotations

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
_TIMEOUT = aiohttp.ClientTimeout(total=5)

_announced = False


def _announce_once() -> None:
    """Log enabled/disabled status exactly once, on first public-API call."""
    global _announced
    if _announced:
        return
    _announced = True
    if _ENABLED:
        logger.info(
            "[dashboard] enabled → %s (agent_id=%s)",
            DASHBOARD_API_URL, DASHBOARD_AGENT_ID,
        )
    else:
        logger.info("[dashboard] disabled (env not set)")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

async def _post(payload: dict[str, Any]) -> None:
    """POST payload to the dashboard ingest endpoint. Never raises."""
    from booking_api import get_session

    url = f"{DASHBOARD_API_URL}{_INGEST_PATH}"
    headers = {"x-aether-secret": DASHBOARD_API_KEY}
    try:
        session = await get_session()
        async with session.post(
            url, json=payload, headers=headers, timeout=_TIMEOUT
        ) as resp:
            if resp.status < 300:
                logger.info(
                    "[dashboard] %s ok (%d)",
                    payload.get("eventType"), resp.status,
                )
            else:
                body = await resp.text()
                logger.warning(
                    "[dashboard] send failed: HTTP %d %s",
                    resp.status, body[:300],
                )
    except Exception as exc:
        # Use %r and include the exception type so empty-string exceptions
        # (e.g. aiohttp ServerDisconnectedError with no args) are visible.
        logger.warning(
            "[dashboard] send failed: type=%s repr=%r str=%r",
            type(exc).__name__, exc, str(exc),
        )


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
            "name": "Tanya",
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
) -> None:
    """Emit a call.completed event with full transcript + extracted summary."""
    _announce_once()
    if not _ENABLED:
        return

    follow_up = extracted.get("follow_up_needed") == "Yes"
    outcome = extracted.get("call_outcome")
    summary = extracted.get("summary")

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
            "metadata": {
                "language": lang,
                "guest_name": extracted.get("guest_name"),
                "num_guests": extracted.get("num_guests"),
                "check_in": extracted.get("check_in"),
                "check_out": extracted.get("check_out"),
                "room_preference": extracted.get("room_preference"),
                "availability_result": extracted.get("availability_result"),
            },
        },
    }
    await _post(payload)


async def send_call_transferred(
    call_sid: str,
    caller_phone: str,
    reason: str,
    human_phone: str,
) -> None:
    """Emit a call.transferred event when a call is handed off to a human."""
    from datetime import datetime, timezone

    _announce_once()
    if not _ENABLED:
        return

    payload = {
        "eventType": "call.transferred",
        "occurredAt": datetime.now(timezone.utc).isoformat(),
        "summary": f"Transferred to human: {reason}",
        "severity": "info",
        "channel": "voice",
        "contact": caller_phone,
        "agent": {
            "id": DASHBOARD_AGENT_ID,
            "name": "Tanya",
            "type": "booking",
        },
        "call": {
            "id": call_sid,
            "status": "transferred",
            "contact": caller_phone,
            "metadata": {
                "transfer_reason": reason,
                "human_phone": human_phone,
            },
        },
    }
    await _post(payload)
