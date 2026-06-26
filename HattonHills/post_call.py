"""
post_call.py -- Post-call data extraction and n8n webhook integration.

After a call ends:
  1. Uses LLM to extract structured booking details from the full transcript.
  2. POSTs structured data + transcript to an n8n webhook.
  3. n8n appends a row to a Google Sheet for the property manager.

Called from server.py via asyncio.create_task() in the WebSocket finally blocks.
All errors are caught and logged -- never raised to the caller.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

try:
    import dashboard_client
except ImportError:
    dashboard_client = None

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
N8N_BASE_URL: str = os.getenv(
    "N8N_BASE_URL", "https://automation.taskforceai.tech"
)
N8N_POSTCALL_WEBHOOK: str = os.getenv(
    "N8N_POSTCALL_WEBHOOK", "/webhook/post-call-data"
)

EXTRACTION_MAX_TOKENS: int = 2000

# ---------------------------------------------------------------------------
# Extraction prompt
# ---------------------------------------------------------------------------
EXTRACTION_SYSTEM_PROMPT: str = """You are a data extraction assistant for a hotel called Hatton Hills in Sri Lanka. Analyze a phone call transcript between the hotel's reservations agent (Tanya) and a caller. The call may be in English, Sinhala, or Tamil.

Extract ALL available information and return ONLY a valid JSON object. No markdown code fences, no explanation — just the raw JSON.

{
  "guest_name": "caller's name or null",
  "num_guests": "e.g. '2 adults, 1 child (age 5)' or null",
  "check_in": "YYYY-MM-DD or null",
  "check_out": "YYYY-MM-DD or null",
  "room_preference": "room type name or null",
  "availability_result": "brief result of availability check, or null if not checked",
  "call_outcome": "see rules below",
  "follow_up_needed": "Yes or No",
  "summary": "1-2 sentence English summary of the entire call"
}

CRITICAL RULES FOR call_outcome — pick the BEST match:
- "booking_confirmed" = guest confirmed a booking during the call
- "booking_inquiry" = guest asked about booking/availability for specific dates (even if no tool was called)
- "general_inquiry" = guest only asked general questions (rates, amenities, directions, policies) without mentioning specific dates
- "callback_requested" = guest said they would call back, or asked to be called back
- "no_availability" = availability was checked but no rooms were available
- "dropped" = conversation ended abruptly or seems incomplete (very short, mid-sentence)
- ONLY use "other" if the call truly does not fit ANY of the above categories

RULES FOR dates:
- Look carefully for ANY mention of dates, months, or time periods in the transcript.
- If the guest says "next Friday", "April 15th", "this weekend", etc., convert to YYYY-MM-DD.
- If only a month is mentioned without specific dates, still note it (e.g. "2026-04-01" for "sometime in April").
- If a number of nights is mentioned, calculate check_out from check_in.

RULES FOR summary:
- ALWAYS provide a summary, even for short calls.
- Summarize what the guest wanted and what happened. Example: "Guest asked about room rates for 2 adults. Tanya provided pricing for Forest Escape Suite and Eco Harmony."
- Write in English regardless of transcript language.

RULES FOR follow_up_needed:
- "Yes" if: guest showed interest but didn't book, said they'd call back, asked to receive info, or conversation dropped before resolution.
- "No" only if: booking was confirmed, OR guest got all the info they needed and the call ended naturally.

All field values must be in English regardless of the transcript language.
If a field was genuinely not discussed at all, use null.
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
    """Extract booking details using Anthropic Claude."""
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
    """Extract booking details using OpenAI."""
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
    """Extract booking details using Google Gemini native SDK."""
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


async def extract_booking_details(
    transcript_text: str,
    lang: str,
    llm_provider: str,
    anthropic_client: Any | None = None,
    openai_client: Any | None = None,
    gemini_client: Any | None = None,
    model: str = "",
) -> dict[str, Any]:
    """Extract structured booking details from a call transcript via LLM.

    Returns a dict with the extracted fields. On failure, returns a dict
    with all fields set to None and an ``_extraction_error`` key.
    """
    empty: dict[str, Any] = {
        "guest_name": None,
        "num_guests": None,
        "check_in": None,
        "check_out": None,
        "room_preference": None,
        "availability_result": None,
        "call_outcome": "other",
        "follow_up_needed": "No",
        "summary": "Extraction failed — see transcript for details.",
    }

    try:
        if llm_provider == "claude" and anthropic_client:
            result = await _extract_with_claude(
                anthropic_client, transcript_text, model or "claude-sonnet-4-6",
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
        logger.error("Failed to parse LLM extraction JSON (attempt 1): %s", exc)
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


RETRY_PROMPT: str = """Extract these fields from the hotel call transcript as a short JSON object. Use null for missing fields.
Fields: guest_name, num_guests, check_in (YYYY-MM-DD), check_out (YYYY-MM-DD), room_preference, availability_result, call_outcome (booking_inquiry/general_inquiry/booking_confirmed/callback_requested/dropped), follow_up_needed (Yes/No), summary (1 sentence English).
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
                model=model or "claude-sonnet-4-6",
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
    # DEMO-SAFE: in the demo build, do NOT ship transcripts to the real n8n /
    # Google Sheets pipeline. Enabled by default; set DEMO_SAFE_BOOKINGS=false
    # to wire the real post-call webhook.
    if os.getenv("DEMO_SAFE_BOOKINGS", "true").lower() in ("1", "true", "yes"):
        logger.info("DEMO-SAFE: skipping n8n post-call webhook (no transcript shipped).")
        return

    from booking_api import get_session

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
        role = "Guest" if entry["role"] == "user" else "Tanya"
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
    """Orchestrate post-call processing: extract details, POST to n8n.

    This runs as a background task (asyncio.create_task). All exceptions
    are caught and logged -- never propagated.
    """
    try:
        logger.info("Starting post-call processing for CallSid: %s", call_sid)

        transcript_text = _format_transcript(full_transcript)

        # Extract structured booking details via LLM
        extracted = await extract_booking_details(
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
            "caller_phone": caller_phone.lstrip("+") if caller_phone else caller_phone,
            "transcript": transcript_text,
            **extracted,
        }

        # POST to n8n
        await _post_to_n8n(payload)

        if dashboard_client is not None:
            try:
                from datetime import datetime
                # duration: parse ISO strings, fall back to 0
                try:
                    _start = datetime.fromisoformat(call_start_time.replace("Z", "+00:00"))
                    _end = datetime.fromisoformat(call_end_time.replace("Z", "+00:00"))
                    _dur = int((_end - _start).total_seconds())
                except Exception:
                    _dur = 0
                logger.info(
                    "[handoff] dispatching call.completed: call_sid=%s caller_phone=%s",
                    call_sid, caller_phone,
                )
                await dashboard_client.send_call_completed(
                    call_sid=call_sid,
                    caller_phone=caller_phone,
                    lang=lang,
                    started_at_iso=call_start_time,
                    ended_at_iso=call_end_time,
                    duration_sec=_dur,
                    full_transcript=full_transcript,
                    extracted=extracted,
                )
            except Exception as exc:
                logger.warning("[dashboard] send_call_completed failed: %s", exc)

        logger.info(
            "Post-call processing complete for %s — outcome: %s, follow_up: %s",
            call_sid, extracted.get("call_outcome"), extracted.get("follow_up_needed"),
        )

    except Exception:
        logger.exception("Post-call processing failed for CallSid: %s", call_sid)
