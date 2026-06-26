"""post_call.py — Flico (Rodrigo Realtors) real-estate post-call lead capture.

After a call ends, server.py schedules ``process_realestate_post_call`` as a
background task. It:
  1. Formats the call transcript.
  2. Uses the LLM to extract a structured real-estate lead from it.
  3. POSTs a flat JSON payload to the n8n "realestate-post-call" webhook, which
     appends a row to the property manager's Google Sheet.

The webhook expects these exact top-level keys (n8n reads ``$json.<key>``):
  caller_name, caller_phone, intent, property_type, preferred_area, bedrooms,
  budget_lkr, lead_captured, summary, next_step, call_id
(Timestamp and the "Caller ID" column are derived inside n8n.)

All errors are caught and logged — never raised to the call path.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

REALESTATE_WEBHOOK_URL: str = os.getenv(
    "REALESTATE_WEBHOOK_URL",
    "https://automation.taskforceai.tech/webhook/realestate-post-call",
)

EXTRACTION_MAX_TOKENS: int = 1000

EXTRACTION_SYSTEM_PROMPT: str = """You are a lead-extraction assistant for Rodrigo Realtors, a Sri Lankan real estate agency that rents out apartments, houses, and commercial/office space across Colombo. Analyse a phone call transcript between the agency's virtual agent (Fiona) and a caller. The call may be in English, Sinhala, or Tamil.

Extract the lead and return ONLY a valid JSON object — no markdown, no code fences, no explanation:

{
  "caller_name": "caller's name, or null",
  "caller_phone": "the phone/WhatsApp number the caller gave for follow-up, digits only, or null",
  "intent": "one of: Rent, Buy, General inquiry, Viewing request, Callback requested",
  "property_type": "one of: apartment, house, commercial, or null",
  "preferred_area": "the Colombo zone or area the caller is interested in (e.g. 'Colombo 2', 'Cinnamon Gardens'), or null",
  "bedrooms": "number of bedrooms the caller wants, as a string (e.g. '3'), or null",
  "budget_lkr": "the caller's monthly budget in Sri Lankan rupees as a whole number (e.g. 200000), or null",
  "lead_captured": "Yes or No",
  "next_step": "the agreed next step (e.g. a salesperson will contact the caller to confirm details and arrange a viewing), or null",
  "summary": "a 1-2 sentence English summary of the whole call",
  "property_id": "the Ref code (e.g. P15) of the single property the caller was most interested in, taken ONLY from the PROPERTY CATALOG provided, or null"
}

RULES:
- All values must be in English regardless of the transcript language.
- "budget_lkr": convert lakhs to rupees (2 lakhs = 200000). Return a plain integer, no commas or currency words. Null if not mentioned.
- "lead_captured" = "Yes" only if the caller gave a name AND a contact number, OR clearly requested a viewing/callback with a reachable number. Otherwise "No".
- "intent": pick the closest match. Rodrigo Realtors only does rentals, so a caller looking for a place to live is "Rent"; a caller asking only general questions is "General inquiry"; a caller who asked to view a property is "Viewing request"; a caller who asked to be called back is "Callback requested".
- "property_id": choose the SINGLE Ref code from the PROPERTY CATALOG that matches the property the caller focused on or agreed to follow up on. Use null if no specific listing was discussed, or if it is genuinely ambiguous between several. Never invent a code that is not in the catalog.
- Always provide "summary". If a field was genuinely not discussed, use null.
- Return ONLY the JSON object."""


# ---------------------------------------------------------------------------
# Transcript formatting
# ---------------------------------------------------------------------------

def _format_transcript(transcript: list[dict[str, Any]]) -> str:
    """Render a transcript list into 'Guest:/Agent:' lines.

    Handles both shapes used in server.py:
      - ConversationRelay: {"role": "user"|"assistant", "text": "..."}
      - Media Streams:     {"role": "user"|"assistant", "content": "..."}
    Strips the '[Reference context: ...]' KB prefix that the Media Streams path
    prepends to user turns.
    """
    lines: list[str] = []
    for entry in transcript or []:
        role = str(entry.get("role", "")).lower()
        text = entry.get("text")
        if text is None:
            text = entry.get("content")
        if isinstance(text, list):  # Anthropic content blocks (no tools here, but be safe)
            text = " ".join(
                b.get("text", "") for b in text if isinstance(b, dict)
            )
        if not text:
            continue
        text = str(text).strip()
        if text.startswith("[Reference context:"):
            marker = text.find("Guest:")
            if marker != -1:
                text = text[marker + len("Guest:"):].strip()
            else:
                close = text.find("]")
                if close != -1:
                    text = text[close + 1:].strip()
        if not text:
            continue
        speaker = "Guest" if role in ("user", "guest") else "Agent"
        lines.append(f"{speaker}: {text}")
    return "\n".join(lines)


def _clean_json_response(text: str) -> str:
    """Strip markdown code fences from LLM JSON output."""
    text = text.strip()
    if text.startswith("```"):
        first_newline = text.index("\n") if "\n" in text else 3
        text = text[first_newline + 1:]
    if text.endswith("```"):
        text = text[:-3]
    return text.strip()


# ---------------------------------------------------------------------------
# LLM extraction (Anthropic Claude)
# ---------------------------------------------------------------------------

_CATALOG_CACHE: str | None = None


def _load_catalog() -> str:
    """Build a compact 'Pxx — listing' catalog from the KB docs (cached).

    Lets the extractor map the property the caller discussed to its Ref code.
    """
    global _CATALOG_CACHE
    if _CATALOG_CACHE is not None:
        return _CATALOG_CACHE
    import glob
    entries: list[str] = []
    try:
        try:
            from knowledge_base import DEFAULT_DOCS_DIRECTORY as docs_dir
        except Exception:
            docs_dir = os.getenv("KB_DOCS_DIRECTORY", "knowledge_docs")
        paths = glob.glob(os.path.join(docs_dir, "*.txt")) + glob.glob(os.path.join(docs_dir, "*.md"))
        for path in paths:
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line.startswith("Rodrigo Realtors has"):
                        continue
                    m = re.search(r"\(ref:\s*(p\d+)\)\s*$", line, re.IGNORECASE)
                    if not m:
                        continue
                    pid = m.group(1).upper()
                    desc = line[: m.start()].strip()
                    if len(desc) > 170:
                        desc = desc[:170].rsplit(" ", 1)[0] + "…"
                    entries.append(f"{pid} — {desc}")
    except Exception:
        logger.exception("[realestate-postcall] failed to load property catalog")
    _CATALOG_CACHE = "\n".join(entries)
    return _CATALOG_CACHE


async def extract_lead(
    anthropic_client: Any,
    transcript_text: str,
    model: str,
    catalog_text: str = "",
) -> dict[str, Any]:
    """Extract a structured real-estate lead from the transcript via Claude."""
    user_content = f"Transcript:\n\n{transcript_text}"
    if catalog_text:
        user_content = (
            f"PROPERTY CATALOG (Ref code — listing):\n{catalog_text}\n\n" + user_content
        )
    response = await anthropic_client.messages.create(
        model=model or "claude-sonnet-4-6",
        max_tokens=EXTRACTION_MAX_TOKENS,
        system=EXTRACTION_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_content}],
    )
    return json.loads(_clean_json_response(response.content[0].text))


# ---------------------------------------------------------------------------
# Payload building (exact n8n schema)
# ---------------------------------------------------------------------------

def _coerce_budget(value: Any) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return int(value)
    digits = re.sub(r"[^0-9]", "", str(value))
    return int(digits) if digits else None


def _normalize_phone(raw: Any) -> str | None:
    """Normalise a Sri Lankan phone number to E.164 (+94…)."""
    if not raw:
        return None
    s = re.sub(r"[^\d+]", "", str(raw))
    if not s:
        return None
    if s.startswith("+"):
        return "+" + re.sub(r"\D", "", s)
    digits = re.sub(r"\D", "", s)
    if not digits:
        return None
    if digits.startswith("0"):
        return "+94" + digits[1:]
    if digits.startswith("94"):
        return "+" + digits
    return "+" + digits


def _fmt_dt(dt: datetime | None) -> str | None:
    """Format a datetime as naive UTC ISO with microseconds (matches n8n schema)."""
    if dt is None:
        return None
    try:
        return dt.astimezone(timezone.utc).replace(tzinfo=None).isoformat()
    except Exception:
        return None


def _lower_or_none(value: Any) -> str | None:
    return value.strip().lower() if isinstance(value, str) and value.strip() else None


def _build_payload(
    call_sid: str,
    caller_phone_fallback: str,
    extracted: dict[str, Any],
    transcript_text: str = "",
    timestamp: str = "",
    call_end_time: str = "",
) -> dict[str, Any]:
    """Map extracted fields to the exact flat keys + types the n8n webhook reads."""
    bedrooms = extracted.get("bedrooms")
    bedrooms = str(bedrooms) if bedrooms not in (None, "") else None

    budget = _coerce_budget(extracted.get("budget_lkr"))
    budget_str = str(budget) if budget is not None else None

    lc = str(extracted.get("lead_captured", "")).strip().lower()
    lead_captured = "true" if lc in ("yes", "true", "y", "1") else "false"

    pid = extracted.get("property_id")
    pid = pid.strip().upper() if isinstance(pid, str) and re.match(r"^p\d+$", pid.strip(), re.IGNORECASE) else None

    return {
        "timestamp": timestamp or None,
        "call_end_time": call_end_time or None,
        "call_id": call_sid,
        "property_id": pid,
        "caller_name": extracted.get("caller_name"),
        "caller_phone": _normalize_phone(extracted.get("caller_phone") or caller_phone_fallback),
        "intent": _lower_or_none(extracted.get("intent")),
        "property_type": _lower_or_none(extracted.get("property_type")),
        "preferred_area": extracted.get("preferred_area"),
        "bedrooms": bedrooms,
        "budget_lkr": budget_str,
        "lead_captured": lead_captured,
        "summary": extracted.get("summary"),
        "next_step": extracted.get("next_step"),
        "transcript": transcript_text or None,
    }


# ---------------------------------------------------------------------------
# n8n webhook POST
# ---------------------------------------------------------------------------

async def _post_to_n8n(payload: dict[str, Any]) -> None:
    """POST the lead payload to the n8n webhook. Fire-and-forget."""
    if not REALESTATE_WEBHOOK_URL:
        logger.info("[realestate-postcall] no webhook URL configured — skipping")
        return
    try:
        async with httpx.AsyncClient(timeout=10) as http:
            resp = await http.post(REALESTATE_WEBHOOK_URL, json=payload)
            if resp.status_code < 300:
                logger.info(
                    "[realestate-postcall] sent ok (%d) for %s",
                    resp.status_code, payload.get("call_id"),
                )
            else:
                logger.warning(
                    "[realestate-postcall] webhook returned %d: %s",
                    resp.status_code, resp.text[:300],
                )
    except Exception:
        logger.exception("[realestate-postcall] POST failed")


# ---------------------------------------------------------------------------
# Orchestrator (scheduled as a background task from server.py)
# ---------------------------------------------------------------------------

async def process_realestate_post_call(
    call_sid: str,
    caller_phone: str,
    transcript: list[dict[str, Any]],
    anthropic_client: Any | None,
    model: str = "",
    started_at: datetime | None = None,
) -> None:
    """Extract the lead from the transcript and POST it to n8n. Never raises."""
    try:
        end_dt = datetime.now(timezone.utc)
        timestamp = _fmt_dt(started_at or end_dt)
        call_end_time = _fmt_dt(end_dt)

        transcript_text = _format_transcript(transcript)
        if not transcript_text.strip():
            logger.info("[realestate-postcall] empty transcript [%s] — skipping", call_sid)
            return

        extracted: dict[str, Any] = {}
        if anthropic_client is not None:
            try:
                extracted = await extract_lead(
                    anthropic_client, transcript_text, model, _load_catalog()
                )
            except Exception:
                logger.exception("[realestate-postcall] extraction failed [%s]", call_sid)
                extracted = {}
        else:
            logger.warning(
                "[realestate-postcall] no Anthropic client [%s] — sending minimal lead", call_sid
            )

        payload = _build_payload(
            call_sid, caller_phone, extracted, transcript_text, timestamp, call_end_time
        )
        await _post_to_n8n(payload)
    except Exception:
        logger.exception("[realestate-postcall] processing failed [%s]", call_sid)
