"""
server.py -- Main FastAPI server for the Hutch Voice Agent (Selina).

Handles:
  - Dialog SmartPBX ("Client Connect") raw-audio ingress (primary, opt-in via
    HUTCH_SERVICE_MODE, default "smartpbx")
  - Twilio ConversationRelay WebSocket (/ws/conversation) -- present for fleet
    consistency but INERT: there is no Twilio number provisioned for Hutch
  - Streaming LLM responses grounded in a Hutch plans/services knowledge base
  - A single tool, `notify_human_handover`, for questions outside the KB
  - Health endpoint (GET /health)

Hutch/Selina is a pure KB/inquiries agent -- no PMS, no bookings, no live call
transfer. When a question is outside the knowledge base, Selina collects the
caller's name and a callback number and calls `notify_human_handover`, which
POSTs to an n8n webhook (see handover.py) so a human can follow up later.

Architecture:
  Dialog SmartPBX call
    -> WS /ws/v1/smartpbx/media (smartpbx_gateway.SmartPBXGateway)
    -> smartpbx_session.HutchSmartPBXSession binds the call into MediaStreamSession
    -> Google/Azure STT -> KB retrieval -> Claude (tool-use loop) -> ElevenLabs TTS
    -> g711_ulaw audio back to Dialog

  Twilio ConversationRelay (inert -- no Twilio number configured)
    -> POST /voice/incoming -> TwiML <Connect><ConversationRelay>
    -> WebSocket /ws/conversation?lang=en
    -> LLM streaming (tool-use loop) -> text tokens -> Twilio TTS -> caller

  TTS:
    ConversationRelay -> ElevenLabs (flash_v2_5) via Twilio's own TTS provider
    MediaStreamSession (SmartPBX / raw audio) -> ElevenLabs (turbo_v2_5) via
      direct API call, mulaw 8kHz output
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os

HUTCH_SERVICE_MODE: str = os.getenv("HUTCH_SERVICE_MODE", "smartpbx").strip().lower()

# Staff/operator WhatsApp number that a handover notification is sent to. Goes
# into the n8n payload as `human_agent_whatsapp`. Left blank by default (the n8n
# workflow may hardcode its own recipient); set in the deploy .env otherwise.
HUTCH_HUMAN_AGENT_WHATSAPP: str = os.getenv("HUTCH_HUMAN_AGENT_WHATSAPP", "").strip()

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
    sentry_sdk.set_tag("agent", "hutch")
import queue
import re
import threading
import time
import xml.sax.saxutils
from contextlib import asynccontextmanager
from datetime import date
from typing import Any

import httpx
from anthropic import AsyncAnthropic, NOT_GIVEN
from openai import AsyncOpenAI
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import Response

from knowledge_base import retrieve_context, initialize_kb, prewarm, reload_kb_from_content
from handover import handover_context
from tools import get_tools_for, execute_tool

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
ELEVENLABS_MODEL_TURBO: str = "eleven_turbo_v2_5"
# No Twilio number exists for Hutch. These are read (and TwiML routes stay
# wired) purely for fleet consistency -- they are unconfigured/inert.
TWILIO_ACCOUNT_SID: str = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN: str = os.getenv("TWILIO_AUTH_TOKEN", "")
KB_DOCS_DIRECTORY: str = os.getenv("KB_DOCS_DIRECTORY", "knowledge_docs")
KB_RELOAD_SECRET: str = os.getenv("KB_RELOAD_SECRET", "")
PORT: int = int(os.getenv("PORT", "8000"))
AZURE_SPEECH_KEY: str = os.getenv("AZURE_SPEECH_KEY", "")
AZURE_SPEECH_REGION: str = os.getenv("AZURE_SPEECH_REGION", "southeastasia")

# Media Streams / SmartPBX STT backend: "google" (default) | "azure"
STT_PROVIDER: str = os.getenv("STT_PROVIDER", "google").lower()

# LLM provider selection: "claude" (default), "openai", or "gemini".
# NOTE: the notify_human_handover tool is only wired into the Claude
# streaming path below. OpenAI/Gemini remain KB-grounded text providers
# without tool-calling, matching Sofia Agent's original scope for those two
# providers -- see "Known limitations" in CLAUDE.md before switching
# LLM_PROVIDER away from "claude" in production.
LLM_PROVIDER: str = os.getenv("LLM_PROVIDER", "claude")
CLAUDE_MODEL: str = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-5-20250929")
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
# Optional: Google Cloud Speech (Media Streams / SmartPBX STT)
# ---------------------------------------------------------------------------
try:
    from google.cloud import speech_v1 as google_speech
    GOOGLE_STT_AVAILABLE = True
except ImportError:
    google_speech = None  # type: ignore[assignment]
    GOOGLE_STT_AVAILABLE = False
    logger.warning("google-cloud-speech not installed -- Media Streams STT unavailable")

# ---------------------------------------------------------------------------
# Optional: Azure Speech (alternative Media Streams / SmartPBX STT)
# ---------------------------------------------------------------------------
try:
    import audioop
except ImportError:
    audioop = None  # type: ignore[assignment]

try:
    import azure.cognitiveservices.speech as azure_speech
    AZURE_STT_AVAILABLE = True
except ImportError:
    azure_speech = None  # type: ignore[assignment]
    AZURE_STT_AVAILABLE = False
    logger.warning("azure-cognitiveservices-speech not installed -- Azure STT unavailable")

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
MAX_TOOL_ROUNDS: int = 3

# ---------------------------------------------------------------------------
# Language config -- English only (no IVR/DTMF menu needed)
# ---------------------------------------------------------------------------
LANGUAGE_CONFIGS: dict[str, dict[str, str]] = {
    "en": {
        "tts_provider": "ElevenLabs",
        "voice": "ZF6FPAbjXT4488VcRRnw-flash_v2_5",
        "language": "en-US",
        "welcome_greeting": "Thank you for calling Hutch. This is Selina. How can I help you today?",
        "extra_attrs": '        elevenlabsTextNormalization="on"\n',
    },
}

# ---------------------------------------------------------------------------
# Media Streams / SmartPBX -- TTS + STT (English only)
# ---------------------------------------------------------------------------
ELEVENLABS_TTS_URL = "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream"

STT_PRIMARY: dict[str, str] = {"en": "en-US"}
STT_ALTERNATIVES: dict[str, list[str]] = {"en": []}

# Silence (seconds) after last STT result before an utterance is considered complete
ENDPOINTING_SILENCE: float = 1.0

# Barge-in echo gating -- Dialog SmartPBX has no echo cancellation on the media
# path, so Selina's own TTS otherwise leaks back into STT and is misread as the
# caller interrupting. A genuine interruption is required to be at least this
# many characters and to arrive after a short debounce from when TTS started;
# anything shorter/earlier is treated as echo and dropped rather than acted on.
BARGEIN_MIN_CHARS: int = max(0, min(200, int(os.getenv("BARGEIN_MIN_CHARS", "12"))))
BARGEIN_DEBOUNCE_SECONDS: float = max(0.0, min(5.0, float(os.getenv("BARGEIN_DEBOUNCE_SECONDS", "0.6"))))

# Welcome greeting spoken via TTS on stream start (Media Streams / SmartPBX)
MEDIA_STREAM_WELCOME: dict[str, str] = {
    "en": "Thank you for calling Hutch. This is Selina. How can I help you today?",
}

# Sentence boundary detection for streaming TTS
_SENTENCE_END = re.compile(r'(?<=[.!?])\s+')

# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

def _build_system_prompt() -> str:
    today = date.today().isoformat()

    return (
        "You are Selina, the friendly and knowledgeable voice agent for Hutch "
        "Enterprise inquiries -- Hutch is one of Sri Lanka's leading mobile "
        "network operators. Today's date is " + today + ".\n\n"

        "LANGUAGE RULES:\n"
        "- Respond only in clear, simple English appropriate for all callers.\n\n"

        "VOICE RULES (you are speaking on a phone call, not writing text):\n"
        "- Keep every response to one or two short sentences.\n"
        "- Ask only ONE question per response. Never ask two questions at once.\n"
        "- Never use markdown, bullet points, numbered lists, asterisks, or URLs.\n"
        "- Use natural spoken language. Say numbers as words (e.g. 'one thousand "
        "one hundred and ninety nine rupees', not 'Rs. 1199').\n"
        "- Do not use abbreviations. Say 'rupees' not 'Rs.', say 'gigabytes' not 'GB'.\n"
        "- Never read out long lists of plans or every FAQ answer at once. Answer "
        "what was asked, then offer to go deeper if the caller wants.\n"
        "- Pause naturally between ideas by using short sentences.\n\n"

        "NUMBER UNDERSTANDING (CRITICAL):\n"
        "- When callers say 'double' before a digit, it means that digit repeated "
        "twice. For example: 'double 7' = 77, 'double zero' = 00.\n"
        "- When callers say 'triple' before a digit, it means that digit repeated "
        "three times. For example: 'triple 7' = 777.\n"
        "- Apply this to phone numbers and any other numbers a caller dictates.\n\n"

        "IMPORTANT RULES:\n"
        "- Answer questions about Hutch mobile plans, pricing, data allowances, "
        "validity periods, activation methods, eligibility, and policies using "
        "ONLY the information provided in the reference context.\n"
        "- Never invent a price, a data allowance, or a plan name that is not in "
        "the reference context. If unsure, say you don't have that detail handy.\n"
        "- For account-specific matters -- checking a caller's own balance, "
        "billing disputes, SIM replacement for their number, enterprise contract "
        "negotiation, or technical faults on their line -- these cannot be "
        "resolved on this call. Explain that plainly, and offer to have a Hutch "
        "team member follow up with them.\n"
        "- If a question is genuinely outside the knowledge base (you don't have "
        "the answer and it isn't an account-specific matter either), say so "
        "honestly rather than guessing, and offer the same follow-up.\n"
        "- To arrange a follow-up: ask for the caller's name, then ask for the "
        "best WhatsApp or callback number, confirming each one back to the "
        "caller before moving on. Once you have both, call the "
        "notify_human_handover tool with a short summary of what they needed. "
        "After it returns, tell the caller a Hutch team member will be in touch, "
        "and mention they can also call the Hutch Hotline on one seven eight "
        "eight in the meantime.\n"
        "- Never call notify_human_handover more than once per call, and never "
        "call it before you have both a name and a number.\n"
        "- You have no ability to activate, cancel, or change any plan yourself. "
        "Direct callers who want to activate a plan to the Hutch App, the Hutch "
        "website, or the USSD codes mentioned in the reference context.\n"
        "- Be warm, helpful, and professional. If a caller seems frustrated, "
        "acknowledge their feelings empathetically.\n"
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


# ---------------------------------------------------------------------------
# Application lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup / shutdown lifecycle for the FastAPI application."""
    # --- Startup ---
    logger.info("Starting Hutch Voice Agent server (service_mode=%s)...", HUTCH_SERVICE_MODE)

    logger.info("Initializing knowledge base from '%s'...", KB_DOCS_DIRECTORY)
    kb_ok = initialize_kb(KB_DOCS_DIRECTORY)
    if kb_ok:
        logger.info("Knowledge base initialized successfully.")
    else:
        logger.warning("Knowledge base initialization failed -- continuing without KB.")

    prewarm()

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
        logger.warning(
            "ELEVENLABS_API_KEY or ELEVENLABS_VOICE_ID not set -- "
            "TTS will not work in production."
        )

    logger.info("Server startup complete.")

    yield

    # --- Shutdown ---
    logger.info("Shutting down server...")
    logger.info("Shutdown complete.")


# ---------------------------------------------------------------------------
# FastAPI application (Twilio / default surface -- present but inert; no
# Twilio number is provisioned for Hutch)
# ---------------------------------------------------------------------------
app = FastAPI(
    title="Hutch Voice Agent (Selina)",
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
        "service_mode": HUTCH_SERVICE_MODE,
        "llm_provider": LLM_PROVIDER,
        "model": MODEL,
        "kb_loaded": os.path.isdir(KB_DOCS_DIRECTORY),
        "media_streams_stt": GOOGLE_STT_AVAILABLE or AZURE_STT_AVAILABLE,
        "stt_provider": STT_PROVIDER,
    }


# ---------------------------------------------------------------------------
# Admin: hot-reload knowledge base without container restart
# ---------------------------------------------------------------------------

@app.post("/kb-reload")
async def kb_reload(request: Request) -> dict:
    """Receive new KB content from the admin portal and rebuild the vector store."""
    secret = request.headers.get("X-KB-Secret", "")
    if not KB_RELOAD_SECRET or secret != KB_RELOAD_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")
    body = await request.json()
    content: str = body.get("content", "")
    filename: str = body.get("filename", "hutch_info.txt")
    if not content:
        return {"ok": False, "error": "Empty content"}
    loop = asyncio.get_event_loop()
    loop.run_in_executor(None, reload_kb_from_content, content, filename)
    logger.info("KB reload triggered for '%s' (%d chars)", filename, len(content))
    return {"ok": True, "message": f"KB reload started for {filename}"}


# ---------------------------------------------------------------------------
# Twilio incoming call webhook (inert -- no Twilio number provisioned)
# ---------------------------------------------------------------------------

@app.post("/voice/incoming")
async def voice_incoming(request: Request) -> Response:
    """Twilio webhook for incoming phone calls.

    English only -- connects straight to ConversationRelay, no DTMF menu.
    Kept for fleet consistency; inert until/unless a Twilio number is ever
    provisioned for Hutch.
    """
    host = request.headers.get("host", request.url.hostname or "localhost")
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
        "Incoming call from %s -- returning ConversationRelay TwiML",
        request.headers.get("x-forwarded-for", "unknown"),
    )
    return Response(content=twiml, media_type="application/xml")


def _build_conversation_relay_twiml(
    host: str, lang: str, config: dict[str, str]
) -> str:
    """Build the <ConversationRelay> XML tag for the given language config."""
    extra = config["extra_attrs"]
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
# Sentence extraction helper (Media Streams / SmartPBX streaming TTS)
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
    if role == "tool":
        return True
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
    if msg.get("tool_calls"):
        return True
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
    """
    if len(history) <= max_messages:
        return history

    trimmed = history[-max_messages:]

    while trimmed:
        if _is_tool_result_msg(trimmed[0]):
            trimmed.pop(0)
        elif _is_tool_call_msg(trimmed[0]):
            trimmed.pop(0)
        else:
            break

    return trimmed


# ---------------------------------------------------------------------------
# Google Cloud STT -- streaming (background thread, Media Streams / SmartPBX)
# ---------------------------------------------------------------------------

class GoogleSTTStream:
    """Streams mulaw 8 kHz audio to Google Cloud Speech-to-Text.

    Runs the synchronous gRPC streaming_recognize in a daemon thread.
    Fires on_final_result(transcript) from that thread.
    Auto-restarts on the ~5-minute gRPC streaming limit.
    """

    def __init__(self, on_final_result: Any, on_interim_result: Any = None, lang: str = "en"):
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
        primary = STT_PRIMARY.get(self._lang, "en-US")
        alternatives = STT_ALTERNATIVES.get(self._lang, [])
        config = google_speech.StreamingRecognitionConfig(
            config=google_speech.RecognitionConfig(
                encoding=google_speech.RecognitionConfig.AudioEncoding.MULAW,
                sample_rate_hertz=8000,
                language_code=primary,
                alternative_language_codes=alternatives,
                enable_automatic_punctuation=True,
                model="telephony",
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
# Azure STT -- alternative backend (background thread, Media Streams / SmartPBX)
# ---------------------------------------------------------------------------

class AzureSTTStream:
    """Streams audio to Azure Speech-to-Text -- drop-in alternative to GoogleSTTStream.

    Mirrors the same interface (start/stop/feed + on_final_result /
    on_interim_result callbacks fired from background threads) so it swaps in
    via the STT_PROVIDER env var. Twilio/Dialog deliver mulaw 8 kHz; Azure's
    PushAudioInputStream wants PCM, so each fed chunk is decoded mulaw -> PCM16
    (audioop) before being written.
    """

    def __init__(self, on_final_result: Any, on_interim_result: Any = None, lang: str = "en"):
        self._on_final = on_final_result
        self._on_interim = on_interim_result
        self._lang = lang
        self._chunk_count = 0
        self._running = False
        self._push_stream = None
        self._recognizer = None

    def start(self):
        if not AZURE_STT_AVAILABLE:
            logger.error("Cannot start Azure STT -- azure-cognitiveservices-speech not installed")
            return
        if audioop is None:
            logger.error("Cannot start Azure STT -- audioop unavailable")
            return
        if not AZURE_SPEECH_KEY:
            logger.error("Cannot start Azure STT -- AZURE_SPEECH_KEY not set")
            return

        primary = STT_PRIMARY.get(self._lang, "en-US")
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


def _make_stt(on_final_result: Any, on_interim_result: Any, lang: str = "en"):
    """Build the configured STT backend. STT_PROVIDER: 'google' (default) | 'azure'.

    Falls back to Google if Azure is selected but its SDK/audioop is missing.
    """
    if STT_PROVIDER == "azure":
        if AZURE_STT_AVAILABLE and audioop is not None:
            return AzureSTTStream(on_final_result, on_interim_result, lang)
        logger.error("STT_PROVIDER=azure but Azure STT unavailable -- falling back to Google")
    return GoogleSTTStream(on_final_result, on_interim_result, lang)


# ---------------------------------------------------------------------------
# Media Stream Session (raw audio -- used by Dialog SmartPBX; also reachable
# via Twilio Media Streams for fleet consistency, though inert in practice)
# ---------------------------------------------------------------------------

class MediaStreamSession:
    """Manages a single raw-audio call for Selina (English only).

    Pipeline per turn:
      STT -> endpointing -> KB retrieval -> LLM (streaming, tool-use loop)
      -> TTS -> mulaw audio -> transport

    Built for Dialog SmartPBX (see smartpbx_session.HutchSmartPBXSession, the
    adapter that binds one Dialog call to an instance of this class), but
    ``websocket`` also accepts a raw Twilio Media Streams WebSocket directly
    via ``/ws/media-stream/en`` for fleet consistency.
    """

    def __init__(
        self,
        websocket: WebSocket,
        lang: str = "en",
        anthropic_client: AsyncAnthropic | None = None,
        openai_client: AsyncOpenAI | None = None,
        gemini_client=None,
    ):
        self.ws = websocket
        self.anthropic_client = anthropic_client
        self.client = openai_client  # OpenAI client (kept for openai provider)
        self.gemini_client = gemini_client
        self.lang = "en"
        self.system_prompt = _build_system_prompt()
        self.tools = get_tools_for("claude") if LLM_PROVIDER == "claude" else []

        self.stream_sid: str | None = None
        self.call_sid: str = "unknown"
        self.history: list[dict] = []

        self._event_loop: asyncio.AbstractEventLoop | None = None
        self._is_speaking = False
        self._speaking_since: float | None = None
        self._speak_lock = asyncio.Lock()
        self._ws_lock = asyncio.Lock()
        self._speak_generation: int = 0

        self._pending_transcript = ""
        self._latest_interim = ""
        self._endpointing_handle: asyncio.TimerHandle | None = None
        self._stt: Any = None

    # -- Main event loop (Twilio Media Streams wire format) ----------------
    # SmartPBX calls do not use this loop; smartpbx_session drives the STT/
    # LLM/TTS methods below directly. This remains only for a raw Twilio
    # Media Streams WebSocket, kept for fleet consistency.

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
                    logger.info(
                        "Media stream started -- Call: %s, Stream: %s, lang: %s",
                        self.call_sid, self.stream_sid, self.lang,
                    )
                    self._set_handover_context()
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
                        self._speaking_since = None
                        logger.info("TTS done -- listening for caller speech [%s]", self.call_sid)

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

    def _set_handover_context(self):
        handover_context.set({
            "call_sid": self.call_sid,
            "human_agent_whatsapp": HUTCH_HUMAN_AGENT_WHATSAPP,
        })

    # -- STT callback (called from background thread) ----------------------

    def _on_stt_result(self, transcript: str):
        """Called from STT thread on FINAL results."""
        logger.info("STT final result [%s]: %r (speaking=%s)", self.call_sid, transcript, self._is_speaking)
        self._latest_interim = ""
        if self._event_loop is None:
            return
        if self._is_speaking:
            if self._should_barge_in(transcript):
                asyncio.run_coroutine_threadsafe(
                    self._handle_bargein(), self._event_loop,
                )
            return
        asyncio.run_coroutine_threadsafe(
            self._accumulate_transcript(transcript), self._event_loop,
        )

    def _on_stt_interim(self, transcript: str):
        """Called from STT thread on INTERIM results.

        Endpointing is interim-driven: each interim resets a silence timer;
        when the timer fires we use the latest interim as the utterance.
        """
        self._latest_interim = transcript
        if self._event_loop is None:
            return
        if self._is_speaking:
            if self._should_barge_in(transcript):
                asyncio.run_coroutine_threadsafe(
                    self._handle_bargein(), self._event_loop,
                )
            return
        asyncio.run_coroutine_threadsafe(
            self._set_transcript_interim(transcript), self._event_loop,
        )

    def _should_barge_in(self, transcript: str) -> bool:
        """Gate barge-in against Selina's own TTS echo.

        Dialog SmartPBX has no echo cancellation, so without this a short STT
        blip caused by Selina's own voice leaking into the mic gets misread as
        the caller interrupting -- cutting her off mid-sentence and dropping
        into a false barge-in instead of letting her finish.
        """
        text = (transcript or "").strip()
        if len(text) < BARGEIN_MIN_CHARS:
            return False
        if BARGEIN_DEBOUNCE_SECONDS > 0 and self._speaking_since:
            if (time.monotonic() - self._speaking_since) < BARGEIN_DEBOUNCE_SECONDS:
                return False
        return True

    async def _handle_bargein(self):
        logger.info("Barge-in detected [%s]", self.call_sid)
        self._is_speaking = False
        self._speaking_since = None
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
        logger.info("Caller [%s]: %s", self.call_sid, transcript)
        await self._process_utterance(transcript)

    # -- Utterance -> KB + LLM + TTS ---------------------------------------

    async def _process_utterance(self, text: str):
        try:
            kb_context = retrieve_context(text)
        except Exception:
            logger.exception("KB retrieval failed")
            kb_context = ""

        if kb_context and "No knowledge base loaded" not in kb_context:
            user_msg = f"[Reference context: {kb_context}]\n\nCaller: {text}"
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
                logger.info("Selina [%s]: %s", self.call_sid, response_text[:200])
        except Exception:
            logger.exception("LLM error [%s]", self.call_sid)
            await self._speak("I'm sorry, I encountered a technical issue. Please try again.")

    # -- OpenAI streaming + sentence-level TTS (no tool support) ------------

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
                sentences, sentence_buffer = _extract_sentences(sentence_buffer)
                for s in sentences:
                    tts_tasks.append(asyncio.create_task(self._speak(s, generation=gen)))

        remaining = sentence_buffer.strip()
        if remaining:
            tts_tasks.append(asyncio.create_task(self._speak(remaining, generation=gen)))
        if tts_tasks:
            await asyncio.gather(*tts_tasks)

        if text_content:
            self.history.append({"role": "assistant", "content": text_content})
        return text_content

    # -- Gemini native streaming + sentence-level TTS (no tool support) -----

    async def _run_llm_gemini(self) -> str:
        logger.info("Gemini call [%s]", self.call_sid)

        text_content = ""
        sentence_buffer = ""
        tts_tasks: list[asyncio.Task] = []
        gen = self._speak_generation

        gemini_contents = [
            {"role": "user" if m.get("role") == "user" else "model", "parts": [{"text": m.get("content", "")}]}
            for m in self.history
            if isinstance(m.get("content"), str)
        ]
        config = {
            "system_instruction": self.system_prompt,
            "max_output_tokens": MAX_TOKENS,
        }

        response = await self.gemini_client.aio.models.generate_content_stream(
            model=MODEL,
            contents=gemini_contents,
            config=config,
        )

        async for chunk in response:
            if not chunk.candidates:
                continue
            candidate = chunk.candidates[0]
            if not candidate.content or not candidate.content.parts:
                continue
            for part in candidate.content.parts:
                if part.text:
                    text_content += part.text
                    sentence_buffer += part.text
                    sentences, sentence_buffer = _extract_sentences(sentence_buffer)
                    for s in sentences:
                        tts_tasks.append(asyncio.create_task(self._speak(s, generation=gen)))

        remaining = sentence_buffer.strip()
        if remaining:
            tts_tasks.append(asyncio.create_task(self._speak(remaining, generation=gen)))
        if tts_tasks:
            await asyncio.gather(*tts_tasks)

        if text_content:
            self.history.append({"role": "assistant", "content": text_content})
        return text_content

    # -- Claude native streaming + sentence-level TTS + tool-use loop -------

    async def _run_llm_claude(self) -> str:
        """Anthropic Claude streaming for Media Streams/SmartPBX with sentence-level
        TTS and a bounded notify_human_handover tool-use loop."""
        logger.info("Claude call [%s]", self.call_sid)

        full_text = ""
        for round_idx in range(MAX_TOOL_ROUNDS):
            text_content = ""
            sentence_buffer = ""
            tts_tasks: list[asyncio.Task] = []
            gen = self._speak_generation
            tool_use_blocks: list[dict[str, Any]] = []
            cur_tool_name: str | None = None
            cur_tool_id: str | None = None
            tool_json = ""

            async with self.anthropic_client.messages.stream(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                system=self.system_prompt,
                messages=self.history,
                tools=self.tools if self.tools else NOT_GIVEN,
            ) as stream:
                async for event in stream:
                    if event.type == "content_block_start":
                        if event.content_block.type == "tool_use":
                            cur_tool_id = event.content_block.id
                            cur_tool_name = event.content_block.name
                            tool_json = ""
                    elif event.type == "content_block_delta":
                        if event.delta.type == "text_delta":
                            text_content += event.delta.text
                            sentence_buffer += event.delta.text
                            sentences, sentence_buffer = _extract_sentences(sentence_buffer)
                            for s in sentences:
                                tts_tasks.append(asyncio.create_task(self._speak(s, generation=gen)))
                        elif event.delta.type == "input_json_delta":
                            tool_json += event.delta.partial_json
                    elif event.type == "content_block_stop":
                        if cur_tool_name:
                            try:
                                parsed = json.loads(tool_json) if tool_json else {}
                            except json.JSONDecodeError:
                                logger.error("Bad tool JSON for %s: %s", cur_tool_name, tool_json[:200])
                                parsed = {}
                            tool_use_blocks.append({"id": cur_tool_id, "name": cur_tool_name, "input": parsed})
                            cur_tool_name = None
                            cur_tool_id = None
                            tool_json = ""

            remaining = sentence_buffer.strip()
            if remaining:
                tts_tasks.append(asyncio.create_task(self._speak(remaining, generation=gen)))
            if tts_tasks:
                await asyncio.gather(*tts_tasks)

            full_text = f"{full_text} {text_content}".strip() if text_content else full_text

            if not tool_use_blocks:
                if text_content:
                    self.history.append({"role": "assistant", "content": text_content})
                break

            logger.info(
                "Claude requested %d tool(s) [%s]: %s",
                len(tool_use_blocks), self.call_sid, [t["name"] for t in tool_use_blocks],
            )
            assistant_content: list[dict[str, Any]] = []
            if text_content:
                assistant_content.append({"type": "text", "text": text_content})
            for tb in tool_use_blocks:
                assistant_content.append({
                    "type": "tool_use", "id": tb["id"], "name": tb["name"], "input": tb["input"],
                })
            self.history.append({"role": "assistant", "content": assistant_content})

            tool_results: list[dict[str, Any]] = []
            for tb in tool_use_blocks:
                result = await execute_tool(tb["name"], tb["input"])
                tool_results.append({
                    "type": "tool_result", "tool_use_id": tb["id"], "content": result,
                })
            self.history.append({"role": "user", "content": tool_results})
            # Loop back so the model can speak its confirmation to the caller.

        return full_text

    # -- TTS -> mulaw audio (ElevenLabs, English) ---------------------------

    async def _speak(self, text: str, generation: int = -1):
        async with self._speak_lock:
            if generation >= 0 and generation != self._speak_generation:
                return
            await self._tts_elevenlabs(text)

    async def _tts_elevenlabs(self, text: str):
        """Stream text via ElevenLabs (turbo) and send mulaw audio to the transport.
        Must only be called from _speak (lock already held).
        """
        if not ELEVENLABS_API_KEY or not ELEVENLABS_VOICE_ID:
            logger.warning("ElevenLabs not configured -- skipping TTS")
            return

        self._is_speaking = True
        self._speaking_since = time.monotonic()
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
            "model_id": ELEVENLABS_MODEL_TURBO,
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
                        logger.error("ElevenLabs %d: %s", resp.status_code, body[:200])
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
                # Twilio's Media Streams path relies on the "mark" event being
                # echoed back through the inbound WebSocket loop (run(), which
                # sets _is_speaking = False on receipt) to know playback has
                # actually finished. Dialog SmartPBX has no such echo -- the
                # transport adapter swallows the mark locally and never calls
                # back in -- so without this, _is_speaking would stay stuck
                # True for the rest of the call on that (the only live) path,
                # making later barge-in detection unreliable in both
                # directions. Clearing it here is a no-op for the inert Twilio
                # path (already cleared again when its echo arrives).
                self._is_speaking = False
                self._speaking_since = None
            else:
                logger.info("ElevenLabs TTS interrupted by barge-in [%s]", self.call_sid)

        except httpx.TimeoutException:
            logger.error("ElevenLabs timeout for: %s", text[:80])
            self._is_speaking = False
        except Exception:
            logger.exception("ElevenLabs TTS failed for: %s", text[:80])
            self._is_speaking = False


# ---------------------------------------------------------------------------
# Streaming LLM calls (OpenAI) -- ConversationRelay (no tool support)
# ---------------------------------------------------------------------------

async def _run_llm_streaming(
    client: AsyncOpenAI,
    system: str,
    conversation_history: list[dict],
    tools: list[dict],
    websocket: WebSocket,
) -> str:
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
            await websocket.send_text(json.dumps({"type": "text", "token": delta.content}))

    full_response_text += text_content
    await websocket.send_text(json.dumps({"type": "text", "token": "", "last": True}))

    if text_content:
        conversation_history.append({"role": "assistant", "content": text_content})

    logger.info("LLM response complete (%d chars)", len(full_response_text))
    return full_response_text


# ---------------------------------------------------------------------------
# Streaming LLM calls (Gemini native) -- ConversationRelay (no tool support)
# ---------------------------------------------------------------------------

async def _run_llm_streaming_gemini(
    gemini_client,
    system: str,
    conversation_history: list[dict],
    tools: list[dict],
    websocket: WebSocket,
) -> str:
    full_response_text = ""
    text_content = ""

    gemini_contents = [
        {"role": "user" if m.get("role") == "user" else "model", "parts": [{"text": m.get("content", "")}]}
        for m in conversation_history
        if isinstance(m.get("content"), str)
    ]
    config = {
        "system_instruction": system,
        "max_output_tokens": MAX_TOKENS,
    }

    response = await gemini_client.aio.models.generate_content_stream(
        model=MODEL,
        contents=gemini_contents,
        config=config,
    )

    async for chunk in response:
        if not chunk.candidates:
            continue
        candidate = chunk.candidates[0]
        if not candidate.content or not candidate.content.parts:
            continue
        for part in candidate.content.parts:
            if part.text:
                text_content += part.text
                await websocket.send_text(json.dumps({"type": "text", "token": part.text}))

    full_response_text += text_content
    await websocket.send_text(json.dumps({"type": "text", "token": "", "last": True}))

    if text_content:
        conversation_history.append({"role": "assistant", "content": text_content})

    logger.info("Gemini response complete (%d chars)", len(full_response_text))
    return full_response_text


# ---------------------------------------------------------------------------
# Streaming LLM calls (Anthropic Claude) -- ConversationRelay, with the
# notify_human_handover tool-use loop
# ---------------------------------------------------------------------------

async def _run_llm_streaming_claude(
    client: AsyncAnthropic,
    system: str,
    conversation_history: list[dict],
    tools: list[dict],
    websocket: WebSocket,
) -> str:
    full_response_text = ""

    for round_idx in range(MAX_TOOL_ROUNDS):
        text_content = ""
        tool_use_blocks: list[dict[str, Any]] = []
        cur_tool_name: str | None = None
        cur_tool_id: str | None = None
        tool_json = ""

        async with client.messages.stream(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=system,
            messages=conversation_history,
            tools=tools if tools else NOT_GIVEN,
        ) as stream:
            async for event in stream:
                if event.type == "content_block_start":
                    if event.content_block.type == "tool_use":
                        cur_tool_id = event.content_block.id
                        cur_tool_name = event.content_block.name
                        tool_json = ""
                elif event.type == "content_block_delta":
                    if event.delta.type == "text_delta":
                        text_content += event.delta.text
                        await websocket.send_text(
                            json.dumps({"type": "text", "token": event.delta.text})
                        )
                    elif event.delta.type == "input_json_delta":
                        tool_json += event.delta.partial_json
                elif event.type == "content_block_stop":
                    if cur_tool_name:
                        try:
                            parsed = json.loads(tool_json) if tool_json else {}
                        except json.JSONDecodeError:
                            logger.error("Bad tool JSON for %s: %s", cur_tool_name, tool_json[:200])
                            parsed = {}
                        tool_use_blocks.append({"id": cur_tool_id, "name": cur_tool_name, "input": parsed})
                        cur_tool_name = None
                        cur_tool_id = None
                        tool_json = ""

        full_response_text = f"{full_response_text} {text_content}".strip() if text_content else full_response_text

        if not tool_use_blocks:
            if text_content:
                conversation_history.append({"role": "assistant", "content": text_content})
            break

        logger.info(
            "Claude requested %d tool(s): %s",
            len(tool_use_blocks), [t["name"] for t in tool_use_blocks],
        )
        assistant_content: list[dict[str, Any]] = []
        if text_content:
            assistant_content.append({"type": "text", "text": text_content})
        for tb in tool_use_blocks:
            assistant_content.append({
                "type": "tool_use", "id": tb["id"], "name": tb["name"], "input": tb["input"],
            })
        conversation_history.append({"role": "assistant", "content": assistant_content})

        tool_results: list[dict[str, Any]] = []
        for tb in tool_use_blocks:
            result = await execute_tool(tb["name"], tb["input"])
            tool_results.append({"type": "tool_result", "tool_use_id": tb["id"], "content": result})
        conversation_history.append({"role": "user", "content": tool_results})
        # Loop back so the model can speak its confirmation to the caller.

    await websocket.send_text(json.dumps({"type": "text", "token": "", "last": True}))
    logger.info("Claude response complete (%d chars)", len(full_response_text))
    return full_response_text


# ---------------------------------------------------------------------------
# WebSocket -- ConversationRelay handler (inert -- no Twilio number configured)
# ---------------------------------------------------------------------------

@app.websocket("/ws/conversation")
async def ws_conversation(websocket: WebSocket, lang: str = "en"):
    """Handle a Twilio ConversationRelay WebSocket session.

    Message types from Twilio:
      - "setup"   : Session initialization (call metadata).
      - "prompt"  : Transcribed user speech ready for processing.
      - "dtmf"    : DTMF tone detected (logged, not acted on).
      - "interrupt": User interrupted the agent mid-speech.
      - Others    : Logged and ignored.
    """
    await websocket.accept()
    logger.info("WebSocket connection accepted -- language: en")

    conversation_history: list[dict] = []
    system_prompt: str = _build_system_prompt()
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

    tools = get_tools_for("claude") if LLM_PROVIDER == "claude" else []

    try:
        while True:
            raw = await websocket.receive_text()

            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                logger.warning("Received non-JSON message on WebSocket: %s", raw[:200])
                continue

            msg_type = message.get("type", "")

            if msg_type == "setup":
                call_sid = message.get("callSid", "unknown")
                handover_context.set({"call_sid": call_sid, "human_agent_whatsapp": HUTCH_HUMAN_AGENT_WHATSAPP})
                logger.info(
                    "Session setup -- CallSid: %s, StreamSid: %s",
                    call_sid, message.get("streamSid", "n/a"),
                )

            elif msg_type == "prompt":
                user_text = message.get("voicePrompt", "").strip()
                if not user_text:
                    logger.debug("Empty voicePrompt received -- ignoring")
                    continue

                logger.info("Caller [%s]: %s", call_sid, user_text)

                try:
                    kb_context = retrieve_context(user_text)
                except Exception:
                    logger.exception("KB retrieval failed")
                    kb_context = ""

                if kb_context and kb_context != "No knowledge base loaded. Answering from general knowledge.":
                    user_message = f"[Reference context: {kb_context}]\n\nCaller: {user_text}"
                else:
                    user_message = user_text

                conversation_history.append({"role": "user", "content": user_message})
                conversation_history = _trim_history(conversation_history)

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
                            tools=[],
                            websocket=websocket,
                        )
                    else:
                        response_text = await _run_llm_streaming(
                            client=openai_client,
                            system=system_prompt,
                            conversation_history=conversation_history,
                            tools=[],
                            websocket=websocket,
                        )
                    logger.info("Selina [%s]: %s", call_sid, response_text[:200])
                except WebSocketDisconnect:
                    logger.info("WebSocket disconnected during LLM streaming [%s]", call_sid)
                    raise
                except Exception:
                    logger.exception("Error during LLM streaming [%s]", call_sid)
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

            elif msg_type == "dtmf":
                digit = message.get("digit", "?")
                logger.info("DTMF received [%s]: %s", call_sid, digit)

            elif msg_type == "interrupt":
                logger.info(
                    "Speech interrupted by caller [%s] -- utteranceUntilInterrupt: '%s'",
                    call_sid, message.get("utteranceUntilInterrupt", ""),
                )

            else:
                logger.debug("Unhandled message type '%s' [%s]: %s", msg_type, call_sid, raw[:200])

    except WebSocketDisconnect:
        logger.info("WebSocket disconnected -- CallSid: %s", call_sid)
    except Exception:
        logger.exception("Unexpected error in WebSocket handler [%s]", call_sid)
    finally:
        logger.info(
            "Session ended -- CallSid: %s, history length: %d messages",
            call_sid, len(conversation_history),
        )


# ---------------------------------------------------------------------------
# WebSocket -- raw Twilio Media Streams handler (English only; fleet
# consistency only -- inert without a Twilio number)
# ---------------------------------------------------------------------------

@app.websocket("/ws/media-stream/{lang}")
async def ws_media_stream(websocket: WebSocket, lang: str):
    """Handle a raw Media Streams WebSocket session for English.

    Language is encoded in the URL path for parity with the rest of the
    fleet, but Hutch is English-only: any value is coerced to "en".
    """
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
        websocket=websocket, lang="en",
        anthropic_client=anthropic_client,
        openai_client=openai_client,
        gemini_client=gemini_client,
    )
    await session.run()


# ---------------------------------------------------------------------------
# Service-mode boundary -- select Twilio (this app) or Dialog SmartPBX
# ---------------------------------------------------------------------------
_twilio_app = app


async def _new_smartpbx_session(context, transport):
    from smartpbx_session import HutchSmartPBXSession

    return HutchSmartPBXSession(context, transport)


def build_service_app(service_mode: str, environ: Any) -> FastAPI:
    """Select one ingress surface; never activate Twilio and Dialog together.

    Hutch has no Twilio number, so the default mode is "smartpbx" -- unlike
    Kavya/Flico, whose default is "twilio". The Twilio app is still built and
    returned when explicitly selected, for fleet consistency and to avoid
    surgical deletion of the ConversationRelay/Media Streams code paths.
    """
    mode = service_mode.strip().lower() if isinstance(service_mode, str) else ""
    if mode == "twilio":
        return _twilio_app
    if mode != "smartpbx":
        raise ValueError("invalid HUTCH_SERVICE_MODE")

    from smartpbx_gateway import SmartPBXGateway, SmartPBXSessionRegistry, SmartPBXSettings

    settings = SmartPBXSettings.from_env(environ)
    registry = SmartPBXSessionRegistry(settings.max_calls)
    gateway = SmartPBXGateway(settings, registry)
    smartpbx_app = FastAPI(
        title="Hutch Voice Agent (Selina) -- SmartPBX",
        version="1.0.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    def smartpbx_health() -> dict[str, str]:
        return {"status": "ok", "service_mode": "smartpbx"}

    def status(request: Request) -> dict[str, bool | int | str]:
        # active_sessions against the cap is a live occupancy oracle and
        # admitted_total is a call-volume counter, so this needs the same
        # shared token as the media socket. Constant-time compare via
        # token_matches. /health stays open for liveness.
        if not settings.token_matches(
            request.headers.get("X-Hutch-SmartPBX-Token", "")
        ):
            raise HTTPException(status_code=401)
        return gateway.snapshot()

    async def smartpbx_media(websocket: WebSocket) -> None:
        await gateway.handle(websocket, _new_smartpbx_session)

    smartpbx_app.add_api_route("/health", smartpbx_health, methods=["GET"])
    smartpbx_app.add_api_route("/smartpbx/status", status, methods=["GET"])
    smartpbx_app.add_api_websocket_route("/ws/v1/smartpbx/media", smartpbx_media)
    smartpbx_app.state.smartpbx_gateway = gateway
    return smartpbx_app


app = build_service_app(HUTCH_SERVICE_MODE, os.environ)


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
