"""
post_call.py -- Post-call data extraction and n8n webhook integration for BSL.

After a call to the Bank of Sri Lanka voice agent ends:
  1. Uses an LLM (Claude / OpenAI / Gemini) to extract structured banking
     interaction details from the full transcript.
  2. POSTs the structured data + transcript to an n8n webhook.
  3. n8n forwards the data downstream (Sheets / CRM / etc.).

Called from server.py via asyncio.create_task() in the WebSocket finally block.
ALL errors are caught and logged -- never raised to the caller. The call is
already over by the time this runs; a webhook 404 or LLM failure must never
crash anything.

PII WARNING: BSL transcripts may contain NIC numbers, account numbers, DOB,
and other sensitive data. The raw transcript is logged at DEBUG level only.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
N8N_BASE_URL: str = os.getenv(
    "N8N_BASE_URL", "https://automation.taskforceai.tech"
)
N8N_POSTCALL_WEBHOOK: str = os.getenv(
    "N8N_POSTCALL_WEBHOOK", "/webhook/bsl-transcript"
)

EXTRACTION_MAX_TOKENS: int = 800
N8N_POST_TIMEOUT_SECONDS: float = 15.0

# ---------------------------------------------------------------------------
# Extraction prompt
# ---------------------------------------------------------------------------
EXTRACTION_SYSTEM_PROMPT: str = """You extract structured data from a Bank of Sri Lanka voice-agent call transcript.

Return a JSON object with EXACTLY these keys:
{
  "caller_phone": str (or null) — the caller's phone number if mentioned,
  "account_no_last4": str (or null) — last 4 digits of the account discussed,
  "account_holder": str (or null) — name as best identified,
  "company_name": str (or null) — only if business account,
  "account_type_discussed": "Personal/Current" | "Business" | null,
  "intent": one of: "balance" | "block_card" | "account_details" | "transactions" | "loans" | "standing_orders" | "multiple" | "other" | null,
  "verified": bool — did the agent successfully verify identity in this call?,
  "verification_attempts": int (0-3) — how many verification attempts the caller made,
  "actions_taken": list[str] — e.g. ["served_balance", "blocked_card", "served_account_details"],
  "outcome": one of: "served" | "verification_failed" | "handed_off_to_live_agent" | "caller_hung_up" | "incomplete",
  "follow_up_needed": bool,
  "summary": str — 1-2 English sentences describing what happened on the call
}

Rules:
- All field values must be in English regardless of the transcript language.
- If a field was genuinely not discussed, use null (or false / 0 / [] as appropriate for the type).
- For account_no_last4, return ONLY the last 4 digits as a string (e.g. "1234"). Never return the full account number.
- For verification_attempts, count the number of distinct identity-verification attempts the caller made (0 if none).
- For actions_taken, include only actions the agent actually completed in this call.
- ALWAYS provide a summary, even for short or aborted calls.

Return ONLY valid JSON, no surrounding markdown, no commentary, no code fences."""

EXTRACTION_USER_TEMPLATE: str = "Transcript:\n\n{transcript}"

RETRY_PROMPT: str = """Extract these fields from the Bank of Sri Lanka voice-agent call transcript as a short JSON object. Use null / false / 0 / [] for missing fields.
Fields: caller_phone, account_no_last4 (last 4 digits string), account_holder, company_name, account_type_discussed (Personal/Current | Business | null), intent (balance | block_card | account_details | transactions | loans | standing_orders | multiple | other | null), verified (true/false), verification_attempts (int 0-3), actions_taken (list of strings), outcome (served | verification_failed | handed_off_to_live_agent | caller_hung_up | incomplete), follow_up_needed (true/false), summary (1 sentence English).
Respond with only valid JSON, no markdown."""


# ---------------------------------------------------------------------------
# JSON cleaning helper
# ---------------------------------------------------------------------------

def _clean_json_response(text: str) -> str:
    """Strip markdown code fences and whitespace from LLM JSON output."""
    text = text.strip()
    if text.startswith("```"):
        first_newline = text.index("\n") if "\n" in text else 3
        text = text[first_newline + 1:]
    if text.endswith("```"):
        text = text[:-3]
    return text.strip()


def _empty_extraction() -> dict[str, Any]:
    """Default skeleton — every key set to a sensible empty value."""
    return {
        "caller_phone": None,
        "account_no_last4": None,
        "account_holder": None,
        "company_name": None,
        "account_type_discussed": None,
        "intent": None,
        "verified": False,
        "verification_attempts": 0,
        "actions_taken": [],
        "outcome": "incomplete",
        "follow_up_needed": False,
        "summary": "Extraction failed — see transcript for details.",
    }


# ---------------------------------------------------------------------------
# LLM extraction -- Claude / OpenAI / Gemini
# ---------------------------------------------------------------------------

async def _extract_with_claude(
    client: Any,
    system: str,
    transcript: str,
    model: str,
) -> dict[str, Any]:
    """Single-shot extraction via Anthropic Claude (no streaming)."""
    response = await client.messages.create(
        model=model,
        max_tokens=EXTRACTION_MAX_TOKENS,
        system=system,
        messages=[
            {"role": "user", "content": EXTRACTION_USER_TEMPLATE.format(transcript=transcript)},
        ],
    )
    text = _clean_json_response(response.content[0].text)
    return json.loads(text)


async def _extract_with_openai(
    client: Any,
    system: str,
    transcript: str,
    model: str,
) -> dict[str, Any]:
    """Single-shot extraction via OpenAI with JSON response format."""
    response = await client.chat.completions.create(
        model=model,
        max_tokens=EXTRACTION_MAX_TOKENS,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": EXTRACTION_USER_TEMPLATE.format(transcript=transcript)},
        ],
    )
    text = _clean_json_response(response.choices[0].message.content)
    return json.loads(text)


async def _extract_with_gemini(
    client: Any,
    system: str,
    transcript: str,
    model: str,
) -> dict[str, Any]:
    """Single-shot extraction via Google Gemini (sync SDK wrapped in to_thread)."""
    from google.genai import types as genai_types

    def _sync_call() -> Any:
        return client.models.generate_content(
            model=model,
            contents=EXTRACTION_USER_TEMPLATE.format(transcript=transcript),
            config=genai_types.GenerateContentConfig(
                system_instruction=system,
                max_output_tokens=EXTRACTION_MAX_TOKENS,
                response_mime_type="application/json",
            ),
        )

    response = await asyncio.to_thread(_sync_call)
    text = _clean_json_response(response.text)
    return json.loads(text)


async def _dispatch_extraction(
    system: str,
    transcript: str,
    llm_provider: str,
    anthropic_client: Any | None,
    openai_client: Any | None,
    gemini_client: Any | None,
    model: str,
) -> dict[str, Any]:
    """Route to the right provider. May raise — caller wraps."""
    if llm_provider == "claude" and anthropic_client:
        return await _extract_with_claude(
            anthropic_client, system, transcript,
            model or "claude-sonnet-4-20250514",
        )
    if llm_provider == "openai" and openai_client:
        return await _extract_with_openai(
            openai_client, system, transcript,
            model or "gpt-4o",
        )
    if llm_provider == "gemini" and gemini_client:
        return await _extract_with_gemini(
            gemini_client, system, transcript,
            model or "gemini-2.5-flash",
        )
    raise RuntimeError(f"No LLM client available for provider={llm_provider!r}")


async def extract_call_details(
    transcript: str,
    llm_provider: str,
    anthropic_client: Any | None = None,
    openai_client: Any | None = None,
    gemini_client: Any | None = None,
    model: str = "",
) -> dict[str, Any]:
    """Extract structured call details. One retry on JSON parse failure.

    Always returns a dict. On hard failure, returns a dict with
    ``_extraction_error`` set and ``summary`` = "Extraction failed".
    """
    empty = _empty_extraction()

    try:
        result = await _dispatch_extraction(
            EXTRACTION_SYSTEM_PROMPT, transcript, llm_provider,
            anthropic_client, openai_client, gemini_client, model,
        )
        for key, default in empty.items():
            if key not in result:
                result[key] = default
        return result

    except json.JSONDecodeError as exc:
        logger.warning("LLM extraction returned invalid JSON, retrying once: %s", exc)
        try:
            result = await _dispatch_extraction(
                RETRY_PROMPT, transcript, llm_provider,
                anthropic_client, openai_client, gemini_client, model,
            )
            for key, default in empty.items():
                if key not in result:
                    result[key] = default
            return result
        except Exception as retry_exc:
            logger.error("Retry extraction also failed: %s", retry_exc)
            return {"_extraction_error": str(retry_exc), "summary": "Extraction failed"}

    except Exception as exc:
        logger.exception("LLM extraction failed: %s", exc)
        return {"_extraction_error": str(exc), "summary": "Extraction failed"}


# ---------------------------------------------------------------------------
# n8n webhook POST
# ---------------------------------------------------------------------------

async def _post_to_n8n(payload: dict[str, Any]) -> None:
    """POST the call data payload to the n8n webhook. Fire-and-forget."""
    from bsl_api import get_session

    url = f"{N8N_BASE_URL}{N8N_POSTCALL_WEBHOOK}"
    timeout = aiohttp.ClientTimeout(total=N8N_POST_TIMEOUT_SECONDS)

    try:
        session = await get_session()
        async with session.post(url, json=payload, timeout=timeout) as resp:
            body = await resp.text()
            if resp.status < 300:
                logger.info(
                    "Post-call data sent to n8n — status %d, body: %s",
                    resp.status, body[:200],
                )
            else:
                logger.error(
                    "n8n post-call webhook returned %d — body: %s",
                    resp.status, body[:500],
                )
    except asyncio.TimeoutError as exc:
        logger.error(
            "Timeout POSTing post-call data to n8n (%.1fs): %s",
            N8N_POST_TIMEOUT_SECONDS, exc,
        )
    except aiohttp.ClientError as exc:
        logger.exception("aiohttp error POSTing post-call data to n8n: %s", exc)
    except Exception as exc:
        logger.exception("Failed to POST post-call data to n8n: %s", exc)


# ---------------------------------------------------------------------------
# Orchestrator — entry point called from server.py
# ---------------------------------------------------------------------------

async def process_post_call_data(
    call_sid: str,
    caller_phone: str,
    full_transcript: str,
    call_start_time: datetime,
    call_end_time: datetime,
    llm_provider: str,
    anthropic_client: Any | None,
    openai_client: Any | None,
    gemini_client: Any | None,
    model: str,
) -> None:
    """Extract + POST. Catches every exception. Returns None always.

    Runs as a background task (asyncio.create_task) from server.py's
    WebSocket finally block. The call is already over by the time this
    runs — webhook 404s, LLM failures, timeouts, etc. must never crash.
    """
    try:
        logger.info("Starting post-call processing for CallSid: %s", call_sid)
        # PII: transcript may contain NIC, DOB, account numbers — DEBUG only.
        logger.debug("Full transcript for %s:\n%s", call_sid, full_transcript)

        # ---- Extract ----
        logger.info(
            "Extracting call details for %s via %s (model=%s)",
            call_sid, llm_provider, model or "<default>",
        )
        extracted = await extract_call_details(
            transcript=full_transcript,
            llm_provider=llm_provider,
            anthropic_client=anthropic_client,
            openai_client=openai_client,
            gemini_client=gemini_client,
            model=model,
        )

        extraction_error = extracted.pop("_extraction_error", None)
        if extraction_error:
            logger.warning(
                "Extraction had issues for %s: %s — sending partial data",
                call_sid, extraction_error,
            )

        populated = sum(
            1 for v in extracted.values() if v not in (None, "", [], False, 0)
        )
        logger.info(
            "Extraction complete for %s — %d non-empty fields, intent=%s, outcome=%s",
            call_sid, populated,
            extracted.get("intent"), extracted.get("outcome"),
        )

        # ---- Build payload ----
        try:
            duration_seconds = max(
                0, int((call_end_time - call_start_time).total_seconds())
            )
        except Exception:
            duration_seconds = 0

        try:
            call_end_iso = call_end_time.isoformat()
        except Exception:
            call_end_iso = str(call_end_time)

        try:
            timestamp_iso = call_start_time.isoformat()
        except Exception:
            timestamp_iso = str(call_start_time)

        payload: dict[str, Any] = {
            **extracted,
            "timestamp": timestamp_iso,
            "call_end_time": call_end_iso,
            "call_sid": call_sid,
            "caller_phone": caller_phone,
            "transcript": full_transcript,
            "call_duration_seconds": duration_seconds,
        }

        # ---- POST ----
        await _post_to_n8n(payload)

        logger.info(
            "Post-call processing complete for %s — outcome=%s, follow_up=%s",
            call_sid,
            extracted.get("outcome"),
            extracted.get("follow_up_needed"),
        )

    except Exception:
        # Last-resort guard — every nested layer also catches, but this
        # ensures we NEVER propagate. The call is already over.
        logger.exception("Post-call processing failed for CallSid: %s", call_sid)
