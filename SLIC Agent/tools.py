"""
Tool definitions for Nimali, the SLIC accident-claim voice agent.

Vehicle registration number is the primary identifier. The caller's phone
is still injected by the server (for SMS delivery + handoff context), so the
LLM never needs to ask for it and no phone field appears in any tool schema.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from claim_api import (
    collect_accident_details,
    dispatch_nearest_assessor,
    is_configured,
    request_live_agent_handoff,
    send_confirmation_sms,
    verify_vehicle_policy,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tool definitions (Anthropic native format — base schema)
# ---------------------------------------------------------------------------

TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "verify_vehicle_policy",
        "description": (
            "Verify a vehicle registration number against the SLIC policy "
            "database. Call this ONLY AFTER you have read the registration "
            "back to the caller digit-by-digit and they have confirmed it. "
            "Returns the customer's name, policy number, make, and model if "
            "the vehicle is on an active policy. If not found, you must call "
            "request_live_agent_handoff next."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "reg_no": {
                    "type": "string",
                    "description": (
                        "The vehicle registration number the caller provided "
                        "(e.g. 'CAB-1234' or 'CBA-1175')."
                    ),
                },
            },
            "required": ["reg_no"],
        },
    },
    {
        "name": "collect_accident_details",
        "description": (
            "Record the accident location. Call this ONLY AFTER you have "
            "asked the caller where the accident happened. The field "
            "assessor will capture injuries and vehicle condition on-site, "
            "so those fields can be left empty."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "reg_no": {
                    "type": "string",
                    "description": "The verified vehicle registration number.",
                },
                "location": {
                    "type": "string",
                    "description": (
                        "Where the accident happened. Include the road/area "
                        "and city if mentioned (e.g. 'Galle Road near Bambalapitiya')."
                    ),
                },
                "injuries": {
                    "type": "string",
                    "description": (
                        "Optional. Leave as empty string — captured by the "
                        "on-site assessor."
                    ),
                },
                "vehicle_condition": {
                    "type": "string",
                    "description": (
                        "Optional. Leave as empty string — captured by the "
                        "on-site assessor."
                    ),
                },
            },
            "required": ["reg_no", "location"],
        },
    },
    {
        "name": "dispatch_nearest_assessor",
        "description": (
            "Dispatch the nearest available SLIC field assessor to the "
            "accident site. Call this ONLY AFTER collect_accident_details "
            "has succeeded. Returns an ETA in minutes, a claim reference "
            "number, the assessor's name, and the assessor's phone "
            "number. Tell the caller ONLY the assessor's name and ETA — "
            "do NOT read the claim reference or the assessor's phone "
            "number aloud; both are delivered by SMS in the next step."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "reg_no": {
                    "type": "string",
                    "description": "The verified vehicle registration number.",
                },
            },
            "required": ["reg_no"],
        },
    },
    {
        "name": "send_confirmation_sms",
        "description": (
            "Send the caller an SMS with the claim reference, assessor "
            "name, assessor phone number, and ETA. Call this ONLY AFTER "
            "dispatch_nearest_assessor has returned successfully. The "
            "caller's phone number is injected automatically."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "claim_reference": {
                    "type": "string",
                    "description": (
                        "The claim reference returned by dispatch_nearest_assessor "
                        "(e.g. 'SLIC-CLM-20260422-AB12CD')."
                    ),
                },
                "eta_minutes": {
                    "type": "integer",
                    "description": (
                        "The assessor ETA in minutes returned by "
                        "dispatch_nearest_assessor."
                    ),
                },
                "assessor_name": {
                    "type": "string",
                    "description": (
                        "The assessor's name returned by "
                        "dispatch_nearest_assessor."
                    ),
                },
                "assessor_phone": {
                    "type": "string",
                    "description": (
                        "The assessor's phone number returned by "
                        "dispatch_nearest_assessor (e.g. '+94771234501')."
                    ),
                },
            },
            "required": ["claim_reference", "eta_minutes"],
        },
    },
    {
        "name": "request_live_agent_handoff",
        "description": (
            "Request transfer to a human operator. Call this when "
            "verify_vehicle_policy returns verified=false, or when the "
            "caller explicitly asks to speak to a human, or when you cannot "
            "proceed for any other reason. After this tool returns, speak "
            "its spoken confirmation verbatim to the caller and then stop — "
            "do not call any further tools and do not ask any more "
            "questions. Wait quietly; the caller may hang up or continue "
            "talking on their own."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": (
                        "Short machine-readable reason, e.g. "
                        "'vehicle_not_on_policy', 'caller_requested_human', "
                        "'could_not_collect_details'."
                    ),
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
    """Anthropic native tool format."""
    if is_configured():
        return TOOL_DEFINITIONS
    logger.warning("Claim API not configured — no tools available.")
    return []


def get_tools_openai() -> list[dict[str, Any]]:
    if not is_configured():
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
    if not is_configured():
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


async def execute_tool(
    tool_name: str,
    tool_input: dict[str, Any],
    *,
    caller_phone: str = "",
) -> str:
    """Dispatch a tool call. Caller phone is always supplied by the server."""
    logger.info(
        "Executing tool '%s' caller=%s input=%s",
        tool_name, caller_phone, tool_input,
    )

    if tool_name == "verify_vehicle_policy":
        result = await verify_vehicle_policy(
            reg_no=tool_input.get("reg_no", ""),
        )

    elif tool_name == "collect_accident_details":
        result = await collect_accident_details(
            reg_no=tool_input.get("reg_no", ""),
            location=tool_input.get("location", ""),
            injuries=tool_input.get("injuries", ""),
            vehicle_condition=tool_input.get("vehicle_condition", ""),
        )

    elif tool_name == "dispatch_nearest_assessor":
        result = await dispatch_nearest_assessor(
            caller_phone=caller_phone,
            reg_no=tool_input.get("reg_no", ""),
        )

    elif tool_name == "send_confirmation_sms":
        result = await send_confirmation_sms(
            caller_phone=caller_phone,
            claim_reference=tool_input.get("claim_reference", ""),
            eta_minutes=int(tool_input.get("eta_minutes", 0)),
            assessor_name=tool_input.get("assessor_name", ""),
            assessor_phone=tool_input.get("assessor_phone", ""),
        )

    elif tool_name == "request_live_agent_handoff":
        result = await request_live_agent_handoff(
            caller_phone=caller_phone,
            reason=tool_input.get("reason", "unspecified"),
        )

    else:
        logger.error("Unknown tool requested: %s", tool_name)
        return json.dumps({"error": f"Unknown tool: {tool_name}"})

    logger.info("Tool '%s' returned: %s", tool_name, result)
    return json.dumps(result)
