"""
supabase_client.py — Lightweight async Supabase REST client for Flico.

Writes call records to the `calls` table via Supabase's PostgREST endpoint.
Uses httpx directly so we don't pull in the heavy supabase-py SDK.

Configured via environment:
    SUPABASE_URL         e.g. https://xxxxxx.supabase.co
    SUPABASE_SECRET_KEY  the secret (service-role) key — bypasses RLS

If either is unset, every helper is a no-op (logs a warning once) so the
agent keeps working with no dashboard backend.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

import httpx

logger = logging.getLogger(__name__)

SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_SECRET_KEY = os.getenv("SUPABASE_SECRET_KEY", "")
TABLE = "calls"

AUTOMATION_WEBHOOK_URL = os.getenv(
    "AUTOMATION_WEBHOOK_URL",
    "https://automation.taskforceai.tech/webhook/flico",
)

_warned_missing = False


def _enabled() -> bool:
    global _warned_missing
    if SUPABASE_URL and SUPABASE_SECRET_KEY:
        return True
    if not _warned_missing:
        logger.warning(
            "Supabase not configured (SUPABASE_URL / SUPABASE_SECRET_KEY missing) "
            "— call records will not be persisted."
        )
        _warned_missing = True
    return False


def _headers() -> dict[str, str]:
    return {
        "apikey": SUPABASE_SECRET_KEY,
        "Authorization": f"Bearer {SUPABASE_SECRET_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal",
    }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def record_call_start(
    call_sid: str,
    from_number: str | None,
    language: str = "en",
) -> None:
    """Insert a new row when a call connects."""
    if not _enabled() or not call_sid or call_sid == "unknown":
        return

    payload = {
        "call_sid": call_sid,
        "from_number": from_number,
        "language": language,
        "started_at": _now(),
        "status": "in_progress",
    }

    url = f"{SUPABASE_URL}/rest/v1/{TABLE}"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(
                url,
                headers={**_headers(), "Prefer": "resolution=merge-duplicates"},
                json=payload,
            )
            if resp.status_code >= 300:
                logger.warning(
                    "Supabase insert failed [%s]: %s %s",
                    call_sid, resp.status_code, resp.text[:300],
                )
            else:
                logger.info("Supabase: recorded call start %s", call_sid)
    except Exception as exc:
        logger.warning("Supabase insert error [%s]: %s", call_sid, exc)


async def record_call_end(
    call_sid: str,
    transcript: list[dict],
    started_at: datetime | None = None,
    captured_name: str | None = None,
    captured_phone: str | None = None,
    captured_email: str | None = None,
    summary: str | None = None,
) -> None:
    """Update a call row when the session ends."""
    if not _enabled() or not call_sid or call_sid == "unknown":
        return

    now_dt = datetime.now(timezone.utc)
    duration = None
    if started_at is not None:
        try:
            duration = max(0, int((now_dt - started_at).total_seconds()))
        except Exception:
            duration = None

    payload: dict[str, Any] = {
        "ended_at": now_dt.isoformat(),
        "duration_seconds": duration,
        "status": "completed",
        "transcript": transcript,
    }
    if captured_name is not None:
        payload["captured_name"] = captured_name
    if captured_phone is not None:
        payload["captured_phone"] = captured_phone
    if captured_email is not None:
        payload["captured_email"] = captured_email
    if summary is not None:
        payload["summary"] = summary

    url = f"{SUPABASE_URL}/rest/v1/{TABLE}?call_sid=eq.{call_sid}"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.patch(url, headers=_headers(), json=payload)
            if resp.status_code >= 300:
                logger.warning(
                    "Supabase update failed [%s]: %s %s",
                    call_sid, resp.status_code, resp.text[:300],
                )
            else:
                logger.info(
                    "Supabase: recorded call end %s (duration=%ss, name=%r, phone=%r)",
                    call_sid, duration, captured_name, captured_phone,
                )
    except Exception as exc:
        logger.warning("Supabase update error [%s]: %s", call_sid, exc)


async def extract_caller_details(
    anthropic_client,
    transcript: list[dict],
    model: str,
) -> tuple[str | None, str | None, str | None, str | None, list[str]]:
    """Run a small LLM call to extract caller name, phone, email, summary, and products.

    Returns (name, phone, email, summary, products) — any of the strings may be
    None if not mentioned; products is [] if none were discussed.
    """
    if not transcript:
        return None, None, None, None, []

    # Build a compact plain-text transcript for the extractor.
    lines: list[str] = []
    for turn in transcript:
        role = turn.get("role", "?")
        text = turn.get("text", "")
        if not text:
            continue
        speaker = "Caller" if role == "user" else "Agent"
        lines.append(f"{speaker}: {text}")
    if not lines:
        return None, None, None, None, []

    convo = "\n".join(lines)
    prompt = (
        "Extract the caller's name, phone number, email address, and a brief "
        "summary of the call from this customer-service phone call transcript. "
        "The caller is the person calling in, NOT the agent. Only return details "
        "the CALLER explicitly gave about themselves. If a field was never "
        "mentioned, return null for that field.\n\n"
        "Phone numbers may be spelled out as words (e.g. 'oh seven seven double "
        "three triple two one one'). Convert to digits only. 'double X' means "
        "XX, 'triple X' means XXX.\n\n"
        "Emails may be spelled out (e.g. 'chrys at taskforce a i dot tech'). "
        "Normalize to a valid email address.\n\n"
        "For the summary, write ONE concise sentence (max ~25 words) describing "
        "what the call was about: which product(s) were discussed, the caller's "
        "intent (price check / wants to buy / general info / complaint), and "
        "the outcome (details requested / will visit store / no further "
        "interest).\n\n"
        "Also extract a list of specific products the caller showed interest in "
        "(model names or product types they asked about, e.g. 'KONKA 250L "
        "Refrigerator', 'Samsung Side-by-Side Fridge'). Empty list if none.\n\n"
        "Return STRICT JSON only, no prose, in this exact shape:\n"
        '{"name": "<first or full name as said>" or null, '
        '"phone": "<digits only, no spaces or dashes>" or null, '
        '"email": "<normalized email>" or null, '
        '"summary": "<one concise sentence>" or null, '
        '"products": ["<product name>", ...]}\n\n'
        f"Transcript:\n{convo}"
    )

    try:
        resp = await anthropic_client.messages.create(
            model=model,
            max_tokens=250,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = "".join(
            block.text for block in resp.content
            if getattr(block, "type", "") == "text"
        ).strip()

        # Strip code fences if any
        if raw.startswith("```"):
            raw = raw.strip("`")
            if raw.lower().startswith("json"):
                raw = raw[4:].strip()

        data = json.loads(raw)
        name = data.get("name")
        phone = data.get("phone")
        email = data.get("email")
        summary = data.get("summary")
        if isinstance(name, str):
            name = name.strip() or None
        else:
            name = None
        if isinstance(phone, str):
            phone = "".join(ch for ch in phone if ch.isdigit()) or None
        else:
            phone = None
        if isinstance(email, str):
            email = email.strip().lower() or None
            if email and "@" not in email:
                email = None
        else:
            email = None
        if isinstance(summary, str):
            summary = summary.strip() or None
        else:
            summary = None
        products_raw = data.get("products")
        products: list[str] = []
        if isinstance(products_raw, list):
            for item in products_raw:
                if isinstance(item, str):
                    cleaned = item.strip()
                    if cleaned:
                        products.append(cleaned)
        return name, phone, email, summary, products
    except Exception as exc:
        logger.warning("Caller-detail extraction failed: %s", exc)
        return None, None, None, None, []


async def send_automation_webhook(
    payload: dict[str, Any],
    *,
    max_attempts: int = 4,
    base_delay_seconds: float = 2.0,
) -> None:
    """POST captured call details to the automation webhook (n8n etc.).

    Retries on transient failures (network errors, HTTP 408/429/5xx) with
    exponential backoff: 2s, 4s, 8s between attempts. 4xx other than 408/429
    are not retried (they will not succeed on retry). All errors are logged,
    never raised — voice-call cleanup must never crash on webhook issues.
    """
    if not AUTOMATION_WEBHOOK_URL:
        return

    import asyncio

    call_sid = payload.get("call_sid")
    transient_status = {408, 425, 429, 500, 502, 503, 504}
    last_err: str | None = None

    async with httpx.AsyncClient(timeout=15.0) as client:
        for attempt in range(1, max_attempts + 1):
            try:
                resp = await client.post(
                    AUTOMATION_WEBHOOK_URL,
                    json=payload,
                    headers={"Content-Type": "application/json"},
                )
                if resp.status_code < 300:
                    logger.info(
                        "Automation webhook sent [%s] on attempt %d -> %s",
                        call_sid, attempt, AUTOMATION_WEBHOOK_URL,
                    )
                    return

                body_preview = resp.text[:300]
                if resp.status_code in transient_status and attempt < max_attempts:
                    delay = base_delay_seconds * (2 ** (attempt - 1))
                    logger.warning(
                        "Automation webhook transient %s [%s] attempt %d/%d, "
                        "retrying in %.1fs: %s",
                        resp.status_code, call_sid, attempt, max_attempts,
                        delay, body_preview,
                    )
                    await asyncio.sleep(delay)
                    continue

                logger.warning(
                    "Automation webhook failed [%s] attempt %d/%d: %s %s",
                    call_sid, attempt, max_attempts,
                    resp.status_code, body_preview,
                )
                return

            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_err = repr(exc)
                if attempt < max_attempts:
                    delay = base_delay_seconds * (2 ** (attempt - 1))
                    logger.warning(
                        "Automation webhook network error [%s] attempt %d/%d, "
                        "retrying in %.1fs: %s",
                        call_sid, attempt, max_attempts, delay, last_err,
                    )
                    await asyncio.sleep(delay)
                    continue
                logger.warning(
                    "Automation webhook gave up after %d attempts [%s]: %s",
                    max_attempts, call_sid, last_err,
                )
                return
            except Exception as exc:
                logger.warning(
                    "Automation webhook unexpected error [%s]: %s",
                    call_sid, exc,
                )
                return
