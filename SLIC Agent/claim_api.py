"""
Claim intake + assessor dispatch for the SLIC demo.

Implements the vehicle-first accident-hotline flow:
  1. verify_vehicle_policy     — reg_no -> policy lookup
  2. collect_accident_details  — stash structured intake (location/injuries/condition)
  3. dispatch_nearest_assessor — pick nearest assessor + generate claim ref
  4. send_confirmation_sms     — SMS the caller the ref + ETA
  5. request_live_agent_handoff — flag session for operator transfer

All functions are mocked except send_confirmation_sms, which uses the real
Twilio Programmable SMS API when TWILIO_* env vars are configured (falls back
to a mock SID when not). Replace the mocks with real SLIC APIs for production.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import string
from datetime import datetime, timezone
from typing import Any

import aiohttp

import active_session
import mock_db

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Shared aiohttp session (used by post_call.py's n8n webhook POST)
# ---------------------------------------------------------------------------
_http_session: aiohttp.ClientSession | None = None


async def get_session() -> aiohttp.ClientSession:
    """Return a shared aiohttp session, creating it on first use."""
    global _http_session
    if _http_session is None or _http_session.closed:
        _http_session = aiohttp.ClientSession()
    return _http_session


async def close_session() -> None:
    """Close the shared aiohttp session (call on app shutdown)."""
    global _http_session
    if _http_session is not None and not _http_session.closed:
        await _http_session.close()
        _http_session = None

# ---------------------------------------------------------------------------
# Mock assessor pool — covers major Sri Lankan cities
# ---------------------------------------------------------------------------
ASSESSORS: list[dict[str, Any]] = [
    {"id": "ASR-01", "name": "Ravi Kumar",       "base_city": "Colombo",     "eta_minutes": 12, "phone": "+94771234501"},
    {"id": "ASR-02", "name": "Nuwan Silva",      "base_city": "Kandy",       "eta_minutes": 15, "phone": "+94771234502"},
    {"id": "ASR-03", "name": "Priya Fernando",   "base_city": "Galle",       "eta_minutes": 18, "phone": "+94771234503"},
    {"id": "ASR-04", "name": "Dinesh Perera",    "base_city": "Negombo",     "eta_minutes": 14, "phone": "+94771234504"},
    {"id": "ASR-05", "name": "Shanika De Silva", "base_city": "Kurunegala",  "eta_minutes": 20, "phone": "+94771234505"},
    {"id": "ASR-06", "name": "Asanka Bandara",   "base_city": "Anuradhapura","eta_minutes": 25, "phone": "+94771234506"},
]

# ---------------------------------------------------------------------------
# In-memory staging for accident details between collect/dispatch
# ---------------------------------------------------------------------------
_PENDING_DETAILS: dict[str, dict[str, str]] = {}  # keyed by normalized reg_no


def is_configured() -> bool:
    """Always True — the demo backend is local and never unavailable."""
    return True


def _generate_claim_reference() -> str:
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    suffix = "".join(random.choices(string.ascii_uppercase + string.digits, k=6))
    return f"SLIC-CLM-{today}-{suffix}"


def _pick_nearest_assessor(location: str) -> dict[str, Any]:
    """Pick the assessor whose base city best matches the accident location.

    Case-insensitive substring match on base_city; falls back to the
    lowest-ETA assessor when no city matches.
    """
    loc = (location or "").lower()
    for assessor in ASSESSORS:
        if assessor["base_city"].lower() in loc:
            return assessor
    return min(ASSESSORS, key=lambda a: a["eta_minutes"])


# ---------------------------------------------------------------------------
# 1. Verify vehicle policy
# ---------------------------------------------------------------------------
async def verify_vehicle_policy(reg_no: str) -> dict[str, Any]:
    """Look up a vehicle registration against the SLIC policy database."""
    hit = mock_db.lookup_by_reg_no(reg_no)
    if hit is None:
        logger.info("verify_vehicle_policy: reg_no='%s' NOT FOUND", reg_no)
        return {
            "verified": False,
            "reason": "not_found",
            "message": (
                f"Vehicle registration '{reg_no}' is not on any active SLIC "
                "policy. Please transfer to a live agent."
            ),
        }

    customer = hit["customer"]
    vehicle = hit["vehicle"]
    logger.info(
        "verify_vehicle_policy: reg_no='%s' -> %s (%s %s)",
        reg_no, customer["name"], vehicle["make"], vehicle["model"],
    )
    return {
        "verified": True,
        "customer_name": customer["name"],
        "policy_no": customer["policy_no"],
        "make": vehicle["make"],
        "model": vehicle["model"],
        "reg_no": vehicle["reg_no"],
        "message": (
            f"Verified. This vehicle is insured under {customer['name']}'s "
            f"policy {customer['policy_no']}."
        ),
    }


# ---------------------------------------------------------------------------
# 2. Collect accident details
# ---------------------------------------------------------------------------
async def collect_accident_details(
    reg_no: str,
    location: str,
    injuries: str = "",
    vehicle_condition: str = "",
) -> dict[str, Any]:
    """Stash structured accident details for later dispatch."""
    key = mock_db._normalize_reg_no(reg_no)
    _PENDING_DETAILS[key] = {
        "location": location.strip(),
        "injuries": injuries.strip(),
        "vehicle_condition": vehicle_condition.strip(),
    }
    logger.info(
        "collect_accident_details: reg_no='%s' | location='%s'",
        reg_no, location,
    )
    return {
        "collected": True,
        "location": location,
        "message": "Accident details recorded.",
    }


# ---------------------------------------------------------------------------
# 3. Dispatch nearest assessor
# ---------------------------------------------------------------------------
async def dispatch_nearest_assessor(
    caller_phone: str,
    reg_no: str,
    location: str = "",
    injuries: str = "",
    vehicle_condition: str = "",
) -> dict[str, Any]:
    """Pick the nearest assessor and create an active session for this caller."""
    hit = mock_db.lookup_by_reg_no(reg_no)
    if hit is None:
        return {
            "dispatched": False,
            "message": (
                "Could not dispatch — vehicle not found on any active SLIC "
                "policy. A live agent will need to take this claim."
            ),
        }

    customer = hit["customer"]
    vehicle = hit["vehicle"]

    # Fill in any missing intake fields from the staging store
    key = mock_db._normalize_reg_no(reg_no)
    staged = _PENDING_DETAILS.get(key, {})
    location = location or staged.get("location", "")
    injuries = injuries or staged.get("injuries", "")
    vehicle_condition = vehicle_condition or staged.get("vehicle_condition", "")

    assessor = _pick_nearest_assessor(location)
    eta = int(assessor["eta_minutes"])
    claim_ref = _generate_claim_reference()

    # Phone from DB if we don't have a live one (some callers ring from alt numbers)
    session_phone = caller_phone or hit["phone"]

    active_session.set(
        session_phone,
        name=customer["name"],
        policy_no=customer["policy_no"],
        vehicle=vehicle,
        assessor_eta_minutes=eta,
        claim_reference=claim_ref,
        reg_no=vehicle["reg_no"],
        assessor_name=assessor["name"],
        accident_description=f"{location} | {injuries} | {vehicle_condition}".strip(" |"),
        location=location,
        injuries=injuries,
        vehicle_condition=vehicle_condition,
    )

    # Clear staged details once dispatched
    _PENDING_DETAILS.pop(key, None)

    logger.info(
        "assessor dispatched for %s | reg=%s | assessor=%s | ETA=%dmin | ref=%s",
        session_phone, vehicle["reg_no"], assessor["name"], eta, claim_ref,
    )

    return {
        "dispatched": True,
        "eta_minutes": eta,
        "claim_reference": claim_ref,
        "assessor_name": assessor["name"],
        "assessor_id": assessor["id"],
        "assessor_phone": assessor.get("phone", ""),
        "vehicle": vehicle,
        "message": (
            f"{assessor['name']} is the nearest assessor and is on the way. "
            f"Expected arrival in approximately {eta} minutes. "
            f"Your claim reference is {claim_ref}."
        ),
    }


# ---------------------------------------------------------------------------
# 4. Send confirmation via WhatsApp (n8n webhook)
# ---------------------------------------------------------------------------
_WHATSAPP_WEBHOOK = "https://automation.taskforceai.tech/webhook/slic"


async def send_confirmation_sms(
    caller_phone: str,
    claim_reference: str,
    eta_minutes: int,
    assessor_name: str = "",
    assessor_phone: str = "",
) -> dict[str, Any]:
    """Send the caller a WhatsApp confirmation via n8n webhook.

    Never raises — failures must not break the voice call.
    """
    if assessor_name and assessor_phone:
        body = (
            f"SLIC Motor Claims: We have received your accident report. "
            f"Your claim reference is {claim_reference}. "
            f"Your assigned assessor, {assessor_name} ({assessor_phone}), "
            f"is approximately {eta_minutes} minutes away. "
            f"Should you need any further assistance, please do not hesitate "
            f"to call us back on this number."
        )
    elif assessor_name:
        body = (
            f"SLIC Motor Claims: We have received your accident report. "
            f"Your claim reference is {claim_reference}. "
            f"Your assigned assessor, {assessor_name}, "
            f"is approximately {eta_minutes} minutes away. "
            f"Should you need any further assistance, please do not hesitate "
            f"to call us back on this number."
        )
    else:
        body = (
            f"SLIC Motor Claims: We have received your accident report. "
            f"Your claim reference is {claim_reference}. "
            f"An assessor will be with you in approximately {eta_minutes} minutes. "
            f"Should you need any further assistance, please do not hesitate "
            f"to call us back on this number."
        )

    if not caller_phone:
        logger.warning("send_confirmation_sms: no caller_phone — skipping")
        return {"sent": False, "error": "no caller_phone"}

    payload = {
        "phone": caller_phone,
        "message": body,
        "claim_reference": claim_reference,
    }

    try:
        session = await get_session()
        async with session.post(_WHATSAPP_WEBHOOK, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            resp_text = await resp.text()
            if resp.status < 300:
                logger.info("WhatsApp sent | to=%s | status=%s", caller_phone, resp.status)
                return {"sent": True, "mocked": False}
            else:
                logger.error("WhatsApp webhook error | to=%s | status=%s | body=%s", caller_phone, resp.status, resp_text)
                return {"sent": False, "error": f"webhook {resp.status}: {resp_text}"}
    except Exception as exc:
        logger.exception("WhatsApp send failed for %s", caller_phone)
        return {"sent": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# 5. Request live-agent handoff
# ---------------------------------------------------------------------------
async def request_live_agent_handoff(
    caller_phone: str,
    reason: str = "unspecified",
) -> dict[str, Any]:
    """Flag this call for transfer to a human operator.

    The actual ConversationRelay end/handoff JSON is emitted by the WebSocket
    handler when it sees this tool result. This function just records the
    intent and returns a spoken confirmation line.
    """
    logger.info(
        "live-agent handoff requested | caller=%s | reason=%s",
        caller_phone, reason,
    )
    return {
        "handoff": True,
        "reason": reason,
        "message": (
            "I'm transferring your call to a live SLIC agent now. "
            "A representative will be with you shortly. "
            "Please stay on the line."
        ),
    }
