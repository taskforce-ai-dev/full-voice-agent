"""
server.py â€” Main FastAPI server for Bank of Sri Lanka (BSL) Voice Agent.

Handles:
  - Incoming call webhook (POST /voice/incoming) â€” fixed BSL welcome greeting.
  - ConversationRelay WebSocket (/ws/conversation) â€” English only, ElevenLabs TTS.
  - Streaming LLM responses (Claude / OpenAI / Gemini) with tool-use support.
  - Knowledge-base context injection.
  - Post-call data capture on WebSocket disconnect.
  - Health endpoint (GET /health).

Architecture:
  Incoming call
    â†’ POST /voice/incoming  (store caller phone keyed by CallSid)
    â†’ TwiML <Connect><ConversationRelay /></Connect> with BSL welcome
    â†’ WebSocket /ws/conversation (English only)
    â†’ LLM streaming with tool use (verify_customer_identity, get_account_balance,
      block_debit_card, get_account_details, get_recent_transactions,
      get_loans, get_standing_orders, request_live_agent_handoff)
    â†’ text tokens â†’ Twilio TTS â†’ caller

BSL differences from SLIC:
  - No active_session / cross-call continuations
  - No language routing â€” English only
  - BSL-specific 5-step flow system prompt
  - execute_tool receives both caller_phone= and call_sid=
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
    )
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
from bsl_api import is_configured, close_session
from post_call import process_post_call_data
import account_state

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
GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "")
ELEVENLABS_API_KEY: str = os.getenv("ELEVENLABS_API_KEY", "")
ELEVENLABS_VOICE_ID: str = os.getenv("ELEVENLABS_VOICE_ID", "")
TWILIO_ACCOUNT_SID: str = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN: str = os.getenv("TWILIO_AUTH_TOKEN", "")
KB_DOCS_DIRECTORY: str = os.getenv("KB_DOCS_DIRECTORY", "knowledge_docs")
KB_RELOAD_SECRET: str = os.getenv("KB_RELOAD_SECRET", "")
PORT: int = int(os.getenv("PORT", "8000"))

# LLM provider selection: "claude" (default), "openai", or "gemini"
LLM_PROVIDER: str = os.getenv("LLM_PROVIDER", "claude")
CLAUDE_MODEL: str = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6")
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

MAX_TOKENS: int = 600
MAX_HISTORY_MESSAGES: int = 20
MAX_TOOL_ROUNDS: int = 5

# ---------------------------------------------------------------------------
# Per-call bridge dicts â€” populated by HTTP handler, consumed by WebSocket
# ---------------------------------------------------------------------------
_call_phone: dict[str, str] = {}  # CallSid -> caller phone number
# BSL has no _call_session â€” no cross-call continuations

# ---------------------------------------------------------------------------
# Filler messages sent while tools execute
# ---------------------------------------------------------------------------
TOOL_FILLERS: dict[str, str] = {
    "verify_customer_identity": "Give me just a moment while I verify that for you.",
    "get_account_balance": "Let me pull up your balance now.",
    "block_debit_card": "I'm processing that card block right now.",
    "get_account_details": "Let me retrieve your account details.",
    "get_recent_transactions": "Let me look up your recent transactions.",
    "get_loans": "Let me check your loan information.",
    "get_standing_orders": "Let me retrieve your standing orders.",
    "request_live_agent_handoff": "Let me transfer you now.",
}
DEFAULT_FILLER: str = "Let me check that for you."

# ---------------------------------------------------------------------------
# English ConversationRelay configuration
# ---------------------------------------------------------------------------
EN_CONFIG: dict[str, str] = {
    "tts_provider": "ElevenLabs",
    "voice": os.getenv(
        "ELEVENLABS_CR_VOICE",
        "bm3QvaZ3fUSCRBC3UV1f-flash_v2_5",
    ),
    "language": "en-US",
    "extra_attrs": '        elevenlabsTextNormalization="on"\n',
}

BSL_WELCOME: str = (
    "Welcome to Bank of Sri Lanka. "
    "You're speaking with our virtual assistant. "
    "What can I assist you with today?"
)


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

def _build_system_prompt() -> str:
    """Build the BSL voice agent system prompt."""
    today = date.today().isoformat()
    return f"""You are a virtual assistant for Bank of Sri Lanka. You have no name. You are professional, concise, and helpful.
Today's date is {today}.

VOICE RULES:
- You are on a telephone call. Speak naturally, not robotically.
- Keep replies short â€” one or two sentences. No monologues.
- No markdown, bullet points, URLs, or abbreviations.
- Say amounts in LKR naturally: "three hundred fifty-four thousand, two hundred seventeen rupees" not "354,217.00 LKR".
- Read account numbers as last 4 digits only.
- Whenever you speak ANY sequence of digits aloud (account numbers, card numbers, confirmation readbacks, or any other numbers read digit by digit), spell each digit as an individual word â€” NEVER output bare numerals for digit-by-digit speech. Always say "zero" for 0, NEVER "oh" or "o". Examples: "9201" â†’ "nine two zero one", "1042" â†’ "one zero four two", "0056" â†’ "zero zero five six".
- NEVER read the full account number, full card number, NIC, DOB, or mother's maiden name aloud.
- Use contractions naturally (I've, you're, I'll).
- DIGIT REPETITION SHORTHAND: when the caller uses "double X" or "triple X" while reading digits (account number, NIC, card number, phone, etc.), expand it inline before using or confirming the number. "double eight" â†’ 88, "double zero" â†’ 00, "triple seven" â†’ 777, "triple four" â†’ 444. Never collapse "double N" into a single N. Example: caller says "one zero four two double eight three seven nine two zero one" â†’ record 104288379201 (12 digits).

STRICT 5-STEP FLOW â€” follow in order, do not skip:

STEP 1 â€” INTENT, ACCOUNT NUMBER AND ACCOUNT TYPE:
The greeting has already asked the caller what they need. Their first utterance is a reply.
- Identify their intent: Account Balance, Block Debit Card, Account Details, Recent Transactions, Loans, or Standing Orders.
- Ask for the account number and account type in a single question: "Of course. Could you please state your account number and let me know if it's a Personal, Current, or Business account?"
- Once you have both, read back the LAST 4 DIGITS for confirmation: "Just to confirm, the account ending in [spell digits] â€” is that correct?"
- Do NOT fire any tool until the caller confirms the last 4 digits.
- If the caller corrects any part of the account number after the read-back (e.g. you said "ending in nine two one" and they reply "no, it's 9201"), do NOT splice the correction onto your previous parse. Instead say: "My apologies. Could you please state the full account number from the beginning?" and re-collect the entire number. Splicing produces invalid hybrid numbers (e.g. an 11-digit string) that fail lookup.

STEP 2 â€” VOICE VERIFICATION (ASK ONE QUESTION AT A TIME):
Collect the three security details ONE AT A TIME. Wait for each answer before asking the next. Do NOT ask multiple at once â€” the caller will interrupt, and long prompts cause STT errors. Each ask is one short sentence.

For Personal/Current accounts, open with: "To verify your identity, I'll need a few details. Can I have your NIC number?"
For Business accounts, open with: "To verify the primary account holder's identity, I'll need a few details. Can I have your NIC number?"

After the caller gives the NIC, sanity-check it silently:
- VALID: 9 digits + V or X (old format, e.g. "901234567V"), OR exactly 12 digits with no letter suffix (new format, e.g. "198245678901"). Never reject a 12-digit all-numeric NIC as "too long".
- If fewer than 9 digits, or 10-11 digits with no suffix: ask them to repeat just the NIC.
If valid, move on without reading the NIC back: "Thank you. And your date of birth, please?"

After the caller gives DOB, sanity-check it silently. DOB must include day, month, AND year. If any part is missing, ask only for the missing piece (e.g. "And the year, please?"). Once complete, move on: "Thank you. And finally, your mother's maiden name?"

After the caller gives the maiden name, do NOT call verify_customer_identity yet. First read back what you heard so the caller can correct STT mis-recognitions of Sinhala/Tamil names: "Just to confirm, I heard your mother's maiden name as [exactly what STT transcribed] â€” is that correct?"
- If the caller confirms: call verify_customer_identity ONCE with all three fields.
- If the caller corrects (e.g. "no, it's [different name]"): use the corrected name and call verify_customer_identity with the corrected value. Do not read it back a second time â€” one readback is enough.
- If the caller says it's close but not quite, ask them to spell it: "Could you spell that for me, letter by letter?"

The maiden-name backend uses phonetic matching, so accept whatever the caller provides â€” even short or garbled transcripts. The readback exists to catch wholesale STT substitutions (e.g. "Rajapaksa" heard as "Roger Parker") that phonetic matching can't recover from.

STEP 3 â€” IDENTITY CONFIRMATION:
On verified=true: "Thank you for that. I've confirmed your identity successfully. Give me just a moment while I retrieve that for you." Then call the tool for Step 1 intent.
On account_not_found=true: "I'm sorry, I couldn't find an account with that number. Could you please repeat the full 12-digit account number for me?" Then return to Step 1 to re-collect the account number. Do NOT re-ask NIC, DOB, or mother's maiden name â€” keep what you already have and re-call verify_customer_identity with the corrected account number once they provide it. This does not count as a verification attempt.
On verified=false with attempts_remaining > 0:
  - Read the `mismatched_fields` list in the tool result. Only re-ask the fields IN that list â€” keep the values of fields NOT in the list from the previous attempt and reuse them on the next call.
  - If only "nic" is in mismatched_fields: "I'm sorry, your NIC didn't match what we have on file. You have [X] attempt(s) remaining. Could you please repeat your NIC number?" Then ask only for NIC.
  - If only "dob" is in mismatched_fields: ask only for DOB with the same phrasing pattern.
  - If only "mothers_maiden_name" is in mismatched_fields: ask only for the maiden name, then read it back before re-calling verify_customer_identity.
  - If multiple fields mismatched: phrase it as "I wasn't able to match a few details. You have [X] attempt(s) remaining â€” could you please repeat [list the fields]?" Then collect each ONE AT A TIME, only the mismatched ones. Read back the maiden name if it's among them.
  - When calling verify_customer_identity again, pass the FRESHLY-COLLECTED value for each mismatched field plus the KEPT value (from the previous attempt) for each non-mismatched field. Do NOT re-collect fields the backend already matched.
On attempts_remaining = 0 (3 failures): call request_live_agent_handoff(reason="verification_failed") then speak its message verbatim. STOP â€” do not ask any more questions.

STEP 4 â€” SERVE REQUEST:
When quoting any account last-4, spell each digit as a word (e.g. "nine two zero one" not "9201").
Account Balance:
  Personal/Current: "The available balance on your account ending in [spell digits] is [amount in natural LKR]."
  Business: "The current balance on your business account ending in [spell digits] stands at [amount in natural LKR]."
Block Debit Card:
  Personal/Current: "I'm placing a block on the debit card associated with account ending in [spell digits] now. That's done â€” your card has been successfully blocked and you're fully protected."
  Business: "I'm blocking the debit card linked to business account ending in [spell digits] right away. That's confirmed â€” your corporate debit card has been blocked successfully."
Account Details:
  Personal/Current: "Here are the details for account ending in [spell digits]: it's a [type] account, opened on [date] at our [branch] branch."
  Business: "Here are the details for business account ending in [spell digits]: registered under [company name], opened on [date]."
Transactions: Summarise the top 5 most recent naturally â€” "Your last few transactions include..."
Loans: Read each loan's outstanding balance, monthly payment, and next due date naturally.
Standing Orders: Read each payee, amount, and execution date naturally.

STEP 5 â€” WRAP-UP / LOOP:
Always ask: "Is there anything else I can help you with today?"
If YES: return to Step 1 for the new request. Re-verification NOT required for the same already-verified account in the same session. If they mention a DIFFERENT account, treat as fresh Step 1 with re-verification.
If NO: "It was a pleasure assisting you today, [Name / Company Name]. On behalf of Bank of Sri Lanka, we wish you a wonderful day. Goodbye!"

CRITICAL RULES:
- NEVER ask for the caller's phone number â€” it is never needed.
- NEVER speak NIC, DOB, or mother's maiden name back to the caller.
- NEVER read the full account number or full card number â€” last 4 digits only.
- All three verification fields must be collected before verify_customer_identity fires â€” one call, not one per field.
- After 3 failed verification attempts: call request_live_agent_handoff(reason="verification_failed"), speak its message, then stop completely.
- Live agent handoff is verbal only â€” the call does NOT hang up after handoff.
- If the caller asks to speak to a human at any time: call request_live_agent_handoff(reason="caller_requested_human") and speak its message.
"""


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
    logger.info("Starting BSL Voice Agent server...")

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
        logger.warning(
            "ELEVENLABS_API_KEY or ELEVENLABS_VOICE_ID not set â€” "
            "ConversationRelay TTS will not work in production."
        )

    logger.info("Server startup complete. bsl_api configured: %s", is_configured())

    yield

    # --- Shutdown ---
    logger.info("Shutting down server...")
    try:
        await close_session()
    except Exception as exc:
        logger.warning("Error closing BSL API session: %s", exc)
    logger.info("Shutdown complete.")


# ---------------------------------------------------------------------------
# FastAPI application
# ---------------------------------------------------------------------------
app = FastAPI(
    title="BSL Voice Agent",
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
        "bsl_api_configured": is_configured(),
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

STT_HINTS: str = (
    # Maiden names stored in mock_customers.json + their common spelling/pronunciation
    # variants. Multiple anchor points per name help STT land on the Sinhala-name
    # alternative instead of an English-sounding decode (e.g. "Roger Parker").
    "Jayasinghe,Jayasingha,Jayasinha,Jayasinhe,"
    "Fernando,Fernandes,Fernandez,Fernandus,"
    "Seneviratne,Seneviratna,Senevirathne,Senevirathna,"
    "Rajapaksa,Rajapakse,Rajapaksha,Rajapakshe,Raja Paksa,Raja Paksha,"
    "Gunawardena,Gunawardhana,Gunawardhane,Goonawardena,Gunavardena,"
    # Common Sri Lankan surnames to bias the STT toward Sinhala/Tamil phoneme patterns.
    "Perera,Silva,De Silva,Wickramasinghe,Wijesinghe,Bandaranaike,Senanayake,"
    "Karunaratne,Dissanayake,Ratnayake,Tennakoon,Mendis,Pieris,Goonetilleke,"
    "Samaraweera,Kotelawala,Hettiarachchi,Amarasinghe,Liyanage,Abeysekera,"
    "Edirisinghe,Wickremesinghe,"
    # Bank-specific vocabulary that STT mangles.
    "NIC,NIC number,National Identity Card,Bank of Sri Lanka,"
    "Personal account,Current account,Business account"
)


def _build_conversation_relay_twiml(host: str, welcome_greeting: str) -> str:
    """Build the <ConversationRelay> XML tag with a welcome greeting."""
    extra = EN_CONFIG["extra_attrs"]
    greeting = xml.sax.saxutils.escape(welcome_greeting)
    hints = xml.sax.saxutils.quoteattr(STT_HINTS)

    return (
        f'<ConversationRelay url="wss://{host}/ws/conversation"\n'
        f'        ttsProvider="{EN_CONFIG["tts_provider"]}"\n'
        f'        voice="{EN_CONFIG["voice"]}"\n'
        f'{extra}'
        f'        language="{EN_CONFIG["language"]}"\n'
        f'        transcriptionProvider="google"\n'
        f'        speechModel="telephony"\n'
        f'        hints={hints}\n'
        f'        speechTimeout="2500"\n'
        f'        welcomeGreeting="{greeting}"\n'
        '        interruptible="true"\n'
        '        dtmfDetection="true">\n'
        "    </ConversationRelay>"
    )


@app.post("/voice/incoming")
async def voice_incoming(request: Request) -> Response:
    """Twilio webhook for incoming phone calls."""
    form = await request.form()
    host = request.headers.get("host", request.url.hostname or "localhost")

    call_sid = str(form.get("CallSid", ""))
    caller_phone = str(form.get("From", ""))

    if call_sid and caller_phone:
        _call_phone[call_sid] = caller_phone

    cr_tag = _build_conversation_relay_twiml(host, BSL_WELCOME)
    twiml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<Response>\n"
        "  <Connect>\n"
        f"    {cr_tag}\n"
        "  </Connect>\n"
        "</Response>"
    )

    logger.info("Incoming call â€” CallSid: %s, From: %s", call_sid, caller_phone)
    return Response(content=twiml, media_type="application/xml")


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
    call_sid: str,
) -> str:
    """Stream an OpenAI response, handling tool use in a loop.

    Sends text tokens to the WebSocket as they arrive so the caller hears
    speech with minimal latency. When the model invokes tools, a filler
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
                        tc["name"], parsed_input,
                        caller_phone=caller_phone, call_sid=call_sid,
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
    call_sid: str,
) -> str:
    """Stream a Gemini response via the native SDK, handling tool use.

    Uses the same history format (OpenAI) internally, converting to Gemini
    format for each API call. Tool results are appended in OpenAI format
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
                        tc["function"]["name"], parsed_input,
                        caller_phone=caller_phone, call_sid=call_sid,
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
    call_sid: str,
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
                        tb["name"], tb["input"],
                        caller_phone=caller_phone, call_sid=call_sid,
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
      - "setup"     : Session initialization (call metadata).
      - "prompt"    : Transcribed user speech ready for processing.
      - "dtmf"      : DTMF tone detected (logged, not acted on).
      - "interrupt" : User interrupted the agent mid-speech.
      - Others      : Logged and ignored.
    """
    await websocket.accept()
    logger.info("WebSocket connection accepted")

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
    call_start_time: datetime = datetime.now()
    system_prompt: str = _build_system_prompt()
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

            # -----------------------------------------------------------
            # SETUP
            # -----------------------------------------------------------
            if msg_type == "setup":
                call_sid = message.get("callSid", "unknown")
                caller_phone = _call_phone.pop(call_sid, "")
                # No active session lookup â€” BSL has no continuations

                logger.info(
                    "Session setup â€” CallSid: %s, StreamSid: %s, Phone: %s",
                    call_sid,
                    message.get("streamSid", "n/a"),
                    caller_phone,
                )
                logger.info(
                    "Session ready â€” system prompt length: %d chars, tools: %d",
                    len(system_prompt),
                    len(tools),
                )

            # -----------------------------------------------------------
            # PROMPT â€” user speech transcribed
            # -----------------------------------------------------------
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
                            call_sid=call_sid,
                        )
                    elif LLM_PROVIDER == "gemini":
                        response_text = await _run_llm_streaming_gemini(
                            gemini_client=gemini_client,
                            system=system_prompt,
                            conversation_history=conversation_history,
                            tools=tools,
                            websocket=websocket,
                            caller_phone=caller_phone,
                            call_sid=call_sid,
                        )
                    else:
                        response_text = await _run_llm_streaming(
                            client=openai_client,
                            system=system_prompt,
                            conversation_history=conversation_history,
                            tools=tools,
                            websocket=websocket,
                            caller_phone=caller_phone,
                            call_sid=call_sid,
                        )
                    logger.info("Agent [%s]: %s", call_sid, response_text[:200])
                    if response_text:
                        full_transcript.append({"role": "assistant", "text": response_text})

                    # Track live-agent handoff (verbal only â€” call stays open)
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

            # -----------------------------------------------------------
            # DTMF
            # -----------------------------------------------------------
            elif msg_type == "dtmf":
                digit = message.get("digit", "?")
                logger.info("DTMF received [%s]: %s", call_sid, digit)

            # -----------------------------------------------------------
            # INTERRUPT â€” user interrupted agent speech
            # -----------------------------------------------------------
            elif msg_type == "interrupt":
                logger.info(
                    "Speech interrupted by caller [%s] â€” utteranceUntilInterrupt: '%s'",
                    call_sid,
                    message.get("utteranceUntilInterrupt", ""),
                )

            # -----------------------------------------------------------
            # OTHER
            # -----------------------------------------------------------
            else:
                logger.debug("Unhandled message type '%s' [%s]: %s", msg_type, call_sid, raw[:200])

    except WebSocketDisconnect:
        logger.info("WebSocket disconnected â€” CallSid: %s", call_sid)
    except Exception:
        logger.exception("Unexpected error in WebSocket handler [%s]", call_sid)
    finally:
        call_end_time = datetime.now()
        account_state.clear(call_sid)
        logger.info(
            "Session ended â€” CallSid: %s, history: %d msgs, transcript: %d msgs",
            call_sid, len(conversation_history), len(full_transcript),
        )
        if full_transcript:
            transcript_str = "\n".join(
                f"{'Caller' if m['role'] == 'user' else 'Agent'}: {m['text']}"
                for m in full_transcript
            )
            asyncio.create_task(
                process_post_call_data(
                    call_sid=call_sid,
                    caller_phone=caller_phone or "unknown",
                    full_transcript=transcript_str,
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
