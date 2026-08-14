"""handover.py -- WhatsApp-notify handover for questions outside the Hutch KB.

Selina (the Hutch voice agent) has no live call transfer -- there is no human
agent line to dial. When a caller's question genuinely cannot be answered from
the knowledge base, Selina collects the caller's name and a callback/WhatsApp
number and calls the `notify_human_handover` tool, which POSTs a payload to an
n8n webhook. n8n is responsible for WhatsApp-ing a Hutch operator so a human
can follow up later. This is a "fire and forget, fail open" notification: a
failed POST must never affect the live call.

Payload contract (n8n `/webhook/hutch-handover`, configurable via
N8N_HANDOVER_WEBHOOK -- this webhook does not exist in n8n yet; it is wired
here so the operator can build it later):

    {
      "call_sid": "CA123...",
      "customer_name": "Nimal",
      "customer_whatsapp": "94771234567",
      "call_summary": "Caller asked about enterprise SIM provisioning ...",
      "human_agent_whatsapp": "94770000000",
      "timestamp": "2026-08-14T08:30:00Z"
    }

Phone numbers are normalised to bare digits with a country code and NO
`@s.whatsapp.net` suffix -- ownership of JID formatting stays with n8n (see
the "n8n Post-Call Processor -- WhatsApp JID rules" section in the repo
CLAUDE.md for why that split matters).
"""

from __future__ import annotations

import contextvars
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

N8N_BASE_URL: str = os.getenv(
    "N8N_BASE_URL", "https://automation.taskforceai.tech"
).rstrip("/")

N8N_HANDOVER_WEBHOOK: str = os.getenv(
    "N8N_HANDOVER_WEBHOOK", "/webhook/hutch-handover"
)

# Sri Lanka. Callers give local numbers ("077...", "77...") far more often
# than international ones, so default to Hutch's own country code.
DEFAULT_COUNTRY_CODE: str = os.getenv("WHATSAPP_COUNTRY_CODE", "94").strip()

# National significant number length for the default country (94 + 9 digits).
# A Sri Lankan NSN is exactly 9 digits (e.g. 77 123 4567).
_NSN_LENGTH = 9

# E.164 sanity bounds for a full international number (country code + national
# number), in digits. Used only to ACCEPT or REJECT a genuinely foreign
# number -- never to reshape a Sri Lankan one.
_E164_MIN_DIGITS = 8
_E164_MAX_DIGITS = 15

# A number given with NO country-code signal at all (no leading 0, no "00",
# not 94-prefixed) is either a Sri Lankan NSN (9 digits) or a foreign number
# dictated WITH its country code (at least 10 digits). Anything shorter is a
# fumbled local number, not a real international one.
_BARE_INTL_MIN_DIGITS = _NSN_LENGTH + 1  # 10

# Per-call context, set by the session before the LLM runs so the
# `notify_human_handover` tool handler can reach call metadata it never
# receives as tool arguments. A ContextVar (not a plain global) because every
# call is its own asyncio task and sessions overlap.
handover_context: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "handover_context", default=None
)


def _get_handover_context() -> dict[str, Any]:
    ctx = handover_context.get()
    if ctx is None:
        ctx = {}
        handover_context.set(ctx)
    return ctx


# ---------------------------------------------------------------------------
# Deterministic spoken-number normalisation
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

_TOKEN_SPLIT_RE = re.compile(r"[^0-9A-Za-z]+")


def expand_spoken_repeats(raw: Any) -> str:
    """Expand spoken 'double'/'triple' shorthand in a dictated number.

    PHONE-NUMBER FIELDS ONLY -- never run this over free-form conversation
    text (it would mangle unrelated words that happen to contain "double").

    >>> expand_spoken_repeats("double seven")
    '77'
    >>> expand_spoken_repeats("triple two")
    '222'
    >>> expand_spoken_repeats("0771 754 double 6 8")
    '0771 754 66 8'
    """
    if not raw:
        return ""

    def _sub(m: "re.Match[str]") -> str:
        count = _REPEAT_WORDS[m.group(1).lower()]
        token = m.group(2).lower()
        digit = token if token.isdigit() else _SPOKEN_DIGITS[token]
        return digit * count

    return _REPEAT_RE.sub(_sub, str(raw))


def spoken_number_to_digits(raw: Any) -> str:
    """Convert a dictated phone number to a bare digit string, deterministically.

    Handles a number spoken entirely in words, entirely in numerals, or a mix,
    plus the double/triple/treble repeat shorthand. PHONE-NUMBER FIELDS ONLY.

    >>> spoken_number_to_digits("triple seven")
    '777'
    >>> spoken_number_to_digits("oh seven one one seven five four double six eight")
    '0711754668'
    >>> spoken_number_to_digits("0771234567")
    '0771234567'
    """
    if not raw:
        return ""
    expanded = expand_spoken_repeats(raw)
    parts: list[str] = []
    for token in _TOKEN_SPLIT_RE.split(expanded):
        if not token:
            continue
        low = token.lower()
        if low in _SPOKEN_DIGITS:
            parts.append(_SPOKEN_DIGITS[low])
        else:
            parts.append(re.sub(r"\D", "", token))
    return "".join(parts)


def is_valid_lk_nsn(nsn: str) -> bool:
    """True if `nsn` is a plausible Sri Lankan national significant number."""
    return len(nsn) == _NSN_LENGTH and nsn.isdigit()


def _is_e164_sane(digits: str) -> bool:
    return _E164_MIN_DIGITS <= len(digits) <= _E164_MAX_DIGITS and digits.isdigit()


def normalize_whatsapp(raw: Any) -> str:
    """Normalise a spoken/dialled phone number to digits with a country code.

    Returns "" when the number is unusable OR the wrong length. It never
    invents digits -- a Sri Lankan number that is too short or too long comes
    back as "" rather than being padded into a plausible-looking WRONG number.

    >>> normalize_whatsapp("+94 77 123 4567")
    '94771234567'
    >>> normalize_whatsapp("0771234567")
    '94771234567'
    >>> normalize_whatsapp("771234567")
    '94771234567'
    >>> normalize_whatsapp("77")
    ''
    """
    if not raw:
        return ""
    digits = spoken_number_to_digits(raw)
    if not digits:
        return ""

    # International access code ("00" -> already a full country code+number).
    if digits.startswith("00"):
        rest = digits[2:]
        return rest if _is_e164_sane(rest) else ""

    # Local trunk form: 0771234567 -> 771234567.
    if digits.startswith("0"):
        nsn = digits.lstrip("0")
        return f"{DEFAULT_COUNTRY_CODE}{nsn}" if is_valid_lk_nsn(nsn) else ""

    # Already carries the Sri Lankan country code.
    if digits.startswith(DEFAULT_COUNTRY_CODE):
        nsn = digits[len(DEFAULT_COUNTRY_CODE):]
        if is_valid_lk_nsn(nsn):
            return digits
        return digits if _is_e164_sane(digits) else ""

    # Bare national significant number.
    if is_valid_lk_nsn(digits):
        return f"{DEFAULT_COUNTRY_CODE}{digits}"

    # Otherwise treat as an already-complete international number.
    if _BARE_INTL_MIN_DIGITS <= len(digits) <= _E164_MAX_DIGITS:
        return digits
    return ""


def utc_timestamp() -> str:
    """Current UTC time as `2026-08-14T08:30:00Z`."""
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
    privacy_safe: bool = False,
) -> dict[str, Any]:
    """POST the handover payload to n8n.

    Returns `{"ok": bool, ...}`. Never raises -- a failed notification must
    not affect the live call. Until the operator builds the n8n workflow, the
    POST simply fails (connection error or 404) and is logged as a warning.
    """
    payload = build_payload(
        call_sid=call_sid,
        customer_name=customer_name,
        customer_whatsapp=customer_whatsapp,
        call_summary=call_summary,
        human_agent_whatsapp=human_agent_whatsapp,
    )

    if not payload["customer_whatsapp"]:
        if privacy_safe:
            logger.info("hutch_handover event=not_actionable")
        else:
            logger.error(
                "[handover] refusing to notify -- no usable caller WhatsApp "
                "number (call_sid=%s, raw=%r)", call_sid, customer_whatsapp,
            )
        return {"ok": False, "error": "missing_customer_whatsapp"}

    url = f"{N8N_BASE_URL}{N8N_HANDOVER_WEBHOOK}"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, json=payload)
        if resp.status_code < 300:
            if privacy_safe:
                logger.info("hutch_handover event=sent status=%d", resp.status_code)
            else:
                logger.info(
                    "[handover] notified for %s (%s) -- status %d",
                    payload["call_sid"], payload["customer_whatsapp"], resp.status_code,
                )
            return {"ok": True, "status": resp.status_code}
        if privacy_safe:
            logger.warning("hutch_handover event=failed status=%d", resp.status_code)
        else:
            logger.warning(
                "[handover] n8n webhook returned %d: %s",
                resp.status_code, resp.text[:500],
            )
        return {"ok": False, "status": resp.status_code, "error": "delivery_failed"}
    except Exception as exc:
        # Fail open, always. The webhook not existing yet is an EXPECTED state
        # until the operator builds it in n8n -- log at warning, not error.
        if privacy_safe:
            logger.warning("hutch_handover event=failed outcome=exception")
        else:
            logger.warning("[handover] failed to POST handover payload: %s", exc)
        return {"ok": False, "error": repr(exc)}
