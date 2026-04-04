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
            "Use this when a guest asks about available rooms, rates, or "
            "whether they can stay on specific dates."
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
                "num_rooms": {
                    "type": "integer",
                    "description": "Number of rooms requested.",
                    "default": 1,
                },
                "num_adults": {
                    "type": "integer",
                    "description": "Number of adults.",
                    "default": 1,
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
                "guest_name": {
                    "type": "string",
                    "description": "Full name of the guest.",
                },
                "guest_phone": {
                    "type": "string",
                    "description": "Guest phone number.",
                },
                "guest_email": {
                    "type": "string",
                    "description": "Guest email address.",
                },
                "room_type_id": {
                    "type": "string",
                    "description": "Room type identifier.",
                },
                "num_rooms": {
                    "type": "integer",
                    "description": "Number of rooms to book.",
                    "default": 1,
                },
                "num_adults": {
                    "type": "integer",
                    "description": "Number of adults.",
                    "default": 1,
                },
                "special_requests": {
                    "type": "string",
                    "description": "Any special requests from the guest.",
                },
            },
            "required": ["check_in", "check_out", "guest_name"],
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
]

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
            num_rooms=tool_input.get("num_rooms", 1),
            num_adults=tool_input.get("num_adults", 1),
        )

    elif tool_name == "create_booking":
        result = await create_booking(
            check_in=tool_input["check_in"],
            check_out=tool_input["check_out"],
            guest_name=tool_input["guest_name"],
            guest_phone=tool_input.get("guest_phone", ""),
            guest_email=tool_input.get("guest_email", ""),
            room_type_id=tool_input.get("room_type_id", ""),
            num_rooms=tool_input.get("num_rooms", 1),
            num_adults=tool_input.get("num_adults", 1),
            special_requests=tool_input.get("special_requests", ""),
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

    else:
        logger.error("Unknown tool requested: %s", tool_name)
        return json.dumps({"error": f"Unknown tool: {tool_name}"})

    logger.info("Tool '%s' returned: %s", tool_name, result)
    return json.dumps(result)
