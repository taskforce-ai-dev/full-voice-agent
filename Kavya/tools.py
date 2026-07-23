"""
Claude tool definitions for the Mosvold Boutique Hotels voice agent (Kavya).

Defines the tool schemas that Claude uses for function calling and
dispatches tool invocations to the booking API (booking_api -> yanolja_service).

Mosvold Boutique Hotels operates TWO properties on Sri Lanka's southern coast,
and a single reservations line (+94 77 335 8800) serves both:

  * Mosvold Villa       — Ahangama
  * Sundara by Mosvold  — Balapitiya

Room names overlap across the two properties ("Deluxe Double Room" and
"Deluxe Twin Room" exist at both under similar names), so the property MUST be
established with the caller before any room type is selected. The helpers below
enforce that at dispatch time.

Rates are NOT published: they are returned live per check-in / check-out date by
the external booking engine (BookingEye). No rate figure may be stated by the
agent; callers asking about price are directed to reservations on
+94 77 335 8800.

The property IS forwarded downstream: execute_tool resolves it via
normalise_property() and passes it as `property_name` into
booking_api.check_availability / create_booking, which forward it to
yanolja_service.derive_availability / book. Those scope room-type matching to
the named property and fail closed (returning an "ask which property" prompt)
when it is missing or unresolvable, so a "Sundara" caller can no longer be
quoted or booked into a Mosvold Villa room.

INTEGRATION TODO (left for a human): the backend wired up here is the Yanolja
PMS. If Mosvold's live engine is in fact BookingEye (property 1 = Mosvold Villa,
property 2 = Sundara), the property routing needs re-verifying against it — the
canonical property names used here match yanolja_service exactly, but no
BookingEye property or room identifier has been invented.
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
# Property / room vocabulary
# ---------------------------------------------------------------------------

MOSVOLD_VILLA = "Mosvold Villa"
SUNDARA = "Sundara by Mosvold"

PROPERTIES: tuple[str, ...] = (MOSVOLD_VILLA, SUNDARA)

# Room types per property. The same guest phrasing ("a deluxe double") is
# ambiguous across properties, which is why these are kept separate and why the
# property is a required tool argument.
ROOM_TYPES_BY_PROPERTY: dict[str, tuple[str, ...]] = {
    MOSVOLD_VILLA: (
        "Deluxe Double Room",
        "Deluxe Twin Room",
        "Family Suite",
        "Founders Suite",
    ),
    SUNDARA: (
        "Deluxe Double Room with Garden View",
        "Deluxe Double Room with Sea View",
        "Deluxe Twin Room with Sea View",
        "Beach Villa",
        "Family Villa with Pool",
    ),
}

RESERVATIONS_PHONE = "+94 77 335 8800"

# Only tokens that unambiguously name ONE property. Deliberately NOT here:
# bare "villa" (Sundara has a Beach Villa and a Family Villa with Pool) and bare
# "mosvold" (the estate name, shared by both properties) — both must fall through
# to None so the agent asks rather than guessing the wrong hotel.
_PROPERTY_ALIASES: dict[str, str] = {
    "mosvold villa": MOSVOLD_VILLA,
    "ahangama": MOSVOLD_VILLA,
    "mosvold villa ahangama": MOSVOLD_VILLA,
    "sundara": SUNDARA,
    "sundara by mosvold": SUNDARA,
    "balapitiya": SUNDARA,
    "sundara by mosvold balapitiya": SUNDARA,
}


def normalise_property(value: str | None) -> str | None:
    """Resolve a caller/LLM-supplied property string to a canonical name.

    Returns None when the value is missing or does not clearly identify one of
    the two properties — the caller must then be asked which property they mean.
    """
    if not value:
        return None
    key = " ".join(str(value).lower().replace("-", " ").split())
    key = key.strip(" .,")
    if key in _PROPERTY_ALIASES:
        return _PROPERTY_ALIASES[key]
    if "sundara" in key or "balapitiya" in key:
        return SUNDARA
    # Only "ahangama" disambiguates Mosvold Villa by substring. Bare "villa" and
    # "mosvold" are intentionally excluded: "Beach Villa" / "Family Villa" are
    # Sundara rooms, and "Mosvold" names the whole estate — matching either here
    # would silently route to the wrong property.
    if "ahangama" in key:
        return MOSVOLD_VILLA
    return None


def _property_required_error(raw_value: str | None) -> str:
    """JSON error telling the model to establish the property first."""
    logger.info("Tool call blocked — property not established (got %r)", raw_value)
    return json.dumps(
        {
            "error": "property_not_established",
            "message": (
                "Which property the guest wants was not established. Mosvold "
                "Boutique Hotels has two properties and several room names are "
                "shared between them. Ask the guest whether they mean Mosvold "
                "Villa in Ahangama or Sundara by Mosvold in Balapitiya, then "
                "call this tool again."
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
        "Which Mosvold property the guest is asking about — 'Mosvold Villa' "
        "(Ahangama) or 'Sundara by Mosvold' (Balapitiya). REQUIRED. One phone "
        "line serves both properties and room names overlap, so ask the guest "
        "which property they mean before calling this tool. Never guess."
    ),
}

TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "check_availability",
        "description": (
            "Check room availability at a Mosvold property for a given date "
            "range. Establish WHICH property first — Mosvold Villa in Ahangama "
            "or Sundara by Mosvold in Balapitiya — and pass it as 'property'; "
            "the call is rejected without it. "
            "Call this EXACTLY ONCE per booking inquiry — it returns results "
            "for ALL room types in a single response. Do NOT filter by "
            "room_type and do NOT call this multiple times in a row. The "
            "guest's room preference is irrelevant at availability check "
            "time; surface all available types from the single response. "
            "Do NOT quote any rate figure from or around this tool — Mosvold "
            f"publishes no rates; refer pricing questions to {RESERVATIONS_PHONE}."
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
            "required": ["property", "check_in", "check_out"],
        },
    },
    {
        "name": "create_booking",
        "description": (
            "Create a new reservation at a Mosvold property. Use ONLY after "
            "confirming availability and getting explicit confirmation from "
            "the guest. The property MUST already be established with the "
            "guest — 'Deluxe Double Room' and 'Deluxe Twin Room' exist at both "
            "Mosvold Villa and Sundara under similar names, so a room type on "
            "its own is ambiguous and the call is rejected without 'property'. "
            "The room_type must be one actually offered at that property."
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
                        "Human-readable room type name, and it must belong to the "
                        "property given above. "
                        "Mosvold Villa (Ahangama): 'Deluxe Double Room', "
                        "'Deluxe Twin Room', 'Family Suite', 'Founders Suite'. "
                        "Sundara by Mosvold (Balapitiya): 'Deluxe Double Room with "
                        "Garden View', 'Deluxe Double Room with Sea View', 'Deluxe "
                        "Twin Room with Sea View', 'Beach Villa', 'Family Villa with "
                        "Pool'. Never carry a Sundara room name over to Mosvold Villa "
                        "or vice versa."
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
                    "description": "Rate plan code, e.g. 'BB' (Bed & Breakfast).",
                    "default": "BB",
                },
                "room_name": {
                    "type": "string",
                    "description": "Specific room name within the room type; defaults to room_type if omitted.",
                },
            },
            "required": ["property", "check_in", "check_out", "room_type", "guest_name"],
        },
    },
    {
        "name": "retrieve_booking",
        "description": (
            "Look up an existing reservation at either Mosvold property. Use "
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
    """Dispatch a tool call to the corresponding booking API function.

    For the two date-specific booking tools the Mosvold property is validated
    first: Mosvold Boutique Hotels runs two properties off one phone line and
    several room names are shared between them, so a call that has not
    established the property is refused rather than guessed at.

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

    else:
        logger.error("Unknown tool requested: %s", tool_name)
        return json.dumps({"error": f"Unknown tool: {tool_name}"})

    # Echo the resolved property back so the model keeps the two properties
    # apart when it reads the result. This is presentation only -- the actual
    # scoping happens upstream: property_name is forwarded into
    # check_availability/create_booking, which pass it to yanolja_service.
    if property_name and isinstance(result, dict):
        result = {**result, "property": property_name}

    logger.info("Tool '%s' returned: %s", tool_name, result)
    return json.dumps(result)
