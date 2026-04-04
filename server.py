"""
server.py — Main FastAPI server for Treehouse Chalets Voice Agent (Kavya).

Handles:
  - IVR / DTMF language menu (POST /voice/incoming)
  - Language routing (POST /voice/language-selected)
  - ConversationRelay WebSocket (/ws/conversation?lang=en|si|ta)
  - Streaming Claude responses with tool-use support
  - Knowledge-base context injection
  - Health endpoint (GET /health)

Architecture:
  Incoming call
    → POST /voice/incoming → TwiML <Gather> (press 1/2/3)
    → POST /voice/language-selected → ConversationRelay TwiML
    → WebSocket /ws/conversation?lang=...
    → Claude streaming with tool use
    → text tokens → Twilio TTS → caller

  TTS routing by language:
    English  → ElevenLabs (flash_v2_5, cloned voice) via ConversationRelay
    Sinhala  → Azure Cognitive Services (si-LK-ThiliniNeural) via Media Streams
    Tamil    → Azure Cognitive Services (ta-LK-SaranyaNeural) via Media Streams
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import queue
import re
import threading
import xml.sax.saxutils
from contextlib import asynccontextmanager
from datetime import date
from typing import Any

import httpx
from openai import AsyncOpenAI
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import Response

from knowledge_base import retrieve_context, initialize_kb, prewarm
from tools import get_tools_openai, get_tools_gemini, execute_tool
from booking_api import close_session, is_configured

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
OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "")
ELEVENLABS_API_KEY: str = os.getenv("ELEVENLABS_API_KEY", "")
ELEVENLABS_VOICE_ID: str = os.getenv("ELEVENLABS_VOICE_ID", "")
ELEVENLABS_MODEL_MULTILINGUAL: str = "eleven_multilingual_v2"
ELEVENLABS_MODEL_TURBO: str = "eleven_turbo_v2_5"
TWILIO_ACCOUNT_SID: str = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN: str = os.getenv("TWILIO_AUTH_TOKEN", "")
KB_DOCS_DIRECTORY: str = os.getenv("KB_DOCS_DIRECTORY", "knowledge_docs")
PORT: int = int(os.getenv("PORT", "8000"))
AZURE_SPEECH_KEY: str = os.getenv("AZURE_SPEECH_KEY", "")
AZURE_SPEECH_REGION: str = os.getenv("AZURE_SPEECH_REGION", "southeastasia")

# LLM provider selection: "openai" (default) or "gemini"
LLM_PROVIDER: str = os.getenv("LLM_PROVIDER", "openai")
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
# LLM configuration
# ---------------------------------------------------------------------------
if LLM_PROVIDER == "gemini":
    MODEL: str = GEMINI_MODEL
else:
    MODEL: str = os.getenv("OPENAI_MODEL", "gpt-4o")
MAX_TOKENS: int = 300
MAX_HISTORY_MESSAGES: int = 20
MAX_TOOL_ROUNDS: int = 5

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

# ---------------------------------------------------------------------------
# IVR language configurations
# ---------------------------------------------------------------------------
# Maps DTMF digit → language code
DIGIT_TO_LANG: dict[str, str] = {"1": "en", "2": "si", "3": "ta"}

# Per-language ConversationRelay TwiML configuration
LANGUAGE_CONFIGS: dict[str, dict[str, str]] = {
    "en": {
        "tts_provider": "ElevenLabs",
        "voice": "ZF6FPAbjXT4488VcRRnw-flash_v2_5",
        "language": "en-US",
        "welcome_greeting": "Welcome to Treehouse Chalets! How may I help you today?",
        "extra_attrs": '        elevenlabsTextNormalization="on"\n',
    },
    "si": {
        "tts_provider": "google",
        "voice": "si-LK-Standard-A",
        "language": "si-LK",
        "welcome_greeting": (
            "\u0D86\u0DBA\u0DD4\u0DB6\u0DDD\u0DC0\u0DB1\u0DCA! "
            "Treehouse Chalets \u0DC0\u0DD9\u0DAD "
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
            "Treehouse Chalets \u0B95\u0BCD\u0B95\u0BC1 "
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
STT_PRIMARY: dict[str, str] = {"si": "si-LK", "ta": "ta-IN"}
STT_ALTERNATIVES: dict[str, list[str]] = {
    "si": ["en-US", "ta-IN"],
    "ta": ["en-US", "si-LK"],
}

# Silence (seconds) after last STT result before utterance is considered complete
ENDPOINTING_SILENCE: float = 1.5

# Welcome greetings for Media Streams (spoken via Azure TTS on stream start)
MEDIA_STREAM_WELCOME: dict[str, str] = {
    "si": (
        "\u0D86\u0DBA\u0DD4\u0DB6\u0DDD\u0DC0\u0DB1\u0DCA! "
        "Treehouse Chalets \u0DC0\u0DD9\u0DAD "
        "\u0DC3\u0DCF\u0DAF\u0DBB\u0DBA\u0DD9\u0DB1\u0DCA "
        "\u0DB4\u0DD2\u0DC5\u0DD2\u0D9C\u0DB1\u0DD2\u0DB8\u0DD4. "
        "\u0DB8\u0DA7 \u0D94\u0DB6\u0DA7 \u0D9A\u0DD9\u0DC3\u0DDA "
        "\u0D8B\u0DAF\u0DC0\u0DCA \u0D9A\u0DC5 \u0DC4\u0DD0\u0D9A\u0DD2\u0DAF?"
    ),
    "ta": (
        "\u0BB5\u0BA3\u0B95\u0BCD\u0B95\u0BAE\u0BCD! "
        "Treehouse Chalets \u0B95\u0BCD\u0B95\u0BC1 "
        "\u0BB5\u0BB0\u0BB5\u0BC7\u0BB1\u0BCD\u0B95\u0BBF\u0BB1\u0BCB\u0BAE\u0BCD. "
        "\u0BA8\u0BBE\u0BA9\u0BCD \u0B89\u0B99\u0BCD\u0B95\u0BB3\u0BC1\u0B95\u0BCD\u0B95\u0BC1 "
        "\u0B8E\u0BAA\u0BCD\u0BAA\u0B9F\u0BBF \u0B89\u0BA4\u0BB5\u0BB2\u0BBE\u0BAE\u0BCD?"
    ),
}

# Tool filler messages in Sinhala and Tamil (spoken during tool execution)
MEDIA_STREAM_FILLERS: dict[str, dict[str, str]] = {
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
    else:
        language_rules = (
            "LANGUAGE RULES:\n"
            "- The caller selected English. Respond only in English.\n"
            "- Use clear, simple English appropriate for international callers.\n\n"
        )

    return (
        f"You are Kavya, the warm and gracious reservations voice agent for "
        f"Treehouse Chalets, Belihuloya, Sri Lanka.\n"
        f"Today's date is {today}.\n\n"

        + language_rules +

        "VOICE RULES (you are speaking on a phone call, not writing text):\n"
        "- Keep every response to one or two short sentences.\n"
        "- Never use markdown, bullet points, numbered lists, asterisks, or URLs.\n"
        "- Use natural spoken language. Say numbers as words.\n"
        "- Do not use abbreviations. Say 'rupees' not 'LKR'.\n"
        "- Never read out full rate lists. Mention only the relevant room.\n"
        "- Pause naturally between ideas by using short sentences.\n\n"

        "IMPORTANT RULES:\n"
        "- For general questions about room types, prices, amenities, policies, "
        "activities, or hotel info, answer directly from the hotel information "
        "provided in context. Do NOT ask for dates or call any tool for general "
        "info questions.\n"
        "- Only use the check_availability tool when the guest wants to actually "
        "BOOK a room or specifically asks if rooms are available on certain dates.\n"
        "- When a guest wants to book, collect in this order: name, location "
        "(to determine local vs foreign rates), number of guests (adults and "
        "children with ages), dates, room preference.\n"
        "- Always check availability before creating a booking.\n"
        "- Confirm all details with the guest before finalizing.\n"
        "- Always mention that nature walks, night walks, and stargazing are "
        "complimentary for stays of 2 or more nights.\n"
        "- If April or December dates are mentioned, note that 50% advance "
        "payment is required.\n"
        "- If a honeymoon or anniversary is detected, proactively mention the "
        "candlelit dinner package.\n"
        "- Be empathetic and attentive. If a guest seems frustrated, acknowledge "
        "their feelings.\n"
        "- If you do not have enough information to use a tool, ask the guest "
        "for the missing details.\n"
    )


# ---------------------------------------------------------------------------
# LLM client (module-level singletons)
# ---------------------------------------------------------------------------
_openai_client: AsyncOpenAI | None = None
_gemini_client: Any = None  # google.genai.Client when available


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
    # Build tool_call_id → tool_name map from assistant messages
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
    logger.info("Starting Treehouse Chalets Voice Agent server...")

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
        if LLM_PROVIDER == "gemini":
            _get_gemini_client()
        else:
            _get_client()
    except RuntimeError as exc:
        logger.error("Cannot create LLM client: %s", exc)

    if not ELEVENLABS_API_KEY or not ELEVENLABS_VOICE_ID:
        logger.warning("ELEVENLABS_API_KEY or ELEVENLABS_VOICE_ID not set — "
                       "ConversationRelay TTS will not work in production.")

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
    title="Treehouse Chalets Voice Agent (Kavya)",
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
        "azure_tts": bool(AZURE_SPEECH_KEY),
    }


# ---------------------------------------------------------------------------
# Twilio incoming call webhook
# ---------------------------------------------------------------------------

@app.post("/voice/incoming")
async def voice_incoming(request: Request) -> Response:
    """Twilio webhook for incoming phone calls.

    Returns TwiML with a DTMF <Gather> menu so the caller can choose
    their language (1 = English, 2 = Sinhala, 3 = Tamil). If no input
    is received, falls back to English.
    """
    host = request.headers.get("host", request.url.hostname or "localhost")

    # Build the fallback English ConversationRelay TwiML (used when no digit pressed)
    en = LANGUAGE_CONFIGS["en"]
    fallback_cr = _build_conversation_relay_twiml(host, "en", en)

    twiml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<Response>\n"
        '  <Gather numDigits="1" action="/voice/language-selected" method="POST"'
        ' timeout="10">\n'
        '    <Say language="en-US">'
        "Welcome to Treehouse Chalets. "
        "For English, press 1. "
        "For Sinhala, press 2. "
        "For Tamil, press 3."
        "</Say>\n"
        "  </Gather>\n"
        '  <Say language="en-US">No input received. Connecting you in English.</Say>\n'
        "  <Connect>\n"
        f"    {fallback_cr}\n"
        "  </Connect>\n"
        "</Response>"
    )

    logger.info("Incoming call from %s — returning IVR Gather TwiML",
                request.headers.get("x-forwarded-for", "unknown"))

    return Response(content=twiml, media_type="application/xml")


# ---------------------------------------------------------------------------
# Language selection handler (called by Twilio after DTMF digit)
# ---------------------------------------------------------------------------

@app.post("/voice/language-selected")
async def voice_language_selected(request: Request) -> Response:
    """Handle the caller's DTMF language selection.

    English (1) → ConversationRelay TwiML (ElevenLabs TTS, text-in/text-out).
    Sinhala (2) / Tamil (3) → Media Streams TwiML (Azure TTS, Google STT,
    full audio control).
    """
    form = await request.form()
    digit = str(form.get("Digits", "1"))
    lang = DIGIT_TO_LANG.get(digit, "en")
    host = request.headers.get("host", request.url.hostname or "localhost")

    if lang == "en":
        # English — ConversationRelay with ElevenLabs
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
        # Sinhala / Tamil — Media Streams with Azure TTS + Google STT
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


def _build_conversation_relay_twiml(
    host: str, lang: str, config: dict[str, str]
) -> str:
    """Build the <ConversationRelay> XML tag for the given language config."""
    extra = config["extra_attrs"]
    # XML-escape the welcome greeting in case it contains special characters
    greeting = xml.sax.saxutils.escape(config["welcome_greeting"])

    return (
        f'<ConversationRelay url="wss://{host}/ws/conversation?lang={lang}"\n'
        f'        ttsProvider="{config["tts_provider"]}"\n'
        f'        voice="{config["voice"]}"\n'
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


# ---------------------------------------------------------------------------
# Conversation history management
# ---------------------------------------------------------------------------

def _trim_history(history: list[dict], max_messages: int = MAX_HISTORY_MESSAGES) -> list[dict]:
    """Keep conversation history within bounds.

    Trims from the front (oldest messages) so the most recent context is
    always preserved. Skips orphaned tool results and assistant messages
    with tool_calls whose results have been trimmed.
    """
    if len(history) <= max_messages:
        return history

    trimmed = history[-max_messages:]

    # Skip leading messages that are orphaned:
    # - role "tool" needs a preceding assistant with tool_calls
    # - assistant with tool_calls needs following tool messages
    while trimmed:
        role = trimmed[0].get("role")
        if role == "tool":
            trimmed.pop(0)
        elif role == "assistant" and trimmed[0].get("tool_calls"):
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


# ---------------------------------------------------------------------------
# Media Stream Session (Sinhala / Tamil calls)
# ---------------------------------------------------------------------------

class MediaStreamSession:
    """Manages a single Twilio Media Streams call for Sinhala or Tamil.

    Pipeline per turn:
      Google STT → endpointing → KB retrieval → Claude (streaming + tools)
      → Azure TTS → mulaw audio → Twilio
    """

    def __init__(
        self,
        websocket: WebSocket,
        client: AsyncOpenAI | None,
        lang: str,
        gemini_client=None,
    ):
        self.ws = websocket
        self.client = client
        self.gemini_client = gemini_client
        self.lang = lang
        self.system_prompt = _build_system_prompt(lang)
        self.tools = get_tools_gemini() if LLM_PROVIDER == "gemini" else get_tools_openai()

        self.stream_sid: str | None = None
        self.call_sid: str = "unknown"
        self.history: list[dict] = []

        self._event_loop: asyncio.AbstractEventLoop | None = None
        self._is_speaking = False
        self._speak_lock = asyncio.Lock()
        self._ws_lock = asyncio.Lock()
        self._speak_generation: int = 0

        self._pending_transcript = ""
        self._latest_interim = ""
        self._endpointing_handle: asyncio.TimerHandle | None = None
        self._stt: GoogleSTTStream | None = None

    # ── Main event loop ───────────────────────────────────────────────────

    async def run(self):
        self._event_loop = asyncio.get_running_loop()
        await self.ws.accept()
        logger.info("Media stream WebSocket accepted (lang=%s)", self.lang)

        self._stt = GoogleSTTStream(
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
                    logger.info(
                        "Media stream started — Call: %s, Stream: %s, lang: %s",
                        self.call_sid, self.stream_sid, self.lang,
                    )
                    asyncio.ensure_future(
                        self._speak(MEDIA_STREAM_WELCOME[self.lang])
                    )

                elif event == "media":
                    audio = base64.b64decode(msg["media"]["payload"])
                    if self._stt:
                        self._stt.feed(audio)

                elif event == "mark":
                    mark_name = msg.get("mark", {}).get("name")
                    logger.info("Mark received [%s]: %s", self.call_sid, mark_name)
                    if mark_name == "tts_done":
                        self._is_speaking = False
                        logger.info("TTS done — listening for guest speech [%s]", self.call_sid)

                elif event == "stop":
                    logger.info("Media stream stopped — Call: %s", self.call_sid)
                    break

        except WebSocketDisconnect:
            logger.info("Media stream disconnected — Call: %s", self.call_sid)
        except Exception:
            logger.exception("Media stream error — Call: %s", self.call_sid)
        finally:
            if self._stt:
                self._stt.stop()
            if self._endpointing_handle:
                self._endpointing_handle.cancel()
            logger.info(
                "Media stream session ended — Call: %s, history: %d msgs",
                self.call_sid, len(self.history),
            )

    # ── STT callback (called from background thread) ──────────────────────

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

    # ── Endpointing ───────────────────────────────────────────────────────

    async def _accumulate_transcript(self, text: str):
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
        await self._process_utterance(transcript)

    # ── Utterance → KB + Claude + TTS ─────────────────────────────────────

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
            if LLM_PROVIDER == "gemini":
                response_text = await self._run_llm_gemini()
            else:
                response_text = await self._run_llm()
            if response_text:
                logger.info("Agent [%s]: %s", self.call_sid, response_text[:200])
        except Exception:
            logger.exception("LLM error [%s]", self.call_sid)
            fillers = MEDIA_STREAM_FILLERS.get(self.lang, {})
            error_msg = fillers.get("_default", "I'm sorry, I encountered an error.")
            await self._speak(error_msg)

    # ── OpenAI streaming with tool use + sentence-level TTS ─────────────────

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
                    logger.info("Tool '%s' → %s", tc["name"], result_str[:200])

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

    # ── Gemini native streaming with tool use + sentence-level TTS ───────

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
                    logger.info("Tool '%s' → %s", tc["function"]["name"], result_str[:200])

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

    # ── TTS → Twilio mulaw audio ─────────────────────────────────────────

    async def _speak(self, text: str, generation: int = -1):
        """Route text to appropriate TTS provider.

        Tamil  → ElevenLabs eleven_multilingual_v2 (cloned voice)
        Sinhala → Azure Cognitive Services (si-LK-SameeraNeural)
        """
        async with self._speak_lock:
            if generation >= 0 and generation != self._speak_generation:
                return
            if self.lang == "ta":
                await self._tts_elevenlabs(text)
            else:
                lang_code, voice_name = AZURE_VOICES[self.lang]
                await self._tts_azure(text, lang_code, voice_name)

    # ── ElevenLabs TTS (Tamil) ───────────────────────────────────────────

    async def _tts_elevenlabs(self, text: str):
        """Stream text via ElevenLabs eleven_multilingual_v2 and send mulaw audio to Twilio.
        Must only be called from _speak (lock already held).
        """
        if not ELEVENLABS_API_KEY or not ELEVENLABS_VOICE_ID:
            logger.warning("ElevenLabs not configured — skipping TTS")
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

    # ── Azure TTS (Sinhala) ──────────────────────────────────────────────

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

            first_tool_name = tool_list[0]["name"]
            filler = TOOL_FILLERS.get(first_tool_name, DEFAULT_FILLER)
            await websocket.send_text(
                json.dumps({"type": "text", "token": filler, "last": True})
            )
            logger.info("Sent filler: '%s'", filler)

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

        full_response_text += text_content

        if function_calls:
            logger.info(
                "Gemini requested %d tool(s): %s",
                len(function_calls),
                [fc["name"] for fc in function_calls],
            )

            first_tool_name = function_calls[0]["name"]
            filler = TOOL_FILLERS.get(first_tool_name, DEFAULT_FILLER)
            await websocket.send_text(
                json.dumps({"type": "text", "token": filler, "last": True})
            )
            logger.info("Sent filler: '%s'", filler)

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
# WebSocket — ConversationRelay handler
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
    logger.info("WebSocket connection accepted — language: %s", lang)

    # -- Per-session state --
    conversation_history: list[dict] = []
    system_prompt: str = _build_system_prompt(lang)
    tools: list[dict] = get_tools_gemini() if LLM_PROVIDER == "gemini" else get_tools_openai()
    call_sid: str = "unknown"

    client = None
    gemini_client = None
    try:
        if LLM_PROVIDER == "gemini":
            gemini_client = _get_gemini_client()
        else:
            client = _get_client()
    except RuntimeError:
        logger.error("LLM client not available — closing WebSocket")
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
                logger.info(
                    "Session setup — CallSid: %s, StreamSid: %s",
                    call_sid,
                    message.get("streamSid", "n/a"),
                )
                # Session state is already initialized above.
                # Log any additional setup metadata.
                logger.info(
                    "Session ready — system prompt length: %d chars, tools: %d",
                    len(system_prompt),
                    len(tools),
                )

            # ---------------------------------------------------------------
            # PROMPT — user speech transcribed
            # ---------------------------------------------------------------
            elif msg_type == "prompt":
                user_text = message.get("voicePrompt", "").strip()
                if not user_text:
                    logger.debug("Empty voicePrompt received — ignoring")
                    continue

                logger.info("Guest [%s]: %s", call_sid, user_text)

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
                try:
                    if LLM_PROVIDER == "gemini":
                        response_text = await _run_llm_streaming_gemini(
                            gemini_client=gemini_client,
                            system=system_prompt,
                            conversation_history=conversation_history,
                            tools=tools,
                            websocket=websocket,
                        )
                    else:
                        response_text = await _run_llm_streaming(
                            client=client,
                            system=system_prompt,
                            conversation_history=conversation_history,
                            tools=tools,
                            websocket=websocket,
                        )
                    logger.info("Agent [%s]: %s", call_sid, response_text[:200])
                except WebSocketDisconnect:
                    logger.info("WebSocket disconnected during Claude streaming [%s]", call_sid)
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
        logger.info(
            "Session ended — CallSid: %s, history length: %d messages",
            call_sid,
            len(conversation_history),
        )


# ---------------------------------------------------------------------------
# WebSocket — Media Streams handler (Sinhala / Tamil)
# ---------------------------------------------------------------------------

@app.websocket("/ws/media-stream/{lang}")
async def ws_media_stream(websocket: WebSocket, lang: str):
    """Handle a Twilio Media Streams WebSocket session for Sinhala or Tamil.

    Language is encoded in the URL path (e.g. /ws/media-stream/si) so it
    is always present — avoids unreliable query-string passing by Twilio.

    Receives raw mulaw 8 kHz audio from Twilio, runs Google Cloud STT,
    sends Claude responses through Azure TTS back as mulaw audio.
    """
    if lang not in ("si", "ta"):
        lang = "si"

    client = None
    gemini_client = None
    try:
        if LLM_PROVIDER == "gemini":
            gemini_client = _get_gemini_client()
        else:
            client = _get_client()
    except RuntimeError:
        logger.error("LLM client unavailable — closing Media Streams WebSocket")
        await websocket.accept()
        await websocket.close(code=1011, reason="Server configuration error")
        return

    session = MediaStreamSession(
        websocket=websocket, client=client, lang=lang, gemini_client=gemini_client,
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
