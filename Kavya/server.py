"""
server.py — Main FastAPI server for Hatton Hills Voice Agent (Kavya).

Hatton Hills is a SINGLE property: a luxury boutique eco retreat in an
eight-acre private forest in Sri Lanka's central hill country, with exactly five
room types. The two-property (Mosvold) disambiguation machinery was collapsed to
single-property mode on 2026-07-30 — see yanolja_service.resolve_property.

Handles:
  - IVR / DTMF language menu (POST /voice/incoming)
  - Language routing (POST /voice/language-selected)
  - ConversationRelay WebSocket (/ws/conversation?lang=en|si|ta)
  - Streaming Claude responses with tool-use support
  - Knowledge-base context injection
  - Health endpoint (GET /health)

Architecture:
  Incoming call
    â†’ POST /voice/incoming â†’ TwiML <Gather> (press 1/2/3)
    â†’ POST /voice/language-selected â†’ ConversationRelay TwiML
    â†’ WebSocket /ws/conversation?lang=...
    â†’ Claude streaming with tool use
    â†’ text tokens â†’ Twilio TTS â†’ caller

  TTS routing by language:
    English  â†’ ElevenLabs (flash_v2_5, cloned voice) via ConversationRelay
    Sinhala  â†’ Azure Cognitive Services (si-LK-ThiliniNeural) via Media Streams
    Tamil    â†’ Azure Cognitive Services (ta-LK-SaranyaNeural) via Media Streams
"""

from __future__ import annotations

import asyncio
import audioop
import base64
import json
import logging
import os

# --- Error tracking (Sentry): no-op unless SENTRY_DSN is set ---
if os.getenv("SENTRY_DSN"):
    import sentry_sdk

    sentry_sdk.init(
        dsn=os.getenv("SENTRY_DSN"),
        traces_sample_rate=float(os.getenv("SENTRY_TRACES_SAMPLE_RATE", "0.0")),
        environment=os.getenv("SENTRY_ENV", "production"),
        send_default_pii=os.getenv("SENTRY_SEND_PII", "false").lower() == "true",
        enable_logs=os.getenv("SENTRY_ENABLE_LOGS", "true").lower() == "true",
    )
    sentry_sdk.set_tag("agent", "kavya")

import queue
import re
import threading
import time
import wave
import xml.sax.saxutils
from contextlib import asynccontextmanager
from datetime import date, datetime
from html import escape as html_escape
from typing import Any
from urllib.parse import quote as url_quote

import httpx
from anthropic import AsyncAnthropic, NOT_GIVEN
from openai import AsyncOpenAI
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import Response
from twilio.rest import Client as TwilioRestClient

from knowledge_base import retrieve_context, initialize_kb, prewarm, reload_kb_from_content
from tools import (
    get_tools,
    get_tools_openai,
    get_tools_gemini,
    get_handover_tools,
    execute_tool,
)
from booking_api import close_session, is_configured
# Imported for DEMO_RATES_ENABLED so the system prompt and the tool results
# agree on whether rates may be quoted. Already loaded transitively via
# booking_api; the explicit import keeps the single source of truth visible.
import yanolja_service
from post_call import process_post_call_data
from handover import handover_context, send_handover_notification

try:
    import dashboard_client
except ImportError:
    dashboard_client = None


def _dashboard_call_started(call_sid, caller_phone, lang, started_at):
    if dashboard_client is None:
        return
    import asyncio
    logger.info(
        "[handoff] dispatching call.started: call_sid=%s caller_phone=%s lang=%s",
        call_sid, caller_phone, lang,
    )
    asyncio.create_task(dashboard_client.send_call_started(call_sid, caller_phone, lang, started_at))

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Environment variables
# ---------------------------------------------------------------------------
ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")
OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "")

# OpenAI gpt-4o-mini-tts -- Kavya's Sinhala voice. Natural prosody, streams
# fast, and handles code-switched English far better than Azure or VITS.
OPENAI_TTS_URL: str = "https://api.openai.com/v1/audio/speech"
OPENAI_TTS_MODEL: str = os.getenv("OPENAI_TTS_MODEL", "gpt-4o-mini-tts")
OPENAI_TTS_VOICE: str = os.getenv("OPENAI_TTS_VOICE", "nova")
OPENAI_TTS_INSTRUCTIONS: str = os.getenv(
    "OPENAI_TTS_INSTRUCTIONS",
    "You are Kavya, a warm and friendly reservations agent at Hatton "
    "Hills, in Sri Lanka's central hill country. Speak in natural, "
    "lively conversational Sinhala with genuine warmth -- smile as you talk. "
    "Vary your pitch and pace naturally, soften when being empathetic, and "
    "pause briefly between ideas. Sound like a real person chatting on the "
    "phone, not a robot reading text.",
)
ELEVENLABS_API_KEY: str = os.getenv("ELEVENLABS_API_KEY", "")
ELEVENLABS_VOICE_ID: str = os.getenv("ELEVENLABS_VOICE_ID", "")
# Dedicated Arabic voice (Media Streams). Falls back to ELEVENLABS_VOICE_ID if unset.
ELEVENLABS_VOICE_ID_AR: str = os.getenv("ELEVENLABS_VOICE_ID_AR", "tavIIPLplRB883FzWU0V")
ELEVENLABS_MODEL_MULTILINGUAL: str = "eleven_multilingual_v2"
ELEVENLABS_MODEL_TURBO: str = "eleven_turbo_v2_5"
TWILIO_ACCOUNT_SID: str = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN: str = os.getenv("TWILIO_AUTH_TOKEN", "")
HUMAN_AGENT_PHONE: str = os.getenv("HUMAN_AGENT_PHONE", "").strip()

# Seconds to let the human agent's phone ring before giving up and falling back
# to the WhatsApp failsafe.
#
# Was hard-coded to 20. On 2026-07-31 four of six transfers to a Sri Lankan
# mobile came back status=no-answer, duration=0, unbilled — Twilio placed the
# call and the carrier accepted it, but nobody picked up inside the window. The
# config was byte-identical on the calls that DID connect, so this is answer-side
# latency, not routing. 20s is tight for an international leg to a mobile:
# carrier setup can eat 5-8s of it, leaving barely a dozen seconds of audible
# ringing — often less than one full ring cycle at the handset.
#
# Twilio allows up to 600. Keep it comfortably under the caller's patience: the
# guest is holding music-free silence while this runs.
HANDOFF_DIAL_TIMEOUT: int = int(os.getenv("HANDOFF_DIAL_TIMEOUT", "40"))

# Minimum plausible time between a dial being placed and a HUMAN answering it.
#
# WHY THIS EXISTS: on 2026-08-03 a transfer to the manager was answered by a
# carrier intercept — the leg was answered in the SAME SECOND it was initiated,
# played a recorded announcement at the guest for 52 seconds, and reported
# DialCallStatus=completed. "completed" is indistinguishable from a real pickup,
# so the failsafe stood down and nobody was ever told the guest had called. A
# real handset cannot be lifted in under a second; anything that fast is a
# network answer (intercept, unconditional divert, or instant voicemail).
HANDOFF_MIN_ANSWER_SECONDS: float = float(
    os.getenv("HANDOFF_MIN_ANSWER_SECONDS", "2.0")
)

# Hang up a leg the moment it is identified as a carrier intercept, instead of
# holding the guest through the recording. See /voice/dial-status for the full
# reasoning. Set to "false" to disable at runtime without a code deploy.
HANDOFF_KILL_INTERCEPT: bool = os.getenv(
    "HANDOFF_KILL_INTERCEPT", "true"
).strip().lower() not in ("0", "false", "no", "off")

# Caller ID presented to the human agent when a call is transferred.
#
# WHY THIS EXISTS: <Dial> with no callerId makes Twilio pass through the
# ORIGINAL caller's number. On 2026-07-31 that meant outbound legs to the
# manager's Sri Lankan mobile were presented as coming FROM another Sri Lankan
# mobile that the Twilio account does not own. Twilio accepts this, but the
# destination carrier commonly filters it as caller-ID spoofing, so the handset
# never rings. The failure is NOT consistent, which is what makes it dangerous:
#   - 2026-07-31: carrier reported status=no-answer, duration=0 (handset silent,
#     failsafe fired correctly).
#   - 2026-08-03: carrier ANSWERED the leg instantly with a recorded intercept,
#     played it at the guest for 52 s, and reported status=completed — so the
#     transfer looked successful, the failsafe stood down, and the lead vanished.
#     See HANDOFF_MIN_ANSWER_SECONDS for the guard against that second shape.
#
# Setting this to a number the Twilio account OWNS makes the leg deliverable.
#
# LEAVING IT UNSET DOES NOT FALL BACK TO AN OWNED NUMBER. An earlier version of
# this comment claimed it fell back to the Twilio number the guest dialled; it
# does not, and that wrong comment is why the variable sat unset in production
# until 2026-08-03. Unset means pass-through, i.e. the broken path above.
# `_transfer_caller_id()` returns exactly this value and nothing else.
TWILIO_CALLER_ID: str = os.getenv("TWILIO_CALLER_ID", "").strip()
PUBLIC_HOSTNAME: str = os.getenv("PUBLIC_HOSTNAME", "voice.taskforceai.tech").strip()

# Twilio REST client singleton — used for Path B human handoff
# (client.calls(sid).update(twiml=...)) to bypass the unreliable
# ConversationRelay {"type":"end"} + HandoffData flow.
_twilio_client: TwilioRestClient | None = None


def _get_twilio_client() -> TwilioRestClient | None:
    global _twilio_client
    if _twilio_client is None and TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN:
        _twilio_client = TwilioRestClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    return _twilio_client
KB_DOCS_DIRECTORY: str = os.getenv("KB_DOCS_DIRECTORY", "knowledge_docs")
KB_RELOAD_SECRET: str = os.getenv("KB_RELOAD_SECRET", "")
PORT: int = int(os.getenv("PORT", "8000"))
AZURE_SPEECH_KEY: str = os.getenv("AZURE_SPEECH_KEY", "")
AZURE_SPEECH_REGION: str = os.getenv("AZURE_SPEECH_REGION", "southeastasia")

# Media Streams STT backend: "google" (default) or "azure".
# Azure reuses AZURE_SPEECH_KEY / AZURE_SPEECH_REGION (already set for Sinhala TTS).
STT_PROVIDER: str = os.getenv("STT_PROVIDER", "google").lower()

# Debug: dump live call audio to an 8 kHz PCM16 wav for offline STT benchmarking.
STT_DEBUG_DUMP: bool = os.getenv("STT_DEBUG_DUMP", "0") == "1"
STT_DEBUG_DIR: str = os.getenv("STT_DEBUG_DIR", "stt_dumps")

# LLM provider selection: "claude" (default), "openai", or "gemini"
LLM_PROVIDER: str = os.getenv("LLM_PROVIDER", "claude")
CLAUDE_MODEL: str = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-20250514")
GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL: str = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

# ---------------------------------------------------------------------------
# Optional: Google Gemini native SDK
# ---------------------------------------------------------------------------
try:
    from google import genai as google_genai
    from google.genai import types as genai_types
    GOOGLE_GENAI_AVAILABLE = True
except ImportError:
    google_genai = None  # type: ignore[assignment]
    genai_types = None  # type: ignore[assignment]
    GOOGLE_GENAI_AVAILABLE = False
    logger.warning("google-genai not installed — native Gemini provider unavailable")

# ---------------------------------------------------------------------------
# Optional: Google Cloud Speech (Media Streams STT)
# ---------------------------------------------------------------------------
try:
    from google.cloud import speech_v1 as google_speech
    GOOGLE_STT_AVAILABLE = True
except ImportError:
    google_speech = None  # type: ignore[assignment]
    GOOGLE_STT_AVAILABLE = False
    logger.warning("google-cloud-speech not installed — Media Streams STT unavailable")

# ---------------------------------------------------------------------------
# Optional: Azure Speech (alternative Media Streams STT, selected via STT_PROVIDER)
# ---------------------------------------------------------------------------
try:
    import azure.cognitiveservices.speech as azure_speech
    AZURE_STT_AVAILABLE = True
except ImportError:
    azure_speech = None  # type: ignore[assignment]
    AZURE_STT_AVAILABLE = False
    logger.warning("azure-cognitiveservices-speech not installed — Azure STT provider unavailable")

# audioop decodes Twilio mulaw â†’ PCM16 for Azure's push stream and for audio
# dumps. Stdlib through Python 3.12; removed in 3.13 (use the audioop-lts shim).
try:
    import audioop
except ImportError:  # pragma: no cover
    audioop = None  # type: ignore[assignment]
    logger.warning("audioop unavailable (Python 3.13+) — Azure STT and audio dump need audioop-lts")

# ---------------------------------------------------------------------------
# LLM configuration
# ---------------------------------------------------------------------------
if LLM_PROVIDER == "claude":
    MODEL: str = CLAUDE_MODEL
elif LLM_PROVIDER == "gemini":
    MODEL: str = GEMINI_MODEL
else:
    MODEL: str = os.getenv("OPENAI_MODEL", "gpt-4o")
MAX_TOKENS: int = 300
MAX_HISTORY_MESSAGES: int = 60
MAX_TOOL_ROUNDS: int = 5

# Caller phone lookup — populated by HTTP handlers, consumed by WebSocket handlers
_call_phone: dict[str, str] = {}  # CallSid -> caller phone number


def _transfer_caller_id(call_sid: str) -> str:
    """Caller ID for the outbound transfer leg, or "" for Twilio's default.

    DEFAULTS TO "" — i.e. Twilio passes the GUEST's number through. That shows
    the manager who is actually calling, which is genuinely useful, but it is
    NOT safe on Sri Lankan mobile destinations: the leg arrives at the local
    carrier from an international gateway claiming a local CLI, and gets
    filtered or intercepted (see the TWILIO_CALLER_ID comment above).

    SET THIS IN PRODUCTION. Treating pass-through as the normal configuration is
    what broke handovers on 2026-07-31 and again on 2026-08-03. Point it at a
    number the account owns — the number the guest dialled is the natural
    choice. The manager loses the guest's CLI, but the whisper announces the
    reason and the failsafe WhatsApp carries the guest's number, so nothing is
    actually lost.
    """
    return TWILIO_CALLER_ID

# Handoff carry-over — populated when a live transfer to a human is dispatched,
# read back by the recovery ConversationRelay session if the human never picked
# up. The relay session that follows a failed dial is a brand-new WebSocket with
# empty history, so everything Kavya needs to run the failsafe (what the guest
# wanted, their name if given, the number they called from) has to survive here.
# CallSid -> {reason, caller_phone, transcript, dial_status, notified, dial_events}
_handoff_state: dict[str, dict] = {}


def _answer_looks_intercepted(state: dict) -> tuple[bool, str]:
    """Did the 'answered' dial leg reach a human, or a network intercept?

    Reads the per-event timestamps recorded by /voice/dial-status. A handset
    cannot be answered in under a second; when it happens the leg was taken by
    an intercept recording, an unconditional divert, or instant voicemail.

    FAILS OPEN. If the timestamps are missing (status callback lost, or it
    raced the action callback) this returns False and the transfer is treated
    as genuine. A false negative costs one missed WhatsApp; a false positive
    would bounce a guest who really did speak to a human back into the
    failsafe, which is worse.
    """
    events = state.get("dial_events") or {}
    initiated = events.get("initiated")
    answered = events.get("answered")
    if initiated is None or answered is None:
        return False, "no timing available"
    gap = answered - initiated
    if gap < HANDOFF_MIN_ANSWER_SECONDS:
        return True, f"answered {gap:.2f}s after dial — too fast for a handset"
    return False, f"answered after {gap:.2f}s"
_HANDOFF_STATE_MAX = 200  # bound the dict; abandoned calls never clean up


def _safe_client(factory):
    """Build an LLM client, returning None instead of raising.

    The client getters raise when their API key is unset. That is the right
    behaviour on the conversation path — no key means no call — but on the
    post-call bookkeeping path it would throw away the record entirely. Here a
    missing client just degrades the summary, so swallow and continue.
    """
    try:
        return factory()
    except Exception:
        logger.warning("LLM client unavailable for post-call summary", exc_info=True)
        return None


def _remember_handoff(call_sid: str, **fields) -> None:
    """Record/merge handoff carry-over for a call, evicting the oldest entries."""
    if not call_sid or call_sid == "unknown":
        return
    entry = _handoff_state.setdefault(call_sid, {})
    entry.update(fields)
    while len(_handoff_state) > _HANDOFF_STATE_MAX:
        _handoff_state.pop(next(iter(_handoff_state)), None)

# ---------------------------------------------------------------------------
# Filler messages sent while tools execute
# ---------------------------------------------------------------------------
TOOL_FILLERS: dict[str, str] = {
    "check_availability": "Let me check availability for those dates.",
    "create_booking": "I'm creating your reservation now.",
    "retrieve_booking": "Let me look up that booking for you.",
    "cancel_booking": "Let me process that cancellation.",
    "notify_human_handover": "Let me pass your details to our team now.",
}
DEFAULT_FILLER: str = "Let me check that for you."


def _join_turn(accumulated: str, new_text: str) -> str:
    """Append one tool-round's text to the running response, with a separator.

    A plain `+=` runs the rounds together in the TRANSCRIPT: the model's
    pre-tool line and its post-tool line arrive as separate streaming rounds,
    so "…right away." + "I'm transferring…" became
    "…right away.I'm transferring…". The spoken audio is unaffected (each round
    is streamed to Twilio on its own), but the mangled string is what gets
    logged and shipped to the Google Sheet call log.

    Only inserts a space when both sides are non-empty and the boundary isn't
    already whitespace, so it never introduces a leading/double space.
    """
    if not new_text:
        return accumulated
    if not accumulated:
        return new_text
    if accumulated[-1].isspace() or new_text[0].isspace():
        return accumulated + new_text
    return accumulated + " " + new_text

# Backchannel filter: short non-semantic utterances that callers emit while
# thinking ("um", "uh", "hmm"). Twilio's STT fires these as full prompts and
# without filtering, Kavya would jump in mid-thought, derailing the call.
# We deliberately do NOT include "ok", "yeah", "yes", "no", "right" — those
# are genuine answers in this booking flow.
BACKCHANNEL_TOKENS: set[str] = {
    "um", "uh", "uhm", "umm", "uhh", "erm", "er",
    "hmm", "hm", "mm", "mhm", "mmhm", "mhmm",
    "ah", "oh", "huh",
    "ah um", "uh um", "um uh", "uh uh",
}


def _is_backchannel(text: str) -> bool:
    """True if the utterance is purely thinking-noise — should be ignored
    so the caller keeps the turn. Strips punctuation and lowercases."""
    # Digit-bearing utterances are real content (phone numbers, dates,
    # room counts, etc.) — never treat them as backchannel.
    if any(c.isdigit() for c in text):
        return False
    cleaned = "".join(c for c in text.lower() if c.isalpha() or c.isspace()).strip()
    if not cleaned:
        return True  # empty / pure punctuation
    if len(cleaned) > 8:  # anything longer than "uh uh um" is probably real
        return False
    return cleaned in BACKCHANNEL_TOKENS

# Sent when the LLM hasn't returned its first token within SLOW_RESPONSE_DELAY
# seconds — covers Anthropic 429 retries and other network latency so the
# guest doesn't think the line dropped and re-speak (which corrupts slot-filling).
SLOW_RESPONSE_DELAY: float = 2.5
SLOW_RESPONSE_FILLERS: dict[str, str] = {
    "en": "One moment please.",
    "ar": "لحظة من فضلك.",
    "si": "à¶šà¶»à·”à¶¯à·à¶šà¶»à· à¶»à·à¶¯à·™à¶±à·Šà¶±.",
    "ta": "à®¤à®¯à®µà¯à®šà¯†à®¯à¯à®¤à¯ à®•à®¾à®¤à¯à®¤à®¿à®°à¯à®™à¯à®•à®³à¯.",
}

# ---------------------------------------------------------------------------
# IVR language configurations
# ---------------------------------------------------------------------------
# Set IVR_MENU_ENABLED=true to present the DTMF language menu on incoming
# calls. Default "false": every call connects straight to the English
# ConversationRelay agent (no "press 1/2/3" prompt). /voice/language-selected
# stays wired either way, so re-enabling the menu needs no code change.
IVR_MENU_ENABLED: bool = os.getenv("IVR_MENU_ENABLED", "false").lower() == "true"

# Maps DTMF digit â†’ language code
# English-only line. Sinhala and Arabic were removed from the menu on
# 2026-07-28; Sinhala/Tamil/Arabic code paths remain fully implemented below
# but no digit routes to them. To re-expose one, add its digit here and a
# matching <Say> prompt in /voice/incoming, and re-add it to the
# ws_media_stream guard.
DIGIT_TO_LANG: dict[str, str] = {"1": "en"}

# ConversationRelay transcription hints (#121). A comma-separated vocabulary
# that biases Google's telephony STT toward tokens it otherwise mishears on
# Sri Lankan-accented English. Two groups:
#   1. Spoken digit shorthand for phone numbers — "double"/"triple" and the
#      digit words. Without these, "double seven" is garbled and Kavya never
#      receives the word "double" to expand, so she cannot understand it live.
#   2. A starter set of common Sri Lankan given names and surnames, so names
#      survive transcription (see the wrong-name booking incident).
# Extend without a code change via CR_HINTS_EN.
_DEFAULT_EN_HINTS = (
    "double, triple, treble, oh, zero, one, two, three, four, five, six, "
    "seven, eight, nine, "
    "Chanya, Shehani, Oshadi, Kavya, Nimal, Kamal, Sunil, Saman, Chaminda, "
    "Ruwan, Nuwan, Kasun, Tharindu, Sachini, Nadeesha, Dilhani, Ishara, "
    "Hasini, Dilan, "
    "Perera, Fernando, Silva, Bandara, Jayawardena, Wickramasinghe, "
    "Gunawardena, Rajapaksa, Dissanayake, Senanayake, Ranasinghe, Wijesinghe"
)
CR_HINTS_EN: str = os.getenv("CR_HINTS_EN", _DEFAULT_EN_HINTS)

# Separate STT language for the English line (#121). Default "" keeps the
# existing behaviour (transcription follows `language`, en-US). Set
# CR_TRANSCRIPTION_LANGUAGE=en-IN to A/B whether Indian-English acoustic models
# recognise Sri Lankan accents better — env-controlled so it flips without a
# redeploy, and emitted only when set so a bad value can't affect the default.
CR_TRANSCRIPTION_LANGUAGE_EN: str = os.getenv("CR_TRANSCRIPTION_LANGUAGE", "").strip()

# Per-language ConversationRelay TwiML configuration
LANGUAGE_CONFIGS: dict[str, dict[str, str]] = {
    "en": {
        "tts_provider": "ElevenLabs",
        "voice": "bm3QvaZ3fUSCRBC3UV1f-flash_v2_5",
        "language": "en-US",
        "transcription_language": CR_TRANSCRIPTION_LANGUAGE_EN,
        "hints": CR_HINTS_EN,
        "welcome_greeting": "Welcome to Hatton Hills! I'm Kavya, how can I help you today?",
        "extra_attrs": '        elevenlabsTextNormalization="on"\n',
    },
    "si": {
        "tts_provider": "google",
        "voice": "si-LK-Standard-A",
        "language": "si-LK",
        "welcome_greeting": (
            "\u0D86\u0DBA\u0DD4\u0DB6\u0DDD\u0DC0\u0DB1\u0DCA! "
            "Hatton Hills \u0DC0\u0DD9\u0DAD "
            "\u0DC3\u0DCF\u0DAF\u0DBB\u0DBA\u0DD9\u0DB1\u0DCA "
            "\u0DB4\u0DD2\u0DC5\u0DD2\u0D9C\u0DB1\u0DD2\u0DB8\u0DD4. "
            "\u0DB8\u0DA7 \u0D94\u0DB6\u0DA7 "
            "\u0D9A\u0DD9\u0DC3\u0DDA "
            "\u0D8B\u0DAF\u0DC0\u0DCA "
            "\u0D9A\u0DC5 \u0DC4\u0DD0\u0D9A\u0DD2\u0DAF?"
        ),
        "extra_attrs": "",
    },
    "ta": {
        "tts_provider": "google",
        "voice": "ta-IN-Standard-A",
        "language": "ta-IN",
        "welcome_greeting": (
            "\u0BB5\u0BA3\u0B95\u0BCD\u0B95\u0BAE\u0BCD! "
            "Hatton Hills \u0B95\u0BCD\u0B95\u0BC1 "
            "\u0BB5\u0BB0\u0BB5\u0BC7\u0BB1\u0BCD\u0B95\u0BBF\u0BB1\u0BCB\u0BAE\u0BCD. "
            "\u0BA8\u0BBE\u0BA9\u0BCD "
            "\u0B89\u0B99\u0BCD\u0B95\u0BB3\u0BC1\u0B95\u0BCD\u0B95\u0BC1 "
            "\u0B8E\u0BAA\u0BCD\u0BAA\u0B9F\u0BBF "
            "\u0B89\u0BA4\u0BB5\u0BB2\u0BBE\u0BAE\u0BCD?"
        ),
        "extra_attrs": "",
    },
}

# ---------------------------------------------------------------------------
# Media Streams — Azure TTS + Google STT (Sinhala / Tamil)
# ---------------------------------------------------------------------------
AZURE_TTS_URL = "https://{region}.tts.speech.microsoft.com/cognitiveservices/v1"
ELEVENLABS_TTS_URL = "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream"

# Azure voice per language code
AZURE_VOICES: dict[str, tuple[str, str]] = {
    "si": ("si-LK", "si-LK-SameeraNeural"),   # male voice
    "ta": ("ta-LK", "ta-LK-SaranyaNeural"),   # female voice
}

# Google STT primary + alternative languages per lang code
STT_PRIMARY: dict[str, str] = {"si": "si-LK", "ta": "ta-IN", "ar": "ar-SA"}
STT_ALTERNATIVES: dict[str, list[str]] = {
    "si": ["en-US", "ta-IN"],
    "ta": ["en-US", "si-LK"],
    "ar": ["en-US"],
}

# Silence (seconds) after last STT result before utterance is considered complete
ENDPOINTING_SILENCE: float = 1.5

# Silence (seconds) after greeting / agent turn before we re-prompt the caller.
# If the caller never speaks, we re-greet them or ask if they're still online,
# up to MAX_REPROMPTS times. After that we stop re-prompting.
SILENCE_REPROMPT_DELAY: float = 18.0
MAX_REPROMPTS: int = 1

# Re-prompt messages spoken when caller is silent. Index 0 = first nudge,
# index 1 = full re-greet on second silence.
REPROMPT_MESSAGES: dict[str, list[str]] = {
    "en": [  # English
        "Hello, are you still there?",
        "Welcome to Hatton Hills. How may I help you today?",
    ],
    "ar": [  # Arabic (MSA)
        "\u0645\u0631\u062d\u0628\u0627\u064b\u060c \u0647\u0644 \u0645\u0627 \u0632\u0644\u062a\u0645 \u0639\u0644\u0649 \u0627\u0644\u062e\u0637\u061f",
        "\u0623\u0647\u0644\u0627\u064b \u0628\u0643\u0645 \u0641\u064a Hatton Hills. \u0643\u064a\u0641 \u064a\u0645\u0643\u0646\u0646\u064a \u0645\u0633\u0627\u0639\u062f\u062a\u0643\u0645 \u0627\u0644\u064a\u0648\u0645\u061f",
    ],
    "si": [  # Sinhala
        "\u0d86\u0dba\u0dd4\u0db6\u0ddd\u0dc0\u0db1\u0dca, \u0d94\u0db6 \u0dad\u0dc0\u0db8\u0dad\u0dca \u0dc3\u0dd2\u0da7\u0dd2\u0db1\u0dca\u0db1\u0dda\u0daf?",
        "\u0d86\u0dba\u0dd4\u0db6\u0ddd\u0dc0\u0db1\u0dca! Hatton Hills \u0dc0\u0dd9\u0dad \u0dc3\u0dcf\u0daf\u0dbb\u0dba\u0dd9\u0db1\u0dca \u0db4\u0dd2\u0dc5\u0dd2\u0d9c\u0db1\u0dd2\u0db8\u0dd4. \u0db8\u0da7 \u0d94\u0db6\u0da7 \u0d9a\u0dd9\u0dc3\u0dda \u0d8b\u0daf\u0dc0\u0dca \u0d9a\u0dc5 \u0dc4\u0dd0\u0d9a\u0dd2\u0daf?",
    ],
    "ta": [  # Tamil
        "\u0bb5\u0ba3\u0b95\u0bcd\u0b95\u0bae\u0bcd, \u0ba8\u0bc0\u0b99\u0bcd\u0b95\u0bb3\u0bcd \u0b87\u0ba9\u0bcd\u0ba9\u0bc1\u0bae\u0bcd \u0b87\u0bb0\u0bc1\u0b95\u0bcd\u0b95\u0bbf\u0bb1\u0bc0\u0bb0\u0bcd\u0b95\u0bb3\u0bbe?",
        "\u0bb5\u0ba3\u0b95\u0bcd\u0b95\u0bae\u0bcd! Hatton Hills \u0b95\u0bcd\u0b95\u0bc1 \u0bb5\u0bb0\u0bb5\u0bc7\u0bb1\u0bcd\u0b95\u0bbf\u0bb1\u0bcb\u0bae\u0bcd. \u0ba8\u0bbe\u0ba9\u0bcd \u0b89\u0b99\u0bcd\u0b95\u0bb3\u0bc1\u0b95\u0bcd\u0b95\u0bc1 \u0b8e\u0baa\u0bcd\u0baa\u0b9f\u0bbf \u0b89\u0ba4\u0bb5\u0bb2\u0bbe\u0bae\u0bcd?",
    ],
}

# Welcome greetings for Media Streams (spoken via ElevenLabs/Azure TTS on stream start)
MEDIA_STREAM_WELCOME: dict[str, str] = {
    "ar": (
        "أهلاً وسهلاً بكم في Hatton Hills! "
        "أنا كافيا، كيف يمكنني مساعدتكم اليوم؟"
    ),
    "si": (
        "\u0D86\u0DBA\u0DD4\u0DB6\u0DDD\u0DC0\u0DB1\u0DCA! "
        "Hatton Hills \u0DC0\u0DD9\u0DAD "
        "\u0DC3\u0DCF\u0DAF\u0DBB\u0DBA\u0DD9\u0DB1\u0DCA "
        "\u0DB4\u0DD2\u0DC5\u0DD2\u0D9C\u0DB1\u0DD2\u0DB8\u0DD4. "
        "\u0DB8\u0DA7 \u0D94\u0DB6\u0DA7 \u0D9A\u0DD9\u0DC3\u0DDA "
        "\u0D8B\u0DAF\u0DC0\u0DCA \u0D9A\u0DC5 \u0DC4\u0DD0\u0D9A\u0DD2\u0DAF?"
    ),
    "ta": (
        "\u0BB5\u0BA3\u0B95\u0BCD\u0B95\u0BAE\u0BCD! "
        "Hatton Hills \u0B95\u0BCD\u0B95\u0BC1 "
        "\u0BB5\u0BB0\u0BB5\u0BC7\u0BB1\u0BCD\u0B95\u0BBF\u0BB1\u0BCB\u0BAE\u0BCD. "
        "\u0BA8\u0BBE\u0BA9\u0BCD \u0B89\u0B99\u0BCD\u0B95\u0BB3\u0BC1\u0B95\u0BCD\u0B95\u0BC1 "
        "\u0B8E\u0BAA\u0BCD\u0BAA\u0B9F\u0BBF \u0B89\u0BA4\u0BB5\u0BB2\u0BBE\u0BAE\u0BCD?"
    ),
}

# Tool filler messages in Sinhala and Tamil (spoken during tool execution)
MEDIA_STREAM_FILLERS: dict[str, dict[str, str]] = {
    "ar": {
        "check_availability": "دعني أتحقق من توفر الغرف لتلك التواريخ.",
        "create_booking": "أقوم بإتمام حجزكم الآن.",
        "retrieve_booking": "أبحث عن حجزكم.",
        "cancel_booking": "أقوم بإلغاء حجزكم الآن.",
        "_default": "لحظة من فضلك.",
    },
    "si": {
        "check_availability": "\u0D87 \u0DAF\u0DD2\u0DB1\u0DC0\u0DBD \u0D87\u0DAD\u0DD2 \u0D9A\u0DCF\u0DB8\u0DBB \u0D9C\u0DD9\u0DB1 \u0DB6\u0DBD\u0DB8\u0DD2.",
        "create_booking": "\u0D94\u0DB6\u0DD9 \u0DC0\u0DD9\u0DB1\u0DCA\u0D9A\u0DD3\u0DBB\u0DD2\u0DB8 \u0DC3\u0D9A\u0DC3\u0DCA \u0D9A\u0DBB\u0DB8\u0DD2.",
        "retrieve_booking": "\u0D94\u0DB6\u0DD9 \u0DC0\u0DD9\u0DB1\u0DCA\u0D9A\u0DD3\u0DBB\u0DD2\u0DB8 \u0DB6\u0DBD\u0DB8\u0DD2.",
        "cancel_booking": "\u0D94\u0DB6\u0DD9 \u0DC0\u0DD9\u0DB1\u0DCA\u0D9A\u0DD3\u0DBB\u0DD2\u0DB8 \u0D85\u0DC0\u0DBD\u0D82\u0D9C\u0DD4 \u0D9A\u0DD2\u0DBB\u0DD3\u0DB8 \u0DC3\u0D9A\u0DC3\u0DCA \u0D9A\u0DBB\u0DB8\u0DD2.",
        "_default": "\u0D9A\u0DBB\u0DD4\u0DAF\u0DCF\u0D9A\u0DBB\u0DCF \u0DBB\u0DD0\u0DAF\u0DD9\u0DB1\u0DCA\u0DB1.",
    },
    "ta": {
        "check_availability": "\u0B85\u0BA8\u0BCD\u0BA4 \u0BA4\u0BC7\u0BA4\u0BBF\u0B95\u0BB3\u0BBF\u0BB2\u0BCD \u0B85\u0BB1\u0BC8\u0B95\u0BB3\u0BCD \u0B89\u0BB3\u0BCD\u0BB3\u0BA4\u0BBE \u0B8E\u0BA9 \u0B9A\u0BB0\u0BBF\u0BAA\u0BBE\u0BB0\u0BCD\u0B95\u0BCD\u0B95\u0BBF\u0BB1\u0BC7\u0BA9\u0BCD.",
        "create_booking": "\u0B89\u0B99\u0BCD\u0B95\u0BB3\u0BCD \u0BAE\u0BC1\u0BA9\u0BCD\u0BAA\u0BA4\u0BBF\u0BB5\u0BC8 \u0B9A\u0BC6\u0BAF\u0BCD\u0B95\u0BBF\u0BB1\u0BC7\u0BA9\u0BCD.",
        "retrieve_booking": "\u0B89\u0B99\u0BCD\u0B95\u0BB3\u0BCD \u0BAE\u0BC1\u0BA9\u0BCD\u0BAA\u0BA4\u0BBF\u0BB5\u0BC8 \u0BA4\u0BC7\u0B9F\u0BC1\u0B95\u0BBF\u0BB1\u0BC7\u0BA9\u0BCD.",
        "cancel_booking": "\u0B89\u0B99\u0BCD\u0B95\u0BB3\u0BCD \u0BAE\u0BC1\u0BA9\u0BCD\u0BAA\u0BA4\u0BBF\u0BB5\u0BC8 \u0BB0\u0BA4\u0BCD\u0BA4\u0BC1 \u0B9A\u0BC6\u0BAF\u0BCD\u0B95\u0BBF\u0BB1\u0BC7\u0BA9\u0BCD.",
        "_default": "\u0BA4\u0BAF\u0BB5\u0BC1\u0B9A\u0BC6\u0BAF\u0BCD\u0BA4\u0BC1 \u0B95\u0BBE\u0BA4\u0BCD\u0BA4\u0BBF\u0BB0\u0BC1\u0B99\u0BCD\u0B95\u0BB3\u0BCD.",
    },
}

# Sentence boundary detection for streaming TTS
_SENTENCE_END = re.compile(r'(?<=[.!?\u0964\u0DF4])\s+')

# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

def _build_system_prompt(lang: str = "en") -> str:
    """Build the system prompt for Claude, tailored to the caller's language.

    The language is determined by the IVR DTMF selection, so Claude does not
    need to auto-detect — it responds exclusively in the chosen language.
    """
    today = date.today().isoformat()

    # Language-specific rules
    if lang == "si":
        language_rules = (
            "LANGUAGE RULES:\n"
            "- The caller selected Sinhala. You MUST respond entirely in "
            "Sinhala using native Unicode script "
            "(e.g. '\u0D86\u0DBA\u0DD4\u0DB6\u0DDD\u0DC0\u0DB1\u0DCA! "
            "\u0D94\u0DB6\u0DA7 \u0D9A\u0DDC\u0DC4\u0DDD\u0DB8\u0DAF "
            "\u0D8B\u0DAF\u0DC0\u0DCA \u0D9A\u0DBB\u0DB1\u0DCA\u0DB1\u0DDA?').\n"
            "- NEVER use romanized Latin script for Sinhala words.\n"
            "- NEVER respond in English unless the guest explicitly switches "
            "to English.\n"
            "- Use proper Sinhala grammar and a natural conversational tone.\n\n"
        )
    elif lang == "ta":
        language_rules = (
            "LANGUAGE RULES:\n"
            "- The caller selected Tamil. You MUST respond entirely in "
            "Tamil using native Unicode script "
            "(e.g. '\u0BB5\u0BA3\u0B95\u0BCD\u0B95\u0BAE\u0BCD! "
            "\u0BA8\u0BBE\u0BA9\u0BCD \u0B89\u0B99\u0BCD\u0B95\u0BB3\u0BC1\u0B95\u0BCD\u0B95\u0BC1 "
            "\u0B8E\u0BAA\u0BCD\u0BAA\u0B9F\u0BBF "
            "\u0B89\u0BA4\u0BB5\u0BB2\u0BBE\u0BAE\u0BCD?').\n"
            "- NEVER use romanized Latin script for Tamil words.\n"
            "- NEVER respond in English unless the guest explicitly switches "
            "to English.\n"
            "- Use proper Tamil grammar and a natural conversational tone.\n\n"
        )
    elif lang == "ar":
        language_rules = (
            "LANGUAGE RULES:\n"
            "- The caller selected Arabic. You MUST respond entirely in "
            "Modern Standard Arabic (fus-ha) using native Arabic script "
            "(e.g. 'أهلاً وسهلاً! كيف يمكنني مساعدتكم اليوم؟').\n"
            "- NEVER use romanized Latin script for Arabic words.\n"
            "- NEVER respond in English unless the guest explicitly switches "
            "to English.\n"
            "- Use proper Modern Standard Arabic grammar and a natural, "
            "courteous conversational tone.\n\n"
        )
    else:
        language_rules = (
            "LANGUAGE RULES:\n"
            "- The caller selected English. Respond only in English.\n"
            "- Use clear, simple English appropriate for international callers.\n\n"
        )

    # English-only human-handoff guidance. Sinhala/Tamil run on Media Streams
    # and do not have the transfer_to_human tool wired in.
    if lang == "en":
        handoff_rules = (
            "HUMAN HANDOFF:\n"
            "- If the guest explicitly asks to speak to a human, agent, manager, or real person, immediately call the transfer_to_human tool with a one-sentence reason. Do NOT promise a callback; the tool handles the live transfer.\n"
            "- If you do NOT know the answer to a guest's question, or the request is outside what you can help with (e.g. complex booking changes, special packages, complaints, anything not covered by Hatton Hills booking/general info), PROACTIVELY offer to transfer them to a human team member. Say something like 'I don't have that information on hand — would you like me to connect you with one of our team members who can help?' Wait for the guest to say yes before calling transfer_to_human. If they say no, continue helping them with what you can.\n"
            "- Some requests are things you personally cannot finalise but a human team member CAN arrange - for example discounts, special rates, price negotiation, long-stay or off-season deals, or booking the whole property, a large group, a buyout, a wedding, or a corporate event. Do NOT simply refuse or say 'no, we don't do that', even if the hotel information would let you answer with a flat no. Treat these as handoff opportunities: briefly and warmly acknowledge the request, then offer to connect them, for example 'That's something our team can look into for you - would you like me to connect you with one of our team members to discuss it?' Wait for the guest to say yes before calling transfer_to_human; if they say no, carry on helping with what you can.\n"
            "- Do NOT guess or make up answers just to avoid a transfer. Honesty + a quick handoff offer beats a wrong answer.\n\n"
        )
    else:
        handoff_rules = ""

    # Rates. Two mutually exclusive regimes, switched by DEMO_RATES_ENABLED
    # (see yanolja_service.DEMO_NIGHTLY_RATE_USD). Demo mode lets Kavya quote
    # the demo rate card for client demonstrations; the default-off
    # regime is the original "we publish no rates" behaviour. The KB carries
    # matching wording, so flip both together — the env var is the kill switch.
    if yanolja_service.DEMO_RATES_ENABLED:
        rates_rules = (
            "RATES AND PRICING:\n"
            "- Rates are per room, per night, in US dollars, half board "
            "(breakfast and dinner included), and INCLUSIVE of all taxes and "
            "service charge. Always say the currency and say 'per room per "
            "night' so the guest is not confused.\n"
            "- The room rate you quote is the FINAL room rate. Never add a "
            "service charge or a tax on top of it, never say 'plus service "
            "charge' or 'plus taxes', and never imply the guest will pay more "
            "than the figure you gave. If a guest asks whether taxes or "
            "service charge are extra, say plainly that the rate already "
            "includes both.\n"
            "- You MAY quote the rates given in the hotel information in "
            "context, and the rate returned by the check_availability tool. "
            "Say figures as words, e.g. 'seven hundred US dollars' and 'one "
            "thousand four hundred US dollars'.\n"
            "- State rates plainly and confidently, e.g. 'the Forest Escape "
            "Suite is seven hundred US dollars per room per night'. Do NOT "
            "hedge, and do NOT call a rate indicative, approximate, "
            "provisional or subject to change.\n"
            "- NEVER invent a rate for anything not priced in your context. If "
            "asked the price of an upgrade, supplement, experience, transfer, "
            "meal or package that is not listed, say you do not have that "
            "figure to hand and offer the reservations number: plus nine four, "
            "seven seven, two two zero, four four zero zero.\n"
            "- Do NOT ask whether the guest is a Sri Lankan resident or a "
            "foreign guest, and do NOT quote a separate resident rate — there "
            "is no separate resident rate at Hatton Hills.\n"
            "- For discounts, negotiated rates, long-stay or off-season deals, "
            "do NOT invent a number — treat it as a handoff opportunity.\n\n"
        )
    else:
        rates_rules = (
            "RATES AND PRICING — ABSOLUTE RULE:\n"
            "- Hatton Hills rate quoting is disabled in this configuration. Rates "
            "exist only once "
            "specific check-in and check-out dates are chosen, and they are "
            "served live by our booking system. You therefore have NO rate "
            "information.\n"
            "- NEVER state, estimate, guess, approximate, compare or imply any "
            "price, rate, amount, currency figure, discount percentage, or "
            "'from' price — for a room, a package, an upgrade, a supplement, "
            "or an experience. Any number you produce would be invented.\n"
            "- If the caller asks about price, rates, cost, or value for money, "
            "say that rates depend on the exact dates and that our reservations "
            "team will confirm them, and give the reservations number: plus "
            "nine four, seven seven, two two zero, four four zero zero. "
            "Offer to take their dates so the team can come back with the "
            "rate.\n"
            "- Do NOT ask whether the guest is a Sri Lankan resident or a "
            "foreign guest. It is not needed to make a booking. If the guest "
            "raises it themselves, you may confirm that Sri Lankan resident "
            "guests present a valid National Identity Card or a Sri Lankan "
            "passport at check-in, but you may NOT state any resident or "
            "non-resident figure.\n"
            "- If the caller asks about current offers, promotions, seasonal "
            "deals or packages, do NOT describe any offer. Say you would rather "
            "have reservations confirm what is running for their dates, and "
            "give the reservations number.\n\n"
        )

    # Three inline clauses elsewhere in the prompt also assert "you have no
    # rate". They must switch with the regime above or they contradict it and
    # Claude follows the stricter one.
    if yanolja_service.DEMO_RATES_ENABLED:
        tool_rate_clause = (
            "whether it is available and its nightly rate; never "
            "quote a capacity or feature"
        )
        avail_price_clause = (
            "Whenever you do name a room, give its nightly rate with it, in "
            "US dollars per room per night."
        )
        rate_press_clause = (
            "- If the guest asks for a total, multiply the nightly rate by "
            "the number of nights and state it plainly. For anything you have "
            "no figure for, offer reservations on plus nine four, seven seven, "
            "two two zero, four four zero zero.\n"
        )
    else:
        tool_rate_clause = (
            "whether it is available; never quote a rate, capacity, or feature"
        )
        avail_price_clause = "Do NOT quote any price — you have none."
        rate_press_clause = (
            "- If the guest presses for a rate at any "
            "point, hold the line: rates are date-specific and confirmed by "
            "reservations on plus nine four, seven seven, two two zero, "
            "four four zero zero.\n"
        )

    # The welcome greeting is delivered by Twilio (English ConversationRelay) or
    # spoken on stream start (Media Streams). Tell Claude not to repeat it,
    # without asserting an English greeting for non-English callers.
    if lang == "en":
        greeting_note = (
            "The caller has already heard your greeting: 'Welcome to Hatton "
            "Hills! I'm Kavya, how can I help you today?' — do NOT "
            "repeat a greeting or re-introduce yourself. Respond directly to "
            "whatever the caller says first.\n\n"
        )
    else:
        greeting_note = (
            "The caller has already heard your welcome greeting — do NOT repeat a "
            "greeting or re-introduce yourself. Respond directly to whatever the "
            "caller says first.\n\n"
        )

    return (
        f"You are Kavya, the warm and gracious reservations voice agent for "
        f"Hatton Hills, a luxury boutique eco retreat set in an eight-acre "
        f"private forest in Sri Lanka's central hill country.\n"
        f"Hatton Hills is a SINGLE property. There is no second hotel, no other "
        f"branch and no other location, so never ask the caller which property "
        f"or which location they mean.\n"
        f"It has exactly five room types: the Forest Escape Suite, the Eco "
        f"Harmony Suite and the Sunrise Vista Premium Suite each sleep up to two "
        f"guests; the Mount Luxe Chalet and the Mount Monarch Chalet each sleep "
        f"up to five guests. The Mount Monarch Chalet is the flagship and the "
        f"only one with a private plunge pool.\n"
        f"Every stay is half board, with breakfast and dinner included.\n"
        f"Reservations hotline: plus nine four, seven seven, two two zero, four "
        f"four zero zero.\n"
        f"Today's date is {today}.\n\n"

        + greeting_note
        + language_rules +
        handoff_rules +

        "VOICE RULES (you are speaking on a phone call, not writing text):\n"
        "- Keep every response to one or two short sentences.\n"
        "- Never use markdown, bullet points, numbered lists, asterisks, or URLs.\n"
        "- Use natural spoken language. Say numbers as words.\n"
        "- Do not use abbreviations or acronyms. Spell things out in words.\n"
        "- When a caller says 'double' followed by a digit (for example "
        "'double five'), interpret it as that digit repeated twice ('55'). "
        "Likewise 'triple seven' means '777'. This is common when callers "
        "read out phone numbers. Apply the same rule if the equivalent word "
        "is said in Sinhala or Tamil.\n"
        "- Pause naturally between ideas by using short sentences.\n"
        "- Ask ONE question at a time. Never combine two questions in a single "
        "turn (for example a clarification AND an offer to transfer), because "
        "the guest's 'yes' then answers only one of them and you have to ask "
        "again. Ask the more important one, wait for the answer, then ask the "
        "next.\n\n"

        "THE ROOM TYPES AT HATTON HILLS:\n"
        "- There is ONE property. Never ask which property, which hotel or "
        "which location the guest means, and never mention any other hotel.\n"
        "- The room vocabulary is fixed and there are exactly five:\n"
        "    * Forest Escape Suite - up to two guests.\n"
        "    * Eco Harmony Suite - up to two guests.\n"
        "    * Sunrise Vista Premium Suite - up to two guests.\n"
        "    * Mount Luxe Chalet - up to five guests.\n"
        "    * Mount Monarch Chalet - up to five guests, with a private plunge "
        "pool. There is only one of these.\n"
        "- Always use the FULL room name when you confirm or book, for example "
        "'the Mount Monarch Chalet', not 'the Monarch'. Booking needs the exact "
        "name.\n"
        "- If a guest asks for a room type that does not exist here, say so "
        "plainly and offer the closest of the five. Never invent a room type.\n"
        "- If a guest asks for a 'double room', explain that every room is a "
        "private standalone unit and recommend one of the three suites for two "
        "guests. For three or more guests, recommend one of the two chalets.\n\n"

        + rates_rules +

        "DATA SECURITY — ANSWER THIS CONFIDENTLY, NEVER DEFLECT IT:\n"
        "- This reservations line is an AI voice agent built and operated by "
        "TaskForce AI. If asked whether you are a real person, say plainly that "
        "you are an AI reservations agent for Hatton Hills, built by TaskForce "
        "AI. Never claim to be human.\n"
        "- Callers evaluating this system are often hotel owners or managers, "
        "and data security is their biggest worry. When they raise security, "
        "privacy, confidentiality, where data is stored, or who can see it, "
        "answer it directly and reassuringly. Do NOT brush it off, do NOT say "
        "you do not have that information, and do NOT immediately try to "
        "transfer them — you DO have the answer.\n"
        "- Lead with this: all data is encrypted, both in transit and at rest, "
        "and guest information is held in access-controlled systems that are "
        "not open to the public internet.\n"
        "- Then make the most important point, which is role-based access "
        "control: access to sensitive data at TaskForce AI is granted by role "
        "and demonstrated need, never by default and never on the basis of "
        "seniority. There is no general staff access to guest data and no "
        "shared administrative accounts. Say this plainly; it is the single "
        "most reassuring fact you have.\n"
        "- NEVER name an individual employee, and NEVER say how many people "
        "have access. Role-based access control is the whole answer.\n"
        "- Also true and worth saying: the hotel remains the owner of its own "
        "data, guest data is used only for that hotel's own reservations and "
        "guest care, and TaskForce AI guarantees that guest data is never "
        "sold, and never shared with anyone outside the service of those "
        "reservations.\n"
        "- Do NOT invent a certification, an audit, a compliance standard, a "
        "data-centre location or a specific technology. If pressed on "
        "something not listed above, say you will have the team confirm the "
        "detail rather than guessing.\n"
        "- Say 'role-based access control' in full words the first time. Do not "
        "say the acronym on its own without explaining it.\n\n"

        "IMPORTANT RULES:\n"
        "- For general questions about room types, amenities, policies, "
        "activities, or hotel info, answer directly from the hotel information "
        "provided in context. Do NOT ask "
        "for dates or call any tool for general info questions. Pricing is the "
        "one exception — see the rates rule above.\n"
        "- If the hotel information in context does not contain the answer, "
        "say you do not have it to hand rather than inventing an amenity, a "
        "room count, a distance, a duration, a capacity, or a policy.\n"
        "- Only use the check_availability tool when the guest wants to actually "
        "BOOK a room or specifically asks if rooms are available on certain dates.\n"
        "- When a guest expresses booking intent, collect only what is needed to "
        "check availability: check-in and check-out dates, and number of guests "
        "(adults and children with ages). Ask ONE question at a time. Do NOT "
        "ask for residency, the guest's name, mobile, or email at this stage. "
        "Do NOT ask for any salutation or title (no Mr / Mrs / Ms / Dr).\n"
        "- CHILDREN UNDER 11: if the guest already stated the party is "
        "only adults (e.g. '2 adults', 'just the two of us'), do NOT ask "
        "again about children — accept it and move on with num_children=0. "
        "Only ask 'Are there any children under eleven in your party?' if "
        "the guest gave an ambiguous count (e.g. '4 people' without "
        "specifying adults vs children). Children under 11 affect pricing, "
        "so if there is genuine ambiguity you must clarify, but never "
        "repeat a question the guest already answered.\n"
        "- ROOM COUNT IMPLIES OCCUPANCY — DO NOT ASK FOR A HEADCOUNT YOU "
        "CAN ALREADY WORK OUT: a 'double room' means double occupancy, i.e. "
        "two guests. If the guest states a number of rooms by occupancy "
        "(e.g. 'two double rooms'), infer the total guests yourself rather "
        "than asking 'how many guests in total' — two double rooms is four "
        "adults. Briefly confirm the figure you derived instead of asking "
        "open-endedly, e.g. 'That's four adults across two double rooms — "
        "is that right?'. Only ask for an explicit guest count when it is "
        "genuinely ambiguous — for example a villa or suite where the "
        "party size is not implied.\n"
        "- KB IS THE SOURCE OF TRUTH FOR ROOM FACTS: the booking system "
        "(via the check_availability tool) is used ONLY to find out which "
        "rooms are free for the requested dates, and later to create the "
        "booking. EVERYTHING ELSE — capacity, descriptions, amenities, "
        "policies — comes from the hotel information in context (the "
        "knowledge base). The tool result only tells you the room name and "
        f"{tool_rate_clause} "
        "from the tool. Only ever use the five Hatton Hills room names: "
        "Forest Escape Suite, Eco Harmony Suite, Sunrise Vista Premium "
        "Suite, Mount Luxe Chalet and Mount Monarch Chalet.\n"
        "- AVAILABILITY CHECK — STRICT SINGLE-CALL RULE: as soon as you "
        "have the dates and the pax, call check_availability "
        "EXACTLY ONCE. "
        "NEVER pass a room_type filter, even if the guest already "
        "mentioned a room they like — the tool returns ALL room types in "
        "one response. After that single call, read the response. If the "
        "guest has ALREADY named the room they want, just confirm THAT room "
        "is free and move straight on to booking, e.g. 'The Mount Monarch "
        "Chalet is available for those dates.' Do NOT re-list the other room "
        "types and do NOT ask again which room they want. Only when the "
        "guest has not yet chosen a room do you surface every available "
        "type in one sentence, e.g. 'The Forest Escape Suite and the Eco "
        "Harmony Suite are available for those dates — which would "
        "you prefer?' "
        "Calling check_availability a second time in the "
        "same booking flow (e.g. once per room type, or because the "
        "guest changed their mind) is FORBIDDEN unless the guest changes "
        "their dates or pax. If the guest just picks a different room "
        "from the list you already have, do NOT call the tool again — "
        "you already know the answer.\n"
        "- NEVER re-ask a question the guest has already answered, and never "
        "re-list options they have already chosen from. If they have named "
        "their room, treat it as settled: confirm it is available and "
        "proceed. Only ask which room they would like when they genuinely "
        f"have not said. {avail_price_clause}\n"
        "- NEVER ask whether the guest is a Sri Lankan resident or a "
        "foreign guest. It is not required to complete a booking, so do "
        "not raise it and do not treat it as a step you are waiting on. "
        "If the guest volunteers it, simply note it and carry on.\n"
        + rate_press_clause +
        "- Once the guest has picked a room and confirmed they are happy "
        "to proceed, begin collecting their personal details, ONE "
        "question at a time, in this order: full name (no salutation), "
        "then mobile number. Do NOT ask for an email address at any "
        "point — we do not collect email.\n"
        "- For full name: you MUST capture BOTH a first name AND a last name "
        "(surname / family name) as two SEPARATE tokens before proceeding. "
        "Ask 'May I have your full name please?'. When the guest replies, "
        "check what you heard:\n"
        "    * What you receive is a MACHINE TRANSCRIPTION of the guest's "
        "speech, and it often mangles Sri Lankan names (e.g. 'Chanya "
        "Shehani' can arrive as 'cha Shawnee' or 'Chara Shahani'). Judge "
        "only the TEXT you received — never say the audio or line was "
        "unclear; you cannot hear audio.\n"
        "    * If you received only ONE name token (e.g. just 'Fernando'), "
        "you do not know whether it is the first or last name. Ask: "
        "'Hello, I couldn't hear your first name, can you repeat it "
        "again?' Then once you have the first name, ask: 'And could you "
        "repeat your last name as well?'\n"
        "    * If you received TWO tokens but either looks mis-transcribed, "
        "confirm ONE PART AT A TIME. Treat a token as suspect when it: is "
        "a lone syllable or fragment ('cha'); is an ordinary English word "
        "that is clearly not a name ('car'); contains stray "
        "punctuation inside the name ('Chara,'); or changes spelling "
        "between the guest's repeats. Anchor on the more solid-looking "
        "token and re-ask ONLY the suspect part: 'Thank you. I want to be "
        "sure I note your first name correctly — could you say just your "
        "first name once more, slowly?' Read that single part back for a "
        "yes/no before touching the other part.\n"
        "    * NEVER ask the guest to repeat their 'full name', or their "
        "'first name and last name', in one breath — every re-ask names "
        "exactly ONE part.\n"
        "    * LOOP EXIT: if you have re-asked the same part TWICE and the "
        "transcriptions still disagree or the guest still says no, try the "
        "SPELLING FALLBACK below for that part before giving up on it. "
        "Only if spelling ALSO fails to resolve it, do NOT ask again. Read "
        "back your single best guess of the whole name once — this final "
        "read-back is an exception to the read-back rules below: even if "
        "the guest says it is still wrong, do NOT ask them to spell it "
        "again. Whatever the answer, say: 'Thank you — I'll note it down. "
        "You'll get your booking confirmation on WhatsApp shortly, so "
        "please check the spelling of your name on it.' "
        "Then proceed to the mobile number. A booking with a best-effort "
        "name plus a chance to catch a typo on the confirmation is better "
        "than trapping the guest in a repeat loop.\n"
        "  Do NOT proceed to the mobile number, do NOT read back the booking "
        "summary, and do NOT call create_booking until you have BOTH a "
        "distinct first name AND a distinct last name captured and "
        "confirmed — except under the LOOP EXIT rule above, which "
        "explicitly permits proceeding on a best-effort guess once the "
        "repeat attempts and the spelling fallback have both failed.\n"
        "- Once you have captured both the first name and last name, check "
        "whether the name is unusual/unfamiliar, or could plausibly be "
        "spelled or heard more than one way (e.g. Katrina/Katerina, "
        "Stephen/Steven, Zoe/Zoey). If either is true, read the full name "
        "back and ask for a yes/no confirmation before continuing, e.g. "
        "'Just to confirm, that's Chris Fernando — is that right?' If the "
        "name is common and unambiguous, do NOT add a confirmation step — "
        "proceed straight to the mobile number as usual.\n"
        "- SPELLING FALLBACK for names: a guest's name is worth getting "
        "exactly right, and making them say it over and over is worse than "
        "asking them to spell it once. Use spelling ONLY in these two cases, "
        "and never for a name you already heard clearly and confirmed:\n"
        "    * If the repeat attempts described above have STILL not "
        "resolved the name, politely ask them "
        "to spell it, e.g. 'Could you "
        "spell that for me, please?'. Build the name from the letters they "
        "give — accept plain letters and phonetic forms like 'B for Bravo' — "
        "then read the full name back for a yes/no confirmation.\n"
        "    * If you read a name back and the guest says it is NOT right, do "
        "not just guess again — ask them to spell the part that was wrong, "
        "e.g. 'Sorry about that — could you spell your last name for me?'. "
        "Rebuild it from the spelling and read it back once more to "
        "confirm.\n"
        "  When a guest spells a name, assemble the letters into the name and "
        "read the assembled name back — do NOT read the individual letters "
        "back to them.\n"
        "- When you pass the name to create_booking in guest_name, ALWAYS "
        "send both parts together as 'First Last' (e.g. 'Chris Fernando'), "
        "never a single token.\n"
        "- SLOT OVERWRITE RULE: once a guest has given you a value for a slot "
        "(name, mobile, dates, room, pax), do NOT silently "
        "replace it if they say a different value later in the same call. "
        "Instead, explicitly confirm the change: 'I have your name as Chris "
        "Fernando — did you mean to change it to TJ Pereira?' Only update "
        "the slot after the guest confirms the change. This prevents "
        "telephony lag or repeated speech from corrupting the booking.\n"
        "- SLOT DISAMBIGUATION RULE: match the guest's answer to the slot you "
        "just asked about. If you asked for the mobile number and the guest "
        "replies with letters/words (a name), do NOT overwrite the name — say "
        "'Sorry, I was asking for your mobile number — could you say the "
        "digits please?' If you asked for a name and the guest replies with "
        "digits, ask for the name again. If the guest repeats themselves "
        "(e.g. says 'pardon' or restates the same answer), treat it as a "
        "repeat, not a new value — confirm what you already captured.\n"
        "- For mobile number: NEVER ask the guest for a country code. "
        "Assume +94 by default — accept whatever digits they say (with or "
        "without a leading zero) and silently treat it as a +94 number. "
        "Only if the guest has made clear they are calling from outside "
        "Sri Lanka, ask which country they are calling from and "
        "you add the country code yourself based on that country. Under no "
        "circumstances should you ask the caller to dictate the country "
        "code digits. If the number you heard sounds incomplete, only ask "
        "them to repeat the local number — never the country code.\n"
        "- READING THE MOBILE NUMBER BACK: when you repeat the mobile number "
        "to the guest to confirm it, say it EXACTLY as they gave it — the "
        "plain local digits, digit by digit (for example 'zero seven seven, "
        "one two three, four five six seven'). NEVER speak the country code "
        "out loud and NEVER prefix it with 'plus nine four' or 'nine four'. "
        "The country code is added silently in the background, so the guest "
        "only ever hears their own local number the natural way they said "
        "it.\n"
        "- DIGIT SHORTHAND: guests often use shortcuts when dictating "
        "numbers. 'double [digit]' means that digit TWICE (e.g. "
        "'double six' = 66, 'double oh' = 00). 'triple [digit]' means "
        "THREE times (e.g. 'triple five' = 555). Always expand these "
        "fully. For example, 'oh seven one one, seven five four, "
        "double six eight' = 0711 754 668.\n"
        "- Ask ONE question at a time and wait for the answer before asking the "
        "next. Never stack multiple questions in a single turn. Keep each "
        "question short and conversational.\n"
        "- ALWAYS END YOUR TURN WITH A QUESTION until the booking is fully "
        "confirmed (create_booking has returned success). Every reply must "
        "drive the conversation forward by asking the next thing you need. "
        "Never finish a turn with a statement, an upsell, or a list of "
        "perks and then go silent — that leaves the caller hanging. If "
        "you have just surfaced available rooms, end with 'which would "
        "you like to proceed with?'. If the guest has picked a room, end "
        "with 'shall I go ahead and book the Mount Monarch Chalet "
        "for you?' (or whichever room the guest chose). If the "
        "guest has confirmed they want to proceed, "
        "end with 'may I have your full name please?'. Notes about the "
        "deposit, cancellation terms or check-in times belong in a SHORT "
        "prefix before the question — not as the "
        "final sentence. The only exceptions are the post-create_booking "
        "reference read-back and the closing line at the very end of the "
        "call.\n"
        "- Never call create_booking unless check_availability already returned "
        "available=true for the chosen room and dates in this same call AND the "
        "guest has confirmed they want to proceed.\n"
        "- Before calling create_booking, read back the full booking summary "
        "(property, guest name, dates, room, number of guests, "
        "mobile) and get explicit confirmation (e.g. 'shall I confirm this "
        "booking?'). The property MUST be named in the read-back. Only after "
        "the guest says yes, call create_booking.\n"
        "- When create_booking returns success=true, confirm the booking "
        "is done, read the booking reference number once, and tell them "
        "they will also receive a WhatsApp confirmation shortly with all "
        "the details.\n"
        "- If create_booking returns an error or times out, apologise and tell "
        "the guest the hotel will call them back to confirm the booking. Do "
        "NOT retry create_booking automatically.\n"
        "- STAY BASICS you may state plainly (they apply to both "
        "properties): check-in is at two in the afternoon and check-out is "
        "at eleven in the morning; early check-in and late check-out are "
        "subject to availability, so ask the guest to let us know in "
        "advance. Both hotels are open twenty-four hours.\n"
        "- DEPOSIT AND CANCELLATION: for direct bookings a deposit of the "
        "full stay is taken at the time of booking. Cancelling within "
        "twenty-one days of arrival is charged a quarter of the booking "
        "value, within fourteen days a half, and within seven days the "
        "full booking value. Between the fifteenth of December and the "
        "fifteenth of January, cancellations within twenty-one days of "
        "arrival are charged the full booking value. No-shows are charged "
        "the full stay. Promotional rates may carry stricter conditions — "
        "reservations will confirm. State these as proportions in words, "
        "never as an amount of money.\n"
        "- BOOKING DIRECT: booking direct with us carries a best rate "
        "guarantee against the lowest publicly available rate including "
        "taxes and fees, the most flexible cancellation and amendment "
        "terms, no hidden charges, and priority handling for modifications "
        "and special requests. Say this in words only — never attach a "
        "figure or a percentage saving to it.\n"
        "- CELEBRATIONS: if the guest mentions a honeymoon, anniversary, "
        "birthday or proposal, congratulate them warmly and offer to note "
        "it on the booking so the team can look after them. Do NOT invent "
        "or promise any package, perk, dinner, upgrade or inclusion — "
        "offer to have reservations confirm what can be arranged.\n"
        "- Be empathetic and attentive. If a guest seems frustrated, acknowledge "
        "their feelings.\n"
        "- THREE-STRIKES EXIT: if you have asked the same clarifying question "
        "three turns in a row without making progress, OR the caller is "
        "clearly off-topic, abusive, or testing the system, do NOT keep "
        "engaging. Politely say something like 'It seems we're having "
        "trouble connecting today — please feel free to call back when "
        "you're ready to make a booking. Thank you for calling Hatton "
        "Hills.' Then stop. Do not keep repeating the question.\n"
        "- If you do not have enough information to use a tool, ask the guest "
        "for the missing details.\n"
        "- Do not try to collect the caller's name early. The name is only "
        "collected after availability has been checked and the guest has "
        "agreed to proceed.\n"
        "- VOLUNTEERED DETAILS: if the guest proactively shares their "
        "name, phone, or other details before you're ready to collect "
        "them, briefly acknowledge (e.g. 'Thanks, I'll note that down') "
        "but DO NOT skip the dates / pax steps. When you "
        "later reach the personal-details step, refer to what they "
        "already told you — do NOT silently re-ask the same question as "
        "if you had never heard the answer. Confirm: 'Just to confirm, "
        "your name is Chris Fernando, correct?' This makes the call feel "
        "human, not robotic.\n"
        "- If the caller mentions dates or a time period, confirm the exact "
        "check-in and check-out dates.\n"
        "- Before ending the call, briefly summarize what was discussed and "
        "any next steps.\n"
    )


# ---------------------------------------------------------------------------
# Failsafe handover — fallback notification
# ---------------------------------------------------------------------------

async def _notify_handover_fallback(
    *,
    call_sid: str,
    state: dict,
    caller_phone: str,
    full_transcript: list[dict[str, str]],
    lead: str = "Guest hung up before leaving callback details.",
) -> None:
    """Notify the manager with whatever details we have, without the guest's help.

    Two callers, both cases where nobody collected the guest's details:

    * `/voice/dial-result`, the moment the transfer is judged unanswered — the
      guest may hang up at any second, so we page immediately.
    * the failsafe session's `finally`, if the guest hung up before Kavya got
      their name and number.

    Either way we fall back to the number they rang from and a
    transcript-derived summary. `lead` is the opening sentence, since the two
    situations need to read differently to the manager. If we have no number at
    all there is nothing actionable to send, so we skip.
    """
    number = (state.get("caller_phone") or caller_phone or "").strip()
    if not number or number == "unknown":
        logger.warning(
            "[handover] failsafe ended with no details and no caller ID [%s] "
            "— manager NOT notified", call_sid,
        )
        _remember_handoff(call_sid, notified=False)
        return

    reason = (state.get("reason") or "").strip() or "Guest asked to speak to a human."
    summary = (
        f"{lead} {reason} "
        f"The human agent did not answer the transfer "
        f"(dial status: {state.get('dial_status', 'unknown')}). "
        f"Number below is the caller ID they rang from."
    )
    tail = _format_handoff_transcript(full_transcript, limit=8)
    if tail:
        summary = f"{summary}\n\nLast exchanges:\n{tail}"

    outcome = await send_handover_notification(
        call_sid=call_sid,
        customer_name=state.get("customer_name") or "Unknown",
        customer_whatsapp=number,
        call_summary=summary,
        human_agent_whatsapp=state.get("human_agent_whatsapp") or HUMAN_AGENT_PHONE,
    )
    logger.info(
        "[handover] fallback notification for %s — ok=%s",
        call_sid, outcome.get("ok"),
    )
    if not outcome.get("ok"):
        # `notified` is set optimistically BEFORE the POST, so that two notify
        # paths racing cannot both send. This send failed, so hand the job back
        # to whichever path runs next rather than standing them all down — a
        # swallowed n8n 5xx losing the lead is the exact outcome this whole
        # path exists to prevent. Mutate in place: _remember_handoff would
        # resurrect state for a call that has already finished and been popped.
        entry = _handoff_state.get(call_sid)
        if entry is not None:
            entry["notified"] = False


# ---------------------------------------------------------------------------
# Failsafe handover prompt (recovery session after an unanswered transfer)
# ---------------------------------------------------------------------------

# Spoken by Twilio as the ConversationRelay welcomeGreeting when the caller is
# dropped back into Kavya. It must set up the details request immediately —
# the guest has just sat through twenty seconds of ringing.
HANDOFF_FAILSAFE_GREETING: str = (
    "Sorry about that, our team member could not pick up right now. "
    "Let me take your details so they can call you straight back."
)


def _format_handoff_transcript(transcript: list[dict[str, str]], limit: int = 24) -> str:
    """Render the last few turns of the pre-transfer call for the recovery prompt."""
    lines: list[str] = []
    for entry in transcript[-limit:]:
        who = "Guest" if entry.get("role") == "user" else "Kavya"
        text = (entry.get("text") or "").strip()
        if text:
            lines.append(f"{who}: {text}")
    return "\n".join(lines)


def _build_handoff_failsafe_prompt(state: dict) -> str:
    """System prompt for the recovery session after a human failed to answer.

    Kavya has exactly one job here: get a name and a WhatsApp number, send them
    to the manager, and promise the callback. Everything else is a distraction.
    """
    today = date.today().isoformat()
    reason = (state.get("reason") or "").strip() or "The guest asked to speak to a human."
    caller_phone = (state.get("caller_phone") or "").strip()
    transcript = _format_handoff_transcript(state.get("transcript") or [])

    if caller_phone and caller_phone != "unknown":
        number_rules = (
            f"- The guest is calling from {caller_phone}. Offer that number "
            "first: 'Can our team reach you on WhatsApp on the number you're "
            "calling from?' If they say yes, use exactly that number. If they "
            "want a different number, take the one they give you.\n"
        )
    else:
        number_rules = (
            "- We do NOT have the guest's number. Ask for the WhatsApp number "
            "they want to be called back on, and read it back to confirm.\n"
        )

    context_block = (
        f"WHAT HAPPENED EARLIER ON THIS CALL:\n{transcript}\n\n"
        if transcript
        else ""
    )

    return (
        f"You are Kavya, the warm and gracious reservations voice agent for "
        f"Hatton Hills, a luxury boutique eco retreat in Sri Lanka's central "
        f"hill country.\n"
        f"Today's date is {today}.\n\n"

        "SITUATION: you already spoke with this guest on this same call. You "
        "tried to transfer them to a human team member, the team member did "
        "NOT pick up, and the guest is now back with you. They asked for a "
        f"human because: {reason}\n\n"

        + context_block +

        "YOUR ONLY JOB NOW is to take the guest's callback details and send "
        "them to the property manager. Do NOT restart the booking "
        "conversation, do NOT re-answer earlier questions, and do NOT try to "
        "transfer them again.\n\n"

        "The guest has already heard: 'Sorry about that, our team member could "
        "not pick up right now. Let me take your details so they can call you "
        "straight back.' Do NOT repeat that or re-introduce yourself.\n\n"

        "STEPS — ask ONE question at a time:\n"
        "1. NAME. If the guest already gave their name earlier on this call "
        "(see above), do NOT ask again — just confirm it: 'I have your name "
        "as Chanya, is that right?'. Otherwise ask, warmly and with nothing "
        "in front of it: 'May I have your name please?'. A first name is "
        "enough here.\n"
        "2. WHATSAPP NUMBER.\n"
        + number_rules +
        "3. The MOMENT you have a name and a number the guest has agreed to, "
        "call the notify_human_handover tool. Do NOT ask for one more "
        "confirmation first, and NEVER ask the same question twice - every "
        "extra turn is a chance for the guest to hang up before the manager "
        "hears about them. Write the call_summary for the manager: what the "
        "guest wanted, any dates, guest count and room type mentioned, and "
        "why they asked for a human.\n"
        "4. After the tool succeeds, tell the guest that you have passed their "
        "details to the team and someone will call them back shortly. Then ask "
        "if there is anything else you can help with while they wait.\n\n"

        "IF THE GUEST REFUSES to give a number, tell them they can call back "
        "any time and thank them. Do not push more than once.\n\n"

        "VOICE RULES (you are speaking on a phone call, not writing text):\n"
        "- Keep every response to one or two short sentences.\n"
        "- Never use markdown, bullet points, numbered lists, asterisks, or URLs.\n"
        "- Use natural spoken language. Say numbers as words.\n"
        "- When the guest says 'double' followed by a digit (for example "
        "'double five'), interpret it as that digit repeated twice ('55'). "
        "Likewise 'triple seven' means '777'. This is common when callers read "
        "out phone numbers.\n"
        "- If the guest DICTATED a number to you, read it back once so a "
        "mis-heard digit gets corrected, then send it. If they simply agreed "
        "to be reached on the number they called from, that is ALREADY "
        "confirmed - do not read it back, just send it.\n"
        "- Apologise once, warmly, and then move on. Do not keep apologising.\n"
        "- NEVER narrate your own records or what you do or do not have. Say "
        "'May I have your name please?', never 'I don't have your name from "
        "our earlier conversation'. The guest should never hear about notes, "
        "records, transcripts, summaries, tools, or a transfer that failed - "
        "only a warm person taking their details.\n"
    )


# ---------------------------------------------------------------------------
# LLM client (module-level singletons)
# ---------------------------------------------------------------------------
_anthropic_client: AsyncAnthropic | None = None
_openai_client: AsyncOpenAI | None = None
_gemini_client: Any = None  # google.genai.Client when available


def _get_anthropic_client() -> AsyncAnthropic:
    """Return the shared AsyncAnthropic client (for LLM_PROVIDER='claude')."""
    global _anthropic_client
    if _anthropic_client is None:
        if not ANTHROPIC_API_KEY:
            raise RuntimeError("ANTHROPIC_API_KEY is not set")
        _anthropic_client = AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
        logger.info("Initialized Anthropic client with model %s", MODEL)
    return _anthropic_client


def _get_client() -> AsyncOpenAI:
    """Return the shared AsyncOpenAI client (for LLM_PROVIDER='openai')."""
    global _openai_client
    if _openai_client is None:
        if not OPENAI_API_KEY:
            raise RuntimeError("OPENAI_API_KEY is not set")
        _openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)
        logger.info("Initialized OpenAI client with model %s", MODEL)
    return _openai_client


def _get_gemini_client():
    """Return the shared native Gemini client (for LLM_PROVIDER='gemini')."""
    global _gemini_client
    if _gemini_client is None:
        if not GOOGLE_GENAI_AVAILABLE:
            raise RuntimeError("google-genai package not installed")
        if not GEMINI_API_KEY:
            raise RuntimeError("GEMINI_API_KEY is not set")
        _gemini_client = google_genai.Client(api_key=GEMINI_API_KEY)
        logger.info("Initialized native Gemini client with model %s", MODEL)
    return _gemini_client


def _history_to_gemini(history: list[dict]) -> list[dict]:
    """Convert OpenAI-format history to Gemini-native contents.

    OpenAI format:
      - {"role": "user",      "content": "..."}
      - {"role": "assistant", "content": "...", "tool_calls": [...]}
      - {"role": "tool",      "tool_call_id": "...", "content": "..."}

    Gemini format:
      - {"role": "user",  "parts": [{"text": "..."}]}
      - {"role": "model", "parts": [{"text": "..."}, {"function_call": {...}}]}
      - {"role": "user",  "parts": [{"function_response": {...}}]}
    """
    # Build tool_call_id â†’ tool_name map from assistant messages
    tc_id_to_name: dict[str, str] = {}
    for msg in history:
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            for tc in msg["tool_calls"]:
                tc_id_to_name[tc["id"]] = tc["function"]["name"]

    contents: list[dict] = []
    i = 0
    while i < len(history):
        msg = history[i]
        role = msg.get("role")

        if role == "user":
            contents.append({"role": "user", "parts": [{"text": msg["content"]}]})
            i += 1

        elif role == "assistant":
            parts: list[dict] = []
            if msg.get("content"):
                parts.append({"text": msg["content"]})
            if msg.get("tool_calls"):
                for tc in msg["tool_calls"]:
                    try:
                        args = json.loads(tc["function"]["arguments"]) if tc["function"]["arguments"] else {}
                    except json.JSONDecodeError:
                        args = {}
                    parts.append({
                        "function_call": {
                            "name": tc["function"]["name"],
                            "args": args,
                        }
                    })
            if parts:
                contents.append({"role": "model", "parts": parts})
            i += 1

        elif role == "tool":
            # Collect consecutive tool result messages into one user message
            fn_parts: list[dict] = []
            while i < len(history) and history[i].get("role") == "tool":
                tool_msg = history[i]
                tc_id = tool_msg.get("tool_call_id", "")
                name = tc_id_to_name.get(tc_id, "unknown")
                try:
                    response_data = json.loads(tool_msg["content"])
                except (json.JSONDecodeError, TypeError):
                    response_data = {"result": tool_msg.get("content", "")}
                fn_parts.append({
                    "function_response": {
                        "name": name,
                        "response": response_data,
                    }
                })
                i += 1
            contents.append({"role": "user", "parts": fn_parts})

        else:
            i += 1  # skip unknown roles

    return contents


# ---------------------------------------------------------------------------
# Application lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup / shutdown lifecycle for the FastAPI application."""
    # --- Startup ---
    logger.info("Starting Hatton Hills Voice Agent server...")

    # Initialize knowledge base
    logger.info("Initializing knowledge base from '%s'...", KB_DOCS_DIRECTORY)
    kb_ok = initialize_kb(KB_DOCS_DIRECTORY)
    if kb_ok:
        logger.info("Knowledge base initialized successfully.")
    else:
        logger.warning("Knowledge base initialization failed — continuing without KB.")

    # Pre-warm embeddings model to reduce first-query latency
    prewarm()

    # Pre-create the LLM client
    logger.info("LLM provider: %s, model: %s", LLM_PROVIDER, MODEL)
    try:
        if LLM_PROVIDER == "claude":
            _get_anthropic_client()
        elif LLM_PROVIDER == "gemini":
            _get_gemini_client()
        else:
            _get_client()
    except RuntimeError as exc:
        logger.error("Cannot create LLM client: %s", exc)

    if not ELEVENLABS_API_KEY or not ELEVENLABS_VOICE_ID:
        logger.warning("ELEVENLABS_API_KEY or ELEVENLABS_VOICE_ID not set — "
                       "ConversationRelay TTS will not work in production.")

    if HUMAN_AGENT_PHONE:
        logger.info("[handoff] enabled â†’ %s", HUMAN_AGENT_PHONE)
    else:
        logger.info("[handoff] disabled (HUMAN_AGENT_PHONE not set)")

    logger.info("[handoff] public hostname: %s", PUBLIC_HOSTNAME)

    if TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN:
        # Eagerly construct the singleton so failures show up at boot, not on first call.
        _get_twilio_client()
        logger.info(
            "[handoff] Twilio REST client configured (account=%s...)",
            TWILIO_ACCOUNT_SID[:10],
        )
    else:
        logger.warning(
            "[handoff] Twilio REST client NOT configured — handoff will fail"
        )

    # NOTE: bookings go to the Yanolja PMS via booking_api -> yanolja_service ->
    # yanolja_client. Hatton Hills is an invented demo property, so there is no
    # real upstream booking engine to re-wire; the PMS IS the source of truth
    # (see ops/hattonhills-pms/).
    logger.info("Server startup complete. Booking backend configured: %s", is_configured())

    yield

    # --- Shutdown ---
    logger.info("Shutting down server...")
    await close_session()
    logger.info("Shutdown complete.")


# ---------------------------------------------------------------------------
# FastAPI application
# ---------------------------------------------------------------------------
app = FastAPI(
    title="Hatton Hills Voice Agent (Kavya)",
    version="1.0.0",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Health endpoint
# ---------------------------------------------------------------------------

@app.get("/health")
async def health() -> dict[str, Any]:
    """Return service health status."""
    return {
        "status": "ok",
        "llm_provider": LLM_PROVIDER,
        "model": MODEL,
        "ezee_configured": is_configured(),
        "kb_loaded": os.path.isdir(KB_DOCS_DIRECTORY),
        "media_streams_stt": GOOGLE_STT_AVAILABLE,
        "stt_provider": STT_PROVIDER,
        "azure_stt": AZURE_STT_AVAILABLE,
        "azure_tts": bool(AZURE_SPEECH_KEY),
    }


# ---------------------------------------------------------------------------
# Admin: hot-reload knowledge base without container restart
# ---------------------------------------------------------------------------

@app.post("/kb-reload")
async def kb_reload(request: Request) -> dict:
    """Receive new KB content from the admin portal and rebuild the vector store.

    Protected by X-KB-Secret header matching KB_RELOAD_SECRET env var.
    The rebuild runs in a thread so the response returns immediately.
    """
    secret = request.headers.get("X-KB-Secret", "")
    if not KB_RELOAD_SECRET or secret != KB_RELOAD_SECRET:
        from fastapi import HTTPException
        raise HTTPException(status_code=401, detail="Unauthorized")
    body = await request.json()
    content: str = body.get("content", "")
    filename: str = body.get("filename", "hotel_info.txt")
    if not content:
        return {"ok": False, "error": "Empty content"}
    import asyncio, concurrent.futures
    loop = asyncio.get_event_loop()
    loop.run_in_executor(None, reload_kb_from_content, content, filename)
    logger.info("KB reload triggered for file '%s' (%d chars)", filename, len(content))
    return {"ok": True, "message": f"KB reload started for {filename}"}


# ---------------------------------------------------------------------------
# Twilio incoming call webhook
# ---------------------------------------------------------------------------

@app.post("/voice/incoming")
async def voice_incoming(request: Request) -> Response:
    """Twilio webhook for incoming phone calls.

    By default (IVR_MENU_ENABLED unset/false) connects the caller straight to
    the English ConversationRelay agent - no IVR / language menu. Set
    IVR_MENU_ENABLED=true to present the DTMF language menu instead.
    """
    form = await request.form()
    host = request.headers.get("host", request.url.hostname or "localhost")

    # Store caller phone for the WebSocket handler to pick up
    incoming_call_sid = str(form.get("CallSid", ""))
    incoming_caller_phone = str(form.get("From", ""))
    if incoming_call_sid and incoming_caller_phone:
        _call_phone[incoming_call_sid] = incoming_caller_phone

    # Extend call duration to 45 minutes (Twilio default is 5 min for ConversationRelay)
    if incoming_call_sid:
        twilio = _get_twilio_client()
        if twilio:
            try:
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(
                    None,
                    lambda: twilio.calls(incoming_call_sid).update(time_limit=2700),
                )
            except Exception:
                logger.warning("Could not update call time_limit for %s", incoming_call_sid)

    en = LANGUAGE_CONFIGS["en"]
    cr = _build_conversation_relay_twiml(host, "en", en)

    # IVR language menu (IVR_MENU_ENABLED=true only): 1 = English
    # (ConversationRelay). Sinhala and Arabic were removed on 2026-07-28, so
    # English is the only option and any other digit falls back to it. With the
    # menu disabled (the default) the <Gather> is omitted entirely and every
    # call connects straight to the English agent below — which is the
    # preferred setting now that there is only one language.
    gather = ""
    if IVR_MENU_ENABLED:
        gather = (
            f'  <Gather numDigits="1" action="https://{host}/voice/language-selected"'
            ' method="POST" timeout="6">\n'
            '    <Say voice="Polly.Joanna">Welcome to Hatton Hills. '
            'For English, press 1.</Say>\n'
            "  </Gather>\n"
        )

    twiml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<Response>\n"
        f"{gather}"
        f'  <Connect action="https://{host}/voice/relay-action" method="POST">\n'
        f"    {cr}\n"
        "  </Connect>\n"
        "</Response>"
    )

    logger.info(
        "Incoming call from %s - %s",
        request.headers.get("x-forwarded-for", "unknown"),
        "presenting English-only language menu" if IVR_MENU_ENABLED
        else "IVR menu disabled, connecting straight to English agent",
    )

    return Response(content=twiml, media_type="application/xml")


# ---------------------------------------------------------------------------
# Language selection handler (called by Twilio after DTMF digit)
# ---------------------------------------------------------------------------

@app.post("/voice/language-selected")
async def voice_language_selected(request: Request) -> Response:
    """Handle the caller's DTMF language selection.

    English (1) â†’ ConversationRelay TwiML (ElevenLabs TTS, text-in/text-out).
    Every other digit falls back to English: DIGIT_TO_LANG maps only "1"
    since Sinhala and Arabic were removed (2026-07-28). The Media Streams
    branch below is kept for whenever a non-English language is re-added.
    """
    form = await request.form()
    digit = str(form.get("Digits", "1"))
    lang = DIGIT_TO_LANG.get(digit, "en")
    host = request.headers.get("host", request.url.hostname or "localhost")

    # Store caller phone for WebSocket handlers to pick up
    sel_call_sid = str(form.get("CallSid", ""))
    sel_caller_phone = str(form.get("From", ""))
    if sel_call_sid and sel_caller_phone:
        _call_phone[sel_call_sid] = sel_caller_phone

    if lang == "en":
        # English — ConversationRelay with ElevenLabs
        config = LANGUAGE_CONFIGS["en"]
        cr_tag = _build_conversation_relay_twiml(host, "en", config)
        twiml = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            "<Response>\n"
            f'  <Connect action="https://{host}/voice/relay-action" method="POST">\n'
            f"    {cr_tag}\n"
            "  </Connect>\n"
            "</Response>"
        )
        mode = "ConversationRelay"
    else:
        # Non-English — Media Streams with Google STT + per-language TTS.
        # Unreachable while DIGIT_TO_LANG is English-only; kept for re-enable.
        twiml = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            "<Response>\n"
            "  <Connect>\n"
            f'    <Stream url="wss://{host}/ws/media-stream/{lang}" />\n'
            "  </Connect>\n"
            "</Response>"
        )
        mode = "Media Streams"

    logger.info(
        "Language selected: %s (digit: %s) — returning %s TwiML",
        lang, digit, mode,
    )
    return Response(content=twiml, media_type="application/xml")


# ---------------------------------------------------------------------------
# Human-agent handoff endpoints (English ConversationRelay only)
# ---------------------------------------------------------------------------

@app.post("/voice/relay-action")
async def relay_action(request: Request) -> Response:
    """Twilio POSTs here when the ConversationRelay session ends.

    If the server sent {"type":"end","handoffData":...} with a
    transfer_to_human action, dial the configured human phone with a whisper
    and fallback. Otherwise (caller hung up etc.) simply hang up.
    """
    form = await request.form()
    # TEMP DIAGNOSTIC: log every form field Twilio posts so we can see the
    # exact name (HandoffData vs handoffData vs other) and confirm presence.
    try:
        logger.info("[handoff] relay-action raw form: %s", dict(form))
    except Exception:
        logger.exception("[handoff] failed to log raw form")
    # Accept both PascalCase (Twilio docs) and camelCase (defensive).
    handoff_raw = form.get("HandoffData") or form.get("handoffData") or ""
    call_sid = form.get("CallSid", "")
    handoff: dict = {}
    if handoff_raw:
        # handoffData may arrive as a JSON-encoded string OR as a literal
        # JSON object depending on Twilio behavior; try string first, then
        # accept already-parsed structures (e.g. dict-like form values).
        if isinstance(handoff_raw, (bytes, bytearray)):
            try:
                handoff_raw = handoff_raw.decode("utf-8")
            except Exception:
                handoff_raw = ""
        if isinstance(handoff_raw, str):
            try:
                parsed = json.loads(handoff_raw)
                if isinstance(parsed, dict):
                    handoff = parsed
                elif isinstance(parsed, str):
                    # Double-encoded — try one more parse
                    try:
                        inner = json.loads(parsed)
                        if isinstance(inner, dict):
                            handoff = inner
                    except json.JSONDecodeError:
                        pass
            except json.JSONDecodeError:
                logger.warning(
                    "[handoff] HandoffData not valid JSON: %r", handoff_raw[:300]
                )
        elif isinstance(handoff_raw, dict):
            handoff = handoff_raw

    if handoff.get("action") == "transfer_to_human" and HUMAN_AGENT_PHONE:
        reason = handoff.get("reason", "Caller requested assistance.")
        caller_phone = handoff.get("caller_phone", "")
        # Legacy Path A fallback — dashboard event is now sent from
        # ws_conversation when the REST-based Path B handoff fires. We keep
        # this endpoint to return Dial TwiML on the off-chance Twilio ever
        # delivers HandoffData via the relay-end callback again.
        host = request.url.hostname
        whisper_url = f"https://{host}/voice/whisper?reason={url_quote(reason)}"
        dial_action_url = f"https://{host}/voice/dial-result"
        logger.info(
            "[handoff] dialing human %s for call %s (reason=%r)",
            HUMAN_AGENT_PHONE, call_sid, reason,
        )
        # Same owned-number caller ID as the live Path B transfer — see
        # TWILIO_CALLER_ID.
        _cid = _transfer_caller_id(call_sid)
        _cid_attr = f' callerId="{html_escape(_cid)}"' if _cid else ""
        twiml = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            "<Response>\n"
            f'  <Dial action="{dial_action_url}" method="POST" timeout="{HANDOFF_DIAL_TIMEOUT}"{_cid_attr} answerOnBridge="true">\n'
            f'    <Number url="{whisper_url}"'
            f' statusCallback="https://{host}/voice/dial-status?parent={call_sid}"'
            ' statusCallbackMethod="POST"'
            ' statusCallbackEvent="initiated ringing answered completed">'
            f'{HUMAN_AGENT_PHONE}</Number>\n'
            "  </Dial>\n"
            "</Response>"
        )
        return Response(content=twiml, media_type="application/xml")

    logger.info(
        "[handoff] relay-action with no transfer (call_sid=%s, action=%r) — hanging up",
        call_sid, handoff.get("action"),
    )
    return Response(
        content='<?xml version="1.0" encoding="UTF-8"?><Response><Hangup/></Response>',
        media_type="application/xml",
    )


@app.post("/voice/whisper")
async def whisper(request: Request) -> Response:
    """Spoken to the human agent on pickup before bridging the caller."""
    reason = request.query_params.get("reason", "Incoming caller.")
    text = f"Incoming caller. {reason}. Connecting now."
    safe = html_escape(text)
    twiml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<Response><Say voice="Polly.Joanna">{safe}</Say></Response>'
    )
    return Response(content=twiml, media_type="application/xml")


@app.post("/voice/dial-status")
async def dial_status(request: Request) -> Response:
    """Per-event status callback for the outbound transfer leg.

    Twilio POSTs here on initiated / ringing / answered / completed. We only
    need the timestamps, so that /voice/dial-result can tell a real pickup from
    a carrier intercept that answered instantly (see _answer_looks_intercepted).

    Only records against calls we actually dispatched a transfer for, so a
    stray or replayed callback cannot grow _handoff_state without bound.
    """
    form = await request.form()
    parent = request.query_params.get("parent", "")
    event = str(form.get("CallStatus") or "").strip().lower()
    entry = _handoff_state.get(parent)
    if entry is not None and event:
        events = entry.setdefault("dial_events", {})
        # Twilio's statusCallbackEvent is *named* "answered", but the
        # CallStatus field it actually POSTs when a leg is picked up reads
        # "in-progress" — Twilio never sends CallStatus=answered.
        # "answered" is DialCallStatus vocabulary, not CallStatus vocabulary;
        # the two were conflated when this endpoint was written. Because of
        # that, the canonical "answered" key this dict is keyed on was NEVER
        # populated, _answer_looks_intercepted's events.get("answered") always
        # missed, and carrier-intercept detection silently never fired once
        # from its 2026-08-03 release until this was found and fixed on
        # 2026-08-04. Normalise "in-progress" onto "answered" here so the
        # detector's lookup matches what Twilio actually sends. Do NOT
        # "simplify" this back to storing the raw CallStatus value — that is
        # the exact bug. Still accept a literal "answered" too, in case a
        # future Twilio change or a replayed callback sends that literal
        # value.
        canonical = "answered" if event in ("in-progress", "answered") else event
        # First occurrence wins — Twilio can retry a callback, and a retry must
        # not overwrite the original timing with a later clock reading.
        events.setdefault(canonical, time.time())
        logger.info(
            "[handoff] dial-status parent=%s event=%s (have: %s)",
            parent, event, ",".join(sorted(events)),
        )

        # Cut a carrier intercept off immediately rather than holding the guest
        # through it.
        #
        # answerOnBridge bridges the guest the instant the leg is "answered".
        # When that answer is an intercept recording, the guest hears it for as
        # long as the carrier plays it — 49s and 52s in the three production
        # incidents — and only when it finally ends does <Dial> return and the
        # failsafe get its turn. By then the guest has almost always hung up,
        # which is why the failsafe kept opening a recovery session with nobody
        # left on the line. Ending the leg here collapses that wait to about a
        # second, so the guest is still there to be recovered.
        #
        # Deliberately STRICTER than _answer_looks_intercepted: this also
        # requires that no ringing event arrived. A genuine handset pickup
        # essentially always rings first, and unlike dial-result — which only
        # reclassifies a call that has already ended — this cuts off a live
        # one. It must not fire on a fast-but-real answer.
        if (
            HANDOFF_KILL_INTERCEPT
            and canonical == "answered"
            and "ringing" not in events
            and not entry.get("intercept_killed")
        ):
            initiated = events.get("initiated")
            answered = events.get("answered")
            child_sid = str(form.get("CallSid") or "").strip()
            gap = (
                answered - initiated
                if initiated is not None and answered is not None
                else None
            )
            if child_sid and gap is not None and gap < HANDOFF_MIN_ANSWER_SECONDS:
                entry["intercept_killed"] = True
                logger.warning(
                    "[handoff] leg %s answered %.2fs after dial with no ringing "
                    "— carrier intercept, hanging it up so the guest is not held "
                    "through the recording", child_sid, gap,
                )
                twilio = _get_twilio_client()
                if twilio:
                    try:
                        loop = asyncio.get_event_loop()
                        await loop.run_in_executor(
                            None,
                            lambda: twilio.calls(child_sid).update(
                                status="completed"
                            ),
                        )
                    except Exception:
                        # Not fatal: <Dial> still ends on its own when the
                        # carrier stops talking, so the failsafe is delayed
                        # rather than lost.
                        logger.warning(
                            "[handoff] could not hang up intercepted leg %s",
                            child_sid, exc_info=True,
                        )

    return Response(status_code=204)


@app.post("/voice/dial-result")
async def dial_result(request: Request) -> Response:
    """Callback from <Dial action>. If the human answered â†’ hang up.
    Otherwise, drop the caller back into Kavya with a recovery greeting.
    """
    form = await request.form()
    status = form.get("DialCallStatus", "")
    call_sid = form.get("CallSid", "")
    host = request.url.hostname
    logger.info("[handoff] dial-result status=%s call_sid=%s", status, call_sid)

    # "completed" only means the leg ended normally — it does NOT prove a human
    # answered. A carrier intercept answers instantly and also reports
    # completed, so check the timing before standing the failsafe down.
    intercepted, why = _answer_looks_intercepted(_handoff_state.get(call_sid, {}))
    if status in ("completed", "answered") and intercepted:
        logger.warning(
            "[handoff] dial reported %s for %s but %s — treating as NOT answered",
            status, call_sid, why,
        )
        status = "intercepted"

    if status in ("completed", "answered"):
        logger.info("[handoff] human answer accepted for %s (%s)", call_sid, why)
        # Human took the call — no failsafe needed, drop the carry-over.
        state = _handoff_state.pop(call_sid, {})
        # This is the end of the line for this call, so THIS is where the
        # post-call record gets written. The ConversationRelay session
        # deliberately skipped it (transfer_initiated) because at that point we
        # did not yet know whether the human would pick up; without emitting it
        # here, a successfully transferred call would leave no row at all.
        transcript = state.get("transcript") or []
        if transcript:
            # Bookkeeping must never break the call. This handler's job is to
            # return TwiML; if building an LLM client or scheduling the task
            # fails, log it and still hang up cleanly rather than 500 at Twilio.
            try:
                asyncio.create_task(
                    process_post_call_data(
                        call_sid=call_sid,
                        lang=state.get("lang", "en"),
                        caller_phone=state.get("caller_phone", "unknown"),
                        full_transcript=transcript,
                        call_start_time=state.get(
                            "call_start_time", datetime.now().isoformat()
                        ),
                        call_end_time=datetime.now().isoformat(),
                        llm_provider=LLM_PROVIDER,
                        # Only the active provider's client is built, and a
                        # failure yields None rather than aborting: post_call
                        # degrades to a transcript-only record, which still
                        # reaches the sheet. Losing the row entirely because a
                        # key is unset would be the worse outcome.
                        anthropic_client=(
                            _safe_client(_get_anthropic_client)
                            if LLM_PROVIDER == "claude" else None
                        ),
                        openai_client=(
                            _safe_client(_get_client)
                            if LLM_PROVIDER == "openai" else None
                        ),
                        gemini_client=(
                            _safe_client(_get_gemini_client)
                            if LLM_PROVIDER == "gemini" else None
                        ),
                        model=MODEL,
                    )
                )
                logger.info(
                    "[handoff] human answered for %s — post-call emitted (%d turns)",
                    call_sid, len(transcript),
                )
            except Exception:
                logger.exception(
                    "[handoff] post-call dispatch failed for %s — hanging up anyway",
                    call_sid,
                )
        return Response(
            content='<?xml version="1.0" encoding="UTF-8"?><Response><Hangup/></Response>',
            media_type="application/xml",
        )

    # No answer / busy / failed / canceled â†’ the failsafe. Recover into Kavya
    # in handover mode: she collects the guest's name and WhatsApp number and
    # messages the property manager instead of leaving the guest stranded.
    _remember_handoff(call_sid, dial_status=status)
    logger.info(
        "[handoff] human did not answer (%s) for %s — entering failsafe",
        status, call_sid,
    )

    # Page the manager NOW, not only from the recovery session below.
    #
    # The recovery session can only notify anyone if its WebSocket actually
    # opens — via the notify_human_handover tool, or via its end-of-session
    # fallback. On a carrier intercept it usually never opens: the guest has
    # just spent the whole dial listening to a recorded intercept message
    # instead of ringing, and hangs up before Kavya comes back. Live on
    # 2026-08-05 two consecutive intercepted transfers both reached
    # "entering failsafe" and the manager was told nothing at all, because the
    # guest was gone by then. The transfer failed completely silently.
    #
    # So notify from here, where we KNOW the transfer failed and depend on
    # nothing further happening. `notified` makes it idempotent: it suppresses
    # only the duplicate end-of-session net. If the guest does stay on the
    # line, notify_human_handover still sends its richer follow-up with the
    # name and number she collects — two messages beat none.
    state = dict(_handoff_state.get(call_sid) or {})
    if not state.get("notified"):
        _remember_handoff(call_sid, notified=True)
        asyncio.create_task(
            _notify_handover_fallback(
                call_sid=call_sid,
                state=state,
                caller_phone=state.get("caller_phone", ""),
                full_transcript=state.get("transcript") or [],
                lead=(
                    "A transfer to you was NOT answered. The guest may still "
                    "be on the line or may already have hung up — please call "
                    "them back on the number below. Details are from the call "
                    "so far."
                ),
            )
        )

    recovery_config = dict(LANGUAGE_CONFIGS["en"])
    recovery_config["welcome_greeting"] = HANDOFF_FAILSAFE_GREETING
    cr_tag = _build_conversation_relay_twiml(
        host, "en", recovery_config, mode="handover_failsafe",
    )
    twiml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<Response>\n"
        f'  <Connect action="https://{host}/voice/relay-action" method="POST">\n'
        f"    {cr_tag}\n"
        "  </Connect>\n"
        "</Response>"
    )
    return Response(content=twiml, media_type="application/xml")


def _build_conversation_relay_twiml(
    host: str, lang: str, config: dict[str, str], mode: str = ""
) -> str:
    """Build the <ConversationRelay> XML tag for the given language config.

    `mode` is appended to the WebSocket URL so the session handler knows it is
    a recovery session (currently only "handover_failsafe") rather than a fresh
    call — the WebSocket carries no other signal of how it was started.
    """
    extra = config["extra_attrs"]
    # XML-escape the welcome greeting in case it contains special characters
    greeting = xml.sax.saxutils.escape(config["welcome_greeting"])
    mode_qs = f"&amp;mode={url_quote(mode)}" if mode else ""

    # Optional STT tuning (#121). `hints` biases recognition toward tokens the
    # telephony model mishears on Sri Lankan-accented English (spoken digit
    # shorthand like "double"/"triple", plus common local names). A separate
    # transcriptionLanguage is emitted only when explicitly configured, so the
    # default en-US behaviour is unchanged.
    hints = config.get("hints", "")
    hints_attr = (
        f'        hints="{xml.sax.saxutils.escape(hints)}"\n' if hints else ""
    )
    tx_lang = config.get("transcription_language", "")
    tx_lang_attr = (
        f'        transcriptionLanguage="{xml.sax.saxutils.escape(tx_lang)}"\n'
        if tx_lang else ""
    )

    return (
        f'<ConversationRelay url="wss://{host}/ws/conversation?lang={lang}{mode_qs}"\n'
        f'        ttsProvider="{config["tts_provider"]}"\n'
        f'        voice="{config["voice"]}"\n'
        f'{extra}'
        f'        language="{config["language"]}"\n'
        f'        transcriptionProvider="google"\n'
        f'{tx_lang_attr}'
        f'        speechModel="telephony"\n'
        f'{hints_attr}'
        f'        welcomeGreeting="{greeting}"\n'
        '        interruptible="true"\n'
        '        dtmfDetection="true">\n'
        "    </ConversationRelay>"
    )



# ---------------------------------------------------------------------------
# Sentence extraction helper (Media Streams streaming TTS)
# ---------------------------------------------------------------------------

def _extract_sentences(buffer: str) -> tuple[list[str], str]:
    """Split buffer on sentence boundaries; return (complete_sentences, remainder)."""
    parts = _SENTENCE_END.split(buffer)
    if len(parts) <= 1:
        return [], buffer
    complete = [p.strip() for p in parts[:-1] if p.strip()]
    remaining = parts[-1]
    return complete, remaining


# ---------------------------------------------------------------------------
# Conversation history management
# ---------------------------------------------------------------------------

def _is_tool_result_msg(msg: dict) -> bool:
    """Check if a message is an orphaned tool result (Anthropic or OpenAI format)."""
    role = msg.get("role")
    # OpenAI format: separate "tool" role
    if role == "tool":
        return True
    # Anthropic format: user message with tool_result content blocks
    if role == "user":
        content = msg.get("content")
        if isinstance(content, list) and content:
            return all(
                isinstance(block, dict) and block.get("type") == "tool_result"
                for block in content
            )
    return False


def _is_tool_call_msg(msg: dict) -> bool:
    """Check if an assistant message contains tool calls (Anthropic or OpenAI format)."""
    if msg.get("role") != "assistant":
        return False
    # OpenAI format
    if msg.get("tool_calls"):
        return True
    # Anthropic format: content is a list with tool_use blocks
    content = msg.get("content")
    if isinstance(content, list):
        return any(
            isinstance(block, dict) and block.get("type") == "tool_use"
            for block in content
        )
    return False


def _trim_history(history: list[dict], max_messages: int = MAX_HISTORY_MESSAGES) -> list[dict]:
    """Keep conversation history within bounds.

    Trims from the front (oldest messages) so the most recent context is
    always preserved. Skips orphaned tool results and assistant messages
    with tool_calls whose results have been trimmed.

    Works with both Anthropic and OpenAI message formats.
    """
    if len(history) <= max_messages:
        return history

    trimmed = history[-max_messages:]

    # Skip leading messages that are orphaned tool exchanges
    while trimmed:
        if _is_tool_result_msg(trimmed[0]):
            trimmed.pop(0)
        elif _is_tool_call_msg(trimmed[0]):
            trimmed.pop(0)
        else:
            break

    return trimmed


# ---------------------------------------------------------------------------
# Google Cloud STT — streaming (background thread, Media Streams only)
# ---------------------------------------------------------------------------

class GoogleSTTStream:
    """Streams mulaw 8 kHz audio to Google Cloud Speech-to-Text.

    Runs the synchronous gRPC streaming_recognize in a daemon thread.
    Fires on_final_result(transcript) from that thread.
    Auto-restarts on the ~5-minute gRPC streaming limit.
    """

    def __init__(self, on_final_result: Any, on_interim_result: Any = None, lang: str = "si"):
        self._on_final = on_final_result
        self._on_interim = on_interim_result
        self._lang = lang
        self._audio_q: queue.Queue[bytes | None] = queue.Queue()
        self._running = False
        self._thread: threading.Thread | None = None
        self._chunk_count = 0

    def start(self):
        if not GOOGLE_STT_AVAILABLE:
            logger.error("Cannot start STT — google-cloud-speech not installed")
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        logger.info("Google STT stream started (lang=%s)", self._lang)

    def stop(self):
        self._running = False
        self._audio_q.put(None)
        if self._thread:
            self._thread.join(timeout=5)

    def feed(self, mulaw_bytes: bytes):
        self._audio_q.put(mulaw_bytes)
        self._chunk_count += 1
        if self._chunk_count % 200 == 0:  # log every ~4s of audio
            logger.info("STT audio feed: %d chunks (lang=%s)", self._chunk_count, self._lang)

    def _audio_generator(self):
        while self._running:
            try:
                chunk = self._audio_q.get(timeout=0.1)
            except queue.Empty:
                continue
            if chunk is None:
                break
            yield chunk

    def _loop(self):
        while self._running:
            try:
                self._run_one_stream()
            except Exception as exc:
                if not self._running:
                    break
                logger.warning("STT stream ended (%s) — restarting...", exc, exc_info=True)

    def _run_one_stream(self):
        client = google_speech.SpeechClient()
        primary = STT_PRIMARY.get(self._lang, "si-LK")
        alternatives = STT_ALTERNATIVES.get(self._lang, ["en-US"])
        config = google_speech.StreamingRecognitionConfig(
            config=google_speech.RecognitionConfig(
                encoding=google_speech.RecognitionConfig.AudioEncoding.MULAW,
                sample_rate_hertz=8000,
                language_code=primary,
                alternative_language_codes=alternatives,
                enable_automatic_punctuation=True,
            ),
            interim_results=True,
        )
        logger.info("STT gRPC stream opening (primary=%s, alts=%s)", primary, alternatives)

        def request_gen():
            for chunk in self._audio_generator():
                yield google_speech.StreamingRecognizeRequest(audio_content=chunk)

        responses = client.streaming_recognize(config=config, requests=request_gen())
        logger.info("STT gRPC stream connected — waiting for speech...")
        for response in responses:
            if not self._running:
                break
            for result in response.results:
                if result.alternatives:
                    transcript = result.alternatives[0].transcript.strip()
                    if result.is_final:
                        logger.info("STT final: %r", transcript)
                        if transcript:
                            self._on_final(transcript)
                    else:
                        logger.info("STT interim: %r", transcript)
                        if transcript and self._on_interim:
                            self._on_interim(transcript)


class AzureSTTStream:
    """Streams audio to Azure Speech-to-Text — drop-in alternative to GoogleSTTStream.

    Mirrors the same interface (start/stop/feed + on_final_result / on_interim_result
    callbacks fired from background threads) so it swaps in via the STT_PROVIDER env var.

    Twilio delivers mulaw 8 kHz; Azure's PushAudioInputStream wants PCM, so each fed
    chunk is decoded mulaw â†’ PCM16 (audioop) before being written. Uses a fixed
    language per call (si-LK / ta-IN) — for a Sinhala-only line that tends to beat
    Google's alternative_language_codes code-switching, which was part of why Google
    rarely committed a final result for conversational Sinhala. Azure fires its own
    `recognized` (final) events, so the 1.5 s interim-endpointing fallback still
    applies but is no longer the only path to a final.
    """

    def __init__(self, on_final_result: Any, on_interim_result: Any = None, lang: str = "si"):
        self._on_final = on_final_result
        self._on_interim = on_interim_result
        self._lang = lang
        self._chunk_count = 0
        self._running = False
        self._push_stream = None
        self._recognizer = None

    def start(self):
        if not AZURE_STT_AVAILABLE:
            logger.error("Cannot start Azure STT — azure-cognitiveservices-speech not installed")
            return
        if audioop is None:
            logger.error("Cannot start Azure STT — audioop unavailable (install audioop-lts on 3.13+)")
            return
        if not AZURE_SPEECH_KEY:
            logger.error("Cannot start Azure STT — AZURE_SPEECH_KEY not set")
            return

        primary = STT_PRIMARY.get(self._lang, "si-LK")
        speech_config = azure_speech.SpeechConfig(
            subscription=AZURE_SPEECH_KEY, region=AZURE_SPEECH_REGION,
        )
        speech_config.speech_recognition_language = primary
        # 8 kHz / 16-bit / mono PCM — what mulaw decodes to.
        fmt = azure_speech.audio.AudioStreamFormat(
            samples_per_second=8000, bits_per_sample=16, channels=1,
        )
        self._push_stream = azure_speech.audio.PushAudioInputStream(stream_format=fmt)
        audio_config = azure_speech.audio.AudioConfig(stream=self._push_stream)
        self._recognizer = azure_speech.SpeechRecognizer(
            speech_config=speech_config, audio_config=audio_config,
        )
        self._recognizer.recognizing.connect(self._on_recognizing)
        self._recognizer.recognized.connect(self._on_recognized)
        self._recognizer.canceled.connect(self._on_canceled)

        self._running = True
        self._recognizer.start_continuous_recognition_async()
        logger.info("Azure STT stream started (lang=%s, primary=%s)", self._lang, primary)

    def stop(self):
        self._running = False
        if self._push_stream is not None:
            try:
                self._push_stream.close()
            except Exception:
                pass
        if self._recognizer is not None:
            try:
                self._recognizer.stop_continuous_recognition_async().get()
            except Exception:
                pass

    def feed(self, mulaw_bytes: bytes):
        if not self._running or self._push_stream is None:
            return
        try:
            pcm = audioop.ulaw2lin(mulaw_bytes, 2)  # mulaw â†’ 16-bit PCM
        except Exception:
            return
        self._push_stream.write(pcm)
        self._chunk_count += 1
        if self._chunk_count % 200 == 0:  # log every ~4s of audio
            logger.info("Azure STT audio feed: %d chunks (lang=%s)", self._chunk_count, self._lang)

    # â”€â”€ Azure SDK event callbacks (fire on the SDK's own threads) â”€â”€â”€â”€â”€â”€â”€â”€â”€
    def _on_recognizing(self, evt):
        text = (evt.result.text or "").strip()
        if text and self._on_interim:
            logger.info("Azure STT interim: %r", text)
            self._on_interim(text)

    def _on_recognized(self, evt):
        if evt.result.reason != azure_speech.ResultReason.RecognizedSpeech:
            return
        text = (evt.result.text or "").strip()
        if text:
            logger.info("Azure STT final: %r", text)
            self._on_final(text)

    def _on_canceled(self, evt):
        logger.warning(
            "Azure STT canceled (lang=%s): reason=%s detail=%s",
            self._lang, evt.reason, getattr(evt, "error_details", ""),
        )


def _make_stt(on_final_result: Any, on_interim_result: Any, lang: str):
    """Build the configured STT backend. STT_PROVIDER: 'google' (default) | 'azure'.

    Falls back to Google if Azure is selected but its SDK/audioop is missing.
    """
    if STT_PROVIDER == "azure":
        if AZURE_STT_AVAILABLE and audioop is not None:
            return AzureSTTStream(on_final_result, on_interim_result, lang)
        logger.error("STT_PROVIDER=azure but Azure STT unavailable — falling back to Google")
    return GoogleSTTStream(on_final_result, on_interim_result, lang)


# ---------------------------------------------------------------------------
# Media Stream Session (Sinhala / Tamil calls)
# ---------------------------------------------------------------------------

class MediaStreamSession:
    """Manages a single Twilio Media Streams call for Sinhala or Tamil.

    Pipeline per turn:
      Google STT â†’ endpointing â†’ KB retrieval â†’ Claude (streaming + tools)
      â†’ Azure TTS â†’ mulaw audio â†’ Twilio
    """

    def __init__(
        self,
        websocket: WebSocket,
        lang: str,
        anthropic_client: AsyncAnthropic | None = None,
        openai_client: AsyncOpenAI | None = None,
        gemini_client=None,
    ):
        self.ws = websocket
        self.anthropic_client = anthropic_client
        self.client = openai_client  # OpenAI client (kept for openai provider)
        self.gemini_client = gemini_client
        self.lang = lang
        self.system_prompt = _build_system_prompt(lang)
        if LLM_PROVIDER == "claude":
            self.tools = get_tools()
        elif LLM_PROVIDER == "gemini":
            self.tools = get_tools_gemini()
        else:
            self.tools = get_tools_openai()

        self.stream_sid: str | None = None
        self.call_sid: str = "unknown"
        self.caller_phone: str = "unknown"
        self.history: list[dict] = []
        self.full_transcript: list[dict[str, str]] = []
        self.call_start_time: str = ""

        self._event_loop: asyncio.AbstractEventLoop | None = None
        self._is_speaking = False
        self._speak_lock = asyncio.Lock()
        self._ws_lock = asyncio.Lock()
        self._speak_generation: int = 0

        self._pending_transcript = ""
        self._latest_interim = ""
        self._endpointing_handle: asyncio.TimerHandle | None = None
        self._stt: GoogleSTTStream | AzureSTTStream | None = None

        # Live-call audio capture (mulaw chunks) for offline STT benchmarking.
        self._audio_dump: list[bytes] = []

        # No-speech re-prompt state
        self._reprompt_task: asyncio.Task | None = None
        self._reprompt_count: int = 0

    # â”€â”€ Main event loop â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    async def run(self):
        self._event_loop = asyncio.get_running_loop()
        await self.ws.accept()
        logger.info("Media stream WebSocket accepted (lang=%s)", self.lang)

        self._stt = _make_stt(
            on_final_result=self._on_stt_result,
            on_interim_result=self._on_stt_interim,
            lang=self.lang,
        )
        self._stt.start()

        try:
            while True:
                raw = await self.ws.receive_text()
                msg = json.loads(raw)
                event = msg.get("event", "")

                if event == "start":
                    meta = msg.get("start", {})
                    self.stream_sid = meta.get("streamSid")
                    self.call_sid = meta.get("callSid", "unknown")
                    self.caller_phone = _call_phone.pop(self.call_sid, "unknown")
                    self.call_start_time = datetime.now().isoformat()
                    _dashboard_call_started(self.call_sid, self.caller_phone, self.lang, self.call_start_time)
                    logger.info(
                        "Media stream started — Call: %s, Stream: %s, lang: %s, phone: %s",
                        self.call_sid, self.stream_sid, self.lang, self.caller_phone,
                    )
                    asyncio.ensure_future(
                        self._speak(MEDIA_STREAM_WELCOME[self.lang])
                    )

                elif event == "media":
                    audio = base64.b64decode(msg["media"]["payload"])
                    if STT_DEBUG_DUMP:
                        self._audio_dump.append(audio)
                    if self._stt:
                        self._stt.feed(audio)

                elif event == "mark":
                    mark_name = msg.get("mark", {}).get("name")
                    logger.info("Mark received [%s]: %s", self.call_sid, mark_name)
                    if mark_name == "tts_done":
                        self._is_speaking = False
                        logger.info("TTS done — listening for guest speech [%s]", self.call_sid)
                        # Agent just finished speaking — arm the no-speech nudge.
                        self._schedule_reprompt()

                elif event == "stop":
                    logger.info("Media stream stopped — Call: %s", self.call_sid)
                    break

        except WebSocketDisconnect:
            logger.info("Media stream disconnected — Call: %s", self.call_sid)
        except Exception:
            logger.exception("Media stream error — Call: %s", self.call_sid)
        finally:
            self._cancel_reprompt()
            if self._stt:
                self._stt.stop()
            self._write_audio_dump()
            if self._endpointing_handle:
                self._endpointing_handle.cancel()
            call_end_time = datetime.now().isoformat()
            logger.info(
                "Media stream session ended — Call: %s, history: %d, transcript: %d msgs",
                self.call_sid, len(self.history), len(self.full_transcript),
            )
            if self.full_transcript:
                asyncio.create_task(
                    process_post_call_data(
                        call_sid=self.call_sid,
                        lang=self.lang,
                        caller_phone=self.caller_phone,
                        full_transcript=self.full_transcript,
                        call_start_time=self.call_start_time,
                        call_end_time=call_end_time,
                        llm_provider=LLM_PROVIDER,
                        anthropic_client=self.anthropic_client,
                        openai_client=self.client,
                        gemini_client=self.gemini_client,
                        model=MODEL,
                    )
                )

    # â”€â”€ STT callback (called from background thread) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def _on_stt_result(self, transcript: str):
        """Called from STT thread on FINAL results."""
        logger.info("STT final result [%s]: %r (speaking=%s)", self.call_sid, transcript, self._is_speaking)
        self._latest_interim = ""  # clear — final supersedes interim
        if self._event_loop is None:
            return
        if self._is_speaking:
            asyncio.run_coroutine_threadsafe(
                self._handle_bargein(), self._event_loop,
            )
            return
        asyncio.run_coroutine_threadsafe(
            self._accumulate_transcript(transcript), self._event_loop,
        )

    def _on_stt_interim(self, transcript: str):
        """Called from STT thread on INTERIM results.

        Google often never fires a final result for conversational speech.
        We drive our own endpointing: each interim resets a 1.5 s silence
        timer; when the timer fires we use the latest interim as the utterance.
        """
        self._latest_interim = transcript
        if self._event_loop is None:
            return
        if self._is_speaking:
            asyncio.run_coroutine_threadsafe(
                self._handle_bargein(), self._event_loop,
            )
            return
        asyncio.run_coroutine_threadsafe(
            self._set_transcript_interim(transcript), self._event_loop,
        )

    async def _handle_bargein(self):
        logger.info("Barge-in detected [%s]", self.call_sid)
        self._is_speaking = False
        self._speak_generation += 1
        self._pending_transcript = ""
        self._latest_interim = ""
        if self._endpointing_handle:
            self._endpointing_handle.cancel()
            self._endpointing_handle = None
        async with self._ws_lock:
            await self.ws.send_text(json.dumps({
                "event": "clear",
                "streamSid": self.stream_sid,
            }))

    # â”€â”€ Debug: live-call audio capture â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def _write_audio_dump(self) -> None:
        """Write captured mulaw call audio to an 8 kHz PCM16 wav (STT bake-off input)."""
        if not self._audio_dump:
            return
        if audioop is None:
            logger.warning("Cannot write audio dump — audioop unavailable")
            self._audio_dump.clear()
            return
        try:
            os.makedirs(STT_DEBUG_DIR, exist_ok=True)
            path = os.path.join(STT_DEBUG_DIR, f"{self.call_sid}_{self.lang}.wav")
            pcm = audioop.ulaw2lin(b"".join(self._audio_dump), 2)
            with wave.open(path, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(8000)
                wf.writeframes(pcm)
            logger.info(
                "Wrote STT debug audio: %s (%d chunks, %.1fs)",
                path, len(self._audio_dump), len(pcm) / 2 / 8000,
            )
        except Exception:
            logger.exception("Failed to write STT audio dump")
        finally:
            self._audio_dump.clear()

    # â”€â”€ No-speech re-prompt â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def _schedule_reprompt(self) -> None:
        """Arm a silence nudge after the agent finishes speaking."""
        if self._reprompt_task and not self._reprompt_task.done():
            self._reprompt_task.cancel()
        self._reprompt_task = asyncio.create_task(self._reprompt_after_silence())

    def _cancel_reprompt(self) -> None:
        if self._reprompt_task and not self._reprompt_task.done():
            self._reprompt_task.cancel()
        self._reprompt_task = None

    async def _reprompt_after_silence(self) -> None:
        try:
            await asyncio.sleep(SILENCE_REPROMPT_DELAY)
            if self._reprompt_count >= MAX_REPROMPTS:
                return
            if self._is_speaking:
                # Agent is talking — re-arm after it finishes.
                self._schedule_reprompt()
                return
            messages = REPROMPT_MESSAGES.get(self.lang, REPROMPT_MESSAGES["en"])
            text = messages[min(self._reprompt_count, len(messages) - 1)]
            self._reprompt_count += 1
            logger.info(
                "No-speech re-prompt [%s] attempt %d (lang=%s)",
                self.call_sid, self._reprompt_count, self.lang,
            )
            self.full_transcript.append({"role": "assistant", "text": text})
            await self._speak(text)
        except asyncio.CancelledError:
            pass

    # â”€â”€ Endpointing â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    async def _accumulate_transcript(self, text: str):
        # Caller is speaking — cancel any pending silence nudge and reset
        # the re-prompt counter so future silences start fresh.
        self._cancel_reprompt()
        self._reprompt_count = 0
        self._pending_transcript = (
            self._pending_transcript + " " + text
            if self._pending_transcript
            else text
        )
        if self._endpointing_handle:
            self._endpointing_handle.cancel()
        self._endpointing_handle = self._event_loop.call_later(
            ENDPOINTING_SILENCE,
            lambda: asyncio.ensure_future(self._flush_transcript()),
        )

    async def _set_transcript_interim(self, text: str):
        """Overwrite (not append) pending transcript with latest interim; reset timer."""
        # Caller is speaking — cancel any pending silence nudge and reset
        # the re-prompt counter.
        self._cancel_reprompt()
        self._reprompt_count = 0
        self._pending_transcript = text
        if self._endpointing_handle:
            self._endpointing_handle.cancel()
        self._endpointing_handle = self._event_loop.call_later(
            ENDPOINTING_SILENCE,
            lambda: asyncio.ensure_future(self._flush_transcript()),
        )

    async def _flush_transcript(self):
        transcript = self._pending_transcript.strip()
        self._pending_transcript = ""
        self._latest_interim = ""
        self._endpointing_handle = None
        if not transcript:
            return
        logger.info("Guest [%s]: %s", self.call_sid, transcript)
        self.full_transcript.append({"role": "user", "text": transcript})
        await self._process_utterance(transcript)

    # â”€â”€ Utterance â†’ KB + Claude + TTS â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    async def _process_utterance(self, text: str):
        try:
            kb_context = retrieve_context(text)
        except Exception:
            logger.exception("KB retrieval failed")
            kb_context = ""

        if kb_context and "No knowledge base loaded" not in kb_context:
            user_msg = f"[Reference context: {kb_context}]\n\nGuest: {text}"
        else:
            user_msg = text

        self.history.append({"role": "user", "content": user_msg})
        self.history = _trim_history(self.history)

        try:
            if LLM_PROVIDER == "claude":
                response_text = await self._run_llm_claude()
            elif LLM_PROVIDER == "gemini":
                response_text = await self._run_llm_gemini()
            else:
                response_text = await self._run_llm()
            if response_text:
                logger.info("Agent [%s]: %s", self.call_sid, response_text[:200])
                self.full_transcript.append({"role": "assistant", "text": response_text})
        except Exception:
            logger.exception("LLM error [%s]", self.call_sid)
            fillers = MEDIA_STREAM_FILLERS.get(self.lang, {})
            error_msg = fillers.get("_default", "I'm sorry, I encountered an error.")
            await self._speak(error_msg)

    # â”€â”€ OpenAI streaming with tool use + sentence-level TTS â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    async def _run_llm(self) -> str:
        full_text = ""
        fillers = MEDIA_STREAM_FILLERS.get(self.lang, {})

        for round_idx in range(MAX_TOOL_ROUNDS):
            logger.info("LLM round %d [%s]", round_idx + 1, self.call_sid)

            text_content = ""
            tool_calls_data: dict[int, dict[str, str]] = {}
            sentence_buffer = ""
            tts_tasks: list[asyncio.Task] = []
            has_tool_use = False
            gen = self._speak_generation

            messages = [{"role": "system", "content": self.system_prompt}] + self.history
            stream = await self.client.chat.completions.create(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                messages=messages,
                tools=self.tools or None,
                stream=True,
            )

            async for chunk in stream:
                choice = chunk.choices[0]
                delta = choice.delta

                if delta.content:
                    text_content += delta.content
                    if not has_tool_use:
                        sentence_buffer += delta.content
                        sentences, sentence_buffer = _extract_sentences(
                            sentence_buffer
                        )
                        for s in sentences:
                            task = asyncio.create_task(
                                self._speak(s, generation=gen)
                            )
                            tts_tasks.append(task)

                if delta.tool_calls:
                    has_tool_use = True
                    for tc_delta in delta.tool_calls:
                        idx = tc_delta.index
                        if idx not in tool_calls_data:
                            tool_calls_data[idx] = {"id": "", "name": "", "arguments": ""}
                        if tc_delta.id:
                            tool_calls_data[idx]["id"] = tc_delta.id
                        if tc_delta.function:
                            if tc_delta.function.name:
                                tool_calls_data[idx]["name"] = tc_delta.function.name
                            if tc_delta.function.arguments:
                                tool_calls_data[idx]["arguments"] += tc_delta.function.arguments

            full_text += text_content

            if tool_calls_data:
                tool_list = list(tool_calls_data.values())
                logger.info(
                    "Tools [%s]: %s", self.call_sid,
                    [t["name"] for t in tool_list],
                )
                if tts_tasks:
                    await asyncio.gather(*tts_tasks)

                first_tool = tool_list[0]["name"]
                filler = fillers.get(first_tool, fillers.get("_default", ""))
                if filler:
                    await self._speak(filler)

                # Build assistant message with tool_calls
                assistant_msg: dict[str, Any] = {
                    "role": "assistant",
                    "content": text_content or None,
                    "tool_calls": [
                        {
                            "id": tc["id"],
                            "type": "function",
                            "function": {
                                "name": tc["name"],
                                "arguments": tc["arguments"],
                            },
                        }
                        for tc in tool_list
                    ],
                }
                self.history.append(assistant_msg)

                # Execute tools and add results
                for tc in tool_list:
                    try:
                        parsed_input = json.loads(tc["arguments"]) if tc["arguments"] else {}
                    except json.JSONDecodeError:
                        logger.error("Bad tool JSON for %s", tc["name"])
                        parsed_input = {}
                    logger.info("Executing tool '%s': %s", tc["name"], parsed_input)
                    try:
                        result_str = await execute_tool(tc["name"], parsed_input)
                    except Exception as exc:
                        logger.exception("Tool '%s' failed", tc["name"])
                        result_str = json.dumps({"error": str(exc)})
                    self.history.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": result_str,
                    })
                    logger.info("Tool '%s' â†’ %s", tc["name"], result_str[:200])

                continue

            # No tools — flush remaining sentence buffer
            remaining = sentence_buffer.strip()
            if remaining:
                tts_tasks.append(
                    asyncio.create_task(
                        self._speak(remaining, generation=gen)
                    )
                )
            if tts_tasks:
                await asyncio.gather(*tts_tasks)

            if text_content:
                self.history.append({"role": "assistant", "content": text_content})
            return full_text

        logger.warning("Exhausted %d tool rounds [%s]", MAX_TOOL_ROUNDS, self.call_sid)
        return full_text

    # â”€â”€ Gemini native streaming with tool use + sentence-level TTS â”€â”€â”€â”€â”€â”€â”€

    async def _run_llm_gemini(self) -> str:
        """Gemini-native streaming version of _run_llm for Media Streams."""
        full_text = ""
        fillers = MEDIA_STREAM_FILLERS.get(self.lang, {})

        for round_idx in range(MAX_TOOL_ROUNDS):
            logger.info("Gemini round %d [%s]", round_idx + 1, self.call_sid)

            text_content = ""
            function_calls: list[dict] = []
            sentence_buffer = ""
            tts_tasks: list[asyncio.Task] = []
            has_tool_use = False
            gen = self._speak_generation

            gemini_contents = _history_to_gemini(self.history)
            config = {
                "system_instruction": self.system_prompt,
                "max_output_tokens": MAX_TOKENS,
            }
            if self.tools:
                config["tools"] = self.tools

            response = await self.gemini_client.aio.models.generate_content_stream(
                model=MODEL,
                contents=gemini_contents,
                config=config,
            )

            finish_reason = None
            async for chunk in response:
                if not chunk.candidates:
                    continue
                candidate = chunk.candidates[0]
                if candidate.finish_reason:
                    finish_reason = candidate.finish_reason
                if not candidate.content or not candidate.content.parts:
                    continue
                for part in candidate.content.parts:
                    if part.text:
                        text_content += part.text
                        if not has_tool_use:
                            sentence_buffer += part.text
                            sentences, sentence_buffer = _extract_sentences(
                                sentence_buffer
                            )
                            for s in sentences:
                                task = asyncio.create_task(
                                    self._speak(s, generation=gen)
                                )
                                tts_tasks.append(task)
                    if part.function_call:
                        has_tool_use = True
                        fc = part.function_call
                        args = dict(fc.args) if fc.args else {}
                        function_calls.append({"name": fc.name, "args": args})

            logger.info(
                "Gemini round %d [%s] — text=%d chars, tools=%d, finish=%s",
                round_idx + 1, self.call_sid, len(text_content),
                len(function_calls), finish_reason,
            )

            full_text += text_content

            if function_calls:
                logger.info(
                    "Tools [%s]: %s", self.call_sid,
                    [fc["name"] for fc in function_calls],
                )
                if tts_tasks:
                    await asyncio.gather(*tts_tasks)

                first_tool = function_calls[0]["name"]
                filler = fillers.get(first_tool, fillers.get("_default", ""))
                if filler:
                    await self._speak(filler)

                # Build assistant message in OpenAI format
                tool_calls_openai = []
                for i, fc in enumerate(function_calls):
                    tc_id = f"gemini_tc_{round_idx}_{i}"
                    tool_calls_openai.append({
                        "id": tc_id,
                        "type": "function",
                        "function": {
                            "name": fc["name"],
                            "arguments": json.dumps(fc["args"]),
                        },
                    })

                assistant_msg: dict[str, Any] = {
                    "role": "assistant",
                    "content": text_content or None,
                    "tool_calls": tool_calls_openai,
                }
                self.history.append(assistant_msg)

                for tc in tool_calls_openai:
                    parsed_input = json.loads(tc["function"]["arguments"]) if tc["function"]["arguments"] else {}
                    logger.info("Executing tool '%s': %s", tc["function"]["name"], parsed_input)
                    try:
                        result_str = await execute_tool(tc["function"]["name"], parsed_input)
                    except Exception as exc:
                        logger.exception("Tool '%s' failed", tc["function"]["name"])
                        result_str = json.dumps({"error": str(exc)})
                    self.history.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": result_str,
                    })
                    logger.info("Tool '%s' â†’ %s", tc["function"]["name"], result_str[:200])

                continue

            # No tools — flush remaining sentence buffer
            remaining = sentence_buffer.strip()
            if remaining:
                tts_tasks.append(
                    asyncio.create_task(
                        self._speak(remaining, generation=gen)
                    )
                )
            if tts_tasks:
                await asyncio.gather(*tts_tasks)

            if text_content:
                self.history.append({"role": "assistant", "content": text_content})
            return full_text

        logger.warning("Exhausted %d tool rounds (Gemini) [%s]", MAX_TOOL_ROUNDS, self.call_sid)
        return full_text

    # â”€â”€ Claude native streaming with tool use + sentence-level TTS â”€â”€â”€â”€â”€â”€â”€

    async def _run_llm_claude(self) -> str:
        """Anthropic Claude streaming for Media Streams with sentence-level TTS."""
        full_text = ""
        fillers = MEDIA_STREAM_FILLERS.get(self.lang, {})

        for round_idx in range(MAX_TOOL_ROUNDS):
            logger.info("Claude round %d [%s]", round_idx + 1, self.call_sid)

            text_content = ""
            tool_use_blocks: list[dict[str, Any]] = []
            cur_tool_name: str | None = None
            cur_tool_id: str | None = None
            tool_json = ""

            sentence_buffer = ""
            tts_tasks: list[asyncio.Task] = []
            has_tool_use = False
            gen = self._speak_generation

            # Prompt caching: marking the system prompt with cache_control
            # caches the entire request prefix (tools + system) for ~5 min.
            # Cuts input tokens and ITPM pressure dramatically on the 2nd+
            # turn of every call.
            async with self.anthropic_client.messages.stream(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                system=[{
                    "type": "text",
                    "text": self.system_prompt,
                    "cache_control": {"type": "ephemeral"},
                }],
                messages=self.history,
                tools=self.tools if self.tools else NOT_GIVEN,
            ) as stream:
                async for event in stream:
                    if event.type == "content_block_start":
                        if event.content_block.type == "tool_use":
                            has_tool_use = True
                            cur_tool_id = event.content_block.id
                            cur_tool_name = event.content_block.name
                            tool_json = ""

                    elif event.type == "content_block_delta":
                        if event.delta.type == "text_delta":
                            text_content += event.delta.text
                            if not has_tool_use:
                                sentence_buffer += event.delta.text
                                sentences, sentence_buffer = _extract_sentences(
                                    sentence_buffer
                                )
                                for s in sentences:
                                    task = asyncio.create_task(
                                        self._speak(s, generation=gen)
                                    )
                                    tts_tasks.append(task)

                        elif event.delta.type == "input_json_delta":
                            tool_json += event.delta.partial_json

                    elif event.type == "content_block_stop":
                        if cur_tool_name:
                            try:
                                parsed = json.loads(tool_json) if tool_json else {}
                            except json.JSONDecodeError:
                                logger.error("Bad tool JSON for %s: %s",
                                             cur_tool_name, tool_json[:200])
                                parsed = {}
                            tool_use_blocks.append({
                                "id": cur_tool_id,
                                "name": cur_tool_name,
                                "input": parsed,
                            })
                            cur_tool_name = None
                            cur_tool_id = None
                            tool_json = ""

            full_text += text_content

            if tool_use_blocks:
                logger.info(
                    "Tools [%s]: %s", self.call_sid,
                    [t["name"] for t in tool_use_blocks],
                )
                if tts_tasks:
                    await asyncio.gather(*tts_tasks)

                first_tool = tool_use_blocks[0]["name"]
                filler = fillers.get(first_tool, fillers.get("_default", ""))
                if filler:
                    await self._speak(filler)

                # Build assistant message with content blocks
                assistant_content: list[dict[str, Any]] = []
                if text_content:
                    assistant_content.append({"type": "text", "text": text_content})
                for tb in tool_use_blocks:
                    assistant_content.append({
                        "type": "tool_use",
                        "id": tb["id"],
                        "name": tb["name"],
                        "input": tb["input"],
                    })
                self.history.append({"role": "assistant", "content": assistant_content})

                # Execute tools and build tool_result blocks
                tool_results: list[dict[str, Any]] = []
                for tb in tool_use_blocks:
                    logger.info("Executing tool '%s': %s", tb["name"], tb["input"])
                    try:
                        result_str = await execute_tool(tb["name"], tb["input"])
                    except Exception as exc:
                        logger.exception("Tool '%s' failed", tb["name"])
                        result_str = json.dumps({"error": str(exc)})
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tb["id"],
                        "content": result_str,
                    })
                    logger.info("Tool '%s' â†’ %s", tb["name"], result_str[:200])

                self.history.append({"role": "user", "content": tool_results})
                continue

            # No tools — flush remaining sentence buffer
            remaining = sentence_buffer.strip()
            if remaining:
                tts_tasks.append(
                    asyncio.create_task(
                        self._speak(remaining, generation=gen)
                    )
                )
            if tts_tasks:
                await asyncio.gather(*tts_tasks)

            if text_content:
                self.history.append({"role": "assistant", "content": text_content})
            return full_text

        logger.warning("Exhausted %d tool rounds (Claude) [%s]", MAX_TOOL_ROUNDS, self.call_sid)
        return full_text

    # â”€â”€ TTS â†’ Twilio mulaw audio â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    async def _speak(self, text: str, generation: int = -1):
        """Route text to appropriate TTS provider.

        Tamil / Arabic â†’ ElevenLabs eleven_multilingual_v2 (cloned voice)
        Sinhala        â†’ OpenAI gpt-4o-mini-tts (nova)
        """
        async with self._speak_lock:
            if generation >= 0 and generation != self._speak_generation:
                return
            if self.lang in ("ta", "ar"):
                await self._tts_elevenlabs(text)
            elif self.lang == "si":
                await self._tts_openai(text)
            else:
                lang_code, voice_name = AZURE_VOICES[self.lang]
                await self._tts_azure(text, lang_code, voice_name)

    # â”€â”€ ElevenLabs TTS (Tamil) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    async def _tts_elevenlabs(self, text: str):
        """Stream text via ElevenLabs eleven_multilingual_v2 and send mulaw audio to Twilio.
        Must only be called from _speak (lock already held).
        """
        if not ELEVENLABS_API_KEY or not ELEVENLABS_VOICE_ID:
            logger.warning("ElevenLabs not configured — skipping TTS")
            return

        self._is_speaking = True
        # Arabic uses its own dedicated voice; Tamil keeps the shared cloned voice.
        voice_id = (
            ELEVENLABS_VOICE_ID_AR or ELEVENLABS_VOICE_ID
        ) if self.lang == "ar" else ELEVENLABS_VOICE_ID
        url = (
            ELEVENLABS_TTS_URL.format(voice_id=voice_id)
            + "?output_format=ulaw_8000"
        )
        headers = {
            "xi-api-key": ELEVENLABS_API_KEY,
            "Content-Type": "application/json",
        }
        voice_settings: dict[str, Any] = {
            "stability": 0.5,
            "similarity_boost": 0.75,
            "style": 0.0,
            "use_speaker_boost": True,
        }
        payload: dict[str, Any] = {
            "text": text,
            "model_id": ELEVENLABS_MODEL_MULTILINGUAL,
            "voice_settings": voice_settings,
        }

        try:
            async with httpx.AsyncClient() as http:
                async with http.stream(
                    "POST", url, json=payload, headers=headers, timeout=15.0,
                ) as resp:
                    if resp.status_code != 200:
                        body = await resp.aread()
                        logger.error("ElevenLabs %d: %s",
                                     resp.status_code, body[:200])
                        self._is_speaking = False
                        return

                    async for chunk in resp.aiter_bytes(chunk_size=640):
                        if not self._is_speaking:
                            break
                        b64 = base64.b64encode(chunk).decode("ascii")
                        async with self._ws_lock:
                            await self.ws.send_text(json.dumps({
                                "event": "media",
                                "streamSid": self.stream_sid,
                                "media": {"payload": b64},
                            }))

            if self._is_speaking:
                async with self._ws_lock:
                    await self.ws.send_text(json.dumps({
                        "event": "mark",
                        "streamSid": self.stream_sid,
                        "mark": {"name": "tts_done"},
                    }))
            else:
                logger.info("ElevenLabs TTS interrupted by barge-in [%s]", self.call_sid)

        except httpx.TimeoutException:
            logger.error("ElevenLabs timeout for: %s", text[:80])
            self._is_speaking = False
        except Exception:
            logger.exception("ElevenLabs TTS failed for: %s", text[:80])
            self._is_speaking = False

    # â”€â”€ Azure TTS (Sinhala) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    async def _tts_azure(self, text: str, lang_code: str, voice_name: str):
        """Stream Azure Cognitive Services TTS as mulaw 8 kHz to Twilio.
        Must only be called from _speak (lock already held).
        """
        if not AZURE_SPEECH_KEY:
            logger.warning("AZURE_SPEECH_KEY not set — skipping TTS")
            return

        self._is_speaking = True
        url = AZURE_TTS_URL.format(region=AZURE_SPEECH_REGION)
        headers = {
            "Ocp-Apim-Subscription-Key": AZURE_SPEECH_KEY,
            "Content-Type": "application/ssml+xml",
            "X-Microsoft-OutputFormat": "raw-8khz-8bit-mono-mulaw",
        }
        escaped = xml.sax.saxutils.escape(text)
        ssml = (
            f"<speak version='1.0' xml:lang='{lang_code}' "
            f"xmlns='http://www.w3.org/2001/10/synthesis'>"
            f"<voice name='{voice_name}'>{escaped}</voice>"
            f"</speak>"
        )

        try:
            async with httpx.AsyncClient() as http:
                async with http.stream(
                    "POST", url, content=ssml.encode("utf-8"),
                    headers=headers, timeout=15.0,
                ) as resp:
                    if resp.status_code != 200:
                        body = await resp.aread()
                        logger.error("Azure TTS %d: %s", resp.status_code, body[:200])
                        self._is_speaking = False
                        return
                    async for chunk in resp.aiter_bytes(chunk_size=640):
                        if not self._is_speaking:
                            break
                        b64 = base64.b64encode(chunk).decode("ascii")
                        async with self._ws_lock:
                            await self.ws.send_text(json.dumps({
                                "event": "media",
                                "streamSid": self.stream_sid,
                                "media": {"payload": b64},
                            }))
            if self._is_speaking:
                async with self._ws_lock:
                    await self.ws.send_text(json.dumps({
                        "event": "mark",
                        "streamSid": self.stream_sid,
                        "mark": {"name": "tts_done"},
                    }))
            else:
                logger.info("Azure TTS interrupted by barge-in [%s]", self.call_sid)
        except httpx.TimeoutException:
            logger.error("Azure TTS timeout for: %s", text[:80])
            self._is_speaking = False
        except Exception:
            logger.exception("Azure TTS failed for: %s", text[:80])
            self._is_speaking = False

    # â”€â”€ OpenAI TTS (Sinhala) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    async def _tts_openai(self, text: str):
        """Stream OpenAI gpt-4o-mini-tts as mulaw 8 kHz to Twilio (Sinhala).

        OpenAI returns raw 24 kHz 16-bit mono PCM; we downsample to 8 kHz and
        mulaw-encode on the fly so it drops straight into the same Twilio media
        framing the Tamil/Azure paths use.
        Must only be called from _speak (lock already held).
        """
        text = text.strip()
        if not OPENAI_API_KEY:
            logger.warning("OPENAI_API_KEY not set — skipping TTS")
            return
        if not text:
            return

        self._is_speaking = True
        payload = {
            "model": OPENAI_TTS_MODEL,
            "voice": OPENAI_TTS_VOICE,
            "input": text,
            "instructions": OPENAI_TTS_INSTRUCTIONS,
            "response_format": "pcm",   # raw 24 kHz 16-bit mono LE PCM
        }
        headers = {
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "Content-Type": "application/json",
        }

        ratecv_state = None   # audioop.ratecv carry-over state (24k -> 8k)
        pcm_tail = b""        # holds a stray odd byte across chunk boundaries
        mulaw_buf = b""       # accumulates mulaw output, flushed in 640-byte frames

        try:
            async with httpx.AsyncClient() as http:
                async with http.stream(
                    "POST", OPENAI_TTS_URL, json=payload, headers=headers,
                    timeout=30.0,
                ) as resp:
                    if resp.status_code != 200:
                        body = await resp.aread()
                        logger.error("OpenAI TTS %d: %s",
                                     resp.status_code, body[:200])
                        self._is_speaking = False
                        return

                    async for chunk in resp.aiter_bytes(chunk_size=4800):
                        if not self._is_speaking:
                            break
                        if not chunk:
                            continue
                        # PCM is 2 bytes/sample — keep sample alignment.
                        data = pcm_tail + chunk
                        if len(data) % 2:
                            data, pcm_tail = data[:-1], data[-1:]
                        else:
                            pcm_tail = b""
                        if not data:
                            continue
                        pcm8k, ratecv_state = audioop.ratecv(
                            data, 2, 1, 24000, 8000, ratecv_state)
                        mulaw_buf += audioop.lin2ulaw(pcm8k, 2)

                        while len(mulaw_buf) >= 640:
                            if not self._is_speaking:
                                break
                            frame, mulaw_buf = mulaw_buf[:640], mulaw_buf[640:]
                            b64 = base64.b64encode(frame).decode("ascii")
                            async with self._ws_lock:
                                await self.ws.send_text(json.dumps({
                                    "event": "media",
                                    "streamSid": self.stream_sid,
                                    "media": {"payload": b64},
                                }))

            # Flush any remaining tail of mulaw audio.
            if self._is_speaking and mulaw_buf:
                b64 = base64.b64encode(mulaw_buf).decode("ascii")
                async with self._ws_lock:
                    await self.ws.send_text(json.dumps({
                        "event": "media",
                        "streamSid": self.stream_sid,
                        "media": {"payload": b64},
                    }))

            if self._is_speaking:
                async with self._ws_lock:
                    await self.ws.send_text(json.dumps({
                        "event": "mark",
                        "streamSid": self.stream_sid,
                        "mark": {"name": "tts_done"},
                    }))
            else:
                logger.info("OpenAI TTS interrupted by barge-in [%s]", self.call_sid)

        except httpx.TimeoutException:
            logger.error("OpenAI TTS timeout for: %s", text[:80])
            self._is_speaking = False
        except Exception:
            logger.exception("OpenAI TTS failed for: %s", text[:80])
            self._is_speaking = False


# ---------------------------------------------------------------------------
# Streaming LLM calls with tool use (OpenAI)
# ---------------------------------------------------------------------------

async def _run_llm_streaming(
    client: AsyncOpenAI,
    system: str,
    conversation_history: list[dict],
    tools: list[dict],
    websocket: WebSocket,
) -> str:
    """Stream an OpenAI response, handling tool use in a loop.

    Sends text tokens to the WebSocket as they arrive so the caller hears
    speech with minimal latency.  When the model invokes tools, a filler
    utterance is spoken before the tool executes, then the loop continues
    with the tool result.

    Returns the final assistant text (concatenated across all rounds).
    """
    full_response_text = ""
    filler_sent = False

    for round_idx in range(MAX_TOOL_ROUNDS):
        logger.info("LLM streaming round %d", round_idx + 1)

        text_content: str = ""
        tool_calls_data: dict[int, dict[str, str]] = {}

        messages = [{"role": "system", "content": system}] + conversation_history
        stream = await client.chat.completions.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            messages=messages,
            tools=tools or None,
            stream=True,
        )

        async for chunk in stream:
            choice = chunk.choices[0]
            delta = choice.delta

            if delta.content:
                text_content += delta.content
                await websocket.send_text(
                    json.dumps({"type": "text", "token": delta.content})
                )

            if delta.tool_calls:
                for tc_delta in delta.tool_calls:
                    idx = tc_delta.index
                    if idx not in tool_calls_data:
                        tool_calls_data[idx] = {"id": "", "name": "", "arguments": ""}
                    if tc_delta.id:
                        tool_calls_data[idx]["id"] = tc_delta.id
                    if tc_delta.function:
                        if tc_delta.function.name:
                            tool_calls_data[idx]["name"] = tc_delta.function.name
                        if tc_delta.function.arguments:
                            tool_calls_data[idx]["arguments"] += tc_delta.function.arguments

        # Accumulate text across rounds
        full_response_text = _join_turn(full_response_text, text_content)

        # -- Handle tool calls --
        if tool_calls_data:
            tool_list = list(tool_calls_data.values())
            logger.info(
                "LLM requested %d tool(s): %s",
                len(tool_list),
                [t["name"] for t in tool_list],
            )

            # Skip the canned filler when the model already streamed its own
            # pre-tool text (avoids duplicate "let me check..." announcements);
            # still send it when the model jumped straight to the tool.
            if not filler_sent and not text_content.strip():
                first_tool_name = tool_list[0]["name"]
                filler = TOOL_FILLERS.get(first_tool_name, DEFAULT_FILLER)
                await websocket.send_text(
                    json.dumps({"type": "text", "token": filler, "last": True})
                )
                logger.info("Sent filler: '%s'", filler)
                filler_sent = True

            # Build assistant message with tool_calls
            assistant_msg: dict[str, Any] = {
                "role": "assistant",
                "content": text_content or None,
                "tool_calls": [
                    {
                        "id": tc["id"],
                        "type": "function",
                        "function": {
                            "name": tc["name"],
                            "arguments": tc["arguments"],
                        },
                    }
                    for tc in tool_list
                ],
            }
            conversation_history.append(assistant_msg)

            # Execute tools and add results
            for tc in tool_list:
                try:
                    parsed_input = json.loads(tc["arguments"]) if tc["arguments"] else {}
                except json.JSONDecodeError:
                    logger.error("Bad tool JSON for %s: %s", tc["name"], tc["arguments"][:200])
                    parsed_input = {}
                logger.info("Executing tool '%s' with input: %s", tc["name"], parsed_input)
                try:
                    result_str = await execute_tool(tc["name"], parsed_input)
                except Exception as exc:
                    logger.exception("Tool execution failed for '%s'", tc["name"])
                    result_str = json.dumps({"error": f"Tool execution failed: {exc}"})

                conversation_history.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": result_str,
                })
                logger.info("Tool '%s' result: %s", tc["name"], result_str[:200])

            text_content = ""
            continue

        # -- No tool calls: we are done --
        await websocket.send_text(
            json.dumps({"type": "text", "token": "", "last": True})
        )

        if text_content:
            conversation_history.append({
                "role": "assistant",
                "content": text_content,
            })

        logger.info("LLM response complete (%d chars)", len(full_response_text))
        return full_response_text

    # Exhausted all tool rounds
    logger.warning("Exhausted %d tool rounds", MAX_TOOL_ROUNDS)
    await websocket.send_text(
        json.dumps({"type": "text", "token": "", "last": True})
    )
    if full_response_text:
        conversation_history.append({
            "role": "assistant",
            "content": full_response_text,
        })
    return full_response_text


# ---------------------------------------------------------------------------
# Streaming LLM calls with tool use (Gemini native)
# ---------------------------------------------------------------------------

async def _run_llm_streaming_gemini(
    gemini_client,
    system: str,
    conversation_history: list[dict],
    tools: list[dict],
    websocket: WebSocket,
) -> str:
    """Stream a Gemini response via the native SDK, handling tool use.

    Uses the same history format (OpenAI) internally, converting to Gemini
    format for each API call.  Tool results are appended in OpenAI format
    so _trim_history works unchanged.
    """
    full_response_text = ""
    filler_sent = False

    for round_idx in range(MAX_TOOL_ROUNDS):
        logger.info("Gemini streaming round %d", round_idx + 1)

        text_content = ""
        function_calls: list[dict] = []  # [{name, args}, ...]

        gemini_contents = _history_to_gemini(conversation_history)
        config = {
            "system_instruction": system,
            "max_output_tokens": MAX_TOKENS,
        }
        if tools:
            config["tools"] = tools

        response = await gemini_client.aio.models.generate_content_stream(
            model=MODEL,
            contents=gemini_contents,
            config=config,
        )

        finish_reason = None
        async for chunk in response:
            if not chunk.candidates:
                continue
            candidate = chunk.candidates[0]
            if candidate.finish_reason:
                finish_reason = candidate.finish_reason
            if not candidate.content or not candidate.content.parts:
                continue
            for part in candidate.content.parts:
                if part.text:
                    text_content += part.text
                    await websocket.send_text(
                        json.dumps({"type": "text", "token": part.text})
                    )
                if part.function_call:
                    fc = part.function_call
                    # Convert args to dict — may be a proto MapComposite
                    args = dict(fc.args) if fc.args else {}
                    function_calls.append({"name": fc.name, "args": args})

        logger.info(
            "Gemini round %d done — text=%d chars, tools=%d, finish=%s",
            round_idx + 1, len(text_content), len(function_calls), finish_reason,
        )

        full_response_text = _join_turn(full_response_text, text_content)

        if function_calls:
            logger.info(
                "Gemini requested %d tool(s): %s",
                len(function_calls),
                [fc["name"] for fc in function_calls],
            )

            # Skip the canned filler when the model already streamed its own
            # pre-tool text (avoids duplicate "let me check..." announcements);
            # still send it when the model jumped straight to the tool.
            if not filler_sent and not text_content.strip():
                first_tool_name = function_calls[0]["name"]
                filler = TOOL_FILLERS.get(first_tool_name, DEFAULT_FILLER)
                await websocket.send_text(
                    json.dumps({"type": "text", "token": filler, "last": True})
                )
                logger.info("Sent filler: '%s'", filler)
                filler_sent = True

            # Build assistant message in OpenAI format (for history storage)
            tool_calls_openai = []
            for i, fc in enumerate(function_calls):
                tc_id = f"gemini_tc_{round_idx}_{i}"
                tool_calls_openai.append({
                    "id": tc_id,
                    "type": "function",
                    "function": {
                        "name": fc["name"],
                        "arguments": json.dumps(fc["args"]),
                    },
                })

            assistant_msg: dict[str, Any] = {
                "role": "assistant",
                "content": text_content or None,
                "tool_calls": tool_calls_openai,
            }
            conversation_history.append(assistant_msg)

            # Execute tools and add results (OpenAI format)
            for tc in tool_calls_openai:
                parsed_input = json.loads(tc["function"]["arguments"]) if tc["function"]["arguments"] else {}
                logger.info("Executing tool '%s' with input: %s", tc["function"]["name"], parsed_input)
                try:
                    result_str = await execute_tool(tc["function"]["name"], parsed_input)
                except Exception as exc:
                    logger.exception("Tool execution failed for '%s'", tc["function"]["name"])
                    result_str = json.dumps({"error": f"Tool execution failed: {exc}"})

                conversation_history.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": result_str,
                })
                logger.info("Tool '%s' result: %s", tc["function"]["name"], result_str[:200])

            text_content = ""
            continue

        # No tool calls — done
        await websocket.send_text(
            json.dumps({"type": "text", "token": "", "last": True})
        )

        if text_content:
            conversation_history.append({
                "role": "assistant",
                "content": text_content,
            })

        logger.info("Gemini response complete (%d chars)", len(full_response_text))
        return full_response_text

    # Exhausted all tool rounds
    logger.warning("Exhausted %d tool rounds (Gemini)", MAX_TOOL_ROUNDS)
    await websocket.send_text(
        json.dumps({"type": "text", "token": "", "last": True})
    )
    if full_response_text:
        conversation_history.append({
            "role": "assistant",
            "content": full_response_text,
        })
    return full_response_text


# ---------------------------------------------------------------------------
# Streaming LLM calls with tool use (Anthropic Claude)
# ---------------------------------------------------------------------------

async def _slow_response_filler(
    websocket: WebSocket, lang: str, delay: float = SLOW_RESPONSE_DELAY
) -> None:
    """Send a brief 'one moment please' filler if the LLM hasn't streamed
    its first token within ``delay`` seconds. Cancelled as soon as content
    starts arriving. Covers Anthropic 429 retries and other latency."""
    try:
        await asyncio.sleep(delay)
        text = SLOW_RESPONSE_FILLERS.get(lang, SLOW_RESPONSE_FILLERS["en"])
        await websocket.send_text(
            json.dumps({"type": "text", "token": text, "last": True})
        )
        logger.info("Sent slow-response filler [%s]: %r", lang, text)
    except asyncio.CancelledError:
        pass
    except Exception:
        logger.exception("Slow-response filler failed to send")


async def _run_llm_streaming_claude(
    client: AsyncAnthropic,
    system: str,
    conversation_history: list[dict],
    tools: list[dict],
    websocket: WebSocket,
    lang: str = "en",
) -> str:
    """Stream a Claude response via the Anthropic SDK, handling tool use.

    Uses Anthropic's native message format with content blocks.
    Sends text tokens to the WebSocket as they arrive for ConversationRelay.

    If the WebSocket closes mid-stream, the Claude response is still drained
    so that ``conversation_history`` ends up with a coherent assistant turn
    (important for post-call transcript capture).
    """
    full_response_text = ""
    filler_sent = False
    ws_closed = False

    async def _safe_send(payload: dict) -> None:
        nonlocal ws_closed
        if ws_closed:
            return
        try:
            await websocket.send_text(json.dumps(payload))
        except (WebSocketDisconnect, RuntimeError) as exc:
            ws_closed = True
            logger.warning(
                "WebSocket closed mid-stream (%s) — draining Claude silently",
                type(exc).__name__,
            )

    for round_idx in range(MAX_TOOL_ROUNDS):
        logger.info("Claude streaming round %d", round_idx + 1)

        text_content = ""
        tool_use_blocks: list[dict[str, Any]] = []
        cur_tool_name: str | None = None
        cur_tool_id: str | None = None
        tool_json = ""

        # Fire a "one moment please" if Claude doesn't start producing
        # content quickly (e.g. during a 429 retry sleep inside the SDK).
        slow_task: asyncio.Task | None = None
        if not ws_closed:
            slow_task = asyncio.create_task(_slow_response_filler(websocket, lang))

        def _cancel_slow() -> None:
            if slow_task and not slow_task.done():
                slow_task.cancel()

        async with client.messages.stream(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=[{
                "type": "text",
                "text": system,
                "cache_control": {"type": "ephemeral"},
            }],
            messages=conversation_history,
            tools=tools if tools else NOT_GIVEN,
        ) as stream:
            async for event in stream:
                if event.type == "content_block_start":
                    _cancel_slow()
                    if event.content_block.type == "tool_use":
                        cur_tool_id = event.content_block.id
                        cur_tool_name = event.content_block.name
                        tool_json = ""

                elif event.type == "content_block_delta":
                    _cancel_slow()
                    if event.delta.type == "text_delta":
                        text_content += event.delta.text
                        await _safe_send({"type": "text", "token": event.delta.text})
                    elif event.delta.type == "input_json_delta":
                        tool_json += event.delta.partial_json

                elif event.type == "content_block_stop":
                    if cur_tool_name:
                        try:
                            parsed = json.loads(tool_json) if tool_json else {}
                        except json.JSONDecodeError:
                            logger.error("Bad tool JSON for %s: %s", cur_tool_name, tool_json[:200])
                            parsed = {}
                        tool_use_blocks.append({
                            "id": cur_tool_id,
                            "name": cur_tool_name,
                            "input": parsed,
                        })
                        cur_tool_name = None
                        cur_tool_id = None
                        tool_json = ""

        _cancel_slow()
        full_response_text = _join_turn(full_response_text, text_content)

        # -- Handle tool calls --
        if tool_use_blocks:
            logger.info(
                "Claude requested %d tool(s): %s",
                len(tool_use_blocks),
                [t["name"] for t in tool_use_blocks],
            )

            # Only play the canned filler if Claude produced NO pre-tool text
            # of its own. If it already announced the action (e.g. "Let me
            # check availability for you now"), a second canned filler would
            # duplicate that announcement back-to-back, so skip it. When Claude
            # jumps straight to the tool with no preamble, the filler still
            # fires to cover tool-execution latency.
            if not filler_sent and not text_content.strip():
                first_tool_name = tool_use_blocks[0]["name"]
                filler = TOOL_FILLERS.get(first_tool_name, DEFAULT_FILLER)
                await _safe_send({"type": "text", "token": filler, "last": True})
                logger.info("Sent filler: '%s'", filler)
                filler_sent = True

            # Build assistant message with content blocks
            assistant_content: list[dict[str, Any]] = []
            if text_content:
                assistant_content.append({"type": "text", "text": text_content})
            for tb in tool_use_blocks:
                assistant_content.append({
                    "type": "tool_use",
                    "id": tb["id"],
                    "name": tb["name"],
                    "input": tb["input"],
                })
            conversation_history.append({"role": "assistant", "content": assistant_content})

            # Execute tools and build tool_result blocks
            tool_results: list[dict[str, Any]] = []
            for tb in tool_use_blocks:
                logger.info("Executing tool '%s' with input: %s", tb["name"], tb["input"])
                try:
                    result_str = await execute_tool(tb["name"], tb["input"])
                except Exception as exc:
                    logger.exception("Tool execution failed for '%s'", tb["name"])
                    result_str = json.dumps({"error": f"Tool execution failed: {exc}"})
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tb["id"],
                    "content": result_str,
                })
                logger.info("Tool '%s' result: %s", tb["name"], result_str[:200])

            conversation_history.append({"role": "user", "content": tool_results})
            text_content = ""
            continue

        # -- No tool calls: we are done --
        await _safe_send({"type": "text", "token": "", "last": True})

        if text_content:
            conversation_history.append({
                "role": "assistant",
                "content": text_content,
            })

        logger.info(
            "Claude response complete (%d chars)%s",
            len(full_response_text),
            " [WS closed]" if ws_closed else "",
        )
        if ws_closed:
            raise WebSocketDisconnect()
        return full_response_text

    # Exhausted all tool rounds
    logger.warning("Exhausted %d tool rounds (Claude)", MAX_TOOL_ROUNDS)
    await _safe_send({"type": "text", "token": "", "last": True})
    if full_response_text:
        conversation_history.append({
            "role": "assistant",
            "content": full_response_text,
        })
    if ws_closed:
        raise WebSocketDisconnect()
    return full_response_text


async def _stream_llm_turn(
    *,
    system: str,
    conversation_history: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    websocket: WebSocket,
    lang: str,
    anthropic_client: Any,
    gemini_client: Any,
    openai_client: Any,
) -> str:
    """Stream one agent turn over the ConversationRelay socket.

    Extracted so the normal prompt path and the handover-failsafe opening turn
    dispatch on LLM_PROVIDER identically — a second inline copy of this branch
    would silently drift the moment one provider's signature changed.
    """
    if LLM_PROVIDER == "claude":
        return await _run_llm_streaming_claude(
            client=anthropic_client,
            system=system,
            conversation_history=conversation_history,
            tools=tools,
            websocket=websocket,
            lang=lang,
        )
    if LLM_PROVIDER == "gemini":
        return await _run_llm_streaming_gemini(
            gemini_client=gemini_client,
            system=system,
            conversation_history=conversation_history,
            tools=tools,
            websocket=websocket,
        )
    return await _run_llm_streaming(
        client=openai_client,
        system=system,
        conversation_history=conversation_history,
        tools=tools,
        websocket=websocket,
    )


# The synthetic turn that makes Kavya OPEN the failsafe conversation instead of
# waiting for the guest. Twilio speaks the apology greeting, then
# ConversationRelay simply waits for guest speech — so without this the guest
# hears the greeting and then silence, and has to say something ("Okay") before
# Kavya asks anything. Observed live on 2026-07-31: 13 s of dead air.
#
# This is NOT added to full_transcript: the guest never said it, so it must not
# appear in the call log or the Google Sheet.
_FAILSAFE_KICKOFF: str = (
    "[SYSTEM: The guest has just heard the apology message and is waiting in "
    "silence. Speak first, right now. Do NOT greet them again and do NOT "
    "repeat the apology. Go straight to STEP 1 of your steps — ask for, or "
    "confirm, their name — in one short sentence.]"
)


# ---------------------------------------------------------------------------
# WebSocket — ConversationRelay handler
# ---------------------------------------------------------------------------

@app.websocket("/ws/conversation")
async def ws_conversation(websocket: WebSocket, lang: str = "en", mode: str = ""):
    """Handle a Twilio ConversationRelay WebSocket session.

    The ``lang`` query parameter is set by the IVR routing and determines
    which language-specific system prompt Claude receives.

    ``mode="handover_failsafe"`` marks the recovery session Twilio opens after a
    human agent failed to answer a transferred call: Kavya drops the booking
    flow and instead collects the guest's callback details for the manager.

    Message types from Twilio:
      - "setup"   : Session initialization (call metadata).
      - "prompt"  : Transcribed user speech ready for processing.
      - "dtmf"    : DTMF tone detected (logged, not acted on).
      - "interrupt": User interrupted the agent mid-speech.
      - Others    : Logged and ignored.
    """
    # Validate lang param
    if lang not in LANGUAGE_CONFIGS:
        lang = "en"

    # The failsafe only exists on the English ConversationRelay path — that is
    # the only path with a live transfer to fail in the first place.
    is_failsafe: bool = mode == "handover_failsafe" and lang == "en"

    await websocket.accept()
    logger.info(
        "WebSocket connection accepted — language: %s%s",
        lang, " (handover failsafe)" if is_failsafe else "",
    )

    # -- Per-session state --
    conversation_history: list[dict] = []
    system_prompt: str = _build_system_prompt(lang)
    if LLM_PROVIDER == "claude":
        tools: list[dict] = get_tools()
    elif LLM_PROVIDER == "gemini":
        tools: list[dict] = get_tools_gemini()
    else:
        tools: list[dict] = get_tools_openai()
    if is_failsafe:
        # Only the notify tool. Leaving check_availability/create_booking in
        # reach would let Kavya wander back into the booking flow instead of
        # taking the callback details.
        tools = get_handover_tools(
            "claude" if LLM_PROVIDER == "claude"
            else "gemini" if LLM_PROVIDER == "gemini"
            else "openai"
        )
    call_sid: str = "unknown"
    caller_phone: str = "unknown"
    full_transcript: list[dict[str, str]] = []
    call_start_time: str = datetime.now().isoformat()
    # Set when this session ends because we handed the caller to a human. The
    # call is NOT over at that point — it either continues on the human's leg or
    # comes back as the failsafe session — so post-call processing is deferred
    # rather than run here. See the `finally` block.
    transfer_initiated: bool = False

    anthropic_client = None
    openai_client = None
    gemini_client = None
    try:
        if LLM_PROVIDER == "claude":
            anthropic_client = _get_anthropic_client()
        elif LLM_PROVIDER == "gemini":
            gemini_client = _get_gemini_client()
        else:
            openai_client = _get_client()
    except RuntimeError:
        logger.error("LLM client not available — closing WebSocket")
        await websocket.close(code=1011, reason="Server configuration error")
        return

    # â”€â”€ Silence re-prompt (no-speech nudge) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    reprompt_task: asyncio.Task | None = None
    reprompt_count: int = 0

    async def _reprompt_after_silence() -> None:
        nonlocal reprompt_count
        try:
            await asyncio.sleep(SILENCE_REPROMPT_DELAY)
            if reprompt_count >= MAX_REPROMPTS:
                return
            messages = REPROMPT_MESSAGES.get(lang, REPROMPT_MESSAGES["en"])
            text = messages[min(reprompt_count, len(messages) - 1)]
            reprompt_count += 1
            logger.info(
                "No-speech re-prompt [%s] attempt %d: %s",
                call_sid, reprompt_count, text,
            )
            try:
                await websocket.send_text(json.dumps({
                    "type": "text", "token": text, "last": True,
                }))
                full_transcript.append({"role": "assistant", "text": text})
            except Exception:
                logger.exception("Failed to send re-prompt [%s]", call_sid)
                return
            # Re-arm for the next silence window.
            _schedule_reprompt()
        except asyncio.CancelledError:
            pass

    def _schedule_reprompt() -> None:
        nonlocal reprompt_task
        if reprompt_task and not reprompt_task.done():
            reprompt_task.cancel()
        reprompt_task = asyncio.create_task(_reprompt_after_silence())

    def _cancel_reprompt() -> None:
        nonlocal reprompt_task
        if reprompt_task and not reprompt_task.done():
            reprompt_task.cancel()
        reprompt_task = None

    try:
        while True:
            raw = await websocket.receive_text()

            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                logger.warning("Received non-JSON message on WebSocket: %s", raw[:200])
                continue

            msg_type = message.get("type", "")

            # ---------------------------------------------------------------
            # SETUP
            # ---------------------------------------------------------------
            if msg_type == "setup":
                call_sid = message.get("callSid", "unknown")
                # Prefer the `from` field on the ConversationRelay setup message —
                # it's authoritative and avoids a race with the /voice/incoming
                # HTTP handler populating `_call_phone`.
                caller_phone = (
                    message.get("from")
                    or _call_phone.pop(call_sid, None)
                    or "unknown"
                )
                _call_phone.pop(call_sid, None)
                _dashboard_call_started(call_sid, caller_phone, lang, call_start_time)
                logger.info(
                    "Session setup — CallSid: %s, StreamSid: %s, Phone: %s",
                    call_sid,
                    message.get("streamSid", "n/a"),
                    caller_phone,
                )

                if is_failsafe:
                    # Rebuild Kavya's context from the pre-transfer leg of this
                    # same call: the WebSocket is new, but the guest is not.
                    handoff_state = _handoff_state.setdefault(call_sid, {})
                    if caller_phone and caller_phone != "unknown":
                        handoff_state.setdefault("caller_phone", caller_phone)
                    else:
                        caller_phone = handoff_state.get("caller_phone") or caller_phone
                    system_prompt = _build_handoff_failsafe_prompt(handoff_state)
                    # Carry the pre-transfer transcript into the post-call
                    # record so the call log shows one continuous conversation.
                    prior = handoff_state.get("transcript") or []
                    if prior and not full_transcript:
                        full_transcript.extend(prior)
                    handoff_state["call_sid"] = call_sid
                    handoff_state["human_agent_whatsapp"] = HUMAN_AGENT_PHONE
                    handoff_state.setdefault("notified", False)
                    # Give the notify_human_handover handler the call metadata
                    # it cannot receive as a tool argument.
                    handover_context.set(handoff_state)
                    logger.info(
                        "[handover] failsafe session ready [%s] — prior turns: %d, "
                        "caller_phone=%s, dial_status=%s",
                        call_sid, len(prior), caller_phone,
                        handoff_state.get("dial_status", "n/a"),
                    )
                # Session state is already initialized above.
                # Log any additional setup metadata.
                logger.info(
                    "Session ready — system prompt length: %d chars, tools: %d",
                    len(system_prompt),
                    len(tools),
                )

                if is_failsafe:
                    # Open the conversation ourselves. Twilio has just spoken the
                    # apology greeting and ConversationRelay now waits for guest
                    # speech, so without this the guest sits in silence until they
                    # say something unprompted. The kickoff turn is seeded into
                    # conversation_history (the model needs it) but deliberately
                    # NOT into full_transcript (the guest never said it).
                    conversation_history.append(
                        {"role": "user", "content": _FAILSAFE_KICKOFF}
                    )
                    try:
                        opening = await _stream_llm_turn(
                            system=system_prompt,
                            conversation_history=conversation_history,
                            # NO TOOLS on the opening turn. This turn exists only
                            # to break the silence and ask for the name. With the
                            # handover tool in reach the model can — and in tests
                            # did — fire notify_human_handover immediately, paging
                            # the manager before it has the guest's name or
                            # number, and again after. All three providers accept
                            # an empty list (NOT_GIVEN / None).
                            tools=[],
                            websocket=websocket,
                            lang=lang,
                            anthropic_client=anthropic_client,
                            gemini_client=gemini_client,
                            openai_client=openai_client,
                        )
                        logger.info("Agent [%s] (failsafe opening): %s",
                                    call_sid, opening[:200])
                        if opening:
                            full_transcript.append(
                                {"role": "assistant", "text": opening}
                            )
                    except WebSocketDisconnect:
                        raise
                    except Exception:
                        # Never let the opening turn kill the session — the guest
                        # can still speak first and the reprompt below covers it.
                        logger.exception("[handover] failsafe opening turn failed")
                    _schedule_reprompt()
                else:
                    _schedule_reprompt()

            # ---------------------------------------------------------------
            # PROMPT — user speech transcribed
            # ---------------------------------------------------------------
            elif msg_type == "prompt":
                user_text = message.get("voicePrompt", "").strip()
                if not user_text:
                    logger.debug("Empty voicePrompt received — ignoring")
                    continue

                # Caller spoke — cancel any pending silence nudge and reset
                # the re-prompt counter.
                _cancel_reprompt()
                reprompt_count = 0

                logger.info("Guest [%s]: %s", call_sid, user_text)
                full_transcript.append({"role": "user", "text": user_text})

                # Retrieve KB context for this utterance. Skipped in the
                # failsafe session — Kavya is only taking a name and a phone
                # number there, so hotel facts are noise (and an embedding
                # lookup per turn the guest has to wait through).
                if is_failsafe:
                    kb_context = ""
                else:
                    try:
                        kb_context = retrieve_context(user_text)
                    except Exception:
                        logger.exception("KB retrieval failed")
                        kb_context = ""

                # Inject KB context into the user message (not the system prompt)
                if kb_context and kb_context != "No knowledge base loaded. Answering from general knowledge.":
                    user_message = f"[Reference context: {kb_context}]\n\nGuest: {user_text}"
                else:
                    user_message = user_text

                conversation_history.append({"role": "user", "content": user_message})

                # Trim history to stay within bounds
                conversation_history = _trim_history(conversation_history)

                # Stream LLM response
                tools_for_session = tools if lang == "en" else [t for t in tools if t.get("name") != "transfer_to_human"]
                try:
                    response_text = await _stream_llm_turn(
                        system=system_prompt,
                        conversation_history=conversation_history,
                        tools=tools_for_session,
                        websocket=websocket,
                        lang=lang,
                        anthropic_client=anthropic_client,
                        gemini_client=gemini_client,
                        openai_client=openai_client,
                    )
                    logger.info("Agent [%s]: %s", call_sid, response_text[:200])
                    if response_text:
                        full_transcript.append({"role": "assistant", "text": response_text})
                    # Note: we do NOT re-arm the silence nudge after agent replies.
                    # ConversationRelay gives no TTS-finished signal, so the timer
                    # would fire while Twilio is still speaking (or right after).
                    # The nudge is only used for the initial welcome (see setup).

                    # ---------------------------------------------------------
                    # Human-handoff detection (Option B):
                    # scan conversation_history for the most recent
                    # transfer_to_human tool_result. If found and signals
                    # transferring, end the ConversationRelay session with
                    # handoffData so Twilio POSTs /voice/relay-action.
                    # ---------------------------------------------------------
                    pending_transfer_reason: str | None = None
                    for _hist_msg in reversed(conversation_history):
                        if _hist_msg.get("role") != "user":
                            continue
                        _content = _hist_msg.get("content")
                        if not isinstance(_content, list):
                            # First non-tool user message — stop scanning
                            break
                        _found_tool_result = False
                        for _block in _content:
                            if not isinstance(_block, dict):
                                continue
                            if _block.get("type") != "tool_result":
                                continue
                            _found_tool_result = True
                            _raw = _block.get("content", "")
                            try:
                                _parsed = json.loads(_raw) if isinstance(_raw, str) else _raw
                            except (json.JSONDecodeError, TypeError):
                                _parsed = None
                            if isinstance(_parsed, dict) and _parsed.get("status") == "transferring":
                                pending_transfer_reason = _parsed.get(
                                    "reason", "Caller requested human assistance."
                                )
                                break
                        if pending_transfer_reason or not _found_tool_result:
                            break

                    if pending_transfer_reason:
                        logger.info(
                            "[handoff] transfer_to_human signal detected [%s] reason=%r",
                            call_sid, pending_transfer_reason,
                        )
                        # Stash everything the failsafe session will need if the
                        # human never picks up. Must happen BEFORE the REST
                        # update — after it, this WebSocket is on borrowed time.
                        transfer_initiated = True
                        _remember_handoff(
                            call_sid,
                            reason=pending_transfer_reason,
                            caller_phone=caller_phone,
                            transcript=list(full_transcript),
                            notified=False,
                            # Carried so whichever leg finishes the call can emit
                            # a post-call record covering the whole conversation.
                            call_start_time=call_start_time,
                            lang=lang,
                        )
                        # Path B: bypass the ConversationRelay {"type":"end"} +
                        # HandoffData handshake (Twilio kept failing it with
                        # ErrorCode 64105 "Websocket ended" and stripping
                        # HandoffData). Instead, update the in-flight call via
                        # the Twilio REST API. The relay WS will be torn down
                        # naturally by Twilio when the new TwiML takes effect.

                        # Best-effort spoken hint. If Twilio cuts it off mid-word
                        # because the REST update lands first, that's fine.
                        try:
                            await websocket.send_text(json.dumps({
                                "type": "text",
                                "token": "Connecting you to a human agent now.",
                                "last": True,
                            }))
                        except Exception:
                            pass

                        # Dispatch dashboard event SYNCHRONOUSLY here so it
                        # doesn't get cancelled when the WS goes away.
                        if dashboard_client is not None:
                            try:
                                await dashboard_client.send_call_transferred(
                                    call_sid=call_sid,
                                    caller_phone=caller_phone,
                                    reason=pending_transfer_reason,
                                    human_phone=HUMAN_AGENT_PHONE,
                                )
                            except Exception as exc:
                                logger.warning(
                                    "[handoff] dashboard send_call_transferred error: %r",
                                    exc,
                                )

                        tw = _get_twilio_client()
                        if tw is None:
                            logger.error(
                                "[handoff] cannot transfer — TWILIO_ACCOUNT_SID/"
                                "AUTH_TOKEN not configured; ending call"
                            )
                        elif not HUMAN_AGENT_PHONE:
                            logger.error(
                                "[handoff] cannot transfer — HUMAN_AGENT_PHONE "
                                "not set; ending call"
                            )
                        else:
                            from urllib.parse import quote as _quote
                            host = PUBLIC_HOSTNAME
                            reason_q = _quote(pending_transfer_reason)
                            # Present a number we own. Without this Twilio passes
                            # the guest's own number through as caller ID, and the
                            # destination carrier filters the leg as spoofing so
                            # the agent's handset never rings.
                            _cid = _transfer_caller_id(call_sid)
                            _cid_attr = f' callerId="{html_escape(_cid)}"' if _cid else ""
                            logger.info(
                                "[handoff] dialing %s for %s with callerId=%s",
                                HUMAN_AGENT_PHONE, call_sid, _cid or "(pass-through)",
                            )
                            twiml = (
                                '<?xml version="1.0" encoding="UTF-8"?>'
                                '<Response>'
                                '<Say voice="Polly.Joanna">Connecting you now. Please hold.</Say>'
                                f'<Dial action="https://{host}/voice/dial-result" method="POST" timeout="{HANDOFF_DIAL_TIMEOUT}"{_cid_attr} answerOnBridge="true">'
                                f'<Number url="https://{host}/voice/whisper?reason={reason_q}"'
                                f' statusCallback="https://{host}/voice/dial-status?parent={call_sid}"'
                                ' statusCallbackMethod="POST"'
                                ' statusCallbackEvent="initiated ringing answered completed">'
                                f'{HUMAN_AGENT_PHONE}</Number>'
                                '</Dial>'
                                '</Response>'
                            )
                            try:
                                # Twilio REST client is sync — run in executor
                                # to avoid blocking the event loop.
                                loop = asyncio.get_event_loop()
                                await loop.run_in_executor(
                                    None,
                                    lambda: tw.calls(call_sid).update(twiml=twiml),
                                )
                                logger.info(
                                    "[handoff] REST update sent for %s â†’ dialing %s",
                                    call_sid, HUMAN_AGENT_PHONE,
                                )
                            except Exception as exc:
                                logger.error(
                                    "[handoff] REST update failed: %r", exc,
                                )

                        # Exit the receive loop; Twilio will tear down the WS
                        # as it processes the new TwiML.
                        break
                except WebSocketDisconnect:
                    # _run_llm_streaming_claude drains the stream and appends
                    # the assistant turn to conversation_history before raising,
                    # so we can recover the latest assistant message for the
                    # post-call transcript even if the line dropped mid-stream.
                    last_assistant = ""
                    if conversation_history:
                        tail = conversation_history[-1]
                        if tail.get("role") == "assistant":
                            content = tail.get("content")
                            if isinstance(content, str):
                                last_assistant = content
                            elif isinstance(content, list):
                                last_assistant = " ".join(
                                    b.get("text", "")
                                    for b in content
                                    if isinstance(b, dict) and b.get("type") == "text"
                                )
                    if last_assistant and (
                        not full_transcript
                        or full_transcript[-1].get("text") != last_assistant
                    ):
                        full_transcript.append({"role": "assistant", "text": last_assistant})
                    logger.info(
                        "WebSocket disconnected during Claude streaming [%s] — "
                        "captured %d chars of partial response",
                        call_sid, len(last_assistant),
                    )
                    raise
                except Exception:
                    logger.exception("Error during Claude streaming [%s]", call_sid)
                    # Attempt to send an error message to the caller
                    try:
                        await websocket.send_text(
                            json.dumps({
                                "type": "text",
                                "token": "I'm sorry, I'm having a technical issue. Could you please try again?",
                                "last": True,
                            })
                        )
                    except Exception:
                        pass

            # ---------------------------------------------------------------
            # DTMF
            # ---------------------------------------------------------------
            elif msg_type == "dtmf":
                digit = message.get("digit", "?")
                logger.info("DTMF received [%s]: %s", call_sid, digit)

            # ---------------------------------------------------------------
            # INTERRUPT — user interrupted agent speech
            # ---------------------------------------------------------------
            elif msg_type == "interrupt":
                logger.info(
                    "Speech interrupted by guest [%s] — utteranceUntilInterrupt: '%s'",
                    call_sid,
                    message.get("utteranceUntilInterrupt", ""),
                )

            # ---------------------------------------------------------------
            # OTHER
            # ---------------------------------------------------------------
            else:
                logger.debug("Unhandled message type '%s' [%s]: %s", msg_type, call_sid, raw[:200])

    except WebSocketDisconnect:
        logger.info("WebSocket disconnected — CallSid: %s", call_sid)
    except Exception:
        logger.exception("Unexpected error in WebSocket handler [%s]", call_sid)
    finally:
        _cancel_reprompt()
        call_end_time = datetime.now().isoformat()
        logger.info(
            "Session ended — CallSid: %s, history: %d msgs, transcript: %d msgs",
            call_sid, len(conversation_history), len(full_transcript),
        )
        if is_failsafe:
            # Last line of defence: the guest hung up (or the line dropped)
            # before Kavya could send the details. Notify the manager anyway
            # with whatever we have — a callback to the number they rang from
            # beats the manager never hearing about the call at all.
            state = _handoff_state.pop(call_sid, {})
            if not state.get("notified"):
                asyncio.create_task(
                    _notify_handover_fallback(
                        call_sid=call_sid,
                        state=state,
                        caller_phone=caller_phone,
                        full_transcript=full_transcript,
                    )
                )
        if transfer_initiated:
            # Do NOT emit a post-call record here. This session is ending only
            # because the caller is being handed to a human — the conversation
            # continues on another leg, so anything written now describes a
            # truncated call. Live on 2026-07-31 this produced a spurious
            # "dropped" row in the Google Sheet at transfer time, followed by a
            # second, correct "callback_requested" row once the failsafe session
            # finished: two rows for one call, the first one misleading.
            #
            # The record is emitted instead by whichever leg actually ends the
            # call: /voice/dial-result when the human answers, or this same
            # `finally` on the failsafe session (is_failsafe, transfer_initiated
            # False) when they don't.
            logger.info(
                "[handoff] post-call deferred for %s — caller handed to a human",
                call_sid,
            )
        elif full_transcript:
            asyncio.create_task(
                process_post_call_data(
                    call_sid=call_sid,
                    lang=lang,
                    caller_phone=caller_phone,
                    full_transcript=full_transcript,
                    call_start_time=call_start_time,
                    call_end_time=call_end_time,
                    llm_provider=LLM_PROVIDER,
                    anthropic_client=anthropic_client,
                    openai_client=openai_client,
                    gemini_client=gemini_client,
                    model=MODEL,
                )
            )


# ---------------------------------------------------------------------------
# WebSocket — Media Streams handler (non-English languages)
# ---------------------------------------------------------------------------

@app.websocket("/ws/media-stream/{lang}")
async def ws_media_stream(websocket: WebSocket, lang: str):
    """Handle a Twilio Media Streams WebSocket session for a non-English call.

    Language is encoded in the URL path (e.g. /ws/media-stream/si) so it
    is always present — avoids unreliable query-string passing by Twilio.

    Receives raw mulaw 8 kHz audio from Twilio, runs Google Cloud STT,
    sends Claude responses through Azure TTS back as mulaw audio.

    Sinhala ("si") and Arabic ("ar") were removed from this guard on
    2026-07-28 along with their IVR digits, so those paths now refuse the
    connection instead of serving a call. Re-add them here to re-enable.
    """
    if lang not in ("ta",):
        logger.warning(
            "Rejecting Media Streams connection for disabled language %r", lang
        )
        await websocket.accept()
        await websocket.close(code=1008, reason="Language not available")
        return

    anthropic_client = None
    openai_client = None
    gemini_client = None
    try:
        if LLM_PROVIDER == "claude":
            anthropic_client = _get_anthropic_client()
        elif LLM_PROVIDER == "gemini":
            gemini_client = _get_gemini_client()
        else:
            openai_client = _get_client()
    except RuntimeError:
        logger.error("LLM client unavailable — closing Media Streams WebSocket")
        await websocket.accept()
        await websocket.close(code=1011, reason="Server configuration error")
        return

    session = MediaStreamSession(
        websocket=websocket, lang=lang,
        anthropic_client=anthropic_client,
        openai_client=openai_client,
        gemini_client=gemini_client,
    )
    await session.run()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    logger.info("Starting server on port %d", PORT)
    uvicorn.run(
        "server:app",
        host="0.0.0.0",
        port=PORT,
        log_level="info",
    )
