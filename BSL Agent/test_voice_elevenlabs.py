"""
test_voice_elevenlabs.py -- Local demo CLI with ElevenLabs TTS for BSL Voice Agent.

Uses ElevenLabs for TTS playback. Text-only mode if TTS env vars are missing.

Usage:
    python test_voice_elevenlabs.py

Environment variables:
    ANTHROPIC_API_KEY      -- Required.
    ELEVENLABS_API_KEY     -- Required for voice. Text-only mode if absent.
    ELEVENLABS_VOICE_ID    -- Required. Your cloned voice ID from ElevenLabs.
    ELEVENLABS_MODEL       -- Optional. Default: eleven_multilingual_v2
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import queue
import re
import sys
import threading
from datetime import date
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# Third-party imports
# ---------------------------------------------------------------------------
from dotenv import load_dotenv

load_dotenv()

import anthropic

# requests for ElevenLabs API
try:
    import requests
    REQUESTS_AVAILABLE = True
except ImportError:
    requests = None  # type: ignore[assignment]
    REQUESTS_AVAILABLE = False

# PyAudio for playback
try:
    import pyaudio
    PYAUDIO_AVAILABLE = True
except ImportError:
    pyaudio = None  # type: ignore[assignment]
    PYAUDIO_AVAILABLE = False

# ---------------------------------------------------------------------------
# Local imports
# ---------------------------------------------------------------------------
from knowledge_base import initialize_kb, prewarm, retrieve_context
from tools import get_tools, execute_tool

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("test_voice_11labs")

# Quieten noisy libraries
logging.getLogger("chromadb").setLevel(logging.WARNING)
logging.getLogger("sentence_transformers").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
MODEL = "claude-sonnet-4-6"
MAX_TOOL_ROUNDS = 5

ELEVENLABS_TTS_URL = "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream"
ELEVENLABS_DEFAULT_MODEL = "eleven_multilingual_v2"

DEMO_CALL_SID = "test-cli-session"

# Filler messages spoken while tools execute
TOOL_FILLERS: Dict[str, str] = {
    "verify_customer_identity": "Give me just a moment while I verify that for you.",
    "get_account_balance": "Let me pull up your balance now.",
    "block_debit_card": "I'm processing that card block right now.",
    "get_account_details": "Let me retrieve your account details.",
    "get_recent_transactions": "Let me look up your recent transactions.",
    "get_loans": "Let me check your loan information.",
    "get_standing_orders": "Let me retrieve your standing orders.",
    "request_live_agent_handoff": "Let me transfer you now.",
}
DEFAULT_FILLER = "Let me check that for you."

SENTENCE_END = re.compile(r"[.!?។]\s*")

# ---------------------------------------------------------------------------
# System prompt (built inline -- do NOT import from server.py)
# ---------------------------------------------------------------------------
def build_system_prompt() -> str:
    today = date.today().isoformat()
    return f"""You are a virtual assistant for Bank of Sri Lanka. You have no name. Professional, concise, helpful.
Today's date is {today}.

VOICE RULES:
- One or two short sentences per reply. No monologues.
- No markdown, bullet points, or URLs.
- Say amounts naturally: "three hundred fifty-four thousand rupees" not "354,000 LKR".
- Read account numbers as last 4 digits only.
- NEVER speak NIC, DOB, or mother's maiden name back.

STRICT 5-STEP FLOW:

STEP 1 -- INTENT AND ACCOUNT NUMBER:
- Identify intent: Balance, Block Card, Account Details, Recent Transactions, Loans, Standing Orders.
- Ask for account number, then confirm: "And just to confirm -- is this a Personal or Current account, or a Business account?"
- Read back last 4 digits for confirmation before proceeding.

STEP 2 -- VOICE VERIFICATION (collect ALL FOUR before calling tool):
Q1 NIC: "Could you please say your National Identity Card number?" (add "of the primary account holder" for business)
Q2 Branch: "Which branch was this account originally opened at?"
Q3 DOB: "Could you please say your date of birth -- the day, month, and year?"
Q4 Mother's Maiden Name: "Could you please say your mother's maiden name?"
Call verify_customer_identity ONCE after collecting all four.

STEP 3 -- IDENTITY CONFIRMATION:
- verified=true: "Thank you for that. I've confirmed your identity successfully." Then call the intent tool.
- verified=false, attempts_remaining > 0: "I'm sorry, I wasn't able to match that information. You have [X] attempt(s) remaining -- please try once more."
- attempts_remaining = 0: call request_live_agent_handoff(reason="verification_failed"), speak its message, STOP.

STEP 4 -- SERVE REQUEST per BSL script wording (balance/block/details/transactions/loans/standing orders).

STEP 5 -- WRAP-UP: "Is there anything else I can help you with today?" Loop or goodbye.

CRITICAL: All 4 verification fields before verify_customer_identity. Never ask for phone number."""


# ---------------------------------------------------------------------------
# ElevenLabs TTS: synthesize and play
# ---------------------------------------------------------------------------
def elevenlabs_speak(
    api_key: str,
    voice_id: str,
    text: str,
    model_id: str = ELEVENLABS_DEFAULT_MODEL,
) -> None:
    """Synthesize text with ElevenLabs streaming API and play through speakers."""
    if not REQUESTS_AVAILABLE or requests is None:
        logger.error("requests library not installed.")
        return
    if not PYAUDIO_AVAILABLE or pyaudio is None:
        logger.warning("pyaudio not available -- cannot play audio.")
        return

    # output_format is a QUERY PARAMETER, not in the JSON body
    url = ELEVENLABS_TTS_URL.format(voice_id=voice_id) + "?output_format=pcm_24000"

    headers = {
        "xi-api-key": api_key,
        "Content-Type": "application/json",
    }

    payload = {
        "text": text,
        "model_id": model_id,
        "voice_settings": {
            "stability": 0.5,
            "similarity_boost": 0.75,
            "style": 0.0,
            "use_speaker_boost": True,
        },
    }

    try:
        # Use streaming to reduce time-to-first-byte
        resp = requests.post(
            url,
            json=payload,
            headers=headers,
            stream=True,
            timeout=15,
        )

        if resp.status_code != 200:
            logger.error(
                "ElevenLabs API error %d: %s",
                resp.status_code,
                resp.text[:200],
            )
            return

        # Collect PCM audio chunks
        audio_buffer = io.BytesIO()
        for chunk in resp.iter_content(chunk_size=4096):
            if chunk:
                audio_buffer.write(chunk)

        audio_data = audio_buffer.getvalue()
        if not audio_data:
            logger.warning("ElevenLabs returned empty audio for: %s", text[:50])
            return

        # Play PCM audio (16-bit, 24kHz, mono)
        p = pyaudio.PyAudio()
        stream = p.open(
            format=pyaudio.paInt16,
            channels=1,
            rate=24000,
            output=True,
        )
        stream.write(audio_data)
        stream.stop_stream()
        stream.close()
        p.terminate()

        logger.info("TTS: played '%s' via ElevenLabs", text[:50])

    except requests.exceptions.Timeout:
        logger.error("ElevenLabs API timeout for: %s", text[:50])
    except Exception:
        logger.exception("ElevenLabs TTS failed for: %s", text[:80])


# ---------------------------------------------------------------------------
# TTS background worker (ElevenLabs)
# ---------------------------------------------------------------------------
def tts_worker(
    tts_queue: queue.Queue,
    api_key: str,
    voice_id: str,
    model_id: str,
) -> None:
    """Background thread that synthesizes and plays sentences from the queue."""
    while True:
        item = tts_queue.get()
        if item is None:
            tts_queue.task_done()
            break

        text = item.strip()
        if not text:
            tts_queue.task_done()
            continue

        try:
            elevenlabs_speak(api_key, voice_id, text, model_id)
        except Exception:
            logger.exception("TTS failed for: %s", text[:80])

        tts_queue.task_done()


# ---------------------------------------------------------------------------
# Tool execution helper
# ---------------------------------------------------------------------------
def run_tool(tool_name: str, tool_input: Dict[str, Any]) -> str:
    """Call the async execute_tool from synchronous code."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                result = pool.submit(
                    asyncio.run,
                    execute_tool(tool_name, tool_input, call_sid=DEMO_CALL_SID),
                ).result()
            return result
        return loop.run_until_complete(
            execute_tool(tool_name, tool_input, call_sid=DEMO_CALL_SID)
        )
    except RuntimeError:
        return asyncio.run(
            execute_tool(tool_name, tool_input, call_sid=DEMO_CALL_SID)
        )


# ---------------------------------------------------------------------------
# Sentence splitting helper
# ---------------------------------------------------------------------------
def split_sentences(text: str) -> List[str]:
    """Split text on sentence-ending punctuation."""
    parts = SENTENCE_END.split(text)
    sentences: List[str] = []
    offset = 0
    for part in parts:
        end = offset + len(part)
        m = SENTENCE_END.search(text, end)
        if m and m.start() == end:
            sentences.append(text[offset : m.end()].strip())
            offset = m.end()
        else:
            leftover = text[offset:].strip()
            if leftover:
                sentences.append(leftover)
            break
    result = [s for s in sentences if s]
    return result if result else [text.strip()] if text.strip() else []


# ---------------------------------------------------------------------------
# Main conversation loop
# ---------------------------------------------------------------------------
def main() -> None:
    # ------------------------------------------------------------------
    # Pre-flight checks
    # ------------------------------------------------------------------
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        print("ERROR: ANTHROPIC_API_KEY is not set. Add it to your .env file.")
        sys.exit(1)

    # ------------------------------------------------------------------
    # Initialise knowledge base
    # ------------------------------------------------------------------
    print("Initialising knowledge base...")
    prewarm()
    initialize_kb()
    print("Knowledge base ready.\n")

    # ------------------------------------------------------------------
    # ElevenLabs TTS setup
    # ------------------------------------------------------------------
    tts_enabled = False
    tts_queue: Optional[queue.Queue] = None
    tts_thread: Optional[threading.Thread] = None

    el_api_key = os.getenv("ELEVENLABS_API_KEY", "")
    el_voice_id = os.getenv("ELEVENLABS_VOICE_ID", "")
    el_model = os.getenv("ELEVENLABS_MODEL", ELEVENLABS_DEFAULT_MODEL)

    if el_api_key and el_voice_id:
        if not REQUESTS_AVAILABLE:
            print("WARNING: requests library not installed. Text-only mode.")
            print("         Install: pip install requests\n")
        elif not PYAUDIO_AVAILABLE:
            print("WARNING: pyaudio not installed. Text-only mode.")
            print("         Install: pip install pyaudio\n")
        else:
            tts_enabled = True
            logger.info(
                "ElevenLabs TTS ready (voice: %s, model: %s)",
                el_voice_id[:12] + "...",
                el_model,
            )

            # Start the TTS background worker
            tts_queue = queue.Queue()
            tts_thread = threading.Thread(
                target=tts_worker,
                args=(tts_queue, el_api_key, el_voice_id, el_model),
                daemon=True,
            )
            tts_thread.start()
    else:
        if not el_api_key:
            print("WARNING: ELEVENLABS_API_KEY not set in .env. Text-only mode.")
        if not el_voice_id:
            print("WARNING: ELEVENLABS_VOICE_ID not set in .env. Text-only mode.")
        print("         Get both from elevenlabs.io > Profile > API Keys / Voices\n")

    # ------------------------------------------------------------------
    # Anthropic client and conversation state
    # ------------------------------------------------------------------
    client = anthropic.Anthropic(api_key=api_key)
    system_prompt = build_system_prompt()
    tools = get_tools()
    messages: List[Dict[str, Any]] = []

    print("=" * 60)
    print("  BSL Voice Agent (Virtual Assistant) -- ElevenLabs Demo")
    if tts_enabled:
        print(f"  ElevenLabs TTS: ON (model: {el_model})")
        print(f"  Voice ID: {el_voice_id[:20]}...")
    else:
        print("  TTS: OFF (text-only mode)")
    print("  Type your message and press Enter.")
    print("  Type 'quit', 'exit', or 'q' to end.")
    print("=" * 60)
    print()

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------
    while True:
        try:
            user_input = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye!")
            break

        if not user_input:
            continue
        if user_input.lower() in ("quit", "exit", "q"):
            print("Goodbye!")
            break

        # -- Knowledge-base retrieval --
        kb_context = retrieve_context(user_input)
        if kb_context:
            user_content = (
                f"[Reference context: {kb_context}]\n\n"
                f"Caller: {user_input}"
            )
        else:
            user_content = user_input

        messages.append({"role": "user", "content": user_content})

        # -- Tool-use loop --
        tool_rounds = 0
        while tool_rounds < MAX_TOOL_ROUNDS:
            try:
                api_kwargs: Dict[str, Any] = {
                    "model": MODEL,
                    "max_tokens": 512,
                    "system": system_prompt,
                    "messages": messages,
                }
                if tools:
                    api_kwargs["tools"] = tools

                response = client.messages.create(**api_kwargs)
            except anthropic.APIError as exc:
                logger.error("Anthropic API error: %s", exc)
                print("Agent: Sorry, I encountered an error. Please try again.")
                messages.pop()
                break

            # Check for tool use
            has_tool_use = any(
                block.type == "tool_use" for block in response.content
            )

            if not has_tool_use:
                # -- Final text response --
                text_parts: List[str] = []
                for block in response.content:
                    if block.type == "text":
                        text_parts.append(block.text)

                full_text = " ".join(text_parts).strip()
                messages.append({"role": "assistant", "content": response.content})
                print(f"Agent: {full_text}")

                # -- TTS (send full response as one call for natural prosody) --
                if tts_enabled and tts_queue is not None and full_text:
                    # Send entire response as one TTS call -- splitting into
                    # sentences causes prosody resets and robotic tone
                    tts_queue.put(full_text)
                    tts_queue.join()

                break

            # -- Tool call detected --
            messages.append({"role": "assistant", "content": response.content})

            tool_results: List[Dict[str, Any]] = []
            for block in response.content:
                if block.type != "tool_use":
                    continue

                tool_name = block.name
                tool_input = block.input
                tool_use_id = block.id

                print(f"  [tool] {tool_name}({json.dumps(tool_input, ensure_ascii=False)})")

                # Speak filler while tool executes
                if tts_enabled and tts_queue is not None:
                    filler = TOOL_FILLERS.get(tool_name, DEFAULT_FILLER)
                    tts_queue.put(filler)

                try:
                    result_str = run_tool(tool_name, tool_input)
                except Exception as exc:
                    logger.error("Tool execution failed: %s", exc)
                    result_str = json.dumps({"error": str(exc)})

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": result_str,
                })

            messages.append({"role": "user", "content": tool_results})
            tool_rounds += 1

        else:
            print("Agent: I'm having trouble completing that request. Could you try again?")

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------
    if tts_queue is not None:
        tts_queue.put(None)
        tts_queue.join()
    if tts_thread is not None:
        tts_thread.join(timeout=5)

    try:
        from bsl_api import close_session
        asyncio.run(close_session())
    except Exception:
        pass

    print("Session ended.")


if __name__ == "__main__":
    main()
