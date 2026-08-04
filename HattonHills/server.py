"""
server.py â€” Main FastAPI server for Hatton Hills Voice Agent (Tanya).

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
    sentry_sdk.set_tag("agent", "hatton")
import queue
import re
import threading
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
from tools import get_tools, get_tools_openai, get_tools_gemini, execute_tool
from booking_api import close_session, is_configured
from post_call import process_post_call_data

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
ELEVENLABS_API_KEY: str = os.getenv("ELEVENLABS_API_KEY", "")
ELEVENLABS_VOICE_ID: str = os.getenv("ELEVENLABS_VOICE_ID", "")
# Dedicated Arabic voice (Media Streams). Falls back to ELEVENLABS_VOICE_ID if unset.
ELEVENLABS_VOICE_ID_AR: str = os.getenv("ELEVENLABS_VOICE_ID_AR", "tavIIPLplRB883FzWU0V")
ELEVENLABS_MODEL_MULTILINGUAL: str = "eleven_multilingual_v2"
ELEVENLABS_MODEL_TURBO: str = "eleven_turbo_v2_5"
# OpenAI TTS (Sinhala, Media Streams). gpt-4o-mini-tts -> raw 24kHz PCM -> mulaw 8k.
OPENAI_TTS_URL: str = "https://api.openai.com/v1/audio/speech"
OPENAI_TTS_MODEL: str = os.getenv("OPENAI_TTS_MODEL", "gpt-4o-mini-tts")
OPENAI_TTS_VOICE: str = os.getenv("OPENAI_TTS_VOICE", "sage")
OPENAI_TTS_INSTRUCTIONS: str = os.getenv(
    "OPENAI_TTS_INSTRUCTIONS",
    "You are Tanya, a warm and professional front-office reservations agent at "
    "Hatton Hills, a boutique hillside hotel in Sri Lanka. Speak natural, "
    "courteous conversational Sinhala with genuine warmth. Vary pitch and pace "
    "naturally and sound like a real person on the phone, not a robot.",
)
TWILIO_ACCOUNT_SID: str = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN: str = os.getenv("TWILIO_AUTH_TOKEN", "")
HUMAN_AGENT_PHONE: str = os.getenv("HUMAN_AGENT_PHONE", "").strip()
PUBLIC_HOSTNAME: str = os.getenv("PUBLIC_HOSTNAME", "voice.taskforceai.tech").strip()

# Twilio REST client singleton â€” used for Path B human handoff
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
CLAUDE_MODEL: str = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6")
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
    logger.warning("google-genai not installed â€” native Gemini provider unavailable")

# ---------------------------------------------------------------------------
# Optional: Google Cloud Speech (Media Streams STT)
# ---------------------------------------------------------------------------
try:
    from google.cloud import speech_v1 as google_speech
    GOOGLE_STT_AVAILABLE = True
except ImportError:
    google_speech = None  # type: ignore[assignment]
    GOOGLE_STT_AVAILABLE = False
    logger.warning("google-cloud-speech not installed â€” Media Streams STT unavailable")

# ---------------------------------------------------------------------------
# Optional: Azure Speech (alternative Media Streams STT, selected via STT_PROVIDER)
# ---------------------------------------------------------------------------
try:
    import azure.cognitiveservices.speech as azure_speech
    AZURE_STT_AVAILABLE = True
except ImportError:
    azure_speech = None  # type: ignore[assignment]
    AZURE_STT_AVAILABLE = False
    logger.warning("azure-cognitiveservices-speech not installed â€” Azure STT provider unavailable")

# audioop decodes Twilio mulaw â†’ PCM16 for Azure's push stream and for audio
# dumps. Stdlib through Python 3.12; removed in 3.13 (use the audioop-lts shim).
try:
    import audioop
except ImportError:  # pragma: no cover
    audioop = None  # type: ignore[assignment]
    logger.warning("audioop unavailable (Python 3.13+) â€” Azure STT and audio dump need audioop-lts")

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

# Caller phone lookup â€” populated by HTTP handlers, consumed by WebSocket handlers
_call_phone: dict[str, str] = {}  # CallSid -> caller phone number

# ---------------------------------------------------------------------------
# Filler messages sent while tools execute
# ---------------------------------------------------------------------------
TOOL_FILLERS: dict[str, str] = {
    "check_availability": "Let me check availability for those dates.",
    "create_booking": "I'm creating your reservation now.",
    "retrieve_booking": "Let me look up that booking for you.",
    "cancel_booking": "Let me process that cancellation.",
}
DEFAULT_FILLER: str = "Let me check that for you."

# Backchannel filter: short non-semantic utterances that callers emit while
# thinking ("um", "uh", "hmm"). Twilio's STT fires these as full prompts and
# without filtering, Tanya would jump in mid-thought, derailing the call.
# We deliberately do NOT include "ok", "yeah", "yes", "no", "right" â€” those
# are genuine answers in this booking flow.
BACKCHANNEL_TOKENS: set[str] = {
    "um", "uh", "uhm", "umm", "uhh", "erm", "er",
    "hmm", "hm", "mm", "mhm", "mmhm", "mhmm",
    "ah", "oh", "huh",
    "ah um", "uh um", "um uh", "uh uh",
}


def _is_backchannel(text: str) -> bool:
    """True if the utterance is purely thinking-noise â€” should be ignored
    so the caller keeps the turn. Strips punctuation and lowercases."""
    # Digit-bearing utterances are real content (phone numbers, dates,
    # room counts, etc.) â€” never treat them as backchannel.
    if any(c.isdigit() for c in text):
        return False
    cleaned = "".join(c for c in text.lower() if c.isalpha() or c.isspace()).strip()
    if not cleaned:
        return True  # empty / pure punctuation
    if len(cleaned) > 8:  # anything longer than "uh uh um" is probably real
        return False
    return cleaned in BACKCHANNEL_TOKENS

# Sent when the LLM hasn't returned its first token within SLOW_RESPONSE_DELAY
# seconds â€” covers Anthropic 429 retries and other network latency so the
# guest doesn't think the line dropped and re-speak (which corrupts slot-filling).
SLOW_RESPONSE_DELAY: float = 2.5
SLOW_RESPONSE_FILLERS: dict[str, str] = {
    "en": "One moment please.",
    "ar": "لحظة من فضلك.",
    "si": "\u0D9A\u0DBB\u0DD4\u0DAF\u0DCF\u0D9A\u0DBB\u0DCF \u0DBB\u0DD0\u0DAF\u0DD9\u0DB1\u0DCA\u0DB1.",
    "ta": "\u0BA4\u0BAF\u0BB5\u0BC1\u0B9A\u0BC6\u0BAF\u0BCD\u0BA4\u0BC1 \u0B95\u0BBE\u0BA4\u0BCD\u0BA4\u0BBF\u0BB0\u0BC1\u0B99\u0BCD\u0B95\u0BB3\u0BCD.",
}

# ---------------------------------------------------------------------------
# IVR language configurations
# ---------------------------------------------------------------------------
# Maps DTMF digit â†’ language code
# Menu currently offers English (ConversationRelay) and Arabic (Media Streams).
# Sinhala/Tamil remain fully implemented below but are not surfaced in the menu.
DIGIT_TO_LANG: dict[str, str] = {"1": "en", "2": "ar"}

# Website demo routing: BookDemo.tsx agent ids → that agent's public host.
# All demos mint tokens for one shared TwiML app whose voiceUrl is THIS
# server's /voice/demo-incoming; non-Hatton agents are <Redirect>ed to their
# own /voice/demo-incoming (see voice_demo_incoming). 'hatton' stays local.
DEMO_AGENT_HOSTS: dict[str, str] = {
    "kitchened": os.getenv("DEMO_HOST_KITCHENED", "kitchened.taskforceai.tech"),
    "worldofrefrigerators": os.getenv(
        "DEMO_HOST_WOR", "worldofrefrigerators.taskforceai.tech"),
    # Star Properties (Amaya) is served by the Rodrigo Realtors agent, whose
    # deployment is still named "flico" for historical reasons.
    "starproperties": os.getenv(
        "DEMO_HOST_STARPROPERTIES", "flico.taskforceai.tech"),
}

# Per-language ConversationRelay TwiML configuration
LANGUAGE_CONFIGS: dict[str, dict[str, str]] = {
    "en": {
        "tts_provider": "ElevenLabs",
        # English ConversationRelay voice — configurable via env (deploy sets CR_VOICE_EN)
        "voice": os.getenv("CR_VOICE_EN", "bm3QvaZ3fUSCRBC3UV1f-flash_v2_5"),
        "language": "en-US",
        "welcome_greeting": "Welcome to Hatton Hills! I'm Tanya, how can I help you today?",
        "extra_attrs": '        elevenlabsTextNormalization="on"\n',
    },
    "ru": {
        "tts_provider": "ElevenLabs",
        # Russian ConversationRelay voice — configurable via env (CR_VOICE_RU).
        # Twilio ConversationRelay natively supports ru-RU (TTS + transcription),
        # so Russian rides CR like English — no Media Streams / own-STT needed.
        "voice": os.getenv("CR_VOICE_RU", "YjESejviApN7SHrbfnA2-flash_v2_5"),
        "language": "ru-RU",
        # Google's "telephony" model doesn't support ru-RU (Twilio 64101); use the
        # multilingual "long" (latest_long) model for Russian transcription.
        "transcription_provider": "google",
        "speech_model": os.getenv("CR_SPEECH_MODEL_RU", "long"),
        "welcome_greeting": (
            "Добро пожаловать в Hatton Hills! Меня зовут Таня, "
            "чем я могу вам помочь сегодня?"
        ),
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
# Media Streams â€” Azure TTS + Google STT (Sinhala / Tamil)
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
    "en": [
        "Hello, are you still there?",
        "Welcome to Hatton Hills. How may I help you today?",
    ],
    "ar": [
        # "Hello, are you still there?"
        "مرحباً، هل ما زلتم على الخط؟",
        # Full welcome re-greet
        "أهلاً بكم في Hatton Hills. كيف يمكنني مساعدتكم اليوم؟",
    ],
    "si": [
        # "Hello, are you still there?"
        (
            "\u0D86\u0DBA\u0DD4\u0DB6\u0DDD\u0DC0\u0DB1\u0DCA, "
            "\u0D94\u0DB6 \u0DAD\u0DC0\u0DB8\u0DAD\u0DCA "
            "\u0DC3\u0DD2\u0DA7\u0DD2\u0DB1\u0DCA\u0DB1\u0DDA\u0DAF?"
        ),
        # Full welcome re-greet
        (
            "\u0D86\u0DBA\u0DD4\u0DB6\u0DDD\u0DC0\u0DB1\u0DCA! "
            "Hatton Hills \u0DC0\u0DD9\u0DAD "
            "\u0DC3\u0DCF\u0DAF\u0DBB\u0DBA\u0DD9\u0DB1\u0DCA "
            "\u0DB4\u0DD2\u0DC5\u0DD2\u0D9C\u0DB1\u0DD2\u0DB8\u0DD4. "
            "\u0DB8\u0DA7 \u0D94\u0DB6\u0DA7 "
            "\u0D9A\u0DD9\u0DC3\u0DDA "
            "\u0D8B\u0DAF\u0DC0\u0DCA "
            "\u0D9A\u0DC5 \u0DC4\u0DD0\u0D9A\u0DD2\u0DAF?"
        ),
    ],
    "ta": [
        # "Hello, are you still there?"
        (
            "\u0BB5\u0BA3\u0B95\u0BCD\u0B95\u0BAE\u0BCD, "
            "\u0BA8\u0BC0\u0B99\u0BCD\u0B95\u0BB3\u0BCD "
            "\u0B87\u0BA9\u0BCD\u0BA9\u0BC1\u0BAE\u0BCD "
            "\u0B87\u0BB0\u0BC1\u0B95\u0BCD\u0B95\u0BBF\u0BB1\u0BC0\u0BB0\u0BCD\u0B95\u0BB3\u0BBE?"
        ),
        # Full welcome re-greet
        (
            "\u0BB5\u0BA3\u0B95\u0BCD\u0B95\u0BAE\u0BCD! "
            "Hatton Hills \u0B95\u0BCD\u0B95\u0BC1 "
            "\u0BB5\u0BB0\u0BB5\u0BC7\u0BB1\u0BCD\u0B95\u0BBF\u0BB1\u0BCB\u0BAE\u0BCD. "
            "\u0BA8\u0BBE\u0BA9\u0BCD "
            "\u0B89\u0B99\u0BCD\u0B95\u0BB3\u0BC1\u0B95\u0BCD\u0B95\u0BC1 "
            "\u0B8E\u0BAA\u0BCD\u0BAA\u0B9F\u0BBF "
            "\u0B89\u0BA4\u0BB5\u0BB2\u0BBE\u0BAE\u0BCD?"
        ),
    ],
}

# Welcome greetings for Media Streams (spoken via ElevenLabs/Azure TTS on stream start)
MEDIA_STREAM_WELCOME: dict[str, str] = {
    "ar": (
        "أهلاً وسهلاً بكم في Hatton Hills! "
        "أنا تانيا، كيف يمكنني مساعدتكم اليوم؟"
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
    need to auto-detect â€” it responds exclusively in the chosen language.
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
    elif lang == "ru":
        language_rules = (
            "LANGUAGE RULES:\n"
            "- The caller selected Russian. You MUST respond entirely in "
            "Russian using native Cyrillic script "
            "(e.g. 'Добро пожаловать! Чем я могу вам помочь сегодня?').\n"
            "- NEVER use romanized Latin script for Russian words.\n"
            "- NEVER respond in English unless the guest explicitly switches "
            "to English.\n"
            "- Use proper Russian grammar and a natural, courteous "
            "conversational tone.\n\n"
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
            "- If you do NOT know the answer to a guest's question, or the request is outside what you can help with (e.g. complex booking changes, special packages, complaints, anything not covered by Hatton Hills booking/general info), PROACTIVELY offer to transfer them to a human team member. Say something like 'I don't have that information on hand â€” would you like me to connect you with one of our team members who can help?' Wait for the guest to say yes before calling transfer_to_human. If they say no, continue helping them with what you can.\n"
            "- Do NOT guess or make up answers just to avoid a transfer. Honesty + a quick handoff offer beats a wrong answer.\n\n"
        )
    else:
        handoff_rules = ""

    # The welcome greeting is delivered by Twilio (English ConversationRelay) or
    # spoken on stream start (Media Streams). Tell Claude not to repeat it,
    # without asserting an English greeting for non-English callers.
    if lang == "en":
        greeting_note = (
            "The caller has already heard your greeting: 'Welcome to Hatton "
            "Hills! I'm Tanya, how can I help you today?' — do NOT "
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
        f"You are Tanya, the warm and gracious reservations voice agent for "
        f"Hatton Hills, Sri Lanka.\n"
        f"Today's date is {today}.\n\n"

        + greeting_note
        + language_rules +
        handoff_rules +

        "VOICE RULES (you are speaking on a phone call, not writing text):\n"
        "- Keep every response to one or two short sentences.\n"
        "- Never use markdown, bullet points, numbered lists, asterisks, or URLs.\n"
        "- Use natural spoken language. Say numbers as words.\n"
        "- Do not use abbreviations. Say 'rupees' not 'LKR'.\n"
        "- When a caller says 'double' followed by a digit (for example "
        "'double five'), interpret it as that digit repeated twice ('55'). "
        "Likewise 'triple seven' means '777'. This is common when callers "
        "read out phone numbers. Apply the same rule if the equivalent word "
        "is said in Sinhala or Tamil.\n"
        "- Never read out full rate lists. Mention only the relevant room.\n"
        "- Pause naturally between ideas by using short sentences.\n\n"

        "IMPORTANT RULES:\n"
        "- For general questions about room types, prices, amenities, policies, "
        "activities, or hotel info, answer directly from the hotel information "
        "provided in context. Do NOT ask for dates or call any tool for general "
        "info questions.\n"
        "- Only use the check_availability tool when the guest wants to actually "
        "BOOK a room or specifically asks if rooms are available on certain dates.\n"
        "- When a guest expresses booking intent, collect only what is needed to "
        "check availability: check-in and check-out dates, and number of guests "
        "(adults and children with ages). Ask ONE question at a time. Do NOT "
        "ask for residency, the guest's name, mobile, or email at this stage. "
        "Do NOT ask for any salutation or title (no Mr / Mrs / Ms / Dr).\n"
        "- CHILDREN UNDER 11: if the guest already stated the party is "
        "only adults (e.g. '2 adults', 'just the two of us'), do NOT ask "
        "again about children â€” accept it and move on with num_children=0. "
        "Only ask 'Are there any children under eleven in your party?' if "
        "the guest gave an ambiguous count (e.g. '4 people' without "
        "specifying adults vs children). Children under 11 affect pricing, "
        "so if there is genuine ambiguity you must clarify, but never "
        "repeat a question the guest already answered.\n"
        "- ROOM COUNT IMPLIES OCCUPANCY â€” DO NOT ASK FOR A HEADCOUNT YOU "
        "CAN ALREADY WORK OUT: a 'double room' means double occupancy, i.e. "
        "two guests. If the guest states a number of rooms by occupancy "
        "(e.g. 'two double rooms'), infer the total guests yourself rather "
        "than asking 'how many guests in total' â€” two double rooms is four "
        "adults. Briefly confirm the figure you derived instead of asking "
        "open-endedly, e.g. 'That's four adults across two double rooms â€” "
        "is that right?'. Only ask for an explicit guest count when it is "
        "genuinely ambiguous â€” for example a chalet (which holds up to "
        "five) where the party size is not implied.\n"
        "- KB IS THE SOURCE OF TRUTH FOR ROOM FACTS: the PMS (via the "
        "check_availability tool) is used ONLY to find out which rooms "
        "are free for the requested dates, and later to create the "
        "booking. EVERYTHING ELSE â€” capacity, rates, descriptions, "
        "amenities, policies â€” comes from the hotel information in "
        "context (the knowledge base). The tool result only tells you "
        "the room name and whether it is available; never quote a rate, "
        "capacity, or feature from the tool. For reference: Forest "
        "Escape, Eco Harmony, and Sunrise Vista are suites for up to 2 "
        "pax; Mount Luxe and Mount Monarch are chalets for up to 5 pax.\n"
        "- AVAILABILITY CHECK â€” STRICT SINGLE-CALL RULE: as soon as you "
        "have dates and pax, call check_availability EXACTLY "
        "ONCE. NEVER pass a room_type filter, even if the guest already "
        "mentioned a room they like â€” the tool returns ALL room types in "
        "one response. After that single call, read the response and "
        "surface every available type in one sentence, e.g. 'Eco Harmony "
        "and Sunrise Vista are available for those dates â€” which would "
        "you prefer?' Calling check_availability a second time in the "
        "same booking flow (e.g. once per room type, or because the "
        "guest changed their mind) is FORBIDDEN unless the guest changes "
        "their dates or pax. If the guest just picks a different room "
        "from the list you already have, do NOT call the tool again â€” "
        "you already know the answer.\n"
        "- After check_availability returns, share the available room names "
        "with the guest (do NOT quote any prices yet) and ask which room "
        "they would like.\n"
        "- RESIDENCY QUESTION â€” ASK ONLY WHEN QUOTING PRICES: once the "
        "guest has picked a room, ask whether they are a Sri Lankan "
        "resident or a foreign guest BEFORE quoting the rate. This is "
        "essential â€” we have two completely different rate sheets (local "
        "resident rates in LKR, and foreigner rates in USD). Quote rates "
        "ONLY from the rate sheet that matches their residency.\n"
        "- Once the guest has told you their residency, NEVER ask again "
        "and NEVER forget it. Anchor every subsequent rate, supplement, "
        "and currency mention to their residency: foreign guest â†’ USD, "
        "Sri Lankan resident â†’ LKR. If you ever find yourself about to "
        "quote a rate, silently verify which rate sheet applies before "
        "speaking.\n"
        "- After quoting the rate and the guest confirms they are happy "
        "to proceed, begin collecting their personal details, ONE "
        "question at a time, in this order: full name (no salutation), "
        "then mobile number. Do NOT ask for an email address at any "
        "point â€” we do not collect email.\n"
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
        "again. Whatever the answer, say: 'Thank you — I've got that noted "
        "down.' Then proceed to the mobile number. A booking with a "
        "best-effort name is better than trapping the guest in a repeat "
        "loop.\n"
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
        "(name, mobile, dates, room, residency, pax), do NOT silently "
        "replace it if they say a different value later in the same call. "
        "Instead, explicitly confirm the change: 'I have your name as Chris "
        "Fernando â€” did you mean to change it to TJ Pereira?' Only update "
        "the slot after the guest confirms the change. This prevents "
        "telephony lag or repeated speech from corrupting the booking.\n"
        "- SLOT DISAMBIGUATION RULE: match the guest's answer to the slot you "
        "just asked about. If you asked for the mobile number and the guest "
        "replies with letters/words (a name), do NOT overwrite the name â€” say "
        "'Sorry, I was asking for your mobile number â€” could you say the "
        "digits please?' If you asked for a name and the guest replies with "
        "digits, ask for the name again. If the guest repeats themselves "
        "(e.g. says 'pardon' or restates the same answer), treat it as a "
        "repeat, not a new value â€” confirm what you already captured.\n"
        "- For mobile number: NEVER ask the guest for a country code. If the "
        "guest is a Sri Lankan resident (default assumption), assume +94 "
        "yourself â€” accept whatever digits they say (with or without a "
        "leading zero) and silently treat it as a +94 number. If the guest "
        "is a foreign guest, ask which country they are calling from and "
        "you add the country code yourself based on that country. Under no "
        "circumstances should you ask the caller to dictate the country "
        "code digits. If the number you heard sounds incomplete, only ask "
        "them to repeat the local number â€” never the country code.\n"
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
        "perks and then go silent â€” that leaves the caller hanging. If "
        "you have just surfaced available rooms, end with 'which would "
        "you like to proceed with?'. If the guest has picked a room, end "
        "with 'shall I go ahead and book Sunrise Vista for you?' (or the "
        "chosen room). If the guest has confirmed they want to proceed, "
        "end with 'may I have your full name please?'. Mentions of "
        "complimentary activities, advance-payment notes, and honeymoon "
        "perks belong in a SHORT prefix before the question â€” not as the "
        "final sentence. The only exceptions are the post-create_booking "
        "reference read-back and the closing line at the very end of the "
        "call.\n"
        "- Never call create_booking unless check_availability already returned "
        "available=true for the chosen room and dates in this same call AND the "
        "guest has confirmed they want to proceed.\n"
        "- Before calling create_booking, read back the full booking summary "
        "(residency, guest name, dates, room, number of guests, mobile) and "
        "get explicit confirmation (e.g. 'shall I confirm this booking?'). "
        "Only after the guest says yes, call create_booking.\n"
        "- When create_booking returns success=true, confirm the booking "
        "is done, read the booking reference number once, and tell them "
        "they will also receive a WhatsApp confirmation shortly with all "
        "the details.\n"
        "- If create_booking returns an error or times out, apologise and tell "
        "the guest the hotel will call them back to confirm the booking. Do "
        "NOT retry create_booking automatically.\n"
        "- Always mention that nature walks, night walks, and stargazing are "
        "complimentary for stays of 2 or more nights.\n"
        "- If April or December dates are mentioned, note that 50% advance "
        "payment is required.\n"
        "- HONEYMOON / ANNIVERSARY UPSELL: if the guest mentions a "
        "honeymoon, anniversary, birthday celebration, or proposal, you "
        "MUST in your VERY NEXT sentence mention our complimentary "
        "candlelit dinner package and offer to add it to the booking. "
        "Do not save this for later â€” say it immediately after "
        "congratulating them. Example: 'Congratulations! For honeymoon "
        "stays, we offer a complimentary candlelit dinner â€” would you "
        "like me to arrange that for one of your nights?' Skipping this "
        "is a missed opportunity and is not acceptable.\n"
        "- Be empathetic and attentive. If a guest seems frustrated, acknowledge "
        "their feelings.\n"
        "- THREE-STRIKES EXIT: if you have asked the same clarifying question "
        "three turns in a row without making progress, OR the caller is "
        "clearly off-topic, abusive, or testing the system, do NOT keep "
        "engaging. Politely say something like 'It seems we're having "
        "trouble connecting today â€” please feel free to call back when "
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
        "but DO NOT skip the dates / pax / residency steps. When you "
        "later reach the personal-details step, refer to what they "
        "already told you â€” do NOT silently re-ask the same question as "
        "if you had never heard the answer. Confirm: 'Just to confirm, "
        "your name is Chris Fernando, correct?' This makes the call feel "
        "human, not robotic.\n"
        "- If the caller mentions dates or a time period, confirm the exact "
        "check-in and check-out dates.\n"
        "- Before ending the call, briefly summarize what was discussed and "
        "any next steps.\n"
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
        logger.warning("Knowledge base initialization failed â€” continuing without KB.")

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
        logger.warning("ELEVENLABS_API_KEY or ELEVENLABS_VOICE_ID not set â€” "
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
            "[handoff] Twilio REST client NOT configured â€” handoff will fail"
        )

    logger.info("Server startup complete. eZee configured: %s", is_configured())

    yield

    # --- Shutdown ---
    logger.info("Shutting down server...")
    await close_session()
    logger.info("Shutdown complete.")


# ---------------------------------------------------------------------------
# FastAPI application
# ---------------------------------------------------------------------------
app = FastAPI(
    title="Hatton Hills Voice Agent (Tanya)",
    version="1.0.0",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# CORS — the marketing-site "Call Now" widget mints a Twilio AccessToken via
# GET /api/voice-token. Lock allowed origins to the TaskforceAI domains only,
# and restrict methods to GET/POST/OPTIONS so existing webhook/WS routes are
# unaffected.
# ---------------------------------------------------------------------------
import uuid as _uuid
import time as _time
from collections import defaultdict as _defaultdict
from fastapi.middleware.cors import CORSMiddleware
from fastapi import HTTPException as _HTTPException
from twilio.jwt.access_token import AccessToken
from twilio.jwt.access_token.grants import VoiceGrant

_CORS_ALLOWED_ORIGINS = [
    "https://taskforceai.tech",
    "https://www.taskforceai.tech",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_CORS_ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Twilio Voice AccessToken endpoint (browser softphone / "Call Now" widget)
# ---------------------------------------------------------------------------
# Lightweight in-process per-IP rate limit + active-token cap. This is a
# single-worker uvicorn deployment (see Dockerfile), so a module-level dict is
# sufficient — it does not need to survive restarts or coordinate across
# containers.
_VOICE_TOKEN_RATE: dict[str, list] = _defaultdict(list)
_VOICE_TOKEN_MAX_PER_MIN = 5      # max tokens minted per IP per 60s window
_VOICE_TOKEN_WINDOW = 60.0        # seconds


def _voice_token_rate_check(client_ip: str) -> None:
    """Per-IP sliding-window rate limit. Raises HTTP 429 when exceeded."""
    now = _time.monotonic()
    hits = _VOICE_TOKEN_RATE[client_ip]
    cutoff = now - _VOICE_TOKEN_WINDOW
    hits[:] = [t for t in hits if t > cutoff]
    if len(hits) >= _VOICE_TOKEN_MAX_PER_MIN:
        raise _HTTPException(status_code=429, detail="Too many token requests. Please wait a moment.")
    hits.append(now)


@app.get("/api/voice-token")
async def voice_token(request: Request):
    """Mint a short-lived Twilio Voice AccessToken for a browser softphone.

    This is the standard Twilio AccessToken pattern (twilio-python 7.x): an
    AccessToken carrying a VoiceGrant whose `outgoing_application_sid` points at
    a TwiML App SID. In 7.x `to_jwt()` returns a `str` — do NOT call `.decode()`.
    Tokens are short-lived (300s TTL) and outgoing-only (incoming disabled).
    """
    # Per-IP rate limit + concurrency cap (lightweight, in-process).
    client_ip = request.client.host if request.client else "unknown"
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        client_ip = fwd.split(",")[0].strip()
    _voice_token_rate_check(client_ip)

    account_sid = os.getenv("TWILIO_ACCOUNT_SID")
    api_key_sid = os.getenv("TWILIO_API_KEY_SID")
    api_key_secret = os.getenv("TWILIO_API_KEY_SECRET")
    app_sid = os.getenv("TWILIO_TWIML_APP_SID")
    if not all([account_sid, api_key_sid, api_key_secret, app_sid]):
        raise _HTTPException(status_code=503, detail="Voice token service not configured.")

    identity = f"demo-{_uuid.uuid4().hex[:12]}"
    # Twilio AccessToken pattern: VoiceGrant + outgoing_application_sid.
    token = AccessToken(account_sid, api_key_sid, api_key_secret, identity=identity, ttl=300)
    token.add_grant(VoiceGrant(outgoing_application_sid=app_sid, incoming_allow=False))
    # twilio 7.x: to_jwt() already returns str — do NOT .decode()
    return {"token": token.to_jwt(), "identity": identity}


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

    Connects the caller straight to the English ConversationRelay agent â€”
    no IVR / language menu.
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

    # IVR language menu: 1 = English (ConversationRelay), 2 = Arabic (Media
    # Streams). If the caller presses nothing, fall through to the English
    # agent (preserves prior straight-to-English behavior for silent callers).
    gather = (
        f'  <Gather numDigits="1" action="https://{host}/voice/language-selected"'
        ' method="POST" timeout="6">\n'
        '    <Say voice="Polly.Joanna">Welcome to Hatton Hills. '
        'For English, press 1.</Say>\n'
        '    <Say voice="Polly.Zeina">للغة العربية، اضغط اثنين.</Say>\n'
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

    logger.info("Incoming call from %s â€” presenting EN/AR language menu",
                request.headers.get("x-forwarded-for", "unknown"))

    return Response(content=twiml, media_type="application/xml")


# ---------------------------------------------------------------------------
# Browser demo entry — straight to English ConversationRelay, no IVR
# ---------------------------------------------------------------------------

@app.api_route("/voice/demo-incoming", methods=["GET", "POST"])
async def voice_demo_incoming(request: Request) -> Response:
    """Browser demo entry — language-aware, no IVR.

    The website softphone (Twilio Voice SDK) passes a ``lang`` param via
    ``Device.connect({params:{lang}})``; Twilio forwards it to this TwiML-app
    voiceUrl as a POST form field (or query string for GET). Routing mirrors
    the phone paths but skips the <Gather> menu:

      - ``en`` (default / unknown) → English ConversationRelay (Tanya, ElevenLabs).
      - ``ru`` → Russian ConversationRelay: Twilio CR natively supports ``ru-RU``
        (TTS + Twilio-managed transcription), so Russian rides CR like English.
      - ``ar`` → Media Streams (``/ws/media-stream/ar``): Twilio ConversationRelay
        has no Arabic locale, so Arabic rides the Media Streams path where we own
        STT (``ar-SA``) and TTS (ElevenLabs multilingual), like the IVR "press 2" flow.
      - ``si`` → Media Streams (``/ws/media-stream/si``): same reasoning as Arabic —
        no Sinhala locale in ConversationRelay. STT via Azure (``si-LK``), TTS via
        OpenAI ``gpt-4o-mini-tts`` (voice ``sage``).
    """
    host = request.headers.get("host", request.url.hostname or "localhost")

    # lang/agent can arrive as query params (GET) or form fields (POST).
    lang = (request.query_params.get("lang") or "").strip().lower()
    agent = (request.query_params.get("agent") or "").strip().lower()
    if request.method == "POST" and not (lang and agent):
        try:
            form = await request.form()
            lang = lang or str(form.get("lang", "")).strip().lower()
            agent = agent or str(form.get("agent", "")).strip().lower()
        except Exception:
            pass
    # (2026-07-02: "si" was temporarily excluded here after a mojibake corruption
    # was found in SLOW_RESPONSE_FILLERS/REPROMPT_MESSAGES["si"]; fixed in source
    # same day. Re-enabled 2026-07-03 after the fix was smoke-tested.)
    if lang not in ("en", "ar", "ru", "si"):
        lang = "en"

    # All three website demo agents share ONE TwiML app whose voiceUrl points
    # here, and the site passes the chosen agent via Device.connect params
    # (Twilio forwards them as POST fields on this FIRST webhook only). For a
    # non-Hatton agent, hand the call to that agent's own demo endpoint with a
    # <Redirect> — lang goes in the query string because Twilio does NOT
    # re-send custom connect params on redirected webhooks.
    other = DEMO_AGENT_HOSTS.get(agent)
    if other and other != host:
        twiml = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            "<Response>\n"
            f'  <Redirect method="POST">https://{other}/voice/demo-incoming?lang={lang}</Redirect>\n'
            "</Response>"
        )
        logger.info("Demo incoming call from %s — redirecting to agent %r (%s)",
                    request.headers.get("x-forwarded-for", "unknown"), agent, other)
        return Response(content=twiml, media_type="application/xml")

    if lang in ("ar", "si"):
        # Arabic/Sinhala — Media Streams (we own STT/TTS); ConversationRelay has
        # no ar/si locale, so both ride the Media Streams path.
        twiml = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            "<Response>\n"
            "  <Connect>\n"
            f'    <Stream url="wss://{host}/ws/media-stream/{lang}" />\n'
            "  </Connect>\n"
            "</Response>"
        )
        mode = f"Media Streams ({lang})"
    else:
        # English or Russian — ConversationRelay (Twilio owns STT+TTS).
        config = LANGUAGE_CONFIGS[lang]
        cr = _build_conversation_relay_twiml(host, lang, config)
        twiml = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            "<Response>\n"
            f'  <Connect action="https://{host}/voice/relay-action" method="POST">\n'
            f"    {cr}\n"
            "  </Connect>\n"
            "</Response>"
        )
        mode = f"ConversationRelay ({lang})"

    logger.info("Demo incoming call from %s â€” %s",
                request.headers.get("x-forwarded-for", "unknown"), mode)

    return Response(content=twiml, media_type="application/xml")


# ---------------------------------------------------------------------------
# Language selection handler (called by Twilio after DTMF digit)
# ---------------------------------------------------------------------------

@app.post("/voice/language-selected")
async def voice_language_selected(request: Request) -> Response:
    """Handle the caller's DTMF language selection.

    English (1) â†’ ConversationRelay TwiML (ElevenLabs TTS, text-in/text-out).
    Sinhala (2) / Tamil (3) â†’ Media Streams TwiML (Azure TTS, Google STT,
    full audio control).
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
        # English â€” ConversationRelay with ElevenLabs
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
        # Sinhala / Tamil â€” Media Streams with Azure TTS + Google STT
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
        "Language selected: %s (digit: %s) â€” returning %s TwiML",
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
                    # Double-encoded â€” try one more parse
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
        # Legacy Path A fallback â€” dashboard event is now sent from
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
        twiml = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            "<Response>\n"
            f'  <Dial action="{dial_action_url}" method="POST" timeout="20" answerOnBridge="true">\n'
            f'    <Number url="{whisper_url}">{HUMAN_AGENT_PHONE}</Number>\n'
            "  </Dial>\n"
            "</Response>"
        )
        return Response(content=twiml, media_type="application/xml")

    logger.info(
        "[handoff] relay-action with no transfer (call_sid=%s, action=%r) â€” hanging up",
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


@app.post("/voice/dial-result")
async def dial_result(request: Request) -> Response:
    """Callback from <Dial action>. If the human answered â†’ hang up.
    Otherwise, drop the caller back into Tanya with a recovery greeting.
    """
    form = await request.form()
    status = form.get("DialCallStatus", "")
    host = request.url.hostname
    logger.info("[handoff] dial-result status=%s", status)
    if status in ("completed", "answered"):
        return Response(
            content='<?xml version="1.0" encoding="UTF-8"?><Response><Hangup/></Response>',
            media_type="application/xml",
        )

    # No answer / busy / failed / canceled â†’ recover into Tanya with a
    # one-off greeting. We build the ConversationRelay TwiML by reusing the
    # standard helper but swapping in the apology greeting.
    recovery_config = dict(LANGUAGE_CONFIGS["en"])
    recovery_config["welcome_greeting"] = (
        "Sorry, no agent was available. I'm Tanya, how can I help?"
    )
    cr_tag = _build_conversation_relay_twiml(host, "en", recovery_config)
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
    host: str, lang: str, config: dict[str, str]
) -> str:
    """Build the <ConversationRelay> XML tag for the given language config."""
    extra = config["extra_attrs"]
    # XML-escape the welcome greeting in case it contains special characters
    greeting = xml.sax.saxutils.escape(config["welcome_greeting"])
    # Transcription provider/model are per-language: Google's "telephony" model is
    # English-optimized and is REJECTED for many locales (Twilio error 64101,
    # e.g. ru-RU), so non-English languages set a multilingual model like "long"
    # (Google latest_long). Defaults preserve the original en behavior.
    transcription_provider = config.get("transcription_provider", "google")
    speech_model = config.get("speech_model", "telephony")

    return (
        f'<ConversationRelay url="wss://{host}/ws/conversation?lang={lang}"\n'
        f'        ttsProvider="{config["tts_provider"]}"\n'
        f'        voice="{config["voice"]}"\n'
        f'{extra}'
        f'        language="{config["language"]}"\n'
        f'        transcriptionProvider="{transcription_provider}"\n'
        f'        speechModel="{speech_model}"\n'
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
# Google Cloud STT â€” streaming (background thread, Media Streams only)
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
            logger.error("Cannot start STT â€” google-cloud-speech not installed")
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
                logger.warning("STT stream ended (%s) â€” restarting...", exc, exc_info=True)

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
        logger.info("STT gRPC stream connected â€” waiting for speech...")
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
    """Streams audio to Azure Speech-to-Text â€” drop-in alternative to GoogleSTTStream.

    Mirrors the same interface (start/stop/feed + on_final_result / on_interim_result
    callbacks fired from background threads) so it swaps in via the STT_PROVIDER env var.

    Twilio delivers mulaw 8 kHz; Azure's PushAudioInputStream wants PCM, so each fed
    chunk is decoded mulaw â†’ PCM16 (audioop) before being written. Uses a fixed
    language per call (si-LK / ta-IN) â€” for a Sinhala-only line that tends to beat
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
            logger.error("Cannot start Azure STT â€” azure-cognitiveservices-speech not installed")
            return
        if audioop is None:
            logger.error("Cannot start Azure STT â€” audioop unavailable (install audioop-lts on 3.13+)")
            return
        if not AZURE_SPEECH_KEY:
            logger.error("Cannot start Azure STT â€” AZURE_SPEECH_KEY not set")
            return

        primary = STT_PRIMARY.get(self._lang, "si-LK")
        speech_config = azure_speech.SpeechConfig(
            subscription=AZURE_SPEECH_KEY, region=AZURE_SPEECH_REGION,
        )
        speech_config.speech_recognition_language = primary
        # 8 kHz / 16-bit / mono PCM â€” what mulaw decodes to.
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
        logger.error("STT_PROVIDER=azure but Azure STT unavailable â€” falling back to Google")
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
                        "Media stream started â€” Call: %s, Stream: %s, lang: %s, phone: %s",
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
                        logger.info("TTS done â€” listening for guest speech [%s]", self.call_sid)
                        # Agent just finished speaking â€” arm the no-speech nudge.
                        self._schedule_reprompt()

                elif event == "stop":
                    logger.info("Media stream stopped â€” Call: %s", self.call_sid)
                    break

        except WebSocketDisconnect:
            logger.info("Media stream disconnected â€” Call: %s", self.call_sid)
        except Exception:
            logger.exception("Media stream error â€” Call: %s", self.call_sid)
        finally:
            self._cancel_reprompt()
            if self._stt:
                self._stt.stop()
            self._write_audio_dump()
            if self._endpointing_handle:
                self._endpointing_handle.cancel()
            call_end_time = datetime.now().isoformat()
            logger.info(
                "Media stream session ended â€” Call: %s, history: %d, transcript: %d msgs",
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
        self._latest_interim = ""  # clear â€” final supersedes interim
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
            logger.warning("Cannot write audio dump â€” audioop unavailable")
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
                # Agent is talking â€” re-arm after it finishes.
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
        # Caller is speaking â€” cancel any pending silence nudge and reset
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
        # Caller is speaking â€” cancel any pending silence nudge and reset
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

            # No tools â€” flush remaining sentence buffer
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
                "Gemini round %d [%s] â€” text=%d chars, tools=%d, finish=%s",
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

            # No tools â€” flush remaining sentence buffer
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

            # No tools â€” flush remaining sentence buffer
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
        Sinhala        â†’ OpenAI gpt-4o-mini-tts (mulaw 8k)
        """
        async with self._speak_lock:
            if generation >= 0 and generation != self._speak_generation:
                return
            if self.lang == "si":
                await self._tts_openai(text)
            elif self.lang in ("ta", "ar"):
                await self._tts_elevenlabs(text)
            else:
                lang_code, voice_name = AZURE_VOICES[self.lang]
                await self._tts_azure(text, lang_code, voice_name)

    # â”€â”€ ElevenLabs TTS (Tamil) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    async def _tts_elevenlabs(self, text: str):
        """Stream text via ElevenLabs eleven_multilingual_v2 and send mulaw audio to Twilio.
        Must only be called from _speak (lock already held).
        """
        if not ELEVENLABS_API_KEY or not ELEVENLABS_VOICE_ID:
            logger.warning("ElevenLabs not configured â€” skipping TTS")
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

    # â”€â”€ OpenAI TTS (Sinhala) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    async def _tts_openai(self, text: str):
        """Stream OpenAI gpt-4o-mini-tts as mulaw 8 kHz to Twilio (Sinhala).

        OpenAI returns raw 24 kHz 16-bit mono LE PCM; we downsample to 8 kHz and
        mulaw-encode on the fly so it drops straight into the same Twilio media
        framing the Tamil/Azure paths use.
        Must only be called from _speak (lock already held).

        # Ported from Flico Agent/server.py _tts_openai (OpenAI gpt-4o-mini-tts â†’ mulaw 8k)
        """
        if not OPENAI_API_KEY:
            logger.warning("OPENAI_API_KEY not set â€” skipping TTS")
            return

        self._is_speaking = True
        payload: dict[str, Any] = {
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
                        # PCM is 2 bytes/sample -- keep sample alignment.
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

    # â”€â”€ Azure TTS (Sinhala) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    async def _tts_azure(self, text: str, lang_code: str, voice_name: str):
        """Stream Azure Cognitive Services TTS as mulaw 8 kHz to Twilio.
        Must only be called from _speak (lock already held).
        """
        if not AZURE_SPEECH_KEY:
            logger.warning("AZURE_SPEECH_KEY not set â€” skipping TTS")
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
        full_response_text += text_content

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
                    # Convert args to dict â€” may be a proto MapComposite
                    args = dict(fc.args) if fc.args else {}
                    function_calls.append({"name": fc.name, "args": args})

        logger.info(
            "Gemini round %d done â€” text=%d chars, tools=%d, finish=%s",
            round_idx + 1, len(text_content), len(function_calls), finish_reason,
        )

        full_response_text += text_content

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

        # No tool calls â€” done
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
                "WebSocket closed mid-stream (%s) â€” draining Claude silently",
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
        full_response_text += text_content

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


# ---------------------------------------------------------------------------
# WebSocket â€” ConversationRelay handler
# ---------------------------------------------------------------------------

@app.websocket("/ws/conversation")
async def ws_conversation(websocket: WebSocket, lang: str = "en"):
    """Handle a Twilio ConversationRelay WebSocket session.

    The ``lang`` query parameter is set by the IVR routing and determines
    which language-specific system prompt Claude receives.

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

    await websocket.accept()
    logger.info("WebSocket connection accepted â€” language: %s", lang)

    # -- Per-session state --
    conversation_history: list[dict] = []
    system_prompt: str = _build_system_prompt(lang)
    if LLM_PROVIDER == "claude":
        tools: list[dict] = get_tools()
    elif LLM_PROVIDER == "gemini":
        tools: list[dict] = get_tools_gemini()
    else:
        tools: list[dict] = get_tools_openai()
    call_sid: str = "unknown"
    caller_phone: str = "unknown"
    full_transcript: list[dict[str, str]] = []
    call_start_time: str = datetime.now().isoformat()

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
        logger.error("LLM client not available â€” closing WebSocket")
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
                # Prefer the `from` field on the ConversationRelay setup message â€”
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
                    "Session setup â€” CallSid: %s, StreamSid: %s, Phone: %s",
                    call_sid,
                    message.get("streamSid", "n/a"),
                    caller_phone,
                )
                # Session state is already initialized above.
                # Log any additional setup metadata.
                logger.info(
                    "Session ready â€” system prompt length: %d chars, tools: %d",
                    len(system_prompt),
                    len(tools),
                )
                _schedule_reprompt()

            # ---------------------------------------------------------------
            # PROMPT â€” user speech transcribed
            # ---------------------------------------------------------------
            elif msg_type == "prompt":
                user_text = message.get("voicePrompt", "").strip()
                if not user_text:
                    logger.debug("Empty voicePrompt received â€” ignoring")
                    continue

                # Caller spoke â€” cancel any pending silence nudge and reset
                # the re-prompt counter.
                _cancel_reprompt()
                reprompt_count = 0

                logger.info("Guest [%s]: %s", call_sid, user_text)
                full_transcript.append({"role": "user", "text": user_text})

                # Retrieve KB context for this utterance
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
                    if LLM_PROVIDER == "claude":
                        response_text = await _run_llm_streaming_claude(
                            client=anthropic_client,
                            system=system_prompt,
                            conversation_history=conversation_history,
                            tools=tools_for_session,
                            websocket=websocket,
                            lang=lang,
                        )
                    elif LLM_PROVIDER == "gemini":
                        response_text = await _run_llm_streaming_gemini(
                            gemini_client=gemini_client,
                            system=system_prompt,
                            conversation_history=conversation_history,
                            tools=tools_for_session,
                            websocket=websocket,
                        )
                    else:
                        response_text = await _run_llm_streaming(
                            client=openai_client,
                            system=system_prompt,
                            conversation_history=conversation_history,
                            tools=tools_for_session,
                            websocket=websocket,
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
                            # First non-tool user message â€” stop scanning
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
                                "[handoff] cannot transfer â€” TWILIO_ACCOUNT_SID/"
                                "AUTH_TOKEN not configured; ending call"
                            )
                        elif not HUMAN_AGENT_PHONE:
                            logger.error(
                                "[handoff] cannot transfer â€” HUMAN_AGENT_PHONE "
                                "not set; ending call"
                            )
                        else:
                            from urllib.parse import quote as _quote
                            host = PUBLIC_HOSTNAME
                            reason_q = _quote(pending_transfer_reason)
                            twiml = (
                                '<?xml version="1.0" encoding="UTF-8"?>'
                                '<Response>'
                                '<Say voice="Polly.Joanna">Connecting you now. Please hold.</Say>'
                                f'<Dial action="https://{host}/voice/dial-result" method="POST" timeout="20" answerOnBridge="true">'
                                f'<Number url="https://{host}/voice/whisper?reason={reason_q}">{HUMAN_AGENT_PHONE}</Number>'
                                '</Dial>'
                                '</Response>'
                            )
                            try:
                                # Twilio REST client is sync â€” run in executor
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
                        "WebSocket disconnected during Claude streaming [%s] â€” "
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
            # INTERRUPT â€” user interrupted agent speech
            # ---------------------------------------------------------------
            elif msg_type == "interrupt":
                logger.info(
                    "Speech interrupted by guest [%s] â€” utteranceUntilInterrupt: '%s'",
                    call_sid,
                    message.get("utteranceUntilInterrupt", ""),
                )

            # ---------------------------------------------------------------
            # OTHER
            # ---------------------------------------------------------------
            else:
                logger.debug("Unhandled message type '%s' [%s]: %s", msg_type, call_sid, raw[:200])

    except WebSocketDisconnect:
        logger.info("WebSocket disconnected â€” CallSid: %s", call_sid)
    except Exception:
        logger.exception("Unexpected error in WebSocket handler [%s]", call_sid)
    finally:
        _cancel_reprompt()
        call_end_time = datetime.now().isoformat()
        logger.info(
            "Session ended â€” CallSid: %s, history: %d msgs, transcript: %d msgs",
            call_sid, len(conversation_history), len(full_transcript),
        )
        if full_transcript:
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
# WebSocket â€” Media Streams handler (Sinhala / Tamil)
# ---------------------------------------------------------------------------

@app.websocket("/ws/media-stream/{lang}")
async def ws_media_stream(websocket: WebSocket, lang: str):
    """Handle a Twilio Media Streams WebSocket session for Sinhala or Tamil.

    Language is encoded in the URL path (e.g. /ws/media-stream/si) so it
    is always present â€” avoids unreliable query-string passing by Twilio.

    Receives raw mulaw 8 kHz audio from Twilio, runs Google Cloud STT,
    sends Claude responses through Azure TTS back as mulaw audio.
    """
    if lang not in ("si", "ta", "ar"):
        lang = "si"

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
        logger.error("LLM client unavailable â€” closing Media Streams WebSocket")
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
