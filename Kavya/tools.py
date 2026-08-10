"""
Claude tool definitions for the Hatton Hills voice agent (Kavya).

Defines the tool schemas that Claude uses for function calling and
dispatches tool invocations to the booking API (booking_api -> yanolja_service).

Hatton Hills is a SINGLE property — a luxury boutique eco retreat in Sri Lanka's
central hill country, reservations line +94 77 220 4400. It has exactly five room
types, all distinct:

  * Forest Escape Suite            (up to 2 guests)
  * Eco Harmony Suite              (up to 2 guests)
  * Sunrise Vista Premium Suite    (up to 2 guests)
  * Mount Luxe Chalet              (up to 5 guests)
  * Mount Monarch Chalet           (up to 5 guests)

Rates ARE published and the agent quotes them: US dollars per room per night,
half board, taxes included. The figures live in
yanolja_service.DEMO_NIGHTLY_RATE_USD and are invented for demonstrations.

SINGLE-PROPERTY MODE (2026-07-30). This module previously served two Mosvold
properties whose room names collided, so the `property` argument was REQUIRED and
dispatch failed closed with an "ask which property" error when it was missing.
Both of those are now gone: `property` is optional and `normalise_property`
always resolves to PROPERTY_HATTON. Leaving either in place would have made the
model ask a "which property?" question that has no valid answer, blocking every
booking. `_property_required_error` is kept but is unreachable.

The `property_name` plumbing itself is retained end to end (execute_tool ->
booking_api.check_availability / create_booking -> yanolja_service) so a second
property can be reintroduced by restoring the alias map, making `property`
required again, and letting `normalise_property` return None. Do not delete it.
"""

import asyncio
import json
import logging
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from booking_api import (
    check_availability,
    create_booking,
    retrieve_booking,
    cancel_booking,
    is_configured,
)

logger = logging.getLogger(__name__)


@dataclass
class SmartPBXTransferContext:
    """Per-utterance Dialog handover state; None deliberately means legacy Twilio."""

    call_control: Any | None
    coordinator: Any | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    transfer_attempted: bool = False


smartpbx_transfer_context: ContextVar[SmartPBXTransferContext | None] = ContextVar(
    "smartpbx_transfer_context", default=None
)

# ---------------------------------------------------------------------------
# Property / room vocabulary
# ---------------------------------------------------------------------------

PROPERTY_HATTON = "Hatton Hills"

PROPERTIES: tuple[str, ...] = (PROPERTY_HATTON,)

# The five Hatton Hills room types. All distinct, none a prefix of another, so a
# room request is never ambiguous. These strings must match
# yanolja_service.ROOM_TYPES_BY_PROPERTY and room_types.name in the PMS
# byte-for-byte — see ops/hattonhills-pms/rename_to_hattonhills.sql.
ROOM_TYPES_BY_PROPERTY: dict[str, tuple[str, ...]] = {
    PROPERTY_HATTON: (
        "Forest Escape Suite",
        "Eco Harmony Suite",
        "Sunrise Vista Premium Suite",
        "Mount Luxe Chalet",
        "Mount Monarch Chalet",
    ),
}

RESERVATIONS_PHONE = "+94 77 220 4400"

# Recognised mentions of the property. Resolution does not depend on this map —
# `normalise_property` falls back to the single property for anything unmatched.
_PROPERTY_ALIASES: dict[str, str] = {
    "hatton hills": PROPERTY_HATTON,
    "hatton": PROPERTY_HATTON,
    "hatton hills resort": PROPERTY_HATTON,
    "hill country": PROPERTY_HATTON,
}


def normalise_property(value: str | None) -> str:
    """Resolve a caller/LLM-supplied property string to the canonical name.

    SINGLE-PROPERTY MODE: always returns PROPERTY_HATTON and never None. Hatton
    Hills is the only property, so a missing or unrecognised value is not an
    error — it is the normal case, because nothing asks the guest which property
    they mean. Returning None here would fail the tool call closed on a question
    the guest cannot answer.

    Mirrors yanolja_service.resolve_property; keep the two in agreement."""
    if value:
        key = " ".join(str(value).lower().replace("-", " ").split()).strip(" .,")
        if key in _PROPERTY_ALIASES:
            return _PROPERTY_ALIASES[key]
        hits = {p for alias, p in _PROPERTY_ALIASES.items() if alias in key}
        if len(hits) == 1:
            return hits.pop()
    return PROPERTY_HATTON


def _property_required_error(raw_value: str | None) -> str:
    """JSON error telling the model to establish the property first.

    UNREACHABLE in single-property mode: `normalise_property` never returns a
    falsy value, so no dispatch path can reach this. Retained only so the
    two-property behaviour can be restored without rewriting dispatch."""
    logger.info("Tool call blocked — property not established (got %r)", raw_value)
    return json.dumps(
        {
            "error": "property_not_established",
            "message": (
                "Which property the guest wants was not established. Ask the "
                "guest which property they mean, then call this tool again."
            ),
            "valid_properties": list(PROPERTIES),
        }
    )


def _room_type_error(property_name: str, room_type: str) -> str:
    """JSON error for a room type that does not belong to the given property."""
    logger.info(
        "Tool call blocked — room type %r is not offered at %s", room_type, property_name
    )
    return json.dumps(
        {
            "error": "room_type_not_at_property",
            "message": (
                f"'{room_type}' is not a room type at {property_name}. Confirm the "
                "property and the room with the guest before booking."
            ),
            "property": property_name,
            "valid_room_types": list(ROOM_TYPES_BY_PROPERTY[property_name]),
        }
    )


def _matches_room_type(property_name: str, room_type: str) -> bool:
    """True when room_type names a room offered at property_name."""
    wanted = " ".join(str(room_type).lower().split())
    if not wanted:
        return False
    return any(
        wanted == candidate.lower() for candidate in ROOM_TYPES_BY_PROPERTY[property_name]
    )


# ---------------------------------------------------------------------------
# Tool definitions (Claude function-calling schema)
# ---------------------------------------------------------------------------

_PROPERTY_SCHEMA: dict[str, Any] = {
    "type": "string",
    "enum": list(PROPERTIES),
    "description": (
        "The property, always 'Hatton Hills'. OPTIONAL — Hatton Hills is the only "
        "property, so this defaults correctly when omitted. Do NOT ask the guest "
        "which property or which location they mean; there is only one."
    ),
}

TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "check_availability",
        "description": (
            "Check room availability at Hatton Hills for a given date range. "
            "Hatton Hills is the only property, so do NOT ask the guest which "
            "property or location they mean; 'property' is optional. "
            "Call this EXACTLY ONCE per booking inquiry — it returns results "
            "for ALL room types in a single response. Do NOT filter by "
            "room_type and do NOT call this multiple times in a row. The "
            "guest's room preference is irrelevant at availability check "
            "time; surface all available types from the single response. "
            "The result includes the nightly rate in US dollars for each "
            "available room type — you MAY quote those figures to the guest."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "property": _PROPERTY_SCHEMA,
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
                    "description": "Rate plan code. Hatton Hills is half board; 'HB'.",
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
            # 'property' deliberately NOT required — single-property mode.
            "required": ["check_in", "check_out"],
        },
    },
    {
        "name": "create_booking",
        "description": (
            "Create a new reservation at Hatton Hills. Use ONLY after "
            "confirming availability and getting explicit confirmation from "
            "the guest. Hatton Hills is the only property and 'property' is "
            "optional. The room_type must be one of the five Hatton Hills room "
            "types."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "property": _PROPERTY_SCHEMA,
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
                    "description": (
                        "Human-readable room type name. One of the five Hatton "
                        "Hills room types: 'Forest Escape Suite', 'Eco Harmony "
                        "Suite', 'Sunrise Vista Premium Suite' (each up to 2 "
                        "guests), 'Mount Luxe Chalet', 'Mount Monarch Chalet' "
                        "(each up to 5 guests). Use the full name exactly."
                    ),
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
                    "description": "Rate plan code. Hatton Hills is half board; 'HB'.",
                    "default": "BB",
                },
                "room_name": {
                    "type": "string",
                    "description": "Specific room name within the room type; defaults to room_type if omitted.",
                },
            },
            # 'property' deliberately NOT required — single-property mode.
            "required": ["check_in", "check_out", "room_type", "guest_name"],
        },
    },
    {
        "name": "retrieve_booking",
        "description": (
            "Look up an existing reservation at Hatton Hills. Use "
            "when a guest asks "
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
    {
        "name": "collect_number_via_keypad",
        "description": (
            "Collect a phone, WhatsApp, or callback NUMBER from the guest via "
            "their phone keypad (DTMF tones), which is 100% accurate — always "
            "use this instead of relying on spoken digits whenever you need a "
            "number. Say a brief lead-in first (e.g. 'Sure, let me take that on "
            "the keypad'); the tool then plays the precise 'key it in, then press "
            "hash' instruction and waits. It returns the collected digits and a "
            "spaced readback form, or a failure so you can ask the guest to say "
            "the number instead. Use ONLY for numbers — never for dates or guest "
            "counts, which stay on the spoken path."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "label": {
                    "type": "string",
                    "description": (
                        "What the number is for, in natural words, e.g. "
                        "'WhatsApp number for your confirmation' or 'best number "
                        "to call you back on'."
                    ),
                },
            },
            "required": ["label"],
        },
    },
]

# ---------------------------------------------------------------------------
# Failsafe handover tool
#
# Not part of TOOL_DEFINITIONS: it is offered ONLY in the recovery session that
# runs after a human agent failed to pick up the transferred call. Exposing it
# during a normal call would let Kavya promise a callback instead of doing the
# live transfer.
# ---------------------------------------------------------------------------

HANDOVER_TOOL_DEFINITION: dict[str, Any] = {
    "name": "notify_human_handover",
    "description": (
        "Send the guest's callback details to the property manager on WhatsApp. "
        "Call this ONCE, only in the recovery conversation that follows a failed "
        "transfer to a human, and only after you have the guest's name AND the "
        "WhatsApp number they want to be called back on. After this returns "
        "successfully, tell the guest a team member will call them back shortly."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "customer_name": {
                "type": "string",
                "description": "The guest's name as they gave it (first name is enough).",
            },
            "customer_whatsapp": {
                "type": "string",
                "description": (
                    "The guest's WhatsApp / callback number, digits only as the "
                    "guest said them (e.g. '0771234567' or '94771234567'). Do not "
                    "invent a number - confirm it with the guest first."
                ),
            },
            "call_summary": {
                "type": "string",
                "description": (
                    "Two or three sentences for the manager: what the guest wanted, "
                    "any dates, guest count and room type discussed, and exactly why "
                    "they asked for a human."
                ),
            },
        },
        "required": ["customer_name", "customer_whatsapp", "call_summary"],
    },
}


def get_handover_tools(fmt: str = "claude") -> list[dict[str, Any]]:
    """Return ONLY the failsafe handover tool, in the given provider format.

    `fmt` is one of "claude" (Anthropic), "openai", or "gemini". Unlike
    `get_tools()`, this does not depend on the booking API being configured -
    notifying the manager must work even when the PMS integration is down.
    """
    tool = HANDOVER_TOOL_DEFINITION
    if fmt == "openai":
        return [{
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool["description"],
                "parameters": tool["input_schema"],
            },
        }]
    if fmt == "gemini":
        return [{
            "function_declarations": [{
                "name": tool["name"],
                "description": tool["description"],
                "parameters": tool["input_schema"],
            }],
        }]
    return [tool]


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

def get_tools() -> list[dict[str, Any]]:
    """Return tool definitions (Anthropic format) if the booking API (n8n) is configured."""
    if is_configured():
        return TOOL_DEFINITIONS
    logger.warning("Booking API (n8n) is not configured — no tools will be available.")
    return []


def get_tools_openai() -> list[dict[str, Any]]:
    """Return tool definitions in OpenAI function-calling format."""
    if not is_configured():
        logger.warning("Booking API (n8n) is not configured — no tools will be available.")
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
    """
    if not is_configured():
        logger.warning("Booking API (n8n) is not configured — no tools will be available.")
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
    """Dispatch a tool call to the corresponding booking API function.

    SINGLE-PROPERTY MODE: `normalise_property` always resolves to "Hatton Hills",
    so the `property_name is None` guards below are unreachable. They are kept as
    the restore point for reintroducing a second property, and cost nothing.

    `create_booking` still validates the room type against the property's
    catalogue via `_matches_room_type`, and that check remains strict (exact
    name match): booking a guest into the wrong room type is worse than
    rejecting the call and making the model retry with the full name, which
    `_room_type_error` supplies.

    Parameters
    ----------
    tool_name:
        One of the registered tool names.
    tool_input:
        The arguments Claude provided for the tool call.

    Returns
    -------
    str
        A JSON-encoded string with the API result or an error payload.
    """
    transfer_context = smartpbx_transfer_context.get()
    if transfer_context is None:
        logger.info("Executing tool '%s' with input: %s", tool_name, tool_input)
    else:
        logger.info("smartpbx_tool event=execute tool=%s", tool_name)

    property_name: str | None = None

    if tool_name == "check_availability":
        property_name = normalise_property(tool_input.get("property"))
        if property_name is None:
            return _property_required_error(tool_input.get("property"))
        logger.info("check_availability for property: %s", property_name)

        result = await check_availability(
            check_in=tool_input["check_in"],
            check_out=tool_input["check_out"],
            room_type=tool_input.get("room_type"),
            room_name=tool_input.get("room_name"),
            num_adults=tool_input.get("num_adults", 1),
            num_children=tool_input.get("num_children", 0),
            rate_type=tool_input.get("rate_type", "BB"),
            salutation=tool_input.get("salutation", "Mr"),
            guest_name=tool_input.get("guest_name"),
            guest_phone=tool_input.get("guest_phone"),
            guest_email=tool_input.get("guest_email"),
            property_name=property_name,
        )

    elif tool_name == "create_booking":
        property_name = normalise_property(tool_input.get("property"))
        if property_name is None:
            return _property_required_error(tool_input.get("property"))

        requested_room = (tool_input.get("room_type") or "").strip()
        if not _matches_room_type(property_name, requested_room):
            return _room_type_error(property_name, requested_room)
        logger.info(
            "create_booking for property: %s, room type: %s", property_name, requested_room
        )

        # The line the guest is calling from (caller ID), stashed on the
        # per-call context by the WebSocket session. If the guest's dictated
        # number is unusable (wrong length -> normalize_whatsapp returns ""),
        # yanolja_service.book falls back to this so the booking still carries a
        # reachable WhatsApp number instead of storing garbage or nothing.
        from handover import handover_context

        caller_phone = (handover_context.get() or {}).get("caller_phone", "")

        result = await create_booking(
            check_in=tool_input["check_in"],
            check_out=tool_input["check_out"],
            room_type=tool_input["room_type"],
            guest_name=tool_input["guest_name"],
            salutation=tool_input.get("salutation", "Mr"),
            guest_email=tool_input.get("guest_email", ""),
            guest_phone=tool_input.get("guest_phone", ""),
            num_adults=tool_input.get("num_adults", 1),
            num_children=tool_input.get("num_children", 0),
            rate_type=tool_input.get("rate_type", "BB"),
            room_name=tool_input.get("room_name", ""),
            property_name=property_name,
            caller_phone=caller_phone,
        )

    elif tool_name == "retrieve_booking":
        result = await retrieve_booking(
            reservation_no=tool_input.get("reservation_no", ""),
            guest_email=tool_input.get("guest_email", ""),
            arrival_from=tool_input.get("arrival_from", ""),
            arrival_to=tool_input.get("arrival_to", ""),
        )

    elif tool_name == "cancel_booking":
        result = await cancel_booking(
            reservation_no=tool_input["reservation_no"],
        )

    elif tool_name == "transfer_to_human":
        if transfer_context is not None:
            if transfer_context.coordinator is not None:
                return await transfer_context.coordinator.attempt(tool_input.get("reason"))
            async with transfer_context.lock:
                if transfer_context.transfer_attempted:
                    return json.dumps({"status": "unavailable"})
                transfer_context.transfer_attempted = True
                if transfer_context.call_control is None:
                    return json.dumps({"status": "unavailable"})
                try:
                    transfer = await transfer_context.call_control.transfer_call(
                        "human_support"
                    )
                except Exception:
                    logger.info("smartpbx_tool event=transfer outcome=unavailable")
                    return json.dumps({"status": "unavailable"})
                if getattr(transfer, "transferred", False):
                    logger.info("smartpbx_tool event=transfer outcome=transferred")
                    return json.dumps({"status": "transferred"})
                logger.info("smartpbx_tool event=transfer outcome=unavailable")
                return json.dumps({"status": "unavailable"})
        reason = (tool_input.get("reason") or "").strip() or "Caller requested human assistance."
        return json.dumps({"status": "transferring", "reason": reason})

    elif tool_name == "notify_human_handover":
        from handover import handover_context, send_handover_notification

        ctx = handover_context.get() or {}
        outcome = await send_handover_notification(
            call_sid=ctx.get("call_sid", ""),
            customer_name=tool_input.get("customer_name", ""),
            customer_whatsapp=tool_input.get("customer_whatsapp", ""),
            call_summary=tool_input.get("call_summary", ""),
            human_agent_whatsapp=ctx.get("human_agent_whatsapp", ""),
        )
        if outcome.get("ok"):
            # Let the session skip its end-of-call safety net.
            ctx["notified"] = True
            return json.dumps({
                "status": "sent",
                "message": (
                    "The manager has been messaged. Tell the guest a team member "
                    "will call them back shortly."
                ),
            })
        if outcome.get("error") == "missing_customer_whatsapp":
            return json.dumps({
                "status": "invalid_number",
                "message": (
                    "That number was not usable. Ask the guest to repeat their "
                    "WhatsApp number digit by digit, then call this tool again."
                ),
            })
        return json.dumps({
            "status": "failed",
            "message": (
                "The message could not be sent right now, but the details are "
                "recorded. Reassure the guest that the team will call them back."
            ),
        })

    elif tool_name == "collect_number_via_keypad":
        # Real collection is intercepted at the session level, where the live
        # call and its DTMF stream are available. Reaching execute_tool means no
        # collector is wired (e.g. the Twilio path), so degrade gracefully and
        # let the model ask the guest to say the number.
        return json.dumps({"status": "unavailable", "reason": "keypad_not_available"})

    else:
        logger.error("Unknown tool requested: %s", tool_name)
        return json.dumps({"error": f"Unknown tool: {tool_name}"})

    # Echo the resolved property back so the model keeps the two properties
    # apart when it reads the result. This is presentation only -- the actual
    # scoping happens upstream: property_name is forwarded into
    # check_availability/create_booking, which pass it to yanolja_service.
    if property_name and isinstance(result, dict):
        result = {**result, "property": property_name}

    if transfer_context is None:
        logger.info("Tool '%s' returned: %s", tool_name, result)
    else:
        logger.info("smartpbx_tool event=result tool=%s", tool_name)
    return json.dumps(result)
