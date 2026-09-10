"""
Tool definitions for the Horizon (Vidya) inquiry-only voice agent.

Horizon is an INQUIRY-ONLY demo: it must never expose booking/availability/
cancellation/transfer tools. Unlike the HattonHills base — which gates tools on
whether the booking API is configured — Horizon returns NO tools BY
CONSTRUCTION, so inheriting a Yanolja/PMS credential from a shared .env can
never turn booking tools on. Enabling them requires the explicit opt-in env
`HORIZON_ENABLE_BOOKING_TOOLS=true`, which the demo never sets.
"""

import json
import logging
import os
import uuid
from datetime import datetime
from typing import Any

from booking_api import (
    check_availability,
    create_booking,
    retrieve_booking,
    cancel_booking,
    is_configured,
)

logger = logging.getLogger(__name__)

# Inquiry-only is the invariant. Tools are OFF unless a human deliberately opts
# in via this env var — never merely because a PMS key leaked in from a shared
# environment. `is_configured()` alone must NEVER be sufficient to expose tools.
_BOOKING_TOOLS_OPT_IN = os.getenv("HORIZON_ENABLE_BOOKING_TOOLS", "false").strip().lower() in (
    "1", "true", "yes",
)


def _tools_enabled() -> bool:
    """True only when booking tools are BOTH explicitly opted in AND configured."""
    if not _BOOKING_TOOLS_OPT_IN:
        return False
    return is_configured()

# ---------------------------------------------------------------------------
# Tool definitions (Claude function-calling schema)
# ---------------------------------------------------------------------------

TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "check_availability",
        "description": (
            "Check room availability at the hotel for a given date range. "
            "Call this EXACTLY ONCE per booking inquiry — it returns results "
            "for ALL room types in a single response. Do NOT filter by "
            "room_type and do NOT call this multiple times in a row. The "
            "guest's room preference is irrelevant at availability check "
            "time; surface all available types from the single response."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "check_in": {
                    "type": "string",
                    "description": "Check-in date in YYYY-MM-DD format.",
                },
                "check_out": {
                    "type": "string",
                    "description": "Check-out date in YYYY-MM-DD format.",
                },
                "num_adults": {
                    "type": "integer",
                    "description": "Number of adults.",
                    "default": 1,
                },
                "num_children": {
                    "type": "integer",
                    "description": "Number of children.",
                    "default": 0,
                },
                "rate_type": {
                    "type": "string",
                    "description": "Rate plan code, e.g. 'BB' (Bed & Breakfast).",
                    "default": "BB",
                },
                "salutation": {
                    "type": "string",
                    "description": "Guest salutation: Mr, Mrs, Ms, or Dr.",
                    "default": "Mr",
                },
                "guest_name": {
                    "type": "string",
                    "description": "Full name of the guest.",
                },
                "guest_phone": {
                    "type": "string",
                    "description": "Guest phone number with country code.",
                },
            },
            "required": ["check_in", "check_out"],
        },
    },
    {
        "name": "create_booking",
        "description": (
            "Create a new hotel reservation. Use ONLY after confirming "
            "availability and getting explicit confirmation from the guest."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "check_in": {
                    "type": "string",
                    "description": "Check-in date in YYYY-MM-DD format.",
                },
                "check_out": {
                    "type": "string",
                    "description": "Check-out date in YYYY-MM-DD format.",
                },
                "room_type": {
                    "type": "string",
                    "description": "Human-readable room type name, e.g. 'Mount Luxe' or 'Forest Escape Suite'.",
                },
                "guest_name": {
                    "type": "string",
                    "description": "Full name of the guest.",
                },
                "salutation": {
                    "type": "string",
                    "description": "Guest salutation: Mr, Mrs, Ms, or Dr.",
                    "default": "Mr",
                },
                "guest_phone": {
                    "type": "string",
                    "description": "Guest phone number with country code.",
                },
                "num_adults": {
                    "type": "integer",
                    "description": "Number of adults.",
                    "default": 1,
                },
                "num_children": {
                    "type": "integer",
                    "description": "Number of children.",
                    "default": 0,
                },
                "rate_type": {
                    "type": "string",
                    "description": "Rate plan code, e.g. 'BB' (Bed & Breakfast).",
                    "default": "BB",
                },
                "room_name": {
                    "type": "string",
                    "description": "Specific room name within the room type; defaults to room_type if omitted.",
                },
            },
            "required": ["check_in", "check_out", "room_type", "guest_name"],
        },
    },
    {
        "name": "retrieve_booking",
        "description": (
            "Look up an existing hotel reservation. Use when a guest asks "
            "about their booking status, wants to check reservation details, "
            "or provides a confirmation number. Can search by reservation "
            "number, email, or date range."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "reservation_no": {
                    "type": "string",
                    "description": "The reservation / confirmation number.",
                },
                "guest_email": {
                    "type": "string",
                    "description": "Email address used for the booking.",
                },
                "arrival_from": {
                    "type": "string",
                    "description": "Start of arrival date range in YYYY-MM-DD format.",
                },
                "arrival_to": {
                    "type": "string",
                    "description": "End of arrival date range in YYYY-MM-DD format.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "cancel_booking",
        "description": (
            "Cancel an existing reservation. ONLY use after getting explicit "
            "confirmation from the guest. Requires the reservation number."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "reservation_no": {
                    "type": "string",
                    "description": "The reservation / confirmation number to cancel.",
                },
            },
            "required": ["reservation_no"],
        },
    },
    {
        "name": "transfer_to_human",
        "description": (
            "Transfer the live phone call to a human agent. Call this ONLY when "
            "the caller explicitly asks to speak to a human, agent, manager, or "
            "real person. Do not use for routine questions you can answer yourself."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": "One short sentence summarising why the caller is being transferred (e.g. 'caller wants to discuss a special booking request').",
                },
            },
            "required": ["reason"],
        },
    },
]

# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

def get_tools() -> list[dict[str, Any]]:
    """Return tool definitions (Anthropic format). Inquiry-only: [] by construction."""
    if not _tools_enabled():
        logger.info("Horizon is inquiry-only — no tools exposed (booking tools not opted in).")
        return []
    return TOOL_DEFINITIONS


def get_tools_openai() -> list[dict[str, Any]]:
    """Return tool definitions in OpenAI function-calling format. Inquiry-only: [] by construction."""
    if not _tools_enabled():
        logger.info("Horizon is inquiry-only — no tools exposed (booking tools not opted in).")
        return []
    return [
        {
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool["description"],
                "parameters": tool["input_schema"],
            },
        }
        for tool in TOOL_DEFINITIONS
    ]


def get_tools_gemini() -> list[dict[str, Any]]:
    """Return tool definitions in Google Gemini native format.

    Returns a list with a single Tool dict containing all function declarations.
    Inquiry-only: [] by construction.
    """
    if not _tools_enabled():
        logger.info("Horizon is inquiry-only — no tools exposed (booking tools not opted in).")
        return []
    return [
        {
            "function_declarations": [
                {
                    "name": tool["name"],
                    "description": tool["description"],
                    "parameters": tool["input_schema"],
                }
                for tool in TOOL_DEFINITIONS
            ]
        }
    ]


async def execute_tool(tool_name: str, tool_input: dict[str, Any]) -> str:
    """Dispatch a tool call and return a JSON-encoded result string.

    DEMO-SAFE: This is a demonstration deployment. None of these tools touch a
    real PMS (Yanolja / eZee) or any n8n webhook. Each returns a plausible,
    realistically-shaped result so conversation quality is identical to
    production, but NOTHING is ever written to a real reservation system. The
    tool schemas above are unchanged, so the LLM behaves exactly as in prod.
    """
    logger.info("Executing tool '%s' (DEMO-SAFE) with input: %s", tool_name, tool_input)

    if tool_name == "check_availability":
        # DEMO-SAFE: no real PMS read — synthesize realistic availability.
        requested = (tool_input.get("room_type") or tool_input.get("room_name") or "").strip()
        all_types = [
            "Mount Monarch", "Mount Luxe", "Sunrise Vista",
            "Eco Harmony", "Forest Escape Suite",
        ]
        types_to_check = [requested] if requested else all_types
        rooms = [{"room_type_name": t, "available": True} for t in types_to_check]
        result = {
            "check_in": tool_input.get("check_in", ""),
            "check_out": tool_input.get("check_out", ""),
            "total_room_types": len(all_types),
            "available_room_types": len(rooms),
            "rooms": rooms,
        }

    elif tool_name == "create_booking":
        # DEMO-SAFE: no real PMS write — return a plausible confirmation.
        booking_ref = "HH" + datetime.now().strftime("%y%m") + uuid.uuid4().hex[:5].upper()
        result = {
            "success": True,
            "booking_reference": booking_ref,
            "guest_name": tool_input.get("guest_name", ""),
            "check_in": tool_input.get("check_in", ""),
            "check_out": tool_input.get("check_out", ""),
            "room_type": tool_input.get("room_type", ""),
            "room_number": "",
        }

    elif tool_name == "retrieve_booking":
        # DEMO-SAFE: no real PMS read — return a plausible confirmed booking.
        ref = (tool_input.get("reservation_no") or "").strip() or "your booking"
        result = {
            "success": True,
            "booking_reference": ref,
            "status": "confirmed",
            "message": "The reservation is confirmed and all set.",
        }

    elif tool_name == "cancel_booking":
        # DEMO-SAFE: no real PMS write — return a plausible cancellation.
        ref = (tool_input.get("reservation_no") or "").strip()
        if not ref:
            return json.dumps({"error": "We need a reservation number to cancel the booking."})
        result = {"success": True, "booking_reference": ref, "status": "cancelled"}

    elif tool_name == "transfer_to_human":
        # DEMO-SAFE: no real dial / handoff. Return a polite acknowledgement
        # only. We intentionally do NOT return status "transferring", so the
        # server's live Twilio-REST transfer path is never triggered.
        reason = (tool_input.get("reason") or "").strip() or "your request"
        return json.dumps({
            "status": "acknowledged",
            "reason": reason,
            "message": (
                "I completely understand. In a live deployment I would connect "
                "you with one of our team right away. For now, please let me "
                "know how else I can help, or leave your number for a callback."
            ),
        })

    else:
        logger.error("Unknown tool requested: %s", tool_name)
        return json.dumps({"error": f"Unknown tool: {tool_name}"})

    logger.info("Tool '%s' returned (DEMO-SAFE): %s", tool_name, result)
    return json.dumps(result)
