"""BSL tool definitions (Anthropic / OpenAI / Gemini formats).

Exports:
    get_tools()          → Anthropic native format
    get_tools_openai()   → OpenAI function-calling format
    get_tools_gemini()   → Gemini native SDK format
    execute_tool(...)    → str (JSON-encoded result)
"""

import json
import logging
from typing import Any

from bsl_api import (
    verify_customer_identity,
    get_account_balance,
    block_debit_card,
    get_account_details,
    get_recent_transactions,
    get_loans,
    get_standing_orders,
    request_live_agent_handoff,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Canonical tool definitions (Anthropic input_schema format)
# ---------------------------------------------------------------------------

TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "verify_customer_identity",
        "description": (
            "Verify the caller's identity against the account record. "
            "Call this ONLY after you have collected all required security fields for this call. "
            "Do NOT call this tool one field at a time — collect all required fields, then call once. "
            "For fields not asked this call, pass an empty string. "
            "Returns verified=true/false and attempts_remaining."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "account_no": {
                    "type": "string",
                    "description": "The customer's account number.",
                },
                "nic": {
                    "type": "string",
                    "description": "National Identity Card number.",
                },
                "dob": {
                    "type": "string",
                    "description": "Date of birth, e.g. '12 April 1990'.",
                },
                "mothers_maiden_name": {
                    "type": "string",
                    "description": "Mother's maiden name.",
                },
            },
            "required": ["account_no", "nic", "dob", "mothers_maiden_name"],
        },
    },
    {
        "name": "get_account_balance",
        "description": (
            "Get the account balance and card status for the given account number. "
            "Call this ONLY after identity has been successfully verified "
            "(verify_customer_identity returned verified=true) AND the caller's intent "
            "is to check their balance."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "account_no": {
                    "type": "string",
                    "description": "The customer's account number.",
                },
            },
            "required": ["account_no"],
        },
    },
    {
        "name": "block_debit_card",
        "description": (
            "Place a block on the debit card associated with this account. "
            "Call this ONLY after identity has been successfully verified AND the "
            "caller's intent is to block their card. This action is session-local and "
            "cannot be undone in this call."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "account_no": {
                    "type": "string",
                    "description": "The customer's account number.",
                },
            },
            "required": ["account_no"],
        },
    },
    {
        "name": "get_account_details",
        "description": (
            "Retrieve account details including account type, branch, opening date, "
            "and channel enrollments. Call this ONLY after identity has been successfully "
            "verified AND the caller's intent is to get account details."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "account_no": {
                    "type": "string",
                    "description": "The customer's account number.",
                },
            },
            "required": ["account_no"],
        },
    },
    {
        "name": "get_recent_transactions",
        "description": (
            "Retrieve the most recent transactions for this account. "
            "Call this ONLY after identity has been successfully verified AND the caller "
            "wants to hear recent transactions. Returns up to 10 recent transactions."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "account_no": {
                    "type": "string",
                    "description": "The customer's account number.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of transactions to return (default 10).",
                },
            },
            "required": ["account_no"],
        },
    },
    {
        "name": "get_loans",
        "description": (
            "Retrieve loan information for this account. "
            "Call this ONLY after identity has been successfully verified AND the caller "
            "wants loan details."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "account_no": {
                    "type": "string",
                    "description": "The customer's account number.",
                },
            },
            "required": ["account_no"],
        },
    },
    {
        "name": "get_standing_orders",
        "description": (
            "Retrieve standing orders (recurring payment instructions) for this account. "
            "Call this ONLY after identity has been successfully verified AND the caller "
            "wants standing order information."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "account_no": {
                    "type": "string",
                    "description": "The customer's account number.",
                },
            },
            "required": ["account_no"],
        },
    },
    {
        "name": "request_live_agent_handoff",
        "description": (
            "Transfer the caller to a live human agent. "
            "Call this when: (1) identity verification failed 3 times, OR (2) the caller "
            "explicitly asks to speak to a human. After this returns, speak its message "
            "verbatim then stop — do not ask further questions. Wait quietly; the caller "
            "may hang up."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": (
                        "Reason for handoff, e.g. 'verification_failed' or "
                        "'caller_requested_human'."
                    ),
                },
            },
            "required": ["reason"],
        },
    },
]

# ---------------------------------------------------------------------------
# Format adapters
# ---------------------------------------------------------------------------


def get_tools() -> list[dict[str, Any]]:
    """Return tool definitions in Anthropic native format."""
    return TOOL_DEFINITIONS


def get_tools_openai() -> list[dict[str, Any]]:
    """Return tool definitions in OpenAI function-calling format."""
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
    """Return tool definitions in Gemini native SDK format."""
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


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


async def execute_tool(
    tool_name: str,
    tool_input: dict[str, Any],
    *,
    caller_phone: str = "",
    call_sid: str = "",
) -> str:
    """Dispatch a tool call to the appropriate bsl_api function.

    PII fields (NIC, DOB, mother's maiden name) are intentionally excluded
    from log output.
    """
    try:
        if tool_name == "verify_customer_identity":
            logger.info(
                "execute_tool: %s | call_sid=%s | account_no=%s",
                tool_name, call_sid, tool_input.get("account_no"),
            )
            result = await verify_customer_identity(
                call_sid,
                tool_input["account_no"],
                tool_input["nic"],
                tool_input["dob"],
                tool_input["mothers_maiden_name"],
            )

        elif tool_name == "get_account_balance":
            logger.info(
                "execute_tool: %s | call_sid=%s | account_no=%s",
                tool_name, call_sid, tool_input.get("account_no"),
            )
            result = await get_account_balance(call_sid, tool_input["account_no"])

        elif tool_name == "block_debit_card":
            logger.info(
                "execute_tool: %s | call_sid=%s | account_no=%s",
                tool_name, call_sid, tool_input.get("account_no"),
            )
            result = await block_debit_card(call_sid, tool_input["account_no"])

        elif tool_name == "get_account_details":
            logger.info(
                "execute_tool: %s | call_sid=%s | account_no=%s",
                tool_name, call_sid, tool_input.get("account_no"),
            )
            result = await get_account_details(call_sid, tool_input["account_no"])

        elif tool_name == "get_recent_transactions":
            limit = int(tool_input.get("limit", 10))
            logger.info(
                "execute_tool: %s | call_sid=%s | account_no=%s | limit=%d",
                tool_name, call_sid, tool_input.get("account_no"), limit,
            )
            result = await get_recent_transactions(
                call_sid, tool_input["account_no"], limit
            )

        elif tool_name == "get_loans":
            logger.info(
                "execute_tool: %s | call_sid=%s | account_no=%s",
                tool_name, call_sid, tool_input.get("account_no"),
            )
            result = await get_loans(call_sid, tool_input["account_no"])

        elif tool_name == "get_standing_orders":
            logger.info(
                "execute_tool: %s | call_sid=%s | account_no=%s",
                tool_name, call_sid, tool_input.get("account_no"),
            )
            result = await get_standing_orders(call_sid, tool_input["account_no"])

        elif tool_name == "request_live_agent_handoff":
            reason = tool_input.get("reason", "unspecified")
            logger.info(
                "execute_tool: %s | call_sid=%s | caller_phone=%s | reason=%s",
                tool_name, call_sid, caller_phone, reason,
            )
            result = await request_live_agent_handoff(caller_phone, reason)

        else:
            logger.warning(
                "execute_tool: unknown tool '%s' | call_sid=%s", tool_name, call_sid
            )
            return json.dumps({"error": f"Unknown tool: {tool_name}"})

    except Exception as exc:
        logger.exception(
            "execute_tool: error in '%s' | call_sid=%s | error=%s",
            tool_name, call_sid, exc,
        )
        return json.dumps({"error": str(exc)})

    return json.dumps(result)
