"""
test_voice.py -- Local demo CLI for Hatton Hills Voice Agent (Tanya).

Mirrors the production server.py flow exactly:
  - Azure TTS (same voices as Twilio ConversationRelay production)
  - Auto language detection from response text (no DTMF, no voice menu)
  - Knowledge base RAG retrieval
  - Claude tool use (eZee PMS) with filler speech
  - Sentence-by-sentence TTS on background thread

The only difference from production: you type instead of speak (no STT).

Usage:
    python test_voice.py

Environment variables:
    ANTHROPIC_API_KEY   -- Required.
    AZURE_SPEECH_KEY    -- Required for voice. Text-only mode if absent.
    AZURE_SPEECH_REGION -- Azure region (default: southeastasia).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import queue
import re
import sys
import threading
import unicodedata
from datetime import date
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# Third-party imports
# ---------------------------------------------------------------------------
from dotenv import load_dotenv

load_dotenv()

import anthropic

# Azure Speech SDK -- optional
try:
    import azure.cognitiveservices.speech as speechsdk
    AZURE_TTS_AVAILABLE = True
except ImportError:
    speechsdk = None  # type: ignore[assignment]
    AZURE_TTS_AVAILABLE = False

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
logger = logging.getLogger("test_voice")

# Quieten noisy libraries
logging.getLogger("chromadb").setLevel(logging.WARNING)
logging.getLogger("sentence_transformers").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
MODEL = "claude-sonnet-4-6"
MAX_TOOL_ROUNDS = 5

# Azure voice map -- same voices used in production ConversationRelay
AZURE_VOICES = {
    "si": "si-LK-SameeraNeural",     # Sinhala
    "ta": "ta-LK-SaranyaNeural",     # Tamil (Sri Lankan)
    "en": "en-US-JennyNeural",       # English
    "hi": "hi-IN-SwaraNeural",       # Hindi
}
DEFAULT_VOICE = "en-US-JennyNeural"

# Filler messages (matches server.py)
TOOL_FILLERS: Dict[str, str] = {
    "check_availability": "Let me check availability for those dates.",
    "create_booking": "I'm creating your reservation now.",
    "retrieve_booking": "Let me look up that booking for you.",
    "cancel_booking": "Let me process that cancellation.",
}
DEFAULT_FILLER = "Let me check that for you."

SENTENCE_END = re.compile(r"[.!?។]\s*")

# ---------------------------------------------------------------------------
# System prompt (matches server.py exactly)
# ---------------------------------------------------------------------------
def build_system_prompt() -> str:
    today = date.today().isoformat()
    return (
        f"You are Tanya, the warm and gracious reservations voice agent for "
        f"Hatton Hills, Sri Lankan hill country.\n"
        f"Today's date is {today}.\n\n"

        "LANGUAGE RULES:\n"
        "- You speak fluently in English, Sinhala, and Tamil.\n"
        "- Detect the guest's preferred language from their first response "
        "and switch immediately and fully into that language.\n"
        "- If they mix languages, match their style naturally.\n"
        "- If the guest speaks Sinhala, respond entirely in Sinhala BUT "
        "always write Sinhala in romanized Latin script (e.g. 'Ayubowan! "
        "Obata kohomada udaw karanne?' NOT 'ආයුබෝවන්! ඔබට කොහොමද උදව් කරන්නේ?'). "
        "Never use Sinhala Unicode script. This is critical for clear TTS pronunciation.\n"
        "- If the guest speaks Tamil, respond entirely in Tamil.\n\n"

        "VOICE RULES (you are speaking on a phone call, not writing text):\n"
        "- Keep every response to one or two short sentences.\n"
        "- Never use markdown, bullet points, numbered lists, asterisks, or URLs.\n"
        "- Use natural spoken language. Say numbers as words.\n"
        "- Do not use abbreviations. Say 'rupees' not 'LKR'.\n"
        "- Never read out full rate lists. Mention only the relevant room.\n"
        "- Ask only one question at a time.\n\n"

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
        "candlelit dinner package.\n\n"

        "GREETING (use on first interaction):\n"
        "English: Good morning! Thank you for calling Hatton Hills. "
        "I'm Tanya, lovely to have you with us! How may I help you today?\n"
        "Sinhala: Ayubowan! Hatton Hills walata obawa sadarayen piligannawa. "
        "Mama Tanya, obata kohomada udaw karanne?\n"
        "Tamil: வணக்கம்! Hatton Hills-க்கு அழைத்தமைக்கு நன்றி. "
        "நான் Tanya, உங்களுக்கு எப்படி உதவலாம்?\n"
    )

# ---------------------------------------------------------------------------
# Language detection from text (Unicode script analysis)
# ---------------------------------------------------------------------------
def detect_language(text: str) -> str:
    """Detect language from Unicode script in the text.
    Returns 'si', 'ta', 'hi', or 'en'."""
    sinhala = 0
    tamil = 0
    devanagari = 0
    latin = 0

    for ch in text:
        try:
            name = unicodedata.name(ch, "")
        except ValueError:
            continue
        if "SINHALA" in name:
            sinhala += 1
        elif "TAMIL" in name:
            tamil += 1
        elif "DEVANAGARI" in name:
            devanagari += 1
        elif "LATIN" in name:
            latin += 1

    # Pick the dominant script
    counts = {"si": sinhala, "ta": tamil, "hi": devanagari, "en": latin}
    dominant = max(counts, key=counts.get)  # type: ignore

    # If no non-Latin script detected, default to English
    if counts[dominant] == 0:
        return "en"
    return dominant

# ---------------------------------------------------------------------------
# Azure TTS: synthesize and play
# ---------------------------------------------------------------------------
def azure_speak(
    speech_config: "speechsdk.SpeechConfig",
    text: str,
    voice_name: str,
) -> None:
    """Synthesize text with Azure TTS and play through speakers."""
    speech_config.speech_synthesis_voice_name = voice_name
    synthesizer = speechsdk.SpeechSynthesizer(speech_config=speech_config)

    result = synthesizer.speak_text_async(text).get()

    if result.reason == speechsdk.ResultReason.SynthesizingAudioCompleted:
        logger.info("TTS: played '%s' with %s", text[:50], voice_name)
    elif result.reason == speechsdk.ResultReason.Canceled:
        cancellation = result.cancellation_details
        logger.error("TTS canceled: %s — %s", cancellation.reason, cancellation.error_details)


# ---------------------------------------------------------------------------
# TTS background worker (Azure)
# ---------------------------------------------------------------------------
def tts_worker(
    tts_queue: queue.Queue,
    speech_config: "speechsdk.SpeechConfig",
) -> None:
    """Background thread that synthesizes and plays sentences from the queue."""
    while True:
        item = tts_queue.get()
        if item is None:
            tts_queue.task_done()
            break

        text, voice_name = item
        text = text.strip()
        if not text:
            tts_queue.task_done()
            continue

        try:
            azure_speak(speech_config, text, voice_name)
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
                result = pool.submit(asyncio.run, execute_tool(tool_name, tool_input)).result()
            return result
        return loop.run_until_complete(execute_tool(tool_name, tool_input))
    except RuntimeError:
        return asyncio.run(execute_tool(tool_name, tool_input))

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
    # Fallback: if splitter returned nothing, send full text as one chunk
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
    # Azure TTS setup (no voice selection menu — auto-detected)
    # ------------------------------------------------------------------
    tts_enabled = False
    speech_config: Any = None
    tts_queue: Optional[queue.Queue] = None
    tts_thread: Optional[threading.Thread] = None

    azure_key = os.getenv("AZURE_SPEECH_KEY", "")
    azure_region = os.getenv("AZURE_SPEECH_REGION", "southeastasia")

    if AZURE_TTS_AVAILABLE and azure_key:
        try:
            speech_config = speechsdk.SpeechConfig(
                subscription=azure_key,
                region=azure_region,
            )
            tts_enabled = True
            logger.info("Azure TTS ready (region: %s)", azure_region)

            # Start the TTS background worker
            tts_queue = queue.Queue()
            tts_thread = threading.Thread(
                target=tts_worker,
                args=(tts_queue, speech_config),
                daemon=True,
            )
            tts_thread.start()

        except Exception as exc:
            logger.warning("Azure TTS init failed (%s). Text-only mode.", exc)
    else:
        if not AZURE_TTS_AVAILABLE:
            print("WARNING: azure-cognitiveservices-speech not installed. Text-only mode.")
            print("         Install: pip install azure-cognitiveservices-speech\n")
        elif not azure_key:
            print("WARNING: AZURE_SPEECH_KEY not set in .env. Text-only mode.")
            print("         Get it from Azure Portal > Speech resource > Keys\n")

    # ------------------------------------------------------------------
    # Anthropic client and conversation state
    # ------------------------------------------------------------------
    client = anthropic.Anthropic(api_key=api_key)
    system_prompt = build_system_prompt()
    tools = get_tools()
    messages: List[Dict[str, Any]] = []

    print("=" * 60)
    print("  Hatton Hills Voice Agent (Tanya) -- Demo CLI")
    if tts_enabled:
        print("  Azure TTS: ON (auto language detection)")
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
                f"[Hotel Information]\n{kb_context}\n\n"
                f"[Guest Message]\n{user_input}"
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
                print(f"Tanya: {full_text}")

                # -- TTS with auto language detection --
                if tts_enabled and tts_queue is not None and full_text:
                    lang = detect_language(full_text)
                    voice_name = AZURE_VOICES.get(lang, DEFAULT_VOICE)
                    logger.info("Detected language: %s → voice: %s", lang, voice_name)

                    sentences = split_sentences(full_text)
                    for sentence in sentences:
                        tts_queue.put((sentence, voice_name))
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

                # Speak filler while tool executes (like production)
                if tts_enabled and tts_queue is not None:
                    filler = TOOL_FILLERS.get(tool_name, DEFAULT_FILLER)
                    filler_lang = detect_language(full_text) if 'full_text' in dir() else "en"
                    filler_voice = AZURE_VOICES.get(filler_lang, DEFAULT_VOICE)
                    tts_queue.put((filler, filler_voice))

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
            print("Tanya: I'm having trouble completing that request. Could you try again?")

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------
    if tts_queue is not None:
        tts_queue.put(None)
        tts_queue.join()
    if tts_thread is not None:
        tts_thread.join(timeout=5)

    try:
        from ezee_api import close_session
        asyncio.run(close_session())
    except Exception:
        pass

    print("Session ended.")


if __name__ == "__main__":
    main()
