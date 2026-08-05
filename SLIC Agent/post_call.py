"""
post_call.py -- Post-call data extraction and n8n webhook integration.

After a call ends:
  1. Uses LLM to extract structured accident-claim details from the full transcript.
  2. POSTs structured data + transcript to an n8n webhook.
  3. n8n appends a row to a Google Sheet for SLIC claim operations.

Google Sheet columns (for reference — populated by n8n workflow):
  Date/Time, Call SID, Caller Phone, Caller Name, Policy No,
  Vehicle Make, Vehicle Model, Reg No, Accident Description,
  Assessor ETA (min), Claim Reference, Dispatch Status, Outcome,
  Follow-Up Needed, Summary, Transcript.

Called from server.py via asyncio.create_task() in the WebSocket finally blocks.
All errors are caught and logged -- never raised to the caller.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
N8N_BASE_URL: str = os.getenv(
    "N8N_BASE_URL", "https://automation.taskforceai.tech"
)
N8N_POSTCALL_WEBHOOK: str = os.getenv(
    "N8N_POSTCALL_WEBHOOK", "/webhook/slic-transcript"
)

EXTRACTION_MAX_TOKENS: int = 2000

# ---------------------------------------------------------------------------
# Extraction prompt
# ---------------------------------------------------------------------------
EXTRACTION_SYSTEM_PROMPT: str = """You are a data extraction assistant for Sri Lanka Insurance Corporation (SLIC). Analyze a phone call transcript between SLIC's motor accident hotline agent (Nimali) and a caller who is reporting a vehicle accident. The call may be in English, Sinhala, or Tamil.

Extract ALL available information and return ONLY a valid JSON object. No markdown code fences, no explanation — just the raw JSON.

{
  "caller_name": "caller's name as confirmed on the call, or null",
  "policy_no": "SLIC policy number e.g. 'SLIC-2024-00123', or null",
  "vehicle_make": "make of the vehicle the claim is against e.g. 'Toyota', or null",
  "vehicle_model": "model of the vehicle e.g. 'Corolla', or null",
  "vehicle_reg_no": "vehicle registration number e.g. 'CAB-1234', or null",
  "location": "where the accident happened (road/area/city), or null",
  "injuries": "any injuries reported; 'None' if no one was hurt; null if not discussed",
  "vehicle_condition": "state of the vehicle after the accident, or null",
  "accident_description": "one-line summary combining location/injuries/condition, or null",
  "assessor_name": "name of the assessor assigned, or null if no dispatch",
  "assessor_eta_minutes": "integer ETA in minutes quoted to the caller, or null if no dispatch happened",
  "claim_reference": "claim reference number generated e.g. 'SLIC-CLM-20261002-AB12CD', or null if no dispatch",
  "sms_sent": true or false,
  "dispatch_status": "see rules below",
  "outcome": "short one-line description of how the call ended",
  "follow_up_needed": true or false,
  "summary": "1-2 sentence English summary of the entire call"
}

CRITICAL RULES FOR dispatch_status — pick EXACTLY ONE:
- "dispatched" = an assessor was dispatched to the accident scene during the call (ETA was quoted AND a claim reference was generated)
- "follow_up_call" = the caller is checking back on an already-active session / existing claim (not a new dispatch)
- "handed_off_to_live_agent" = the AI transferred the caller to a human operator (e.g. vehicle not on any SLIC policy, caller asked for a human)
- "not_dispatched" = the call ended before dispatch happened (caller hung up, incomplete info, call dropped, etc.)

RULES FOR sms_sent:
- true if the agent called the send_confirmation_sms tool and it succeeded. false otherwise.

RULES FOR assessor_eta_minutes:
- Must be an integer (number of minutes). If the agent said "30 minutes", use 30.
- Use null if no ETA was quoted / no dispatch happened.

RULES FOR claim_reference:
- Copy the exact reference string the agent generated (format: SLIC-CLM-YYYYMMDD-XXXXXX).
- Use null if no claim reference was generated during the call.

RULES FOR accident_description:
- One concise line describing the incident. Example: "Rear-ended at traffic light on Galle Road, minor bumper damage."
- Use null if the caller did not describe an accident at all.

RULES FOR summary:
- ALWAYS provide a summary, even for short calls.
- Summarize what the caller reported and what happened on the call. Example: "Caller reported a collision in Colombo involving a Toyota Corolla. An assessor was dispatched with a 25-minute ETA."
- Write in English regardless of transcript language.

RULES FOR follow_up_needed:
- true if: dispatch did not happen and the caller still needs assistance, details were missing, caller asked to be called back, or the call dropped before resolution.
- false if: dispatch was completed successfully, OR the caller's existing active session was confirmed and no further action is pending.

RULES FOR outcome:
- Short one-line description. Examples: "Assessor dispatched successfully.", "Call dropped before policy verification.", "Follow-up on active session — assessor already en route."

All field values must be in English regardless of the transcript language.
If a field was genuinely not discussed at all, use null (or false for follow_up_needed).
Return ONLY the JSON object."""

EXTRACTION_USER_TEMPLATE: str = "Transcript:\n\n{transcript}"


def _clean_json_response(text: str) -> str:
    """Strip markdown code fences and whitespace from LLM JSON output."""
    text = text.strip()
    # Remove ```json ... ``` or ``` ... ```
    if text.startswith("```"):
        # Remove opening fence (with optional language tag)
        first_newline = text.index("\n") if "\n" in text else 3
        text = text[first_newline + 1:]
    if text.endswith("```"):
        text = text[:-3]
    return text.strip()


# ---------------------------------------------------------------------------
# LLM extraction -- supports Claude, OpenAI, Gemini
# ---------------------------------------------------------------------------

async def _extract_with_claude(
    client: Any,
    transcript_text: str,
    model: str,
) -> dict[str, Any]:
    """Extract claim details using Anthropic Claude."""
    response = await client.messages.create(
        model=model,
        max_tokens=EXTRACTION_MAX_TOKENS,
        system=EXTRACTION_SYSTEM_PROMPT,
        messages=[
            {"role": "user", "content": EXTRACTION_USER_TEMPLATE.format(transcript=transcript_text)},
        ],
    )
    text = _clean_json_response(response.content[0].text)
    return json.loads(text)


async def _extract_with_openai(
    client: Any,
    transcript_text: str,
    model: str,
) -> dict[str, Any]:
    """Extract claim details using OpenAI."""
    response = await client.chat.completions.create(
        model=model,
        max_tokens=EXTRACTION_MAX_TOKENS,
        messages=[
            {"role": "system", "content": EXTRACTION_SYSTEM_PROMPT},
            {"role": "user", "content": EXTRACTION_USER_TEMPLATE.format(transcript=transcript_text)},
        ],
    )
    text = _clean_json_response(response.choices[0].message.content)
    return json.loads(text)


async def _extract_with_gemini(
    client: Any,
    transcript_text: str,
    model: str,
) -> dict[str, Any]:
    """Extract claim details using Google Gemini native SDK."""
    from google.genai import types as genai_types

    response = await client.aio.models.generate_content(
        model=model,
        contents=EXTRACTION_USER_TEMPLATE.format(transcript=transcript_text),
        config=genai_types.GenerateContentConfig(
            system_instruction=EXTRACTION_SYSTEM_PROMPT,
            max_output_tokens=EXTRACTION_MAX_TOKENS,
            response_mime_type="application/json",
        ),
    )
    text = _clean_json_response(response.text)
    return json.loads(text)


async def extract_claim_details(
    transcript_text: str,
    lang: str,
    llm_provider: str,
    anthropic_client: Any | None = None,
    openai_client: Any | None = None,
    gemini_client: Any | None = None,
    model: str = "",
) -> dict[str, Any]:
    """Extract structured accident-claim details from a call transcript via LLM.

    Returns a dict with the extracted fields. On failure, returns a dict
    with all fields set to sensible defaults and an ``_extraction_error`` key.
    """
    empty: dict[str, Any] = {
        "caller_name": None,
        "policy_no": None,
        "vehicle_make": None,
        "vehicle_model": None,
        "vehicle_reg_no": None,
        "location": None,
        "injuries": None,
        "vehicle_condition": None,
        "accident_description": None,
        "assessor_name": None,
        "assessor_eta_minutes": None,
        "claim_reference": None,
        "sms_sent": False,
        "dispatch_status": "not_dispatched",
        "outcome": "Extraction failed — see transcript for details.",
        "follow_up_needed": False,
        "summary": "Extraction failed — see transcript for details.",
    }

    try:
        if llm_provider == "claude" and anthropic_client:
            result = await _extract_with_claude(
                anthropic_client, transcript_text, model or "claude-sonnet-4-20250514",
            )
        elif llm_provider == "openai" and openai_client:
            result = await _extract_with_openai(
                openai_client, transcript_text, model or "gpt-4o",
            )
        elif llm_provider == "gemini" and gemini_client:
            result = await _extract_with_gemini(
                gemini_client, transcript_text, model or "gemini-2.5-flash",
            )
        else:
            logger.warning("No LLM client available for extraction (provider=%s)", llm_provider)
            empty["_extraction_error"] = "no_client"
            return empty

        # Merge with empty to ensure all keys exist
        for key in empty:
            if key not in result:
                result[key] = empty[key]
        return result

    except json.JSONDecodeError as exc:
        logger.warning("Failed to parse LLM extraction JSON (attempt 1): %s", exc)
        # Retry once with a simpler prompt
        try:
            logger.info("Retrying extraction with simplified prompt...")
            retry_result = await _retry_extraction(
                transcript_text, llm_provider,
                anthropic_client, openai_client, gemini_client, model,
            )
            if retry_result:
                for key in empty:
                    if key not in retry_result:
                        retry_result[key] = empty[key]
                return retry_result
        except Exception as retry_exc:
            logger.error("Retry extraction also failed: %s", retry_exc)
        empty["_extraction_error"] = f"json_parse: {exc}"
        return empty
    except Exception as exc:
        logger.exception("LLM extraction failed: %s", exc)
        empty["_extraction_error"] = str(exc)
        return empty


RETRY_PROMPT: str = """Extract these fields from the SLIC motor accident hotline call transcript as a short JSON object. Use null for missing fields (false for follow_up_needed and sms_sent).
Fields: caller_name, policy_no, vehicle_make, vehicle_model, vehicle_reg_no, location, injuries, vehicle_condition, accident_description, assessor_name, assessor_eta_minutes (integer), claim_reference, sms_sent (true/false), dispatch_status (dispatched/follow_up_call/handed_off_to_live_agent/not_dispatched), outcome, follow_up_needed (true/false), summary (1 sentence English).
Return ONLY valid JSON, no markdown."""


async def _retry_extraction(
    transcript_text: str,
    llm_provider: str,
    anthropic_client: Any | None,
    openai_client: Any | None,
    gemini_client: Any | None,
    model: str,
) -> dict[str, Any] | None:
    """Retry extraction with a shorter prompt if the first attempt failed."""
    prompt = f"{RETRY_PROMPT}\n\nTranscript:\n{transcript_text}"
    try:
        if llm_provider == "gemini" and gemini_client:
            from google.genai import types as genai_types
            response = await gemini_client.aio.models.generate_content(
                model=model or "gemini-2.5-flash",
                contents=prompt,
                config=genai_types.GenerateContentConfig(
                    max_output_tokens=EXTRACTION_MAX_TOKENS,
                    response_mime_type="application/json",
                ),
            )
            text = _clean_json_response(response.text)
        elif llm_provider == "claude" and anthropic_client:
            response = await anthropic_client.messages.create(
                model=model or "claude-sonnet-4-20250514",
                max_tokens=EXTRACTION_MAX_TOKENS,
                messages=[{"role": "user", "content": prompt}],
            )
            text = _clean_json_response(response.content[0].text)
        elif llm_provider == "openai" and openai_client:
            response = await openai_client.chat.completions.create(
                model=model or "gpt-4o",
                max_tokens=EXTRACTION_MAX_TOKENS,
                messages=[{"role": "user", "content": prompt}],
            )
            text = _clean_json_response(response.choices[0].message.content)
        else:
            return None
        return json.loads(text)
    except Exception as exc:
        logger.error("Retry extraction failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# n8n webhook POST
# ---------------------------------------------------------------------------

async def _post_to_n8n(payload: dict[str, Any]) -> None:
    """POST the call data payload to the n8n webhook. Fire-and-forget."""
    from claim_api import get_session

    url = f"{N8N_BASE_URL}{N8N_POSTCALL_WEBHOOK}"
    try:
        session = await get_session()
        async with session.post(url, json=payload) as resp:
            if resp.status < 300:
                logger.info("Post-call data sent to n8n — status %d", resp.status)
            else:
                body = await resp.text()
                logger.error(
                    "n8n post-call webhook returned %d: %s", resp.status, body[:500],
                )
    except Exception as exc:
        logger.exception("Failed to POST post-call data to n8n: %s", exc)


# ---------------------------------------------------------------------------
# Orchestrator — entry point called from server.py
# ---------------------------------------------------------------------------

def _format_transcript(full_transcript: list[dict[str, str]]) -> str:
    """Convert transcript list to readable text."""
    lines: list[str] = []
    for entry in full_transcript:
        role = "Caller" if entry["role"] == "user" else "Agent"
        lines.append(f"{role}: {entry['text']}")
    return "\n".join(lines)


async def process_post_call_data(
    call_sid: str,
    lang: str,
    caller_phone: str,
    full_transcript: list[dict[str, str]],
    call_start_time: str,
    call_end_time: str,
    llm_provider: str,
    anthropic_client: Any | None = None,
    openai_client: Any | None = None,
    gemini_client: Any | None = None,
    model: str = "",
) -> None:
    """Orchestrate post-call processing: extract claim details, POST to n8n.

    This runs as a background task (asyncio.create_task). All exceptions
    are caught and logged -- never propagated.

    Resulting Google Sheet row columns (populated by n8n):
      Date/Time, Call SID, Caller Phone, Caller Name, Policy No,
      Vehicle Make, Vehicle Model, Reg No, Accident Description,
      Assessor ETA (min), Claim Reference, Dispatch Status, Outcome,
      Follow-Up Needed, Summary, Transcript.
    """
    try:
        logger.info("Starting post-call processing for CallSid: %s", call_sid)

        transcript_text = _format_transcript(full_transcript)

        # Extract structured claim details via LLM
        extracted = await extract_claim_details(
            transcript_text=transcript_text,
            lang=lang,
            llm_provider=llm_provider,
            anthropic_client=anthropic_client,
            openai_client=openai_client,
            gemini_client=gemini_client,
            model=model,
        )

        # Remove internal error key before sending to n8n
        extraction_error = extracted.pop("_extraction_error", None)
        if extraction_error:
            logger.warning(
                "Extraction had issues for %s: %s — sending partial data",
                call_sid, extraction_error,
            )

        # Build the payload
        lang_names = {"en": "English", "si": "Sinhala", "ta": "Tamil"}
        payload: dict[str, Any] = {
            "timestamp": call_start_time,
            "call_end_time": call_end_time,
            "call_sid": call_sid,
            "language": lang_names.get(lang, lang),
            "caller_phone": caller_phone,
            "transcript": transcript_text,
            **extracted,
        }

        # POST to n8n
        await _post_to_n8n(payload)

        logger.info(
            "Post-call processing complete for %s — dispatch_status: %s, follow_up: %s",
            call_sid, extracted.get("dispatch_status"), extracted.get("follow_up_needed"),
        )

    except Exception:
        logger.exception("Post-call processing failed for CallSid: %s", call_sid)
