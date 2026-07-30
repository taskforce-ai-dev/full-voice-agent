"""
server.py -- Main FastAPI server for Flico Voice Agent.

Handles:
  - IVR / DTMF language menu (POST /voice/incoming)
  - Language routing (POST /voice/language-selected)
  - ConversationRelay WebSocket (/ws/conversation?lang=en)
  - Media Streams WebSocket (/ws/media-stream/{lang} for ta|si)
  - Streaming LLM responses
  - Knowledge-base context injection
  - Health endpoint (GET /health)

Architecture:
  Incoming call
    -> POST /voice/incoming -> TwiML <Gather> (press 1/2/3)
    -> POST /voice/language-selected -> ConversationRelay or Media Streams TwiML
    -> WebSocket /ws/conversation?lang=... or /ws/media-stream/{lang}
    -> LLM streaming
    -> text tokens -> Twilio TTS -> caller

  TTS routing by language:
    English  -> ElevenLabs (flash_v2_5, cloned voice) via ConversationRelay
    Tamil    -> ElevenLabs (eleven_multilingual_v2, cloned voice) via Media Streams
    Sinhala  -> Sinhala VITS self-hosted TTS service via Media Streams
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
    sentry_sdk.set_tag("agent", "flico")
import queue
import re
import threading
import xml.sax.saxutils
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from typing import Any

import httpx
from anthropic import AsyncAnthropic, NOT_GIVEN
from twilio.rest import Client as TwilioRestClient
from urllib.parse import quote
from openai import AsyncOpenAI
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import Response

from knowledge_base import retrieve_context, initialize_kb, prewarm, reload_kb_from_content, portfolio_facts
from supabase_client import (
    record_call_start,
    record_call_end,
    extract_caller_details,
    send_automation_webhook,
)
from post_call import process_realestate_post_call
from media_transport import MediaTransport, TwilioMediaTransport
from asterisk_ari import AsteriskAriClient
from asterisk_rtp import AsteriskRtpTransport, RtpPortAllocator
from brands import BRANDS, DEFAULT_BRAND, resolve_brand

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
# Distinct ConversationRelay voice for the Star Properties demo persona, so the
# two demos on the website do not sound like the same person. Falls back to the
# language default when unset.
CR_VOICE_STARPROPERTIES: str = os.getenv("CR_VOICE_STARPROPERTIES", "").strip()
ELEVENLABS_VOICE_SPEED: float = float(os.getenv("ELEVENLABS_VOICE_SPEED", "1.0"))
ELEVENLABS_MODEL_MULTILINGUAL: str = "eleven_multilingual_v2"
ELEVENLABS_MODEL_TURBO: str = "eleven_turbo_v2_5"
TWILIO_ACCOUNT_SID: str = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN: str = os.getenv("TWILIO_AUTH_TOKEN", "")
KB_DOCS_DIRECTORY: str = os.getenv("KB_DOCS_DIRECTORY", "knowledge_docs")
KB_RELOAD_SECRET: str = os.getenv("KB_RELOAD_SECRET", "")
PORT: int = int(os.getenv("PORT", "8000"))
ENABLE_ASTERISK_ARI: bool = os.getenv("ENABLE_ASTERISK_ARI", "false").lower() == "true"
ASTERISK_ARI_URL: str = os.getenv("ASTERISK_ARI_URL", "http://host.docker.internal:8088/ari")
ASTERISK_ARI_USER: str = os.getenv("ASTERISK_ARI_USER", "flico_ari")
ASTERISK_ARI_PASSWORD: str = os.getenv("ASTERISK_ARI_PASSWORD", "")
ASTERISK_APP_NAME: str = os.getenv("ASTERISK_APP_NAME", "flico-sip-agent")
ASTERISK_RTP_BIND_HOST: str = os.getenv("ASTERISK_RTP_BIND_HOST", "0.0.0.0")
ASTERISK_RTP_ADVERTISE_HOST: str = os.getenv("ASTERISK_RTP_ADVERTISE_HOST", "127.0.0.1")
ASTERISK_RTP_PORT_START: int = int(os.getenv("ASTERISK_RTP_PORT_START", "18000"))
ASTERISK_RTP_PORT_END: int = int(os.getenv("ASTERISK_RTP_PORT_END", "18100"))
ASTERISK_DEFAULT_LANG: str = os.getenv("ASTERISK_DEFAULT_LANG", "en")
AZURE_SPEECH_KEY: str = os.getenv("AZURE_SPEECH_KEY", "")
AZURE_SPEECH_REGION: str = os.getenv("AZURE_SPEECH_REGION", "southeastasia")
STT_PROVIDER: str = os.getenv("STT_PROVIDER", "google").lower()
# Self-hosted Sinhala VITS TTS service (legacy fallback -- no longer the
# active Sinhala path; resolvable via the shared docker network if revived).
SINHALA_TTS_URL: str = os.getenv("SINHALA_TTS_URL", "http://sinhala-tts:8000")

# OpenAI gpt-4o-mini-tts -- Fiona's Sinhala voice. Natural prosody, streams
# fast, and handles code-switched English far better than Azure or VITS.
OPENAI_TTS_URL: str = "https://api.openai.com/v1/audio/speech"
OPENAI_TTS_MODEL: str = os.getenv("OPENAI_TTS_MODEL", "gpt-4o-mini-tts")
OPENAI_TTS_VOICE: str = os.getenv("OPENAI_TTS_VOICE", "nova")
OPENAI_TTS_INSTRUCTIONS: str = os.getenv(
    "OPENAI_TTS_INSTRUCTIONS",
    "You are Fiona, a warm and personable young Sri Lankan property consultant "
    "at a real estate agency. Speak in natural, lively conversational "
    "Sinhala with genuine warmth and enthusiasm — smile as you talk. Vary "
    "your pitch and pace naturally: rise with excitement when describing "
    "properties, soften when being empathetic, and pause briefly between ideas. "
    "Sound like a real person chatting on the phone, not a robot reading text. "
    "Use a light, friendly tone with natural breathing pauses.",
)

# LLM provider selection: "claude" (default), "openai", or "gemini"
LLM_PROVIDER: str = os.getenv("LLM_PROVIDER", "claude")

# -- Human handoff (English ConversationRelay only) -------------------------
TWILIO_ACCOUNT_SID: str = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN: str = os.getenv("TWILIO_AUTH_TOKEN", "")
HUMAN_AGENT_PHONE: str = os.getenv("HUMAN_AGENT_PHONE", "").strip()
PUBLIC_HOSTNAME: str = os.getenv("PUBLIC_HOSTNAME", "flico.taskforceai.tech").strip()

TRANSFER_TOOL: dict = {
    "name": "transfer_to_human",
    "description": (
        "Transfer the live phone call to a human Rodrigo Realtors consultant. "
        "Call this ONLY when the caller explicitly asks to speak to a human, agent, "
        "consultant, manager, or real person, or agrees to be connected to one. Do "
        "not use it for routine property questions you can answer yourself."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "reason": {
                "type": "string",
                "description": "One short sentence summarising why the caller is being transferred.",
            },
        },
        "required": ["reason"],
    },
}


def _tools_for(lang: str, brand: str = DEFAULT_BRAND) -> list[dict]:
    """Tools available to a session. Brands with no human consultant behind
    them (transfer=False) must never be offered the handoff tool."""
    return [TRANSFER_TOOL] if (lang == "en" and resolve_brand(brand)["transfer"]) else []


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
    logger.warning("google-genai not installed -- native Gemini provider unavailable")

# ---------------------------------------------------------------------------
# Optional: Google Cloud Speech (Media Streams STT)
# ---------------------------------------------------------------------------
try:
    from google.cloud import speech_v1 as google_speech
    GOOGLE_STT_AVAILABLE = True
except ImportError:
    google_speech = None  # type: ignore[assignment]
    GOOGLE_STT_AVAILABLE = False
    logger.warning("google-cloud-speech not installed -- Media Streams STT unavailable")

# ---------------------------------------------------------------------------
# Optional: Azure Speech (alternative Media Streams STT, selected via STT_PROVIDER)
# ---------------------------------------------------------------------------
try:
    import azure.cognitiveservices.speech as azure_speech
    AZURE_STT_AVAILABLE = True
except ImportError:
    azure_speech = None  # type: ignore[assignment]
    AZURE_STT_AVAILABLE = False
    logger.warning("azure-cognitiveservices-speech not installed -- Azure STT provider unavailable")

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
MAX_HISTORY_MESSAGES: int = 20

# Sampling temperature. This was previously UNSET, i.e. the provider default of
# 1.0 -- maximum sampling variance for an agent whose entire job is reciting
# facts from the reference context faithfully. Every invention we have measured
# is a low-probability token winning: asked in Sinhala for the cheapest listing,
# Fiona got the property and rent right and then called it "අසාත්මික රහිත"
# (allergen-free) instead of "unfurnished". Lower temperature biases toward the
# high-probability -- i.e. the grounded -- rendering.
# Kept above 0 so she still sounds like a person rather than a lookup table.
# Tune via env without a rebuild; see evals/answer_eval.py before changing it.
LLM_TEMPERATURE: float = float(os.getenv("LLM_TEMPERATURE", "0.3"))

# ---------------------------------------------------------------------------
# IVR language configurations
# ---------------------------------------------------------------------------
# Maps DTMF digit -> language code
DIGIT_TO_LANG: dict[str, str] = {"1": "en", "2": "ta", "3": "si"}

# Per-language ConversationRelay TwiML configuration
LANGUAGE_CONFIGS: dict[str, dict[str, str]] = {
    "en": {
        "tts_provider": "ElevenLabs",
        "voice": "bm3QvaZ3fUSCRBC3UV1f-flash_v2_5",
        "language": "en-US",
        "welcome_greeting": "You have reached Rodrigo Realtors — you are speaking with our virtual property consultant. How can I help you today?",
        "extra_attrs": '        elevenlabsTextNormalization="on"\n',
    },
    "ta": {
        "tts_provider": "google",
        "voice": "ta-IN-Standard-A",
        "language": "ta-IN",
        "welcome_greeting": (
            "\u0BB5\u0BA3\u0B95\u0BCD\u0B95\u0BAE\u0BCD! "
            "Rodrigo Realtors \u0B95\u0BCD\u0B95\u0BC1 "
            "\u0BB5\u0BB0\u0BB5\u0BC7\u0BB1\u0BCD\u0B95\u0BBF\u0BB1\u0BCB\u0BAE\u0BCD. "
            "\u0BA8\u0BBE\u0BA9\u0BCD "
            "\u0B89\u0B99\u0BCD\u0B95\u0BB3\u0BC1\u0B95\u0BCD\u0B95\u0BC1 "
            "\u0B8E\u0BAA\u0BCD\u0BAA\u0B9F\u0BBF "
            "\u0B89\u0BA4\u0BB5\u0BB2\u0BBE\u0BAE\u0BCD?"
        ),
        "extra_attrs": "",
    },
    # Sinhala -- Media Streams + Google STT (si-LK) + self-hosted Sinhala VITS TTS.
    "si": {
        "tts_provider": "sinhala_vits",
        "voice": "sinhala-vits",
        "language": "si-LK",
        "welcome_greeting": (
            "\u0D86\u0DBA\u0DD4\u0DB6\u0DDD\u0DC0\u0DB1\u0DCA! \u0D94\u0DB6 Rodrigo Realtors \u0DC0\u0DD9\u0DAD \u0DC3\u0DB8\u0DCA\u0DB6\u0DB1\u0DCA\u0DB0 \u0DC0\u0DD3 \u0DC3\u0DD2\u0DA7\u0DD3. "
            "\u0DB8\u0DB8 \u0DC6\u0DD2\u0DBA\u0DDD\u0DB1\u0DCF, \u0D94\u0DB6\u0D9C\u0DDA \u0D85\u0DAD\u0DAE\u0DCA\u200D\u0DBA \u0DB4\u0DCF\u0DBB\u0DD2\u0DB7\u0DDD\u0D9C\u0DD2\u0D9A \u0DC3\u0DDA\u0DC0\u0DCF \u0DC3\u0DC4\u0DCF\u0DBA\u0DD2\u0D9A\u0DCF\u0DC0. "
            "\u0DB8\u0DB8 \u0D94\u0DB6\u0DA7 \u0D85\u0DAF \u0D9A\u0DD9\u0DC3\u0DDA \u0D8B\u0DAF\u0DC0\u0DCA \u0D9A\u0DC5 \u0DC4\u0DD0\u0D9A\u0DD2\u0DAF?"
        ),
        "extra_attrs": "",
    },
}

# ---------------------------------------------------------------------------
# Media Streams -- Azure TTS + Google STT (Tamil)
# ---------------------------------------------------------------------------
AZURE_TTS_URL = "https://{region}.tts.speech.microsoft.com/cognitiveservices/v1"
ELEVENLABS_TTS_URL = "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream"

# Azure voice per language code
AZURE_VOICES: dict[str, tuple[str, str]] = {
    "ta": ("ta-LK", "ta-LK-SaranyaNeural"),   # female voice
    "si": ("si-LK", "si-LK-ThiliniNeural"),   # female voice (Fiona)
}

# Google STT primary + alternative languages per lang code
STT_PRIMARY: dict[str, str] = {"en": "en-US", "ta": "ta-IN", "si": "si-LK"}
STT_ALTERNATIVES: dict[str, list[str]] = {
    "en": [],
    "ta": ["en-US"],
    "si": ["en-US"],
}

# Silence (seconds) after last STT result before utterance is considered complete
ENDPOINTING_SILENCE: float = 1.5

# Welcome greetings for Media Streams (spoken via TTS on stream start)
MEDIA_STREAM_WELCOME: dict[str, str] = {
    "en": "You have reached Rodrigo Realtors. How can I help you today?",
    "ta": (
        "\u0BB5\u0BA3\u0B95\u0BCD\u0B95\u0BAE\u0BCD! "
        "Rodrigo Realtors \u0B95\u0BCD\u0B95\u0BC1 "
        "\u0BB5\u0BB0\u0BB5\u0BC7\u0BB1\u0BCD\u0B95\u0BBF\u0BB1\u0BCB\u0BAE\u0BCD. "
        "\u0BA8\u0BBE\u0BA9\u0BCD \u0B89\u0B99\u0BCD\u0B95\u0BB3\u0BC1\u0B95\u0BCD\u0B95\u0BC1 "
        "\u0B8E\u0BAA\u0BCD\u0BAA\u0B9F\u0BBF \u0B89\u0BA4\u0BB5\u0BB2\u0BBE\u0BAE\u0BCD?"
    ),
    # Kept deliberately short: the Sinhala VITS TTS is non-streaming and CPU-bound,
    # so a long greeting means seconds of dead air at call start while it synthesizes.
    "si": "\u0D86\u0DBA\u0DD4\u0DB6\u0DDD\u0DC0\u0DB1\u0DCA! \u0DB8\u0DB8 \u0DBB\u0DDC\u0DA9\u0DCA\u200D\u0DBB\u0DD2\u0D9C\u0DDD \u0DBB\u0DD2\u0DBA\u0DBD\u0DCA\u0DA7\u0DBB\u0DCA\u0DC3\u0DCA \u0D86\u0DBA\u0DAD\u0DB1\u0DBA\u0DDA \u0DC6\u0DD2\u0DBA\u0DDD\u0DB1\u0DCF. \u0D94\u0DB6\u0DA7 \u0D9A\u0DDC\u0DC4\u0DDC\u0DB8\u0DAF \u0D8B\u0DAF\u0DC0\u0DCA \u0D9A\u0DBB\u0DB1\u0DCA\u0DB1\u0DDA?",
}

# Sentence boundary detection for streaming TTS
_SENTENCE_END = re.compile(r'(?<=[.!?\u0964\u0DF4])\s+')

# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

def _portfolio_facts_block(agency: str) -> str:
    """Portfolio claims derived from the live KB, or a safe fallback.

    Never let a prompt-building fault break a live call: an agent with no
    portfolio claims still works from the reference context. `agency` is
    forwarded so the generated facts name the brand actually calling.
    """
    try:
        facts = portfolio_facts(agency=agency)
    except Exception:
        logger.exception("portfolio_facts failed; falling back to no claims")
        facts = ""
    if facts:
        return facts
    return ("PORTFOLIO FACTS:\n"
            "- Describe ONLY properties that appear in the REFERENCE CONTEXT. If a "
            "caller asks for something that is not there, say we do not have it. "
            "Never state what the portfolio does or does not contain beyond what "
            "the reference context shows.\n\n")


def _build_system_prompt(lang: str = "en", brand: str = DEFAULT_BRAND) -> str:
    today = date.today().isoformat()
    _brand = resolve_brand(brand)
    agency = _brand["agency"]
    agent_name = _brand["agent"]
    greeting = _brand["greeting"].get(lang) or _brand["greeting"]["en"]

    if lang == "ta":
        language_rules = (
            "LANGUAGE RULES:\n"
            "- The caller selected Tamil. You MUST respond entirely in "
            "Tamil using native Unicode script "
            "(e.g. '\u0BB5\u0BA3\u0B95\u0BCD\u0B95\u0BAE\u0BCD! "
            "\u0BA8\u0BBE\u0BA9\u0BCD \u0B89\u0B99\u0BCD\u0B95\u0BB3\u0BC1\u0B95\u0BCD\u0B95\u0BC1 "
            "\u0B8E\u0BAA\u0BCD\u0BAA\u0B9F\u0BBF "
            "\u0B89\u0BA4\u0BB5\u0BB2\u0BBE\u0BAE\u0BCD?').\n"
            "- NEVER use romanized Latin script for Tamil words.\n"
            "- NEVER respond in English unless the caller explicitly switches "
            "to English.\n"
            "- Use proper Tamil grammar and a natural conversational tone.\n"
            "- TRANSLATE, NEVER EMBELLISH. The reference context is in English. "
            "Render ONLY what it actually says. Do NOT add any feature, adjective "
            "or descriptive word that is not in the reference context. If you are "
            "unsure of the Tamil word for something in the context, describe it "
            "plainly using words you are certain of, or leave it out entirely -- "
            "NEVER substitute a different feature. Inventing an attribute while "
            "translating is the same as lying to the caller about the property.\n\n"
        )
    elif lang == "si":
        language_rules = (
            "LANGUAGE RULES:\n"
            "- The caller selected Sinhala. Respond ENTIRELY in Sinhala using "
            "native Sinhala Unicode script.\n"
            "- CRITICAL: your reply is read aloud by a Sinhala-only text-to-"
            "speech voice that CANNOT pronounce Latin letters. Your response "
            "must contain ZERO Latin/English characters -- not one.\n"
            "- Every product category, brand name, technical term and English "
            "loan-word MUST be written in Sinhala script: translate it to a "
            "natural Sinhala word where one exists, otherwise spell it "
            "phonetically in Sinhala letters. Examples:\n"
            "    house -> \u0DB1\u0DD2\u0DC0\u0DC3    land / bare land -> \u0D89\u0DA9\u0DB8\n"
            "    apartment -> \u0DB8\u0DC4\u0DBD\u0DCA \u0DB1\u0DD2\u0DC0\u0DCF\u0DC3\u0DBA    commercial property -> \u0DC0\u0DCF\u0DAB\u0DD2\u0DA2 \u0DAF\u0DDA\u0DB4\u0DC5\n"
            "    bedroom -> \u0DB1\u0DD2\u0DAF\u0DB1 \u0D9A\u0DCF\u0DB8\u0DBB\u0DBA    bathroom -> \u0DB1\u0DCF\u0DB1 \u0D9A\u0DCF\u0DB8\u0DBB\u0DBA\n"
            "    perches -> \u0DB4\u0DBB\u0DCA\u0DA0\u0DC3\u0DCA    square feet -> \u0DC0\u0DBB\u0DCA\u0D9C \u0D85\u0DA9\u0DD2\n"
            "    Rodrigo Realtors -> \u0DBB\u0DDC\u0DA9\u0DCA\u200D\u0DBB\u0DD2\u0D9C\u0DDD \u0DBB\u0DD2\u0DBA\u0DBD\u0DCA\u0DA7\u0DBB\u0DCA\u0DC3\n"
            # Furnishing is on nearly every listing and was missing here, so the
            # model improvised: it rendered "unfurnished" to a caller as
            # "\u0D85\u0DC3\u0DCF\u0DAD\u0DCA\u0DB8\u0DD2\u0D9A \u0DBB\u0DC4\u0DD2\u0DAD" (allergen-free). Any attribute the KB states on
            # every row needs a fixed term here, or it gets invented.
            # \u0D9C\u0DD8\u0DC4 \u0DB7\u0DCF\u0DAB\u0DCA\u0DA9 = furniture; \u0DC3\u0DC4\u0DD2\u0DAD = with; \u0DBB\u0DC4\u0DD2\u0DAD = without.
            "    furnished -> \u0D9C\u0DD8\u0DC4 \u0DB7\u0DCF\u0DAB\u0DCA\u0DA9 \u0DC3\u0DC4\u0DD2\u0DAD    "
            "unfurnished -> \u0D9C\u0DD8\u0DC4 \u0DB7\u0DCF\u0DAB\u0DCA\u0DA9 \u0DBB\u0DC4\u0DD2\u0DAD\n"
            "    semi-furnished -> \u0D85\u0DBB\u0DCA\u0DB0 \u0DC0\u0DC1\u0DBA\u0DD9\u0DB1\u0DCA \u0D9C\u0DD8\u0DC4 \u0DB7\u0DCF\u0DAB\u0DCA\u0DA9 \u0DC3\u0DC4\u0DD2\u0DAD\n"
            "- Write every number and price as Sinhala words, never digits.\n"
            "- The reference/knowledge material is in English; translate it "
            "into natural spoken Sinhala -- never copy English words verbatim.\n"
            "- TRANSLATE, NEVER EMBELLISH. Render ONLY what the reference context "
            "actually says. Do NOT add any feature, adjective or descriptive word "
            "that is not in it. If you are unsure of the Sinhala word for "
            "something in the context, describe it plainly using words you are "
            "certain of, or leave it out entirely -- NEVER substitute a different "
            "feature. Inventing an attribute while translating is the same as "
            "lying to the caller about the property.\n"
            "- Use natural, colloquial spoken Sinhala (not formal/literary), "
            "polite and warm. Keep replies short -- they are spoken on a call.\n\n"
        )
    else:
        language_rules = (
            "LANGUAGE RULES:\n"
            "- The caller selected English. Respond only in English.\n"
            "- Use clear, simple English appropriate for all callers.\n\n"
        )

    handoff_rules = ""
    if lang == "en" and _brand["transfer"]:
        handoff_rules = (
            "HUMAN HANDOFF:\n"
            "- If the caller explicitly asks to speak to a human, agent, consultant, "
            "manager, or real person, immediately call the transfer_to_human tool with a "
            "one-sentence reason. Do NOT promise a callback instead — the tool connects them live.\n"
            "- If the caller needs something you genuinely cannot help with from the available "
            "information (price negotiations, complaints, legal or contract questions, anything "
            "beyond property details and viewings), proactively offer: 'Would you like me to connect "
            "you with one of our consultants?' and call transfer_to_human once they say yes.\n"
            "- Never invent answers just to avoid a transfer.\n\n"
        )

    return (
        f"You are {agent_name}, a warm, confident, top-performing SALES consultant for "
        f"{agency}, a trusted Sri Lankan real estate agency that helps people rent "
        f"apartments and houses across Colombo. You are consultative, "
        f"not pushy: you listen, qualify what the caller really needs, build genuine value in the "
        f"properties we have, handle hesitation gracefully, and always move the conversation toward "
        f"a viewing and a follow-up by one of our salespeople. You tell callers the full listing "
        f"details and arrange viewings -- but you NEVER invent a property, a price, or any detail "
        f"that is not in the reference context, and you NEVER promise to send anything; a real "
        f"salesperson always follows up personally.\n"
        f"Today's date is {today}.\n\n"

        + language_rules +
        handoff_rules +
        # Generated from the live inventory every call. The portfolio facts used
        # to be hard-coded here, went stale when the inventory changed, and made
        # Fiona deny listings that retrieval had correctly handed her. Empty
        # string when the backend cannot derive them -- saying nothing about the
        # portfolio is safe; saying something stale is not.
        _portfolio_facts_block(agency) +

        "GREETING & CALLER DETAILS:\n"
        f"- The opening greeting is already spoken automatically: '{greeting}' "
        "Do NOT repeat it.\n"
        "- NEVER ask for the caller's name or phone number at the start "
        "of the call. First listen to what they need and answer their questions.\n"
        "- IMPORTANT: We do NOT send property details, photos, prices, or documents "
        "to the caller by WhatsApp, email, or text message. Never offer to 'send' "
        f"anything. Instead, a {agency} salesperson personally contacts the "
        "caller to take things forward.\n"
        "- ONLY collect caller details AFTER the caller shows clear interest in a "
        "property or in proceeding - for example they want a viewing, ask to be "
        "contacted, or say they are interested. Asking about price, features, or "
        "specs is NOT a commitment - it is the caller still gathering information. "
        "Do NOT ask for their name at that stage.\n"
        "- NEVER ask for the caller's name more than once. If they ignore your "
        "request or change the subject, drop it and continue answering their "
        "questions. Do NOT loop back to ask again.\n"
        "- When the caller is interested, explain that one of our salespeople will "
        "contact them, then collect their details so the salesperson can reach them. "
        "Frame the request around that follow-up - never ask for details out of nowhere.\n"
        "- Collect in this order, ONE at a time: (1) name, then (2) the best phone "
        "number for our salesperson to contact them on. Read each one back to "
        "confirm before moving on.\n"
        "- Only ask for name and a phone number. Do NOT ask for email, address, NIC, "
        "date of birth, or any other personal details.\n"
        "- Once you have their name, use it naturally throughout the rest of "
        "the conversation.\n"
        "- If the caller only wants quick information and doesn't show clear "
        "interest, do NOT collect any details - just answer and wrap up.\n"
        "- ALWAYS prioritise answering the caller's actual question. If they "
        "ask about price, features, availability, or anything else, answer it "
        "FIRST before collecting any details.\n\n"

        "NUMBER UNDERSTANDING (CRITICAL):\n"
        "- When callers say 'double' before a digit, it means that digit repeated twice. "
        "For example: 'double 3' = 33, 'double 7' = 77, 'double zero' = 00.\n"
        "- When callers say 'triple' before a digit, it means that digit repeated three times. "
        "For example: 'triple 7' = 777, 'triple 3' = 333, 'triple zero' = 000.\n"
        "- Apply this to phone numbers, email addresses, and any other numbers.\n"
        "- When reading back numbers, you may use 'double' and 'triple' yourself for clarity.\n\n"

        "OCCUPANCY vs BEDROOMS (CRITICAL -- do not confuse these):\n"
        "- When a caller says a property is 'for 4 people', 'for a family of five', "
        "or 'for the two of us', that is the number of PEOPLE who will live there. "
        "It is NOT the number of bedrooms they want. Do NOT map 'for N people' to "
        "an N-bedroom property. A family of four is very comfortable in a three-"
        "bedroom apartment.\n"
        "- A comfortable rule of thumb is roughly two people per bedroom. Match "
        "the group size to a real listing in the PORTFOLIO FACTS and REFERENCE "
        "CONTEXT rather than searching for an exact bedroom count.\n"
        "- Only treat a number as a bedroom requirement when the caller explicitly "
        "says 'bedrooms' (e.g. 'I need two bedrooms').\n"
        "- If a caller asks for a size we do not have, be honest and offer the "
        "closest option FROM THE REFERENCE CONTEXT -- never invent a unit. The "
        "sizes we actually have are stated in PORTFOLIO FACTS above; that block "
        "is generated from the live inventory, so trust it over any assumption.\n"
        "- Always honour the property TYPE the caller asked for. If they asked for "
        "an apartment, keep recommending apartments; do not drift to a house just "
        "because it matches on area. If a listing you are about to mention is a "
        "house and they wanted an apartment, skip it and offer an apartment "
        "instead, or be honest that the closest apartment option is what we have.\n\n"

        "SALES APPROACH (be a real, helpful agent):\n"
        "- Qualify gently: understand their preferred area, budget range, how many "
        "will live there, furnishing preference, and when they want to move -- ONE "
        "question at a time, woven naturally into the conversation, never as a "
        "checklist.\n"
        "- When the caller wants an apartment or a house, ASK how many bedrooms they "
        "would like -- for example 'How many bedrooms are you looking for?'. This is "
        "the correct way to learn their bedroom need: ask it directly rather than "
        "guessing it from the number of people. Ask it early, as one of your first "
        "qualifying questions, so you can match the right property.\n"
        "- Respect the caller's budget. When they give a budget, present the options "
        "WITHIN it first and lead with the best in-budget match. Do NOT pitch a "
        "property that is well above their budget. Only mention an over-budget "
        "option if they ask about it, or if nothing in their budget fits -- and then "
        "only briefly. It is pointless to describe a property far outside their "
        "stated budget; spend the call on what they can actually afford.\n"
        "- Build value in what we actually have. Lead with the strengths of a real "
        "listing from the reference context -- location, space, views, security, "
        "pool, parking -- instead of apologising for what we do not have.\n"
        "- Handle objections warmly. If the caller says 'that's a house, I wanted an "
        "apartment' or 'that's too big', acknowledge it, then pivot to a better-"
        "matched listing from the context. Never argue, never pressure -- reassure.\n"
        "- If the price feels high, point to the value or offer to find options "
        "closer to their budget from the reference context. Never invent discounts "
        "or terms that are not in the context.\n"
        "- Always advance toward a viewing. Once there is genuine interest, offer to "
        f"arrange a viewing and note a {agency} salesperson will follow up to "
        "schedule it, then capture their name and phone number.\n"
        "- If we genuinely have nothing matching, say so honestly and offer to have "
        "a consultant follow up with options -- never fabricate a listing.\n\n"

        "VOICE RULES (you are speaking on a phone call, not writing text):\n"
        "- Keep most responses to one or two short sentences. The ONE exception "
        "is describing a specific property the caller asked about: there you "
        "give the full details and it may run several sentences (see PROPERTY "
        "DETAILS below).\n"
        "- Ask only ONE question per response. Never ask two questions at once.\n"
        "- Never use markdown, bullet points, numbered lists, asterisks, or URLs.\n"
        "- Use natural spoken language. Say numbers as words.\n"
        "- Do not use abbreviations. Say 'rupees' not 'LKR'.\n"
        "- Do not reel off the entire catalogue at once. When several properties match, mention only the few most relevant. But when the caller asks about ONE specific property, give its complete details (see PROPERTY DETAILS).\n"
        "- Pause naturally between ideas by using short sentences.\n\n"

        "PROPERTY DETAILS (tell the caller everything):\n"
        "- When a caller asks about a specific property (an apartment or a "
        "house), TELL THEM ALL of its details over "
        "voice. Do not hold details back and do not push them to WhatsApp "
        "instead of answering.\n"
        "- If they ask HOW MUCH something costs -- the deposit, the advance, or "
        "what they must pay before moving in -- give them the actual RUPEE "
        "AMOUNT, not just the number of months. The reference context states "
        "every one of these figures in full ('a 2-month deposit of three hundred "
        "and sixty thousand rupees'), so read the amount from there. NEVER "
        "calculate it yourself and never estimate: if a figure is not in the "
        "reference context, say a consultant will confirm it.\n"
        "- Cover every detail you have for that property from the reference "
        "context: the property type, the building or area and the Colombo zone, "
        "the number of bedrooms and bathrooms, the floor area (and the land "
        "extent in perches if given), the monthly rent (say the full amount in "
        "rupees, and the US dollar figure too if it is given) or, if the rent "
        "is not published, that the rent is available on request, whether it is "
        "furnished, semi-furnished, or unfurnished, the availability, the lease "
        "terms such as deposit, advance, minimum lease period and parking, and "
        "the key features.\n"
        "- Speak it as a natural flow of short sentences, grouping related "
        "facts, so it is easy to follow on a phone. Do not say it all in one "
        "breath. It is completely fine for a property description to run several "
        "sentences here, completeness matters more than brevity.\n"
        "- Read out the actual values from the reference context. Never invent "
        "or guess a detail. If a particular detail is not in the context, just "
        "leave it out and move on.\n"
        "- Each listing in the reference context ends with an internal reference "
        "like '(Ref: P15)'. This is for our records only — NEVER read it aloud, "
        "spell it out, or mention it to the caller.\n"
        "- If the caller asks only about one aspect (just the rent, or just the "
        "size), answer that directly. Give the full rundown when they ask about "
        "a property in general or say things like 'tell me about it' or 'tell "
        "me everything'.\n"
        "- We do NOT send photos, details, or documents to the caller by any "
        "channel. After you have told them about a property, if they are "
        f"interested, let them know a {agency} salesperson will contact "
        "them, and collect their name and phone number per the GREETING & "
        "CALLER DETAILS rules.\n"
        "- If a caller is interested, offer to arrange a viewing and note that "
        f"a {agency} consultant will follow up to schedule it.\n\n"

        "IMPORTANT RULES:\n"
        f"- Answer questions about {agency} property listings, locations, prices, "
        "property types, sizes, features, and the renting or viewing process using ONLY "
        "the information provided in the reference context.\n"
        "- If the caller asks about a specific property's price, tell them the rent if "
        "available, or that the rent is on request, along with the other details. State the "
        "rent EXACTLY as written in the reference context, including its period -- say 'per "
        "month' or 'per day' exactly as given, and never assume it is monthly.\n"
        f"- If the caller wants to proceed, let them know a {agency} salesperson will "
        "contact them to confirm details and arrange a viewing.\n"
        "- If the answer is not in the reference context, politely say you don't have "
        "that specific listing right now and offer to have a consultant follow up with options.\n"
        "- NEVER flatly tell a caller we have nothing in an area unless you have actually "
        "checked and the reference context for that area is genuinely empty. Phone audio "
        "garbles place names (for example 'Colombo' may arrive as 'Columbo' or 'Columbus', "
        "and 'Havelock' as 'Havoc'). If a caller insists they saw a property advertised, or "
        "names a specific building or area, TRUST them: ask for the building or area name, "
        "re-check, and offer the matching listing. Do not deny inventory because a name was "
        "mis-heard.\n"
        "- You do NOT have any tools. Do not promise to reserve a property, finalise a sale, "
        "or perform any actions. You can only provide information and arrange follow-up.\n"
        "- Be warm, helpful, and professional.\n"
        "- If a caller seems frustrated, acknowledge their feelings empathetically.\n"
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
    # Build tool_call_id -> tool_name map from assistant messages
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
    logger.info("Starting Flico Voice Agent server...")
    asterisk_task: asyncio.Task | None = None
    app.state.asterisk_ari_client = None
    app.state.asterisk_rtp_allocator = None

    # Initialize knowledge base
    logger.info("Initializing knowledge base from '%s'...", KB_DOCS_DIRECTORY)
    kb_ok = initialize_kb(KB_DOCS_DIRECTORY)
    if kb_ok:
        logger.info("Knowledge base initialized successfully.")
    else:
        logger.warning("Knowledge base initialization failed -- continuing without KB.")

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
        logger.warning("ELEVENLABS_API_KEY or ELEVENLABS_VOICE_ID not set -- "
                       "ConversationRelay TTS will not work in production.")

    if ENABLE_ASTERISK_ARI:
        if not ASTERISK_ARI_PASSWORD:
            logger.error("ENABLE_ASTERISK_ARI=true but ASTERISK_ARI_PASSWORD is empty")
        else:
            allocator = RtpPortAllocator(
                ASTERISK_RTP_BIND_HOST,
                ASTERISK_RTP_PORT_START,
                ASTERISK_RTP_PORT_END,
            )
            app.state.asterisk_rtp_allocator = allocator

            async def make_asterisk_session(
                call_sid: str,
                caller_phone: str,
                lang: str,
                rtp: AsteriskRtpTransport,
            ):
                lang = lang if lang in ("en", "ta", "si") else ASTERISK_DEFAULT_LANG
                session = MediaStreamSession(
                    websocket=None,
                    lang=lang,
                    transport=rtp,
                    call_sid=call_sid,
                    caller_phone=caller_phone,
                )
                if LLM_PROVIDER == "claude":
                    session.anthropic_client = _get_anthropic_client()
                elif LLM_PROVIDER == "gemini":
                    session.gemini_client = _get_gemini_client()
                else:
                    session.client = _get_client()
                return session

            ari_client = AsteriskAriClient(
                base_url=ASTERISK_ARI_URL,
                username=ASTERISK_ARI_USER,
                password=ASTERISK_ARI_PASSWORD,
                app_name=ASTERISK_APP_NAME,
                rtp_bind_host=ASTERISK_RTP_BIND_HOST,
                rtp_advertise_host=ASTERISK_RTP_ADVERTISE_HOST,
                port_allocator=allocator,
                session_factory=make_asterisk_session,
            )
            app.state.asterisk_ari_client = ari_client
            asterisk_task = asyncio.create_task(ari_client.run_forever())
            logger.info(
                "Asterisk ARI worker enabled: app=%s url=%s rtp=%s:%d-%d advertise=%s",
                ASTERISK_APP_NAME,
                ASTERISK_ARI_URL,
                ASTERISK_RTP_BIND_HOST,
                ASTERISK_RTP_PORT_START,
                ASTERISK_RTP_PORT_END,
                ASTERISK_RTP_ADVERTISE_HOST,
            )

    logger.info("Server startup complete.")

    yield

    # --- Shutdown ---
    logger.info("Shutting down server...")
    if asterisk_task:
        asterisk_task.cancel()
        try:
            await asterisk_task
        except asyncio.CancelledError:
            pass
    app.state.asterisk_ari_client = None
    app.state.asterisk_rtp_allocator = None
    logger.info("Shutdown complete.")


# ---------------------------------------------------------------------------
# FastAPI application
# ---------------------------------------------------------------------------
app = FastAPI(
    title="Flico Voice Agent",
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
        "kb_loaded": os.path.isdir(KB_DOCS_DIRECTORY),
        "media_streams_stt": GOOGLE_STT_AVAILABLE,
        "azure_tts": bool(AZURE_SPEECH_KEY),
    }


@app.get("/asterisk/status")
async def asterisk_status(request: Request) -> dict[str, Any]:
    """Return runtime status for the optional Asterisk ARI pilot."""
    ari_client = getattr(request.app.state, "asterisk_ari_client", None)
    allocator = getattr(request.app.state, "asterisk_rtp_allocator", None)

    status: dict[str, Any] = {
        "enabled": ENABLE_ASTERISK_ARI,
        "configured": bool(ASTERISK_ARI_PASSWORD),
        "app_name": ASTERISK_APP_NAME,
        "ari_url": ASTERISK_ARI_URL,
        "active_calls": 0,
        "sessions": 0,
        "languages": [],
        "rtp_ports": [],
        "call_ids": [],
        "remote_rtp": {
            "known": 0,
            "unknown": 0,
        },
        "calls": [],
        "allocator": allocator.snapshot() if allocator else None,
        "connected": False,
    }

    if ari_client:
        status.update(ari_client.snapshot())

    return status


# ---------------------------------------------------------------------------
# Admin: hot-reload knowledge base without container restart
# ---------------------------------------------------------------------------

@app.post("/kb-reload")
async def kb_reload(request: Request) -> dict:
    """Receive new KB content from the admin portal and rebuild the vector store."""
    secret = request.headers.get("X-KB-Secret", "")
    if not KB_RELOAD_SECRET or secret != KB_RELOAD_SECRET:
        from fastapi import HTTPException
        raise HTTPException(status_code=401, detail="Unauthorized")
    body = await request.json()
    content: str = body.get("content", "")
    filename: str = body.get("filename", "hotel_info.txt")
    if not content:
        return {"ok": False, "error": "Empty content"}
    import asyncio
    loop = asyncio.get_event_loop()
    # AWAIT the reload and report what actually happened. This used to fire the
    # executor and return {"ok": True, "message": "KB reload started"}
    # unconditionally -- so a batch REJECTED by validation still told the admin
    # portal "published". They would see success, the agent would keep serving the
    # old inventory, and nobody would be told. A false success is worse than no
    # validation at all: it is the one failure mode that guarantees nobody looks.
    try:
        ok = await loop.run_in_executor(None, reload_kb_from_content, content, filename)
    except Exception:
        logger.exception("KB reload raised for '%s'", filename)
        return {"ok": False, "error": "KB reload failed; see agent logs."}
    if not ok:
        return {"ok": False,
                "error": ("KB reload REJECTED by validation -- the previous "
                          "inventory is still live and unchanged. The agent logs "
                          "name the offending listing and reason.")}
    logger.info("KB reloaded for '%s' (%d chars)", filename, len(content))
    return {"ok": True, "message": f"KB reloaded for {filename}"}


# ---------------------------------------------------------------------------
# Twilio incoming call webhook
# ---------------------------------------------------------------------------

@app.post("/voice/incoming")
async def voice_incoming(request: Request) -> Response:
    """Twilio webhook for incoming phone calls.

    English only -- no IVR/language menu. Connects every inbound call directly
    to the English ConversationRelay WebSocket.
    """
    host = request.headers.get("host", request.url.hostname or "localhost")

    # Pull Twilio webhook form data for call metadata
    try:
        form = await request.form()
        call_sid = str(form.get("CallSid", ""))
        from_number = str(form.get("From", "")) or None
    except Exception:
        call_sid = ""
        from_number = None

    if call_sid:
        await record_call_start(call_sid, from_number, language="en")

    # IVR removed -- English only. Connect every inbound call straight to the
    # English ConversationRelay (no language menu).
    config = LANGUAGE_CONFIGS["en"]
    cr_tag = _build_conversation_relay_twiml(host, "en", config)
    twiml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<Response>\n"
        "  <Connect>\n"
        f"    {cr_tag}\n"
        "  </Connect>\n"
        "</Response>"
    )

    logger.info(
        "Incoming call from %s -- connecting directly to English ConversationRelay (IVR removed)",
        request.headers.get("x-forwarded-for", "unknown"),
    )

    return Response(content=twiml, media_type="application/xml")


# ---------------------------------------------------------------------------
# Language selection handler (called by Twilio after DTMF digit)
# ---------------------------------------------------------------------------

@app.post("/voice/language-selected")
async def voice_language_selected(request: Request) -> Response:
    """Handle the caller's DTMF language selection.

    English (1) -> ConversationRelay TwiML (ElevenLabs TTS, text-in/text-out).
    Tamil (2) -> Media Streams TwiML (ElevenLabs TTS, Google STT si=ta-IN,
    full audio control).
    Sinhala (3) -> Media Streams TwiML (self-hosted Sinhala VITS TTS,
    Google STT si-LK, full audio control).
    """
    form = await request.form()
    digit = str(form.get("Digits", "1"))
    lang = DIGIT_TO_LANG.get(digit, "en")
    host = request.headers.get("host", request.url.hostname or "localhost")

    if lang == "en":
        # English -- ConversationRelay with ElevenLabs
        config = LANGUAGE_CONFIGS["en"]
        cr_tag = _build_conversation_relay_twiml(host, "en", config)
        twiml = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            "<Response>\n"
            "  <Connect>\n"
            f"    {cr_tag}\n"
            "  </Connect>\n"
            "</Response>"
        )
        mode = "ConversationRelay"
    else:
        # Tamil / Sinhala -- Media Streams with Google STT.
        # Tamil uses ElevenLabs TTS; Sinhala uses the self-hosted Sinhala VITS service.
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
        "Language selected: %s (digit: %s) -- returning %s TwiML",
        lang, digit, mode,
    )
    return Response(content=twiml, media_type="application/xml")


@app.api_route("/voice/demo-incoming", methods=["GET", "POST"])
async def voice_demo_incoming(request: Request) -> Response:
    """Website Book-a-Demo entry -- Star Properties (Amaya), English, no IVR.

    Hatton's shared TwiML app <Redirect>s here for agent id 'starproperties'.
    Unlike the phone IVR there is no <Gather>: the demo card declares
    langs: ['en'], so everything collapses to English ConversationRelay.
    """
    host = request.headers.get("host", request.url.hostname or "localhost")

    config = LANGUAGE_CONFIGS["en"]
    cr = _build_conversation_relay_twiml(
        host, "en", config,
        brand="starproperties",
        voice=CR_VOICE_STARPROPERTIES or None,
    )
    twiml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<Response>\n"
        "  <Connect>\n"
        f"    {cr}\n"
        "  </Connect>\n"
        "</Response>"
    )
    logger.info("Demo incoming call from %s -- Star Properties (en)",
                request.headers.get("x-forwarded-for", "unknown"))
    return Response(content=twiml, media_type="application/xml")


def _build_conversation_relay_twiml(
    host: str, lang: str, config: dict[str, str],
    brand: str = DEFAULT_BRAND, voice: str | None = None,
) -> str:
    """Build the <ConversationRelay> XML tag for the given language config.

    `brand` rides the WebSocket query string because Twilio re-sends
    Device.connect params on neither a redirected webhook nor the WebSocket
    handshake. `voice` overrides the language default so a second brand can
    sound like a different person.
    """
    extra = config["extra_attrs"]
    _brand = resolve_brand(brand)
    greeting_text = _brand["greeting"].get(lang) or config["welcome_greeting"]
    # XML-escape in case the greeting contains special characters
    greeting = xml.sax.saxutils.escape(greeting_text)
    ws_voice = voice or config["voice"]
    # Derive the key from the already-resolved brand dict (identity lookup)
    # rather than re-checking `brand in BRANDS`, which does an exact-match
    # comparison while resolve_brand() normalizes with .strip().lower(). Two
    # separate normalizations of the same input must never be allowed to
    # disagree -- a mixed-case brand like "StartProperty" would otherwise
    # render Amaya's greeting while the URL still said "brand=rodrigo".
    brand_key = next((k for k, v in BRANDS.items() if v is _brand), DEFAULT_BRAND)

    return (
        f'<ConversationRelay url="wss://{host}/ws/conversation?lang={lang}&amp;brand={brand_key}"\n'
        f'        ttsProvider="{config["tts_provider"]}"\n'
        f'        voice="{ws_voice}"\n'
        f'{extra}'
        f'        language="{config["language"]}"\n'
        f'        transcriptionProvider="google"\n'
        f'        speechModel="telephony"\n'
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


# Clause-level split (sentence enders + commas) for low-latency TTS chunking.
_TTS_CHUNK_SPLIT = re.compile(r'(?<=[.!?,à¥¤à·´])\s+')


def _split_tts_chunks(text: str, max_len: int = 70) -> list[str]:
    """Break text into short clause-level chunks for low-latency TTS.

    The Sinhala VITS service is non-streaming: synthesis time scales with the
    input length. Feeding it one short clause at a time lets the first audio
    reach the caller in ~2 s instead of waiting on the whole utterance, while
    Twilio's playback buffer hides the synth time of later chunks.
    """
    pieces = _TTS_CHUNK_SPLIT.split(text.strip())
    chunks: list[str] = []
    cur = ""
    for p in pieces:
        p = p.strip()
        if not p:
            continue
        if cur and len(cur) + len(p) + 1 > max_len:
            chunks.append(cur)
            cur = p
        else:
            cur = f"{cur} {p}" if cur else p
    if cur:
        chunks.append(cur)
    return chunks


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
# Google Cloud STT -- streaming (background thread, Media Streams only)
# ---------------------------------------------------------------------------

class GoogleSTTStream:
    """Streams mulaw 8 kHz audio to Google Cloud Speech-to-Text.

    Runs the synchronous gRPC streaming_recognize in a daemon thread.
    Fires on_final_result(transcript) from that thread.
    Auto-restarts on the ~5-minute gRPC streaming limit.
    """

    def __init__(self, on_final_result: Any, on_interim_result: Any = None, lang: str = "ta"):
        self._on_final = on_final_result
        self._on_interim = on_interim_result
        self._lang = lang
        self._audio_q: queue.Queue[bytes | None] = queue.Queue()
        self._running = False
        self._thread: threading.Thread | None = None
        self._chunk_count = 0

    def start(self):
        if not GOOGLE_STT_AVAILABLE:
            logger.error("Cannot start STT -- google-cloud-speech not installed")
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
                logger.warning("STT stream ended (%s) -- restarting...", exc, exc_info=True)

    def _run_one_stream(self):
        client = google_speech.SpeechClient()
        primary = STT_PRIMARY.get(self._lang, "ta-IN")
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
        logger.info("STT gRPC stream connected -- waiting for speech...")
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
    """Streams audio to Azure Speech-to-Text — drop-in alternative to GoogleSTTStream."""

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
            logger.error("Cannot start Azure STT — audioop unavailable")
            return
        if not AZURE_SPEECH_KEY:
            logger.error("Cannot start Azure STT — AZURE_SPEECH_KEY not set")
            return

        primary = STT_PRIMARY.get(self._lang, "si-LK")
        speech_config = azure_speech.SpeechConfig(
            subscription=AZURE_SPEECH_KEY, region=AZURE_SPEECH_REGION,
        )
        speech_config.speech_recognition_language = primary
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
            pcm = audioop.ulaw2lin(mulaw_bytes, 2)
        except Exception:
            return
        self._push_stream.write(pcm)
        self._chunk_count += 1
        if self._chunk_count % 200 == 0:
            logger.info("Azure STT audio feed: %d chunks (lang=%s)", self._chunk_count, self._lang)

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
    """Build the configured STT backend. STT_PROVIDER: 'google' (default) | 'azure'."""
    if STT_PROVIDER == "azure":
        if AZURE_STT_AVAILABLE and audioop is not None:
            return AzureSTTStream(on_final_result, on_interim_result, lang)
        logger.error("STT_PROVIDER=azure but Azure STT unavailable — falling back to Google")
    return GoogleSTTStream(on_final_result, on_interim_result, lang)


# ---------------------------------------------------------------------------
# Media Stream Session (Tamil calls)
# ---------------------------------------------------------------------------

class MediaStreamSession:
    """Manages a single Twilio Media Streams call for Tamil or Sinhala.

    Pipeline per turn:
      Google STT -> endpointing -> KB retrieval -> LLM (streaming)
      -> TTS -> mulaw audio -> Twilio

    TTS provider depends on language: Tamil uses ElevenLabs, Sinhala uses
    the self-hosted Sinhala VITS service.
    """

    def __init__(
        self,
        websocket: WebSocket | None,
        lang: str,
        anthropic_client: AsyncAnthropic | None = None,
        openai_client: AsyncOpenAI | None = None,
        gemini_client=None,
        transport: MediaTransport | None = None,
        call_sid: str = "unknown",
        caller_phone: str = "unknown",
    ):
        self.ws = websocket
        self.anthropic_client = anthropic_client
        self.client = openai_client  # OpenAI client (kept for openai provider)
        self.gemini_client = gemini_client
        self.lang = lang
        self.system_prompt = _build_system_prompt(lang)
        self.tools = []
        self.media_transport = transport

        self.stream_sid: str | None = None
        self.call_sid: str = call_sid
        self.caller_phone: str = caller_phone
        self.history: list[dict] = []
        # Sticky retrieval constraints (property_type / zone) carried across turns
        # so KB retrieval does not lose "apartment" when a later utterance only
        # mentions a zone. Passed to retrieve_context() each turn.
        self.sticky_filters: dict = {}
        self.started_at = datetime.now(timezone.utc)

        self._event_loop: asyncio.AbstractEventLoop | None = None
        self._is_speaking = False
        self._speak_lock = asyncio.Lock()
        self._ws_lock = asyncio.Lock()
        self._speak_generation: int = 0

        self._pending_transcript = ""
        self._latest_interim = ""
        self._endpointing_handle: asyncio.TimerHandle | None = None
        self._stt: GoogleSTTStream | None = None
        # Cleared when the call ends -- guards against sending to a closed
        # WebSocket if the LLM/TTS pipeline is still running after hangup.
        self._active = True

    # -- Main event loop ---------------------------------------------------

    async def run(self):
        self._event_loop = asyncio.get_running_loop()
        if self.ws is None:
            raise RuntimeError("Twilio Media Streams run() requires a WebSocket")
        if self.media_transport is None:
            self.media_transport = TwilioMediaTransport(
                self.ws,
                lambda: self.stream_sid,
            )
        await self.ws.accept()
        logger.info("Media stream WebSocket accepted (lang=%s)", self.lang)

        self._start_stt()

        try:
            while True:
                raw = await self.ws.receive_text()
                msg = json.loads(raw)
                event = msg.get("event", "")

                if event == "start":
                    meta = msg.get("start", {})
                    self.stream_sid = meta.get("streamSid")
                    self.call_sid = meta.get("callSid", "unknown")
                    logger.info(
                        "Media stream started -- Call: %s, Stream: %s, lang: %s",
                        self.call_sid, self.stream_sid, self.lang,
                    )
                    asyncio.ensure_future(
                        self._speak(MEDIA_STREAM_WELCOME[self.lang])
                    )

                elif event == "media":
                    audio = base64.b64decode(msg["media"]["payload"])
                    await self.feed_audio(audio)

                elif event == "mark":
                    mark_name = msg.get("mark", {}).get("name")
                    logger.info("Mark received [%s]: %s", self.call_sid, mark_name)
                    if mark_name == "tts_done":
                        self._is_speaking = False
                        logger.info("TTS done -- listening for guest speech [%s]", self.call_sid)

                elif event == "stop":
                    logger.info("Media stream stopped -- Call: %s", self.call_sid)
                    break

        except WebSocketDisconnect:
            logger.info("Media stream disconnected -- Call: %s", self.call_sid)
        except Exception:
            logger.exception("Media stream error -- Call: %s", self.call_sid)
        finally:
            # Mark the session dead first so any in-flight LLM/TTS work stops
            # trying to write to the now-closed WebSocket.
            self._active = False
            self._is_speaking = False
            if self._stt:
                self._stt.stop()
            if self._endpointing_handle:
                self._endpointing_handle.cancel()
            logger.info(
                "Media stream session ended -- Call: %s, history: %d msgs",
                self.call_sid, len(self.history),
            )

            # Real-estate lead post-call -> n8n -> Google Sheet (fire-and-forget).
            if self.history:
                try:
                    asyncio.create_task(
                        process_realestate_post_call(
                            call_sid=self.call_sid,
                            caller_phone=self.caller_phone,
                            transcript=self.history,
                            anthropic_client=self.anthropic_client,
                            model=MODEL,
                            started_at=self.started_at,
                        )
                    )
                except Exception:
                    logger.exception(
                        "[realestate-postcall] schedule failed [%s]", self.call_sid
                    )

    async def start(self):
        """Start an Asterisk/RTP-driven media session."""
        self._event_loop = asyncio.get_running_loop()
        if self.media_transport is None:
            raise RuntimeError("External media session requires a transport")
        self._active = True
        logger.info(
            "External media session started -- Call: %s, lang: %s, phone: %s",
            self.call_sid, self.lang, self.caller_phone,
        )
        self._start_stt()
        asyncio.ensure_future(self._speak(MEDIA_STREAM_WELCOME.get(self.lang, MEDIA_STREAM_WELCOME["si"])))

    async def feed_audio(self, audio: bytes):
        if self._active and self._stt:
            self._stt.feed(audio)

    async def stop(self):
        self._active = False
        self._is_speaking = False
        if self._stt:
            self._stt.stop()
            self._stt = None
        if self._endpointing_handle:
            self._endpointing_handle.cancel()
            self._endpointing_handle = None
        logger.info("External media session stopped -- Call: %s", self.call_sid)

    def _start_stt(self):
        self._stt = _make_stt(
            on_final_result=self._on_stt_result,
            on_interim_result=self._on_stt_interim,
            lang=self.lang,
        )
        self._stt.start()

    # -- STT callback (called from background thread) ----------------------

    def _on_stt_result(self, transcript: str):
        """Called from STT thread on FINAL results."""
        logger.info("STT final result [%s]: %r (speaking=%s)", self.call_sid, transcript, self._is_speaking)
        self._latest_interim = ""  # clear -- final supersedes interim
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
        await self._clear_audio()

    async def _send_audio(self, audio: bytes):
        if not self._active:
            return
        if self.media_transport and getattr(self.media_transport, "is_active", True):
            await self.media_transport.send_audio(audio)

    async def _send_mark(self, name: str):
        if not self._active:
            if not isinstance(self.media_transport, TwilioMediaTransport):
                self._is_speaking = False
            return
        if self.media_transport:
            await self.media_transport.send_mark(name)
        if not isinstance(self.media_transport, TwilioMediaTransport):
            self._is_speaking = False

    async def _clear_audio(self):
        if self.media_transport:
            await self.media_transport.clear_audio()

    # -- Endpointing -------------------------------------------------------

    async def _accumulate_transcript(self, text: str):
        if self._endpointing_handle:
            self._endpointing_handle.cancel()
            self._endpointing_handle = None
        self._pending_transcript = text
        self._endpointing_handle = self._event_loop.call_later(
            ENDPOINTING_SILENCE,
            lambda: asyncio.ensure_future(self._flush_transcript()),
        )

    async def _set_transcript_interim(self, text: str):
        """Overwrite (not append) pending transcript with latest interim; reset timer."""
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
        if len(transcript) < 4:
            logger.info("Ignoring short transcript [%s]: %r", self.call_sid, transcript)
            return
        logger.info("Guest [%s]: %s", self.call_sid, transcript)
        await self._process_utterance(transcript)

    # -- Utterance -> KB + LLM + TTS ---------------------------------------

    async def _process_utterance(self, text: str):
        try:
            kb_context = retrieve_context(text, sticky=self.sticky_filters)
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
        except Exception:
            logger.exception("LLM error [%s]", self.call_sid)
            if self.lang == "si":
                error_msg = "\u0DC3\u0DB8\u0DCF\u0DC0\u0DB1\u0DCA\u0DB1, \u0DAD\u0DCF\u0D9A\u0DCA\u0DC2\u0DAB\u0DD2\u0D9A \u0DAF\u0DDD\u0DC2\u0DBA\u0D9A\u0DCA \u0D87\u0DAD\u0DD2 \u0DC0\u0DD2\u0DBA. \u0D9A\u0DBB\u0DD4\u0DAB\u0DCF\u0D9A\u0DBB \u0DB1\u0DD0\u0DC0\u0DAD \u0D8B\u0DAD\u0DCA\u0DC3\u0DCF\u0DC4 \u0D9A\u0DBB\u0DB1\u0DCA\u0DB1."
            elif self.lang == "ta":
                error_msg = "\u0BAE\u0BA9\u0BCD\u0BA9\u0BBF\u0B95\u0BCD\u0B95\u0BB5\u0BC1\u0BAE\u0BCD, \u0B92\u0BB0\u0BC1 \u0BA4\u0BCA\u0BB4\u0BBF\u0BB2\u0BCD\u0BA8\u0BC1\u0B9F\u0BCD\u0BAA \u0B9A\u0BBF\u0B95\u0BCD\u0B95\u0BB2\u0BCD \u0B8F\u0BB1\u0BCD\u0BAA\u0B9F\u0BCD\u0B9F\u0BA4\u0BC1."
            else:
                error_msg = "I'm sorry, I encountered a technical issue. Please try again."
            await self._speak(error_msg)

    # -- OpenAI streaming + sentence-level TTS -----------------------------

    async def _run_llm(self) -> str:
        logger.info("LLM call [%s]", self.call_sid)

        text_content = ""
        sentence_buffer = ""
        tts_tasks: list[asyncio.Task] = []
        gen = self._speak_generation

        messages = [{"role": "system", "content": self.system_prompt}] + self.history
        stream = await self.client.chat.completions.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            temperature=LLM_TEMPERATURE,
            messages=messages,
            tools=None,
            stream=True,
        )

        async for chunk in stream:
            choice = chunk.choices[0]
            delta = choice.delta

            if delta.content:
                text_content += delta.content
                sentence_buffer += delta.content
                sentences, sentence_buffer = _extract_sentences(
                    sentence_buffer
                )
                for s in sentences:
                    task = asyncio.create_task(
                        self._speak(s, generation=gen)
                    )
                    tts_tasks.append(task)

        # Flush remaining sentence buffer
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
        return text_content

    # -- Gemini native streaming + sentence-level TTS ----------------------

    async def _run_llm_gemini(self) -> str:
        """Gemini-native streaming version of _run_llm for Media Streams."""
        logger.info("Gemini call [%s]", self.call_sid)

        text_content = ""
        sentence_buffer = ""
        tts_tasks: list[asyncio.Task] = []
        gen = self._speak_generation

        gemini_contents = _history_to_gemini(self.history)
        config = {
            "system_instruction": self.system_prompt,
            "max_output_tokens": MAX_TOKENS,
            "temperature": LLM_TEMPERATURE,
        }

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
                    sentence_buffer += part.text
                    sentences, sentence_buffer = _extract_sentences(
                        sentence_buffer
                    )
                    for s in sentences:
                        task = asyncio.create_task(
                            self._speak(s, generation=gen)
                        )
                        tts_tasks.append(task)

        logger.info(
            "Gemini call [%s] -- text=%d chars, finish=%s",
            self.call_sid, len(text_content), finish_reason,
        )

        # Flush remaining sentence buffer
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
        return text_content

    # -- Claude native streaming + sentence-level TTS ----------------------

    async def _run_llm_claude(self) -> str:
        """Anthropic Claude streaming for Media Streams with sentence-level TTS."""
        logger.info("Claude call [%s]", self.call_sid)

        text_content = ""
        sentence_buffer = ""
        tts_tasks: list[asyncio.Task] = []
        gen = self._speak_generation

        async with self.anthropic_client.messages.stream(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            temperature=LLM_TEMPERATURE,
            # cache_control caches the system prefix for ~5 min,
            # cutting input tokens on the 2nd+ turn of every call.
            system=[{
                "type": "text",
                "text": self.system_prompt,
                "cache_control": {"type": "ephemeral"},
            }],
            messages=self.history,
            tools=NOT_GIVEN,
        ) as stream:
            async for event in stream:
                if event.type == "content_block_delta":
                    if event.delta.type == "text_delta":
                        text_content += event.delta.text
                        sentence_buffer += event.delta.text
                        sentences, sentence_buffer = _extract_sentences(
                            sentence_buffer
                        )
                        for s in sentences:
                            task = asyncio.create_task(
                                self._speak(s, generation=gen)
                            )
                            tts_tasks.append(task)

        # Flush remaining sentence buffer
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
        return text_content

    # -- TTS -> Twilio mulaw audio -----------------------------------------

    async def _speak(self, text: str, generation: int = -1):
        """Route text to appropriate TTS provider.

        Tamil   -> ElevenLabs eleven_multilingual_v2 (cloned voice)
        Sinhala -> self-hosted VITS (SinhalaVITS-TTS-M2)
        """
        async with self._speak_lock:
            if not self._active:
                return
            if generation >= 0 and generation != self._speak_generation:
                return
            if self.lang in ("en", "ta"):
                await self._tts_elevenlabs(text)
            else:
                await self._tts_openai(text)

    # -- ElevenLabs TTS (Tamil) --------------------------------------------

    async def _tts_elevenlabs(self, text: str):
        """Stream text via ElevenLabs eleven_multilingual_v2 and send mulaw audio to Twilio.
        Must only be called from _speak (lock already held).
        """
        if not ELEVENLABS_API_KEY or not ELEVENLABS_VOICE_ID:
            logger.warning("ElevenLabs not configured -- skipping TTS")
            return
        if not text.strip() or not self._active:
            return

        self._is_speaking = True
        url = (
            ELEVENLABS_TTS_URL.format(voice_id=ELEVENLABS_VOICE_ID)
            + "?output_format=ulaw_8000"
        )
        headers = {
            "xi-api-key": ELEVENLABS_API_KEY,
            "Content-Type": "application/json",
        }
        payload: dict[str, Any] = {
            "text": text,
            "model_id": ELEVENLABS_MODEL_MULTILINGUAL,
            "voice_settings": {
                "stability": 0.5,
                "similarity_boost": 0.75,
                "style": 0.0,
                "use_speaker_boost": True,
                "speed": ELEVENLABS_VOICE_SPEED,
            },
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
                        if not self._is_speaking or not self._active:
                            break
                        await self._send_audio(chunk)

            if self._is_speaking and self._active:
                await self._send_mark("tts_done")
            else:
                logger.info("ElevenLabs TTS interrupted by barge-in [%s]", self.call_sid)

        except httpx.TimeoutException:
            logger.error("ElevenLabs timeout for: %s", text[:80])
            self._is_speaking = False
        except Exception:
            logger.exception("ElevenLabs TTS failed for: %s", text[:80])
            self._is_speaking = False

    # -- Sinhala VITS TTS (Sinhala) ----------------------------------------

    async def _tts_sinhala(self, text: str):
        """Synthesize Sinhala speech via the self-hosted Sinhala VITS service.

        POSTs to {SINHALA_TTS_URL}/tts?format=mulaw8k with JSON {"text": ...}.
        The response body is RAW 8 kHz mono 8-bit mulaw bytes (no WAV header),
        so it is fed directly into the same Twilio media framing the Tamil and
        Azure paths use -- no resampling needed.

        Must only be called from _speak (lock already held).
        """
        text = text.strip()
        if not text or not self._active:
            return

        self._is_speaking = True
        url = f"{SINHALA_TTS_URL}/tts?format=mulaw8k"

        # Synthesize one short clause at a time so the first audio reaches the
        # caller quickly; Twilio's playback buffer absorbs later chunks' synth.
        chunks = _split_tts_chunks(text)

        try:
            async with httpx.AsyncClient() as http:
                for clause in chunks:
                    if not self._is_speaking or not self._active:
                        break
                    resp = await http.post(
                        url, json={"text": clause}, timeout=30.0,
                    )
                    if resp.status_code != 200:
                        logger.error("Sinhala TTS %d: %s",
                                     resp.status_code, resp.content[:200])
                        self._is_speaking = False
                        return

                    audio = resp.content  # raw 8 kHz mulaw bytes

                    # Frame as 80 ms / 640-byte mulaw chunks for Twilio Media
                    # Streams -- matches the Tamil (ElevenLabs) / Azure paths.
                    for i in range(0, len(audio), 640):
                        if not self._is_speaking or not self._active:
                            break
                        await self._send_audio(audio[i:i + 640])

            if self._is_speaking and self._active:
                await self._send_mark("tts_done")
            else:
                logger.info("Sinhala TTS interrupted [%s]", self.call_sid)

        except httpx.TimeoutException:
            logger.error("Sinhala TTS timeout for: %s", text[:80])
            self._is_speaking = False
        except (WebSocketDisconnect, RuntimeError) as exc:
            # Caller hung up mid-send -- the WebSocket closed underneath us.
            logger.info("Sinhala TTS aborted, websocket closed [%s]: %s",
                        self.call_sid, exc)
            self._is_speaking = False
            self._active = False
        except Exception:
            logger.exception("Sinhala TTS failed for: %s", text[:80])
            self._is_speaking = False

    # -- Azure TTS ---------------------------------------------------------

    async def _tts_azure(self, text: str, lang_code: str, voice_name: str):
        """Stream Azure Cognitive Services TTS as mulaw 8 kHz to Twilio.
        Must only be called from _speak (lock already held).
        """
        if not AZURE_SPEECH_KEY:
            logger.warning("AZURE_SPEECH_KEY not set -- skipping TTS")
            return
        if not self._active:
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
                        if not self._is_speaking or not self._active:
                            break
                        await self._send_audio(chunk)
            if self._is_speaking and self._active:
                await self._send_mark("tts_done")
            else:
                logger.info("Azure TTS interrupted [%s]", self.call_sid)
        except httpx.TimeoutException:
            logger.error("Azure TTS timeout for: %s", text[:80])
            self._is_speaking = False
        except (WebSocketDisconnect, RuntimeError) as exc:
            # Caller hung up mid-send -- the WebSocket closed underneath us.
            logger.info("Azure TTS aborted, websocket closed [%s]: %s",
                        self.call_sid, exc)
            self._is_speaking = False
            self._active = False
        except Exception:
            logger.exception("Azure TTS failed for: %s", text[:80])
            self._is_speaking = False

    # -- OpenAI TTS (Sinhala) ----------------------------------------------

    async def _tts_openai(self, text: str):
        """Stream OpenAI gpt-4o-mini-tts as mulaw 8 kHz to Twilio (Sinhala).

        OpenAI returns raw 24 kHz 16-bit mono PCM; we downsample to 8 kHz and
        mulaw-encode on the fly so it drops straight into the same Twilio media
        framing the Tamil/Azure paths use.
        Must only be called from _speak (lock already held).
        """
        text = text.strip()
        if not OPENAI_API_KEY:
            logger.warning("OPENAI_API_KEY not set -- skipping TTS")
            return
        if not text or not self._active:
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
                        if not self._is_speaking or not self._active:
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
                            if not self._is_speaking or not self._active:
                                break
                            frame, mulaw_buf = mulaw_buf[:640], mulaw_buf[640:]
                            await self._send_audio(frame)

            # Flush any remaining tail of mulaw audio.
            if self._is_speaking and self._active and mulaw_buf:
                await self._send_audio(mulaw_buf)

            if self._is_speaking and self._active:
                await self._send_mark("tts_done")
            else:
                logger.info("OpenAI TTS interrupted [%s]", self.call_sid)

        except httpx.TimeoutException:
            logger.error("OpenAI TTS timeout for: %s", text[:80])
            self._is_speaking = False
        except (WebSocketDisconnect, RuntimeError) as exc:
            # Caller hung up mid-send -- the WebSocket closed underneath us.
            logger.info("OpenAI TTS aborted, websocket closed [%s]: %s",
                        self.call_sid, exc)
            self._is_speaking = False
            self._active = False
        except Exception:
            logger.exception("OpenAI TTS failed for: %s", text[:80])
            self._is_speaking = False


# ---------------------------------------------------------------------------
# Streaming LLM calls (OpenAI) -- ConversationRelay
# ---------------------------------------------------------------------------

async def _run_llm_streaming(
    client: AsyncOpenAI,
    system: str,
    conversation_history: list[dict],
    tools: list[dict],
    websocket: WebSocket,
) -> str:
    """Stream an OpenAI response to the ConversationRelay WebSocket.

    Sends text tokens to the WebSocket as they arrive so the caller hears
    speech with minimal latency.

    Returns the final assistant text.
    """
    full_response_text = ""
    text_content: str = ""

    messages = [{"role": "system", "content": system}] + conversation_history
    stream = await client.chat.completions.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        temperature=LLM_TEMPERATURE,
        messages=messages,
        tools=None,
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

    full_response_text += text_content

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


# ---------------------------------------------------------------------------
# Streaming LLM calls (Gemini native) -- ConversationRelay
# ---------------------------------------------------------------------------

async def _run_llm_streaming_gemini(
    gemini_client,
    system: str,
    conversation_history: list[dict],
    tools: list[dict],
    websocket: WebSocket,
) -> str:
    """Stream a Gemini response via the native SDK to the ConversationRelay WebSocket.

    Uses the same history format (OpenAI) internally, converting to Gemini
    format for each API call.
    """
    full_response_text = ""
    text_content = ""

    gemini_contents = _history_to_gemini(conversation_history)
    config = {
        "system_instruction": system,
        "max_output_tokens": MAX_TOKENS,
        "temperature": LLM_TEMPERATURE,
    }

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

    logger.info(
        "Gemini done -- text=%d chars, finish=%s",
        len(text_content), finish_reason,
    )

    full_response_text += text_content

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


# ---------------------------------------------------------------------------
# Human handoff -- transfer the live call to a consultant (English only)
# ---------------------------------------------------------------------------

_twilio_client: TwilioRestClient | None = None


def _get_twilio_client() -> TwilioRestClient | None:
    """Return the shared Twilio REST client (used to redirect a live call)."""
    global _twilio_client
    if _twilio_client is None and TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN:
        _twilio_client = TwilioRestClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    return _twilio_client


async def _transfer_to_human(
    websocket: WebSocket, call_sid: str, caller_phone: str, reason: str
) -> None:
    """Bridge the live call to HUMAN_AGENT_PHONE via Twilio REST (Path B).

    Pushes <Dial> TwiML onto the in-progress call; Twilio then tears down the
    ConversationRelay WebSocket as the new TwiML takes effect.
    """
    logger.info("Handoff requested [%s] -> %s: %s", call_sid, HUMAN_AGENT_PHONE, reason)
    try:
        await websocket.send_text(json.dumps({
            "type": "text",
            "token": "Connecting you to a Rodrigo Realtors consultant now. Please hold.",
            "last": True,
        }))
    except Exception:
        pass

    tw = _get_twilio_client()
    if not tw or not HUMAN_AGENT_PHONE:
        logger.warning(
            "Handoff not configured (Twilio creds or HUMAN_AGENT_PHONE missing) [%s]", call_sid
        )
        return

    reason_q = quote((reason or "")[:200])
    twiml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Response>'
        '<Say>Connecting you now. Please hold.</Say>'
        f'<Dial action="https://{PUBLIC_HOSTNAME}/voice/dial-result" method="POST" '
        f'timeout="25" answerOnBridge="true">'
        f'<Number url="https://{PUBLIC_HOSTNAME}/voice/whisper?reason={reason_q}">'
        f'{HUMAN_AGENT_PHONE}</Number>'
        '</Dial>'
        '</Response>'
    )
    try:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, lambda: tw.calls(call_sid).update(twiml=twiml))
        logger.info("Handoff TwiML pushed to live call [%s]", call_sid)
    except Exception:
        logger.exception("Handoff Twilio update failed [%s]", call_sid)


@app.post("/voice/whisper")
async def voice_whisper(request: Request) -> Response:
    """Short message played to the consultant when they pick up, before bridging."""
    reason = request.query_params.get("reason", "Incoming caller.")
    safe = (reason or "Incoming caller.").replace("&", " and ").replace("<", "").replace(">", "")
    twiml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<Response><Say>Incoming Rodrigo Realtors caller. {safe}. Connecting now.</Say></Response>'
    )
    return Response(content=twiml, media_type="application/xml")


@app.post("/voice/dial-result")
async def voice_dial_result(request: Request) -> Response:
    """<Dial action> callback: hang up if bridged, else apologise and end."""
    try:
        form = await request.form()
        status = str(form.get("DialCallStatus", ""))
    except Exception:
        status = ""
    if status in ("completed", "answered"):
        twiml = '<?xml version="1.0" encoding="UTF-8"?><Response><Hangup/></Response>'
    else:
        twiml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Response><Say>Sorry, no consultant was available right now. A Rodrigo '
            'Realtors consultant will follow up with you shortly. Goodbye.</Say><Hangup/></Response>'
        )
    return Response(content=twiml, media_type="application/xml")


# ---------------------------------------------------------------------------
# Streaming LLM calls (Anthropic Claude) -- ConversationRelay
# ---------------------------------------------------------------------------

async def _run_llm_streaming_claude(
    client: AsyncAnthropic,
    system: str,
    conversation_history: list[dict],
    tools: list[dict],
    websocket: WebSocket,
) -> str:
    """Stream a Claude response via the Anthropic SDK to the ConversationRelay WebSocket.

    Sends text tokens to the WebSocket as they arrive for ConversationRelay.
    """
    full_response_text = ""
    text_content = ""
    transfer_reason: str | None = None

    async with client.messages.stream(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        temperature=LLM_TEMPERATURE,
        # cache_control caches the system prefix for ~5 min,
        # cutting input tokens on the 2nd+ turn of every call.
        system=[{
            "type": "text",
            "text": system,
            "cache_control": {"type": "ephemeral"},
        }],
        messages=conversation_history,
        tools=(tools or NOT_GIVEN),
    ) as stream:
        async for event in stream:
            if event.type == "content_block_delta":
                if event.delta.type == "text_delta":
                    text_content += event.delta.text
                    await websocket.send_text(
                        json.dumps({"type": "text", "token": event.delta.text})
                    )
        # Detect a transfer_to_human tool call in the completed message.
        try:
            final_message = await stream.get_final_message()
            for block in final_message.content:
                if getattr(block, "type", None) == "tool_use" and getattr(block, "name", "") == "transfer_to_human":
                    _inp = block.input if isinstance(block.input, dict) else {}
                    transfer_reason = (_inp.get("reason") or "").strip() or "Caller requested human assistance."
        except Exception:
            logger.exception("Failed to inspect final message for transfer tool")

    full_response_text += text_content

    await websocket.send_text(
        json.dumps({"type": "text", "token": "", "last": True})
    )

    if text_content:
        conversation_history.append({
            "role": "assistant",
            "content": text_content,
        })

    logger.info("Claude response complete (%d chars)", len(full_response_text))
    return full_response_text, transfer_reason


# ---------------------------------------------------------------------------
# WebSocket -- ConversationRelay handler
# ---------------------------------------------------------------------------

@app.websocket("/ws/conversation")
async def ws_conversation(websocket: WebSocket, lang: str = "en", brand: str = DEFAULT_BRAND):
    """Handle a Twilio ConversationRelay WebSocket session.

    The ``lang`` query parameter is set by the IVR routing and determines
    which language-specific system prompt the LLM receives.

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
    _brand = resolve_brand(brand)
    logger.info("WebSocket connection accepted -- language: %s, brand: %s",
                lang, _brand["agency"])

    # -- Per-session state --
    conversation_history: list[dict] = []
    # Sticky retrieval constraints (property_type / zone) carried across turns so
    # KB retrieval keeps "apartment" even when a later utterance only names a zone.
    sticky_filters: dict = {}
    system_prompt: str = _build_system_prompt(lang, brand)
    # Amaya has no human consultant behind her -- never offer a transfer that
    # cannot be performed. Decision lives in _tools_for() so it has exactly
    # one implementation, directly covered by tests/test_demo_endpoint.py.
    tools: list[dict] = _tools_for(lang, brand)
    call_sid: str = "unknown"
    from_number: str | None = None
    # Clean transcript (no KB-context prefix) used for dashboard persistence.
    transcript: list[dict] = []
    session_started_at = datetime.now(timezone.utc)

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
        logger.error("LLM client not available -- closing WebSocket")
        await websocket.close(code=1011, reason="Server configuration error")
        return

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
                from_number = message.get("from") or from_number
                logger.info(
                    "Session setup -- CallSid: %s, From: %s, StreamSid: %s",
                    call_sid,
                    from_number,
                    message.get("streamSid", "n/a"),
                )
                # Session state is already initialized above.
                # Log any additional setup metadata.
                logger.info(
                    "Session ready -- system prompt length: %d chars, tools: %d",
                    len(system_prompt),
                    len(tools),
                )

            # ---------------------------------------------------------------
            # PROMPT -- user speech transcribed
            # ---------------------------------------------------------------
            elif msg_type == "prompt":
                user_text = message.get("voicePrompt", "").strip()
                if not user_text:
                    logger.debug("Empty voicePrompt received -- ignoring")
                    continue

                logger.info("Guest [%s]: %s", call_sid, user_text)
                transcript.append({
                    "role": "user",
                    "text": user_text,
                    "ts": datetime.now(timezone.utc).isoformat(),
                })

                # Retrieve KB context for this utterance, carrying the caller's
                # property_type / zone constraints across turns.
                try:
                    kb_context = retrieve_context(user_text, sticky=sticky_filters)
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
                transfer_reason = None
                try:
                    if LLM_PROVIDER == "claude":
                        response_text, transfer_reason = await _run_llm_streaming_claude(
                            client=anthropic_client,
                            system=system_prompt,
                            conversation_history=conversation_history,
                            tools=tools,
                            websocket=websocket,
                        )
                    elif LLM_PROVIDER == "gemini":
                        response_text = await _run_llm_streaming_gemini(
                            gemini_client=gemini_client,
                            system=system_prompt,
                            conversation_history=conversation_history,
                            tools=tools,
                            websocket=websocket,
                        )
                    else:
                        response_text = await _run_llm_streaming(
                            client=openai_client,
                            system=system_prompt,
                            conversation_history=conversation_history,
                            tools=tools,
                            websocket=websocket,
                        )
                    logger.info("Agent [%s]: %s", call_sid, response_text[:200])
                    if response_text:
                        transcript.append({
                            "role": "assistant",
                            "text": response_text,
                            "ts": datetime.now(timezone.utc).isoformat(),
                        })
                    # Human handoff: Claude called transfer_to_human -> bridge the call.
                    if transfer_reason:
                        await _transfer_to_human(
                            websocket, call_sid, from_number or "", transfer_reason
                        )
                        break
                except WebSocketDisconnect:
                    logger.info("WebSocket disconnected during LLM streaming [%s]", call_sid)
                    raise
                except Exception:
                    logger.exception("Error during LLM streaming [%s]", call_sid)
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
            # INTERRUPT -- user interrupted agent speech
            # ---------------------------------------------------------------
            elif msg_type == "interrupt":
                logger.info(
                    "Speech interrupted by guest [%s] -- utteranceUntilInterrupt: '%s'",
                    call_sid,
                    message.get("utteranceUntilInterrupt", ""),
                )

            # ---------------------------------------------------------------
            # OTHER
            # ---------------------------------------------------------------
            else:
                logger.debug("Unhandled message type '%s' [%s]: %s", msg_type, call_sid, raw[:200])

    except WebSocketDisconnect as exc:
        logger.info(
            "WebSocket disconnected -- CallSid: %s, close_code: %s (caller hangup or Twilio end)",
            call_sid,
            getattr(exc, "code", "n/a"),
        )
    except Exception:
        logger.exception("Unexpected error in WebSocket handler [%s]", call_sid)
    finally:
        logger.info(
            "Session ended -- CallSid: %s, history length: %d messages",
            call_sid,
            len(conversation_history),
        )

        # Persist call record to Supabase (post-call LLM extraction of name + phone).
        try:
            name, phone, email, summary, products = (None, None, None, None, [])
            if anthropic_client is not None and transcript:
                name, phone, email, summary, products = await extract_caller_details(
                    anthropic_client=anthropic_client,
                    transcript=transcript,
                    model=MODEL,
                )
            await record_call_end(
                call_sid=call_sid,
                transcript=transcript,
                started_at=session_started_at,
                captured_name=name,
                captured_phone=phone,
                captured_email=email,
                summary=summary,
            )

            # Real-estate lead post-call -> n8n -> Google Sheet (fire-and-forget).
            if transcript:
                asyncio.create_task(
                    process_realestate_post_call(
                        call_sid=call_sid,
                        caller_phone=(phone or from_number or ""),
                        transcript=transcript,
                        anthropic_client=anthropic_client,
                        model=MODEL,
                        started_at=session_started_at,
                    )
                )

            # Fire automation webhook only when we have a way to reach the caller.
            if phone or email:
                ended_at = datetime.now(timezone.utc)
                duration = None
                if session_started_at is not None:
                    try:
                        duration = max(
                            0,
                            int((ended_at - session_started_at).total_seconds()),
                        )
                    except Exception:
                        duration = None
                await send_automation_webhook({
                    "call_sid": call_sid,
                    "from_number": from_number,
                    "language": "en",
                    "started_at": session_started_at.isoformat() if session_started_at else None,
                    "ended_at": ended_at.isoformat(),
                    "duration_seconds": duration,
                    "captured_name": name,
                    "captured_phone": phone,
                    "captured_email": email,
                    "summary": summary,
                    "products": products,
                })
        except Exception:
            logger.exception("Failed to persist call record [%s]", call_sid)


# ---------------------------------------------------------------------------
# WebSocket -- Media Streams handler (Tamil)
# ---------------------------------------------------------------------------

@app.websocket("/ws/media-stream/{lang}")
async def ws_media_stream(websocket: WebSocket, lang: str):
    """Handle a Twilio Media Streams WebSocket session for Tamil or Sinhala.

    Language is encoded in the URL path (e.g. /ws/media-stream/ta or
    /ws/media-stream/si) so it is always present -- avoids unreliable
    query-string passing by Twilio.

    Receives raw mulaw 8 kHz audio from Twilio, runs Google Cloud STT
    (ta-IN for Tamil, si-LK for Sinhala), sends LLM responses through the
    language's TTS provider back as mulaw audio.
    """
    if lang not in ("ta", "si"):
        lang = "ta"

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
        logger.error("LLM client unavailable -- closing Media Streams WebSocket")
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
