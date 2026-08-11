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
# A Sri Lankan NSN is exactly 9 digits (e.g. 77 123 4567). This is the check
# that makes a too-short/too-long local number DETECTABLE, instead of it being
# silently turned into a plausible-looking wrong 94... number.
_NSN_LENGTH = 9

# E.164 sanity bounds for a full international number (country code + national
# number), in digits. The standard caps the total at 15; anything shorter than
# 8 is too mangled to be a real number. Used only to ACCEPT or REJECT a
# genuinely foreign number -- never to reshape a Sri Lankan one. The 8-digit
# floor applies where an explicit international signal is present (a leading
# "00" access code, or the 94 country code itself).
_E164_MIN_DIGITS = 8
_E164_MAX_DIGITS = 15

# A number given with NO country-code signal at all (no leading 0, no "00", not
# 94-prefixed) is either a Sri Lankan NSN (9 digits) or a foreign number the
# guest dictated WITH its country code -- which makes it at least 10 digits.
# Anything shorter is a fumbled local number, not a real international one, so
# it is rejected rather than stored as a too-short stray.
_BARE_INTL_MIN_DIGITS = _NSN_LENGTH + 1  # 10


def is_valid_lk_nsn(nsn: str) -> bool:
    """True if `nsn` is a plausible Sri Lankan national significant number.

    A Sri Lankan NSN is exactly nine digits (mobiles lead with 7, e.g.
    77 123 4567). This single check is what lets us tell a complete local
    number from a fumbled one, instead of prefixing 94 onto whatever length we
    happened to be handed.

    >>> is_valid_lk_nsn("771234567")
    True
    >>> is_valid_lk_nsn("74294451")     # 8 digits -- a digit was dropped
    False
    >>> is_valid_lk_nsn("7712345678")   # 10 digits -- a digit too many
    False
    """
    return len(nsn) == _NSN_LENGTH and nsn.isdigit()


def _is_e164_sane(digits: str) -> bool:
    """True if `digits` is a plausible full international number (8-15 digits)."""
    return _E164_MIN_DIGITS <= len(digits) <= _E164_MAX_DIGITS and digits.isdigit()

# Per-call context, set by the WebSocket session before the LLM runs so the
# `notify_human_handover` tool handler can reach call metadata it never
# receives as tool arguments. A ContextVar (not a plain global) because every
# call is its own asyncio task and sessions overlap.
handover_context: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "handover_context", default=None
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

_SPOKEN_TO_LETTER = {
    "a": "A", "alpha": "A",
    "b": "B", "bravo": "B",
    "c": "C", "charlie": "C",
    "d": "D", "delta": "D",
    "e": "E", "echo": "E",
    "f": "F", "foxtrot": "F",
    "g": "G", "golf": "G",
    "h": "H", "hotel": "H",
    "i": "I", "india": "I",
    "j": "J", "juliett": "J",
    "k": "K", "kilo": "K",
    "l": "L", "lima": "L",
    "m": "M", "mike": "M",
    "n": "N", "november": "N",
    "are": "R",
    "o": "O", "oscar": "O", "oh": "O", "zero": "O", "nought": "O", "naught": "O",
    "p": "P", "papa": "P",
    "q": "Q", "quebec": "Q",
    "r": "R", "romeo": "R",
    "s": "S", "sierra": "S",
    "t": "T", "tango": "T",
    "u": "U", "uniform": "U",
    "v": "V", "victor": "V",
    "w": "W", "whiskey": "W", "doubleyou": "W", "you": "W",
    "x": "X", "xray": "X",
    "y": "Y", "yankee": "Y", "why": "Y",
    "z": "Z", "zulu": "Z",
}

_SPOKEN_NAME_REPEAT_WORDS = {
    "double": 2,
    "triple": 3,
    "treble": 3,
}

_NAME_SEPARATOR_TOKENS = {
    "for", "as", "in", "and", "is", "of", "the", "to", "with", "like", "and",
}


def _get_handover_context() -> dict[str, Any]:
    ctx = handover_context.get()
    if ctx is None:
        ctx = {}
        handover_context.set(ctx)
    return ctx


def _is_spoken_name_letter_token(raw: str) -> bool:
    token = raw.strip().lower()
    if not token:
        return False
    return bool(_map_spoken_name_token(token)) and token not in {"and", "or", "are"}


def _map_spoken_name_token(raw: str) -> str:
    token = raw.strip().lower()
    if not token:
        return ""
    if len(token) == 1 and token.isalpha():
        return token.upper()
    return _SPOKEN_TO_LETTER.get(token, "")


def _format_spelled_name_part(part: str) -> str:
    lowered = part.lower()
    if not lowered:
        return ""
    if len(set(lowered)) == 1:
        return part.upper()
    return lowered.title()


def assemble_spoken_name(raw: Any) -> str:
    """Assemble a spelled name from dictated letters into a title-cased string.

    Handles plain letters (``d i l s h a n``), NATO words (``B for Bravo``),
    repeat shorthand (``double e``, ``triple J``), mixed punctuation
    (commas/periods) and common spoken-artifacts (``double you`` as W, ``are``
    as R, ``why`` as Y, ``oh`` as O). The output is safe for booking fields:
    it only contains letters and spaces.

    This is a code-path helper; it is intentionally strict and will ignore
    words that cannot map cleanly to a name letter.
    """
    if not raw:
        return ""

    raw_text = str(raw).replace("-", " ")
    raw_text = raw_text.replace("|", " ")
    cleaned_text = re.sub(r"[.,]", " ", raw_text)
    tokens = re.findall(r"[A-Za-z]+", cleaned_text)
    current: list[str] = []

    i = 0
    while i < len(tokens):
        token = tokens[i].strip()
        if not token:
            i += 1
            continue
        low = token.lower()
        if low in _NAME_SEPARATOR_TOKENS:
            i += 1
            continue

        repeat_count = _SPOKEN_NAME_REPEAT_WORDS.get(low)
        if repeat_count:
            next_token = tokens[i + 1] if i + 1 < len(tokens) else ""
            letter = _map_spoken_name_token(next_token)
            if letter:
                # "double you" is spoken as a single letter (W), not two W's.
                current.append(letter if letter == "W" else (letter * repeat_count))
                i += 2
                continue

        letter = _map_spoken_name_token(low)
        if not letter:
            i += 1
            continue
        if low in {"you", "oh"}:
            prev_token = tokens[i - 1].lower() if i > 0 else ""
            next_token = tokens[i + 1].lower() if i + 1 < len(tokens) else ""
            if not (
                _is_spoken_name_letter_token(prev_token)
                or _is_spoken_name_letter_token(next_token)
            ):
                i += 1
                continue

        # Caller says "B for Bravo" or "D as in Delta". Use the first token
        # and skip the immediate descriptive suffix to prevent accidental
        # duplicate letters (B + Bravo).
        next_token = tokens[i + 1].lower() if i + 1 < len(tokens) else ""
        next_next = tokens[i + 2] if i + 2 < len(tokens) else ""
        next_next_next = tokens[i + 3] if i + 3 < len(tokens) else ""
        if (
            next_token in {"for", "in"}
            and _map_spoken_name_token(next_next) == letter
        ):
            i += 3
        elif (
            next_token == "as"
            and next_next.lower() == "in"
            and _map_spoken_name_token(next_next_next) == letter
        ):
            i += 4
        elif (
            next_token == "as"
            and _map_spoken_name_token(next_next) == letter
        ):
            i += 3
        else:
            i += 1

        current.append(letter)

    if not current:
        return ""
    return _format_spelled_name_part("".join(current))


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

    REQUIREMENT -- PHONE-NUMBER FIELDS ONLY. Never run this over free-form
    conversation text. "double" is a room type at Hatton Hills, so "I'd like a
    double, two nights" would be mangled into "22". This is safe only because
    its two callers (`normalize_whatsapp` and the booking phone field) hand it
    nothing but a dictated phone number. Do NOT reuse it on general text.

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


_TOKEN_SPLIT_RE = re.compile(r"[^0-9A-Za-z]+")


def spoken_number_to_digits(raw: Any) -> str:
    """Convert a dictated phone number to a bare digit string, deterministically.

    Handles a number spoken entirely in words ("nought seven six"), entirely in
    numerals ("076"), or a mix, plus the double/triple/treble repeat shorthand.
    This is the code's job so the LLM never has to do digit arithmetic in its
    head: it relays the caller's words verbatim and reads back what this returns.

    PHONE-NUMBER FIELDS ONLY, exactly like :func:`expand_spoken_repeats`: "double"
    is a room type at Hatton Hills, so this must never run over conversation text.

    >>> spoken_number_to_digits("triple seven")
    '777'
    >>> spoken_number_to_digits("treble two")
    '222'
    >>> spoken_number_to_digits("nought seven six")
    '076'
    >>> spoken_number_to_digits("oh seven one one seven five four double six eight")
    '0711754668'
    >>> spoken_number_to_digits("double oh")
    '00'
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


def normalize_whatsapp(raw: Any) -> str:
    """Normalise a spoken/dialled phone number to digits with a country code.

    Returns "" when the number is unusable OR the wrong length. It never
    invents digits: a Sri Lankan number that is too short or too long comes
    back as "" rather than being padded with a country code into a
    plausible-looking WRONG number. (The Booking 80 defect: a guest's
    "074294451" -- nine spoken digits with one dropped in transit -- used to be
    stored as "9474294451", a real-looking but wrong number.) Callers treat ""
    as "no usable number" and fall back to the caller's own line. Never raises.

    Sri Lankan numbers are validated to a 9-digit national significant number
    (local trunk `077...`, bare `77...`, or `94...`). Genuine international
    numbers are accepted on their own terms if they pass an E.164 sanity check
    (8-15 digits), so a foreign guest is not forced into Sri Lankan lengths --
    only absurd lengths are rejected.

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
    >>> normalize_whatsapp("double oh 44 7700 900123")
    '447700900123'
    >>> normalize_whatsapp("074294451")    # one digit dropped -- not padded to 94...
    ''
    >>> normalize_whatsapp("07742944510")  # one digit too many
    ''
    >>> normalize_whatsapp("77")           # far too short
    ''
    """
    if not raw:
        return ""
    # Single chokepoint: the same deterministic word->digit conversion used by
    # the live capture tool, so a wholly-spoken number ("nought seven six ...")
    # and its dialled equivalent normalise identically and cannot diverge.
    digits = spoken_number_to_digits(raw)
    if not digits:
        return ""

    # International access code: a leading "00" means the caller dialled out
    # internationally, so the rest is already a full country-code + number
    # ("0044 7700 900123" -> "447700900123"). This MUST come before the
    # local-trunk branch below, which strips every leading zero and would
    # otherwise turn "0044..." into a non-existent "94..." Sri Lankan number.
    # Note expand_spoken_repeats turns a spoken "double oh" into "00", so this
    # is exactly the input that reaches here. Keep it only if it is E.164-sane.
    if digits.startswith("00"):
        rest = digits[2:]
        return rest if _is_e164_sane(rest) else ""

    # Local trunk form: 0771234567 -> 771234567. Valid ONLY if the remaining
    # national number is exactly 9 digits; otherwise the caller gave a
    # wrong-length local number and we return "" rather than manufacture one.
    if digits.startswith("0"):
        nsn = digits.lstrip("0")
        return f"{DEFAULT_COUNTRY_CODE}{nsn}" if is_valid_lk_nsn(nsn) else ""

    # Already carries the Sri Lankan country code: 94 + 9-digit NSN.
    if digits.startswith(DEFAULT_COUNTRY_CODE):
        nsn = digits[len(DEFAULT_COUNTRY_CODE):]
        if is_valid_lk_nsn(nsn):
            return digits
        # A "94..." of the wrong local length is not a Sri Lankan number. It may
        # be a foreign number that merely happens to start 94 -- keep it only if
        # it is E.164-sane, otherwise reject it.
        return digits if _is_e164_sane(digits) else ""

    # Bare national significant number ("771234567").
    if is_valid_lk_nsn(digits):
        return f"{DEFAULT_COUNTRY_CODE}{digits}"

    # Anything else is treated as an already-complete international number
    # dictated with its country code (e.g. +1 415 555 0132 -> 14155550132).
    # With no country-code signal we require at least 10 digits: shorter than
    # that is a fumbled local number, not a real foreign one. Cap at the E.164
    # maximum so absurd lengths are rejected rather than stored as garbage.
    if _BARE_INTL_MIN_DIGITS <= len(digits) <= _E164_MAX_DIGITS:
        return digits
    return ""


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
    privacy_safe: bool = False,
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
        if privacy_safe:
            logger.info("smartpbx_handover event=not_actionable")
            return {"ok": False, "error": "missing_customer_whatsapp"}
        logger.error(
            "[handover] refusing to notify -- no usable customer WhatsApp number "
            "(call_sid=%s, raw=%r)", call_sid, customer_whatsapp,
        )
        return {"ok": False, "error": "missing_customer_whatsapp", "payload": payload}

    if privacy_safe and not payload["human_agent_whatsapp"]:
        logger.info("smartpbx_handover event=not_actionable")
        return {"ok": False, "error": "missing_human_agent_whatsapp"}

    url = f"{N8N_BASE_URL}{N8N_HANDOVER_WEBHOOK}"
    try:
        from booking_api import get_session

        session = await get_session()
        async with session.post(url, json=payload) as resp:
            if privacy_safe:
                if resp.status < 300:
                    logger.info("smartpbx_handover event=sent status=%d", resp.status)
                    return {"ok": True, "status": resp.status}
                logger.warning("smartpbx_handover event=failed status=%d", resp.status)
                return {"ok": False, "status": resp.status, "error": "delivery_failed"}
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
        if privacy_safe:
            logger.warning("smartpbx_handover event=failed outcome=exception")
            return {"ok": False, "error": "delivery_failed"}
        logger.exception("[handover] failed to POST handover payload: %s", exc)
        return {"ok": False, "error": repr(exc), "payload": payload}
