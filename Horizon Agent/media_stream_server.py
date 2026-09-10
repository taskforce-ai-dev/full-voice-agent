"""
media_stream_server.py — Multilingual Tanya server using Twilio Media Streams.

Run this INSTEAD of server.py to get full trilingual TTS support
(English, Sinhala, Tamil) via ElevenLabs eleven_multilingual_v2 with cloned voice.

Architecture:
  Twilio inbound audio (mulaw 8 kHz)
    → Google Cloud Speech-to-Text  (streaming, background thread)
    → Endpointing                  (1.5 s silence timeout)
    → KB retrieval + Claude        (streaming, async, tool use)
    → ElevenLabs TTS               (REST streaming, ulaw_8000 output)
    → Twilio outbound audio        (mulaw 8 kHz, no conversion needed)

Usage:
    python media_stream_server.py

Required env vars:
    ANTHROPIC_API_KEY
    ELEVENLABS_API_KEY
    ELEVENLABS_VOICE_ID
    GOOGLE_APPLICATION_CREDENTIALS  (path to GCP service-account JSON)

Optional env vars:
    ELEVENLABS_MODEL   (default: eleven_multilingual_v2)
    PORT               (default: 8000)
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
from contextlib import asynccontextmanager
from datetime import date
from typing import Any

import xml.sax.saxutils
import httpx
from anthropic import AsyncAnthropic, NOT_GIVEN
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import Response

from knowledge_base import retrieve_context, initialize_kb, prewarm
from tools import get_tools, execute_tool
from booking_api import close_session, is_configured

# ╔═══════════════════════════════════════════════════════════════════════════╗
# ║  Logging                                                                ║
# ╚═══════════════════════════════════════════════════════════════════════════╝
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger(__name__)

# ╔═══════════════════════════════════════════════════════════════════════════╗
# ║  Environment / Config                                                   ║
# ╚═══════════════════════════════════════════════════════════════════════════╝
ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")
ELEVENLABS_API_KEY: str = os.getenv("ELEVENLABS_API_KEY", "")
ELEVENLABS_VOICE_ID: str = os.getenv("ELEVENLABS_VOICE_ID", "")
ELEVENLABS_MODEL: str = os.getenv("ELEVENLABS_MODEL", "eleven_turbo_v2_5")
AZURE_SPEECH_KEY: str = os.getenv("AZURE_SPEECH_KEY", "")
AZURE_SPEECH_REGION: str = os.getenv("AZURE_SPEECH_REGION", "southeastasia")
PORT: int = int(os.getenv("PORT", "8000"))

# ── Claude ────────────────────────────────────────────────────────────────
MODEL: str = "claude-sonnet-4-6"
MAX_TOKENS: int = 300
MAX_HISTORY: int = 20
MAX_TOOL_ROUNDS: int = 5

# ── ElevenLabs TTS ────────────────────────────────────────────────────────
ELEVENLABS_TTS_URL = "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream"

# ── Azure TTS ─────────────────────────────────────────────────────────────
AZURE_TTS_URL = "https://{region}.tts.speech.microsoft.com/cognitiveservices/v1"

# ── Google STT ────────────────────────────────────────────────────────────
STT_LANGUAGE: str = "en-US"
STT_ALT_LANGUAGES: list[str] = ["si-LK", "ta-IN"]
ENDPOINTING_SILENCE: float = 1.5  # seconds after last final result

# ── Filler messages while tools execute ───────────────────────────────────
TOOL_FILLERS: dict[str, str] = {
    "check_availability": "Let me check availability for those dates.",
    "create_booking":     "I'm creating your reservation now.",
    "retrieve_booking":   "Let me look up that booking for you.",
    "cancel_booking":     "Let me process that cancellation.",
}
DEFAULT_FILLER: str = "Let me check that for you."

WELCOME_GREETING = "Welcome to Hatton Hills! How may I help you today?"

# ── Pronunciation rewriting (romanized Sinhala → phonetic hints) ──────────
# ElevenLabs doesn't know these are Sinhala — rewrite to phonetic spelling
# so TTS pronounces them correctly. Add entries as needed.
SINHALA_PRONUNCIATION: dict[str, str] = {
    # Greetings / politeness
    "Ayubowan":     "ah yoo boh wun",
    "Sthuthi":      "stoo tee",
    "Isthuthi":     "is too tee",
    "Bohoma":       "boh hoh mah",
    "Suba":         "soo bah",
    "Subapathum":   "soo bah pah toom",
    # Questions / conversation
    "Kohomada":     "koh hoh mah dah",
    "Mokakda":      "moh kahk dah",
    "Kiyada":       "ki yah dah",
    "Keeyak":       "kee yahk",
    "Hedenna":      "heh den nah",
    "Kiyanna":      "ki yan nah",
    "Puluwan":      "poo loo wun",
    "Wenakam":      "weh nah kahm",
    "Denna":        "den nah",
    # Pronouns / common words
    "Obata":        "oh bah tah",
    "Oyata":        "oh yah tah",
    "Oya":          "oh yah",
    "Mama":         "mah mah",
    "Api":          "ah pee",
    "Eka":          "eh kah",
    "Mekata":       "meh kah tah",
    "Uda":          "oo dah",
    "Udawwa":       "oo daw wah",
    # Booking / hotel words
    "Kamarai":      "kah mah rye",
    "Kaamara":      "kah mah rah",
    "Dinak":        "di nahk",
    "Raathri":      "rah tree",
    "Hari":         "hah ri",
    "Karunakara":   "kah roo nah kah rah",
    "Podi":         "poh dee",
    "Loku":         "loh koo",
    "Deka":         "deh kah",
    "Thuna":        "too nah",
    "Hathara":      "hah tah rah",
    "Passa":        "pahs sah",
    # Place name
    "the hill country":   "beh li hoo loh yah",
}

# Pre-compile pronunciation regex for speed
_PRONUNCIATION_PATTERN = re.compile(
    "|".join(rf"\b{re.escape(w)}\b" for w in SINHALA_PRONUNCIATION),
    re.IGNORECASE,
)


def _rewrite_pronunciation(text: str) -> str:
    """Replace romanized Sinhala words with phonetic hints for TTS."""
    def _replace(m: re.Match) -> str:
        word = m.group(0)
        # Preserve original case pattern approximately
        for key, val in SINHALA_PRONUNCIATION.items():
            if key.lower() == word.lower():
                return val
        return word
    return _PRONUNCIATION_PATTERN.sub(_replace, text)


# ── Sentence boundary detection for streaming TTS ─────────────────────────
_SENTENCE_END = re.compile(r'(?<=[.!?])\s+')


def _extract_sentences(buffer: str) -> tuple[list[str], str]:
    """Split buffer into complete sentences and leftover text.

    Returns (complete_sentences, remaining_buffer).
    """
    parts = _SENTENCE_END.split(buffer)
    if len(parts) <= 1:
        return [], buffer
    complete = [p.strip() for p in parts[:-1] if p.strip()]
    remaining = parts[-1]
    return complete, remaining


# ── TTS model selection based on script ──────────────────────────────────
# eleven_turbo_v2_5 supports Tamil and English but NOT Sinhala.
# eleven_multilingual_v2 supports Sinhala but is slower.
_NON_ENGLISH_SCRIPT_RE = re.compile(r"[\u0D80-\u0DFF\u0B80-\u0BFF]")
# \u0D80-\u0DFF = Sinhala Unicode block
# \u0B80-\u0BFF = Tamil Unicode block

ELEVENLABS_MODEL_MULTILINGUAL = "eleven_multilingual_v2"
ELEVENLABS_MODEL_TURBO = "eleven_turbo_v2_5"


def _select_tts_model(text: str) -> str:
    """Return the appropriate ElevenLabs model for the given text.

    Sinhala or Tamil Unicode → multilingual_v2 (proper language support).
    English → turbo_v2_5 (faster).
    """
    if _NON_ENGLISH_SCRIPT_RE.search(text):
        return ELEVENLABS_MODEL_MULTILINGUAL
    return ELEVENLABS_MODEL_TURBO


# ── Azure TTS voice routing ────────────────────────────────────────────────
_SINHALA_RE = re.compile(r"[\u0D80-\u0DFF]")
_TAMIL_RE = re.compile(r"[\u0B80-\u0BFF]")

AZURE_VOICES: dict[str, tuple[str, str]] = {
    "si": ("si-LK", "si-LK-SameeraNeural"),
    "ta": ("ta-LK", "ta-LK-SaranyaNeural"),
}


def _get_azure_voice(text: str) -> tuple[str, str] | None:
    """Return (lang_code, voice_name) for Azure TTS if text is Sinhala or Tamil.

    Returns None if text is English (ElevenLabs should handle it instead).
    """
    if _SINHALA_RE.search(text):
        return AZURE_VOICES["si"]
    if _TAMIL_RE.search(text):
        return AZURE_VOICES["ta"]
    return None


# ── Google Cloud Speech (optional import) ─────────────────────────────────
try:
    from google.cloud import speech_v1 as google_speech
    GOOGLE_STT_AVAILABLE = True
except ImportError:
    google_speech = None  # type: ignore[assignment]
    GOOGLE_STT_AVAILABLE = False
    logger.warning("google-cloud-speech not installed — STT will not work")


# ╔═══════════════════════════════════════════════════════════════════════════╗
# ║  System Prompt                                                          ║
# ╚═══════════════════════════════════════════════════════════════════════════╝

def _build_system_prompt() -> str:
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
        "- If the guest speaks Sinhala, respond entirely in native Sinhala script "
        "(e.g. 'ආයුබෝවන්! ඔබට කොහොමද උදව් කරන්නේ?'). Always use Unicode Sinhala — "
        "never romanized Latin.\n"
        "- If the guest speaks Tamil, respond entirely in native Tamil script "
        "(e.g. 'வணக்கம்! நான் உங்களுக்கு எப்படி உதவலாம்?'). Always use Unicode Tamil — "
        "never romanized Latin.\n\n"

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


# ╔═══════════════════════════════════════════════════════════════════════════╗
# ║  Anthropic Client (singleton)                                           ║
# ╚═══════════════════════════════════════════════════════════════════════════╝
_anthropic_client: AsyncAnthropic | None = None


def _get_client() -> AsyncAnthropic:
    global _anthropic_client
    if _anthropic_client is None:
        if not ANTHROPIC_API_KEY:
            raise RuntimeError("ANTHROPIC_API_KEY is not set")
        _anthropic_client = AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
        logger.info("Initialized AsyncAnthropic client")
    return _anthropic_client


# ╔═══════════════════════════════════════════════════════════════════════════╗
# ║  History Trimming                                                       ║
# ╚═══════════════════════════════════════════════════════════════════════════╝

def _trim_history(history: list[dict], max_msgs: int = MAX_HISTORY) -> list[dict]:
    """Cap conversation history; ensure first message is role=user."""
    if len(history) <= max_msgs:
        return history
    trimmed = history[-max_msgs:]
    while trimmed and trimmed[0]["role"] != "user":
        trimmed.pop(0)
    return trimmed


# ╔═══════════════════════════════════════════════════════════════════════════╗
# ║  Google STT — streaming (background thread)                             ║
# ╚═══════════════════════════════════════════════════════════════════════════╝

class GoogleSTTStream:
    """Streams mulaw 8 kHz audio to Google Cloud Speech-to-Text.

    Runs the synchronous gRPC streaming_recognize in a daemon thread.
    Fires *on_final_result(transcript)* from that thread.
    Auto-restarts when the ~5-minute gRPC streaming limit is reached.
    """

    def __init__(self, on_final_result: Any):
        self._on_final = on_final_result
        self._audio_q: queue.Queue[bytes | None] = queue.Queue()
        self._running = False
        self._thread: threading.Thread | None = None

    def start(self):
        if not GOOGLE_STT_AVAILABLE:
            logger.error("Cannot start STT — google-cloud-speech not installed")
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        logger.info("Google STT stream started")

    def stop(self):
        self._running = False
        self._audio_q.put(None)
        if self._thread:
            self._thread.join(timeout=5)

    def feed(self, mulaw_bytes: bytes):
        """Feed raw mulaw 8 kHz audio from Twilio."""
        self._audio_q.put(mulaw_bytes)

    # ── internal ──────────────────────────────────────────────────────────

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
                logger.warning("STT stream ended (%s), restarting…", exc)

    def _run_one_stream(self):
        client = google_speech.SpeechClient()
        config = google_speech.StreamingRecognitionConfig(
            config=google_speech.RecognitionConfig(
                encoding=google_speech.RecognitionConfig.AudioEncoding.MULAW,
                sample_rate_hertz=8000,
                language_code=STT_LANGUAGE,
                alternative_language_codes=STT_ALT_LANGUAGES,
                enable_automatic_punctuation=True,
            ),
            interim_results=False,
        )

        def request_gen():
            for chunk in self._audio_generator():
                yield google_speech.StreamingRecognizeRequest(audio_content=chunk)

        responses = client.streaming_recognize(config=config, requests=request_gen())
        for response in responses:
            if not self._running:
                break
            for result in response.results:
                if result.is_final and result.alternatives:
                    transcript = result.alternatives[0].transcript.strip()
                    if transcript:
                        self._on_final(transcript)


# ╔═══════════════════════════════════════════════════════════════════════════╗
# ║  Media Stream Session                                                   ║
# ╚═══════════════════════════════════════════════════════════════════════════╝

class MediaStreamSession:
    """Manages a single Twilio Media Streams phone call.

    Pipeline per turn:
      Google STT → endpointing → KB retrieval → Claude (streaming + tools)
      → ElevenLabs TTS → mulaw audio → Twilio
    """

    def __init__(
        self,
        websocket: WebSocket,
        client: AsyncAnthropic,
        system_prompt: str,
        tools: list[dict],
    ):
        self.ws = websocket
        self.client = client
        self.system_prompt = system_prompt
        self.tools = tools

        self.stream_sid: str | None = None
        self.call_sid: str = "unknown"
        self.history: list[dict] = []

        self._loop: asyncio.AbstractEventLoop | None = None
        self._is_speaking = False
        self._speak_lock = asyncio.Lock()
        self._ws_lock = asyncio.Lock()
        self._speak_generation: int = 0  # Incremented on barge-in to cancel queued TTS

        # Transcript accumulation + endpointing
        self._pending_transcript = ""
        self._endpointing_handle: asyncio.TimerHandle | None = None

        # STT
        self._stt: GoogleSTTStream | None = None

    # ── Main event loop ───────────────────────────────────────────────────

    async def run(self):
        self._loop = asyncio.get_running_loop()
        await self.ws.accept()
        logger.info("Media stream WebSocket accepted")

        self._stt = GoogleSTTStream(on_final_result=self._on_stt_result)
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
                    logger.info("Stream started — Call: %s, Stream: %s",
                                self.call_sid, self.stream_sid)
                    asyncio.ensure_future(self._speak(WELCOME_GREETING))

                elif event == "media":
                    audio = base64.b64decode(msg["media"]["payload"])
                    if self._stt:
                        self._stt.feed(audio)

                elif event == "mark":
                    if msg.get("mark", {}).get("name") == "tts_done":
                        self._is_speaking = False

                elif event == "stop":
                    logger.info("Stream stopped — Call: %s", self.call_sid)
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
            logger.info("Session ended — Call: %s, history: %d msgs",
                        self.call_sid, len(self.history))

    # ── STT callback (from background thread) ─────────────────────────────

    def _on_stt_result(self, transcript: str):
        if self._loop is None:
            return
        if self._is_speaking:
            # Tanya is speaking — this is likely telephony echo, not the user.
            # Trigger barge-in (clears audio buffer) but do NOT accumulate
            # the transcript.  Genuine user speech will produce more STT
            # results after _is_speaking becomes False.
            asyncio.run_coroutine_threadsafe(self._handle_bargein(), self._loop)
            return
        asyncio.run_coroutine_threadsafe(
            self._accumulate_transcript(transcript), self._loop,
        )

    async def _handle_bargein(self):
        logger.info("Barge-in detected [%s]", self.call_sid)
        self._is_speaking = False
        self._speak_generation += 1  # Cancel all queued TTS chunks
        # Clear any transcript accumulated during speaking — it's echo, not user speech
        self._pending_transcript = ""
        if self._endpointing_handle:
            self._endpointing_handle.cancel()
            self._endpointing_handle = None
        async with self._ws_lock:
            await self.ws.send_text(json.dumps({
                "event": "clear",
                "streamSid": self.stream_sid,
            }))

    # ── Endpointing ──────────────────────────────────────────────────────

    async def _accumulate_transcript(self, text: str):
        if self._pending_transcript:
            self._pending_transcript += " " + text
        else:
            self._pending_transcript = text

        if self._endpointing_handle:
            self._endpointing_handle.cancel()

        self._endpointing_handle = self._loop.call_later(
            ENDPOINTING_SILENCE,
            lambda: asyncio.ensure_future(self._flush_transcript()),
        )

    async def _flush_transcript(self):
        transcript = self._pending_transcript.strip()
        self._pending_transcript = ""
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
            response_text = await self._run_claude()
            if response_text:
                logger.info("Agent [%s]: %s", self.call_sid, response_text[:200])
            # TTS is handled inside _run_claude via sentence streaming — do NOT call _speak here
        except Exception:
            logger.exception("Claude error [%s]", self.call_sid)
            await self._speak(
                "I'm sorry, I'm having a technical issue. "
                "Could you please try again?"
            )

    # ── Claude streaming with tool use + sentence-level TTS ────────────────

    async def _run_claude(self) -> str:
        full_text = ""

        for round_idx in range(MAX_TOOL_ROUNDS):
            logger.info("Claude round %d [%s]", round_idx + 1, self.call_sid)

            text_content = ""
            tool_use_blocks: list[dict[str, Any]] = []
            cur_tool_name: str | None = None
            cur_tool_id: str | None = None
            tool_json = ""

            # Sentence-level streaming state
            sentence_buffer = ""
            spoken_text = ""      # All text already sent to TTS
            tts_tasks: list[asyncio.Task] = []
            has_tool_use = False
            gen = self._speak_generation  # Snapshot for barge-in cancellation

            async with self.client.messages.stream(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                system=self.system_prompt,
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
                            # Stream sentences to TTS (only if no tool use yet)
                            if not has_tool_use:
                                sentence_buffer += event.delta.text
                                sentences, sentence_buffer = _extract_sentences(
                                    sentence_buffer
                                )
                                for s in sentences:
                                    task = asyncio.create_task(
                                        self._speak(s, previous_text=spoken_text,
                                                    generation=gen)
                                    )
                                    tts_tasks.append(task)
                                    spoken_text += (" " + s) if spoken_text else s

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

            # ── Handle tool calls ─────────────────────────────────────────
            if tool_use_blocks:
                logger.info("Tools [%s]: %s", self.call_sid,
                            [t["name"] for t in tool_use_blocks])

                # Wait for any already-queued sentence TTS to finish
                if tts_tasks:
                    await asyncio.gather(*tts_tasks)

                filler = TOOL_FILLERS.get(tool_use_blocks[0]["name"], DEFAULT_FILLER)
                await self._speak(filler)

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
                    logger.info("Tool '%s' → %s", tb["name"], result_str[:200])

                self.history.append({"role": "user", "content": tool_results})
                continue

            # ── No tools → flush remaining sentence buffer ────────────────
            remaining = sentence_buffer.strip()
            if remaining:
                task = asyncio.create_task(
                    self._speak(remaining, previous_text=spoken_text, generation=gen)
                )
                tts_tasks.append(task)

            if tts_tasks:
                await asyncio.gather(*tts_tasks)

            if text_content:
                self.history.append({"role": "assistant", "content": text_content})
            return full_text

        logger.warning("Exhausted %d tool rounds [%s]", MAX_TOOL_ROUNDS, self.call_sid)
        return full_text

    # ── TTS routing → Twilio audio ────────────────────────────────────────

    async def _speak(
        self,
        text: str,
        previous_text: str = "",
        generation: int = -1,
    ):
        """Route text to Azure TTS (Sinhala/Tamil) or ElevenLabs (English).

        Args:
            text: The sentence/chunk to speak.
            previous_text: Text already spoken this turn — passed to ElevenLabs
                so prosody stays natural across sentence chunks.
            generation: Speak-generation snapshot.  If barge-in increments
                ``_speak_generation`` before this chunk starts, it is skipped.
                Pass -1 to bypass the check (filler messages, welcome greeting).
        """
        async with self._speak_lock:
            # ── Generation check (barge-in cancellation) ──────────────────
            if generation >= 0 and generation != self._speak_generation:
                return

            azure_voice = _get_azure_voice(text)
            if azure_voice:
                lang_code, voice_name = azure_voice
                await self._tts_azure(text, lang_code, voice_name)
            else:
                await self._tts_elevenlabs(text, previous_text)

    # ── ElevenLabs TTS (English) ──────────────────────────────────────────

    async def _tts_elevenlabs(self, text: str, previous_text: str = ""):
        """Stream text via ElevenLabs and send mulaw audio to Twilio.

        Must only be called from within _speak (lock already held).
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
        model = _select_tts_model(text)
        logger.debug("ElevenLabs TTS model for chunk: %s", model)
        payload: dict[str, Any] = {
            "text": text,
            "model_id": model,
            "voice_settings": {
                "stability": 0.5,
                "similarity_boost": 0.75,
                "style": 0.0,
                "use_speaker_boost": True,
            },
        }
        # Prosody context — lets ElevenLabs maintain intonation across
        # sentence chunks without the "reset" that happens otherwise.
        if previous_text:
            payload["previous_text"] = previous_text[-1000:]  # cap length

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

    # ── Azure TTS (Sinhala / Tamil) ───────────────────────────────────────

    async def _tts_azure(self, text: str, lang_code: str, voice_name: str):
        """Stream text via Azure Cognitive Services TTS and send mulaw audio to Twilio.

        Must only be called from within _speak (lock already held).
        """
        if not AZURE_SPEECH_KEY:
            logger.warning("AZURE_SPEECH_KEY not configured — skipping Azure TTS")
            return

        self._is_speaking = True
        url = AZURE_TTS_URL.format(region=AZURE_SPEECH_REGION)
        headers = {
            "Ocp-Apim-Subscription-Key": AZURE_SPEECH_KEY,
            "Content-Type": "application/ssml+xml",
            "X-Microsoft-OutputFormat": "raw-8khz-8bit-mono-mulaw",
        }
        escaped_text = xml.sax.saxutils.escape(text)
        ssml = (
            f"<speak version='1.0' xml:lang='{lang_code}' "
            f"xmlns='http://www.w3.org/2001/10/synthesis'>"
            f"<voice name='{voice_name}'>{escaped_text}</voice>"
            f"</speak>"
        )
        logger.debug("Azure TTS voice: %s, lang: %s", voice_name, lang_code)

        try:
            async with httpx.AsyncClient() as http:
                async with http.stream(
                    "POST", url, content=ssml.encode("utf-8"),
                    headers=headers, timeout=15.0,
                ) as resp:
                    if resp.status_code != 200:
                        body = await resp.aread()
                        logger.error("Azure TTS %d: %s",
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
                logger.info("Azure TTS interrupted by barge-in [%s]", self.call_sid)

        except httpx.TimeoutException:
            logger.error("Azure TTS timeout for: %s", text[:80])
            self._is_speaking = False
        except Exception:
            logger.exception("Azure TTS failed for: %s", text[:80])
            self._is_speaking = False


# ╔═══════════════════════════════════════════════════════════════════════════╗
# ║  FastAPI Application                                                    ║
# ╚═══════════════════════════════════════════════════════════════════════════╝

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting Media Streams server…")

    logger.info("Initializing knowledge base…")
    kb_ok = initialize_kb("knowledge_docs")
    if kb_ok:
        logger.info("Knowledge base ready.")
    else:
        logger.warning("Knowledge base init failed — continuing without KB.")
    prewarm()

    try:
        _get_client()
    except RuntimeError as exc:
        logger.error("Cannot create Anthropic client: %s", exc)

    if not ELEVENLABS_API_KEY or not ELEVENLABS_VOICE_ID:
        logger.warning("ELEVENLABS_API_KEY or ELEVENLABS_VOICE_ID not set — "
                       "TTS will not work.")

    if not GOOGLE_STT_AVAILABLE:
        logger.warning("google-cloud-speech not installed — STT will not work.")

    logger.info("Server ready. eZee configured: %s", is_configured())
    yield

    logger.info("Shutting down…")
    await close_session()
    logger.info("Shutdown complete.")


app = FastAPI(
    title="Tanya — Media Streams (Multilingual)",
    version="2.0.0",
    lifespan=lifespan,
)


# ── Health ────────────────────────────────────────────────────────────────

@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "mode": "media_streams",
        "ezee_configured": is_configured(),
        "kb_loaded": os.path.isdir("knowledge_docs"),
        "stt_available": GOOGLE_STT_AVAILABLE,
        "tts_configured": bool(ELEVENLABS_API_KEY and ELEVENLABS_VOICE_ID),
        "model": MODEL,
    }


# ── Twilio webhook — returns Media Streams TwiML ─────────────────────────

@app.post("/voice/incoming")
async def voice_incoming(request: Request) -> Response:
    """Return TwiML that connects the caller via Media Streams."""
    host = request.headers.get("host", request.url.hostname or "localhost")
    twiml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<Response>\n"
        "  <Connect>\n"
        f'    <Stream url="wss://{host}/ws/media-stream" />\n'
        "  </Connect>\n"
        "</Response>"
    )
    logger.info("Incoming call from %s — returning Media Streams TwiML",
                request.headers.get("x-forwarded-for", "unknown"))
    return Response(content=twiml, media_type="application/xml")


# ── WebSocket — Media Streams handler ─────────────────────────────────────

@app.websocket("/ws/media-stream")
async def ws_media_stream(websocket: WebSocket):
    try:
        client = _get_client()
    except RuntimeError:
        logger.error("Anthropic client unavailable — closing WebSocket")
        await websocket.accept()
        await websocket.close(code=1011, reason="Server configuration error")
        return

    session = MediaStreamSession(
        websocket=websocket,
        client=client,
        system_prompt=_build_system_prompt(),
        tools=get_tools(),
    )
    await session.run()


# ╔═══════════════════════════════════════════════════════════════════════════╗
# ║  Entry Point                                                            ║
# ╚═══════════════════════════════════════════════════════════════════════════╝

if __name__ == "__main__":
    import uvicorn
    logger.info("Starting Media Streams server on port %d", PORT)
    uvicorn.run("media_stream_server:app", host="0.0.0.0", port=PORT, log_level="info")
