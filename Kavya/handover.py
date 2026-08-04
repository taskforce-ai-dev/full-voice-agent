"""handover.py -- Failsafe manager notification when a live handoff goes unanswered.

When a caller asks for a human, `server.py` dials `HUMAN_AGENT_PHONE`. If nobody
picks up, the caller is dropped back into Kavya. This module is the failsafe:
Kavya collects (or confirms) the guest's name and WhatsApp number and POSTs a
handover payload to n8n, which sends the property manager a WhatsApp message.

Payload contract (n8n `/webhook/kavya-handover`):

    {
      "call_sid": "CA123...",
      "customer_name": "Chanya",
      "customer_whatsapp": "94771234567",
      "call_summary": "Guest asked about availability Aug 1-3 ...",
      "human_agent_whatsapp": "94770000000",
      "timestamp": "2026-07-31T08:30:00Z"
    }

Phone numbers are normalised to bare digits with a country code and NO
`@s.whatsapp.net` suffix -- the n8n workflow owns JID formatting (see the
"n8n Post-Call Processor -- WhatsApp JID rules" section in the repo CLAUDE.md).
"""

from __future__ import annotations

import contextvars
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

N8N_BASE_URL: str = os.getenv(
    "N8N_BASE_URL", "https://automation.taskforceai.tech"
).rstrip("/")

N8N_HANDOVER_WEBHOOK: str = os.getenv(
    "N8N_HANDOVER_WEBHOOK", "/webhook/kavya-handover"
)

# Sri Lanka. Callers give local numbers ("077...", "77...") far more often than
# international ones, so we default to the property's own country code.
DEFAULT_COUNTRY_CODE: str = os.getenv("WHATSAPP_COUNTRY_CODE", "94").strip()

# National significant number length for the default country (94 + 9 digits).
_NSN_LENGTH = 9

# Per-call context, set by the WebSocket session before the LLM runs so the
# `notify_human_handover` tool handler can reach call metadata it never
# receives as tool arguments. A ContextVar (not a plain global) because every
# call is its own asyncio task and sessions overlap.
handover_context: contextvars.ContextVar[dict[str, Any]] = contextvars.ContextVar(
    "handover_context", default={}
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Spoken shorthand callers use when dictating a number: "double seven" -> "77",
# "triple two" -> "222". The operand may be a numeral ("double 7") or a spoken
# digit-word ("double seven"); "oh"/"o" count as zero.
_SPOKEN_DIGITS = {
    "zero": "0", "oh": "0", "o": "0", "nought": "0", "naught": "0",
    "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
    "six": "6", "seven": "7", "eight": "8", "nine": "9",
}

_REPEAT_WORDS = {"double": 2, "triple": 3, "treble": 3}

_REPEAT_RE = re.compile(
    r"\b(double|triple|treble)\b[\s.\-]*"
    r"(\d|zero|oh|o|nought|naught|one|two|three|four|five|six|seven|eight|nine)\b",
    re.IGNORECASE,
)


def expand_spoken_repeats(raw: Any) -> str:
    """Expand spoken 'double'/'triple' shorthand in a dictated number.

    Callers reading out a phone number constantly say "double seven" for "77"
    or "triple two" for "222". The system prompt already tells the LLM to expand
    these, but callers use them so often that the model occasionally passes the
    words straight through -- and the plain digit-strip in `normalize_whatsapp`
    would then silently DROP those digits ("double" is not a digit). Expanding
    them here deterministically means every digit survives regardless of what
    the model does. If the shorthand was already expanded upstream there is
    nothing to match and this is a harmless no-op.

    >>> expand_spoken_repeats("double seven")
    '77'
    >>> expand_spoken_repeats("triple two")
    '222'
    >>> expand_spoken_repeats("double 5")
    '55'
    >>> expand_spoken_repeats("0771 754 double 6 8")
    '0771 754 66 8'
    >>> expand_spoken_repeats("0771234567")
    '0771234567'
    """
    if not raw:
        return ""

    def _sub(m: "re.Match[str]") -> str:
        count = _REPEAT_WORDS[m.group(1).lower()]
        token = m.group(2).lower()
        digit = token if token.isdigit() else _SPOKEN_DIGITS[token]
        return digit * count

    return _REPEAT_RE.sub(_sub, str(raw))


def normalize_whatsapp(raw: Any) -> str:
    """Normalise a spoken/dialled phone number to digits with a country code.

    Returns "" when there is nothing usable. Never raises.

    >>> normalize_whatsapp("+94 77 123 4567")
    '94771234567'
    >>> normalize_whatsapp("0771234567")
    '94771234567'
    >>> normalize_whatsapp("771234567")
    '94771234567'
    >>> normalize_whatsapp("0771 754 double 6 8")
    '94771754668'
    >>> normalize_whatsapp("0044 7700 900123")
    '447700900123'
    >>> normalize_whatsapp("001 415 555 0132")
    '14155550132'
    """
    if not raw:
        return ""
    digits = re.sub(r"\D", "", expand_spoken_repeats(raw))
    if not digits:
        return ""

    # International access code: a leading "00" means the caller dialled out
    # internationally, so the rest is already a full country-code + number
    # ("0044 7700 900123" -> "447700900123"). Return it untouched. This MUST come
    # before the local-trunk branch below, which strips every leading zero and
    # would otherwise turn "0044..." into a non-existent "94..." Sri Lankan number.
    if digits.startswith("00"):
        return digits[2:]

    # Local trunk form: 0771234567 -> 771234567 -> 94771234567
    if digits.startswith("0"):
        digits = digits.lstrip("0")
        if not digits:
            return ""
        return f"{DEFAULT_COUNTRY_CODE}{digits}"

    if digits.startswith(DEFAULT_COUNTRY_CODE):
        return digits

    # Bare national significant number ("771234567").
    if len(digits) == _NSN_LENGTH:
        return f"{DEFAULT_COUNTRY_CODE}{digits}"

    # Anything else is already an international number (e.g. +1 415...).
    return digits


def utc_timestamp() -> str:
    """Current UTC time as `2026-07-31T08:30:00Z` (matches the payload sample)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_payload(
    *,
    call_sid: str,
    customer_name: str,
    customer_whatsapp: str,
    call_summary: str,
    human_agent_whatsapp: str,
) -> dict[str, str]:
    """Assemble the n8n handover payload with both numbers normalised."""
    return {
        "call_sid": (call_sid or "").strip() or "unknown",
        "customer_name": (customer_name or "").strip() or "Unknown",
        "customer_whatsapp": normalize_whatsapp(customer_whatsapp),
        "call_summary": (call_summary or "").strip(),
        "human_agent_whatsapp": normalize_whatsapp(human_agent_whatsapp),
        "timestamp": utc_timestamp(),
    }


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------

async def send_handover_notification(
    *,
    call_sid: str,
    customer_name: str,
    customer_whatsapp: str,
    call_summary: str,
    human_agent_whatsapp: str,
) -> dict[str, Any]:
    """POST the handover payload to n8n.

    Returns `{"ok": bool, ...}`. Never raises -- a failed notification must not
    break the live call.
    """
    payload = build_payload(
        call_sid=call_sid,
        customer_name=customer_name,
        customer_whatsapp=customer_whatsapp,
        call_summary=call_summary,
        human_agent_whatsapp=human_agent_whatsapp,
    )

    if not payload["customer_whatsapp"]:
        logger.error(
            "[handover] refusing to notify -- no usable customer WhatsApp number "
            "(call_sid=%s, raw=%r)", call_sid, customer_whatsapp,
        )
        return {"ok": False, "error": "missing_customer_whatsapp", "payload": payload}

    url = f"{N8N_BASE_URL}{N8N_HANDOVER_WEBHOOK}"
    try:
        from booking_api import get_session

        session = await get_session()
        async with session.post(url, json=payload) as resp:
            body = await resp.text()
            if resp.status < 300:
                logger.info(
                    "[handover] manager notified for %s (%s) -- status %d",
                    payload["call_sid"], payload["customer_whatsapp"], resp.status,
                )
                return {"ok": True, "status": resp.status, "payload": payload}
            logger.error(
                "[handover] n8n webhook returned %d: %s", resp.status, body[:500],
            )
            return {
                "ok": False,
                "status": resp.status,
                "error": body[:500],
                "payload": payload,
            }
    except Exception as exc:
        logger.exception("[handover] failed to POST handover payload: %s", exc)
        return {"ok": False, "error": repr(exc), "payload": payload}
