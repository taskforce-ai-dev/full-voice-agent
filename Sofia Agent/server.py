"""
server.py -- Main FastAPI server for Abans Voice Agent (Sofia).

Handles:
  - IVR / DTMF language menu (POST /voice/incoming)
  - Language routing (POST /voice/language-selected)
  - ConversationRelay WebSocket (/ws/conversation?lang=en|ta)
  - Streaming LLM responses
  - Knowledge-base context injection
  - Health endpoint (GET /health)

Architecture:
  Incoming call
    -> POST /voice/incoming -> TwiML <Gather> (press 1/2)
    -> POST /voice/language-selected -> ConversationRelay TwiML
    -> WebSocket /ws/conversation?lang=...
    -> LLM streaming
    -> text tokens -> Twilio TTS -> caller

  TTS routing by language:
    English  -> ElevenLabs (flash_v2_5, cloned voice) via ConversationRelay
    Tamil    -> ElevenLabs (eleven_multilingual_v2, cloned voice) via Media Streams
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
from anthropic import AsyncAnthropic, NOT_GIVEN
from openai import AsyncOpenAI
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import Response

from knowledge_base import retrieve_context, initialize_kb, prewarm, reload_kb_from_content

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
ELEVENLABS_MODEL_MULTILINGUAL: str = "eleven_multilingual_v2"
ELEVENLABS_MODEL_TURBO: str = "eleven_turbo_v2_5"
TWILIO_ACCOUNT_SID: str = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN: str = os.getenv("TWILIO_AUTH_TOKEN", "")
KB_DOCS_DIRECTORY: str = os.getenv("KB_DOCS_DIRECTORY", "knowledge_docs")
KB_RELOAD_SECRET: str = os.getenv("KB_RELOAD_SECRET", "")
PORT: int = int(os.getenv("PORT", "8000"))
AZURE_SPEECH_KEY: str = os.getenv("AZURE_SPEECH_KEY", "")
AZURE_SPEECH_REGION: str = os.getenv("AZURE_SPEECH_REGION", "southeastasia")

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

# ---------------------------------------------------------------------------
# IVR language configurations
# ---------------------------------------------------------------------------
# Maps DTMF digit -> language code
DIGIT_TO_LANG: dict[str, str] = {"1": "en", "2": "ta"}

# Per-language ConversationRelay TwiML configuration
LANGUAGE_CONFIGS: dict[str, dict[str, str]] = {
    "en": {
        "tts_provider": "ElevenLabs",
        "voice": "ZF6FPAbjXT4488VcRRnw-flash_v2_5",
        "language": "en-US",
        "welcome_greeting": "Welcome to Aa-bans! How may I help you today?",
        "extra_attrs": '        elevenlabsTextNormalization="on"\n',
    },
    "ta": {
        "tts_provider": "google",
        "voice": "ta-IN-Standard-A",
        "language": "ta-IN",
        "welcome_greeting": (
            "\u0BB5\u0BA3\u0B95\u0BCD\u0B95\u0BAE\u0BCD! "
            "Abans \u0B95\u0BCD\u0B95\u0BC1 "
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
# Media Streams -- Azure TTS + Google STT (Tamil)
# ---------------------------------------------------------------------------
AZURE_TTS_URL = "https://{region}.tts.speech.microsoft.com/cognitiveservices/v1"
ELEVENLABS_TTS_URL = "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream"

# Azure voice per language code
AZURE_VOICES: dict[str, tuple[str, str]] = {
    "ta": ("ta-LK", "ta-LK-SaranyaNeural"),   # female voice
}

# Google STT primary + alternative languages per lang code
STT_PRIMARY: dict[str, str] = {"ta": "ta-IN"}
STT_ALTERNATIVES: dict[str, list[str]] = {
    "ta": ["en-US"],
}

# Silence (seconds) after last STT result before utterance is considered complete
ENDPOINTING_SILENCE: float = 1.5

# Welcome greetings for Media Streams (spoken via TTS on stream start)
MEDIA_STREAM_WELCOME: dict[str, str] = {
    "ta": (
        "\u0BB5\u0BA3\u0B95\u0BCD\u0B95\u0BAE\u0BCD! "
        "Abans \u0B95\u0BCD\u0B95\u0BC1 "
        "\u0BB5\u0BB0\u0BB5\u0BC7\u0BB1\u0BCD\u0B95\u0BBF\u0BB1\u0BCB\u0BAE\u0BCD. "
        "\u0BA8\u0BBE\u0BA9\u0BCD \u0B89\u0B99\u0BCD\u0B95\u0BB3\u0BC1\u0B95\u0BCD\u0B95\u0BC1 "
        "\u0B8E\u0BAA\u0BCD\u0BAA\u0B9F\u0BBF \u0B89\u0BA4\u0BB5\u0BB2\u0BBE\u0BAE\u0BCD?"
    ),
}

# Sentence boundary detection for streaming TTS
_SENTENCE_END = re.compile(r'(?<=[.!?\u0964\u0DF4])\s+')

# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

def _build_system_prompt(lang: str = "en") -> str:
    today = date.today().isoformat()

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
            "- Use proper Tamil grammar and a natural conversational tone.\n\n"
        )
    else:
        language_rules = (
            "LANGUAGE RULES:\n"
            "- The caller selected English. Respond only in English.\n"
            "- Use clear, simple English appropriate for all callers.\n\n"
        )

    return (
        f"You are Sofia, the friendly and knowledgeable customer service voice agent for "
        f"Aa-bans, one of Sri Lanka's leading retail companies. "
        f"Aa-bans offers home appliances, electronics, mobile "
        f"phones, Apple products, computers, kitchen appliances, fashion and lifestyle "
        f"goods, MINISO products, personal care, kids toys, furniture, and more.\n"
        f"Today's date is {today}.\n\n"

        + language_rules +

        "PRONUNCIATION (CRITICAL â€” follow every single time):\n"
        "- The company name is spelled 'Abans' but you MUST ALWAYS write it as 'Aa-bans' "
        "in every response so the text-to-speech engine pronounces it correctly.\n"
        "- Similarly write 'Buy Aa-bans' instead of 'BuyAbans'.\n"
        "- NEVER write 'Abans' or 'BuyAbans' â€” ALWAYS 'Aa-bans' or 'Buy Aa-bans'.\n\n"

        "GREETING:\n"
        "- At the start of the call, ask the caller for their name so you can "
        "address them personally. For example: 'May I have your name please?'\n"
        "- Once you have their name, use it naturally throughout the conversation.\n"
        "- After that, focus entirely on helping them with their questions.\n\n"

        "NUMBER UNDERSTANDING (CRITICAL):\n"
        "- When callers say 'double' before a digit, it means that digit repeated twice. "
        "For example: 'double 3' = 33, 'double 7' = 77, 'double zero' = 00.\n"
        "- When callers say 'triple' before a digit, it means that digit repeated three times. "
        "For example: 'triple 7' = 777, 'triple 3' = 333, 'triple zero' = 000.\n"
        "- Apply this to phone numbers, email addresses, and any other numbers.\n"
        "- When reading back numbers, you may use 'double' and 'triple' yourself for clarity.\n\n"

        "VOICE RULES (you are speaking on a phone call, not writing text):\n"
        "- Keep every response to one or two short sentences.\n"
        "- Ask only ONE question per response. Never ask two questions at once.\n"
        "- Never use markdown, bullet points, numbered lists, asterisks, or URLs.\n"
        "- Use natural spoken language. Say numbers as words.\n"
        "- Do not use abbreviations. Say 'rupees' not 'LKR'.\n"
        "- Never read out full product lists or price lists. Mention only the most relevant items.\n"
        "- Pause naturally between ideas by using short sentences.\n\n"

        "IMPORTANT RULES:\n"
        "- Answer questions about Aa-bans products, product categories, brands, prices, "
        "services, warranties, store locations, delivery, promotions, hire purchase, "
        "loyalty rewards, and policies using ONLY the information provided in the "
        "reference context.\n"
        "- If the caller asks about a specific product price, provide the current "
        "discounted price and mention the original price and discount percentage.\n"
        "- If the caller wants to buy something, let them know they can shop online at "
        "buy aa-bans dot com or visit the nearest Aa-bans showroom.\n"
        "- For service or repair inquiries, direct them to aa-ban service dot lk or the "
        "hotline at zero one one two, two two two, eight eight eight.\n"
        "- For order tracking, direct them to buy aa-bans dot com slash track your order.\n"
        "- If the answer is not in the reference context, politely say you don't have "
        "that information and suggest the caller visit buy aa-bans dot com or call the hotline.\n"
        "- You do NOT have any tools. Do not promise to check stock, place orders, "
        "or perform any actions. You can only provide information.\n"
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
    logger.info("Starting Abans Voice Agent server...")

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

    logger.info("Server startup complete.")

    yield

    # --- Shutdown ---
    logger.info("Shutting down server...")
    logger.info("Shutdown complete.")


# ---------------------------------------------------------------------------
# FastAPI application
# ---------------------------------------------------------------------------
app = FastAPI(
    title="Abans Voice Agent (Sofia)",
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
    loop.run_in_executor(None, reload_kb_from_content, content, filename)
    logger.info("KB reload triggered for '%s' (%d chars)", filename, len(content))
    return {"ok": True, "message": f"KB reload started for {filename}"}


# ---------------------------------------------------------------------------
# Twilio incoming call webhook
# ---------------------------------------------------------------------------

@app.post("/voice/incoming")
async def voice_incoming(request: Request) -> Response:
    """Twilio webhook for incoming phone calls.

    Returns TwiML with a DTMF <Gather> menu so the caller can choose
    their language (1 = English, 2 = Tamil). If no input
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
        "Welcome to Aebans. "
        "For English, press one. "
        "For Tamil, press two."
        "</Say>\n"
        "  </Gather>\n"
        '  <Say language="en-US">No input received. Connecting you in English.</Say>\n'
        "  <Connect>\n"
        f"    {fallback_cr}\n"
        "  </Connect>\n"
        "</Response>"
    )

    logger.info("Incoming call from %s -- returning IVR Gather TwiML",
                request.headers.get("x-forwarded-for", "unknown"))

    return Response(content=twiml, media_type="application/xml")


# ---------------------------------------------------------------------------
# Language selection handler (called by Twilio after DTMF digit)
# ---------------------------------------------------------------------------

@app.post("/voice/language-selected")
async def voice_language_selected(request: Request) -> Response:
    """Handle the caller's DTMF language selection.

    English (1) -> ConversationRelay TwiML (ElevenLabs TTS, text-in/text-out).
    Tamil (2) -> Media Streams TwiML (ElevenLabs TTS, Google STT,
    full audio control).
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
        # Tamil -- Media Streams with ElevenLabs TTS + Google STT
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


# ---------------------------------------------------------------------------
# Media Stream Session (Tamil calls)
# ---------------------------------------------------------------------------

class MediaStreamSession:
    """Manages a single Twilio Media Streams call for Tamil.

    Pipeline per turn:
      Google STT -> endpointing -> KB retrieval -> LLM (streaming)
      -> TTS -> mulaw audio -> Twilio
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
        self.tools = []

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

    # -- Main event loop ---------------------------------------------------

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
                        "Media stream started -- Call: %s, Stream: %s, lang: %s",
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
                        logger.info("TTS done -- listening for guest speech [%s]", self.call_sid)

                elif event == "stop":
                    logger.info("Media stream stopped -- Call: %s", self.call_sid)
                    break

        except WebSocketDisconnect:
            logger.info("Media stream disconnected -- Call: %s", self.call_sid)
        except Exception:
            logger.exception("Media stream error -- Call: %s", self.call_sid)
        finally:
            if self._stt:
                self._stt.stop()
            if self._endpointing_handle:
                self._endpointing_handle.cancel()
            logger.info(
                "Media stream session ended -- Call: %s, history: %d msgs",
                self.call_sid, len(self.history),
            )

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
        async with self._ws_lock:
            await self.ws.send_text(json.dumps({
                "event": "clear",
                "streamSid": self.stream_sid,
            }))

    # -- Endpointing -------------------------------------------------------

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

    # -- Utterance -> KB + LLM + TTS ---------------------------------------

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
        except Exception:
            logger.exception("LLM error [%s]", self.call_sid)
            error_msg = "I'm sorry, I encountered a technical issue. Please try again." if self.lang == "en" else "\u0BAE\u0BA9\u0BCD\u0BA9\u0BBF\u0B95\u0BCD\u0B95\u0BB5\u0BC1\u0BAE\u0BCD, \u0B92\u0BB0\u0BC1 \u0BA4\u0BCA\u0BB4\u0BBF\u0BB2\u0BCD\u0BA8\u0BC1\u0B9F\u0BCD\u0BAA \u0B9A\u0BBF\u0B95\u0BCD\u0B95\u0BB2\u0BCD \u0B8F\u0BB1\u0BCD\u0BAA\u0B9F\u0BCD\u0B9F\u0BA4\u0BC1."
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
            system=self.system_prompt,
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

        Tamil  -> ElevenLabs eleven_multilingual_v2 (cloned voice)
        """
        async with self._speak_lock:
            if generation >= 0 and generation != self._speak_generation:
                return
            if self.lang == "ta":
                await self._tts_elevenlabs(text)
            else:
                lang_code, voice_name = AZURE_VOICES[self.lang]
                await self._tts_azure(text, lang_code, voice_name)

    # -- ElevenLabs TTS (Tamil) --------------------------------------------

    async def _tts_elevenlabs(self, text: str):
        """Stream text via ElevenLabs eleven_multilingual_v2 and send mulaw audio to Twilio.
        Must only be called from _speak (lock already held).
        """
        if not ELEVENLABS_API_KEY or not ELEVENLABS_VOICE_ID:
            logger.warning("ElevenLabs not configured -- skipping TTS")
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

    # -- Azure TTS ---------------------------------------------------------

    async def _tts_azure(self, text: str, lang_code: str, voice_name: str):
        """Stream Azure Cognitive Services TTS as mulaw 8 kHz to Twilio.
        Must only be called from _speak (lock already held).
        """
        if not AZURE_SPEECH_KEY:
            logger.warning("AZURE_SPEECH_KEY not set -- skipping TTS")
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

    async with client.messages.stream(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=system,
        messages=conversation_history,
        tools=NOT_GIVEN,
    ) as stream:
        async for event in stream:
            if event.type == "content_block_delta":
                if event.delta.type == "text_delta":
                    text_content += event.delta.text
                    await websocket.send_text(
                        json.dumps({"type": "text", "token": event.delta.text})
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

    logger.info("Claude response complete (%d chars)", len(full_response_text))
    return full_response_text


# ---------------------------------------------------------------------------
# WebSocket -- ConversationRelay handler
# ---------------------------------------------------------------------------

@app.websocket("/ws/conversation")
async def ws_conversation(websocket: WebSocket, lang: str = "en"):
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
    logger.info("WebSocket connection accepted -- language: %s", lang)

    # -- Per-session state --
    conversation_history: list[dict] = []
    system_prompt: str = _build_system_prompt(lang)
    tools: list[dict] = []
    call_sid: str = "unknown"

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
                logger.info(
                    "Session setup -- CallSid: %s, StreamSid: %s",
                    call_sid,
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
                    if LLM_PROVIDER == "claude":
                        response_text = await _run_llm_streaming_claude(
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

    except WebSocketDisconnect:
        logger.info("WebSocket disconnected -- CallSid: %s", call_sid)
    except Exception:
        logger.exception("Unexpected error in WebSocket handler [%s]", call_sid)
    finally:
        logger.info(
            "Session ended -- CallSid: %s, history length: %d messages",
            call_sid,
            len(conversation_history),
        )


# ---------------------------------------------------------------------------
# WebSocket -- Media Streams handler (Tamil)
# ---------------------------------------------------------------------------

@app.websocket("/ws/media-stream/{lang}")
async def ws_media_stream(websocket: WebSocket, lang: str):
    """Handle a Twilio Media Streams WebSocket session for Tamil.

    Language is encoded in the URL path (e.g. /ws/media-stream/ta) so it
    is always present -- avoids unreliable query-string passing by Twilio.

    Receives raw mulaw 8 kHz audio from Twilio, runs Google Cloud STT,
    sends LLM responses through TTS back as mulaw audio.
    """
    if lang != "ta":
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
