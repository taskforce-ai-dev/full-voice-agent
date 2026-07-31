"""
Claude tool definitions for the hotel voice agent.

Defines the tool schemas that Claude uses for function calling and
dispatches tool invocations to the booking API (n8n webhook integration).
"""

import json
import logging
from typing import Any

from booking_api import (
    check_availability,
    create_booking,
    retrieve_booking,
    cancel_booking,
    is_configured,
)

logger = logging.getLogger(__name__)

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
    """Dispatch a tool call to the corresponding eZee API function.

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
    logger.info("Executing tool '%s' with input: %s", tool_name, tool_input)

    if tool_name == "check_availability":
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
        )

    elif tool_name == "create_booking":
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

    else:
        logger.error("Unknown tool requested: %s", tool_name)
        return json.dumps({"error": f"Unknown tool: {tool_name}"})

    logger.info("Tool '%s' returned: %s", tool_name, result)
    return json.dumps(result)
