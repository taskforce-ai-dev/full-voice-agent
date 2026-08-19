"""
tools.py -- Tool definitions for the Hutch voice agent (Selina).

Hutch/Selina is a pure KB/inquiries agent: she answers plan and service
questions from the knowledge base and has no booking, PMS, or live call
transfer of any kind. The ONLY tool is `notify_human_handover`, used when a
caller's question is genuinely outside the knowledge base -- it collects the
caller's name and a callback/WhatsApp number and hands off to `handover.py`,
which POSTs to n8n so a Hutch operator can follow up later.

Unlike Kavya (which restricts its handover tool to a post-failed-transfer
recovery session because a normal call always has a live transfer to
attempt), Hutch/Selina has no live transfer at all, so this tool is a normal,
always-available tool alongside knowledge-base answering -- offered whenever
the system prompt determines a question is out of scope.
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tool definition (Anthropic / Claude native format)
# ---------------------------------------------------------------------------

HANDOVER_TOOL_DEFINITION: dict[str, Any] = {
    "name": "notify_human_handover",
    "description": (
        "Send the caller's callback details to a Hutch operator on WhatsApp "
        "for follow-up. Use this ONLY when the caller's question genuinely "
        "cannot be answered from the Hutch knowledge base (e.g. account-specific "
        "billing disputes, technical faults, enterprise contract negotiation, or "
        "anything requiring access to the caller's actual account). Call this "
        "ONCE per call, only after you have the caller's name AND a callback "
        "or WhatsApp number. After this returns successfully, tell the caller "
        "a Hutch team member will follow up with them, and mention they can "
        "also call the Hutch Hotline on 1788 in the meantime."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "customer_name": {
                "type": "string",
                "description": "The caller's name as they gave it (first name is enough).",
            },
            "customer_whatsapp": {
                "type": "string",
                "description": (
                    "The caller's WhatsApp / callback number, digits only as the "
                    "caller said them (e.g. '0771234567' or '94771234567'). Do not "
                    "invent a number -- confirm it with the caller first."
                ),
            },
            "call_summary": {
                "type": "string",
                "description": (
                    "Two or three sentences for the Hutch operator: what the "
                    "caller wanted, and exactly why it could not be answered "
                    "from the knowledge base."
                ),
            },
        },
        "required": ["customer_name", "customer_whatsapp", "call_summary"],
    },
}

TOOL_DEFINITIONS: list[dict[str, Any]] = [HANDOVER_TOOL_DEFINITION]


# ---------------------------------------------------------------------------
# Public helpers -- provider-format conversion
# ---------------------------------------------------------------------------

def get_tools() -> list[dict[str, Any]]:
    """Return tool definitions in Anthropic (Claude) native format."""
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
    """Return tool definitions in Google Gemini native format."""
    return [
        {
            "function_declarations": [
                {
                    "name": tool["name"],
                    "description": tool["description"],
                    "parameters": tool["input_schema"],
                }
                for tool in TOOL_DEFINITIONS
            ],
        }
    ]


def get_tools_for(fmt: str) -> list[dict[str, Any]]:
    """Return tool definitions in the given provider format ('claude'|'openai'|'gemini')."""
    if fmt == "openai":
        return get_tools_openai()
    if fmt == "gemini":
        return get_tools_gemini()
    return get_tools()


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

async def execute_tool(tool_name: str, tool_input: dict[str, Any]) -> str:
    """Execute a tool call by name and return a JSON string result.

    Only `notify_human_handover` exists. Any other tool name is a
    programming error (a provider hallucinated a tool), so it returns a
    JSON error payload rather than raising -- a bad tool call must never
    crash the call.
    """
    if tool_name != "notify_human_handover":
        logger.warning("execute_tool called with unknown tool: %s", tool_name)
        return json.dumps({"status": "error", "error": f"Unknown tool: {tool_name}"})

    from handover import _get_handover_context, send_handover_notification

    ctx = _get_handover_context()
    outcome = await send_handover_notification(
        call_sid=ctx.get("call_sid", ""),
        customer_name=tool_input.get("customer_name", ""),
        customer_whatsapp=tool_input.get("customer_whatsapp", ""),
        call_summary=tool_input.get("call_summary", ""),
        human_agent_whatsapp=ctx.get("human_agent_whatsapp", ""),
    )

    if outcome.get("ok"):
        return json.dumps({"status": "notified"})
    # Fail open: the caller experience must not change just because the
    # notification failed (e.g. the n8n workflow is not built yet).
    logger.warning("notify_human_handover did not succeed: %s", outcome)
    return json.dumps({"status": "notified"})
