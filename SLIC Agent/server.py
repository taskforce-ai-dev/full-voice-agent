"""
server.py â€” Main FastAPI server for SLIC Accident-Claim Voice Agent (Nimali).

Handles:
  - Incoming call webhook (POST /voice/incoming) â€” generic greeting by default;
    personalized only when there is a live active-session continuation.
  - ConversationRelay WebSocket (/ws/conversation) â€” English only, ElevenLabs TTS.
  - Streaming LLM responses (Claude / OpenAI / Gemini) with tool-use support.
  - Knowledge-base context injection.
  - Post-call data capture on WebSocket disconnect.
  - Health endpoint (GET /health).

Architecture:
  Incoming call
    â†’ POST /voice/incoming  (look up phone in mock_db + active_session)
    â†’ TwiML <Connect><ConversationRelay /></Connect> with personalized welcome
    â†’ WebSocket /ws/conversation (English)
    â†’ LLM streaming with tool use (verify_vehicle_policy, collect_accident_details,
      dispatch_nearest_assessor, send_confirmation_sms, request_live_agent_handoff)
    â†’ text tokens â†’ Twilio TTS â†’ caller
"""

from __future__ import annotations

import asyncio
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
    sentry_sdk.set_tag("agent", "slic")
import xml.sax.saxutils
from contextlib import asynccontextmanager
from datetime import date, datetime
from typing import Any

from anthropic import AsyncAnthropic, NOT_GIVEN
from openai import AsyncOpenAI
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import Response

from knowledge_base import retrieve_context, initialize_kb, prewarm, reload_kb_from_content
from tools import get_tools, get_tools_openai, get_tools_gemini, execute_tool
from claim_api import is_configured
from post_call import process_post_call_data

import active_session

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
TWILIO_ACCOUNT_SID: str = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN: str = os.getenv("TWILIO_AUTH_TOKEN", "")
KB_DOCS_DIRECTORY: str = os.getenv("KB_DOCS_DIRECTORY", "knowledge_docs")
KB_RELOAD_SECRET: str = os.getenv("KB_RELOAD_SECRET", "")
PORT: int = int(os.getenv("PORT", "8000"))

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
    from google.genai import types as genai_types  # noqa: F401
    GOOGLE_GENAI_AVAILABLE = True
except ImportError:
    google_genai = None  # type: ignore[assignment]
    genai_types = None  # type: ignore[assignment]
    GOOGLE_GENAI_AVAILABLE = False
    logger.warning("google-genai not installed â€” native Gemini provider unavailable")

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
MAX_TOOL_ROUNDS: int = 5

# ---------------------------------------------------------------------------
# Per-call bridge dicts â€” populated by HTTP handler, consumed by WebSocket
# ---------------------------------------------------------------------------
_call_phone: dict[str, str] = {}  # CallSid -> caller phone number
_call_session: dict[str, object | None] = {}  # CallSid -> SessionRecord (continuation)

# ---------------------------------------------------------------------------
# Filler messages sent while tools execute
# ---------------------------------------------------------------------------
TOOL_FILLERS: dict[str, str] = {
    "verify_vehicle_policy": "Let me check that vehicle against our policy database.",
    "collect_accident_details": "Got it, noting that down.",
    "dispatch_nearest_assessor": "Dispatching the nearest assessor to your location now.",
    "send_confirmation_sms": "Sending you an SMS confirmation.",
    "request_live_agent_handoff": "Let me transfer you to a live agent.",
}
DEFAULT_FILLER: str = "Let me check that for you."

# ---------------------------------------------------------------------------
# English ConversationRelay configuration
# ---------------------------------------------------------------------------
EN_CONFIG: dict[str, str] = {
    "tts_provider": "ElevenLabs",
    "voice": os.getenv(
        "ELEVENLABS_CR_VOICE",
        "ZF6FPAbjXT4488VcRRnw-flash_v2_5",
    ),
    "language": "en-US",
    "extra_attrs": '        elevenlabsTextNormalization="on"\n',
}

DEFAULT_WELCOME: str = (
    "Thank you for calling Sri Lanka Insurance Corporation. "
    "I'm your AI assistant, here to help you report your accident quickly "
    "so we can get an assessor to you as soon as possible. "
    "To get started, may I have your vehicle registration number, please?"
)


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

def _build_system_prompt(caller_context: dict) -> str:
    """Build the system prompt for the LLM.

    The new flow is vehicle-first: identity comes from the registration
    number the caller speaks, not from the incoming phone. `caller_context`
    may still carry an active-session continuation for returning callers.
    """
    today = date.today().isoformat()
    active = caller_context.get("active_session")

    if active:
        vehicle = active.get("vehicle") or {}
        make = vehicle.get("make", "unknown")
        model_ = vehicle.get("model", "unknown")
        reg_no = vehicle.get("reg_no", "unknown")
        active_section = (
            "\nACTIVE SESSION (RETURNING CALLER):\n"
            f"- Prior vehicle: {make} {model_} ({reg_no})\n"
            f"- Claim reference: {active.get('claim_reference')}\n"
            f"- Assessor ETA now: ~{active.get('eta_minutes')} minutes\n"
            "- Treat this as a follow-up, not a new intake. Do NOT re-ask "
            "for vehicle or accident details â€” answer their follow-up directly.\n"
        )
    else:
        active_section = ""

    return (
        "You are Nimali, a 24/7 accident-hotline voice agent for Sri Lanka "
        "Insurance Corporation (SLIC). You are a Sri Lankan woman speaking "
        "Sri Lankan / South Asian English â€” natural, warm, not robotic.\n"
        f"Today's date is {today}.\n"
        + active_section +
        "\n"
        "EMOTIONAL ADAPTATION (this is critical â€” match the caller, don't "
        "stay monotone):\n"
        "- DEFAULT tone: warm, professional, gently reassuring.\n"
        "- If caller sounds PANICKED, SHAKEN, or CRYING: slow down, soften "
        "your voice, use short reassuring lines ('Take a deep breath. You're "
        "safe now. I'm right here with you.'). Lead with empathy before "
        "asking the next question.\n"
        "- If caller reports INJURIES or someone HURT: shift to urgent but "
        "steady â€” 'Okay, listen carefully â€” please call one-nine-zero right "
        "now for emergency services. I'll stay on the line and handle your "
        "claim.'\n"
        "- If caller sounds ANGRY or FRUSTRATED: acknowledge it directly "
        "('I understand this is frustrating, I'll make this quick'), then "
        "move faster through the flow.\n"
        "- If caller is CALM and matter-of-fact: match their pace, keep it "
        "efficient, minimal small talk.\n"
        "- If caller is HAPPY or RELIEVED (e.g. 'only a scratch'): lighten "
        "your tone a touch, friendly not solemn.\n"
        "- Occasional natural fillers are fine ('right', 'okay', 'alright') "
        "but NEVER use slang like 'machan' or mid-English Sinhala code-mix.\n\n"
        "VOICE RULES (phone call, not text):\n"
        "- Speak at a CALM, MEASURED pace. You are a professional hotline "
        "operator â€” steady and clear, not rushed. Natural pauses between "
        "sentences are fine. Do not race through the words.\n"
        "- Use contractions (I've, you're, we'll) so it sounds natural, "
        "not robotic.\n"
        "- One or two short sentences per reply â€” never a monologue.\n"
        "- No markdown, bullet points, URLs, or abbreviations â€” spoken aloud.\n"
        "- Read registration numbers digit-by-digit "
        "(e.g. 'C-B-A one one seven five').\n"
        "- EXPAND 'double' and 'triple' shorthand when interpreting what the "
        "caller says â€” this applies to BOTH digits AND letters. "
        "'double five' = 55, 'triple five' = 555, 'double A' = AA, "
        "'triple B' = BBB, 'double zero' = 00. So 'C-C-A double three four "
        "four' means 'CCA-3344' and 'C-double A-three-four-three-four' means "
        "'CAA-3434'. Apply this expansion silently before read-back; "
        "read back the expanded form digit-by-digit (e.g. 'C-C-A three "
        "three four four').\n"
        "- NEVER read the claim reference aloud. The caller will get it by "
        "SMS. Just say 'I've sent the claim reference to your phone by SMS.'\n"
        "- Never ask for the caller's phone number â€” it is already known.\n"
        "- Never ask for a policy number â€” look it up from the vehicle.\n\n"

        "REQUIRED FLOW (do not skip steps, do not re-order):\n"
        "Step 1 â€” The welcome greeting has already introduced you AND asked "
        "the caller for their vehicle registration number. So the caller's "
        "first utterance is almost always a reply to that question.\n"
        "  â€¢ If the caller gave a registration number, skip straight to the "
        "read-back in Step 2.\n"
        "  â€¢ If the caller said something else first (said hello, described "
        "the accident, sounded shaken), give ONE short empathetic "
        "acknowledgement (e.g. 'I'm sorry to hear that â€” I'll help you right "
        "away.') and then politely ask: 'May I have your vehicle "
        "registration number, please?' Never say 'give me' or 'I need' â€” "
        "keep it warm and polite.\n"
        "  â€¢ Do NOT re-introduce yourself and do NOT do a 'how can I help' "
        "check â€” the welcome already covered that.\n"
        "If at any point the caller mentions serious injury or an emergency, "
        "tell them to hang up and call 1-9-0 for emergency services "
        "immediately, then offer request_live_agent_handoff.\n"
        "Step 2 â€” Read-back confirmation. Read the reg number back "
        "digit-by-digit and ask 'Is that correct?' BEFORE calling any tool.\n"
        "Step 3 â€” Call verify_vehicle_policy with the confirmed reg number. "
        "If it returns verified=false, apologize and call "
        "request_live_agent_handoff with reason='vehicle_not_on_policy'. "
        "Do NOT proceed.\n"
        "Step 3b â€” On verified=true, announce the vehicle make and model "
        "from the tool response so the caller knows the system found the "
        "right car. Example: 'Great â€” I have a BMW X5 registered under "
        "this policy.' DO NOT reveal the customer_name here.\n"
        "Step 4 â€” IDENTITY CHECK. verify_vehicle_policy returns the "
        "policyholder's customer_name. DO NOT reveal that name. Ask the "
        "caller: 'For verification, may I have your full name please?' "
        "Silently compare what they say to customer_name, LENIENTLY â€” "
        "telephony STT is noisy so you MUST tolerate mis-hearings:\n"
        "    â€¢ MATCH if ANY of these hold: (a) first name is the same or a "
        "      close phonetic variant (e.g. Chrys/Chris, Jayne/Jane, "
        "      Nuwan/Newan, Dilshan/Dilsan); OR (b) last name is the same "
        "      or a close phonetic variant; OR (c) the full name sounds "
        "      similar overall. Honorifics (Mr, Mrs, Miss) are ignored. "
        "      When in doubt between match and mismatch, LEAN TOWARD MATCH "
        "      â€” it's better to let a real customer through than to block "
        "      them for an STT slip. On match: say 'Thank you, verified' "
        "      and continue.\n"
        "    â€¢ MISMATCH only if the name is CLEARLY a different person "
        "      (no first-name overlap AND no last-name overlap, e.g. "
        "      policy says 'Chrys Fernando' and caller says 'Rohan Bandara' "
        "      â€” that's a mismatch). Give ONE polite retry 'Sorry, I "
        "      didn't quite catch that â€” could you please repeat your full "
        "      name?' before declaring mismatch. On confirmed mismatch, "
        "      call request_live_agent_handoff with reason='name_mismatch'.\n"
        "Step 5 â€” ACCIDENT LOCATION. Only after identity is verified, say: "
        "'Thank you. Now, where did the accident take place? Please describe "
        "the location as clearly as you can â€” the road, town, or any nearby "
        "landmark.' If the caller's answer is vague (just a city name, or "
        "'somewhere on the highway'), ask ONE follow-up: 'Could you give me "
        "a nearby landmark, junction, or town name so our assessor can find "
        "you quickly?' Do NOT ask about injuries or vehicle condition â€” the "
        "field assessor will inspect both in person. Then call "
        "collect_accident_details with the location only (pass empty strings "
        "for injuries and vehicle_condition).\n"
        "Step 6 â€” Call dispatch_nearest_assessor with the reg_no.\n"
        "Step 7 â€” Tell the caller the assessor's name and ETA only. "
        "Do NOT say the claim reference aloud. Then call send_confirmation_sms "
        "with the claim_reference, eta_minutes, assessor_name, AND "
        "assessor_phone â€” all returned by dispatch_nearest_assessor â€” the SMS "
        "carries the claim reference and the assessor's contact number to "
        "the caller.\n"
        "Step 8 â€” Close: 'Help is on the way. I've sent your claim reference "
        "to your phone by SMS. Please stay safe.'\n\n"

        "HANDOFF TRIGGERS (call request_live_agent_handoff immediately):\n"
        "- verify_vehicle_policy returned verified=false.\n"
        "- Identity check failed (caller's name doesn't match customer_name).\n"
        "- Caller explicitly asks to speak to a human.\n"
        "- You cannot progress after two clarification attempts on the same "
        "question.\n\n"

        "KEEP THE CALL UNDER THREE MINUTES. Don't repeat information the "
        "caller already gave you. Skip small talk.\n"
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
    logger.info("Starting SLIC Accident-Claim Voice Agent server...")

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

    logger.info("Server startup complete. claim_api configured: %s", is_configured())

    yield

    # --- Shutdown ---
    logger.info("Shutting down server...")
    logger.info("Shutdown complete.")


# ---------------------------------------------------------------------------
# FastAPI application
# ---------------------------------------------------------------------------
app = FastAPI(
    title="SLIC Accident-Claim Voice Agent (Nimali)",
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
        "claim_api_configured": is_configured(),
        "kb_loaded": os.path.isdir(KB_DOCS_DIRECTORY),
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

    The new flow identifies the caller by vehicle registration number
    (collected by the LLM), not by phone. We still capture the phone for
    SMS delivery, and we still honour a live active-session continuation
    so a follow-up call within TTL picks up where it left off.
    """
    form = await request.form()
    host = request.headers.get("host", request.url.hostname or "localhost")

    call_sid = str(form.get("CallSid", ""))
    caller_phone = str(form.get("From", ""))

    # Store phone for the WebSocket handler (needed for SMS)
    if call_sid and caller_phone:
        _call_phone[call_sid] = caller_phone

    # Only check for a live claim-session continuation; no phone-based
    # customer identification â€” the LLM verifies by vehicle.
    session = active_session.get(caller_phone) if caller_phone else None
    if call_sid:
        _call_session[call_sid] = session

    greeting = _build_greeting(session=session)

    cr_tag = _build_conversation_relay_twiml(host, greeting)
    twiml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<Response>\n"
        "  <Connect>\n"
        f"    {cr_tag}\n"
        "  </Connect>\n"
        "</Response>"
    )

    logger.info(
        "Incoming call â€” CallSid: %s, From: %s, continuation=%s",
        call_sid, caller_phone, "yes" if session else "no",
    )

    return Response(content=twiml, media_type="application/xml")


def _build_greeting(session) -> str:
    """Build the welcome greeting. Generic by default; personalized only when
    we have a live active session (returning caller inside TTL)."""
    if session is not None:
        vehicle = getattr(session, "vehicle", {}) or {}
        make = vehicle.get("make", "vehicle")
        model_ = vehicle.get("model", "")
        eta = active_session.remaining_eta_minutes(session)
        return (
            f"Hi {session.name}, good to hear from you again. I see you "
            f"called earlier about your {make} {model_}. Your assessor is "
            f"about {eta} minutes away. Is there anything else I can help with?"
        )
    return DEFAULT_WELCOME


def _build_conversation_relay_twiml(host: str, welcome_greeting: str) -> str:
    """Build the <ConversationRelay> XML tag with a personalized welcome."""
    extra = EN_CONFIG["extra_attrs"]
    greeting = xml.sax.saxutils.escape(welcome_greeting)

    return (
        f'<ConversationRelay url="wss://{host}/ws/conversation"\n'
        f'        ttsProvider="{EN_CONFIG["tts_provider"]}"\n'
        f'        voice="{EN_CONFIG["voice"]}"\n'
        f'{extra}'
        f'        language="{EN_CONFIG["language"]}"\n'
        f'        transcriptionProvider="google"\n'
        f'        speechModel="telephony"\n'
        f'        welcomeGreeting="{greeting}"\n'
        '        interruptible="true"\n'
        '        dtmfDetection="true">\n'
        "    </ConversationRelay>"
    )


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


def _handoff_just_executed(history: list[dict]) -> bool:
    """Return True if the most recent tool call in history was a successful
    request_live_agent_handoff. Handles both Anthropic and OpenAI formats.
    """
    if not history:
        return False

    # Walk backwards to find the most recent tool result and its matching call.
    # Anthropic: user message with a tool_result content block.
    # OpenAI:   tool role message.
    tool_name: str | None = None
    tool_output: str | None = None
    tool_use_id: str | None = None

    for msg in reversed(history):
        role = msg.get("role")
        content = msg.get("content")

        if tool_output is None:
            # Looking for the tool result
            if role == "tool":
                tool_call_id = msg.get("tool_call_id")
                tool_output = content if isinstance(content, str) else None
                tool_use_id = tool_call_id
                continue
            if role == "user" and isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        tool_output = block.get("content")
                        tool_use_id = block.get("tool_use_id")
                        break
                if tool_output is not None:
                    continue
            # Not a tool result â€” keep scanning back
            continue

        # Now find the matching assistant call to identify the tool name
        if role == "assistant":
            if isinstance(content, list):
                for block in content:
                    if (isinstance(block, dict) and block.get("type") == "tool_use"
                            and block.get("id") == tool_use_id):
                        tool_name = block.get("name")
                        break
            if tool_name is None:
                for tc in msg.get("tool_calls") or []:
                    if tc.get("id") == tool_use_id:
                        tool_name = tc.get("function", {}).get("name")
                        break
            break

    if tool_name != "request_live_agent_handoff" or not tool_output:
        return False

    try:
        parsed = json.loads(tool_output) if isinstance(tool_output, str) else tool_output
    except (json.JSONDecodeError, TypeError):
        return False
    return bool(parsed.get("handoff"))


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
# Streaming LLM calls with tool use (OpenAI)
# ---------------------------------------------------------------------------

async def _run_llm_streaming(
    client: AsyncOpenAI,
    system: str,
    conversation_history: list[dict],
    tools: list[dict],
    websocket: WebSocket,
    caller_phone: str,
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
                    result_str = await execute_tool(
                        tc["name"], parsed_input, caller_phone=caller_phone,
                    )
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
    caller_phone: str,
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
                    result_str = await execute_tool(
                        tc["function"]["name"], parsed_input, caller_phone=caller_phone,
                    )
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

async def _run_llm_streaming_claude(
    client: AsyncAnthropic,
    system: str,
    conversation_history: list[dict],
    tools: list[dict],
    websocket: WebSocket,
    caller_phone: str,
) -> str:
    """Stream a Claude response via the Anthropic SDK, handling tool use.

    Uses Anthropic's native message format with content blocks.
    Sends text tokens to the WebSocket as they arrive for ConversationRelay.
    """
    full_response_text = ""

    for round_idx in range(MAX_TOOL_ROUNDS):
        logger.info("Claude streaming round %d", round_idx + 1)

        text_content = ""
        tool_use_blocks: list[dict[str, Any]] = []
        cur_tool_name: str | None = None
        cur_tool_id: str | None = None
        tool_json = ""

        async with client.messages.stream(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            # cache_control caches the tools+system prefix for ~5 min,
            # cutting input tokens on the 2nd+ turn of every call.
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
                        tool_use_blocks.append({
                            "id": cur_tool_id,
                            "name": cur_tool_name,
                            "input": parsed,
                        })
                        cur_tool_name = None
                        cur_tool_id = None
                        tool_json = ""

        full_response_text += text_content

        # -- Handle tool calls --
        if tool_use_blocks:
            logger.info(
                "Claude requested %d tool(s): %s",
                len(tool_use_blocks),
                [t["name"] for t in tool_use_blocks],
            )

            first_tool_name = tool_use_blocks[0]["name"]
            filler = TOOL_FILLERS.get(first_tool_name, DEFAULT_FILLER)
            await websocket.send_text(
                json.dumps({"type": "text", "token": filler, "last": True})
            )
            logger.info("Sent filler: '%s'", filler)

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
                    result_str = await execute_tool(
                        tb["name"], tb["input"], caller_phone=caller_phone,
                    )
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

    # Exhausted all tool rounds
    logger.warning("Exhausted %d tool rounds (Claude)", MAX_TOOL_ROUNDS)
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
# WebSocket â€” ConversationRelay handler
# ---------------------------------------------------------------------------

@app.websocket("/ws/conversation")
async def ws_conversation(websocket: WebSocket):
    """Handle a Twilio ConversationRelay WebSocket session (English only).

    Message types from Twilio:
      - "setup"   : Session initialization (call metadata).
      - "prompt"  : Transcribed user speech ready for processing.
      - "dtmf"    : DTMF tone detected (logged, not acted on).
      - "interrupt": User interrupted the agent mid-speech.
      - Others    : Logged and ignored.
    """
    await websocket.accept()
    logger.info("WebSocket connection accepted (English)")

    # -- Per-session state --
    conversation_history: list[dict] = []
    if LLM_PROVIDER == "claude":
        tools: list[dict] = get_tools()
    elif LLM_PROVIDER == "gemini":
        tools: list[dict] = get_tools_gemini()
    else:
        tools: list[dict] = get_tools_openai()

    call_sid: str = "unknown"
    caller_phone: str = ""
    full_transcript: list[dict[str, str]] = []
    call_start_time: str = datetime.now().isoformat()
    system_prompt: str = _build_system_prompt({"known": False})
    handoff_already_fired: bool = False

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
                caller_phone = _call_phone.pop(call_sid, "")
                session = _call_session.pop(call_sid, None)

                caller_context: dict = {
                    "active_session": (
                        {
                            "vehicle": session.vehicle,
                            "claim_reference": session.claim_reference,
                            "eta_minutes": active_session.remaining_eta_minutes(session),
                            "accident_description": session.accident_description,
                        }
                        if session else None
                    ),
                }

                system_prompt = _build_system_prompt(caller_context)

                logger.info(
                    "Session setup â€” CallSid: %s, StreamSid: %s, Phone: %s, continuation=%s",
                    call_sid,
                    message.get("streamSid", "n/a"),
                    caller_phone,
                    caller_context["active_session"] is not None,
                )
                logger.info(
                    "Session ready â€” system prompt length: %d chars, tools: %d",
                    len(system_prompt),
                    len(tools),
                )

            # ---------------------------------------------------------------
            # PROMPT â€” user speech transcribed
            # ---------------------------------------------------------------
            elif msg_type == "prompt":
                user_text = message.get("voicePrompt", "").strip()
                if not user_text:
                    logger.debug("Empty voicePrompt received â€” ignoring")
                    continue

                logger.info("Caller [%s]: %s", call_sid, user_text)
                full_transcript.append({"role": "user", "text": user_text})

                # Retrieve KB context for this utterance
                try:
                    kb_context = retrieve_context(user_text)
                except Exception:
                    logger.exception("KB retrieval failed")
                    kb_context = ""

                # Inject KB context into the user message (not the system prompt)
                if kb_context and kb_context != "No knowledge base loaded. Answering from general knowledge.":
                    user_message = f"[Reference context: {kb_context}]\n\nCaller: {user_text}"
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
                            caller_phone=caller_phone,
                        )
                    elif LLM_PROVIDER == "gemini":
                        response_text = await _run_llm_streaming_gemini(
                            gemini_client=gemini_client,
                            system=system_prompt,
                            conversation_history=conversation_history,
                            tools=tools,
                            websocket=websocket,
                            caller_phone=caller_phone,
                        )
                    else:
                        response_text = await _run_llm_streaming(
                            client=openai_client,
                            system=system_prompt,
                            conversation_history=conversation_history,
                            tools=tools,
                            websocket=websocket,
                            caller_phone=caller_phone,
                        )
                    logger.info("Agent [%s]: %s", call_sid, response_text[:200])
                    if response_text:
                        full_transcript.append({"role": "assistant", "text": response_text})

                    # If the agent just handed off to a live agent, log the
                    # event for post-call analytics but do NOT end the
                    # ConversationRelay session â€” Nimali has already spoken
                    # the handoff confirmation line via the normal TTS path,
                    # and we want the caller to be able to stay on / hang up
                    # themselves. There is no real live-agent queue in this
                    # demo, so the handoff is simulated verbally.
                    if not handoff_already_fired and _handoff_just_executed(conversation_history):
                        handoff_already_fired = True
                        logger.info(
                            "Live-agent handoff fired [%s] â€” staying on call, "
                            "caller may hang up when ready",
                            call_sid,
                        )
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
            # INTERRUPT â€” user interrupted agent speech
            # ---------------------------------------------------------------
            elif msg_type == "interrupt":
                logger.info(
                    "Speech interrupted by caller [%s] â€” utteranceUntilInterrupt: '%s'",
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
        call_end_time = datetime.now().isoformat()
        logger.info(
            "Session ended â€” CallSid: %s, history: %d msgs, transcript: %d msgs",
            call_sid, len(conversation_history), len(full_transcript),
        )
        if full_transcript:
            asyncio.create_task(
                process_post_call_data(
                    call_sid=call_sid,
                    lang="en",
                    caller_phone=caller_phone or "unknown",
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
