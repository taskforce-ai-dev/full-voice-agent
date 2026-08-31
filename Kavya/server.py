"""
server.py — Main FastAPI server for Hatton Hills Voice Agent (Kavya).

Hatton Hills is a SINGLE property: a luxury boutique eco retreat in an
eight-acre private forest in Sri Lanka's central hill country, with exactly five
room types. The two-property disambiguation machinery was collapsed to
single-property mode on 2026-07-30 — see yanolja_service.resolve_property.

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
import audioop
import base64
import binascii
import contextlib
import enum
import json
import logging
import math
import os
import inspect
import difflib
import secrets
from collections import OrderedDict
from contextvars import ContextVar
from dataclasses import dataclass, field

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
    sentry_sdk.set_tag("agent", "kavya")

import queue
import re
import threading
import time
import wave
import xml.sax.saxutils
from contextlib import asynccontextmanager
from datetime import date, datetime
from html import escape as html_escape
from typing import Any, AsyncIterator, Awaitable, Callable, Mapping
from urllib.parse import quote as url_quote

import httpx
from anthropic import AsyncAnthropic, NOT_GIVEN
from openai import AsyncOpenAI
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import Response
from twilio.rest import Client as TwilioRestClient

from knowledge_base import retrieve_context, initialize_kb, prewarm, reload_kb_from_content
from tools import (
    get_tools,
    get_tools_openai,
    get_tools_gemini,
    get_handover_tools,
    execute_tool,
    smartpbx_transfer_context,
    ROOM_TYPES_BY_PROPERTY,
    PROPERTY_HATTON,
)
from booking_api import close_session, is_configured
# Imported for DEMO_RATES_ENABLED so the system prompt and the tool results
# agree on whether rates may be quoted. Already loaded transitively via
# booking_api; the explicit import keeps the single source of truth visible.
import yanolja_service
from rate_catalog import (
    RateResolution,
    classify_room_rate_intent,
    is_room_rate_follow_up,
    recognize_residency,
    recognize_selected_room,
    resolve_rate,
)
from post_call import (
    UNCONFIRMED_TRANSCRIPT_LABEL,
    UNCONFIRMED_TRANSCRIPT_ROLE,
    process_post_call_data,
)
from handover import handover_context, send_handover_notification
from english_voice_profile import load_kavya_english_voice_profile
from smartpbx_diagnostics import DiagnosticFailureClass, DiagnosticOutcome, DiagnosticStage
from smartpbx_dtmf import DtmfCollector

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


# Retained speech never became a normal guest turn.  It is still useful call
# evidence, but downstream must not mistake it for an answered statement.
RETAINED_SPEECH_ROLE = UNCONFIRMED_TRANSCRIPT_ROLE
RETAINED_SPEECH_MAX_CHARS = 1000
STT_CALLBACK_DRAIN_TIMEOUT_SECONDS = 2.0
_RETAINED_SPEECH_PROVENANCE = frozenset({"final", "interim"})
_RETAINED_SPEECH_REASONS = frozenset(
    {"barge_in", "transfer", "transfer_flush", "session_end", "hangup"}
)


_SMARTPBX_TELEMETRY_MAX_MS = 600_000
_SMARTPBX_ENDPOINT_SOURCES = frozenset({"final", "interim", "capture", "unknown"})
_SMARTPBX_TURN_OUTCOMES = frozenset({
    "completed", "llm_failed", "tool_failed", "tts_failed", "interrupted",
    "transfer_pending", "cancelled",
})
_SMARTPBX_TURN_STAGES = frozenset({
    "started", "endpoint", "kb_start", "kb_complete", "llm_request",
    "llm_first_token", "llm_complete", "tool_start", "tool_complete",
    "tts_request", "tts_first_chunk", "first_media_sent", "queue_drained",
    "barge_clear", "llm_timeout", "late_tool_completion",
})


def _emit_smartpbx_turn_telemetry(_event: str, **fields: object) -> None:
    """Emit fixed-shape telemetry without serializing call or provider data."""
    ordered = " ".join(f"{key}={value}" for key, value in fields.items())
    logger.info("smartpbx_media %s", ordered)


@dataclass
class _SmartPBXTurnState:
    start_ns: int
    endpoint_source: str
    stages: dict[str, int] = field(default_factory=dict)
    finished: bool = False
    stt_interim_events: int = 0
    stt_final_events: int = 0


@dataclass
class _SmartPBXRunnerContext:
    """Task-local ownership for one SmartPBX utterance runner."""

    turn_id: str | None
    dropped_frame_baseline: int
    speak_generation: int
    raw_utterance: str
    rate_context: str = ""
    pending_room: str | None = None
    pending_residency: str | None = None
    residency_question_asked: bool = False
    rate_turn: bool = False
    clear_rate_followup: bool = False
    rate_followup_eligible: bool = False
    initial_filler: Any | None = None
    tool_filler_tasks: set[asyncio.Task] = field(default_factory=set)


_smartpbx_runner_context: ContextVar[_SmartPBXRunnerContext | None] = ContextVar(
    "smartpbx_runner_context", default=None
)


class SmartPBXTurnTelemetry:
    """Call-local, opaque SmartPBX turn timing recorder.

    This object deliberately knows neither Dialog context nor utterance content.
    All timestamps are monotonic and become bounded integer milliseconds only
    when a fixed-shape event is emitted.
    """

    def __init__(
        self,
        *,
        emit: Callable[..., None] = _emit_smartpbx_turn_telemetry,
        monotonic_ns: Callable[[], int] = time.monotonic_ns,
        new_id: Callable[[], str] = lambda: secrets.token_urlsafe(16),
        session_trace_id: str = "",
    ) -> None:
        self._emit = emit
        self._monotonic_ns = monotonic_ns
        self._new_id = new_id
        self._session_trace_id = session_trace_id
        self._turns: dict[str, _SmartPBXTurnState] = {}
        self._last_turn_id: str | None = None
        self.turns_started = 0
        self.turns_summarized = 0

    @property
    def session_trace_id(self) -> str:
        return self._session_trace_id

    def set_session_trace_id(self, session_trace_id: str) -> None:
        """Bind the opaque session trace once; never rebind mid-call."""
        if not self._session_trace_id:
            self._session_trace_id = session_trace_id

    @staticmethod
    def _bounded_ms(start_ns: int, end_ns: int) -> int:
        return min(max((end_ns - start_ns) // 1_000_000, 0), _SMARTPBX_TELEMETRY_MAX_MS)

    def start_turn(
        self,
        endpoint_source: str,
        *,
        stt_interim_events: int = 0,
        stt_final_events: int = 0,
    ) -> str:
        endpoint_source = endpoint_source if endpoint_source in _SMARTPBX_ENDPOINT_SOURCES else "unknown"
        turn_id = self._new_id()
        # Default IDs are random, but retain a bounded collision guard so an
        # adversarial/custom ID source cannot merge an old runner into a new
        # one. Do not retain an unbounded completed-ID history.
        while turn_id in self._turns or turn_id == self._last_turn_id:
            turn_id = secrets.token_urlsafe(16)
        self._turns[turn_id] = _SmartPBXTurnState(
            self._monotonic_ns(),
            endpoint_source,
            stt_interim_events=min(max(int(stt_interim_events), 0), 100_000),
            stt_final_events=min(max(int(stt_final_events), 0), 100_000),
        )
        self._last_turn_id = turn_id
        self.turns_started += 1
        self._emit(
            "turn_stage", event="turn_stage",
            session_trace_id=self._session_trace_id,
            turn_id=turn_id, stage="started", endpoint_source=endpoint_source,
        )
        return turn_id

    def mark(self, turn_id: str, stage: str, *, at_ns: int | None = None) -> None:
        state = self._turns.get(turn_id)
        if state is None or state.finished or stage not in _SMARTPBX_TURN_STAGES:
            return
        timestamp = self._monotonic_ns() if at_ns is None else at_ns
        state.stages[stage] = timestamp
        self._emit(
            "turn_stage", event="turn_stage",
            session_trace_id=self._session_trace_id,
            turn_id=turn_id, stage=stage,
            elapsed_ms=self._bounded_ms(state.start_ns, timestamp),
        )

    def mark_once(self, turn_id: str, stage: str, *, at_ns: int | None = None) -> None:
        state = self._turns.get(turn_id)
        if state is None or stage in state.stages:
            return
        self.mark(turn_id, stage, at_ns=at_ns)

    def has_active_turn(self, turn_id: str) -> bool:
        """Whether this opaque turn still needs transport-stage snapshots."""
        state = self._turns.get(turn_id)
        return state is not None and not state.finished

    def finish(self, turn_id: str, outcome: str, **counts: int) -> None:
        state = self._turns.get(turn_id)
        if state is None or state.finished:
            return
        state.finished = True
        outcome = outcome if outcome in _SMARTPBX_TURN_OUTCOMES else "cancelled"
        end_ns = self._monotonic_ns()
        fields: dict[str, object] = {
            "event": "turn_summary",
            "session_trace_id": self._session_trace_id,
            "turn_id": turn_id, "outcome": outcome,
            "endpoint_source": state.endpoint_source,
            "stt_interim_events": state.stt_interim_events,
            "stt_final_events": state.stt_final_events,
        }
        stage_fields = {
            "endpoint": "endpoint_ms",
            "llm_first_token": "llm_first_token_ms",
            "llm_complete": "llm_complete_ms",
            "tts_first_chunk": "tts_first_chunk_ms",
            "first_media_sent": "first_media_sent_ms",
            "queue_drained": "queue_drained_ms",
            "barge_clear": "barge_clear_ms",
        }
        for stage, field_name in stage_fields.items():
            if stage in state.stages:
                fields[field_name] = self._bounded_ms(state.start_ns, state.stages[stage])
        if "kb_start" in state.stages and "kb_complete" in state.stages:
            fields["kb_ms"] = self._bounded_ms(state.stages["kb_start"], state.stages["kb_complete"])
        if "tool_start" in state.stages and "tool_complete" in state.stages:
            fields["tool_ms"] = self._bounded_ms(state.stages["tool_start"], state.stages["tool_complete"])
        for name, maximum in (
            ("generated_chars", 20_000),
            ("delivered_sentences", 100),
            ("dropped_frames", 100_000),
            ("frames_160b", 100_000),
            ("frames_partial", 100_000),
            ("frames_other", 100_000),
            ("inter_send_gap_p95_ms", _SMARTPBX_TELEMETRY_MAX_MS),
            ("inter_send_gap_max_ms", _SMARTPBX_TELEMETRY_MAX_MS),
            ("queue_underruns", 100_000),
            ("ignored_post_dispatch_finals", 100_000),
            ("ignored_post_dispatch_interims", 100_000),
            # The constant, not a literal: the event clamp and the summary
            # clamp must not be able to drift apart.
            ("ignored_post_dispatch_max_elapsed_ms", POST_DISPATCH_ELAPSED_MS_MAX),
        ):
            if name in counts:
                fields[name] = min(max(int(counts[name]), 0), maximum)
        self.turns_summarized += 1
        try:
            self._emit("turn_summary", **fields)
        finally:
            # Terminal summaries contain all state needed by the session
            # aggregate. Never retain a completed turn for the call lifetime.
            self._turns.pop(turn_id, None)

    def finalize_open_turns(
        self,
        outcome: str,
        *,
        counts_for_turn: Callable[[str], dict[str, int]] | None = None,
    ) -> list[str]:
        """Summarize every unfinished turn exactly once at pipeline teardown.

        finish() pops completed turns, so a delayed runner finishing after this
        snapshot sees no state and cannot emit a second summary.
        """
        open_turn_ids = [
            turn_id for turn_id, state in self._turns.items() if not state.finished
        ]
        for turn_id in open_turn_ids:
            counts = counts_for_turn(turn_id) if counts_for_turn is not None else {}
            self.finish(turn_id, outcome, **counts)
        return open_turn_ids

# ---------------------------------------------------------------------------
# Environment variables
# ---------------------------------------------------------------------------
ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")
OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "")

# OpenAI gpt-4o-mini-tts -- Kavya's Sinhala voice. Natural prosody, streams
# fast, and handles code-switched English far better than Azure or VITS.
OPENAI_TTS_URL: str = "https://api.openai.com/v1/audio/speech"
OPENAI_TTS_MODEL: str = os.getenv("OPENAI_TTS_MODEL", "gpt-4o-mini-tts")
OPENAI_TTS_VOICE: str = os.getenv("OPENAI_TTS_VOICE", "nova")
OPENAI_TTS_INSTRUCTIONS: str = os.getenv(
    "OPENAI_TTS_INSTRUCTIONS",
    "You are Kavya, a warm and friendly reservations agent at Hatton "
    "Hills, in Sri Lanka's central hill country. Speak in natural, "
    "lively conversational Sinhala with genuine warmth -- smile as you talk. "
    "Vary your pitch and pace naturally, soften when being empathetic, and "
    "pause briefly between ideas. Sound like a real person chatting on the "
    "phone, not a robot reading text.",
)
ELEVENLABS_API_KEY: str = os.getenv("ELEVENLABS_API_KEY", "")
ELEVENLABS_VOICE_ID: str = os.getenv("ELEVENLABS_VOICE_ID", "")
# Dedicated Arabic voice (Media Streams). Falls back to ELEVENLABS_VOICE_ID if unset.
ELEVENLABS_VOICE_ID_AR: str = os.getenv("ELEVENLABS_VOICE_ID_AR", "tavIIPLplRB883FzWU0V")
ELEVENLABS_MODEL_MULTILINGUAL: str = "eleven_multilingual_v2"
ELEVENLABS_MODEL_TURBO: str = "eleven_turbo_v2_5"
TWILIO_ACCOUNT_SID: str = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN: str = os.getenv("TWILIO_AUTH_TOKEN", "")
HUMAN_AGENT_PHONE: str = os.getenv("HUMAN_AGENT_PHONE", "").strip()

# Seconds to let the human agent's phone ring before giving up and falling back
# to the WhatsApp failsafe.
#
# Was hard-coded to 20. On 2026-07-31 four of six transfers to a Sri Lankan
# mobile came back status=no-answer, duration=0, unbilled — Twilio placed the
# call and the carrier accepted it, but nobody picked up inside the window. The
# config was byte-identical on the calls that DID connect, so this is answer-side
# latency, not routing. 20s is tight for an international leg to a mobile:
# carrier setup can eat 5-8s of it, leaving barely a dozen seconds of audible
# ringing — often less than one full ring cycle at the handset.
#
# Twilio allows up to 600. Keep it comfortably under the caller's patience: the
# guest is holding music-free silence while this runs.
HANDOFF_DIAL_TIMEOUT: int = int(os.getenv("HANDOFF_DIAL_TIMEOUT", "40"))

# Minimum plausible time between a dial being placed and a HUMAN answering it.
#
# WHY THIS EXISTS: on 2026-08-03 a transfer to the manager was answered by a
# carrier intercept — the leg was answered in the SAME SECOND it was initiated,
# played a recorded announcement at the guest for 52 seconds, and reported
# DialCallStatus=completed. "completed" is indistinguishable from a real pickup,
# so the failsafe stood down and nobody was ever told the guest had called. A
# real handset cannot be lifted in under a second; anything that fast is a
# network answer (intercept, unconditional divert, or instant voicemail).
HANDOFF_MIN_ANSWER_SECONDS: float = float(
    os.getenv("HANDOFF_MIN_ANSWER_SECONDS", "2.0")
)

# Hang up a leg the moment it is identified as a carrier intercept, instead of
# holding the guest through the recording. See /voice/dial-status for the full
# reasoning. Set to "false" to disable at runtime without a code deploy.
HANDOFF_KILL_INTERCEPT: bool = os.getenv(
    "HANDOFF_KILL_INTERCEPT", "true"
).strip().lower() not in ("0", "false", "no", "off")

# Caller ID presented to the human agent when a call is transferred.
#
# WHY THIS EXISTS: <Dial> with no callerId makes Twilio pass through the
# ORIGINAL caller's number. On 2026-07-31 that meant outbound legs to the
# manager's Sri Lankan mobile were presented as coming FROM another Sri Lankan
# mobile that the Twilio account does not own. Twilio accepts this, but the
# destination carrier commonly filters it as caller-ID spoofing, so the handset
# never rings. The failure is NOT consistent, which is what makes it dangerous:
#   - 2026-07-31: carrier reported status=no-answer, duration=0 (handset silent,
#     failsafe fired correctly).
#   - 2026-08-03: carrier ANSWERED the leg instantly with a recorded intercept,
#     played it at the guest for 52 s, and reported status=completed — so the
#     transfer looked successful, the failsafe stood down, and the lead vanished.
#     See HANDOFF_MIN_ANSWER_SECONDS for the guard against that second shape.
#
# Setting this to a number the Twilio account OWNS makes the leg deliverable.
#
# LEAVING IT UNSET DOES NOT FALL BACK TO AN OWNED NUMBER. An earlier version of
# this comment claimed it fell back to the Twilio number the guest dialled; it
# does not, and that wrong comment is why the variable sat unset in production
# until 2026-08-03. Unset means pass-through, i.e. the broken path above.
# `_transfer_caller_id()` returns exactly this value and nothing else.
TWILIO_CALLER_ID: str = os.getenv("TWILIO_CALLER_ID", "").strip()
PUBLIC_HOSTNAME: str = os.getenv("PUBLIC_HOSTNAME", "voice.taskforceai.tech").strip()

# Twilio REST client singleton — used for Path B human handoff
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
KAVYA_SERVICE_MODE: str = os.getenv("KAVYA_SERVICE_MODE", "twilio").strip().lower()
AZURE_SPEECH_KEY: str = os.getenv("AZURE_SPEECH_KEY", "")
AZURE_SPEECH_REGION: str = os.getenv("AZURE_SPEECH_REGION", "southeastasia")

# Media Streams STT backend: "google" (default) or "azure".
# Azure reuses AZURE_SPEECH_KEY / AZURE_SPEECH_REGION (already set for Sinhala TTS).
STT_PROVIDER: str = os.getenv("STT_PROVIDER", "google").lower()

# Debug: dump live call audio to an 8 kHz PCM16 wav for offline STT benchmarking.
STT_DEBUG_DUMP: bool = os.getenv("STT_DEBUG_DUMP", "0") == "1"
STT_DEBUG_DIR: str = os.getenv("STT_DEBUG_DIR", "stt_dumps")
# Break-glass phrase visibility for controlled SmartPBX pilot calls. The exact
# value ``1`` is required; the default keeps the production privacy contract.
# Provider interims, identifiers, prompts and tool data remain redacted even
# when this is enabled -- only finalized guest turns and TTS-submitted Kavya
# phrases use the dedicated pilot record below.
SMARTPBX_PILOT_TRANSCRIPT_LOGGING: bool = (
    os.getenv("SMARTPBX_PILOT_TRANSCRIPT_LOGGING", "0") == "1"
)

# LLM provider selection: "claude" (default), "openai", or "gemini"
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
# Optional: Azure Speech (alternative Media Streams STT, selected via STT_PROVIDER)
# ---------------------------------------------------------------------------
try:
    import azure.cognitiveservices.speech as azure_speech
    AZURE_STT_AVAILABLE = True
except ImportError:
    azure_speech = None  # type: ignore[assignment]
    AZURE_STT_AVAILABLE = False
    logger.warning("azure-cognitiveservices-speech not installed — Azure STT provider unavailable")

# audioop decodes Twilio mulaw â†’ PCM16 for Azure's push stream and for audio
# dumps. Stdlib through Python 3.12; removed in 3.13 (use the audioop-lts shim).
try:
    import audioop
except ImportError:  # pragma: no cover
    audioop = None  # type: ignore[assignment]
    logger.warning("audioop unavailable (Python 3.13+) — Azure STT and audio dump need audioop-lts")

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


def _resolve_smartpbx_max_tokens(raw: object) -> int:
    """Resolve the bounded direct-SmartPBX output budget without touching Twilio."""
    try:
        value = int(raw) if raw not in (None, "") else 120
    except (TypeError, ValueError):
        value = 120
    return min(max(value, 40), 200)


def _resolve_smartpbx_claude_max_tokens(raw: object) -> int:
    """Resolve the Claude-ONLY direct-SmartPBX output budget (canary: 600).

    Claude Sonnet 5 runs adaptive thinking by default, so a tool-calling turn
    spends its first ~50-140 output tokens inside a thinking block before the
    tool_use block even opens. A `check_availability` call needs roughly 160
    output tokens on its own, so at the shared 120-token SmartPBX budget the
    turn hit stop_reason=max_tokens part-way through the tool block: the block
    never reached `content_block_stop`, was therefore never accumulated, and
    the round was misread as an empty response.

    This budget is deliberately Claude-only. OpenAI and Gemini emit no
    thinking tokens, their SmartPBX rounds fit inside SMARTPBX_MAX_TOKENS,
    and they must stay on it (see `_provider_max_tokens`). The global
    ConversationRelay/Twilio `MAX_TOKENS` is likewise untouched.
    """
    try:
        value = int(raw) if raw not in (None, "") else 600
    except (TypeError, ValueError):
        value = 600
    return min(max(value, 200), 1024)


def _resolve_smartpbx_initial_filler_delay(raw: object) -> float:
    try:
        value = float(raw) if raw not in (None, "") else 1.5
    except (TypeError, ValueError):
        value = 1.5
    if not math.isfinite(value):
        value = 1.5
    return min(max(value, 0.5), 5.0)


def _resolve_smartpbx_initial_response_timeout_seconds(raw: object) -> float:
    try:
        value = float(raw) if raw not in (None, "") else 8.0
    except (TypeError, ValueError):
        value = 8.0
    if not math.isfinite(value):
        value = 8.0
    return min(max(value, 1.0), 30.0)


def _resolve_smartpbx_llm_stall_timeout_seconds(raw: object) -> float:
    try:
        value = float(raw) if raw not in (None, "") else 8.0
    except (TypeError, ValueError):
        value = 8.0
    if not math.isfinite(value):
        value = 8.0
    return min(max(value, 1.0), 30.0)


def _resolve_smartpbx_claude_thinking_stall_timeout_seconds(
    raw: object, general_stall_timeout: float,
) -> float:
    """Resolve Claude's first-attempt thinking grace without weakening stalls.

    An explicit value may lower the grace to the normal stall deadline for a
    fast rollback. The general deadline remains the floor, so this setting can
    never make any direct SmartPBX stream less responsive than the shared
    policy.
    """
    try:
        value = float(raw) if raw not in (None, "") else 12.0
    except (TypeError, ValueError):
        value = 12.0
    if not math.isfinite(value):
        value = 12.0
    return max(min(max(value, 1.0), 30.0), general_stall_timeout)


SMARTPBX_MAX_TOKENS: int = _resolve_smartpbx_max_tokens(
    os.getenv("SMARTPBX_MAX_TOKENS")
)
SMARTPBX_CLAUDE_MAX_TOKENS: int = _resolve_smartpbx_claude_max_tokens(
    os.getenv("SMARTPBX_CLAUDE_MAX_TOKENS")
)
SMARTPBX_INITIAL_FILLER_DELAY_SECONDS: float = _resolve_smartpbx_initial_filler_delay(
    os.getenv("SMARTPBX_INITIAL_FILLER_DELAY_SECONDS")
)
SMARTPBX_LLM_INITIAL_RESPONSE_TIMEOUT_SECONDS: float = _resolve_smartpbx_initial_response_timeout_seconds(
    os.getenv("SMARTPBX_LLM_INITIAL_RESPONSE_TIMEOUT_SECONDS")
)
SMARTPBX_LLM_STALL_TIMEOUT_SECONDS: float = _resolve_smartpbx_llm_stall_timeout_seconds(
    os.getenv("SMARTPBX_LLM_STALL_TIMEOUT_SECONDS")
)
SMARTPBX_CLAUDE_THINKING_STALL_TIMEOUT_SECONDS: float = (
    _resolve_smartpbx_claude_thinking_stall_timeout_seconds(
        os.getenv("SMARTPBX_CLAUDE_THINKING_STALL_TIMEOUT_SECONDS"),
        SMARTPBX_LLM_STALL_TIMEOUT_SECONDS,
    )
)
MAX_HISTORY_MESSAGES: int = 60
MAX_TOOL_ROUNDS: int = 5

SMARTPBX_CLAUDE_STREAM_PROGRESS_VALUES: frozenset[str] = frozenset({
    "none", "metadata", "thinking", "text", "tool",
})
SMARTPBX_CLAUDE_STREAM_PROGRESS_RANK: dict[str, int] = {
    "none": 0,
    "metadata": 1,
    "thinking": 2,
    "text": 3,
    "tool": 4,
}
SMARTPBX_TIMEOUT_PROVIDERS: frozenset[str] = frozenset({
    "openai", "gemini", "claude",
})


def _advance_claude_stream_progress(current: str, candidate: str) -> str:
    """Keep Claude timeout progress monotonic and within its closed enum."""
    if candidate not in SMARTPBX_CLAUDE_STREAM_PROGRESS_VALUES:
        return current
    if current not in SMARTPBX_CLAUDE_STREAM_PROGRESS_VALUES:
        current = "none"
    if (
        SMARTPBX_CLAUDE_STREAM_PROGRESS_RANK[candidate]
        > SMARTPBX_CLAUDE_STREAM_PROGRESS_RANK[current]
    ):
        return candidate
    return current


def _normalized_smartpbx_timeout_provider(raw: object) -> str:
    """Map timeout provider input to the finite logging provider enum."""
    if isinstance(raw, str) and raw in SMARTPBX_TIMEOUT_PROVIDERS:
        return raw
    return "unknown"


class SmartPBXClaudeRoundOutcome(str, enum.Enum):
    """How one Claude streaming round actually ended.

    Before this existed the runner asked a single question -- "is there text
    or a complete tool block?" -- and called everything else EMPTY. That
    conflated a clean no-content turn with a turn whose tool block was cut
    off mid-JSON by the output budget, which made the real failure mode
    (max_tokens truncation) invisible in the logs. These six outcomes are
    mutually exclusive and classified in `_classify_claude_round_outcome`.

    `COMPLETED` is the ONLY outcome that may proceed to speak or dispatch
    tools. Every other member routes to the shared retry-once-then-recovery
    path; the classification, not an ad-hoc content check, is what decides.
    """

    COMPLETED = "completed"
    MAX_TOKENS_TRUNCATED = "max_tokens_truncated"
    TRUE_EMPTY = "true_empty"
    INCOMPLETE_TOOL_BLOCK = "incomplete_tool_block"
    MALFORMED_TOOL_JSON = "malformed_tool_json"
    # The stream ended with no `message_delta` and no `message_stop` -- the
    # connection dropped mid-turn rather than the model finishing one. Before
    # this member existed such a round looked byte-identical to a clean
    # no-content turn and was logged as `true_empty`, hiding a transport fault
    # behind a model-behaviour label.
    STREAM_ABORTED = "stream_aborted"


# Outcomes whose partial output must be DISCARDED WHOLE before the retry /
# recovery path runs: no tool from that round is dispatched (not even a
# `content_block_stop`-complete one that shared the round with a truncated
# sibling), and any per-sentence TTS already in flight for it is cancelled and
# generation-fenced. Discarding the complete siblings too is what makes the
# retry safe: nothing from the round executed, so re-asking is not a replay.
#
# TRUE_EMPTY is deliberately NOT here. By construction it produced no text, no
# tool block and therefore no TTS task, so there is nothing to discard or
# fence; it keeps exactly the caller-facing handling it always had.
SMARTPBX_CLAUDE_DISCARD_ROUND_OUTCOMES: frozenset[SmartPBXClaudeRoundOutcome] = (
    frozenset({
        SmartPBXClaudeRoundOutcome.MAX_TOKENS_TRUNCATED,
        SmartPBXClaudeRoundOutcome.INCOMPLETE_TOOL_BLOCK,
        SmartPBXClaudeRoundOutcome.MALFORMED_TOOL_JSON,
        SmartPBXClaudeRoundOutcome.STREAM_ABORTED,
    })
)


def _classify_claude_round_outcome(
    *,
    text_content: str,
    tool_use_blocks: list[dict[str, Any]],
    incomplete_tool_block: bool,
    malformed_tool_json: bool,
    stop_reason: str | None,
    saw_terminal_metadata: bool = True,
) -> SmartPBXClaudeRoundOutcome:
    """Classify one Claude round. `tool_use_blocks` holds ONLY blocks that
    reached `content_block_stop` with parseable JSON -- truncated and
    malformed blocks are discarded at accumulation time and never reach here,
    so a caller can act on this result without re-validating it.

    ORDER: TERMINAL FAILURE FIRST, visible output LAST. Whether the round ended
    badly is decided before -- and independently of -- whether it happened to
    leave anything usable behind, because partial output from a round that was
    cut off is not evidence of success; it is the wreckage of the failure.
    Reading it the other way round is what let a truncated round speak its
    partial sentence and dispatch its complete tool blocks:

    1. `stop_reason == "max_tokens"` -> MAX_TOKENS_TRUNCATED, unconditionally.
       The budget ran out mid-turn. The model had more to say and could not say
       it, so whatever it DID emit is a prefix of an answer, not an answer --
       even when that prefix is a fully-terminated tool block. A round that
       genuinely finished never reports this stop reason.
    2. An unterminated tool block -> INCOMPLETE_TOOL_BLOCK, and unparseable
       tool JSON -> MALFORMED_TOOL_JSON. These sit under (1) only because
       max_tokens names the actionable cause of the very same wreckage; under
       any other stop reason they are the more specific description of a
       genuine stream defect and must not be laundered into the generic
       transport label below.
    3. No `message_delta` and no `message_stop` -> STREAM_ABORTED. The model
       never reported ending its turn, so the connection dropped mid-stream.
       This is checked BEFORE visible output for the same reason as (1): text
       that arrived before an EOF is an unfinished sentence, and a tool block
       that arrived before an EOF belongs to a batch we never saw the end of.
       We cannot know what else the round intended to emit.
    4. Only then does visible output mean COMPLETED -- i.e. a terminal,
       non-truncated, structurally sound round that produced something.
    5. Terminal, sound, and empty is TRUE_EMPTY: the model reported ending its
       turn having chosen to say nothing. That claim is only available once (3)
       has ruled out an abrupt EOF.

    Every outcome except COMPLETED routes to the retry-once-then-recovery path,
    and every one that can carry partial output (all of them but TRUE_EMPTY,
    which by construction produced none) is in
    `SMARTPBX_CLAUDE_DISCARD_ROUND_OUTCOMES`, so the round is discarded whole
    and its in-flight TTS fenced before anything else speaks.
    """
    if stop_reason == "max_tokens":
        return SmartPBXClaudeRoundOutcome.MAX_TOKENS_TRUNCATED
    if incomplete_tool_block:
        return SmartPBXClaudeRoundOutcome.INCOMPLETE_TOOL_BLOCK
    if malformed_tool_json:
        return SmartPBXClaudeRoundOutcome.MALFORMED_TOOL_JSON
    if not saw_terminal_metadata:
        return SmartPBXClaudeRoundOutcome.STREAM_ABORTED
    if text_content.strip() or tool_use_blocks:
        return SmartPBXClaudeRoundOutcome.COMPLETED
    return SmartPBXClaudeRoundOutcome.TRUE_EMPTY


# The complete set of `stop_reason` values the runner will ever LOG. Anthropic
# may add new ones and a proxy may return anything at all, so the raw string
# never reaches a log line: an unrecognised value logs as `unknown` and an
# absent one as `none`. That keeps `stop_reason` a bounded enum field in the
# runbook's approved telemetry allowlist rather than an open text channel that
# could carry a stop_sequence's contents (which are caller-derived).
SMARTPBX_CLAUDE_STOP_REASONS: frozenset[str] = frozenset({
    "end_turn", "max_tokens", "tool_use", "stop_sequence", "refusal",
})
SMARTPBX_CLAUDE_STOP_REASON_ABSENT: str = "none"
SMARTPBX_CLAUDE_STOP_REASON_UNKNOWN: str = "unknown"
# Wide enough that no legitimate Anthropic output budget is ever clamped
# (the largest published max_tokens is far below this), narrow enough that a
# corrupt or hostile usage payload cannot write an unbounded numeral.
SMARTPBX_CLAUDE_MAX_LOGGED_OUTPUT_TOKENS: int = 1_000_000
SMARTPBX_CLAUDE_MAX_LOGGED_ATTEMPT: int = 9


def _normalized_claude_stop_reason(raw: object) -> str:
    """Map any stop_reason onto the fixed logging enum above."""
    if raw is None or raw == "":
        return SMARTPBX_CLAUDE_STOP_REASON_ABSENT
    if isinstance(raw, str) and raw in SMARTPBX_CLAUDE_STOP_REASONS:
        return raw
    return SMARTPBX_CLAUDE_STOP_REASON_UNKNOWN


def _bounded_claude_output_tokens(raw: object) -> str:
    """Clamp the logged output-token count to a sane numeric range.

    Returns `unknown` when the stream never reported usage, so the field is
    always one of a bounded numeral or that single sentinel.
    """
    if isinstance(raw, bool) or not isinstance(raw, int):
        return "unknown"
    return str(min(max(raw, 0), SMARTPBX_CLAUDE_MAX_LOGGED_OUTPUT_TOKENS))


def _bounded_claude_attempt(raw: object) -> int:
    """Clamp the logged attempt number to `[1, SMARTPBX_CLAUDE_MAX_LOGGED_ATTEMPT]`."""
    if isinstance(raw, bool) or not isinstance(raw, int):
        return 1
    return min(max(raw, 1), SMARTPBX_CLAUDE_MAX_LOGGED_ATTEMPT)


SMARTPBX_LLM_TOOL_STARTED_RECOVERY_TEXT: str = (
    "I'm sorry, I wasn't able to give you a clear update. Would you like me "
    "to continue?"
)

# Spoken when a direct SmartPBX turn produces no text and no tool call BEFORE
# any tool/side effect has started, after the single same-provider retry has
# also come back empty. Distinct from SMARTPBX_LLM_TOOL_STARTED_RECOVERY_TEXT
# above (which is used once a tool may already have run) and from the
# per-language LLM_EMPTY_FALLBACKS used by the Twilio Media Streams path,
# which this constant does not replace.
SMARTPBX_LLM_EMPTY_RETRY_RECOVERY_TEXT: str = (
    "I'm sorry, I'm having trouble responding right now. Could you please "
    "say that again?"
)

# Ephemeral system hint for the single pre-tool empty-response retry on the
# direct SmartPBX path. Never stored in self.history — built fresh per retry
# attempt for OpenAI/Claude; Gemini uses its own GEMINI_EMPTY_RETRY_NUDGE via
# the existing failover-aware retry path.
SMARTPBX_EMPTY_RETRY_NUDGE: str = (
    "[SYSTEM: Your previous attempt returned no text and no tool call. The "
    "caller is waiting in silence. Reply now with one short spoken sentence "
    "or a tool call. Do not mention this instruction.]"
)

# Caller phone lookup — populated by HTTP handlers, consumed by WebSocket handlers
_call_phone: dict[str, str] = {}  # CallSid -> caller phone number


def _transfer_caller_id(call_sid: str) -> str:
    """Caller ID for the outbound transfer leg, or "" for Twilio's default.

    DEFAULTS TO "" — i.e. Twilio passes the GUEST's number through. That shows
    the manager who is actually calling, which is genuinely useful, but it is
    NOT safe on Sri Lankan mobile destinations: the leg arrives at the local
    carrier from an international gateway claiming a local CLI, and gets
    filtered or intercepted (see the TWILIO_CALLER_ID comment above).

    SET THIS IN PRODUCTION. Treating pass-through as the normal configuration is
    what broke handovers on 2026-07-31 and again on 2026-08-03. Point it at a
    number the account owns — the number the guest dialled is the natural
    choice. The manager loses the guest's CLI, but the whisper announces the
    reason and the failsafe WhatsApp carries the guest's number, so nothing is
    actually lost.
    """
    return TWILIO_CALLER_ID

# Handoff carry-over — populated when a live transfer to a human is dispatched,
# read back by the recovery ConversationRelay session if the human never picked
# up. The relay session that follows a failed dial is a brand-new WebSocket with
# empty history, so everything Kavya needs to run the failsafe (what the guest
# wanted, their name if given, the number they called from) has to survive here.
# CallSid -> {reason, caller_phone, transcript, dial_status, notified, dial_events}
_handoff_state: dict[str, dict] = {}


def _answer_looks_intercepted(state: dict) -> tuple[bool, str]:
    """Did the 'answered' dial leg reach a human, or a network intercept?

    Reads the per-event timestamps recorded by /voice/dial-status. A handset
    cannot be answered in under a second; when it happens the leg was taken by
    an intercept recording, an unconditional divert, or instant voicemail.

    FAILS OPEN. If the timestamps are missing (status callback lost, or it
    raced the action callback) this returns False and the transfer is treated
    as genuine. A false negative costs one missed WhatsApp; a false positive
    would bounce a guest who really did speak to a human back into the
    failsafe, which is worse.
    """
    events = state.get("dial_events") or {}
    initiated = events.get("initiated")
    answered = events.get("answered")
    if initiated is None or answered is None:
        return False, "no timing available"
    gap = answered - initiated
    if gap < HANDOFF_MIN_ANSWER_SECONDS:
        return True, f"answered {gap:.2f}s after dial — too fast for a handset"
    return False, f"answered after {gap:.2f}s"
_HANDOFF_STATE_MAX = 200  # bound the dict; abandoned calls never clean up


def _safe_client(factory):
    """Build an LLM client, returning None instead of raising.

    The client getters raise when their API key is unset. That is the right
    behaviour on the conversation path — no key means no call — but on the
    post-call bookkeeping path it would throw away the record entirely. Here a
    missing client just degrades the summary, so swallow and continue.
    """
    try:
        return factory()
    except Exception:
        logger.warning("LLM client unavailable for post-call summary", exc_info=True)
        return None


def _remember_handoff(call_sid: str, **fields) -> None:
    """Record/merge handoff carry-over for a call, evicting the oldest entries."""
    if not call_sid or call_sid == "unknown":
        return
    entry = _handoff_state.setdefault(call_sid, {})
    entry.update(fields)
    while len(_handoff_state) > _HANDOFF_STATE_MAX:
        _handoff_state.pop(next(iter(_handoff_state)), None)

# ---------------------------------------------------------------------------
# Filler messages sent while tools execute
# ---------------------------------------------------------------------------
TOOL_FILLERS: dict[str, str] = {
    "check_availability": "Let me check availability for those dates.",
    "create_booking": "I'm creating your reservation now.",
    "retrieve_booking": "Let me look up that booking for you.",
    "cancel_booking": "Let me process that cancellation.",
    "notify_human_handover": "Let me pass your details to our team now.",
}
DEFAULT_FILLER: str = "Let me check that for you."

# A small set of alternate one-liners for check/create/transfer tool calls.
# Rotate these naturally to avoid repetitive canned wording across repeated retries.
TOOL_FILLER_VARIANTS: dict[str, tuple[str, ...]] = {
    "check_availability": (
        "Just a moment while I check availability.",
        "Let me check that for you.",
        "One second while I check it.",
    ),
    "create_booking": (
        "Let me prepare that booking now.",
        "One moment while I create that for you.",
        "I'm doing that now; give me a second.",
    ),
    "transfer_to_human": (
        "One moment while I connect you to a human agent.",
        "I’ll transfer you now.",
        "Just a second while I connect that call.",
    ),
}

_TOOL_FILLER_CYCLES: dict[str, int] = {
    "check_availability": 0,
    "create_booking": 0,
    "transfer_to_human": 0,
}


SMARTPBX_INITIAL_FILLER_TEXT = "Just a moment while I check that for you."
_SMARTPBX_CAPTURE_TOOLS = frozenset({
    "capture_spoken_number", "capture_spoken_name", "collect_number_via_keypad",
})

SMARTPBX_FILLER_ALTERNATES: tuple[str, ...] = (
    "I am looking into that for you.",
    "Let me review those details carefully.",
    "I will gather the information.",
    "I am taking a closer look.",
    "I can help with that request.",
    "I will handle that with care.",
)
SMARTPBX_INITIAL_FILLER_BANK: tuple[str, ...] = (
    SMARTPBX_INITIAL_FILLER_TEXT,
    *SMARTPBX_FILLER_ALTERNATES,
)
SMARTPBX_TOOL_FILLER_BANKS: dict[str, tuple[str, ...]] = {
    "check_availability": (
        TOOL_FILLERS["check_availability"],
        *SMARTPBX_FILLER_ALTERNATES,
    ),
    "create_booking": (
        TOOL_FILLERS["create_booking"],
        *SMARTPBX_FILLER_ALTERNATES,
    ),
    "retrieve_booking": (
        TOOL_FILLERS["retrieve_booking"],
        *SMARTPBX_FILLER_ALTERNATES,
    ),
    "cancel_booking": (
        TOOL_FILLERS["cancel_booking"],
        *SMARTPBX_FILLER_ALTERNATES,
    ),
    "notify_human_handover": (
        TOOL_FILLERS["notify_human_handover"],
        *SMARTPBX_FILLER_ALTERNATES,
    ),
}
SMARTPBX_DEFAULT_FILLER_BANK: tuple[str, ...] = (
    DEFAULT_FILLER,
    *SMARTPBX_FILLER_ALTERNATES,
)


class _CallFillerLease:
    """A reserved phrase committed only once its delivery task starts."""

    def __init__(self, rotation, bank_name: str, text: str) -> None:
        self._rotation = rotation
        self.bank_name = bank_name
        self.text = text
        self._active = True

    def commit(self) -> None:
        if self._active:
            self._active = False
            self._rotation._commit(self)

    def release(self) -> None:
        if self._active:
            self._active = False
            self._rotation._release(self)


class _CallFillerRotation:
    """Deterministic per-call selection with actual-start commit semantics."""

    def __init__(self) -> None:
        self._used: set[str] = set()
        self._reserved: set[str] = set()
        self._last_spoken: str | None = None

    def reserve(self, bank_name: str, phrases: tuple[str, ...]) -> _CallFillerLease:
        candidates = tuple(dict.fromkeys(phrase for phrase in phrases if phrase))
        if not candidates:
            raise ValueError("filler rotation bank must not be empty")

        available = [
            phrase
            for phrase in candidates
            if phrase not in self._used and phrase not in self._reserved
        ]
        if not available:
            self._used.clear()
            available = [phrase for phrase in candidates if phrase not in self._reserved]
        non_repeating = [phrase for phrase in available if phrase != self._last_spoken]
        if non_repeating:
            selected = non_repeating[0]
        elif available:
            selected = available[0]
        else:
            selected = candidates[0]

        self._reserved.add(selected)
        return _CallFillerLease(self, bank_name, selected)

    def next(self, bank_name: str, phrases: tuple[str, ...]) -> str:
        """Select and immediately commit a phrase for small synchronous callers."""
        lease = self.reserve(bank_name, phrases)
        lease.commit()
        return lease.text

    def _commit(self, lease: _CallFillerLease) -> None:
        self._reserved.discard(lease.text)
        self._used.add(lease.text)
        self._last_spoken = lease.text

    def _release(self, lease: _CallFillerLease) -> None:
        self._reserved.discard(lease.text)


class SmartPBXInitialFillerController:
    """One cancellable neutral filler for a direct SmartPBX first provider round.

    The timer is deliberately independent of the provider stream. Provider
    content waits for a started filler to finish; a tool cancels only a pending
    timer so its side effect can start while a delivered filler still drains.
    Barge-in, transfer, and terminal teardown are the only lifecycle owners
    allowed to cancel audible speech.
    """

    def __init__(
        self,
        *,
        speak,
        generation: int,
        delay_seconds: float,
        sleep=asyncio.sleep,
        clear_audio=None,
        text: str = SMARTPBX_INITIAL_FILLER_TEXT,
        lease: _CallFillerLease | None = None,
    ) -> None:
        self._speak = speak
        self.generation = generation
        self.delay_seconds = delay_seconds
        self._sleep = sleep
        self._clear_audio = clear_audio
        self.text = text
        self._lease = lease
        self._task: asyncio.Task | None = None
        self.spoke = False
        self._cleared_after_spoke = False

    @property
    def suppress_specialized_tool_filler(self) -> bool:
        return self.spoke

    def start(self, *, capture_tool: str | None = None) -> None:
        if self._task is not None or capture_tool in _SMARTPBX_CAPTURE_TOOLS:
            return
        self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        try:
            await self._sleep(self.delay_seconds)
            # Mark before awaiting TTS so an arriving tool suppresses a second
            # specialized filler while this one drains to its delivery barrier.
            self.spoke = True
            if self._lease is not None:
                self._lease.commit()
            await self._speak(self.text, generation=self.generation)
        except asyncio.CancelledError:
            return

    async def _cancel(self, *, clear_if_spoke: bool) -> None:
        task = self._task
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if (
            clear_if_spoke
            and self.spoke
            and not self._cleared_after_spoke
            and self._clear_audio is not None
        ):
            self._cleared_after_spoke = True
            await self._clear_audio()
        if not self.spoke and self._lease is not None:
            self._lease.release()

    async def _cancel_pending_or_drain_delivery(self) -> None:
        """Let model readiness cancel only a timer that has not spoken yet.

        Once the filler has entered TTS, its task owns the current media
        generation through ``send_mark``.  Provider content is not a caller
        interruption, so it queues behind that delivery rather than cancelling
        the task and clearing its audible tail.
        """
        task = self._task
        if task is None or task.done():
            return
        if self.spoke:
            await asyncio.gather(task, return_exceptions=True)
            return
        await self._cancel(clear_if_spoke=False)

    async def on_content_delta(self) -> None:
        await self._cancel_pending_or_drain_delivery()

    async def on_tool_delta(self) -> None:
        # A tool needs to start immediately to cover PMS/MCP latency. It may
        # still be followed by response speech only after ``wait`` drains the
        # started filler at the end of the tool batch.
        if not self.spoke:
            await self._cancel(clear_if_spoke=False)

    async def on_barge_in(self) -> None:
        # The caller owns the established generation-clear sequence; doing it
        # here too would emit two clears for one barge-in.
        await self._cancel(clear_if_spoke=False)

    async def on_generation_change(self, generation: int) -> None:
        if generation != self.generation:
            await self._cancel(clear_if_spoke=True)

    async def on_session_finish(self) -> None:
        # Session/transfer teardown performs the authoritative media clear.
        await self._cancel(clear_if_spoke=False)

    async def wait(self) -> None:
        if self._task is not None:
            await asyncio.gather(self._task, return_exceptions=True)
        if not self.spoke and self._lease is not None:
            self._lease.release()


def _next_tool_filler(tool_name: str) -> str:
    variants = TOOL_FILLER_VARIANTS.get(tool_name)
    if not variants:
        return DEFAULT_FILLER
    index = _TOOL_FILLER_CYCLES.get(tool_name, 0)
    _TOOL_FILLER_CYCLES[tool_name] = (index + 1) % len(variants)
    return variants[index]


_CONVERSATION_CLAUDE_CAPTURE_TOOLS: set[str] = {
    "capture_spoken_number",
    "capture_spoken_name",
    "collect_number_via_keypad",
}


def _join_turn(accumulated: str, new_text: str) -> str:
    """Append one tool-round's text to the running response, with a separator.

    A plain `+=` runs the rounds together in the TRANSCRIPT: the model's
    pre-tool line and its post-tool line arrive as separate streaming rounds,
    so "…right away." + "I'm transferring…" became
    "…right away.I'm transferring…". The spoken audio is unaffected (each round
    is streamed to Twilio on its own), but the mangled string is what gets
    logged and shipped to the Google Sheet call log.

    Only inserts a space when both sides are non-empty and the boundary isn't
    already whitespace, so it never introduces a leading/double space.
    """
    if not new_text:
        return accumulated
    if not accumulated:
        return new_text
    if accumulated[-1].isspace() or new_text[0].isspace():
        return accumulated + new_text
    return accumulated + " " + new_text


def _bounded_smartpbx_timeout_ms(raw: object) -> int:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 8_000
    if not math.isfinite(value):
        return 8_000
    return min(max(int(round(value)), 1_000), 30_000)


def _smartpbx_timeout_seconds_to_ms(timeout: float) -> int:
    return _bounded_smartpbx_timeout_ms(timeout * 1_000)


class _SmartPBXStreamTimeout(Exception):
    """Raised when a direct-SmartPBX provider stream stalls or never starts.

    ``phase`` is one of the two fixed enum values below — never raw provider
    text, payloads, or exception bodies — so it is safe to log and fold into
    telemetry unchanged.
    """

    PHASE_INITIAL = "initial"
    PHASE_STALL = "stall"

    def __init__(self, *, phase: str, timeout_ms: int | None = None) -> None:
        self.phase = phase
        self.timeout_ms = (
            _bounded_smartpbx_timeout_ms(timeout_ms)
            if timeout_ms is not None
            else None
        )
        super().__init__(phase)


async def _smartpbx_acquire_stream_within_deadline(acquire, *, timeout: float):
    """Await ``acquire()`` — the client call that creates/opens a provider
    stream — bounded by ``timeout``.

    A hung ``create()``/``generate_content_stream()`` call (the client
    never returns) must recover exactly like a first item that never
    arrives, so this raises the same ``_SmartPBXStreamTimeout`` with
    ``phase=PHASE_INITIAL`` on timeout rather than letting the caller wait
    forever before the guarded-iteration timeout ever gets a chance to run.
    Used by the OpenAI and Gemini SmartPBX runners; Claude's stream is an
    async context manager and uses ``_TimeoutGuardedAsyncCM`` instead.
    """
    try:
        return await asyncio.wait_for(acquire(), timeout=timeout)
    except asyncio.TimeoutError:
        raise _SmartPBXStreamTimeout(
            phase=_SmartPBXStreamTimeout.PHASE_INITIAL,
            timeout_ms=_smartpbx_timeout_seconds_to_ms(timeout),
        ) from None


class _TimeoutGuardedAsyncCM:
    """Wrap an async context manager so *entering* it is bounded by ``timeout``.

    Anthropic's ``client.messages.stream(...)`` returns a context manager
    whose ``__aenter__`` performs the actual network call — a hang there
    (the client call never returns) must trigger the same recovery path as a
    hanging first delta, not block forever before the per-item stall guard
    ever starts. ``__aexit__`` is only forwarded to the wrapped context
    manager if entry actually completed, mirroring normal ``async with``
    semantics (a failed/timed-out ``__aenter__`` never gets a matching
    ``__aexit__``).
    """

    def __init__(self, cm, *, timeout: float):
        self._cm = cm
        self._timeout = timeout
        self._entered = False

    async def __aenter__(self):
        try:
            result = await asyncio.wait_for(self._cm.__aenter__(), timeout=self._timeout)
        except asyncio.TimeoutError:
            raise _SmartPBXStreamTimeout(
                phase=_SmartPBXStreamTimeout.PHASE_INITIAL,
                timeout_ms=_smartpbx_timeout_seconds_to_ms(self._timeout),
            ) from None
        self._entered = True
        return result

    async def __aexit__(self, exc_type, exc, tb):
        if not self._entered:
            return False
        return await self._cm.__aexit__(exc_type, exc, tb)


async def _smartpbx_timeout_guarded_stream(
    source,
    *,
    initial_timeout: float,
    stall_timeout: float,
    stall_timeout_getter: Callable[[], float] | None = None,
):
    """Re-yield ``source``'s items, raising ``_SmartPBXStreamTimeout`` on stall.

    Shared by all three Media Streams provider runners (OpenAI, Gemini,
    Claude) on the direct SmartPBX English path only. The FIRST item must
    arrive within ``initial_timeout``; every item after that must arrive
    within ``stall_timeout`` of the previous one. Callers may provide a
    bounded ``stall_timeout_getter`` when their already-closed progress state
    changes that interval. No total stream deadline — content that keeps
    arriving keeps the guard resetting.

    ``initial_timeout`` here is expected to already be the REMAINDER of the
    provider's initial-response budget after stream acquisition (see
    ``_smartpbx_acquire_stream_within_deadline`` / ``_TimeoutGuardedAsyncCM``)
    — acquisition and the wait for the first item are together bounded by
    ``SMARTPBX_LLM_INITIAL_RESPONSE_TIMEOUT_SECONDS`` at the call site.
    """
    aiter = source.__aiter__()
    first = True
    while True:
        timeout = (
            initial_timeout
            if first
            else (
                stall_timeout_getter()
                if stall_timeout_getter is not None
                else stall_timeout
            )
        )
        try:
            item = await asyncio.wait_for(aiter.__anext__(), timeout=timeout)
        except asyncio.TimeoutError:
            raise _SmartPBXStreamTimeout(
                phase=(
                    _SmartPBXStreamTimeout.PHASE_INITIAL
                    if first
                    else _SmartPBXStreamTimeout.PHASE_STALL
                ),
                timeout_ms=_smartpbx_timeout_seconds_to_ms(timeout),
            ) from None
        except StopAsyncIteration:
            return
        first = False
        yield item

# Backchannel filter: short non-semantic utterances that callers emit while
# thinking ("um", "uh", "hmm"). Twilio's STT fires these as full prompts and
# without filtering, Kavya would jump in mid-thought, derailing the call.
# We deliberately do NOT include "ok", "yeah", "yes", "no", "right" — those
# are genuine answers in this booking flow.
BACKCHANNEL_TOKENS: set[str] = {
    "um", "uh", "uhm", "umm", "uhh", "erm", "er",
    "hmm", "hm", "mm", "mhm", "mmhm", "mhmm",
    "ah", "oh", "huh",
    "ah um", "uh um", "um uh", "uh uh",
}


def _is_backchannel(text: str) -> bool:
    """True if the utterance is purely thinking-noise — should be ignored
    so the caller keeps the turn. Strips punctuation and lowercases."""
    # Digit-bearing utterances are real content (phone numbers, dates,
    # room counts, etc.) — never treat them as backchannel.
    if any(c.isdigit() for c in text):
        return False
    cleaned = "".join(c for c in text.lower() if c.isalpha() or c.isspace()).strip()
    if not cleaned:
        return True  # empty / pure punctuation
    if len(cleaned) > 8:  # anything longer than "uh uh um" is probably real
        return False
    return cleaned in BACKCHANNEL_TOKENS

# Sent when the LLM hasn't returned its first token within SLOW_RESPONSE_DELAY
# seconds — covers Anthropic 429 retries and other network latency so the
# guest doesn't think the line dropped and re-speak (which corrupts slot-filling).
SLOW_RESPONSE_DELAY: float = 2.5
SLOW_RESPONSE_FILLERS: dict[str, str] = {
    "en": "One moment please.",
    "ar": "لحظة من فضلك.",
    "si": "à¶šà¶»à·”à¶¯à·à¶šà¶»à· à¶»à·à¶¯à·™à¶±à·Šà¶±.",
    "ta": "à®¤à®¯à®µà¯à®šà¯†à®¯à¯à®¤à¯ à®•à®¾à®¤à¯à®¤à®¿à®°à¯à®™à¯à®•à®³à¯.",
}

# ---------------------------------------------------------------------------
# IVR language configurations
# ---------------------------------------------------------------------------
# Set IVR_MENU_ENABLED=true to present the DTMF language menu on incoming
# calls. Default "false": every call connects straight to the English
# ConversationRelay agent (no "press 1/2/3" prompt). /voice/language-selected
# stays wired either way, so re-enabling the menu needs no code change.
IVR_MENU_ENABLED: bool = os.getenv("IVR_MENU_ENABLED", "false").lower() == "true"

# Maps DTMF digit â†’ language code
# English-only line. Sinhala and Arabic were removed from the menu on
# 2026-07-28; Sinhala/Tamil/Arabic code paths remain fully implemented below
# but no digit routes to them. To re-expose one, add its digit here and a
# matching <Say> prompt in /voice/incoming, and re-add it to the
# ws_media_stream guard.
DIGIT_TO_LANG: dict[str, str] = {"1": "en"}

# ConversationRelay transcription hints (#121). A comma-separated vocabulary
# that biases Google's telephony STT toward tokens it otherwise mishears on
# Sri Lankan-accented English. Two groups:
#   1. Spoken digit shorthand for phone numbers — "double"/"triple" and the
#      digit words. Without these, "double seven" is garbled and Kavya never
#      receives the word "double" to expand, so she cannot understand it live.
#   2. A starter set of common Sri Lankan given names and surnames, so names
#      survive transcription (see the wrong-name booking incident).
# Extend without a code change via CR_HINTS_EN.
_DEFAULT_EN_HINTS = (
    "double, triple, treble, oh, zero, one, two, three, four, five, six, "
    "seven, eight, nine, "
    "Chanya, Shehani, Oshadi, Kavya, Nimal, Kamal, Sunil, Saman, Chaminda, "
    "Ruwan, Nuwan, Kasun, Tharindu, Sachini, Nadeesha, Dilhani, Ishara, "
    "Hasini, Dilan, "
    "Perera, Fernando, Silva, Bandara, Jayawardena, Wickramasinghe, "
    "Gunawardena, Rajapaksa, Dissanayake, Senanayake, Ranasinghe, Wijesinghe"
)
CR_HINTS_EN: str = os.getenv("CR_HINTS_EN", _DEFAULT_EN_HINTS)

# Separate STT language for the English line (#121). Default "" keeps the
# existing behaviour (transcription follows `language`, en-US). Set
# CR_TRANSCRIPTION_LANGUAGE=en-IN to A/B whether Indian-English acoustic models
# recognise Sri Lankan accents better — env-controlled so it flips without a
# redeploy, and emitted only when set so a bad value can't affect the default.
CR_TRANSCRIPTION_LANGUAGE_EN: str = os.getenv("CR_TRANSCRIPTION_LANGUAGE", "").strip()

# Per-language ConversationRelay TwiML configuration
LANGUAGE_CONFIGS: dict[str, dict[str, str]] = {
    "en": {
        "tts_provider": "ElevenLabs",
        "language": "en-US",
        "transcription_language": CR_TRANSCRIPTION_LANGUAGE_EN,
        "hints": CR_HINTS_EN,
        "welcome_greeting": "Welcome to Hatton Hills! I'm Kavya, how can I help you today?",
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


def conversation_relay_config(language: str) -> dict[str, str]:
    config = dict(LANGUAGE_CONFIGS[language])
    if language == "en":
        config["voice"] = load_kavya_english_voice_profile().twilio_composite_voice
    return config


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
STT_PRIMARY: dict[str, str] = {
    "en": "en-US", "si": "si-LK", "ta": "ta-IN", "ar": "ar-SA",
}
STT_ALTERNATIVES: dict[str, list[str]] = {
    "en": [],
    "si": ["en-US", "ta-IN"],
    "ta": ["en-US", "si-LK"],
    "ar": ["en-US"],
}

def _parse_endpointing_seconds(
    environ, name: str, default: float, minimum: float, maximum: float
) -> float:
    """Read a float endpointing duration from the environment, clamped.

    A missing, blank, or unparseable value falls back to the default; a value
    outside the range is clamped rather than rejected, so a mis-set knob can
    never arm a zero-length or runaway timer.
    """
    raw = environ.get(name, "")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    return min(max(value, minimum), maximum)


def _parse_clamped_float(
    environ, name: str, default: float, minimum: float, maximum: float
) -> float:
    """Read a float tuning knob from the environment, clamped."""
    raw = environ.get(name, "")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    return min(max(value, minimum), maximum)


_ECHO_NORMALIZE_PATTERN = re.compile(r"[^\w\s]+", re.UNICODE)
_ECHO_AFFIRMATION_TOKENS: set[str] = {"yes", "yeah", "no", "correct", "right", "okay"}
_REFERENCE_CONTEXT_PREFIX_PATTERN = re.compile(
    r"^\[Reference context:\s*.*?\]\s*(?:\r?\n\s*)*Guest:\s*",
    re.DOTALL,
)


def _normalize_for_overlap(text: str) -> str:
    """Lowercase and strip punctuation and duplicate spacing for scoring."""
    normalized = _ECHO_NORMALIZE_PATTERN.sub(" ", (text or "").lower())
    return re.sub(r"\s+", " ", normalized).strip()


def _starts_with_affirmation_token(text: str) -> bool:
    """Return whether the normalized text starts with an affirmation token."""
    tokens = _normalize_for_overlap(text).split()
    return bool(tokens) and tokens[0] in _ECHO_AFFIRMATION_TOKENS


def _token_overlap_ratio(reference: str, transcript: str) -> float:
    """Order-aware fraction of transcript tokens matching the reference."""
    reference_tokens = _normalize_for_overlap(reference).split()
    transcript_tokens = _normalize_for_overlap(transcript).split()
    if not reference_tokens or not transcript_tokens:
        return 0.0
    matcher = difflib.SequenceMatcher(None, reference_tokens, transcript_tokens)
    matched = sum(block.size for block in matcher.get_matching_blocks())
    return matched / len(transcript_tokens)


def _strip_reference_context(utterance: str) -> str:
    """Drop a ConversationRelay reference-context wrapper from user turns."""
    if not utterance:
        return utterance
    stripped = utterance.lstrip()
    if not stripped.startswith("[Reference context:"):
        return utterance.strip()
    if _REFERENCE_CONTEXT_PREFIX_PATTERN.match(stripped) is None:
        return utterance.strip()
    return _REFERENCE_CONTEXT_PREFIX_PATTERN.sub("", stripped).strip()


def _extract_last_user_utterance(conversation_history: list[dict[str, Any]]) -> str:
    """Find the latest user text in history, preferring the raw utterance."""
    for message in reversed(conversation_history):
        if not isinstance(message, dict):
            continue
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            return _strip_reference_context(content)
    return ""


_CONVERSATION_CAPTURE_TOOLS: set[str] = {
    "capture_spoken_number",
    "capture_spoken_name",
}


def _override_capture_spoken_argument(
    tool_name: str,
    tool_input: dict[str, Any],
    override_spoken: str,
    *,
    source: str,
) -> tuple[dict[str, Any], bool]:
    """Return tool args with spoken text replaced from the raw utterance."""
    if tool_name not in _CONVERSATION_CAPTURE_TOOLS:
        return tool_input, False

    if not isinstance(tool_input, dict):
        return tool_input, False

    model_spoken = tool_input.get("spoken")
    if model_spoken is None:
        model_spoken = ""
    elif not isinstance(model_spoken, str):
        model_spoken = str(model_spoken)

    if not override_spoken:
        return tool_input, False

    if override_spoken == model_spoken:
        return tool_input, False

    logger.info(
        "%s event=tool_arg_override tool=%s model_len=%d raw_len=%d",
        source,
        tool_name,
        len(model_spoken),
        len(override_spoken),
    )
    overridden = dict(tool_input)
    overridden["spoken"] = override_spoken
    return overridden, True


def _append_booking_confirmation_marker(
    transcript_sink: list[dict[str, str]],
    tool_name: str,
    tool_input: dict[str, Any],
    tool_result: str,
) -> None:
    if tool_name != "create_booking":
        return
    if not isinstance(tool_result, str):
        return
    try:
        parsed = json.loads(tool_result)
    except (json.JSONDecodeError, TypeError):
        return
    if not isinstance(parsed, dict):
        return

    success = parsed.get("success")
    if not success and success is not True:
        return

    guest_name = (parsed.get("guest_name") or tool_input.get("guest_name") or "").strip()
    if not guest_name:
        return
    marker_parts = [f"guest_name={guest_name}"]
    if parsed.get("booking_reference"):
        marker_parts.append(f"booking_reference={parsed.get('booking_reference')}")
    if parsed.get("room_type"):
        marker_parts.append(f"room_type={parsed.get('room_type')}")
    if parsed.get("check_in"):
        marker_parts.append(f"check_in={parsed.get('check_in')}")
    if parsed.get("check_out"):
        marker_parts.append(f"check_out={parsed.get('check_out')}")
    transcript_sink.append({
        "role": "system",
        "text": (
            "BOOKING CONFIRMED via create_booking: "
            + ", ".join(marker_parts)
        ),
    })


# Endpointing timers. A provider FINAL (Azure `recognized`) has already
# segmented the utterance, so it flushes after a short grace that only guards
# against a mid-thought continuation ("a room" ... "for next weekend"). The
# longer silence timer is the fallback for the interim-only path, where the
# provider (typically Google) never fires a final and we endpoint ourselves.
# Both are env-tunable and clamped.
ENDPOINTING_SILENCE: float = _parse_endpointing_seconds(
    os.environ, "STT_ENDPOINTING_SILENCE_SECONDS", 1.0, 0.2, 5.0
)
STT_FINAL_GRACE_SECONDS: float = _parse_endpointing_seconds(
    os.environ, "STT_FINAL_GRACE_SECONDS", 0.5, 0.05, 5.0
)
CAPTURE_ENDPOINTING_SILENCE_SECONDS: float = _parse_endpointing_seconds(
    os.environ, "CAPTURE_ENDPOINTING_SILENCE_SECONDS", 1.5, 0.5, 5.0
)
CAPTURE_FINAL_GRACE_SECONDS: float = _parse_endpointing_seconds(
    os.environ, "CAPTURE_FINAL_GRACE_SECONDS", 1.2, 0.2, 3.0
)
SMARTPBX_LANGUAGE_SELECTION_TIMEOUT_SECONDS = _parse_endpointing_seconds(
    os.environ,
    "SMARTPBX_LANGUAGE_SELECTION_TIMEOUT_SECONDS",
    8.0,
    3.0,
    20.0,
)
SMARTPBX_SINHALA_GEMINI_TTS_MODEL = os.getenv(
    "SMARTPBX_SINHALA_GEMINI_TTS_MODEL", "gemini-3.1-flash-tts-preview"
)
SMARTPBX_SINHALA_GEMINI_TTS_VOICE = os.getenv(
    "SMARTPBX_SINHALA_GEMINI_TTS_VOICE", "Vindemiatrix"
)
SMARTPBX_SINHALA_GEMINI_TTS_TIMEOUT_SECONDS = _parse_endpointing_seconds(
    os.environ,
    "SMARTPBX_SINHALA_GEMINI_TTS_TIMEOUT_SECONDS",
    15.0,
    3.0,
    30.0,
)

# Upper bound for the only integer the post-dispatch STT telemetry emits. The
# event records that a late provider result was ignored while a turn was already
# dispatched; the age of that turn is useful, an unbounded number is not. Not an
# env knob — it is a log-shape clamp, not a tuning parameter.
POST_DISPATCH_ELAPSED_MS_MAX: int = 60_000

# NOTE (PR #269 review): there is deliberately NO staleness window and no
# text-relationship comparison here any more. Deciding that a late result is the
# dispatched turn's own tail — rather than the caller immediately repeating or
# correcting themselves — requires provider result identity, and this pipeline
# has none: `GoogleSTTStream` and `AzureSTTStream` both hand their callbacks a
# bare `str`, and `GoogleSTTStream._stream_epoch` is an internal gRPC-swap fence
# that is identical in both cases. Matching text plus a short elapsed time is
# exactly what an immediate caller repetition looks like, so it proves nothing
# and must not delete speech.


def _has_material_text(text: str) -> bool:
    """True when the text carries at least one letter or digit."""
    return any(ch.isalnum() for ch in text)


def _log_smartpbx_pilot_transcript(role: str, phrase: str) -> None:
    """Log one explicitly enabled pilot phrase without any call identifier."""
    if not SMARTPBX_PILOT_TRANSCRIPT_LOGGING:
        return
    if role not in {"guest", "kavya"} or not isinstance(phrase, str) or not phrase:
        return
    # %r escapes embedded newlines/control characters into this one log record.
    logger.info("smartpbx_pilot_transcript role=%s text=%r", role, phrase)

# Domain phrase list that biases the ENGLISH Google and Azure recognizers toward
# booking vocabulary — digit words (phone numbers), the property and room names
# (from the single tools source of truth), and common booking terms. English only:
# phrase lists are language-specific and the Sinhala/Tamil/Arabic configs are left
# as the owner keeps them. Applied via Google SpeechContext and Azure
# PhraseListGrammar.
_STT_DIGIT_WORDS: tuple[str, ...] = (
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight",
    "nine", "oh", "double", "triple", "treble", "nought", "naught",
)
_STT_REPEAT_OPERANDS: tuple[str, ...] = (
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight",
    "nine", "oh", "o", "nought", "naught", "0", "1", "2", "3", "4", "5",
    "6", "7", "8", "9",
)
# Individual repeat words are not enough when the telephony recognizer has to
# decide whether the word binds to the following digit. Keep these phrases in
# the phone-number STT vocabulary; no free-form text is expanded here.
_STT_REPEAT_PHRASES: tuple[str, ...] = tuple(
    f"{repeat} {operand}"
    for repeat in ("double", "triple")
    for operand in _STT_REPEAT_OPERANDS
)
_STT_BOOKING_TERMS: tuple[str, ...] = (
    "Hatton Hills", "check-in", "check in", "check-out", "check out",
    "honeymoon", "anniversary", "half board", "full board", "bed and breakfast",
    "adults", "children", "child", "guests", "nights", "double room",
    "plunge pool", "king bed", "twin beds", "sea view", "mountain view",
    "breakfast", "dinner", "availability", "reservation", "booking",
)
EN_STT_PHRASE_LIST: tuple[str, ...] = (
    tuple(ROOM_TYPES_BY_PROPERTY[PROPERTY_HATTON])
    + _STT_BOOKING_TERMS
    + _STT_DIGIT_WORDS
    + _STT_REPEAT_PHRASES
)


def _parse_clamped_int(environ, name: str, default: int, minimum: int, maximum: int) -> int:
    """Read a clamped integer from the environment, falling back on the default."""
    raw = environ.get(name, "")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return min(max(value, minimum), maximum)


# DTMF keypad number capture. Keypad entry bypasses STT and is exact, so Kavya
# collects phone/WhatsApp/callback numbers this way. Timeouts are env-tunable and
# clamped so a mis-set knob cannot arm a zero or runaway wait.
DTMF_INTERDIGIT_TIMEOUT_SECONDS: float = _parse_endpointing_seconds(
    os.environ, "DTMF_INTERDIGIT_TIMEOUT_SECONDS", 6.0, 1.0, 30.0
)
DTMF_OVERALL_TIMEOUT_SECONDS: float = _parse_endpointing_seconds(
    os.environ, "DTMF_OVERALL_TIMEOUT_SECONDS", 30.0, 5.0, 120.0
)
DTMF_MAX_DIGITS: int = _parse_clamped_int(
    os.environ, "DTMF_MAX_DIGITS", 15, 1, 40
)

# Capture round counter so spoken-number/name collection does not stall the
# caller indefinitely after repeated short fragments. The value intentionally
# decays to end capture mode after bounded attempts.
CAPTURE_MODE_MAX_TURNS: int = _parse_clamped_int(
    os.environ, "CAPTURE_MODE_MAX_TURNS", 3, 1, 10
)

# Hard cap on the combined dictation held across provider finals. A phone number
# and a spelled name are both far under this; the cap only exists so a stuck
# capture episode (open mic, TV in the room) cannot grow an unbounded utterance
# and hand the model a wall of text. Env-tunable and clamped like its neighbours,
# so a mis-set knob can neither truncate a normal number nor lift the cap.
CAPTURE_BUFFER_MAX_CHARS: int = _parse_clamped_int(
    os.environ, "CAPTURE_BUFFER_MAX_CHARS", 600, 60, 4000
)

# Below this share of digit/letter-like tokens the combined utterance is not a
# dictation any more — the caller changed the subject — and capture mode ends.
# 0.0 makes capture mode never exit on content (only on success/turn cap); 1.0
# would demand a pure dictation, so both extremes stay reachable but clamped.
CAPTURE_DICTATION_MIN_RATIO: float = _parse_clamped_float(
    os.environ, "CAPTURE_DICTATION_MIN_RATIO", 0.3, 0.0, 1.0
)

# ---------------------------------------------------------------------------
# Capture-ask detection (arms capture mode BEFORE the caller answers)
# ---------------------------------------------------------------------------
# Callers dictate phone numbers and name spellings in two-to-four digit
# fragments: the recognizer commits a final at every natural pause, and each
# final used to cost a whole LLM turn (capture tool -> needs_more -> re-ask).
# Capture mode fixes the endpointing, but it only engaged AFTER the first
# capture tool call, so the opening fragments still paid full price.
#
# These patterns arm capture mode from Kavya's own ask, matched ONLY against
# sentences she actually delivered (see `_delivered_sentences`) — an ask cut off
# by a barge-in was never heard and must not pre-arm anything. The list is
# deliberately conservative: a false positive makes the following turns wait the
# patient capture timers, which reads as sluggish on ordinary conversation.
_CAPTURE_ASK_PATTERNS: tuple[re.Pattern[str], ...] = (
    # "your phone number", "a WhatsApp number", "the callback number"
    re.compile(
        r"\b(?:phone|mobile|cell|contact|whats\s?app|call\s?back|callback)\s+"
        r"(?:phone\s+)?number\b"
    ),
    # "may I have the number", "could you repeat the number"
    re.compile(
        r"\b(?:may|could|can|would|will)\s+(?:i|you|we)\b[^.?!]{0,60}\bnumber\b"
    ),
    # "what's your number", "what is the number"
    re.compile(r"\bwhat(?:'s|s| is)\s+(?:your|the)\b[^.?!]{0,40}\bnumber\b"),
    # spelling asks
    re.compile(r"\bhow\s+do\s+you\s+spell\b"),
    re.compile(r"\bspell\b[^.?!]{0,40}\b(?:it|that|your|the|out|for\s+me)\b"),
    # digit-wise asks and continuations
    re.compile(
        r"\b(?:digit\s+by\s+digit|one\s+digit\s+at\s+a\s+time|"
        r"remaining\s+digits|rest\s+of\s+the\s+(?:number|digits)|"
        r"(?:last|next|first)\s+(?:few\s+)?digits)\b"
    ),
    re.compile(r"\bgo\s+ahead\b[^.?!]{0,40}\b(?:number|digits)\b"),
)

# A read-back is not an ask. "So I have your phone number as oh seven seven..."
# matches the first pattern above, and re-arming capture mode on Kavya's own
# confirmation would keep a finished episode alive for another three turns.
_CAPTURE_ASK_SUPPRESS_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Possession, not a request: "I have your phone number", "so I have...",
    # "thank you, I have it". Anchored so the modal-fronted REQUEST form
    # ("may I have your mobile number?") is not swallowed with it.
    re.compile(
        r"(?:^|[,;]\s*|\b(?:and|so|then|okay|right|now)\s+)"
        r"i\s+(?:have|'ve\s+got|have\s+got|heard|noted)\b"
    ),
    re.compile(r"\bthat(?:'s|s| is)\s+(?:correct|right|noted|saved|confirmed)\b"),
    re.compile(r"\b(?:so|then)\s+your\b"),
    re.compile(r"\b(?:let\s+me|i'll|i\s+will)\s+(?:confirm|read)\b"),
    re.compile(r"\bnumber\s+(?:is|as|was)\b"),
    # "the number of guests/nights" is a count question, not a dictation.
    re.compile(
        r"\bnumber\s+of\s+(?:guests|people|adults|children|kids|nights|rooms)\b"
    ),
)

# Tokens that read as dictated digits or a spelled letter. Used only to decide
# whether an already-buffered capture utterance is still a dictation.
_CAPTURE_DICTATION_WORDS: frozenset[str] = frozenset({
    "zero", "oh", "o", "nought", "naught", "nil",
    "one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
    "double", "triple", "treble", "plus", "hundred", "dash", "hyphen", "dot",
})


def _is_capture_ask_sentence(sentence: str) -> bool:
    """True when one delivered sentence asks the caller to dictate."""
    text = " ".join((sentence or "").lower().split())
    if not text:
        return False
    if any(pattern.search(text) for pattern in _CAPTURE_ASK_SUPPRESS_PATTERNS):
        return False
    return any(pattern.search(text) for pattern in _CAPTURE_ASK_PATTERNS)


def _detect_capture_ask(sentences: Any) -> bool:
    """True when any delivered sentence of the turn asked for a dictation."""
    if not sentences:
        return False
    return any(_is_capture_ask_sentence(sentence) for sentence in sentences)


def _capture_dictation_ratio(text: str) -> float:
    """Share of tokens in ``text`` that look like dictated digits or letters."""
    tokens = re.findall(r"[a-z0-9]+", (text or "").lower())
    if not tokens:
        return 0.0
    hits = 0
    for token in tokens:
        if token.isdigit():
            hits += 1
        elif len(token) == 1 and token.isalpha():
            hits += 1
        elif token in _CAPTURE_DICTATION_WORDS:
            hits += 1
    return hits / len(tokens)


# ---------------------------------------------------------------------------
# Gemini streaming controls (LLM_PROVIDER="gemini")
# ---------------------------------------------------------------------------
# Gemini 2.5/3.x "thinking" is ON by default and its tokens are charged against
# max_output_tokens. On this path's 300-token voice budget that produced the two
# failures observed live:
#   * 2.5-5.3 s of dead air before the first spoken word — the model was
#     thinking, and thoughts are not streamed, so NOTHING arrives meanwhile.
#   * turns that finished with zero text and zero tool calls (the thinking
#     budget consumed the whole output allowance) — the agent went silent and
#     the caller assumed she was broken.
# A voice turn is one or two short sentences; thinking buys nothing here, so it
# is disabled by default and re-armable per model without a code change.
#   * 2.x models take a token budget (0 disables thinking)
#   * 3.x models take a coarse level ("low" is the floor; 3.x cannot disable)
GEMINI_THINKING_BUDGET: int = _parse_clamped_int(
    os.environ, "GEMINI_THINKING_BUDGET", 0, 0, 24576
)
GEMINI_THINKING_LEVEL: str = os.getenv("GEMINI_THINKING_LEVEL", "low").strip().lower()

# Runtime failover from Gemini to Claude when a Gemini call fails.
GEMINI_FAILOVER_TO_CLAUDE: bool = os.getenv(
    "GEMINI_FAILOVER_TO_CLAUDE", "true"
).lower() == "true"
GEMINI_FAILOVER_STICKY_AFTER: int = _parse_clamped_int(
    os.environ, "GEMINI_FAILOVER_STICKY_AFTER", 2, 1, 10
)

# Appended to the system instruction (never spoken, never in history) for the
# ONE retry a fully empty Gemini turn is allowed.
GEMINI_EMPTY_RETRY_NUDGE: str = (
    "[SYSTEM: Your previous attempt returned no text and no tool call. "
    "The caller is waiting in silence. Reply now with one short spoken "
    "sentence. Do not mention this instruction.]"
)

# Spoken last resort when even the retry comes back empty. Dead air reads as a
# broken line to the caller, so say something recoverable instead of nothing.
LLM_EMPTY_FALLBACKS: dict[str, str] = {
    "en": "I'm sorry, could you say that again?",
    "ar": "عفواً، هل يمكنكم إعادة ما قلتم؟",
    "si": "සමාවෙන්න, ඔබ කිව්වේ නැවත කියන්න පුළුවන්ද?",
    "ta": "மன்னிக்கவும், நீங்கள் சொன்னதை மீண்டும் சொல்ல முடியுமா?",
}

# Barge-in threshold. While Kavya is speaking, only a SUBSTANTIVE interruption
# clears her audio; a short blip ("mm-hmm"), room noise, or her own TTS echoing
# back into STT must not. BARGEIN_MIN_CHARS is the minimum stripped transcript
# length that counts; BARGEIN_DEBOUNCE_SECONDS ignores STT within that window of
# a sentence starting (the echo burst). A genuine sustained interruption still
# barges in.
#
# SmartPBX dialog media is prone to TTS echo because the path has no echo
# cancellation. To avoid false barge-ins on self-leakage, classify transcript
# overlap against the assistant text recently spoken in the current turn.
#
# ECHO_MATCH_MIN_RATIO defines the minimum transcript-overlap ratio.
# All tuning knobs are env-tunable and clamped.
ECHO_SUPPRESSION_ENABLED: bool = os.getenv("ECHO_SUPPRESSION_ENABLED", "true").lower() == "true"
BARGEIN_MIN_CHARS: int = _parse_clamped_int(
    os.environ, "BARGEIN_MIN_CHARS", 12, 0, 200
)
BARGEIN_DEBOUNCE_SECONDS: float = _parse_endpointing_seconds(
    os.environ, "BARGEIN_DEBOUNCE_SECONDS", 0.6, 0.0, 5.0
)
ECHO_MATCH_MIN_RATIO: float = _parse_clamped_float(
    os.environ, "ECHO_MATCH_MIN_RATIO", 0.8, 0.5, 1.0
)
ECHO_SENTENCE_COVERAGE_MIN_RATIO: float = 0.6

# Bounds on restarting a failed STT stream. Google caps a streaming_recognize
# call at ~5 minutes, so healthy restarts are normal and must stay cheap; a
# persistent failure (rotated credentials, quota) must not become a tight spin.
STT_RESTART_BACKOFF_BASE: float = 0.25
STT_RESTART_BACKOFF_MAX: float = 5.0
STT_MAX_CONSECUTIVE_FAILURES: int = 8
# Inbound audio waiting for the STT worker. Bounded so a stopped or wedged
# consumer cannot grow it for the length of the call.
STT_QUEUE_MAX_CHUNKS: int = 1000

# Google terminates a single streaming_recognize at 305 SECONDS OF AUDIO with a
# 400. Discovering that ceiling by hitting it is not survivable: observed live
# on 2026-08-11, a call went deaf from 11:07 to 11:10 after the 400 because the
# dead stream's request generator kept draining the shared audio queue while the
# replacement stream starved. So the stream is now rotated BEFORE the ceiling,
# preferring a gap in the caller's speech so the swap cannot cut a word in half,
# with a hard deadline that rotates regardless if the caller never pauses.
STT_STREAM_ROTATE_SECONDS: float = _parse_endpointing_seconds(
    os.environ, "STT_STREAM_ROTATE_SECONDS", 240.0, 30.0, 300.0
)
STT_STREAM_ROTATE_DEADLINE_SECONDS: float = _parse_endpointing_seconds(
    os.environ, "STT_STREAM_ROTATE_DEADLINE_SECONDS", 285.0, 60.0, 304.0
)
STT_ROTATE_QUIET_SECONDS: float = _parse_endpointing_seconds(
    os.environ, "STT_ROTATE_QUIET_SECONDS", 0.3, 0.0, 3.0
)
# Audio that arrives while no stream is accepting is buffered and replayed into
# the replacement, bounded to roughly ten seconds of Twilio's 20 ms mulaw frames.
# Beyond that the OLDEST frames are dropped: replaying a deep backlog would push
# the fresh stream toward its own 305 s audio ceiling and leave the recognizer
# minutes behind the live caller.
STT_SWAP_BUFFER_CHUNKS: int = _parse_clamped_int(
    os.environ, "STT_SWAP_BUFFER_CHUNKS", 500, 50, STT_QUEUE_MAX_CHUNKS
)

# Google's `telephony` model is trained on 8 kHz narrowband phone audio, which
# is exactly what Twilio and Dialog deliver. It is applied to ENGLISH ONLY:
# coverage for si-LK / ta-IN / ar-SA is not something this repo can verify, and
# a rejected model would take a whole language down. Any rejection degrades to
# the default model once, permanently, rather than failing the call.
STT_GOOGLE_MODEL_EN: str = os.getenv("STT_GOOGLE_MODEL_EN", "telephony")
# Speech adaptation boost for the booking-domain phrase list (English only).
STT_ADAPTATION_BOOST: float = _parse_clamped_float(
    os.environ, "STT_ADAPTATION_BOOST", 15.0, 0.0, 20.0
)
# Boost for the digit-class speech context appended to English streams. Set to
# ``0`` to disable the context entirely.
STT_DIGIT_CLASS_BOOST: float = _parse_clamped_float(
    os.environ, "STT_DIGIT_CLASS_BOOST", 4.0, 0.0, 20.0
)
STT_MAX_ADAPTATION_PHRASES: int = 500

# ElevenLabs streaming latency optimisation. Higher values return the first audio
# frame sooner at some cost to prosody; 3 is the useful ceiling for conversational
# telephony (4 additionally disables text normalisation, which mangles spoken
# numbers and prices — exactly what this agent reads out). 0 omits the parameter
# entirely and takes ElevenLabs' own default.
ELEVENLABS_OPTIMIZE_STREAMING_LATENCY: int = _parse_clamped_int(
    os.environ, "ELEVENLABS_OPTIMIZE_STREAMING_LATENCY", 3, 0, 4
)


def _elevenlabs_stream_url(voice_id: str) -> str:
    """Streaming TTS URL for a voice, including the latency knob when armed."""
    url = ELEVENLABS_TTS_URL.format(voice_id=voice_id) + "?output_format=ulaw_8000"
    if ELEVENLABS_OPTIMIZE_STREAMING_LATENCY:
        url += f"&optimize_streaming_latency={ELEVENLABS_OPTIMIZE_STREAMING_LATENCY}"
    return url


_SMARTPBX_WELCOME_FADE_SAMPLES = 480  # 60 ms at 8 kHz
_SMARTPBX_WELCOME_MATERIAL_PCM_ABS = 256
_SMARTPBX_WELCOME_AUDIO_CACHE_MAX_ENTRIES = 8
_SMARTPBX_WELCOME_AUDIO_CACHE: OrderedDict[tuple[object, ...], bytes] = OrderedDict()
_SMARTPBX_WELCOME_AUDIO_LOCK = threading.Lock()


def _smartpbx_welcome_audio_cache_key(
    text: str,
    url: str,
    model_id: str,
    voice_settings: Mapping[str, object],
) -> tuple[object, ...]:
    """Return the complete canonical ElevenLabs request identity for welcome bytes."""
    return (
        text,
        url,
        model_id,
        tuple(sorted((str(name), repr(value)) for name, value in voice_settings.items())),
    )


def _fade_smartpbx_welcome_mulaw(raw: bytes) -> bytes:
    """Fade only the first material 60 ms of 8 kHz μ-law, or return raw on failure."""
    if audioop is None or not raw:
        return raw
    try:
        pcm = audioop.ulaw2lin(raw, 2)
        onset = next(
            (
                offset // 2
                for offset in range(0, len(pcm) - 1, 2)
                if abs(int.from_bytes(pcm[offset:offset + 2], "little", signed=True))
                >= _SMARTPBX_WELCOME_MATERIAL_PCM_ABS
            ),
            None,
        )
        if onset is None:
            return raw
        fade_samples = min(_SMARTPBX_WELCOME_FADE_SAMPLES, len(raw) - onset)
        faded_pcm = bytearray()
        for index in range(fade_samples):
            sample_offset = (onset + index) * 2
            sample = int.from_bytes(pcm[sample_offset:sample_offset + 2], "little", signed=True)
            scale = index / (_SMARTPBX_WELCOME_FADE_SAMPLES - 1)
            faded_pcm.extend(round(sample * scale).to_bytes(2, "little", signed=True))
        faded_mulaw = audioop.lin2ulaw(bytes(faded_pcm), 2)
        return raw[:onset] + faded_mulaw + raw[onset + fade_samples:]
    except Exception:
        return raw


async def _get_cached_smartpbx_welcome_audio(
    key: tuple[object, ...], fetch: Callable[[], Awaitable[bytes]],
) -> bytes:
    """Fetch per caller, then atomically publish or reuse immutable welcome bytes."""
    with _SMARTPBX_WELCOME_AUDIO_LOCK:
        cached = _SMARTPBX_WELCOME_AUDIO_CACHE.get(key)
        if cached is not None:
            _SMARTPBX_WELCOME_AUDIO_CACHE.move_to_end(key)
            return cached

    raw = await fetch()
    if not isinstance(raw, bytes):
        raise TypeError("ElevenLabs welcome audio must be bytes")
    if not raw:
        raise _ElevenLabsWelcomeEmptyAudio()
    processed = _fade_smartpbx_welcome_mulaw(raw)
    with _SMARTPBX_WELCOME_AUDIO_LOCK:
        cached = _SMARTPBX_WELCOME_AUDIO_CACHE.get(key)
        if cached is not None:
            _SMARTPBX_WELCOME_AUDIO_CACHE.move_to_end(key)
            return cached
        _SMARTPBX_WELCOME_AUDIO_CACHE[key] = processed
        _SMARTPBX_WELCOME_AUDIO_CACHE.move_to_end(key)
        while len(_SMARTPBX_WELCOME_AUDIO_CACHE) > _SMARTPBX_WELCOME_AUDIO_CACHE_MAX_ENTRIES:
            _SMARTPBX_WELCOME_AUDIO_CACHE.popitem(last=False)
    return processed


class _ElevenLabsWelcomeHTTPStatus(Exception):
    """The greeting fetch already recorded its precise provider failure."""


class _ElevenLabsWelcomeEmptyAudio(Exception):
    """A successful provider status without greeting audio is not delivery."""

# Silence (seconds) after greeting / agent turn before we re-prompt the caller.
# If the caller never speaks, we re-greet them or ask if they're still online,
# up to MAX_REPROMPTS times. After that we stop re-prompting.
SILENCE_REPROMPT_DELAY: float = 18.0
MAX_REPROMPTS: int = 1

# Re-prompt messages spoken when caller is silent. Index 0 = first nudge,
# index 1 = full re-greet on second silence.
REPROMPT_MESSAGES: dict[str, list[str]] = {
    "en": [  # English
        "Hello, are you still there?",
        "Welcome to Hatton Hills. How may I help you today?",
    ],
    "ar": [  # Arabic (MSA)
        "\u0645\u0631\u062d\u0628\u0627\u064b\u060c \u0647\u0644 \u0645\u0627 \u0632\u0644\u062a\u0645 \u0639\u0644\u0649 \u0627\u0644\u062e\u0637\u061f",
        "\u0623\u0647\u0644\u0627\u064b \u0628\u0643\u0645 \u0641\u064a Hatton Hills. \u0643\u064a\u0641 \u064a\u0645\u0643\u0646\u0646\u064a \u0645\u0633\u0627\u0639\u062f\u062a\u0643\u0645 \u0627\u0644\u064a\u0648\u0645\u061f",
    ],
    "si": [  # Sinhala
        "\u0d86\u0dba\u0dd4\u0db6\u0ddd\u0dc0\u0db1\u0dca, \u0d94\u0db6 \u0dad\u0dc0\u0db8\u0dad\u0dca \u0dc3\u0dd2\u0da7\u0dd2\u0db1\u0dca\u0db1\u0dda\u0daf?",
        "\u0d86\u0dba\u0dd4\u0db6\u0ddd\u0dc0\u0db1\u0dca! Hatton Hills \u0dc0\u0dd9\u0dad \u0dc3\u0dcf\u0daf\u0dbb\u0dba\u0dd9\u0db1\u0dca \u0db4\u0dd2\u0dc5\u0dd2\u0d9c\u0db1\u0dd2\u0db8\u0dd4. \u0db8\u0da7 \u0d94\u0db6\u0da7 \u0d9a\u0dd9\u0dc3\u0dda \u0d8b\u0daf\u0dc0\u0dca \u0d9a\u0dc5 \u0dc4\u0dd0\u0d9a\u0dd2\u0daf?",
    ],
    "ta": [  # Tamil
        "\u0bb5\u0ba3\u0b95\u0bcd\u0b95\u0bae\u0bcd, \u0ba8\u0bc0\u0b99\u0bcd\u0b95\u0bb3\u0bcd \u0b87\u0ba9\u0bcd\u0ba9\u0bc1\u0bae\u0bcd \u0b87\u0bb0\u0bc1\u0b95\u0bcd\u0b95\u0bbf\u0bb1\u0bc0\u0bb0\u0bcd\u0b95\u0bb3\u0bbe?",
        "\u0bb5\u0ba3\u0b95\u0bcd\u0b95\u0bae\u0bcd! Hatton Hills \u0b95\u0bcd\u0b95\u0bc1 \u0bb5\u0bb0\u0bb5\u0bc7\u0bb1\u0bcd\u0b95\u0bbf\u0bb1\u0bcb\u0bae\u0bcd. \u0ba8\u0bbe\u0ba9\u0bcd \u0b89\u0b99\u0bcd\u0b95\u0bb3\u0bc1\u0b95\u0bcd\u0b95\u0bc1 \u0b8e\u0baa\u0bcd\u0baa\u0b9f\u0bbf \u0b89\u0ba4\u0bb5\u0bb2\u0bbe\u0bae\u0bcd?",
    ],
}

# Welcome greetings for Media Streams (spoken via ElevenLabs/Azure TTS on stream start)
MEDIA_STREAM_WELCOME: dict[str, str] = {
    "ar": (
        "أهلاً وسهلاً بكم في Hatton Hills! "
        "أنا كافيا، كيف يمكنني مساعدتكم اليوم؟"
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
            "- If the guest explicitly asks for a human, agent, manager, or real person, immediately acknowledge and transfer in the same turn with one clear sentence. Call transfer_to_human with a one-sentence reason. Do NOT promise a callback; the tool handles the live transfer.\n"
            "- Do not ask for separate confirmation if the request is explicit. Ask one clarifying question only if the request is ambiguous.\n"
            "- If you do NOT know the answer to a guest's question, or the request is outside what you can help with (e.g. complex booking changes, special packages, complaints, anything not covered by Hatton Hills booking/general info), PROACTIVELY offer transfer and take the next obvious handoff step.\n"
            "- Some requests are things you personally cannot finalise but a human team member CAN arrange - for example discounts, special rates, price negotiation, long-stay or off-season deals, or booking the whole property, a large group, a buyout, a wedding, or a corporate event. Do NOT simply refuse or say 'no, we don't do that', even if the hotel information would let you answer with a flat no. Treat these as handoff opportunities: briefly and warmly acknowledge the request, then proactively connect them.\n"
            "- When the obvious next step after a repeated failure is available (for example, after repeated availability misses or repeated tool/understanding friction), do the obvious next step instead of waiting to be asked again.\n"
            "- Do NOT guess or make up answers just to avoid a transfer. Honesty + a quick handoff offer beats a wrong answer.\n\n"
        )
    else:
        handoff_rules = ""

    # Rates. Two mutually exclusive regimes, switched by DEMO_RATES_ENABLED
    # (see yanolja_service.DEMO_NIGHTLY_RATE_USD). Demo mode lets Kavya quote
    # the demo rate card for client demonstrations; the default-off
    # regime is the original "we publish no rates" behaviour. The KB carries
    # matching wording, so flip both together — the env var is the kill switch.
    if yanolja_service.DEMO_RATES_ENABLED:
        rates_rules = (
            "RATES AND PRICING:\n"
            "- Every room-rate sentence must include the meal plan and value: "
            "half board with breakfast and dinner included, NET AND INCLUSIVE OF "
            "ALL TAXES and all service charge. For every rate you quote, include "
            "the words 'per room per night' and the currency.\n"
            "- The room rate you quote is the FINAL room rate. Never add a "
            "service charge or a tax on top of it, never say 'plus service "
            "charge' or 'plus taxes', and never imply the guest will pay more "
            "than the figure you gave. If a guest asks whether taxes or "
            "service charge are extra, say plainly that the rate already "
            "includes both.\n"
            "- You MAY quote the rates given in the hotel information in "
            "context, and the rate returned by the check_availability tool.\n"
            "- For foreign guests, quote the foreign-guest USD examples as words: "
            "'the Forest Escape Suite is seven hundred US dollars per room per "
            "night' and 'the Mount Monarch Chalet is one thousand four hundred "
            "US dollars per room per night'.\n"
            "- For Sri Lankan residents, quote resident rates, such as "
            "'the Forest Escape Suite is one hundred and fifty eight thousand "
            "rupees per room per night on half board with breakfast and dinner "
            "included, net and inclusive of all taxes and service charge'.\n"
            "- State rates plainly and confidently. Do NOT hedge, and do NOT call a "
            "rate indicative, approximate, provisional or subject to change.\n"
            "- NEVER invent a rate for anything not priced in your context. If "
            "asked the price of an upgrade, supplement, experience, transfer, "
            "meal or package that is not listed, say you do not have that "
            "figure to hand and offer the reservations number: plus nine four, "
            "seven seven, two two zero, four four zero zero.\n"
            "- For discounts, negotiated rates, long-stay or off-season deals, "
            "do NOT invent a number — treat it as a handoff opportunity.\n\n"
        )
    else:
        rates_rules = (
            "RATES AND PRICING — ABSOLUTE RULE:\n"
            "- Hatton Hills rate quoting is disabled in this configuration. Rates "
            "exist only once "
            "specific check-in and check-out dates are chosen, and they are "
            "served live by our booking system. You therefore have NO rate "
            "information.\n"
            "- NEVER state, estimate, guess, approximate, compare or imply any "
            "price, rate, amount, currency figure, discount percentage, or "
            "'from' price — for a room, a package, an upgrade, a supplement, "
            "or an experience. Any number you produce would be invented.\n"
            "- If the caller asks about price, rates, cost, or value for money, "
            "say that rates depend on the exact dates and that our reservations "
            "team will confirm them, and give the reservations number: plus "
            "nine four, seven seven, two two zero, four four zero zero. "
            "Offer to take their dates so the team can come back with the "
            "rate.\n"
            "- If the caller asks about current offers, promotions, seasonal "
            "deals or packages, do NOT describe any offer. Say you would rather "
            "have reservations confirm what is running for their dates, and "
            "give the reservations number.\n\n"
        )

    # Three inline clauses elsewhere in the prompt also assert "you have no
    # rate". They must switch with the regime above or they contradict it and
    # Claude follows the stricter one.
    if yanolja_service.DEMO_RATES_ENABLED:
        tool_rate_clause = (
            "whether it is available and its nightly rate; never "
            "quote a capacity or feature"
        )
        avail_price_clause = (
            "Whenever you do name a room, give its nightly rate with it, in "
            "the currency that matches the guest's residency: Sri Lankan "
            "residents in LKR, foreign guests in US dollars. In every quoted "
            "rate sentence include half board with breakfast and dinner, and "
            "say 'net and inclusive of all taxes and service charge.' "
            "State it as per room per night.\n"
        )
        rate_press_clause = (
            "- If the guest asks for a total, multiply the nightly rate by "
            "the number of nights and state it plainly. For anything you have "
            "no figure for, offer reservations on plus nine four, seven seven, "
            "two two zero, four four zero zero.\n"
        )
    else:
        tool_rate_clause = (
            "whether it is available; never quote a rate, capacity, or feature"
        )
        avail_price_clause = "Do NOT quote any price — you have none."
        rate_press_clause = (
            "- If the guest presses for a rate at any "
            "point, hold the line: rates are date-specific and confirmed by "
            "reservations on plus nine four, seven seven, two two zero, "
            "four four zero zero.\n"
        )

    # The welcome greeting is delivered by Twilio (English ConversationRelay) or
    # spoken on stream start (Media Streams). Tell Claude not to repeat it,
    # without asserting an English greeting for non-English callers.
    if lang == "en":
        greeting_note = (
            "The caller has already heard your greeting: 'Welcome to Hatton "
            "Hills! I'm Kavya, how can I help you today?' — do NOT "
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
        f"You are Kavya, the warm and gracious reservations voice agent for "
        f"Hatton Hills, a luxury boutique eco retreat set in an eight-acre "
        f"private forest in Sri Lanka's central hill country.\n"
        f"Hatton Hills is a SINGLE property. There is no second hotel, no other "
        f"branch and no other location, so never ask the caller which property "
        f"or which location they mean.\n"
        f"It has exactly five room types: the Forest Escape Suite, the Eco "
        f"Harmony Suite and the Sunrise Vista Premium Suite each sleep up to two "
        f"guests; the Mount Luxe Chalet and the Mount Monarch Chalet each sleep "
        f"up to five guests. The Mount Monarch Chalet is the flagship and the "
        f"only one with a private plunge pool.\n"
        f"Every stay is half board, with breakfast and dinner included.\n"
        f"Reservations hotline: plus nine four, seven seven, two two zero, four "
        f"four zero zero.\n"
        f"Today's date is {today}.\n\n"

        + greeting_note
        + language_rules +
        handoff_rules +

        "VOICE RULES (you are speaking on a phone call, not writing text):\n"
        "- Keep every response to one or two short sentences.\n"
        "- Never use markdown, bullet points, numbered lists, asterisks, or URLs.\n"
        "- Use natural spoken language. Say numbers as words.\n"
        "- Do not use abbreviations or acronyms. Spell things out in words.\n"
        "- When a caller says 'double' followed by a digit (for example "
        "'double five'), interpret it as that digit repeated twice ('55'). "
        "Likewise 'triple seven' (or 'treble seven') means '777', and 'nought' "
        "or 'naught' means zero ('0'), as do 'oh' and 'o'. This is common when "
        "callers read out phone numbers. But for a phone/WhatsApp/callback "
        "number you do NOT expand these yourself — pass the caller's words "
        "verbatim to capture_spoken_number and let it produce the digits. Apply "
        "the same rule if the equivalent word is said in Sinhala or Tamil.\n"
        "- Pause naturally between ideas by using short sentences.\n"
        "- Ask ONE question at a time. Never combine two questions in a single "
        "turn (for example a clarification AND an offer to transfer), because "
        "the guest's 'yes' then answers only one of them and you have to ask "
        "again. Ask the more important one, wait for the answer, then ask the "
        "next.\n"
        "- Use warm, conversational phrasing with brief empathetic acknowledgments "
        "such as 'I completely understand' and 'Of course, happy to help with "
        "that.'\n"
        "- Use natural transitions and punctuation for delivery pacing, including "
        "em-dashes for a short beat.\n"
        "- Use contractions naturally so the voice sounds human and unforced.\n"
        "- Use exclamation marks only for genuine delight, not as filler.\n"
        "- Never emit bracketed stage directions or audio tags (for example "
        "'[warmly]' or '[laughs]') or asterisk actions.\n\n"

        "THE ROOM TYPES AT HATTON HILLS:\n"
        "- There is ONE property. Never ask which property, which hotel or "
        "which location the guest means, and never mention any other hotel.\n"
        "- The room vocabulary is fixed and there are exactly five:\n"
        "    * Forest Escape Suite - up to two guests.\n"
        "    * Eco Harmony Suite - up to two guests.\n"
        "    * Sunrise Vista Premium Suite - up to two guests.\n"
        "    * Mount Luxe Chalet - up to five guests.\n"
        "    * Mount Monarch Chalet - up to five guests, with a private plunge "
        "pool. There is only one of these.\n"
        "- Always use the FULL room name when you confirm or book, for example "
        "'the Mount Monarch Chalet', not 'the Monarch'. Booking needs the exact "
        "name.\n"
        "- If a guest asks for a room type that does not exist here, say so "
        "plainly and offer the closest of the five. Never invent a room type.\n"
        "- If a guest asks for a 'double room', explain that every room is a "
        "private standalone unit and recommend one of the three suites for two "
        "guests. For three or more guests, recommend one of the two chalets.\n\n"

        + rates_rules +

        "DATA SECURITY — ANSWER THIS CONFIDENTLY, NEVER DEFLECT IT:\n"
        "- This reservations line is an AI voice agent built and operated by "
        "TaskForce AI. If asked whether you are a real person, say plainly that "
        "you are an AI reservations agent for Hatton Hills, built by TaskForce "
        "AI. Never claim to be human.\n"
        "- Callers evaluating this system are often hotel owners or managers, "
        "and data security is their biggest worry. When they raise security, "
        "privacy, confidentiality, where data is stored, or who can see it, "
        "answer it directly and reassuringly. Do NOT brush it off, do NOT say "
        "you do not have that information, and do NOT immediately try to "
        "transfer them — you DO have the answer.\n"
        "- Lead with this: all data is encrypted, both in transit and at rest, "
        "and guest information is held in access-controlled systems that are "
        "not open to the public internet.\n"
        "- Then make the most important point, which is role-based access "
        "control: access to sensitive data at TaskForce AI is granted by role "
        "and demonstrated need, never by default and never on the basis of "
        "seniority. There is no general staff access to guest data and no "
        "shared administrative accounts. Say this plainly; it is the single "
        "most reassuring fact you have.\n"
        "- NEVER name an individual employee, and NEVER say how many people "
        "have access. Role-based access control is the whole answer.\n"
        "- Also true and worth saying: the hotel remains the owner of its own "
        "data, guest data is used only for that hotel's own reservations and "
        "guest care, and TaskForce AI guarantees that guest data is never "
        "sold, and never shared with anyone outside the service of those "
        "reservations.\n"
        "- Do NOT invent a certification, an audit, a compliance standard, a "
        "data-centre location or a specific technology. If pressed on "
        "something not listed above, say you will have the team confirm the "
        "detail rather than guessing.\n"
        "- Say 'role-based access control' in full words the first time. Do not "
        "say the acronym on its own without explaining it.\n\n"

        "IMPORTANT RULES:\n"
        "- For general questions about room types, amenities, policies, "
        "activities, or hotel info, answer directly from the hotel information "
        "provided in context. Do NOT ask "
        "for dates or call any tool for general info questions. Pricing is the "
        "one exception — see the rates rule above.\n"
        "- If the hotel information in context does not contain the answer, "
        "say you do not have it to hand rather than inventing an amenity, a "
        "room count, a distance, a duration, a capacity, or a policy.\n"
        "- Only use the check_availability tool when the guest wants to actually "
        "BOOK a room or specifically asks if rooms are available on certain dates.\n"
        "- PHONE / WHATSAPP / CALLBACK NUMBERS — CAPTURE THE SPOKEN WORDS, LET "
        "THE TOOL DO THE DIGITS: when you need the guest's phone, WhatsApp, or "
        "callback number, ask them to say it and then call the "
        "capture_spoken_number tool, passing what they said EXACTLY as they said "
        "it — words and all, e.g. 'nought seven six triple seven double five "
        "one'. Do NOT convert double, triple, treble or nought into digits "
        "yourself and never do the digit arithmetic in your head; that is the "
        "tool's job. Each tool call is ONE complete attempt: read back only "
        "when the status is `captured`, using the tool's `readback` digits (for "
        "example, 'I've got 0 7 6 7 7 7 5 5 1 0 — is that right?'). If the "
        "status is anything else the attempt was discarded — ask the guest to "
        "say their WHOLE number again from the start and pass those new words "
        "verbatim. Never add digits to an earlier attempt, and never apply a "
        "correction such as 'double six at the end' to a number you already "
        "have; ask for the complete number instead. Only after at least two "
        "full failed spoken "
        "attempts, or if the guest is struggling or asks to use the keypad, "
        "offer collect_number_via_keypad as a fallback. The fallback may be "
        "offered only after `attempts >= 2`.\n"
        "- When a guest expresses booking intent, collect only what is needed to "
        "check availability: check-in and check-out dates, and number of guests "
        "(adults and children with ages). Ask ONE question at a time. Do NOT "
        "ask for residency, the guest's name, mobile, or email at this stage. "
        "Do NOT ask for any salutation or title (no Mr / Mrs / Ms / Dr).\n"
        "- CHILDREN UNDER 11: if the guest already stated the party is "
        "only adults (e.g. '2 adults', 'just the two of us'), do NOT ask "
        "again about children — accept it and move on with num_children=0. "
        "Only ask 'Are there any children under eleven in your party?' if "
        "the guest gave an ambiguous count (e.g. '4 people' without "
        "specifying adults vs children). Children under 11 affect pricing, "
        "so if there is genuine ambiguity you must clarify, but never "
        "repeat a question the guest already answered.\n"
        "- ROOM COUNT IMPLIES OCCUPANCY — DO NOT ASK FOR A HEADCOUNT YOU "
        "CAN ALREADY WORK OUT: a 'double room' means double occupancy, i.e. "
        "two guests. If the guest states a number of rooms by occupancy "
        "(e.g. 'two double rooms'), infer the total guests yourself rather "
        "than asking 'how many guests in total' — two double rooms is four "
        "adults. Briefly confirm the figure you derived instead of asking "
        "open-endedly, e.g. 'That's four adults across two double rooms — "
        "is that right?'. Only ask for an explicit guest count when it is "
        "genuinely ambiguous — for example a villa or suite where the "
        "party size is not implied.\n"
        "- KB IS THE SOURCE OF TRUTH FOR ROOM FACTS: the booking system "
        "(via the check_availability tool) is used ONLY to find out which "
        "rooms are free for the requested dates, and later to create the "
        "booking. EVERYTHING ELSE — capacity, descriptions, amenities, "
        "policies — comes from the hotel information in context (the "
        "knowledge base). The tool result only tells you the room name and "
        f"{tool_rate_clause} "
        "from the tool. Only ever use the five Hatton Hills room names: "
        "Forest Escape Suite, Eco Harmony Suite, Sunrise Vista Premium "
        "Suite, Mount Luxe Chalet and Mount Monarch Chalet.\n"
        "- AVAILABILITY CHECK — STRICT SINGLE-CALL RULE: as soon as you "
        "have the dates and the pax, call check_availability "
        "EXACTLY ONCE. "
        "NEVER pass a room_type filter, even if the guest already "
        "mentioned a room they like — the tool returns ALL room types in "
        "one response. After that single call, read the response. If the "
        "guest has ALREADY named the room they want, just confirm THAT room "
        "is free and move straight on to booking, e.g. 'The Mount Monarch "
        "Chalet is available for those dates.' Do NOT re-list the other room "
        "types and do NOT ask again which room they want. Only when the "
        "guest has not yet chosen a room do you surface every available "
        "type in one sentence, e.g. 'The Forest Escape Suite and the Eco "
        "Harmony Suite are available for those dates — which would "
        "you prefer?' "
        "Calling check_availability a second time in the "
        "same booking flow (e.g. once per room type, or because the "
        "guest changed their mind) is FORBIDDEN unless the guest changes "
        "their dates or pax. If the guest just picks a different room "
        "from the list you already have, do NOT call the tool again — "
        "you already know the answer.\n"
        "- NEVER re-ask a question the guest has already answered, and never "
        "re-list options they have already chosen from. If they have named "
        "their room, treat it as settled: confirm it is available and "
        "proceed. Only ask which room they would like when they genuinely "
        f"have not said. {avail_price_clause}\n"
        "- RESIDENCY QUESTION — ASK ONLY WHEN QUOTING PRICES: once the guest "
        "has picked a room, ask whether they are a Sri Lankan resident or a "
        "foreign guest before quoting a rate. Ask it once and once only per "
        "booking. Do not ask again, and do not change the answer in the same "
        "call. Anchor every rate, every supplement, and every currency mention "
        "to their residency: foreign guest = US dollars, Sri Lankan resident = "
        "rupees.\n"
        "- If the guest has told you this is a two-night or longer stay, "
        "proactively mention once that guided nature walk, guided birding tour "
        "and stargazing are complimentary. Mention this during room / rate "
        "collection, not only after all booking details are already final.\n"
        + rate_press_clause +
        "- Once the guest has picked a room and confirmed they are happy "
        "to proceed, begin collecting their personal details, ONE "
        "question at a time, in this order: full name (no salutation), "
        "then mobile number. Do NOT ask for an email address at any "
        "point — we do not collect email.\n"
        "- The order is a default, never a gate: if the guest offers their "
        "phone number (or any detail) before you asked for it, capture it "
        "IMMEDIATELY with the proper tool — never deflect or ask them to "
        "wait — then return to whatever was still missing.\n"
        "- For full name: you MUST capture a first name and a last name "
        "(surname / family name) before proceeding. The guest may give more "
        "than two tokens — a first, middle and last name is a normal, "
        "common shape for a Sri Lankan name (e.g. 'Chanya Malsha Shehani') "
        "and is NOT an error condition or a transcription problem. Ask "
        "'May I have your full name please?'. When the guest replies, "
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
        "    * If you received TWO OR MORE tokens, decide first whether any "
        "of them look mis-transcribed. Treat a token as suspect when it: is "
        "a lone syllable or fragment ('cha'); is an ordinary English word "
        "that is clearly not a name ('car'); contains stray "
        "punctuation inside the name ('Chara,'); or changes spelling "
        "between the guest's repeats.\n"
        "    * If NONE of the tokens look suspect, the tokens ARE the name: "
        "take the FIRST token as the first name and join every remaining "
        "token together as the last name (e.g. 'Chanya Malsha Shehani' → "
        "first name 'Chanya', last name 'Malsha Shehani'). This applies "
        "whether the guest gave two tokens or more — extra tokens are a "
        "normal name, not a transcription problem, and do NOT by "
        "themselves trigger a re-ask.\n"
        "    * If ONE OR MORE tokens look suspect, confirm ONE PART AT A "
        "TIME. Anchor on the solid-looking token(s) and re-ask ONLY the "
        "first suspect part, naming whichever part it actually is — e.g. "
        "'Thank you. I want to be sure I note your first name correctly — "
        "could you say just your first name once more, slowly?' if the "
        "suspect part is the first name, or the equivalent for the last "
        "name. Read that single part back for a yes/no before moving on to "
        "the next suspect part, if any. Once every token is resolved, map "
        "them the same way as the clean case above: the first token is the "
        "first name and every other token together is the last name.\n"
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
        "again. Whatever the answer, say: 'Thank you — I'll note it down. "
        "You'll get your booking confirmation on WhatsApp shortly, so "
        "please check the spelling of your name on it.' "
        "Then proceed to the mobile number. A booking with a best-effort "
        "name plus a chance to catch a typo on the confirmation is better "
        "than trapping the guest in a repeat loop.\n"
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
        "then call capture_spoken_name with the exact words they spoke and "
        "read the tool's `readback` once for a yes/no confirmation.\n"
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
        "(name, residency, mobile, dates, room, pax), do NOT silently "
        "replace it if they say a different value later in the same call. "
        "Instead, explicitly confirm the change: 'I have your name as Chris "
        "Fernando — did you mean to change it to TJ Pereira?' Only update "
        "the slot after the guest confirms the change. This prevents "
        "telephony lag or repeated speech from corrupting the booking.\n"
        "- SLOT DISAMBIGUATION RULE: match the guest's answer to the slot you "
        "just asked about. If you asked for the mobile number and the guest "
        "replies with letters/words (a name), do NOT overwrite the name — say "
        "'Sorry, I was asking for your mobile number — could you say the "
        "digits please?' If you asked for a name and the guest replies with "
        "digits, ask for the name again. If the guest repeats themselves "
        "(e.g. says 'pardon' or restates the same answer), treat it as a "
        "repeat, not a new value — confirm what you already captured.\n"
        "- For mobile number: NEVER ask the guest for a country code. "
        "Assume +94 by default — accept whatever digits they say (with or "
        "without a leading zero) and silently treat it as a +94 number. "
        "Only if the guest has made clear they are calling from outside "
        "Sri Lanka, ask which country they are calling from and "
        "you add the country code yourself based on that country. Under no "
        "circumstances should you ask the caller to dictate the country "
        "code digits. If the local number is the wrong length, follow the "
        "MOBILE NUMBER LENGTH CHECK rule below — only ask them to repeat the "
        "local number, never the country code.\n"
        "- READING THE MOBILE NUMBER BACK: when you repeat the mobile number "
        "to the guest to confirm it, say it EXACTLY as they gave it — the "
        "plain local digits, digit by digit (for example 'zero seven seven, "
        "one two three, four five six seven'). NEVER speak the country code "
        "out loud and NEVER prefix it with 'plus nine four' or 'nine four'. "
        "The country code is added silently in the background, so the guest "
        "only ever hears their own local number the natural way they said "
        "it.\n"
        "- DIGIT SHORTHAND: guests often use shortcuts when dictating "
        "numbers. 'double [digit]' means that digit TWICE (e.g. "
        "'double six' = 66, 'double oh' = 00). 'triple [digit]' means "
        "THREE times (e.g. 'triple five' = 555). Always expand these "
        "fully. For example, 'oh seven one one, seven five four, "
        "double six eight' = 0711 754 668.\n"
        "- MOBILE NUMBER LENGTH CHECK: a Sri Lankan mobile number has "
        "exactly NINE digits after the leading zero (for example 'zero "
        "seven seven, one two three, four five six seven' is 077 123 4567 "
        "— nine digits after the zero). ACCEPT EVERY natural way a guest "
        "gives it: with or without the leading zero, with or without 'plus "
        "nine four' or 'nine four' in front, in any grouping, and using "
        "'double'/'triple' shorthand or 'oh' for zero. Those are all valid "
        "— never reject a number because of HOW it was said. After "
        "expanding any shorthand, silently count the local digits (ignore a "
        "leading zero and any 'nine four' country code you may have "
        "assumed). Exactly nine local digits means it is complete: do NOT "
        "re-ask.\n"
        "- ONLY when the count is genuinely WRONG — fewer than nine or more "
        "than nine local digits — tell the guest plainly and re-ask, e.g. "
        "'Sorry, that came through as only eight digits — could you say "
        "your mobile number once more?'. If the next attempt is also the "
        "wrong length, ask them to say it slowly, DIGIT BY DIGIT, e.g. "
        "'Let's try once more, nice and slow — could you say your number "
        "one digit at a time?'.\n"
        "- READ IT BACK AND KEEP TRYING: after a digit-by-digit attempt, "
        "read the number back to the guest to confirm, e.g. 'Let me read "
        "that back — zero seven seven, one two three, four five six seven, "
        "is that right?'. If the guest corrects a digit, take the "
        "correction and, once you have nine local digits, continue the "
        "booking normally — this is the ordinary, expected case. Do NOT "
        "give up early: keep patiently re-asking (digit by digit, reading "
        "back each time) for SEVERAL attempts. Do NOT announce that you "
        "will use the number they are calling from after just one failed "
        "digit-by-digit attempt.\n"
        "- LAST RESORT — ASK PERMISSION (only after several failed "
        "attempts): if, after several tries INCLUDING at least one "
        "digit-by-digit read-back, the number is STILL the wrong length, "
        "stop asking for the number and instead ASK the guest for "
        "permission to use the line they are calling from, phrased as a "
        "question, e.g. 'I'm sorry, I'm still not catching your number "
        "clearly — may I send your confirmation to the number you're "
        "calling from instead?'.\n"
        "    * If the guest says YES, use the number they are calling from "
        "and continue the booking normally. You do NOT need to read that "
        "number back or say or pass any digits — the system automatically "
        "uses the line the guest is calling from when the dictated number "
        "is unusable.\n"
        "    * If the guest says NO (they do not want the calling number "
        "used and still cannot give a workable one), tell them warmly that "
        "you are not able to confirm the booking without a contact number, "
        "e.g. 'I understand — unfortunately I can't confirm the booking "
        "without a number we can reach you on. Please do call us back any "
        "time and we'll be glad to complete it for you.' Do NOT force the "
        "booking through in this case.\n"
        "  Never jump straight to this last-resort step, and never use the "
        "calling number without asking first: it is only reached after "
        "genuine, repeated attempts to capture the number the guest gave.\n"
        "- FOREIGN NUMBERS: if the guest has made clear they are calling "
        "from outside Sri Lanka, the nine-digit rule does NOT apply — take "
        "the digits they give for their own country, do not force them to "
        "Sri Lankan length, and do not re-ask on length alone.\n"
        "- Ask ONE question at a time and wait for the answer before asking the "
        "next. Never stack multiple questions in a single turn. Keep each "
        "question short and conversational.\n"
        "- ALWAYS END YOUR TURN WITH A QUESTION until the booking is fully "
        "confirmed (create_booking has returned success). Every reply must "
        "drive the conversation forward by asking the next thing you need. "
        "Never finish a turn with a statement, an upsell, or a list of "
        "perks and then go silent — that leaves the caller hanging. If "
        "you have just surfaced available rooms, end with 'which would "
        "you like to proceed with?'. If the guest has picked a room, end "
        "with 'shall I go ahead and book the Mount Monarch Chalet "
        "for you?' (or whichever room the guest chose). If the "
        "guest has confirmed they want to proceed, "
        "end with 'may I have your full name please?'. Notes about the "
        "deposit, cancellation terms or check-in times belong in a SHORT "
        "prefix before the question — not as the "
        "final sentence. The only exceptions are the post-create_booking "
        "reference read-back and the closing line at the very end of the "
        "call.\n"
        "- Never call create_booking unless check_availability already returned "
        "available=true for the chosen room and dates in this same call AND the "
        "guest has confirmed they want to proceed.\n"
        "- Before calling create_booking, read back the full booking summary "
        "(property, guest name, dates, room, number of guests, "
        "mobile) and get explicit confirmation (e.g. 'shall I confirm this "
        "booking?'). The property MUST be named in the read-back. Only after "
        "the guest says yes, call create_booking.\n"
        "- When create_booking returns success=true, confirm the booking "
        "is done, read the booking reference number once, and tell them "
        "they will also receive a WhatsApp confirmation shortly with all "
        "the details.\n"
        "- If create_booking returns an error or times out, apologise and tell "
        "the guest the hotel will call them back to confirm the booking. Do "
        "NOT retry create_booking automatically.\n"
        "- STAY BASICS you may state plainly (they apply to both "
        "properties): check-in is at two in the afternoon and check-out is "
        "at eleven in the morning; early check-in and late check-out are "
        "subject to availability, so ask the guest to let us know in "
        "advance. Both hotels are open twenty-four hours.\n"
        "- DEPOSIT AND CANCELLATION: for direct bookings a deposit of the "
        "full stay is taken at the time of booking. Cancelling within "
        "twenty-one days of arrival is charged a quarter of the booking "
        "value, within fourteen days a half, and within seven days the "
        "full booking value. Between the fifteenth of December and the "
        "fifteenth of January, cancellations within twenty-one days of "
        "arrival are charged the full booking value. No-shows are charged "
        "the full stay. Promotional rates may carry stricter conditions — "
        "reservations will confirm. State these as proportions in words, "
        "never as an amount of money.\n"
        "- BOOKING DIRECT: booking direct with us carries a best rate "
        "guarantee against the lowest publicly available rate including "
        "taxes and fees, the most flexible cancellation and amendment "
        "terms, no hidden charges, and priority handling for modifications "
        "and special requests. Say this in words only — never attach a "
        "figure or a percentage saving to it.\n"
        "- CELEBRATIONS: if the guest mentions a honeymoon, anniversary, "
        "birthday or proposal, congratulate them warmly and offer to note it on "
        "the booking so the team can look after them. If the offer is available, "
        "mention the honeymoon and special occasions package in that same turn and "
        "ask whether they'd like candlelit dinner for the special occasion. "
        "If the package is not available in the knowledge base, do not mention any "
        "honeymoon-specific package and keep to the base note above.\n"
        "- Be empathetic and attentive. If a guest seems frustrated, acknowledge "
        "their feelings.\n"
        "- THREE-STRIKES EXIT: if you have asked the same clarifying question "
        "three turns in a row without making progress, OR the caller is "
        "clearly off-topic, abusive, or testing the system, do NOT keep "
        "engaging. Politely say something like 'It seems we're having "
        "trouble connecting today — please feel free to call back when "
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
        "but DO NOT skip the dates / pax steps. When you "
        "later reach the personal-details step, refer to what they "
        "already told you — do NOT silently re-ask the same question as "
        "if you had never heard the answer. Confirm: 'Just to confirm, "
        "your name is Chris Fernando, correct?' This makes the call feel "
        "human, not robotic.\n"
        "- If the caller mentions dates or a time period, confirm the exact "
        "check-in and check-out dates.\n"
        "- Before ending the call, briefly summarize what was discussed and "
        "any next steps.\n"
    )


# ---------------------------------------------------------------------------
# Failsafe handover — fallback notification
# ---------------------------------------------------------------------------

async def _notify_handover_fallback(
    *,
    call_sid: str,
    state: dict,
    caller_phone: str,
    full_transcript: list[dict[str, str]],
    lead: str = "Guest hung up before leaving callback details.",
) -> None:
    """Notify the manager with whatever details we have, without the guest's help.

    Two callers, both cases where nobody collected the guest's details:

    * `/voice/dial-result`, the moment the transfer is judged unanswered — the
      guest may hang up at any second, so we page immediately.
    * the failsafe session's `finally`, if the guest hung up before Kavya got
      their name and number.

    Either way we fall back to the number they rang from and a
    transcript-derived summary. `lead` is the opening sentence, since the two
    situations need to read differently to the manager. If we have no number at
    all there is nothing actionable to send, so we skip.
    """
    number = (state.get("caller_phone") or caller_phone or "").strip()
    if not number or number == "unknown":
        logger.warning(
            "[handover] failsafe ended with no details and no caller ID [%s] "
            "— manager NOT notified", call_sid,
        )
        _remember_handoff(call_sid, notified=False)
        return

    reason = (state.get("reason") or "").strip() or "Guest asked to speak to a human."
    summary = (
        f"{lead} {reason} "
        f"The human agent did not answer the transfer "
        f"(dial status: {state.get('dial_status', 'unknown')}). "
        f"Number below is the caller ID they rang from."
    )
    tail = _format_handoff_transcript(full_transcript, limit=8)
    if tail:
        summary = f"{summary}\n\nLast exchanges:\n{tail}"

    outcome = await send_handover_notification(
        call_sid=call_sid,
        customer_name=state.get("customer_name") or "Unknown",
        customer_whatsapp=number,
        call_summary=summary,
        human_agent_whatsapp=state.get("human_agent_whatsapp") or HUMAN_AGENT_PHONE,
    )
    logger.info(
        "[handover] fallback notification for %s — ok=%s",
        call_sid, outcome.get("ok"),
    )
    if not outcome.get("ok"):
        # `notified` is set optimistically BEFORE the POST, so that two notify
        # paths racing cannot both send. This send failed, so hand the job back
        # to whichever path runs next rather than standing them all down — a
        # swallowed n8n 5xx losing the lead is the exact outcome this whole
        # path exists to prevent. Mutate in place: _remember_handoff would
        # resurrect state for a call that has already finished and been popped.
        entry = _handoff_state.get(call_sid)
        if entry is not None:
            entry["notified"] = False


# ---------------------------------------------------------------------------
# Failsafe handover prompt (recovery session after an unanswered transfer)
# ---------------------------------------------------------------------------

# Spoken by Twilio as the ConversationRelay welcomeGreeting when the caller is
# dropped back into Kavya. It must set up the details request immediately —
# the guest has just sat through twenty seconds of ringing.
HANDOFF_FAILSAFE_GREETING: str = (
    "Sorry about that, our team member could not pick up right now. "
    "Let me take your details so they can call you straight back."
)


def _format_handoff_transcript(transcript: list[dict[str, str]], limit: int = 24) -> str:
    """Render the last few turns of the pre-transfer call for the recovery prompt."""
    lines: list[str] = []
    for entry in transcript[-limit:]:
        role = entry.get("role")
        provenance = entry.get("provenance") if entry.get("provenance") in {"final", "interim"} else "interim"
        answered = entry.get("answered") if entry.get("answered") == "unanswered" else "unanswered"
        who = (
            "Guest"
            if role == "user"
            else f"{UNCONFIRMED_TRANSCRIPT_LABEL}; provenance={provenance}; answered={answered}"
            if role == RETAINED_SPEECH_ROLE
            else "Kavya"
        )
        text = (entry.get("text") or "").strip()
        if text:
            lines.append(f"{who}: {text}")
    return "\n".join(lines)


def _build_handoff_failsafe_prompt(state: dict) -> str:
    """System prompt for the recovery session after a human failed to answer.

    Kavya has exactly one job here: get a name and a WhatsApp number, send them
    to the manager, and promise the callback. Everything else is a distraction.
    """
    today = date.today().isoformat()
    reason = (state.get("reason") or "").strip() or "The guest asked to speak to a human."
    caller_phone = (state.get("caller_phone") or "").strip()
    transcript = _format_handoff_transcript(state.get("transcript") or [])

    if caller_phone and caller_phone != "unknown":
        number_rules = (
            f"- The guest is calling from {caller_phone}. Offer that number "
            "first: 'Can our team reach you on WhatsApp on the number you're "
            "calling from?' If they say yes, use exactly that number. If they "
            "want a different number, take the one they give you. If a number "
            "they dictate keeps coming out the wrong length, do not loop — "
            "reassure them you'll reach them on the number they're calling "
            "from and use that.\n"
        )
    else:
        number_rules = (
            "- We do NOT have the guest's number. Ask for the WhatsApp number "
            "they want to be called back on, and read it back to confirm.\n"
        )

    context_block = (
        f"WHAT HAPPENED EARLIER ON THIS CALL:\n{transcript}\n\n"
        if transcript
        else ""
    )

    return (
        f"You are Kavya, the warm and gracious reservations voice agent for "
        f"Hatton Hills, a luxury boutique eco retreat in Sri Lanka's central "
        f"hill country.\n"
        f"Today's date is {today}.\n\n"

        "SITUATION: you already spoke with this guest on this same call. You "
        "tried to transfer them to a human team member, the team member did "
        "NOT pick up, and the guest is now back with you. They asked for a "
        f"human because: {reason}\n\n"

        + context_block +

        "YOUR ONLY JOB NOW is to take the guest's callback details and send "
        "them to the property manager. Do NOT restart the booking "
        "conversation, do NOT re-answer earlier questions, and do NOT try to "
        "transfer them again.\n\n"

        "The guest has already heard: 'Sorry about that, our team member could "
        "not pick up right now. Let me take your details so they can call you "
        "straight back.' Do NOT repeat that or re-introduce yourself.\n\n"

        "STEPS — ask ONE question at a time:\n"
        "1. NAME. If the guest already gave their name earlier on this call "
        "(see above), do NOT ask again — just confirm it: 'I have your name "
        "as Chanya, is that right?'. Otherwise ask, warmly and with nothing "
        "in front of it: 'May I have your name please?'. A first name is "
        "enough here.\n"
        "2. WHATSAPP NUMBER.\n"
        + number_rules +
        "3. The MOMENT you have a name and a number the guest has agreed to, "
        "call the notify_human_handover tool. Do NOT ask for one more "
        "confirmation first, and NEVER ask the same question twice - every "
        "extra turn is a chance for the guest to hang up before the manager "
        "hears about them. Write the call_summary for the manager: what the "
        "guest wanted, any dates, guest count and room type mentioned, and "
        "why they asked for a human.\n"
        "4. After the tool succeeds, tell the guest that you have passed their "
        "details to the team and someone will call them back shortly. Then ask "
        "if there is anything else you can help with while they wait.\n\n"

        "IF THE GUEST REFUSES to give a number, tell them they can call back "
        "any time and thank them. Do not push more than once.\n\n"

        "VOICE RULES (you are speaking on a phone call, not writing text):\n"
        "- Keep every response to one or two short sentences.\n"
        "- Never use markdown, bullet points, numbered lists, asterisks, or URLs.\n"
        "- Use natural spoken language. Say numbers as words.\n"
        "- When the guest says 'double' followed by a digit (for example "
        "'double five'), interpret it as that digit repeated twice ('55'). "
        "Likewise 'triple seven' means '777'. This is common when callers read "
        "out phone numbers.\n"
        "- NUMBER LENGTH: a Sri Lankan mobile number is nine digits after the "
        "leading zero. Accept any natural way it is said, but if the number "
        "the guest dictates is clearly the wrong length (too few or too many "
        "digits), say so warmly and ask once more. Do NOT loop endlessly on "
        "it — an unclear or wrong-length number must NEVER stop you from "
        "passing the guest's details to the team.\n"
        "- If the guest DICTATED a number to you, read it back once so a "
        "mis-heard digit gets corrected, then send it. If they simply agreed "
        "to be reached on the number they called from, that is ALREADY "
        "confirmed - do not read it back, just send it.\n"
        "- Apologise once, warmly, and then move on. Do not keep apologising.\n"
        "- NEVER narrate your own records or what you do or do not have. Say "
        "'May I have your name please?', never 'I don't have your name from "
        "our earlier conversation'. The guest should never hear about notes, "
        "records, transcripts, summaries, tools, or a transfer that failed - "
        "only a warm person taking their details.\n"
    )


# ---------------------------------------------------------------------------
# LLM client (module-level singletons)
# ---------------------------------------------------------------------------
_anthropic_client: AsyncAnthropic | None = None
_openai_client: AsyncOpenAI | None = None
_gemini_client: Any = None  # google.genai.Client when available
_gemini_tts_client: Any = None  # independent Gemini TTS client


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


def _get_gemini_tts_client():
    """Return the isolated lazy Gemini client used only for SmartPBX Sinhala TTS."""
    global _gemini_tts_client
    if _gemini_tts_client is None:
        if not GOOGLE_GENAI_AVAILABLE:
            raise RuntimeError("google-genai package not installed")
        if not GEMINI_API_KEY:
            raise RuntimeError("GEMINI_API_KEY is not set")
        _gemini_tts_client = google_genai.Client(api_key=GEMINI_API_KEY)
        logger.info("Initialized native Gemini client for SmartPBX Sinhala TTS")
    return _gemini_tts_client


async def _iter_gemini_tts_audio_deltas(stream: Any) -> AsyncIterator[tuple[str, Any]]:
    """Yield base64 audio payloads from the documented Interactions SSE shape."""
    async for event in stream:
        if getattr(event, "event_type", None) != "step.delta":
            continue
        delta = getattr(event, "delta", None)
        if getattr(delta, "type", None) != "audio":
            continue
        data = getattr(delta, "data", None)
        if isinstance(data, str) and data:
            yield data, delta


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
                    fc_part: dict[str, Any] = {
                        "function_call": {
                            "name": tc["function"]["name"],
                            "args": args,
                        }
                    }
                    # Gemini 3.x hands back a thought signature alongside a
                    # streamed function call and requires it echoed on the next
                    # request; without it the follow-up round is rejected. It is
                    # carried on the internal history entry only (never sent to
                    # another provider, never serialised).
                    signature = tc.get("gemini_thought_signature")
                    if signature:
                        fc_part["thought_signature"] = signature
                    parts.append(fc_part)
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
# Gemini native streaming primitives (shared by both Gemini runners)
# ---------------------------------------------------------------------------
# Set once if the installed SDK / selected model rejects the thinking controls,
# so the process stops paying for a doomed first attempt on every turn.
_gemini_thinking_unsupported: bool = False

_GEMINI_MAJOR_VERSION = re.compile(r"gemini-(\d+)")


def _gemini_thinking_config(model: str) -> dict[str, Any] | None:
    """Thinking controls for a short voice turn — see GEMINI_THINKING_BUDGET."""
    if _gemini_thinking_unsupported:
        return None
    match = _GEMINI_MAJOR_VERSION.search((model or "").lower())
    major = int(match.group(1)) if match else 2
    if major >= 3:
        # 3.x replaced the token budget with a coarse level and cannot turn
        # thinking off entirely; the floor is the best this path can do.
        return {"thinking_level": GEMINI_THINKING_LEVEL or "low"}
    return {"thinking_budget": GEMINI_THINKING_BUDGET}


def _build_gemini_config(
    *,
    system: str,
    tools: list[dict] | None,
    model: str,
    nudge: str | None = None,
    max_output_tokens: int = MAX_TOKENS,
) -> dict[str, Any]:
    """Assemble the per-round google-genai config.

    ``nudge`` is appended to the system instruction only — it is never added to
    the conversation history, so the caller never hears it and it cannot leak
    into the transcript or the post-call record.
    """
    system_instruction = f"{system}\n\n{nudge}" if nudge else system
    config: dict[str, Any] = {
        "system_instruction": system_instruction,
        "max_output_tokens": max_output_tokens,
    }
    thinking = _gemini_thinking_config(model)
    if thinking is not None:
        config["thinking_config"] = thinking
    if tools:
        config["tools"] = tools
    return config


async def _open_gemini_stream(client, *, model: str, contents: list, config: dict):
    """Open a native Gemini streaming response.

    Degrades once, permanently, if the installed SDK or the selected model
    rejects the thinking controls — an unknown model name must not take the
    whole provider down.
    """
    global _gemini_thinking_unsupported
    try:
        return await client.aio.models.generate_content_stream(
            model=model, contents=contents, config=config
        )
    except Exception as exc:
        if "thinking_config" not in config or "thinking" not in str(exc).lower():
            raise
        _gemini_thinking_unsupported = True
        logger.warning(
            "gemini_diagnostic event=thinking_config_rejected model=%s detail=%s",
            model, str(exc)[:200],
        )
        degraded = {k: v for k, v in config.items() if k != "thinking_config"}
        return await client.aio.models.generate_content_stream(
            model=model, contents=contents, config=degraded
        )


async def _iter_gemini_stream(stream) -> AsyncIterator[tuple[str, Any]]:
    """Normalise a native Gemini stream into ``(kind, payload)`` deltas.

    Kinds: ``"text"`` (str), ``"tool"`` (dict with name/args/thought_signature),
    ``"finish"`` (finish reason or block reason).

    Thought parts are DROPPED. On 3.x models the model's private reasoning
    arrives as ordinary text parts flagged ``thought=True``; speaking that to a
    caller is worse than saying nothing. Every attribute is read defensively —
    the same normaliser has to survive both the real SDK objects and the
    lightweight fakes the tests stream through it.
    """
    async for chunk in stream:
        candidates = getattr(chunk, "candidates", None)
        if not candidates:
            feedback = getattr(chunk, "prompt_feedback", None)
            block_reason = getattr(feedback, "block_reason", None) if feedback else None
            if block_reason:
                yield "finish", block_reason
            continue
        candidate = candidates[0]
        content = getattr(candidate, "content", None)
        for part in (getattr(content, "parts", None) or []):
            if getattr(part, "thought", False):
                continue
            text = getattr(part, "text", None)
            if text:
                yield "text", text
            function_call = getattr(part, "function_call", None)
            if function_call:
                raw_args = getattr(function_call, "args", None)
                yield "tool", {
                    "name": function_call.name,
                    "args": dict(raw_args) if raw_args else {},
                    "thought_signature": getattr(part, "thought_signature", None),
                }
        finish_reason = getattr(candidate, "finish_reason", None)
        if finish_reason:
            yield "finish", finish_reason


def _log_gemini_empty(
    *, path: str, attempt: int, finish_reason: Any, retrying: bool, call_sid: str = ""
) -> None:
    """Diagnostic for a Gemini turn that produced no text and no tool call.

    One greppable line: this failure is invisible from the caller's side (they
    just hear silence) and was previously indistinguishable in the logs from a
    healthy short turn.
    """
    logger.warning(
        "gemini_diagnostic event=empty_response path=%s attempt=%d finish=%s "
        "retrying=%s call=%s",
        path, attempt, finish_reason, str(retrying).lower(), call_sid or "-",
    )


def _extract_gemini_exception_code(exc: Any) -> Any:
    """Read a status-like error code from a Gemini exception using duck-typing."""

    candidates: list[Any] = [
        getattr(exc, "status", None),
        getattr(exc, "status_code", None),
        getattr(exc, "code", None),
    ]

    nested_error = getattr(exc, "error", None)
    if nested_error is not None:
        if isinstance(nested_error, dict):
            candidates.extend([
                nested_error.get("status"),
                nested_error.get("status_code"),
                nested_error.get("code"),
            ])
        else:
            candidates.extend([
                getattr(nested_error, "status", None),
                getattr(nested_error, "status_code", None),
                getattr(nested_error, "code", None),
            ])

    for candidate in candidates:
        if candidate is None:
            continue

        if isinstance(candidate, int):
            return candidate

        if isinstance(candidate, str):
            stripped = candidate.strip()
            if stripped:
                return stripped

        if isinstance(candidate, bool):
            continue

        candidate_value = getattr(candidate, "value", None)
        if isinstance(candidate_value, int):
            return candidate_value
        if isinstance(candidate_value, str) and candidate_value.strip():
            return candidate_value.strip()

        candidate_name = getattr(candidate, "name", None)
        if isinstance(candidate_name, str) and candidate_name.strip():
            return candidate_name.strip()

    return None


def _classify_gemini_exception(exc: Exception) -> str:
    """Map a Gemini exception to one of ``quota``, ``server``, ``client_error``."""

    raw_code = _extract_gemini_exception_code(exc)
    if raw_code is None:
        message = str(exc).upper()
        if "RESOURCE_EXHAUSTED" in message or "429" in message:
            return "quota"
        return "client_error"

    if isinstance(raw_code, int):
        if raw_code == 429:
            return "quota"
        if 500 <= raw_code <= 599:
            return "server"
        return "client_error"

    code = str(raw_code).strip().upper()
    if code in {"429", "RESOURCE_EXHAUSTED"}:
        return "quota"
    if code.startswith("5") and len(code) == 3 and code.isdigit():
        return "server"
    return "client_error"


class _GeminiEmptyTurnError(Exception):
    """A Gemini turn produced no text and no tool call, twice in a row.

    Modelled as a provider failure rather than a turn outcome: the caller hears
    dead air, which is exactly what a quota error sounds like, so it takes the
    same failover route. Raised ONLY when Claude can actually answer the turn —
    when failover is unavailable the runners speak the canned line instead, and
    this exception never appears.
    """

    def __init__(self) -> None:
        super().__init__("gemini returned an empty turn twice")


def _claude_tools_from_gemini(tools: Any) -> list[dict[str, Any]]:
    """Re-shape Gemini function declarations as Anthropic tool definitions.

    A Gemini→Claude failover swaps the runner but carries the SAME tool list, and
    the two wire shapes are incompatible: Anthropic 400s on Gemini's
    ``[{"function_declarations": [...]}]``. Converting — rather than substituting
    ``get_tools()`` — preserves WHICH tools the caller allowed: the
    handover-failsafe session is deliberately restricted to
    ``notify_human_handover``, and handing it the full booking set would let Kavya
    try to book a room instead of notifying the manager.

    Anything already Anthropic-shaped passes through, so this is safe to apply on
    a path whose provider was never Gemini.
    """
    if not tools:
        return []
    converted: list[dict[str, Any]] = []
    for entry in tools:
        if not isinstance(entry, dict):
            continue
        declarations = entry.get("function_declarations")
        if declarations is None:
            if entry.get("name"):
                converted.append(entry)
            continue
        for declaration in declarations:
            if not isinstance(declaration, dict) or not declaration.get("name"):
                continue
            converted.append({
                "name": declaration["name"],
                "description": declaration.get("description", ""),
                "input_schema": declaration.get("parameters")
                or {"type": "object", "properties": {}},
            })
    return converted


def _gemini_relay_failover_ready() -> bool:
    """True when the ConversationRelay Gemini turn can be re-run on Claude."""
    if not GEMINI_FAILOVER_TO_CLAUDE:
        return False
    if not ANTHROPIC_API_KEY:
        return False
    try:
        return _get_anthropic_client() is not None
    except RuntimeError:
        return False


def _init_gemini_failover_state() -> dict[str, Any]:
    return {
        "consecutive_failovers": 0,
        "degraded": False,
        "degraded_logged": False,
    }


def _note_gemini_failover(state: dict[str, Any]) -> str:
    """Record one failover and return updated state."""

    state["consecutive_failovers"] = state.get("consecutive_failovers", 0) + 1
    consecutive = state["consecutive_failovers"]

    if state.get("degraded", False):
        return "degraded"

    if consecutive >= GEMINI_FAILOVER_STICKY_AFTER:
        state["degraded"] = True
        if not state.get("degraded_logged", False):
            logger.info("smartpbx_media event=llm_provider_degraded")
            state["degraded_logged"] = True
        return "degraded"

    return "tracking"


def _note_gemini_success(state: dict[str, Any]) -> None:
    state["consecutive_failovers"] = 0


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
        logger.warning("Knowledge base initialization failed — continuing without KB.")

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
        logger.warning("ELEVENLABS_API_KEY or ELEVENLABS_VOICE_ID not set — "
                       "ConversationRelay TTS will not work in production.")

    # SmartPBX must not initialize or advertise the legacy Twilio handoff path.
    # The established Twilio service retains this startup behavior unchanged.
    if KAVYA_SERVICE_MODE != "smartpbx":
        if HUMAN_AGENT_PHONE:
            logger.info("[handoff] enabled â†’ %s", HUMAN_AGENT_PHONE)
        else:
            logger.info("[handoff] disabled (HUMAN_AGENT_PHONE not set)")

        logger.info("[handoff] public hostname: %s", PUBLIC_HOSTNAME)

        if TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN:
            _get_twilio_client()
            logger.info(
                "[handoff] Twilio REST client configured (account=%s...)",
                TWILIO_ACCOUNT_SID[:10],
            )
        else:
            logger.warning(
                "[handoff] Twilio REST client NOT configured — handoff will fail"
            )
    else:
        logger.info("SmartPBX mode: legacy Twilio handoff startup is disabled")

    # NOTE: bookings go to the Yanolja PMS via booking_api -> yanolja_service ->
    # yanolja_client. Hatton Hills is an invented demo property, so there is no
    # real upstream booking engine to re-wire; the PMS IS the source of truth
    # (see ops/hattonhills-pms/).
    logger.info("Server startup complete. Booking backend configured: %s", is_configured())

    yield

    # --- Shutdown ---
    logger.info("Shutting down server...")
    await close_session()
    logger.info("Shutdown complete.")


# ---------------------------------------------------------------------------
# FastAPI application
# ---------------------------------------------------------------------------
app = FastAPI(
    title="Hatton Hills Voice Agent (Kavya)",
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

    By default (IVR_MENU_ENABLED unset/false) connects the caller straight to
    the English ConversationRelay agent - no IVR / language menu. Set
    IVR_MENU_ENABLED=true to present the DTMF language menu instead.
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

    en = conversation_relay_config("en")
    cr = _build_conversation_relay_twiml(host, "en", en)

    # IVR language menu (IVR_MENU_ENABLED=true only): 1 = English
    # (ConversationRelay). Sinhala and Arabic were removed on 2026-07-28, so
    # English is the only option and any other digit falls back to it. With the
    # menu disabled (the default) the <Gather> is omitted entirely and every
    # call connects straight to the English agent below — which is the
    # preferred setting now that there is only one language.
    gather = ""
    if IVR_MENU_ENABLED:
        gather = (
            f'  <Gather numDigits="1" action="https://{host}/voice/language-selected"'
            ' method="POST" timeout="6">\n'
            '    <Say voice="Polly.Joanna">Welcome to Hatton Hills. '
            'For English, press 1.</Say>\n'
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

    logger.info(
        "Incoming call from %s - %s",
        request.headers.get("x-forwarded-for", "unknown"),
        "presenting English-only language menu" if IVR_MENU_ENABLED
        else "IVR menu disabled, connecting straight to English agent",
    )

    return Response(content=twiml, media_type="application/xml")


# ---------------------------------------------------------------------------
# Language selection handler (called by Twilio after DTMF digit)
# ---------------------------------------------------------------------------

@app.post("/voice/language-selected")
async def voice_language_selected(request: Request) -> Response:
    """Handle the caller's DTMF language selection.

    English (1) â†’ ConversationRelay TwiML (ElevenLabs TTS, text-in/text-out).
    Every other digit falls back to English: DIGIT_TO_LANG maps only "1"
    since Sinhala and Arabic were removed (2026-07-28). The Media Streams
    branch below is kept for whenever a non-English language is re-added.
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
        # English — ConversationRelay with ElevenLabs
        config = conversation_relay_config("en")
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
        # Non-English — Media Streams with Google STT + per-language TTS.
        # Unreachable while DIGIT_TO_LANG is English-only; kept for re-enable.
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
                    # Double-encoded — try one more parse
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
        # Legacy Path A fallback — dashboard event is now sent from
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
        # Same owned-number caller ID as the live Path B transfer — see
        # TWILIO_CALLER_ID.
        _cid = _transfer_caller_id(call_sid)
        _cid_attr = f' callerId="{html_escape(_cid)}"' if _cid else ""
        twiml = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            "<Response>\n"
            f'  <Dial action="{dial_action_url}" method="POST" timeout="{HANDOFF_DIAL_TIMEOUT}"{_cid_attr} answerOnBridge="true">\n'
            f'    <Number url="{whisper_url}"'
            f' statusCallback="https://{host}/voice/dial-status?parent={call_sid}"'
            ' statusCallbackMethod="POST"'
            ' statusCallbackEvent="initiated ringing answered completed">'
            f'{HUMAN_AGENT_PHONE}</Number>\n'
            "  </Dial>\n"
            "</Response>"
        )
        return Response(content=twiml, media_type="application/xml")

    logger.info(
        "[handoff] relay-action with no transfer (call_sid=%s, action=%r) — hanging up",
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


@app.post("/voice/dial-status")
async def dial_status(request: Request) -> Response:
    """Per-event status callback for the outbound transfer leg.

    Twilio POSTs here on initiated / ringing / answered / completed. We only
    need the timestamps, so that /voice/dial-result can tell a real pickup from
    a carrier intercept that answered instantly (see _answer_looks_intercepted).

    Only records against calls we actually dispatched a transfer for, so a
    stray or replayed callback cannot grow _handoff_state without bound.
    """
    form = await request.form()
    parent = request.query_params.get("parent", "")
    event = str(form.get("CallStatus") or "").strip().lower()
    entry = _handoff_state.get(parent)
    if entry is not None and event:
        events = entry.setdefault("dial_events", {})
        # Twilio's statusCallbackEvent is *named* "answered", but the
        # CallStatus field it actually POSTs when a leg is picked up reads
        # "in-progress" — Twilio never sends CallStatus=answered.
        # "answered" is DialCallStatus vocabulary, not CallStatus vocabulary;
        # the two were conflated when this endpoint was written. Because of
        # that, the canonical "answered" key this dict is keyed on was NEVER
        # populated, _answer_looks_intercepted's events.get("answered") always
        # missed, and carrier-intercept detection silently never fired once
        # from its 2026-08-03 release until this was found and fixed on
        # 2026-08-04. Normalise "in-progress" onto "answered" here so the
        # detector's lookup matches what Twilio actually sends. Do NOT
        # "simplify" this back to storing the raw CallStatus value — that is
        # the exact bug. Still accept a literal "answered" too, in case a
        # future Twilio change or a replayed callback sends that literal
        # value.
        canonical = "answered" if event in ("in-progress", "answered") else event
        # First occurrence wins — Twilio can retry a callback, and a retry must
        # not overwrite the original timing with a later clock reading.
        events.setdefault(canonical, time.time())
        logger.info(
            "[handoff] dial-status parent=%s event=%s (have: %s)",
            parent, event, ",".join(sorted(events)),
        )

        # Cut a carrier intercept off immediately rather than holding the guest
        # through it.
        #
        # answerOnBridge bridges the guest the instant the leg is "answered".
        # When that answer is an intercept recording, the guest hears it for as
        # long as the carrier plays it — 49s and 52s in the three production
        # incidents — and only when it finally ends does <Dial> return and the
        # failsafe get its turn. By then the guest has almost always hung up,
        # which is why the failsafe kept opening a recovery session with nobody
        # left on the line. Ending the leg here collapses that wait to about a
        # second, so the guest is still there to be recovered.
        #
        # Deliberately STRICTER than _answer_looks_intercepted: this also
        # requires that no ringing event arrived. A genuine handset pickup
        # essentially always rings first, and unlike dial-result — which only
        # reclassifies a call that has already ended — this cuts off a live
        # one. It must not fire on a fast-but-real answer.
        if (
            HANDOFF_KILL_INTERCEPT
            and canonical == "answered"
            and "ringing" not in events
            and not entry.get("intercept_killed")
        ):
            initiated = events.get("initiated")
            answered = events.get("answered")
            child_sid = str(form.get("CallSid") or "").strip()
            gap = (
                answered - initiated
                if initiated is not None and answered is not None
                else None
            )
            if child_sid and gap is not None and gap < HANDOFF_MIN_ANSWER_SECONDS:
                entry["intercept_killed"] = True
                logger.warning(
                    "[handoff] leg %s answered %.2fs after dial with no ringing "
                    "— carrier intercept, hanging it up so the guest is not held "
                    "through the recording", child_sid, gap,
                )
                twilio = _get_twilio_client()
                if twilio:
                    try:
                        loop = asyncio.get_event_loop()
                        await loop.run_in_executor(
                            None,
                            lambda: twilio.calls(child_sid).update(
                                status="completed"
                            ),
                        )
                    except Exception:
                        # Not fatal: <Dial> still ends on its own when the
                        # carrier stops talking, so the failsafe is delayed
                        # rather than lost.
                        logger.warning(
                            "[handoff] could not hang up intercepted leg %s",
                            child_sid, exc_info=True,
                        )

    return Response(status_code=204)


@app.post("/voice/dial-result")
async def dial_result(request: Request) -> Response:
    """Callback from <Dial action>. If the human answered â†’ hang up.
    Otherwise, drop the caller back into Kavya with a recovery greeting.
    """
    form = await request.form()
    status = form.get("DialCallStatus", "")
    call_sid = form.get("CallSid", "")
    host = request.url.hostname
    logger.info("[handoff] dial-result status=%s call_sid=%s", status, call_sid)

    # "completed" only means the leg ended normally — it does NOT prove a human
    # answered. A carrier intercept answers instantly and also reports
    # completed, so check the timing before standing the failsafe down.
    intercepted, why = _answer_looks_intercepted(_handoff_state.get(call_sid, {}))
    if status in ("completed", "answered") and intercepted:
        logger.warning(
            "[handoff] dial reported %s for %s but %s — treating as NOT answered",
            status, call_sid, why,
        )
        status = "intercepted"

    if status in ("completed", "answered"):
        logger.info("[handoff] human answer accepted for %s (%s)", call_sid, why)
        # Human took the call — no failsafe needed, drop the carry-over.
        state = _handoff_state.pop(call_sid, {})
        # This is the end of the line for this call, so THIS is where the
        # post-call record gets written. The ConversationRelay session
        # deliberately skipped it (transfer_initiated) because at that point we
        # did not yet know whether the human would pick up; without emitting it
        # here, a successfully transferred call would leave no row at all.
        transcript = state.get("transcript") or []
        if transcript:
            # Bookkeeping must never break the call. This handler's job is to
            # return TwiML; if building an LLM client or scheduling the task
            # fails, log it and still hang up cleanly rather than 500 at Twilio.
            try:
                asyncio.create_task(
                    process_post_call_data(
                        call_sid=call_sid,
                        lang=state.get("lang", "en"),
                        caller_phone=state.get("caller_phone", "unknown"),
                        full_transcript=transcript,
                        call_start_time=state.get(
                            "call_start_time", datetime.now().isoformat()
                        ),
                        call_end_time=datetime.now().isoformat(),
                        llm_provider=LLM_PROVIDER,
                        # Only the active provider's client is built, and a
                        # failure yields None rather than aborting: post_call
                        # degrades to a transcript-only record, which still
                        # reaches the sheet. Losing the row entirely because a
                        # key is unset would be the worse outcome.
                        anthropic_client=(
                            _safe_client(_get_anthropic_client)
                            if LLM_PROVIDER == "claude" else None
                        ),
                        openai_client=(
                            _safe_client(_get_client)
                            if LLM_PROVIDER == "openai" else None
                        ),
                        gemini_client=(
                            _safe_client(_get_gemini_client)
                            if LLM_PROVIDER == "gemini" else None
                        ),
                        model=MODEL,
                    )
                )
                logger.info(
                    "[handoff] human answered for %s — post-call emitted (%d turns)",
                    call_sid, len(transcript),
                )
            except Exception:
                logger.exception(
                    "[handoff] post-call dispatch failed for %s — hanging up anyway",
                    call_sid,
                )
        return Response(
            content='<?xml version="1.0" encoding="UTF-8"?><Response><Hangup/></Response>',
            media_type="application/xml",
        )

    # No answer / busy / failed / canceled â†’ the failsafe. Recover into Kavya
    # in handover mode: she collects the guest's name and WhatsApp number and
    # messages the property manager instead of leaving the guest stranded.
    _remember_handoff(call_sid, dial_status=status)
    logger.info(
        "[handoff] human did not answer (%s) for %s — entering failsafe",
        status, call_sid,
    )

    # Page the manager NOW, not only from the recovery session below.
    #
    # The recovery session can only notify anyone if its WebSocket actually
    # opens — via the notify_human_handover tool, or via its end-of-session
    # fallback. On a carrier intercept it usually never opens: the guest has
    # just spent the whole dial listening to a recorded intercept message
    # instead of ringing, and hangs up before Kavya comes back. Live on
    # 2026-08-05 two consecutive intercepted transfers both reached
    # "entering failsafe" and the manager was told nothing at all, because the
    # guest was gone by then. The transfer failed completely silently.
    #
    # So notify from here, where we KNOW the transfer failed and depend on
    # nothing further happening. `notified` makes it idempotent: it suppresses
    # only the duplicate end-of-session net. If the guest does stay on the
    # line, notify_human_handover still sends its richer follow-up with the
    # name and number she collects — two messages beat none.
    state = dict(_handoff_state.get(call_sid) or {})
    if not state.get("notified"):
        _remember_handoff(call_sid, notified=True)
        asyncio.create_task(
            _notify_handover_fallback(
                call_sid=call_sid,
                state=state,
                caller_phone=state.get("caller_phone", ""),
                full_transcript=state.get("transcript") or [],
                lead=(
                    "A transfer to you was NOT answered. The guest may still "
                    "be on the line or may already have hung up — please call "
                    "them back on the number below. Details are from the call "
                    "so far."
                ),
            )
        )

    recovery_config = conversation_relay_config("en")
    recovery_config["welcome_greeting"] = HANDOFF_FAILSAFE_GREETING
    cr_tag = _build_conversation_relay_twiml(
        host, "en", recovery_config, mode="handover_failsafe",
    )
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
    host: str, lang: str, config: dict[str, str], mode: str = ""
) -> str:
    """Build the <ConversationRelay> XML tag for the given language config.

    `mode` is appended to the WebSocket URL so the session handler knows it is
    a recovery session (currently only "handover_failsafe") rather than a fresh
    call — the WebSocket carries no other signal of how it was started.
    """
    extra = config["extra_attrs"]
    # XML-escape the welcome greeting in case it contains special characters
    greeting = xml.sax.saxutils.escape(config["welcome_greeting"])
    mode_qs = f"&amp;mode={url_quote(mode)}" if mode else ""

    # Optional STT tuning (#121). `hints` biases recognition toward tokens the
    # telephony model mishears on Sri Lankan-accented English (spoken digit
    # shorthand like "double"/"triple", plus common local names). A separate
    # transcriptionLanguage is emitted only when explicitly configured, so the
    # default en-US behaviour is unchanged.
    hints = config.get("hints", "")
    hints_attr = (
        f'        hints="{xml.sax.saxutils.escape(hints)}"\n' if hints else ""
    )
    tx_lang = config.get("transcription_language", "")
    tx_lang_attr = (
        f'        transcriptionLanguage="{xml.sax.saxutils.escape(tx_lang)}"\n'
        if tx_lang else ""
    )

    return (
        f'<ConversationRelay url="wss://{host}/ws/conversation?lang={lang}{mode_qs}"\n'
        f'        ttsProvider="{config["tts_provider"]}"\n'
        f'        voice="{config["voice"]}"\n'
        f'{extra}'
        f'        language="{config["language"]}"\n'
        f'        transcriptionProvider="google"\n'
        f'{tx_lang_attr}'
        f'        speechModel="telephony"\n'
        f'{hints_attr}'
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
# Google Cloud STT — streaming (background thread, Media Streams only)
# ---------------------------------------------------------------------------

class GoogleSTTStream:
    """Streams mulaw 8 kHz audio to Google Cloud Speech-to-Text.

    Runs the synchronous gRPC streaming_recognize in a daemon thread.
    Fires on_final_result(transcript) from that thread.

    Google caps ONE streaming_recognize at 305 seconds of audio, so a call
    longer than five minutes spans several streams. Rotation is proactive (see
    STT_STREAM_ROTATE_SECONDS) and each stream is fenced by an epoch, because
    gRPC consumes the request generator on its own thread and a generator that
    outlives its RPC keeps stealing frames from the shared queue — which is how
    a "restarted" stream ended up deaf while the dead one ate the audio.
    """

    def __init__(
        self,
        on_final_result: Any,
        on_interim_result: Any = None,
        lang: str = "si",
        privacy_safe: bool = False,
    ):
        self._on_final = on_final_result
        self._on_interim = on_interim_result
        self._lang = lang
        self._privacy_safe = privacy_safe
        self._audio_q: queue.Queue[bytes | None] = queue.Queue(STT_QUEUE_MAX_CHUNKS)
        self._running = False
        self._thread: threading.Thread | None = None
        self._chunk_count = 0
        self._dropped_chunks = 0
        # Monotonically increasing per stream. The request generator dies as soon
        # as its own epoch is superseded, so exactly one generator is ever
        # draining the audio queue.
        self._stream_epoch = 0
        # Monotonic timestamp of the last provider response (interim or final) —
        # the rotation point prefers a moment when the caller is not mid-word.
        self._last_voice_activity = 0.0
        self._rotations = 0
        # Injectable so rotation timing is tested without sleeping for minutes.
        self._clock: Any = time.monotonic
        # Set once if Google rejects the telephony model / adaptation for this
        # deployment, so every later stream skips straight to the default model.
        self._enhanced_config_rejected = False
        # A privacy-safe SmartPBX configuration event is emitted once per STT
        # stream object, including when enhanced configuration later falls back.
        self._digit_class_state_logged = False
        # Set by the owning session; called from this thread when the restart
        # cap trips so the call can end instead of sitting in silence.
        self.on_fatal: Any = None

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
        try:
            self._audio_q.put_nowait(None)
        except queue.Full:
            # Make room for the sentinel; _running=False already ends the loop.
            with contextlib.suppress(queue.Empty):
                self._audio_q.get_nowait()
            with contextlib.suppress(queue.Full):
                self._audio_q.put_nowait(None)
        if self._thread:
            self._thread.join(timeout=5)

    def feed(self, mulaw_bytes: bytes):
        try:
            # Never block: this is called from the shared event loop.
            self._audio_q.put_nowait(mulaw_bytes)
        except queue.Full:
            self._dropped_chunks += 1
            if self._dropped_chunks % 200 == 1:
                logger.warning(
                    "STT queue full — dropped %d inbound chunks (lang=%s)",
                    self._dropped_chunks, self._lang,
                )
            return
        self._chunk_count += 1
        if self._chunk_count % 200 == 0:  # log every ~4s of audio
            logger.info("STT audio feed: %d chunks (lang=%s)", self._chunk_count, self._lang)

    def _rotation_due(self, started_at: float) -> bool:
        """Whether the current stream should be swapped out now.

        Past STT_STREAM_ROTATE_SECONDS the swap waits for a short gap in the
        caller's speech so a word is not cut in half; past the deadline it
        happens regardless, because hitting Google's 305 s ceiling costs far more
        than a clipped syllable.
        """
        age = self._clock() - started_at
        if age >= STT_STREAM_ROTATE_DEADLINE_SECONDS:
            return True
        if age < STT_STREAM_ROTATE_SECONDS:
            return False
        quiet_for = self._clock() - self._last_voice_activity
        return quiet_for >= STT_ROTATE_QUIET_SECONDS

    def _trim_stale_backlog(self) -> int:
        """Drop all but the newest STT_SWAP_BUFFER_CHUNKS queued frames.

        Called as each stream opens. Whatever the caller said during the swap is
        replayed into the new stream; anything older than the buffer window is
        dropped OLDEST-first, so the fresh stream is not immediately pushed back
        toward its own audio ceiling by a stale backlog.
        """
        dropped = 0
        while self._audio_q.qsize() > STT_SWAP_BUFFER_CHUNKS:
            try:
                chunk = self._audio_q.get_nowait()
            except queue.Empty:
                break
            if chunk is None:
                # Never swallow the stop sentinel: stop() is waiting on it.
                with contextlib.suppress(queue.Full):
                    self._audio_q.put_nowait(None)
                break
            dropped += 1
        return dropped

    def _audio_generator(self, epoch: int | None = None, started_at: float | None = None):
        """Yield queued frames for ONE stream, then terminate.

        Terminating matters as much as yielding: the generator runs on gRPC's
        writer thread, so an old one left alive competes with its replacement
        for frames off the single shared queue.
        """
        while self._running:
            if epoch is not None and epoch != self._stream_epoch:
                return
            if started_at is not None and self._rotation_due(started_at):
                self._rotations += 1
                logger.info(
                    "STT rotating stream proactively at %.0fs of age "
                    "(lang=%s, rotation=%d)",
                    self._clock() - started_at, self._lang, self._rotations,
                )
                # Half-closing the request stream ends the RPC cleanly, so _loop
                # reopens immediately with no backoff and no 400.
                return
            try:
                chunk = self._audio_q.get(timeout=0.1)
            except queue.Empty:
                continue
            if chunk is None:
                break
            yield chunk

    def _loop(self):
        # A synchronous failure (rotated credentials, quota, network) would
        # otherwise restart with no delay and no ceiling: a tight spin that
        # writes one traceback per iteration and destroys the log ring.
        consecutive_failures = 0
        while self._running:
            try:
                self._run_one_stream()
                consecutive_failures = 0
                # A clean end is either a proactive rotation or the caller
                # hanging up: reopen immediately, no backoff.
                continue
            except Exception as exc:
                if not self._running:
                    break
                consecutive_failures += 1
                if consecutive_failures >= STT_MAX_CONSECUTIVE_FAILURES:
                    self._running = False
                    logger.error(
                        "STT stream failed %d times consecutively (%s) — giving up so the call can end",
                        consecutive_failures, exc,
                    )
                    self._signal_fatal()
                    break
                backoff = min(
                    STT_RESTART_BACKOFF_BASE * (2 ** (consecutive_failures - 1)),
                    STT_RESTART_BACKOFF_MAX,
                )
                logger.warning(
                    "STT stream ended (%s) — restart %d/%d in %.1fs",
                    exc, consecutive_failures, STT_MAX_CONSECUTIVE_FAILURES, backoff,
                    exc_info=consecutive_failures == 1,
                )
                time.sleep(backoff)

    def _signal_fatal(self) -> None:
        """Tell the owning session STT is gone. Must never raise into the loop."""
        on_fatal = self.on_fatal
        if on_fatal is None:
            return
        try:
            on_fatal()
        except Exception:
            logger.warning("STT fatal signal failed", exc_info=True)

    def _run_one_stream(self):
        # Google caps a streaming_recognize call at 305 s of audio, so this runs
        # many times per long call. Each client owns a gRPC channel, fds and
        # completion-queue threads, so it must be released every time.
        client = google_speech.SpeechClient()
        try:
            self._stream_until_closed(client)
        finally:
            # Fence the stream we are leaving BEFORE the next one opens, so its
            # request generator cannot outlive it and steal the caller's audio.
            self._stream_epoch += 1
            close = getattr(client, "close", None)
            if close is not None:
                try:
                    close()
                except Exception:
                    logger.warning("STT client close failed", exc_info=True)

    def _streaming_config(self, primary: str, alternatives: list[str], *, enhanced: bool):
        """Build the per-stream recognition config.

        ``enhanced`` adds the narrowband telephony model and the booking-domain
        phrase list. English only — see STT_GOOGLE_MODEL_EN.
        """
        recognition: dict[str, Any] = {
            "encoding": google_speech.RecognitionConfig.AudioEncoding.MULAW,
            "sample_rate_hertz": 8000,
            "language_code": primary,
            # English resolves to an EMPTY list, deliberately: naming en-US as an
            # alternative to itself is what this key must never do.
            "alternative_language_codes": alternatives,
            "enable_automatic_punctuation": True,
        }
        if self._privacy_safe and self._lang == "en" and not self._digit_class_state_logged:
            self._digit_class_state_logged = True
            logger.info(
                "smartpbx_media event=stt_digit_class_state "
                "digit_class_enabled=%s digit_class_boost=%s",
                str(STT_DIGIT_CLASS_BOOST > 0).lower(),
                STT_DIGIT_CLASS_BOOST,
            )
        if enhanced:
            if STT_GOOGLE_MODEL_EN:
                recognition["model"] = STT_GOOGLE_MODEL_EN
            speech_context = getattr(google_speech, "SpeechContext", None)
            if speech_context is not None and EN_STT_PHRASE_LIST:
                recognition["speech_contexts"] = [
                    speech_context(
                        phrases=list(EN_STT_PHRASE_LIST[:STT_MAX_ADAPTATION_PHRASES]),
                        boost=STT_ADAPTATION_BOOST,
                    )
                ]
                if STT_DIGIT_CLASS_BOOST > 0:
                    recognition["speech_contexts"].append(
                        speech_context(
                            phrases=["$OOV_CLASS_DIGIT_SEQUENCE"],
                            boost=STT_DIGIT_CLASS_BOOST,
                        )
                    )
        return google_speech.StreamingRecognitionConfig(
            config=google_speech.RecognitionConfig(**recognition),
            interim_results=True,
        )

    def _use_enhanced_config(self) -> bool:
        return self._lang == "en" and not self._enhanced_config_rejected

    def _open_stream(self, client, primary: str, alternatives: list[str], request_gen):
        """Open the gRPC stream, degrading once if the enhanced config is refused."""
        enhanced = self._use_enhanced_config()
        try:
            return client.streaming_recognize(
                config=self._streaming_config(primary, alternatives, enhanced=enhanced),
                requests=request_gen(),
            )
        except Exception as exc:
            if not enhanced:
                raise
            self._enhanced_config_rejected = True
            logger.warning(
                "STT telephony model/adaptation rejected (%s) — retrying with the "
                "default model for lang=%s", str(exc)[:200], self._lang,
            )
            return client.streaming_recognize(
                config=self._streaming_config(primary, alternatives, enhanced=False),
                requests=request_gen(),
            )

    def _stream_until_closed(self, client):
        primary = STT_PRIMARY.get(self._lang, "si-LK")
        alternatives = STT_ALTERNATIVES.get(self._lang, ["en-US"])
        epoch = self._stream_epoch
        started_at = self._clock()
        self._last_voice_activity = started_at

        dropped = self._trim_stale_backlog()
        if dropped:
            logger.warning(
                "STT dropped %d stale frames buffered across the stream swap "
                "(lang=%s)", dropped, self._lang,
            )

        logger.info(
            "STT gRPC stream opening (primary=%s, alts=%s, enhanced=%s)",
            primary, alternatives, self._use_enhanced_config(),
        )

        def request_gen():
            for chunk in self._audio_generator(epoch=epoch, started_at=started_at):
                yield google_speech.StreamingRecognizeRequest(audio_content=chunk)

        responses = self._open_stream(client, primary, alternatives, request_gen)
        logger.info("STT gRPC stream connected — waiting for speech...")
        for response in responses:
            if not self._running:
                break
            self._last_voice_activity = self._clock()
            for result in response.results:
                if result.alternatives:
                    transcript = result.alternatives[0].transcript.strip()
                    if result.is_final:
                        if self._privacy_safe:
                            logger.info("smartpbx_media event=stt_provider_final")
                        else:
                            logger.info("STT final: %r", transcript)
                        if transcript:
                            self._on_final(transcript)
                    else:
                        if self._privacy_safe:
                            logger.info("smartpbx_media event=stt_provider_interim")
                        else:
                            logger.info("STT interim: %r", transcript)
                        if transcript and self._on_interim:
                            self._on_interim(transcript)


class AzureSTTStream:
    """Streams audio to Azure Speech-to-Text — drop-in alternative to GoogleSTTStream.

    Mirrors the same interface (start/stop/feed + on_final_result / on_interim_result
    callbacks fired from background threads) so it swaps in via the STT_PROVIDER env var.

    Twilio delivers mulaw 8 kHz; Azure's PushAudioInputStream wants PCM, so each fed
    chunk is decoded mulaw â†’ PCM16 (audioop) before being written. Uses a fixed
    language per call (si-LK / ta-IN) — for a Sinhala-only line that tends to beat
    Google's alternative_language_codes code-switching, which was part of why Google
    rarely committed a final result for conversational Sinhala. Azure fires its own
    `recognized` (final) events, so the 1.5 s interim-endpointing fallback still
    applies but is no longer the only path to a final.
    """

    def __init__(
        self,
        on_final_result: Any,
        on_interim_result: Any = None,
        lang: str = "si",
        privacy_safe: bool = False,
    ):
        self._on_final = on_final_result
        self._on_interim = on_interim_result
        self._lang = lang
        self._privacy_safe = privacy_safe
        self._chunk_count = 0
        self._running = False
        self._push_stream = None
        self._recognizer = None

    def start(self):
        if not AZURE_STT_AVAILABLE:
            logger.error("Cannot start Azure STT — azure-cognitiveservices-speech not installed")
            return
        if audioop is None:
            logger.error("Cannot start Azure STT — audioop unavailable (install audioop-lts on 3.13+)")
            return
        if not AZURE_SPEECH_KEY:
            logger.error("Cannot start Azure STT — AZURE_SPEECH_KEY not set")
            return

        primary = STT_PRIMARY.get(self._lang, "si-LK")
        speech_config = azure_speech.SpeechConfig(
            subscription=AZURE_SPEECH_KEY, region=AZURE_SPEECH_REGION,
        )
        speech_config.speech_recognition_language = primary
        # FOLLOW-UP (not applied here): Azure's own segmentation silence
        # (PropertyId.Speech_SegmentationSilenceTimeoutMs, ~500ms default) is the
        # ~549ms measured interim→final gap before our grace even starts. Lowering
        # it would cut more latency, but it changes recognition segmentation and
        # cannot be verified without live Azure, so it is left for a measured,
        # separately-tested change rather than forced blind.
        # 8 kHz / 16-bit / mono PCM — what mulaw decodes to.
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

        # Bias English recognition toward the booking domain. English only —
        # phrase lists are language-specific, so si/ta/ar keep the bare config.
        # Defensive: a missing PhraseListGrammar (older SDK) or any failure here
        # must never prevent recognition from starting.
        phrase_list_grammar = getattr(azure_speech, "PhraseListGrammar", None)
        if self._lang == "en" and EN_STT_PHRASE_LIST and phrase_list_grammar is not None:
            try:
                phrase_grammar = phrase_list_grammar.from_recognizer(self._recognizer)
                for phrase in EN_STT_PHRASE_LIST:
                    phrase_grammar.addPhrase(phrase)
                logger.info("Azure STT English phrase list applied (%d phrases)", len(EN_STT_PHRASE_LIST))
            except Exception:
                logger.warning("Azure STT phrase list not applied", exc_info=True)

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
        # Mirrors feed()'s guard: a residual SDK-thread callback can still
        # fire after stop() has flipped this False (the async
        # stop_continuous_recognition_async().get() above does not
        # guarantee no callback is already in flight). Without this a late
        # interim can arm a fresh endpointing timer after teardown.
        if not self._running:
            return
        text = (evt.result.text or "").strip()
        if text and self._on_interim:
            if self._privacy_safe:
                logger.info("smartpbx_media event=stt_provider_interim")
            else:
                logger.info("Azure STT interim: %r", text)
            self._on_interim(text)

    def _on_recognized(self, evt):
        # See _on_recognizing — same post-teardown guard.
        if not self._running:
            return
        if evt.result.reason != azure_speech.ResultReason.RecognizedSpeech:
            return
        text = (evt.result.text or "").strip()
        if text:
            if self._privacy_safe:
                logger.info("smartpbx_media event=stt_provider_final")
            else:
                logger.info("Azure STT final: %r", text)
            self._on_final(text)

    def _on_canceled(self, evt):
        logger.warning(
            "Azure STT canceled (lang=%s): reason=%s detail=%s",
            self._lang, evt.reason, getattr(evt, "error_details", ""),
        )


def _make_stt(
    on_final_result: Any,
    on_interim_result: Any,
    lang: str,
    privacy_safe: bool = False,
):
    """Build the configured STT backend. STT_PROVIDER: 'google' (default) | 'azure'.

    Falls back to Google if Azure is selected but its SDK/audioop is missing.
    """
    if STT_PROVIDER == "azure":
        if AZURE_STT_AVAILABLE and audioop is not None:
            return AzureSTTStream(on_final_result, on_interim_result, lang, privacy_safe)
        logger.error("STT_PROVIDER=azure but Azure STT unavailable — falling back to Google")
    return GoogleSTTStream(on_final_result, on_interim_result, lang, privacy_safe)


# ---------------------------------------------------------------------------
# Prompt-cache layout: the stable system prompt is the cache_control-marked
# block; the volatile booking-slots note rides behind it uncached so slot
# updates never invalidate the ~8k-token cached prefix (tools + system).
# ---------------------------------------------------------------------------

_BOOKING_SLOTS_NOTE_PREFIX = (
    "\n\nBOOKING DETAILS COLLECTED SO FAR THIS CALL (the guest already "
    "gave these — do NOT re-ask; only confirm if genuinely unsure):\n"
)


def _build_claude_system_blocks(
    stable_system_prompt: str,
    booking_slots_note: str = "",
) -> list[dict[str, Any]]:
    system_blocks: list[dict[str, Any]] = [{
        "type": "text",
        "text": stable_system_prompt,
        "cache_control": {"type": "ephemeral"},
    }]
    if booking_slots_note:
        system_blocks.append({
            "type": "text",
            "text": booking_slots_note,
        })
    return system_blocks


def _split_claude_system_blocks(system_prompt: str) -> list[dict[str, Any]]:
    if _BOOKING_SLOTS_NOTE_PREFIX in system_prompt:
        stable, note_tail = system_prompt.split(_BOOKING_SLOTS_NOTE_PREFIX, 1)
        return _build_claude_system_blocks(
            stable,
            f"{_BOOKING_SLOTS_NOTE_PREFIX}{note_tail}"
            if note_tail
            else "",
        )
    return _build_claude_system_blocks(system_prompt)


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
        websocket: WebSocket | None,
        lang: str,
        anthropic_client: AsyncAnthropic | None = None,
        openai_client: AsyncOpenAI | None = None,
        gemini_client=None,
        media_transport: Any | None = None,
        llm_provider: str | None = None,
        model: str | None = None,
    ):
        self.ws = websocket
        self._media_transport = media_transport
        self.anthropic_client = anthropic_client
        self.client = openai_client  # OpenAI client (kept for openai provider)
        self.gemini_client = gemini_client
        self._gemini_tts_client: Any = None
        self.lang = lang
        self.llm_provider = LLM_PROVIDER if llm_provider is None else llm_provider
        if self.llm_provider not in {"claude", "gemini", "openai"}:
            raise ValueError(f"invalid LLM provider: {self.llm_provider}")
        self.model = MODEL if model is None else model
        self.system_prompt = _build_system_prompt(lang)
        if self.llm_provider == "claude":
            self.tools = get_tools()
        elif self.llm_provider == "gemini":
            self.tools = get_tools_gemini()
        else:
            self.tools = get_tools_openai()

        self.stream_sid: str | None = None
        self.call_sid: str = "unknown"
        self.caller_phone: str = "unknown"
        # Booking slots captured from tool calls, re-injected into the system
        # context every turn so they survive history trimming (a long
        # number-retry loop would otherwise evict the early date/guest turns).
        self._booking_slots: dict[str, str] = {}
        # Active DTMF keypad collector while collect_number_via_keypad is running.
        self._dtmf_collector: DtmfCollector | None = None
        self.history: list[dict] = []
        self.full_transcript: list[dict[str, str]] = []
        self.call_start_time: str = ""

        self._event_loop: asyncio.AbstractEventLoop | None = None
        # Provider callbacks originate on worker threads.  Submission and
        # teardown share this lock so a callback is either registered for the
        # closing drain or refused before it can touch event-loop state.
        self._stt_callback_lock = threading.Lock()
        self._stt_callback_futures: set[Any] = set()
        self._stt_callback_errors = 0
        self._stt_closing = False
        self._teardown_dispatch_closed = False
        self._smartpbx_deferred_tts_closed = False
        self._is_speaking = False
        self._speaking_since = 0.0
        self._speak_lock = asyncio.Lock()
        self._ws_lock = asyncio.Lock()
        self._speak_generation: int = 0
        self._assistant_turn_generation: int = -1
        self._assistant_turn_generated_sentences: list[str] = []
        self._delivered_sentences: list[str] = []
        self._track_assistant_turn_delivery: bool = False
        self._assistant_turn_speech_end_at: float = 0.0
        # Gemini failover state is per MediaStreamSession.
        self._gemini_failover_state: dict[str, Any] = _init_gemini_failover_state()

        self._pending_transcript = ""
        # Text from provider finals in the current utterance. Interims of a later
        # segment are appended to this so a mid-utterance continuation keeps the
        # already-committed words. Empty on the interim-only path (Google), where
        # each cumulative interim simply overwrites the pending text.
        self._committed_transcript = ""
        self._latest_interim = ""
        self._endpointing_handle: asyncio.TimerHandle | None = None
        # Held for the duration of one guest turn (dispatch → agent finishes
        # responding). A stale endpointing timer or a late final/interim that
        # fires while a turn is in flight must not start a second llm_round.
        # The monotonic turn id lets an in-flight turn's own flush release the
        # guard without clobbering a newer turn started by a barge-in.
        self._utterance_dispatched = False
        # Monotonic stamp of the moment the guard above was claimed. Only read
        # while the guard is held, purely to bound the post-dispatch telemetry's
        # elapsed_ms; never used for control flow.
        self._utterance_dispatched_at: float | None = None
        # Set when an endpointing deadline fired while a turn still owned the
        # guard and left admitted caller speech buffered. The turn's release
        # re-arms the flush so that speech becomes the next turn instead of
        # sitting in the buffer until the caller speaks again.
        self._deferred_flush_pending: bool = False
        self._utterance_turn = 0
        self._last_guest_utterance_raw: str = ""
        # Capture-mode keeps endpointing looser while the caller is dictating
        # a number or name across fragments, and combines the fragments into one
        # utterance instead of spending an LLM turn on each.
        self._capture_mode_active: bool = False
        self._capture_mode_turns_left: int = 0
        # Set while the turn that completed a capture is running, so its
        # read-back ("your number is oh seven seven...") cannot re-arm capture
        # mode through the ask detector.
        self._capture_success_this_turn: bool = False
        # One capture-buffer-bounded log line per episode, not per overflowing final.
        self._capture_bound_logged: bool = False
        self._stt: GoogleSTTStream | AzureSTTStream | None = None

        # Live-call audio capture (mulaw chunks) for offline STT benchmarking.
        self._audio_dump: list[bytes] = []

        # No-speech re-prompt state
        self._reprompt_task: asyncio.Task | None = None
        self._reprompt_count: int = 0
        # Set only by KavyaSmartPBXSession. None preserves legacy Twilio tools.
        self._smartpbx_transfer_context: Any | None = None
        self._smartpbx_welcome_audio_pending: str | None = None
        self._smartpbx_caller_context: dict[str, str] | None = None
        self._record_echo_rejection: Callable[[int, float], None] | None = None
        self.transfer_pending = False
        self._turn_telemetry: SmartPBXTurnTelemetry | None = None
        # Set by KavyaSmartPBXSession before the first turn can begin; random
        # and opaque — never derived from Dialog, phone, CDR or transcript data.
        self._smartpbx_session_trace_id: str | None = None
        self._active_smartpbx_turn_id: str | None = None
        self._smartpbx_stt_interim_events = 0
        self._smartpbx_stt_final_events = 0
        self._interrupted_smartpbx_turn_ids: set[str] = set()
        self._tool_failed_smartpbx_turn_ids: set[str] = set()
        self._tts_failed_smartpbx_turn_ids: set[str] = set()
        self._smartpbx_barge_ins = 0
        self._smartpbx_cadence_by_turn: dict[str, dict[str, int]] = {}
        self._smartpbx_dropped_frame_baselines: dict[str, int] = {}
        self._smartpbx_dropped_frames_by_turn: dict[str, int] = {}
        # Per-owning-turn tally of provider results the dispatch guard refused:
        # {"finals": n, "interims": n, "max_elapsed_ms": n}. Keyed by turn id
        # like the dropped-frame maps above, so the barge-in / late-runner reset
        # race cannot mix two turns' counts. Event-loop-only mutation.
        self._smartpbx_post_dispatch_by_turn: dict[str, dict[str, int]] = {}
        self._smartpbx_last_finished_dropped_frames = 0
        # This permits exactly the immediately following bounded amount
        # pronoun after a deterministic rate answer or clarification. It is
        # read into each runner and committed only by the current owner.
        self._rate_followup_eligible = False
        self._smartpbx_filler_rotation = _CallFillerRotation()
        self._smartpbx_initial_filler: SmartPBXInitialFillerController | None = None
        # Direct SmartPBX specialized tool fillers are session-owned. A runner
        # can return early, be cancelled, or lose a turn while one still holds
        # `_speak_lock`; this registry gives barge/transfer/teardown one place
        # to retire every such task.
        self._smartpbx_tool_filler_tasks: set[asyncio.Task] = set()
        # Deferred model sentences can coexist with a preceding PMS tool in a
        # batch whose later tool transfers. They need equivalent terminal and
        # barge ownership rather than remaining only in a runner-local list.
        self._smartpbx_deferred_tts_tasks: set[asyncio.Task] = set()
        # Set only by the transfer prelude after it has fenced old audio. The
        # acknowledged transfer consumes it to avoid clearing a second time.
        self._smartpbx_transfer_audio_fenced = False
        # Defense-in-depth against a residual STT-thread callback (or an
        # already-scheduled run_coroutine_threadsafe hop) landing after
        # _finalize_smartpbx_turns() has run: once torn down, transcript
        # accumulation is a silent no-op rather than arming a fresh
        # endpointing timer / opening a new "owned" turn post session_summary.
        # Stays False for non-SmartPBX (Twilio) sessions, which never call
        # _finalize_smartpbx_turns().
        self._smartpbx_torn_down = False

    def _is_smartpbx_session(self) -> bool:
        return self._smartpbx_transfer_context is not None

    def _is_direct_smartpbx_english(self) -> bool:
        return (
            self._is_smartpbx_session()
            and self._media_transport is not None
            and self.lang == "en"
        )

    def _consume_smartpbx_welcome_audio_marker(self, text: str) -> bool:
        """Claim the one direct-English welcome request before it can be retried."""
        if (
            not self._is_direct_smartpbx_english()
            or self._smartpbx_welcome_audio_pending != text
        ):
            return False
        self._smartpbx_welcome_audio_pending = None
        return True

    def _is_direct_smartpbx_english_non_capture(self) -> bool:
        """Gate for the Phase B empty-retry-nudge and stream-timeout-guard
        policy only. Capture-name, capture-number and keypad flows retain
        their pre-Phase-B specialised logic (spec §5) — they must not get
        the second-attempt nudge or the timeout guard, and an exhausted
        empty response must still speak the existing per-language canned
        fallback rather than the new shared recovery line. Every OTHER use
        of `_is_direct_smartpbx_english()` (formatting, fillers, tool
        execution, telemetry) is unaffected and must keep calling that
        method directly.
        """
        return self._is_direct_smartpbx_english() and not self._is_capture_mode_active()

    def _reserve_smartpbx_initial_filler(self) -> _CallFillerLease:
        return self._smartpbx_filler_rotation.reserve(
            "initial", SMARTPBX_INITIAL_FILLER_BANK
        )

    def _reserve_smartpbx_tool_filler(self, tool_name: str) -> _CallFillerLease:
        bank = SMARTPBX_TOOL_FILLER_BANKS.get(
            tool_name, SMARTPBX_DEFAULT_FILLER_BANK
        )
        return self._smartpbx_filler_rotation.reserve(f"tool:{tool_name}", bank)

    def _provider_max_tokens(self, provider: str | None = None) -> int:
        """Per-provider output budget for one direct-SmartPBX English round.

        Claude alone is on the raised canary budget, because Sonnet 5's
        default adaptive thinking spends output tokens before any visible
        block opens (see `_resolve_smartpbx_claude_max_tokens`). Passing no
        provider keeps the original behaviour, so the OpenAI and Gemini call
        sites are unchanged; every non-direct-SmartPBX path still gets the
        global MAX_TOKENS.
        """
        if not self._is_direct_smartpbx_english():
            return MAX_TOKENS
        if provider == "claude":
            return SMARTPBX_CLAUDE_MAX_TOKENS
        return SMARTPBX_MAX_TOKENS

    def _start_initial_smartpbx_filler(
        self, *, round_idx: int, generation: int
    ) -> SmartPBXInitialFillerController | None:
        if (
            round_idx != 0
            or not self._is_direct_smartpbx_english()
            or self.transfer_pending
            or self._is_speaking
            or self._is_capture_mode_active()
            or generation != self._speak_generation
        ):
            return None

        # Provider failover (Gemini -> Claude) runs the whole turn again on a
        # new provider without resetting turn/generation state. If the failed
        # round already armed a filler that hasn't spoken yet, adopt THAT
        # controller instead of starting a second timer -- one filler per
        # turn, not one per provider attempt. Left alone, the old controller
        # would become unreachable (this call would overwrite both
        # self._smartpbx_initial_filler and runner.initial_filler) while its
        # task keeps running, orphaned, free to speak over the new provider's
        # first sentence with nothing left able to cancel it.
        existing = self._smartpbx_initial_filler
        if (
            existing is not None
            and not existing.spoke
            and existing.generation == generation
        ):
            controller = existing
        else:
            async def speak(text: str, *, generation: int) -> None:
                await self._invoke_speak(text, generation=generation)

            async def clear_audio() -> None:
                # A late delta from an interrupted runner must not clear the newer
                # turn's transport generation.
                runner = _smartpbx_runner_context.get()
                active_turn_id = self._active_smartpbx_turn_id
                barge_ins = self._smartpbx_barge_ins
                if (
                    runner is None
                    or self.transfer_pending
                    or generation != self._speak_generation
                    or runner.speak_generation != generation
                    or self._assistant_turn_generation != generation
                    or (
                        runner.turn_id is not None
                        and runner.turn_id != active_turn_id
                    )
                ):
                    return
                await self._clear_media_audio()
                # Retiring the filler is a handoff to this turn's own real
                # content, not a caller interruption. Re-anchor both ownership
                # fences together, but only if this exact runner still owns the
                # turn and the clear made the expected single generation bump.
                if (
                    _smartpbx_runner_context.get() is runner
                    and self._active_smartpbx_turn_id == active_turn_id
                    and self._smartpbx_barge_ins == barge_ins
                    and not self.transfer_pending
                    and runner.speak_generation == generation
                    and (
                        runner.turn_id is None
                        or runner.turn_id == active_turn_id
                    )
                    and self._assistant_turn_generation == generation
                    and self._speak_generation == generation + 1
                ):
                    runner.speak_generation = self._speak_generation
                    self._assistant_turn_generation = self._speak_generation

            filler_lease = self._reserve_smartpbx_initial_filler()
            controller = SmartPBXInitialFillerController(
                speak=speak,
                generation=generation,
                delay_seconds=SMARTPBX_INITIAL_FILLER_DELAY_SECONDS,
                clear_audio=clear_audio,
                text=filler_lease.text,
                lease=filler_lease,
            )
            controller.start()
        runner = _smartpbx_runner_context.get()
        if runner is not None:
            runner.initial_filler = controller
        # A stale runner can still unwind after a barge-in. It may retain its
        # own controller, but it must never replace the newer runner's one.
        if runner is None or runner.turn_id == self._active_smartpbx_turn_id:
            self._smartpbx_initial_filler = controller
        return controller

    async def _finish_initial_smartpbx_filler(
        self, controller: SmartPBXInitialFillerController | None
    ) -> None:
        if controller is None:
            return
        await controller.on_session_finish()
        if self._smartpbx_initial_filler is controller:
            self._smartpbx_initial_filler = None

    async def _finish_smartpbx_tool_filler(
        self, task: asyncio.Task | None, *, cancel: bool = False
    ) -> None:
        """Join a specialized filler, or retire it on a real ownership loss."""
        if task is None:
            return
        if cancel and not task.done():
            task.cancel()
        if cancel:
            await asyncio.gather(task, return_exceptions=True)
            return
        await task

    async def _cancel_smartpbx_round_tts(
        self, tts_tasks: list[asyncio.Task],
    ) -> None:
        """Retire this runner's deferred model speech on an ownership exit."""
        pending = [task for task in tts_tasks if not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        tts_tasks.clear()

    async def _cancel_smartpbx_deferred_tts(self) -> None:
        """Cancel and join model-speech tasks held by this SmartPBX session."""
        tasks = set(self._smartpbx_deferred_tts_tasks)
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _cancel_smartpbx_deferred_tts_now(self) -> None:
        """Synchronously request terminal cancellation of deferred model speech."""
        for task in tuple(self._smartpbx_deferred_tts_tasks):
            if not task.done():
                task.cancel()

    def _start_smartpbx_round_tts(
        self, text: str, *, generation: int, sentence: str,
    ) -> asyncio.Task | None:
        """Start model speech with session/runner cancellation ownership."""
        if (
            self._is_direct_smartpbx_english()
            and self._smartpbx_deferred_tts_closed
        ):
            return None
        task = asyncio.create_task(
            self._invoke_speak(text, generation=generation, sentence=sentence)
        )
        if not self._is_direct_smartpbx_english():
            return task
        self._smartpbx_deferred_tts_tasks.add(task)
        task.add_done_callback(self._smartpbx_deferred_tts_tasks.discard)
        owner = asyncio.current_task()
        if owner is not None:
            owner.add_done_callback(
                lambda _owner: None if task.done() else task.cancel()
            )
        return task

    def _start_smartpbx_tool_filler(
        self,
        text: str,
        *,
        generation: int,
        lease: _CallFillerLease | None = None,
    ) -> asyncio.Task:
        """Start a direct-path specialized filler under runner/session ownership."""
        started = False

        async def speak_filler() -> None:
            nonlocal started
            started = True
            if lease is not None:
                lease.commit()
            await self._invoke_speak(text, generation=generation, sentence=text)

        task = asyncio.create_task(speak_filler())
        if lease is not None:
            task.add_done_callback(
                lambda _task: lease.release() if not started else None
            )
        self._smartpbx_tool_filler_tasks.add(task)
        task.add_done_callback(self._smartpbx_tool_filler_tasks.discard)
        runner = _smartpbx_runner_context.get()
        if runner is not None:
            runner.tool_filler_tasks.add(task)
            task.add_done_callback(runner.tool_filler_tasks.discard)
        owner = asyncio.current_task()
        if owner is not None:
            # Covers cancellation while the provider runner is between awaits;
            # the explicit batch/terminal paths below still await the child.
            owner.add_done_callback(
                lambda _owner: None if task.done() else task.cancel()
            )
        return task

    async def _cancel_smartpbx_tool_fillers(
        self, *, runner: _SmartPBXRunnerContext | None = None
    ) -> None:
        """Cancel and await specialized fillers owned by this session/runner."""
        tasks = (
            set(runner.tool_filler_tasks)
            if runner is not None else set(self._smartpbx_tool_filler_tasks)
        )
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _cancel_smartpbx_tool_fillers_now(self) -> None:
        """Synchronously request cancellation from non-awaiting terminal paths."""
        for task in tuple(self._smartpbx_tool_filler_tasks):
            if not task.done():
                task.cancel()

    async def _prepare_smartpbx_transfer_handoff(
        self,
        *,
        tts_tasks: list[asyncio.Task],
        initial_filler: SmartPBXInitialFillerController | None,
        generation: int,
        tool_filler_task: asyncio.Task | None = None,
    ) -> int | None:
        """Fence active turn speech before the canonical transfer delivery barrier.

        The transfer tool supplies its own canonical announcement and waits for
        its delivery before MCP execution. No model preamble, initial filler,
        or specialized tool filler may delay, overlap, or be mistaken for that
        announcement.
        """
        # A quiet failed transfer must leave the current generation and
        # delivery ledger alone so the provider can continue its normal tool
        # batch.  Conversely, any real model/initial/specialized preamble has
        # to be retired before the canonical handoff line may be delivered.
        active_preamble = (
            bool(tts_tasks)
            or (
                self._assistant_turn_generation == generation
                and bool(self._assistant_turn_generated_sentences)
            )
            or (
                initial_filler is not None
                and initial_filler.spoke
                and not getattr(initial_filler, "_cleared_after_spoke", False)
            )
            or tool_filler_task is not None
        )
        await self._finish_smartpbx_tool_filler(tool_filler_task, cancel=True)
        await self._finish_initial_smartpbx_filler(initial_filler)
        if not self._current_smartpbx_runner_owns_shared_state():
            return None
        # A caller that has already lost its generation must not claim the
        # transfer clear.  In particular, direct provider harnesses have no
        # endpointing wrapper to make that ownership implicit.
        if self.transfer_pending or generation != self._speak_generation:
            return None
        if not active_preamble:
            # The transfer outcome is not known until execute_tool returns.
            # Leave a quiet handoff unfenced here; a later acknowledged
            # enter_transfer_pending() owns its one clear and reanchors the
            # current runner, while an immediate failure continues normally.
            return generation
        # Transfer owns one authoritative generation fence even when no old
        # preamble has started. Otherwise its queued audio could overlap the
        # canonical announcement/delivery barrier.
        fenced_from_generation = generation
        generation = await self._smartpbx_fence_stalled_generation(
            tts_tasks=tts_tasks, gen=fenced_from_generation,
        )
        tts_tasks.clear()
        # A transfer prelude may suppress enter_transfer_pending()'s ordinary
        # clear only when this exact runner really advanced the generation and
        # survived the awaited transport clear.  A no-op stale fence must not
        # consume that later clear.
        did_fence = (
            generation == self._speak_generation
            and generation == self._assistant_turn_generation
            and generation == fenced_from_generation + 1
        )
        if (
            not did_fence
            or not self._current_smartpbx_runner_owns_shared_state()
        ):
            return None
        # Only an exact, ownership-safe fence may replace the old delivery
        # ledger with the canonical transfer announcement's ledger. A newer
        # turn that wins during the awaited clear keeps its own accounting.
        self._start_assistant_turn_delivery_tracking()
        self._smartpbx_transfer_audio_fenced = True
        return generation

    def _ensure_smartpbx_turn_telemetry(self) -> SmartPBXTurnTelemetry | None:
        if not self._is_direct_smartpbx_english():
            return None
        if self._turn_telemetry is None:
            self._turn_telemetry = SmartPBXTurnTelemetry(
                session_trace_id=self._smartpbx_session_trace_id or "",
            )
        elif self._smartpbx_session_trace_id:
            # Injected/test telemetry may predate the session binding; the
            # setter is set-once, so an already-bound trace is never rebound.
            self._turn_telemetry.set_session_trace_id(self._smartpbx_session_trace_id)
        return self._turn_telemetry

    def _finalize_smartpbx_turns(self) -> None:
        """Idempotent pipeline teardown: summarize unfinished turns exactly once.

        Runs immediately before session_summary. Uses the existing terminal
        outcomes, carries the existing cadence/drop counters, and retires all
        turn-owned state so per-turn maps are empty afterwards. A delayed
        runner completing later finds no open turn and emits nothing.
        """
        # Set first, unconditionally: a residual STT callback landing after
        # this point (even if telemetry was never constructed) must not
        # accumulate transcript or arm a new endpointing timer.
        self._smartpbx_torn_down = True
        self._smartpbx_transfer_audio_fenced = False
        self._cancel_smartpbx_deferred_tts_now()
        self._cancel_smartpbx_tool_fillers_now()
        telemetry = self._turn_telemetry
        if telemetry is None:
            return
        outcome = "transfer_pending" if self.transfer_pending else "cancelled"
        finalized = telemetry.finalize_open_turns(
            outcome,
            counts_for_turn=lambda turn_id: {
                "dropped_frames": self._smartpbx_dropped_frames_for_turn(turn_id),
                **self._smartpbx_cadence_counts(turn_id),
                **self._post_dispatch_counts_for_turn(turn_id),
            },
        )
        for turn_id in finalized:
            self._retire_smartpbx_turn(turn_id)
        for turn_id in list(self._interrupted_smartpbx_turn_ids):
            self._retire_smartpbx_turn(turn_id)
        self._interrupted_smartpbx_turn_ids.clear()
        self._tool_failed_smartpbx_turn_ids.clear()
        self._tts_failed_smartpbx_turn_ids.clear()
        self._smartpbx_cadence_by_turn.clear()
        self._smartpbx_dropped_frame_baselines.clear()
        self._smartpbx_dropped_frames_by_turn.clear()
        self._smartpbx_post_dispatch_by_turn.clear()
        self._active_smartpbx_turn_id = None

    def _current_smartpbx_turn_id(self) -> str | None:
        """Use the task's captured turn while a runner is active."""
        runner = _smartpbx_runner_context.get()
        return runner.turn_id if runner is not None else self._active_smartpbx_turn_id

    def _runner_speak_generation(self, fallback: int) -> int:
        """Use the runner's current fence after a filler-only clear."""
        runner = _smartpbx_runner_context.get()
        return fallback if runner is None else runner.speak_generation

    def _owns_smartpbx_tts_delivery(self, expected_generation: int) -> bool:
        """Fence Sinhala SmartPBX audio without changing general runner semantics."""
        if not self._is_smartpbx_session():
            return True
        if (
            self._smartpbx_torn_down
            or self._speak_generation != expected_generation
        ):
            return False
        runner = _smartpbx_runner_context.get()
        if runner is None or runner.turn_id is None:
            return True
        return (
            runner.turn_id == self._active_smartpbx_turn_id
            and runner.speak_generation == expected_generation
        )

    def _current_smartpbx_runner_owns_shared_state(
        self, *, tool_executed: bool = False
    ) -> bool:
        """Whether this runner may mutate call state after an awaited boundary.

        Fix-4 (pre-merge review): a full task-cancellation approach for the
        teardown race was investigated and rejected — cancelling a task
        mid-``await execute_tool(...)`` risks a half-committed side effect on
        the wire (the booking HTTP request may already be in flight when
        CancelledError lands), which is worse than letting it finish. This
        codebase already discards a tool's result when ownership is lost
        (e.g. an ordinary barge-in mid-tool), so teardown racing ahead of an
        in-flight tool is the same shape of event, just via
        ``_smartpbx_torn_down`` clearing ``_active_smartpbx_turn_id`` instead
        of a newer turn claiming it. The one gap was that the discard was
        silent. When the caller passes ``tool_executed=True`` (a tool such as
        create_booking already executed this turn) and ownership has been
        lost, emit one bounded, payload-free telemetry event before
        discarding — otherwise a successfully-executed tool becomes a silent
        success with no trace.
        """
        if not self._is_direct_smartpbx_english():
            return True
        runner = _smartpbx_runner_context.get()
        # Older injected/direct callers may have no task-local wrapper or no
        # telemetry-owned turn. They have no runner ownership to validate;
        # only an explicitly captured turn can be stale after a barge-in.
        if runner is None or runner.turn_id is None:
            return not self._smartpbx_torn_down
        owns = (
            not self._smartpbx_torn_down
            and runner.turn_id == self._active_smartpbx_turn_id
            and runner.speak_generation == self._speak_generation
        )
        if not owns and tool_executed:
            _emit_smartpbx_turn_telemetry(
                "turn_stage",
                event="turn_stage",
                session_trace_id=getattr(self, "_smartpbx_session_trace_id", "") or "",
                turn_id=runner.turn_id,
                stage="late_tool_completion",
            )
        return owns

    def _current_smartpbx_runner_can_execute_tools(self) -> bool:
        """Keep a barged-out task from crossing the tool side-effect boundary."""
        return self._current_smartpbx_runner_owns_shared_state()

    def _smartpbx_runner_raw_utterance(self) -> str:
        """Return raw capture input owned by this task, never a newer turn's."""
        runner = _smartpbx_runner_context.get()
        if self._is_direct_smartpbx_english() and runner is not None:
            return runner.raw_utterance
        return self._last_guest_utterance_raw

    def _current_smartpbx_dropped_frames(self) -> int:
        return max(int(getattr(self._media_transport, "frames_dropped_total", 0)), 0)

    def _smartpbx_dropped_frames_for_turn(self, turn_id: str) -> int:
        """Return overflow reported through this turn's bound generation."""
        recorded = self._smartpbx_dropped_frames_by_turn.get(turn_id)
        if recorded is not None:
            return recorded
        # Legacy test doubles do not emit transport events. Production turns
        # initialize the bound counter before audio can be sent.
        runner = _smartpbx_runner_context.get()
        if runner is not None and runner.turn_id == turn_id:
            baseline = runner.dropped_frame_baseline
        else:
            baseline = self._smartpbx_dropped_frame_baselines.get(
                turn_id, self._smartpbx_last_finished_dropped_frames,
            )
        current = self._current_smartpbx_dropped_frames()
        return max(current - baseline, 0)

    def _retire_smartpbx_turn(self, turn_id: str) -> None:
        self._smartpbx_cadence_by_turn.pop(turn_id, None)
        self._smartpbx_dropped_frame_baselines.pop(turn_id, None)
        self._smartpbx_dropped_frames_by_turn.pop(turn_id, None)
        self._smartpbx_post_dispatch_by_turn.pop(turn_id, None)
        self._smartpbx_last_finished_dropped_frames = (
            self._current_smartpbx_dropped_frames()
        )

    def _mark_smartpbx_turn(self, stage: str, *, at_ns: int | None = None) -> None:
        telemetry = self._ensure_smartpbx_turn_telemetry()
        turn_id = self._current_smartpbx_turn_id()
        if telemetry is not None and turn_id is not None:
            telemetry.mark(turn_id, stage, at_ns=at_ns)

    def _mark_smartpbx_turn_once(self, stage: str, *, at_ns: int | None = None) -> None:
        telemetry = self._ensure_smartpbx_turn_telemetry()
        turn_id = self._current_smartpbx_turn_id()
        if telemetry is not None and turn_id is not None:
            telemetry.mark_once(turn_id, stage, at_ns=at_ns)

    def _on_smartpbx_transport_event(
        self, turn_id: str, generation: int, stage: str, monotonic_ns: int, dropped_frames: int
    ) -> None:
        telemetry = self._turn_telemetry
        if telemetry is None or not telemetry.has_active_turn(turn_id):
            return
        snapshot = getattr(self._media_transport, "cadence_summary_for_generation", None)
        if callable(snapshot):
            self._smartpbx_cadence_by_turn[turn_id] = snapshot(generation)
        if stage == "frame_dropped":
            self._smartpbx_dropped_frames_by_turn[turn_id] = (
                self._smartpbx_dropped_frames_by_turn.get(turn_id, 0) + 1
            )
            return
        telemetry.mark_once(turn_id, stage, at_ns=monotonic_ns)

    def _smartpbx_cadence_counts(self, turn_id: str) -> dict[str, int]:
        """Return fixed numeric transport aggregates for this opaque turn only."""
        return dict(self._smartpbx_cadence_by_turn.get(turn_id, {}))

    async def _smartpbx_speak_recovery_and_finish(
        self, *, tool_executed: bool, gen: int, full_text: str
    ) -> str:
        """Speak the one shared SmartPBX recovery line and end the turn.

        The single policy used by all three provider runners for both
        failure modes this phase covers: an empty response that has used up
        its one retry, and a stream timeout/stall. ``tool_executed`` selects
        the line — before any tool/side effect the caller hears the retry
        line; once a tool may have started this turn is never replayed, so
        the caller hears the "clear update" line instead. Both branches
        revalidate ownership (via `_invoke_speak`/`_append_assistant_history`,
        which already no-op for a stale runner) so a barge-in mid-recovery
        never produces stale speech or history.
        """
        if not self._current_smartpbx_runner_owns_shared_state():
            return ""
        recovery_text = (
            SMARTPBX_LLM_TOOL_STARTED_RECOVERY_TEXT
            if tool_executed
            else SMARTPBX_LLM_EMPTY_RETRY_RECOVERY_TEXT
        )
        await self._invoke_speak(recovery_text, generation=gen, sentence=recovery_text)
        if not self._current_smartpbx_runner_owns_shared_state():
            return ""
        self._append_assistant_history({
            "role": "assistant",
            "content": recovery_text,
        })
        return _join_turn(full_text, recovery_text)

    async def _smartpbx_cancel_stalled_filler(self, gen: int) -> None:
        """Cancel and await this stalled generation's initial filler.

        Must run — and be fully awaited — BEFORE any generation fence or
        recovery speech. The filler's own TTS may currently hold
        ``_speak_lock`` (it can be mid-flight at the exact moment the
        provider stream stalls). Cancelling its task and awaiting it lets
        that lock release: ``_speak``'s ``async with self._speak_lock``
        releases on ``CancelledError`` the same as any other exit, and
        ``_invoke_tts`` never swallows cancellation. Skipping this step (or
        doing it after the recovery line tries to speak) is exactly how the
        recovery speak's own ``_speak_lock`` acquisition would deadlock
        behind a filler TTS call that will otherwise never finish on its own.

        Only ever touches the filler that belongs to the SAME generation that
        just stalled, matching ``_smartpbx_fence_stalled_generation``'s own
        guard. A filler for an older generation is already retired and
        irrelevant; a filler for a NEWER generation belongs to a turn that has
        already taken over (a barge-in raced ahead of this stall) and must be
        left completely alone — cancelling it here would silence a live turn's
        filler out from under it.

        The generation check alone is NOT sufficient to identify a newer turn,
        which is why the lookup is the task-local ``runner.initial_filler``
        first and only falls back to the session-wide
        ``self._smartpbx_initial_filler`` while this runner still owns the
        active turn — the same two-step the post-turn cleanup in
        ``_process_utterance_bound_runner`` uses. ``_speak_generation`` is
        bumped only by barge-in, transfer-pending and this fence, NOT per
        utterance, so a fresh turn that starts while this one is still stalled
        shares this exact generation and would sail through a
        generation-only guard while its own filler is live.
        """
        runner = _smartpbx_runner_context.get()
        controller = runner.initial_filler if runner is not None else None
        if controller is None and (
            runner is None or runner.turn_id == self._active_smartpbx_turn_id
        ):
            controller = self._smartpbx_initial_filler
        if controller is None or controller.generation != gen:
            return
        await self._finish_initial_smartpbx_filler(controller)
        # Step 3: drop every stored reference to the retired controller.
        # ``_finish_initial_smartpbx_filler`` clears the session-wide one; the
        # task-local one is this method's job, so the post-turn cleanup cannot
        # re-adopt a controller this transition has already retired.
        if runner is not None and runner.initial_filler is controller:
            runner.initial_filler = None

    async def _smartpbx_fence_stalled_generation(
        self, *, tts_tasks: list[asyncio.Task], gen: int
    ) -> int:
        """Retire a stalled round's in-flight TTS before recovery speaks.

        Two things must both finish before the recovery line is spoken, or a
        late-arriving TTS task from the dead generation can deliver audio
        after (or on top of) the recovery line:
        1. Cancel-and-await any of THIS round's per-sentence TTS tasks that
           are still pending (queued sentences from partial content spoken
           before the stall). The stalled generation's initial filler is
           NOT among these — it is cancelled separately and earlier, by
           ``_smartpbx_cancel_stalled_filler``, before this method ever runs.
        2. Fence the transport against undelivered old-generation audio the
           same way genuine barge-in does: bump ``_speak_generation`` and
           clear queued transport audio — reusing the existing generation
           fence / clear mechanism, not a new one.

        Only fences/clears when ``gen`` is still the active generation. If a
        newer turn or barge-in has already taken over, that path owns its
        own fence and clear; this only cleans up this generation's dangling
        TTS tasks and leaves the newer turn's audio untouched (same rule the
        filler's ``clear_audio`` follows).

        When this runner still owns ``gen`` and performs the bump, the
        active runner context's ``speak_generation`` is advanced to match in
        the same breath — otherwise the recovery speak that follows would
        fail its own ownership check (``runner.speak_generation`` stuck on
        the dead generation) and silently no-op instead of ever being heard.
        A runner that had already lost ``gen`` before this call (the early
        return above) must NOT have its runner context touched here: forcing
        it to the current generation would hand a barged-out/stale runner
        ownership it no longer has, letting it clobber a newer turn.

        Returns the generation the recovery line should speak on.
        """
        pending = [task for task in tts_tasks if not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        runner = _smartpbx_runner_context.get()
        runner_turn_id = runner.turn_id if runner is not None else None
        active_turn_id = self._active_smartpbx_turn_id
        barge_ins = self._smartpbx_barge_ins
        transfer_pending = self.transfer_pending
        if (
            transfer_pending
            or gen != self._speak_generation
            or self._assistant_turn_generation != gen
            or (
                runner is not None
                and (
                    runner.speak_generation != gen
                    or (
                        runner_turn_id is not None
                        and runner_turn_id != active_turn_id
                    )
                )
            )
        ):
            return self._speak_generation
        self._speak_generation += 1
        fenced_generation = self._speak_generation
        await self._clear_media_audio()
        # Re-anchor recovery only if the exact runner still owns this turn
        # after the awaited clear. A barge-in or newer turn leaves the old
        # runner/tracker stale so the existing recovery fences suppress it.
        if (
            _smartpbx_runner_context.get() is runner
            and self._active_smartpbx_turn_id == active_turn_id
            and self._smartpbx_barge_ins == barge_ins
            and self.transfer_pending == transfer_pending
            and not self.transfer_pending
            and self._speak_generation == fenced_generation == gen + 1
            and self._assistant_turn_generation == gen
            and (
                runner is None
                or (
                    runner.speak_generation == gen
                    and runner.turn_id == runner_turn_id
                    and (
                        runner_turn_id is None
                        or runner_turn_id == active_turn_id
                    )
                )
            )
        ):
            if runner is not None:
                runner.speak_generation = fenced_generation
            self._assistant_turn_generation = fenced_generation
        return self._speak_generation

    async def _smartpbx_handle_stream_timeout(
        self,
        exc: "_SmartPBXStreamTimeout",
        *,
        provider: str,
        tool_executed: bool,
        gen: int,
        full_text: str,
        tts_tasks: list[asyncio.Task] = (),
        progress: str = "none",
        retrying: bool = False,
        attempt: int = 1,
        timeout_ms: int | None = None,
        recover: bool = True,
    ) -> str:
        """Recover from an initial-response or inter-delta stall timeout.

        This is one atomic, non-interleavable recovery transition, run in
        exactly this order:
        1-3. Cancel and fully await this generation's initial filler
             (``_smartpbx_cancel_stalled_filler``) and drop the stored
             reference — done first, and BEFORE any generation fence, so a
             filler TTS call in flight when the stream stalled cannot still
             be holding ``_speak_lock`` when the recovery line tries to
             acquire it.
        4-6. Cancel/await this round's remaining per-sentence TTS tasks,
             bump ``_speak_generation`` exactly once, clear queued transport
             audio, and advance the active runner context to that same new
             generation (``_smartpbx_fence_stalled_generation``).
        7.   When ``recover`` is true, speak the shared timeout recovery line
             and end the turn (``_smartpbx_speak_recovery_and_finish``) — the
             same recovery-and-finish policy an exhausted empty response uses,
             so a stalled provider and a genuinely empty one degrade the call
             identically. When ``recover`` is false, stop after the fence and
             return without recovery speech; the caller uses that fence-only
             path to prepare an eligible Claude retry and revalidates exact
             runner ownership before issuing the next request.

        Step 8 (never touch a newer turn's filler) is enforced inside
        ``_smartpbx_cancel_stalled_filler`` via its generation guard, not
        here.
        """
        self._mark_smartpbx_turn_once("llm_timeout")
        normalized_provider = _normalized_smartpbx_timeout_provider(provider)
        phase = (
            exc.phase
            if exc.phase in {
                _SmartPBXStreamTimeout.PHASE_INITIAL,
                _SmartPBXStreamTimeout.PHASE_STALL,
            }
            else _SmartPBXStreamTimeout.PHASE_STALL
        )
        effective_timeout_ms = _bounded_smartpbx_timeout_ms(
            timeout_ms
            if timeout_ms is not None
            else (
                exc.timeout_ms
                if exc.timeout_ms is not None
                else _smartpbx_timeout_seconds_to_ms(
                    SMARTPBX_LLM_INITIAL_RESPONSE_TIMEOUT_SECONDS
                    if phase == _SmartPBXStreamTimeout.PHASE_INITIAL
                    else SMARTPBX_LLM_STALL_TIMEOUT_SECONDS
                )
            )
        )
        if self._is_smartpbx_session():
            logger.warning(
                "smartpbx_media event=llm_stream_timeout provider=%s phase=%s "
                "tool_executed=%s progress=%s retrying=%s attempt=%d timeout_ms=%d",
                normalized_provider,
                phase,
                "true" if tool_executed is True else "false",
                progress if progress in SMARTPBX_CLAUDE_STREAM_PROGRESS_VALUES else "none",
                "true" if retrying is True else "false",
                _bounded_claude_attempt(attempt),
                effective_timeout_ms,
            )
        await self._smartpbx_cancel_stalled_filler(gen)
        fenced_gen = await self._smartpbx_fence_stalled_generation(
            tts_tasks=list(tts_tasks), gen=gen
        )
        if not recover:
            return ""
        return await self._smartpbx_speak_recovery_and_finish(
            tool_executed=tool_executed, gen=fenced_gen, full_text=full_text,
        )

    def _assistant_text_for_echo_scoring(self) -> list[str]:
        if self._is_speaking:
            return list(self._assistant_turn_generated_sentences)
        return []

    def _emit_echo_rejection(self, transcript: str, score: float) -> None:
        chars = len(transcript)
        logger.info(
            "smartpbx_media event=echo_rejected chars=%d score=%.3f",
            chars,
            score,
        )
        record = getattr(self, "_record_echo_rejection", None)
        if callable(record):
            record(chars, score)

    def _is_echo(self, transcript: str) -> bool:
        if not self._is_speaking:
            return False
        if not ECHO_SUPPRESSION_ENABLED:
            return False
        if not self._is_smartpbx_session():
            return False
        transcript_tokens = _normalize_for_overlap(transcript).split()
        if len(transcript_tokens) < 5:
            return False
        if not transcript_tokens:
            return False
        transcript_starts_with_affirmation = transcript_tokens[0] in _ECHO_AFFIRMATION_TOKENS
        assistant_sentences = self._assistant_text_for_echo_scoring()
        if not assistant_sentences:
            return False
        for sentence in assistant_sentences:
            if transcript_starts_with_affirmation and not _starts_with_affirmation_token(sentence):
                continue
            transcript_ratio = _token_overlap_ratio(sentence, transcript)
            if transcript_ratio < ECHO_MATCH_MIN_RATIO:
                continue
            sentence_ratio = _token_overlap_ratio(transcript, sentence)
            if sentence_ratio < ECHO_SENTENCE_COVERAGE_MIN_RATIO:
                continue
            self._emit_echo_rejection(transcript, transcript_ratio)
            return True
        return False

    async def enter_transfer_pending(self) -> None:
        """Silence AI activity after carrier acknowledgement without closing Dialog."""
        if self.transfer_pending:
            return
        transfer_audio_fenced = self._smartpbx_transfer_audio_fenced
        runner = _smartpbx_runner_context.get()
        runner_turn_id = runner.turn_id if runner is not None else None
        active_turn_id = self._active_smartpbx_turn_id
        pre_transfer_generation = self._speak_generation
        self._smartpbx_transfer_audio_fenced = False
        await self._cancel_smartpbx_tool_fillers()
        if self._smartpbx_initial_filler is not None:
            await self._smartpbx_initial_filler.on_session_finish()
        # Transfer-pending ownership: RETAINED. Everything still buffered — a
        # half-dictated number, or ordinary speech admitted while a turn held the
        # guard — is about to be discarded, and after the hand-off nothing in
        # this process will ever record it again. The transcript is what travels
        # onward, so it goes there first.
        await self._retain_pending_speech("transfer")
        self.transfer_pending = True
        self._cancel_reprompt()
        # A keypad entry in flight belongs to a conversation that is over.
        self._cancel_dtmf_collection()
        if self._endpointing_handle:
            self._endpointing_handle.cancel()
            self._endpointing_handle = None
        self._pending_transcript = ""
        self._committed_transcript = ""
        self._latest_interim = ""
        self._deferred_flush_pending = False
        self._utterance_dispatched = False
        self._utterance_turn += 1
        self._is_speaking = False
        if not transfer_audio_fenced:
            self._speak_generation += 1
        transfer_generation = self._speak_generation
        self._capture_mode_active = False
        self._capture_mode_turns_left = 0
        if not transfer_audio_fenced:
            await self._clear_media_audio(force=True)
            # Quiet transfers deliberately defer their one clear until the
            # carrier has acknowledged. Re-anchor only the exact runner that
            # still owns the generation after that awaited clear; a barge-in
            # or newer turn changes either identity or generation and leaves
            # the old runner stale.
            if (
                _smartpbx_runner_context.get() is runner
                and runner is not None
                and runner.turn_id == runner_turn_id == active_turn_id
                and runner.speak_generation == pre_transfer_generation
                and self._active_smartpbx_turn_id == active_turn_id
                and self._speak_generation == transfer_generation
                and transfer_generation == pre_transfer_generation + 1
            ):
                runner.speak_generation = transfer_generation

    def _cancel_dtmf_collection(self) -> None:
        """Resolve any active keypad collection so its awaiter unwinds on teardown."""
        collector = self._dtmf_collector
        if collector is not None:
            self._dtmf_collector = None
            collector.cancel()

    async def feed_dtmf(self, digit: str) -> bool:
        """Feed a keypad digit to the active collector. True if it was consumed."""
        collector = self._dtmf_collector
        if collector is None:
            return False
        collector.feed(digit)
        return True

    async def _collect_number_via_keypad(self, tool_input: Any) -> str:
        """Collect the guest's number via DTMF, guarded by the keypad instruction.

        The collector is installed BEFORE the instruction is spoken, because
        guests start keying in over the prompt and the collector buffers input
        it has not been started for. The instruction is still spoken and awaited
        in full — early digits do not interrupt it — and only then is the overall
        entry window armed, so the guest keeps the whole window.
        Returns the collected digits (with a spaced readback) or a failure so the
        model can fall back to asking the guest to say the number.
        """
        # DTMF is wired for the SmartPBX path (Dialog sends clean dtmf events).
        if not self._is_smartpbx_session():
            return json.dumps({"status": "unavailable", "reason": "keypad_not_available"})
        if self._event_loop is None:
            return json.dumps({"status": "unavailable", "reason": "keypad_not_available"})

        label = ""
        if isinstance(tool_input, dict):
            label = str(tool_input.get("label") or "").strip()
        spoken_label = label or "your number"

        instruction = (
            f"Please key in {spoken_label} on your phone's keypad now, "
            "then press the hash key when you're done."
        )
        collector = DtmfCollector(
            loop=self._event_loop,
            interdigit_timeout=DTMF_INTERDIGIT_TIMEOUT_SECONDS,
            overall_timeout=DTMF_OVERALL_TIMEOUT_SECONDS,
            max_digits=DTMF_MAX_DIGITS,
        )
        self._dtmf_collector = collector
        if self._is_smartpbx_session():
            logger.info("smartpbx_media event=dtmf_collect_start")
        try:
            # Digits pressed over this prompt are already being buffered.
            await self._invoke_speak(instruction)
        except BaseException:
            # A prompt the guest never heard cannot be answered on the keypad:
            # drop the entry rather than leave a collector nothing will resolve.
            self._release_dtmf_collector(collector)
            collector.cancel("prompt_failed")
            raise
        # The entry window starts when the guest has heard what to do. An entry
        # already completed over the prompt leaves start() a no-op.
        collector.start()
        try:
            result = await collector.future
        finally:
            # External task cancellation also cancels the awaited Future, but
            # not this collector's timer handles. The collector's cancellation
            # is idempotent, so it safely cleans that case as well as the normal
            # result paths before the identity-fenced ownership release below.
            collector.cancel()
            self._release_dtmf_collector(collector)
        if self._is_smartpbx_session():
            logger.info("smartpbx_media event=dtmf_collect_done status=%s", result.get("status"))
        return json.dumps(result)

    def _release_dtmf_collector(self, collector: DtmfCollector) -> None:
        """Clear the session slot only while this collection still owns it."""
        if self._dtmf_collector is collector:
            self._dtmf_collector = None

    def _log_tool_execution(
        self, tool_name: str, tool_input: Any, *, capture_slots: bool = True,
    ) -> None:
        if capture_slots:
            self._capture_booking_slots(tool_name, tool_input)
        self._mark_smartpbx_turn_once("tool_start")
        if self._is_smartpbx_session():
            logger.info("smartpbx_media event=tool_execute tool=%s", tool_name)
        else:
            logger.info("Executing tool '%s': %s", tool_name, tool_input)

    # Slot keys the booking flow carries through its tool arguments. Captured
    # from any tool call and re-injected every turn so a long call cannot make
    # Kavya forget details the guest already gave (mid-call slot amnesia).
    _BOOKING_SLOT_KEYS: tuple[str, ...] = (
        "check_in", "check_out", "room_type", "room_name",
        "guest_name", "salutation", "guest_phone", "residency",
        "num_adults", "num_children",
    )

    def _capture_booking_slots(self, tool_name: str, tool_input: Any) -> None:
        """Merge booking-slot values from a tool call into the persistent state."""
        if tool_name not in {"check_availability", "create_booking"}:
            return
        if not isinstance(tool_input, dict):
            return
        for key in self._BOOKING_SLOT_KEYS:
            if key not in tool_input:
                continue
            value = tool_input[key]
            if value is None:
                continue
            text = str(value).strip()
            if text:
                self._booking_slots[key] = text

    def _capture_explicit_residency(self, utterance: str) -> None:
        """Persist an explicit residency statement without inferring identity."""
        residency = recognize_residency(utterance)
        if residency:
            self._booking_slots["residency"] = residency

    @staticmethod
    def _assistant_content_text(content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, dict):
            text = content.get("text")
            return text if isinstance(text, str) else ""
        if isinstance(content, list):
            return " ".join(
                block.get("text", "")
                for block in content
                if isinstance(block, dict) and isinstance(block.get("text"), str)
            )
        return ""

    @classmethod
    def _is_explicit_residency_question(cls, text: str) -> bool:
        normalized = " ".join(re.findall(r"[a-z]+", str(text).lower()))
        if "?" not in str(text) or not normalized:
            return False
        has_residency_term = any(
            term in normalized for term in ("resident", "local")
        )
        has_question_lead = any(
            phrase in normalized
            for phrase in ("are you", "is your party", "may i know", "whether you are")
        )
        return has_residency_term and has_question_lead

    def _latest_assistant_asked_residency(self) -> bool:
        """Return whether the immediately preceding assistant turn asked it."""
        if self.history:
            for message in reversed(self.history):
                if not isinstance(message, dict):
                    continue
                if message.get("role") != "assistant":
                    continue
                return self._is_explicit_residency_question(
                    self._assistant_content_text(message.get("content"))
                )
            return False
        for message in reversed(self.full_transcript):
            if not isinstance(message, dict):
                continue
            if message.get("role") != "assistant":
                continue
            return self._is_explicit_residency_question(
                self._assistant_content_text(message.get("text"))
            )
        return False

    def _capture_selected_room(self, utterance: str) -> None:
        """Persist a confirmed canonical room from the guest's selection turn."""
        room = recognize_selected_room(utterance)
        if room:
            self._booking_slots["room_type"] = room

    def _staged_rate_slots(self, runner: _SmartPBXRunnerContext) -> dict[str, str]:
        """Return this runner's prospective slots without mutating the session."""
        slots = dict(self._booking_slots)
        if runner.pending_room:
            slots["room_type"] = runner.pending_room
        if runner.pending_residency:
            slots["residency"] = runner.pending_residency
        return slots

    @staticmethod
    def _rate_resolution_for_slots(
        slots: dict[str, str], *, room: str | None = None,
    ) -> RateResolution:
        return resolve_rate(
            room=(
                room
                if room is not None
                else slots.get("room_type", slots.get("room_name", ""))
            ),
            residency=slots.get("residency", ""),
            check_in=slots.get("check_in", ""),
            check_out=slots.get("check_out", ""),
        )

    def _rate_context_for_turn(
        self,
        text: str,
        runner: _SmartPBXRunnerContext,
    ) -> str:
        """Return a classifier-bound rate/no-quote record for this runner only."""
        slots = self._staged_rate_slots(runner)
        intent = classify_room_rate_intent(text)
        if intent.kind == "AMBIGUOUS_RATE":
            runner.rate_turn = True
            return RateResolution(
                None, None, None, None, "ambiguous_room",
            ).authoritative_context()
        if intent.kind == "RATE":
            runner.rate_turn = True
            target_room = (
                intent.rooms[0]
                if intent.rooms
                else "" if intent.unresolved else None
            )
            return self._rate_resolution_for_slots(
                slots, room=target_room,
            ).authoritative_context()
        resolution = self._rate_resolution_for_slots(slots)
        has_complete_rate_state = resolution.reason not in {
            "unknown_room", "unknown_residency", "invalid_dates",
        }
        if (
            is_room_rate_follow_up(text)
            and runner.rate_followup_eligible
            and has_complete_rate_state
        ):
            runner.rate_turn = True
            return resolution.authoritative_context()
        if runner.pending_residency and has_complete_rate_state:
            runner.rate_turn = True
            return resolution.authoritative_context()
        runner.clear_rate_followup = True
        return ""

    def _stage_turn_rate_state(
        self, text: str, runner: _SmartPBXRunnerContext,
    ) -> None:
        """Capture turn input locally; shared slots are committed only after fencing."""
        intent = classify_room_rate_intent(text)
        selected_room = recognize_selected_room(text)
        if intent.kind == "RATE" and intent.rooms:
            selected_room = intent.rooms[0]
        runner.pending_room = selected_room
        runner.pending_residency = recognize_residency(
            text, allow_terse=runner.residency_question_asked,
        )
        runner.rate_context = self._rate_context_for_turn(text, runner)

    def _commit_staged_turn_rate_state(
        self, runner: _SmartPBXRunnerContext,
    ) -> None:
        """Commit this current runner's local classification after ownership fencing."""
        if runner.pending_room:
            self._booking_slots["room_type"] = runner.pending_room
        if runner.pending_residency:
            self._booking_slots["residency"] = runner.pending_residency
        if runner.clear_rate_followup:
            self._rate_followup_eligible = False

    def _current_rate_context(self) -> str:
        """Return this runner's rate record without sharing it across turns."""
        runner = _smartpbx_runner_context.get()
        return "" if runner is None else runner.rate_context

    def _compose_turn_user_message(self, text: str, kb_context: str) -> str:
        """Keep semantic KB prose out of a deterministic-rate model request."""
        if self._current_rate_context():
            return text
        if kb_context and "No knowledge base loaded" not in kb_context:
            return f"[Reference context: {kb_context}]\n\nGuest: {text}"
        return text

    def _booking_slots_note(self) -> str:
        """Render the captured slots as a context block, or '' when empty."""
        slots = self._booking_slots
        rate_context = self._current_rate_context()
        if not slots and not rate_context:
            return ""
        lines: list[str] = []
        if slots.get("guest_name"):
            who = slots["guest_name"]
            if slots.get("salutation"):
                who = f"{slots['salutation']} {who}"
            lines.append(f"- guest name: {who}")
        if slots.get("residency"):
            lines.append(f"- residency: {slots['residency']}")
        if slots.get("check_in"):
            lines.append(f"- check-in: {slots['check_in']}")
        if slots.get("check_out"):
            lines.append(f"- check-out: {slots['check_out']}")
        if slots.get("num_adults") or slots.get("num_children"):
            adults = slots.get("num_adults", "1")
            children = slots.get("num_children", "0")
            guests = f"- guests: {adults} adult(s)"
            if children and children != "0":
                guests += f", {children} child(ren)"
            lines.append(guests)
        if slots.get("room_type"):
            room = slots["room_type"]
            if slots.get("room_name") and slots["room_name"] != room:
                room = f"{room} ({slots['room_name']})"
            lines.append(f"- room: {room}")
        if slots.get("guest_phone"):
            lines.append(f"- phone: {slots['guest_phone']}")
        joined = "\n".join(lines)
        return _BOOKING_SLOTS_NOTE_PREFIX + joined + (
            f"\n\n{rate_context}" if rate_context else ""
        )

    def _smartpbx_rhythm_rule(self) -> str:
        return (
            "\n\nSMARTPBX CALLER RHYTHM:\n"
            "- Answer first in one or two concise sentences.\n"
            "- Ask no more than one necessary next question.\n"
            if self._is_direct_smartpbx_english()
            else ""
        )

    def _active_system_prompt(self) -> str:
        """The system prompt plus direct-SmartPBX rhythm and booking slots."""
        return self.system_prompt + self._smartpbx_rhythm_rule() + self._booking_slots_note()

    def _log_tool_result(self, tool_name: str, result: str) -> None:
        self._mark_smartpbx_turn("tool_complete")
        if self._is_smartpbx_session():
            logger.info("smartpbx_media event=tool_result tool=%s", tool_name)
        else:
            logger.info("Tool '%s' â†’ %s", tool_name, result[:200])

    def _log_tool_failure(
        self, tool_name: str, error: BaseException | None = None,
    ) -> None:
        turn_id = self._current_smartpbx_turn_id()
        if turn_id is not None:
            self._tool_failed_smartpbx_turn_ids.add(turn_id)
        if self._is_smartpbx_session():
            logger.error("smartpbx_media event=tool_error tool=%s", tool_name)
        elif error is not None:
            logger.error("Tool '%s' failed", tool_name, exc_info=error)
        else:
            logger.exception("Tool '%s' failed", tool_name)

    def _log_tts_failure(self, provider: str, outcome: str, status: int | None = None) -> None:
        turn_id = self._current_smartpbx_turn_id()
        if turn_id is not None:
            self._tts_failed_smartpbx_turn_ids.add(turn_id)
        if self._is_smartpbx_session():
            if status is None:
                logger.error("smartpbx_media event=tts_failure provider=%s outcome=%s", provider, outcome)
            else:
                logger.error("smartpbx_media event=tts_failure provider=%s outcome=http_status status=%d", provider, status)

    # â”€â”€ Main event loop â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def _emit_smartpbx_tts_diagnostic(self, failure_class: DiagnosticFailureClass) -> None:
        if self.lang not in {"en", "si"} or not self._is_smartpbx_session():
            return
        diagnostic_sink = getattr(self, "_smartpbx_diagnostic_sink", None)
        if not callable(diagnostic_sink):
            return
        try:
            diagnostic_sink(DiagnosticStage.TTS, DiagnosticOutcome.FAILED, failure_class)
        except Exception:
            return

    async def run(self):
        self._event_loop = asyncio.get_running_loop()
        media_context_token: int | None = None
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
                    if (
                        not self._is_smartpbx_session()
                        and media_context_token is None
                    ):
                        # Media-stream sessions have no SmartPBX context branch, so
                        # each session gets a dedicated per-call context dict for
                        # handover/caller metadata. Tool handlers mutate this dict
                        # in-place across the call lifecycle.
                        media_context_token = handover_context.set({
                            "caller_phone": self.caller_phone,
                        })
                    if not self._is_smartpbx_session() and media_context_token is not None:
                        context = handover_context.get() or {}
                        if isinstance(context, dict):
                            context["caller_phone"] = self.caller_phone
                    self.call_start_time = datetime.now().isoformat()
                    _dashboard_call_started(self.call_sid, self.caller_phone, self.lang, self.call_start_time)
                    logger.info(
                        "Media stream started — Call: %s, Stream: %s, lang: %s, phone: %s",
                        self.call_sid, self.stream_sid, self.lang, self.caller_phone,
                    )
                    asyncio.ensure_future(
                        self._invoke_speak(MEDIA_STREAM_WELCOME[self.lang])
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
                        logger.info("TTS done — listening for guest speech [%s]", self.call_sid)
                        # Agent just finished speaking — arm the no-speech nudge.
                        self._schedule_reprompt()

                elif event == "stop":
                    logger.info("Media stream stopped — Call: %s", self.call_sid)
                    break

        except WebSocketDisconnect:
            logger.info("Media stream disconnected — Call: %s", self.call_sid)
        except Exception:
            logger.exception("Media stream error — Call: %s", self.call_sid)
        finally:
            self._close_teardown_dispatch()
            if media_context_token is not None:
                handover_context.reset(media_context_token)
            self._cancel_reprompt()
            if self._smartpbx_initial_filler is not None:
                await self._smartpbx_initial_filler.on_session_finish()
            if self._stt:
                # Both supported STT implementations can block while stopping.
                await asyncio.to_thread(self._stt.stop)
            self._write_audio_dump()
            await self._close_stt_callbacks()
            if self._endpointing_handle:
                self._endpointing_handle.cancel()
            # Twilio Media Streams teardown ownership: RETAINED. Anything still
            # buffered — mid-dictation or ordinary speech admitted during a turn
            # — only survives if it reaches the transcript before the post-call
            # task below snapshots it.
            await self._retain_pending_speech("session_end")
            call_end_time = datetime.now().isoformat()
            logger.info(
                "Media stream session ended — Call: %s, history: %d, transcript: %d msgs",
                self.call_sid, len(self.history), len(self.full_transcript),
            )
            if self.full_transcript:
                post_call = process_post_call_data(
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
                if inspect.isawaitable(post_call):
                    asyncio.create_task(post_call)

    # â”€â”€ STT callback (called from background thread) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def _on_stt_result(self, transcript: str):
        """Called from STT thread on FINAL results."""
        if self.transfer_pending:
            return
        if self._is_smartpbx_session():
            logger.info("smartpbx_media event=stt_final")
        else:
            logger.info("STT final result [%s]: %r (speaking=%s)", self.call_sid, transcript, self._is_speaking)
        # `_latest_interim` is transcript-owned state and is therefore cleared by
        # `_accumulate_transcript` on the event loop, NOT here: this method runs
        # on the synchronous STT worker thread, and a cross-thread write races
        # every loop-side reader of the transcript buffers.
        if self._event_loop is None:
            return
        if self._is_speaking:
            if self._is_echo(transcript):
                return
            if self._should_barge_in(transcript):
                self._submit_stt_callback(self._handle_bargein)
            return
        self._submit_stt_callback(self._accumulate_transcript, transcript)

    def _on_stt_interim(self, transcript: str):
        """Called from STT thread on INTERIM results.

        Google often never fires a final result for conversational speech.
        We drive our own endpointing: each interim resets a 1.5 s silence
        timer; when the timer fires we use the latest interim as the utterance.
        """
        if self.transfer_pending:
            return
        # See `_on_stt_result`: the interim is handed to the loop and recorded
        # there by `_set_transcript_interim`. Nothing transcript-, interim- or
        # timer-owned is mutated on this thread.
        if self._event_loop is None:
            return
        if self._is_speaking:
            if self._is_echo(transcript):
                return
            if self._should_barge_in(transcript):
                self._submit_stt_callback(self._handle_bargein)
            return
        self._submit_stt_callback(self._set_transcript_interim, transcript)

    def _submit_stt_callback(self, callback: Callable, *args: Any) -> bool:
        """Admit one provider callback or refuse it after the closing fence."""
        loop = self._event_loop
        if loop is None:
            return False
        with self._stt_callback_lock:
            if self._stt_closing:
                return False
            try:
                future = asyncio.run_coroutine_threadsafe(callback(*args), loop)
            except (RuntimeError, TypeError):
                return False
            self._stt_callback_futures.add(future)
        future.add_done_callback(self._discard_stt_callback_future)
        return True

    def _discard_stt_callback_future(self, future: Any) -> None:
        with self._stt_callback_lock:
            if not future.cancelled():
                try:
                    if future.exception() is not None:
                        self._stt_callback_errors = min(
                            self._stt_callback_errors + 1, 100_000
                        )
                except Exception:
                    self._stt_callback_errors = min(
                        self._stt_callback_errors + 1, 100_000
                    )
            self._stt_callback_futures.discard(future)

    async def _close_stt_callbacks(self) -> None:
        """Close callback admission and bounded-drain every accepted callback."""
        with self._stt_callback_lock:
            self._stt_closing = True
            pending = tuple(self._stt_callback_futures)
        started = time.monotonic()
        wrapped = tuple(asyncio.wrap_future(future) for future in pending)
        timed_out = False
        if wrapped:
            _done, still_pending = await asyncio.wait(
                wrapped, timeout=STT_CALLBACK_DRAIN_TIMEOUT_SECONDS
            )
            timed_out = bool(still_pending)
            for future in still_pending:
                future.cancel()
            if still_pending:
                await asyncio.gather(*still_pending, return_exceptions=True)
        with self._stt_callback_lock:
            errors = self._stt_callback_errors
        outcome = "timeout" if timed_out else "error" if errors else "drained"
        elapsed_ms = min(max(int((time.monotonic() - started) * 1000), 0), 10_000)
        logger.info(
            "stt_callback_drain event=stt_callback_drain outcome=%s pending=%d elapsed_ms=%d",
            outcome, min(len(pending), 100_000), elapsed_ms,
        )

    def _close_teardown_dispatch(self) -> None:
        """Synchronously prevent all endpoint/turn dispatch before teardown awaits."""
        self._teardown_dispatch_closed = True
        self._smartpbx_deferred_tts_closed = True
        if self._endpointing_handle is not None:
            self._endpointing_handle.cancel()
            self._endpointing_handle = None
        self._deferred_flush_pending = False

    def _should_barge_in(self, transcript: str) -> bool:
        """True only for a substantive interruption, filtering blips and echo."""
        text = (transcript or "").strip()
        if len(text) < BARGEIN_MIN_CHARS:
            return False
        if BARGEIN_DEBOUNCE_SECONDS > 0 and self._speaking_since:
            if (time.monotonic() - self._speaking_since) < BARGEIN_DEBOUNCE_SECONDS:
                return False
        return True

    async def _handle_bargein(self):
        if self._is_smartpbx_session():
            logger.info("smartpbx_media event=barge_in")
        else:
            logger.info("Barge-in detected [%s]", self.call_sid)
        self._is_speaking = False
        self._smartpbx_transfer_audio_fenced = False
        await self._cancel_smartpbx_deferred_tts()
        await self._cancel_smartpbx_tool_fillers()
        if self._smartpbx_initial_filler is not None:
            await self._smartpbx_initial_filler.on_barge_in()
        if self._is_direct_smartpbx_english():
            self._smartpbx_barge_ins += 1
            if self._active_smartpbx_turn_id is not None:
                self._interrupted_smartpbx_turn_ids.add(self._active_smartpbx_turn_id)
                telemetry = self._ensure_smartpbx_turn_telemetry()
                if telemetry is not None:
                    telemetry.mark_once(
                        self._active_smartpbx_turn_id, "barge_clear"
                    )
        self._assistant_turn_speech_end_at = time.monotonic()
        self._cancel_reprompt()
        self._speak_generation += 1
        # Barge-in ownership: SUPERSEDED for dispatch, RETAINED for the record.
        # The guest is speaking NEW content right now, and that utterance — not
        # an older buffer — is what the next turn must answer; prepending stale
        # text would answer a question the guest has already moved past. The
        # supersession is therefore deliberate. What is NOT deliberate is losing
        # the words: anything pending here was admitted while `_is_speaking` was
        # False (results arriving during speech take the echo/barge-in branch and
        # never reach the accumulator), so it is genuine guest speech that no
        # turn ever answered. It is written to the transcript before the buffer
        # is cleared, which also clears `_deferred_flush_pending` — the flush it
        # would have re-armed no longer has anything to flush.
        await self._retain_pending_speech("barge_in")
        # Capture mode deliberately SURVIVES a barge-in. A caller talking over the
        # tail of "...could I take your number?" is a dictation starting, and
        # dropping back to the short timers there re-creates the fragment-per-turn
        # problem this exists to fix. The buffer is still cleared — the new turn
        # starts from the caller's fresh speech, not from a half-heard utterance.
        # The guest interrupted: a new turn begins, so release the guard now even
        # though the interrupted turn's _process_utterance has not unwound yet.
        # Bumping the turn id stops that stale turn's finally from clobbering it.
        self._utterance_dispatched = False
        self._utterance_turn += 1
        if self._endpointing_handle:
            self._endpointing_handle.cancel()
            self._endpointing_handle = None
        await self._clear_media_audio()

    async def _send_media_audio(self, audio: bytes) -> None:
        """Send raw mulaw through the active provider-specific media transport."""
        if self.transfer_pending:
            return
        runner = _smartpbx_runner_context.get()
        if runner is not None and runner.speak_generation != self._speak_generation:
            return
        if self._media_transport is not None:
            turn_id = self._current_smartpbx_turn_id()
            if turn_id is not None:
                bind_turn = getattr(self._media_transport, "bind_turn", None)
                if callable(bind_turn):
                    bind_turn(self._speak_generation, turn_id)
            await self._media_transport.send_audio(audio)
            return
        async with self._ws_lock:
            await self.ws.send_text(json.dumps({
                "event": "media",
                "streamSid": self.stream_sid,
                "media": {"payload": base64.b64encode(audio).decode("ascii")},
            }))

    async def _clear_media_audio(self, force: bool = False) -> None:
        """Clear pending speech without leaking one provider's wire protocol."""
        if self.transfer_pending and not force:
            return
        if self._media_transport is not None:
            cleared_generation = await self._media_transport.clear_audio()
            transport_generation = getattr(
                self._media_transport,
                "generation",
                getattr(self._media_transport, "_generation", cleared_generation),
            )
            if isinstance(transport_generation, int) and not isinstance(
                transport_generation, bool,
            ):
                self._speak_generation = max(transport_generation, 0)
            return
            async with self._ws_lock:
                await self.ws.send_text(json.dumps({
                    "event": "clear",
                    "streamSid": self.stream_sid,
                }))

    async def _invoke_tts(
        self,
        tts_method,
        text: str,
        *,
        sentence: str | None = None,
        turn_generation: int | None = None,
    ) -> None:
        """Call legacy-compatible TTS methods.

        Some tests patch ``_tts_*`` helpers with pre-refactor one-argument
        callables. Preserve those tests while keeping the richer contract in this
        branch.
        """
        try:
            sig = inspect.signature(tts_method)
            params = sig.parameters
            if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()):
                await tts_method(text, sentence=sentence, turn_generation=turn_generation)
                return
            kwargs = {}
            if "sentence" in params:
                kwargs["sentence"] = sentence
            if "turn_generation" in params:
                kwargs["turn_generation"] = turn_generation
            await tts_method(text, **kwargs)
            return
        except TypeError as exc:
            if "unexpected keyword argument" in str(exc) or "positional" in str(exc):
                await tts_method(text)
                return
            raise

    async def _invoke_speak(
        self, text: str, generation: int = -1, sentence: str | None = None
    ) -> None:
        """Call legacy-compatible _speak implementations.

        Some tests monkey-patch _speak with a legacy two-argument signature.
        Preserve those tests while keeping the richer sentence-tracking contract.
        """
        if not self._current_smartpbx_runner_owns_shared_state():
            return
        sig = inspect.signature(self._speak)
        params = sig.parameters
        has_generation = "generation" in params
        has_sentence = "sentence" in params
        if any(param.kind == inspect.Parameter.VAR_KEYWORD for param in params.values()):
            await self._speak(text, generation=generation, sentence=sentence)
            return
        if has_generation and has_sentence:
            await self._speak(text, generation=generation, sentence=sentence)
            return
        if has_generation:
            await self._speak(text, generation)
            return
        if has_sentence:
            await self._speak(text, sentence)
            return
        await self._speak(text)

    async def _send_tts_done(
        self, sentence: str | None = None, turn_generation: int | None = None
    ) -> bool:
        """Complete one utterance using local acknowledgement when available."""
        if self.transfer_pending:
            return False
        generation = self._speak_generation
        if turn_generation is None:
            turn_generation = generation
        if self._media_transport is not None:
            await self._media_transport.send_mark("tts_done")
            self._is_speaking = False
            delivered = (
                generation == self._speak_generation
                and not self.transfer_pending
                and turn_generation == generation
            )
            if delivered:
                self._record_delivered_sentence(sentence, turn_generation or generation)
                self._schedule_reprompt()
            self._assistant_turn_speech_end_at = time.monotonic()
            return delivered
        async with self._ws_lock:
            await self.ws.send_text(json.dumps({
                "event": "mark",
                "streamSid": self.stream_sid,
                "mark": {"name": "tts_done"},
            }))
            return True

    def _start_assistant_turn_delivery_tracking(self) -> None:
        self._assistant_turn_generation = self._speak_generation
        self._assistant_turn_generated_sentences = []
        self._delivered_sentences = []
        self._track_assistant_turn_delivery = True

    def _record_generated_sentence(self, sentence: str) -> None:
        if self._track_assistant_turn_delivery:
            self._assistant_turn_generated_sentences.append(sentence)

    def _record_delivered_sentence(self, sentence: str | None, turn_generation: int) -> None:
        if not self._track_assistant_turn_delivery or sentence is None:
            return
        if self._assistant_turn_generation != turn_generation:
            return
        if len(self._delivered_sentences) >= len(self._assistant_turn_generated_sentences):
            return
        start_index = len(self._delivered_sentences)
        for idx in range(start_index, len(self._assistant_turn_generated_sentences)):
            if self._assistant_turn_generated_sentences[idx] == sentence:
                self._delivered_sentences.append(sentence)
                return

    def _assistant_turn_was_interrupted(self) -> bool:
        if not self._track_assistant_turn_delivery:
            return False
        return (
            self._assistant_turn_generation != self._speak_generation or
            len(self._assistant_turn_generated_sentences) != len(self._delivered_sentences)
        )

    def _assistant_turn_text_for_history(self, generated_text: str) -> str:
        if not self._assistant_turn_was_interrupted():
            return generated_text
        delivered_text = " ".join(self._delivered_sentences)
        return f"{delivered_text} [interrupted]" if delivered_text else "[interrupted]"

    def _append_assistant_history(self, assistant_content: Any) -> None:
        if not self._current_smartpbx_runner_owns_shared_state():
            return
        if self._smartpbx_transfer_context is not None:
            if isinstance(assistant_content, dict):
                assistant_msg = dict(assistant_content)
                if assistant_msg.get("role") != "assistant":
                    self.history.append(assistant_content)
                    return

                content = assistant_msg.get("content")
                if self._is_smartpbx_session() and isinstance(content, str):
                    recorded = self._assistant_turn_text_for_history(content)
                    assistant_msg["content"] = recorded
                self.history.append(assistant_msg)
            else:
                self.history.append(assistant_content)
            return

        self.history.append(assistant_content)

    def _append_assistant_turn_to_transcript(self, generated_text: str) -> None:
        if not self._current_smartpbx_runner_owns_shared_state():
            return
        if not self._is_smartpbx_session():
            return
        text = self._assistant_turn_text_for_history(generated_text)
        self.full_transcript.append({"role": "assistant", "text": text})
        if self._smartpbx_transfer_context is not None:
            logger.info(
                "smartpbx_media event=assistant_turn_delivery generated=%d delivered=%d interrupted=%s",
                len(self._assistant_turn_generated_sentences),
                len(self._delivered_sentences),
                self._assistant_turn_was_interrupted(),
            )

    @staticmethod
    def _capture_complete_tools() -> tuple[str, ...]:
        return (
            "capture_spoken_number",
            "capture_spoken_name",
            "collect_number_via_keypad",
        )

    @staticmethod
    def _capture_followup_required(result: dict[str, Any]) -> bool:
        status = str(result.get("status", "")).lower()
        return status in {"needs_more", "invalid", "unavailable"}

    def _enter_capture_mode(
        self, turns: int = CAPTURE_MODE_MAX_TURNS, *, reason: str = "tool"
    ) -> None:
        if self._capture_mode_active:
            # An episode already in flight keeps its REMAINING allowance. A
            # needs_more re-ask must never hand an exhausted episode a fresh
            # budget, or a caller whose number never parses is asked forever.
            return
        self._capture_mode_active = True
        self._capture_mode_turns_left = turns
        self._capture_bound_logged = False
        if self._is_smartpbx_session():
            logger.info(
                "smartpbx_media event=capture_mode_enter reason=%s turns=%d",
                reason, turns,
            )

    def _exit_capture_mode(self, reason: str = "reset") -> None:
        was_active = self._capture_mode_active
        self._capture_mode_active = False
        self._capture_mode_turns_left = 0
        if was_active and self._is_smartpbx_session():
            logger.info("smartpbx_media event=capture_mode_exit reason=%s", reason)

    def _consume_capture_mode_turn(self) -> None:
        if not self._capture_mode_active:
            return
        if self._capture_mode_turns_left <= 0:
            self._exit_capture_mode("max_turns")
            return
        self._capture_mode_turns_left -= 1
        if self._capture_mode_turns_left <= 0:
            self._exit_capture_mode("max_turns")

    def _is_capture_mode_active(self) -> bool:
        return self._capture_mode_active

    def _capture_turn_timeout(self, *, final: bool) -> float:
        if self._is_capture_mode_active():
            if final:
                # In capture mode a final no longer dispatches — it refreshes the
                # combining window — so it must never wait LESS than the silence
                # timer. Take the more patient of the two knobs so neither can
                # undercut the other when an operator tunes only one.
                return max(
                    CAPTURE_FINAL_GRACE_SECONDS, CAPTURE_ENDPOINTING_SILENCE_SECONDS
                )
            return CAPTURE_ENDPOINTING_SILENCE_SECONDS
        return STT_FINAL_GRACE_SECONDS if final else ENDPOINTING_SILENCE

    def _direct_smartpbx_captured_number_confirmation(
        self,
        tool_use_blocks: list[dict[str, Any]],
        staged_results: list[tuple[int, dict[str, Any], Any, str, BaseException | None]],
    ) -> str | None:
        """Return the one safe deterministic readback for a completed tool batch.

        Only the direct SmartPBX English Claude path calls this after it has
        committed the normal Anthropic tool-use/result pair.  The parser owns
        number correctness; this guard merely refuses a malformed result rather
        than turning arbitrary tool output into caller-facing speech.
        """
        if (
            not self._is_direct_smartpbx_english()
            or len(tool_use_blocks) != 1
            or len(staged_results) != 1
            or tool_use_blocks[0].get("name") != "capture_spoken_number"
        ):
            return None
        _tool_index, tool, _tool_input, result_str, tool_error = staged_results[0]
        if tool_error is not None or tool.get("name") != "capture_spoken_number":
            return None
        try:
            result = json.loads(result_str)
        except (json.JSONDecodeError, TypeError):
            return None
        if (
            not isinstance(result, dict)
            or str(result.get("status", "")).lower() != "captured"
            or result.get("valid") is False
        ):
            return None
        readback = result.get("readback")
        if not isinstance(readback, str):
            return None
        digits = readback.replace(" ", "")
        if (
            not digits
            or len(digits) > DTMF_MAX_DIGITS
            or not digits.isascii()
            or any(char not in "0123456789" for char in digits)
            or readback != " ".join(digits)
        ):
            return None
        return f"I've got that as {readback} — is that correct?"

    def _record_capture_tool_completion(self, tool_name: str, result: dict[str, Any]) -> None:
        if tool_name not in self._capture_complete_tools():
            return
        if self._capture_followup_required(result):
            self._enter_capture_mode(reason="tool_needs_more")
            return
        captured = str(result.get("status", "")).lower() == "captured"
        if captured:
            if tool_name == "capture_spoken_number" and result.get("valid") is not False:
                normalized = str(result.get("normalized", "")).strip()
                if normalized:
                    self._booking_slots["guest_phone"] = normalized
            elif tool_name == "capture_spoken_name":
                name = str(result.get("name", "")).strip()
                if name:
                    self._booking_slots["guest_name"] = name
            # The read-back of a successful capture reads exactly like an ask, so
            # stand the ask detector down for the remainder of this turn.
            self._capture_success_this_turn = True
        self._exit_capture_mode("captured" if captured else "tool_complete")

    def _maybe_enter_capture_mode_from_ask(self) -> None:
        """Arm capture mode from a delivered ask, before the caller answers.

        Only sentences the caller actually heard count — `_delivered_sentences` is
        the post-TTS record, so an ask cut off by a barge-in cannot pre-arm
        anything. This is what makes the FIRST fragment of a dictated number
        patient: capture mode used to engage only after the first capture tool
        call, by which point two or three fragments had each cost a full turn.
        """
        if self._capture_success_this_turn:
            return
        if self._is_capture_mode_active():
            return
        if not _detect_capture_ask(self._delivered_sentences):
            return
        self._enter_capture_mode(reason="ask")

    def _bound_capture_text(self, text: str) -> str:
        """Cap the combined dictation so a stuck episode cannot run away."""
        if len(text) <= CAPTURE_BUFFER_MAX_CHARS:
            return text
        if self._capture_bound_logged:
            # Once at the cap EVERY further final overflows; one line per episode
            # is the signal, the rest is noise.
            return text[:CAPTURE_BUFFER_MAX_CHARS].rstrip()
        self._capture_bound_logged = True
        if self._is_smartpbx_session():
            logger.info(
                "smartpbx_media event=capture_buffer_bounded limit=%d",
                CAPTURE_BUFFER_MAX_CHARS,
            )
        else:
            logger.info(
                "Capture buffer bounded at %d chars [%s]",
                CAPTURE_BUFFER_MAX_CHARS, self.call_sid,
            )
        # Keep the HEAD: the digits said first are the start of the number.
        return text[:CAPTURE_BUFFER_MAX_CHARS].rstrip()

    def _capture_dispatch_pending(self) -> bool:
        """True while a capture episode still owns buffered text or a live timer."""
        if not self._is_capture_mode_active():
            return False
        if self._pending_transcript.strip() or self._committed_transcript.strip():
            return True
        return self._endpointing_handle is not None

    async def _retain_pending_speech(self, reason: str) -> None:
        """RETAINED ownership: buffered caller speech reaches the transcript.

        Every lifecycle boundary that clears the pending buffers must first
        decide who owns what is in them. There are exactly three answers —
        DISPATCHED (it becomes a turn), RETAINED (it reaches `full_transcript`,
        and therefore the call log and post-call extraction), TRANSFERRED (it
        travels with the transfer context) — and a silent clear is not one of
        them. This is the RETAINED implementation, and it is the sole one: no
        boundary drops the buffer without calling it.

        It records the buffered utterance rather than starting an LLM turn. On
        the teardown call sites the audio path is already gone, so there is
        nobody to speak to and a turn would only delay teardown; on the barge-in
        call site the guest is already speaking the utterance that supersedes it.

        Originally `_force_pending_capture_dispatch`, and gated on capture mode:
        a half-dictated number was the only thing considered worth preserving.
        Since the post-dispatch predicate was narrowed (see
        `_reject_post_dispatch_result`) ordinary speech is routinely admitted and
        left pending too, and it is no less the guest's words, so the gate is
        gone. The `capture_forced_dispatch` event name is kept: it is the
        allowlisted event for exactly this action, and `reason` distinguishes the
        boundary that fired it.
        """
        buffered = (self._pending_transcript or self._committed_transcript).strip()
        provenance = (
            "final"
            if self._committed_transcript and not self._latest_interim
            else "interim"
        )
        if provenance not in _RETAINED_SPEECH_PROVENANCE:
            provenance = "interim"
        retention_reason = reason if reason in _RETAINED_SPEECH_REASONS else "other"
        buffered = buffered[:RETAINED_SPEECH_MAX_CHARS].rstrip()
        if self._endpointing_handle is not None:
            self._endpointing_handle.cancel()
            self._endpointing_handle = None
        self._pending_transcript = ""
        self._committed_transcript = ""
        self._latest_interim = ""
        self._deferred_flush_pending = False
        if not buffered:
            return
        self.full_transcript.append(
            {
                "role": RETAINED_SPEECH_ROLE,
                "text": buffered,
                "provenance": provenance,
                "answered": "unanswered",
                "retention_reason": retention_reason,
            }
        )
        if self._is_smartpbx_session():
            logger.info(
                "smartpbx_media event=capture_forced_dispatch reason=%s "
                "provenance=%s answered=unanswered chars=%d",
                retention_reason, provenance, len(buffered),
            )
        else:
            logger.info(
                "Retained pending speech (%s) [%s]", reason, self.call_sid
            )

    # â”€â”€ Debug: live-call audio capture â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def _write_audio_dump(self) -> None:
        """Write captured mulaw call audio to an 8 kHz PCM16 wav (STT bake-off input)."""
        if not self._audio_dump:
            return
        if audioop is None:
            logger.warning("Cannot write audio dump — audioop unavailable")
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
            if self._is_smartpbx_session():
                logger.info("smartpbx_media event=audio_dump_written")
            else:
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
        if (
            self.transfer_pending
            or self._smartpbx_torn_down
            or self._teardown_dispatch_closed
        ):
            return
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
            if (
                self.transfer_pending
                or self._smartpbx_torn_down
                or self._teardown_dispatch_closed
            ):
                return
            if self._reprompt_count >= MAX_REPROMPTS:
                return
            if self._is_speaking:
                # Agent is talking — re-arm after it finishes.
                self._schedule_reprompt()
                return
            if self._utterance_dispatched:
                # A dispatched turn owns the floor. Every delivered sentence
                # arms this timer, so the deadline routinely expires inside a
                # tool/model gap where _is_speaking is already False but the
                # turn is still running. Nudging there talks over the turn.
                # Defer exactly like the speaking case; the re-arm below (or
                # the turn's own next delivered sentence, which cancel-replaces
                # it) carries the nudge past the release of the guard.
                self._schedule_reprompt()
                return
            if self._capture_dispatch_pending():
                # A nudge mid-number is exactly the UX this buffering exists to
                # kill: the caller is pausing between digit groups, not silent.
                # Buffered speech or a live capture timer both mean "still
                # dictating" — wait, do not talk over them.
                self._schedule_reprompt()
                return
            messages = REPROMPT_MESSAGES.get(self.lang, REPROMPT_MESSAGES["en"])
            text = messages[min(self._reprompt_count, len(messages) - 1)]
            self._reprompt_count += 1
            if self._is_smartpbx_session():
                logger.info(
                    "smartpbx_media event=silence_reprompt attempt=%d",
                    self._reprompt_count,
                )
            else:
                logger.info(
                    "No-speech re-prompt [%s] attempt %d (lang=%s)",
                    self.call_sid, self._reprompt_count, self.lang,
                )
            self.full_transcript.append({"role": "assistant", "text": text})
            await self._invoke_speak(text)
        except asyncio.CancelledError:
            pass

    # â”€â”€ Endpointing â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def _post_dispatch_elapsed_ms(self) -> int:
        """Age of the owning turn, clamped — the only number this event carries."""
        started = self._utterance_dispatched_at
        elapsed_ms = 0
        if started is not None:
            elapsed_ms = int((time.monotonic() - started) * 1000)
        return max(0, min(elapsed_ms, POST_DISPATCH_ELAPSED_MS_MAX))

    def _emit_post_dispatch_result(
        self, result_type: str, elapsed_ms: int | None = None
    ) -> None:
        """Record that a late provider result was ignored by a dispatched turn.

        Privacy-safe and bounded by construction: two closed enums plus one
        clamped integer. No transcript text, no provider payload, no header, no
        phone/call identifier — see the runbook's event allowlist.

        `elapsed_ms` is optional so the caller can reuse the value it already
        computed for the per-turn aggregate; omitting it computes the same
        clamped number here.
        """
        if result_type not in ("final", "interim"):
            # Closed enum. An unrecognised caller emits nothing rather than
            # widening the log vocabulary silently.
            return
        if not self._is_smartpbx_session():
            return
        if elapsed_ms is None:
            elapsed_ms = self._post_dispatch_elapsed_ms()
        elapsed_ms = max(0, min(int(elapsed_ms), POST_DISPATCH_ELAPSED_MS_MAX))
        logger.info(
            "smartpbx_media event=stt_post_dispatch_result result_type=%s "
            "action=ignored_active_turn elapsed_ms=%d",
            result_type,
            elapsed_ms,
        )

    def _reject_post_dispatch_result(self, result_type: str, text: str) -> bool:
        """True when a dispatched turn owns the endpoint and this result is empty.

        Called FIRST in both accumulation paths — before any counter, buffer or
        timer is touched — so a refused result cannot contaminate the next turn,
        resurrect itself as a spurious later turn, or vanish at hangup.

        The predicate is deliberately narrow: a result is refused only when it
        carries NO material characters (nothing alphanumeric — empty, whitespace
        or punctuation only). That is the one thing provable here without
        provider identity, and it is provable by construction: a result with no
        material characters contains no caller speech, so refusing it cannot lose
        any.

        Everything material is ADMITTED, including a result whose text is exactly
        the dispatched utterance, a prefix of it, or a punctuation variation of
        it. Such a result may be the provider's own tail — but it may equally be
        the caller repeating or correcting themselves, which is the most common
        thing a caller does when the agent falls silent mid-turn, and NOTHING
        available here separates the two. The STT callbacks
        (`GoogleSTTStream._on_final` / `_on_interim`, `AzureSTTStream._on_recognized`
        / `_on_recognizing`) deliver a bare `str`: no result id, no segment id, no
        audio-time span. `_stream_epoch` is an internal gRPC-swap fence, identical
        for a tail and for a repetition. Text plus elapsed time is not proof of
        ownership, so neither is used, and the staleness window was removed rather
        than left as an unused knob.

        Admitted results take the normal path: they accumulate into the pending
        buffers, cancel and reset the silence re-prompt, and `_flush_transcript`
        defers them into the NEXT turn (the turn's release re-arms that flush).
        They never join the running turn's history or telemetry. From there every
        lifecycle boundary gives that pending speech an explicit owner — see
        `_retain_pending_speech`.

        The policy is SHARED with the Twilio Media Streams path on purpose. An
        empty provider result is a provider-level shape, not a transport-level
        one, and Twilio Media Streams drives this exact accumulator;
        ConversationRelay has its own handler and never reaches it. Twilio
        sessions simply emit no SmartPBX telemetry, which the
        `_is_smartpbx_session` gate in the emitter already handles.

        Genuine speaking-time barge-in is unaffected: `_on_stt_result` /
        `_on_stt_interim` handle that branch and return before anything reaches
        this gate, so a real interruption still stops the speech and starts a
        fresh turn.

        The silence re-prompt is deliberately NOT touched on the rejection path.
        Its task is already re-armed by every delivered sentence of the turn that
        owns the guard, and mutating a timer from a refused result is exactly the
        state-change-before-rejection this gate exists to remove. The nudge
        cannot voice mid-turn regardless: `_reprompt_after_silence` defers while
        this same guard is held.

        Volume is bounded per turn: the INFO line is emitted only for the FIRST
        result of each type an owning turn refuses (at most two lines per turn),
        and the per-turn totals travel on the turn_summary instead. Admitted
        speech is counted nowhere here — it is not an ignored result.
        """
        if not self._utterance_dispatched:
            return False
        if _has_material_text(text or ""):
            # There are words (or digits) in here. Whoever produced them, they
            # cannot be shown to be a duplicate of what was already answered, so
            # they are admitted rather than deleted.
            return False
        # `elapsed_ms` is computed only to describe the refusal in telemetry. It
        # is NOT part of the predicate: the age of the owning turn says nothing
        # about who produced the result.
        elapsed_ms = self._post_dispatch_elapsed_ms()
        turn_id = self._active_smartpbx_turn_id
        # Same closed enum as the emitter: an unrecognised type is neither
        # counted nor logged, so the two can never disagree.
        key = {"final": "finals", "interim": "interims"}.get(result_type)
        first_of_type = True
        if turn_id is not None and key is not None:
            record = self._smartpbx_post_dispatch_by_turn.get(turn_id)
            if record is None:
                record = {"finals": 0, "interims": 0, "max_elapsed_ms": 0}
                self._smartpbx_post_dispatch_by_turn[turn_id] = record
            record[key] = min(record[key] + 1, 100_000)
            record["max_elapsed_ms"] = max(record["max_elapsed_ms"], elapsed_ms)
            first_of_type = record[key] == 1
        if first_of_type:
            self._emit_post_dispatch_result(result_type, elapsed_ms)
        return True

    def _post_dispatch_counts_for_turn(self, turn_id: str | None) -> dict[str, int]:
        """Summary fields for one turn, or {} when it refused nothing.

        Absent rather than zero, matching the kb_ms / tool_ms convention.
        """
        record = self._smartpbx_post_dispatch_by_turn.get(turn_id or "")
        if not record:
            return {}
        return {
            "ignored_post_dispatch_finals": record["finals"],
            "ignored_post_dispatch_interims": record["interims"],
            "ignored_post_dispatch_max_elapsed_ms": record["max_elapsed_ms"],
        }

    async def _accumulate_transcript(self, text: str):
        if self._smartpbx_torn_down:
            # A residual STT callback landed after teardown finalized this
            # session's turns — drop silently rather than arm a new
            # endpointing timer / open a new turn after session_summary.
            return
        if self.transfer_pending:
            return
        if self._reject_post_dispatch_result("final", text):
            return
        # A final supersedes any interim of the same utterance. Cleared here, on
        # the event loop, rather than in the STT worker-thread callback.
        self._latest_interim = ""
        # Counted here (event-loop side) rather than in the STT-thread callback
        # so the per-turn counters never race the flush that snapshots them.
        self._smartpbx_stt_final_events = min(
            self._smartpbx_stt_final_events + 1, 100_000
        )
        # Caller is speaking — cancel any pending silence nudge and reset
        # the re-prompt counter so future silences start fresh.
        self._cancel_reprompt()
        self._reprompt_count = 0
        # Provider finals commit across segments of one utterance.
        combined = (
            self._committed_transcript + " " + text
            if self._committed_transcript
            else text
        )
        capture = self._is_capture_mode_active()
        if capture:
            combined = self._bound_capture_text(combined)
        self._committed_transcript = combined
        self._pending_transcript = self._committed_transcript
        if capture and self._is_smartpbx_session():
            # No transcript text in the log line — this path carries the caller's
            # phone number.
            logger.info(
                "smartpbx_media event=capture_final_buffered chars=%d",
                len(self._committed_transcript),
            )
        # Outside capture mode the provider already segmented, so wait only a
        # short grace for a mid-thought continuation. Inside capture mode a final
        # does NOT dispatch: it refreshes the full capture-silence window so the
        # 2-4 digit fragments of one number combine into a single utterance.
        self._arm_endpointing(self._capture_turn_timeout(final=True))

    async def _set_transcript_interim(self, text: str):
        """Set pending to the latest interim (over the committed finals); reset timer."""
        if self._smartpbx_torn_down:
            # See _accumulate_transcript — same post-teardown guard.
            return
        if self.transfer_pending:
            return
        if self._reject_post_dispatch_result("interim", text):
            return
        # Loop-side record of the latest interim, mirroring the final path.
        self._latest_interim = text
        # Event-loop-side count, mirroring _accumulate_transcript.
        self._smartpbx_stt_interim_events = min(
            self._smartpbx_stt_interim_events + 1, 100_000
        )
        # Caller is speaking — cancel any pending silence nudge and reset
        # the re-prompt counter.
        self._cancel_reprompt()
        self._reprompt_count = 0
        # Preserve any committed finals from earlier segments; on the interim-only
        # path there are none, so this is a plain overwrite of the cumulative
        # interim as before.
        committed = self._committed_transcript
        exact_prefix = f"{committed} "
        has_one_exact_separator = (
            text.startswith(exact_prefix)
            and len(text) > len(exact_prefix)
            and not text[len(exact_prefix)].isspace()
        )
        if committed and has_one_exact_separator:
            shape = "exact_cumulative"
        elif committed and text.casefold().startswith(committed.casefold()):
            # A POSSIBLE cumulative shape, for counting only. Anything short of
            # a byte-exact prefix plus exactly one separator keeps the
            # conservative concatenation below — no fuzzy matching.
            shape = "unknown"
        else:
            shape = "segment"
        if self._is_smartpbx_session():
            logger.info(
                "smartpbx_media event=stt_interim_shape shape=%s "
                "committed_chars=%d interim_chars=%d",
                shape,
                len(committed),
                len(text),
            )
        if shape == "exact_cumulative":
            # Google's cumulative interims already CONTAIN the committed prefix.
            # Concatenating again emitted the prefix twice ("first segment first
            # segment second segment"), so use the provider's own text verbatim.
            pending = text
        elif committed:
            pending = committed + " " + text
        else:
            pending = text
        if self._is_capture_mode_active():
            pending = self._bound_capture_text(pending)
        self._pending_transcript = pending
        # No final has segmented this, so use the longer self-endpointing timer.
        self._arm_endpointing(self._capture_turn_timeout(final=False))

    def _arm_endpointing(self, delay: float) -> None:
        if self._stt_closing or self._teardown_dispatch_closed:
            return
        # A live timer now owns the pending buffer, so any request to re-flush it
        # at the end of the active turn is superseded by this deadline.
        self._deferred_flush_pending = False
        if self._endpointing_handle:
            self._endpointing_handle.cancel()
        self._endpointing_handle = self._event_loop.call_later(
            delay,
            lambda: asyncio.ensure_future(self._flush_transcript()),
        )

    async def _flush_transcript(self):
        if self._teardown_dispatch_closed or self._stt_closing or self._smartpbx_torn_down:
            # See _accumulate_transcript — an endpointing timer armed just
            # before teardown must not dispatch a new turn after it.
            return
        if self.transfer_pending:
            # Same ownership as `enter_transfer_pending`: RETAINED. A deadline
            # that lands after the hand-off began must not start a turn, but the
            # buffer it was going to flush is still guest speech.
            await self._retain_pending_speech("transfer_flush")
            return
        # Exactly-once: a turn is already dispatched and the agent is responding.
        # A stale timer that fires now must not start a second llm_round. Results
        # PROVEN to be that turn's own tail were already refused upstream by
        # `_reject_post_dispatch_result`, so any buffered text here is genuine
        # caller speech admitted during the turn (or a timer armed before the
        # dispatch claimed it). It is neither merged into the running turn nor
        # dropped: it stays buffered and the turn's release re-arms this flush.
        if self._utterance_dispatched:
            if self._pending_transcript.strip():
                self._deferred_flush_pending = True
            return
        transcript = self._pending_transcript.strip()
        had_committed_final = bool(self._committed_transcript)
        self._pending_transcript = ""
        self._committed_transcript = ""
        self._latest_interim = ""
        self._endpointing_handle = None
        self._deferred_flush_pending = False
        if not transcript:
            return
        # Claim the turn synchronously, before any await, so a concurrently-queued
        # flush task sees the guard set and bails.
        self._utterance_dispatched = True
        self._utterance_dispatched_at = time.monotonic()
        # A turn only spends the capture allowance if capture mode was already
        # armed when the caller's speech was dispatched — the turn that ARMED it
        # (the ask, or the first needs_more) is not itself a capture turn.
        capture_turn = self._is_capture_mode_active()
        if capture_turn and (
            _capture_dictation_ratio(transcript) < CAPTURE_DICTATION_MIN_RATIO
        ):
            # The caller moved on ("actually, can I ask about breakfast?").
            # Staying in capture mode would make every following turn wait the
            # patient timers for a conversation that is no longer a dictation.
            self._exit_capture_mode("low_dictation_ratio")
            capture_turn = False
        endpoint_source = (
            "capture" if capture_turn else "final" if had_committed_final else "interim"
        )
        telemetry = self._ensure_smartpbx_turn_telemetry()
        if telemetry is not None:
            self._active_smartpbx_turn_id = telemetry.start_turn(
                endpoint_source,
                stt_interim_events=self._smartpbx_stt_interim_events,
                stt_final_events=self._smartpbx_stt_final_events,
            )
            self._smartpbx_stt_interim_events = 0
            self._smartpbx_stt_final_events = 0
            self._smartpbx_dropped_frame_baselines[
                self._active_smartpbx_turn_id
            ] = self._current_smartpbx_dropped_frames()
            self._smartpbx_dropped_frames_by_turn[
                self._active_smartpbx_turn_id
            ] = 0
            telemetry.mark(self._active_smartpbx_turn_id, "endpoint")
        self._utterance_turn += 1
        turn = self._utterance_turn
        if self._is_smartpbx_session():
            logger.info("smartpbx_media event=guest_utterance")
            _log_smartpbx_pilot_transcript("guest", transcript)
        else:
            logger.info("Guest [%s]: %s", self.call_sid, transcript)
        self.full_transcript.append({"role": "user", "text": transcript})
        try:
            await self._process_utterance(transcript)
        finally:
            # Release only if a barge-in (or transfer) has not already started a
            # newer turn; otherwise that newer turn owns the guard.
            if self._utterance_turn == turn:
                # Spend the allowance AFTER the turn, so a needs_more re-ask
                # inside it cannot resurrect an episode that just ran out.
                if capture_turn:
                    self._consume_capture_mode_turn()
                self._utterance_dispatched = False
                if self._deferred_flush_pending:
                    # Caller speech admitted during this turn had its endpointing
                    # deadline expire while the guard was still held. The guard is
                    # free now, so re-arm it as the next turn rather than leaving
                    # it stranded in the buffer. A delivered capture ask may have
                    # armed capture mode while this turn was in flight: retain
                    # that existing patient window for its dictation fragments.
                    # Ordinary deferred speech keeps the zero-delay release.
                    capture_rearm = self._is_capture_mode_active()
                    final = bool(self._committed_transcript)
                    delay = (
                        self._capture_turn_timeout(final=final)
                        if capture_rearm
                        else 0.0
                    )
                    if (
                        capture_rearm
                        and delay > 0.0
                        and self._is_smartpbx_session()
                    ):
                        provenance = "final" if final else "interim"
                        delay_ms = max(0, min(int(delay * 1000), 5000))
                        logger.info(
                            "smartpbx_media event=capture_deferred_rearm "
                            "provenance=%s delay_ms=%d",
                            provenance,
                            delay_ms,
                        )
                    self._arm_endpointing(delay)

    # â”€â”€ Utterance â†’ KB + Claude + TTS â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    async def _process_utterance(self, text: str):
        """Run one turn with call-local SmartPBX state when this is a Dialog call."""
        if self.transfer_pending:
            return
        transfer_token = caller_token = None
        if self._smartpbx_transfer_context is not None:
            transfer_token = smartpbx_transfer_context.set(
                self._smartpbx_transfer_context
            )
            if self._smartpbx_caller_context is None:
                self._smartpbx_caller_context = {}
            # Keep a live reference to the per-session caller-context dict.
            # execute_tool paths intentionally mutate this dict in-place, and
            # callers set/reset between turns would otherwise lose state.
            caller_token = handover_context.set(self._smartpbx_caller_context)
        try:
            await self._process_utterance_bound(text)
        finally:
            if caller_token is not None:
                handover_context.reset(caller_token)
            if transfer_token is not None:
                smartpbx_transfer_context.reset(transfer_token)

    async def _process_utterance_bound(self, text: str):
        turn_id = self._active_smartpbx_turn_id
        telemetry = self._ensure_smartpbx_turn_telemetry()
        dropped_frame_baseline = self._smartpbx_dropped_frame_baselines.get(
            turn_id, self._smartpbx_last_finished_dropped_frames,
        )
        runner = _SmartPBXRunnerContext(
            turn_id=turn_id,
            dropped_frame_baseline=dropped_frame_baseline,
            speak_generation=self._speak_generation,
            raw_utterance=text,
            rate_followup_eligible=self._rate_followup_eligible,
        )
        runner_token = _smartpbx_runner_context.set(runner)
        try:
            await self._process_utterance_bound_runner(text, turn_id, telemetry)
        finally:
            _smartpbx_runner_context.reset(runner_token)

    async def _process_utterance_bound_runner(
        self,
        text: str,
        turn_id: str | None,
        telemetry: SmartPBXTurnTelemetry | None,
    ) -> None:
        if not self._current_smartpbx_runner_owns_shared_state():
            return
        runner = _smartpbx_runner_context.get()
        if runner is not None:
            runner.residency_question_asked = self._latest_assistant_asked_residency()
            self._stage_turn_rate_state(text, runner)
        self._last_guest_utterance_raw = text
        self._capture_success_this_turn = False
        self._start_assistant_turn_delivery_tracking()
        outcome = "completed"
        response_text = ""
        try:
            self._mark_smartpbx_turn_once("kb_start")
            # An exact price record is authoritative and deliberately excludes
            # semantic rate prose for this turn. General descriptive turns keep
            # the normal KB retrieval path.
            if self._current_rate_context():
                kb_context = ""
            else:
                # Embedding + Chroma query is tens of ms of CPU. On the SmartPBX path
                # this loop is shared by every concurrent call, so keep it off-loop.
                kb_context = await asyncio.to_thread(retrieve_context, text)
        except asyncio.CancelledError:
            if telemetry is not None and turn_id is not None:
                telemetry.finish(
                    turn_id,
                    "transfer_pending" if self.transfer_pending else "cancelled",
                    delivered_sentences=len(self._delivered_sentences),
                    dropped_frames=self._smartpbx_dropped_frames_for_turn(turn_id),
                    **self._smartpbx_cadence_counts(turn_id),
                    **self._post_dispatch_counts_for_turn(turn_id),
                )
                self._retire_smartpbx_turn(turn_id)
            raise
        except Exception:
            if self._is_smartpbx_session():
                logger.error("smartpbx_media event=kb_error")
            else:
                logger.exception("KB retrieval failed")
            kb_context = ""
        finally:
            self._mark_smartpbx_turn("kb_complete")

        # A direct SmartPBX turn may lose ownership while its KB lookup is
        # awaiting a worker thread. Do not compose history or construct an LLM
        # request after that handoff; legacy/non-SmartPBX sessions retain their
        # existing permissive behavior through the shared ownership predicate.
        if not self._current_smartpbx_runner_owns_shared_state():
            outcome = "interrupted"
            return

        if runner is not None:
            self._commit_staged_turn_rate_state(runner)

        user_msg = self._compose_turn_user_message(text, kb_context)

        self.history.append({"role": "user", "content": user_msg})
        self.history = _trim_history(self.history)

        try:
            self._mark_smartpbx_turn_once("llm_request")
            if self.llm_provider == "claude":
                response_text = await self._run_llm_claude()
            elif self.llm_provider == "gemini":
                response_text = await self._run_llm_gemini()
            else:
                response_text = await self._run_llm()
            if not self._current_smartpbx_runner_owns_shared_state():
                outcome = "interrupted"
                return
            if runner is not None and runner.rate_turn:
                self._rate_followup_eligible = True
            self._mark_smartpbx_turn("llm_complete")
            if response_text:
                if self._is_smartpbx_session():
                    logger.info("smartpbx_media event=agent_response")
                else:
                    logger.info("Agent [%s]: %s", self.call_sid, response_text[:200])
                self._append_assistant_turn_to_transcript(response_text)
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        except Exception:
            if not self._current_smartpbx_runner_owns_shared_state():
                outcome = "interrupted"
                return
            outcome = "llm_failed"
            if self._is_smartpbx_session():
                logger.error("smartpbx_media event=llm_error")
            else:
                logger.exception("LLM error [%s]", self.call_sid)
            fillers = MEDIA_STREAM_FILLERS.get(self.lang, {})
            error_msg = fillers.get("_default", "I'm sorry, I encountered an error.")
            await self._invoke_speak(error_msg)
        finally:
            runner = _smartpbx_runner_context.get()
            if runner is not None:
                await self._cancel_smartpbx_tool_fillers(runner=runner)
            controller = None if runner is None else runner.initial_filler
            if (
                controller is None
                and turn_id == self._active_smartpbx_turn_id
            ):
                controller = self._smartpbx_initial_filler
            await self._finish_initial_smartpbx_filler(controller)
            if self.transfer_pending:
                outcome = "transfer_pending"
            elif turn_id in self._interrupted_smartpbx_turn_ids:
                outcome = "interrupted"
            elif turn_id in self._tts_failed_smartpbx_turn_ids:
                outcome = "tts_failed"
            elif turn_id in self._tool_failed_smartpbx_turn_ids:
                outcome = "tool_failed"
            if telemetry is not None and turn_id is not None:
                stale_runner = not self._current_smartpbx_runner_owns_shared_state()
                telemetry.finish(
                    turn_id, outcome,
                    generated_chars=len(response_text),
                    delivered_sentences=0 if stale_runner else len(self._delivered_sentences),
                    dropped_frames=self._smartpbx_dropped_frames_for_turn(turn_id),
                    **self._smartpbx_cadence_counts(turn_id),
                    **self._post_dispatch_counts_for_turn(turn_id),
                )
                self._retire_smartpbx_turn(turn_id)
                self._interrupted_smartpbx_turn_ids.discard(turn_id)
                self._tool_failed_smartpbx_turn_ids.discard(turn_id)
                self._tts_failed_smartpbx_turn_ids.discard(turn_id)
        # Pre-arm the patient timers for the NEXT guest turn(s) when this turn
        # actually asked the caller to dictate. Runs after the turn so it sees the
        # delivered sentences and cannot be undone by the capture tool's own exit.
        if self._current_smartpbx_runner_owns_shared_state():
            self._maybe_enter_capture_mode_from_ask()

    # â”€â”€ OpenAI streaming with tool use + sentence-level TTS â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    async def _run_llm(self) -> str:
        if not self._current_smartpbx_runner_owns_shared_state():
            return ""
        full_text = ""
        fillers = MEDIA_STREAM_FILLERS.get(self.lang, {})
        smartpbx_filler_sent = False
        # Capture-name/number/keypad flows are excluded here (spec §5): they
        # keep their pre-Phase-B specialised logic, not the new retry-nudge/
        # timeout-guard/shared-recovery policy this flag drives below.
        smartpbx_direct = self._is_direct_smartpbx_english_non_capture()
        # Turn-scoped (not round-scoped): one retry total, and only while no
        # tool/side effect has started this turn. Non-direct-SmartPBX callers
        # (Twilio Media Streams ar/si/ta) are untouched — max_attempts stays 1.
        empty_retry_used = False
        tool_executed = False

        for round_idx in range(MAX_TOOL_ROUNDS):
            if self._is_smartpbx_session():
                logger.info("smartpbx_media event=llm_round provider=openai round=%d", round_idx + 1)
            else:
                logger.info("LLM round %d [%s]", round_idx + 1, self.call_sid)

            gen = self._speak_generation
            initial_filler = self._start_initial_smartpbx_filler(
                round_idx=round_idx, generation=gen
            )
            max_attempts = 2 if (smartpbx_direct and not tool_executed) else 1

            for attempt in range(max_attempts):
                text_content = ""
                tool_calls_data: dict[int, dict[str, str]] = {}
                sentence_buffer = ""
                tts_tasks: list[asyncio.Task] = []
                has_tool_use = False

                messages = [{"role": "system", "content": self._active_system_prompt()}] + self.history
                if attempt > 0:
                    messages = messages + [
                        {"role": "system", "content": SMARTPBX_EMPTY_RETRY_NUDGE}
                    ]
                async def _acquire_openai_stream():
                    return await self.client.chat.completions.create(
                        model=self.model,
                        max_tokens=self._provider_max_tokens(),
                        messages=messages,
                        tools=self.tools or None,
                        stream=True,
                    )

                try:
                    if smartpbx_direct:
                        acquire_deadline = (
                            time.monotonic() + SMARTPBX_LLM_INITIAL_RESPONSE_TIMEOUT_SECONDS
                        )
                        stream = await _smartpbx_acquire_stream_within_deadline(
                            _acquire_openai_stream,
                            timeout=SMARTPBX_LLM_INITIAL_RESPONSE_TIMEOUT_SECONDS,
                        )
                        stream_iter = _smartpbx_timeout_guarded_stream(
                            stream,
                            initial_timeout=max(acquire_deadline - time.monotonic(), 0.0),
                            stall_timeout=SMARTPBX_LLM_STALL_TIMEOUT_SECONDS,
                        )
                    else:
                        stream_iter = await _acquire_openai_stream()

                    async for chunk in stream_iter:
                        choice = chunk.choices[0]
                        delta = choice.delta

                        if delta.content:
                            if initial_filler is not None:
                                await initial_filler.on_content_delta()
                                if initial_filler._cleared_after_spoke:
                                    gen = self._speak_generation
                            self._mark_smartpbx_turn_once("llm_first_token")
                            text_content += delta.content
                            if not has_tool_use:
                                sentence_buffer += delta.content
                                sentences, sentence_buffer = _extract_sentences(
                                    sentence_buffer
                                )
                                for s in sentences:
                                    task = self._start_smartpbx_round_tts(
                                        s, generation=gen, sentence=s,
                                    )
                                    if task is not None:
                                        tts_tasks.append(task)

                        if delta.tool_calls:
                            if initial_filler is not None:
                                await initial_filler.on_tool_delta()
                                if initial_filler._cleared_after_spoke:
                                    gen = self._speak_generation
                            self._mark_smartpbx_turn_once("llm_first_token")
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
                except _SmartPBXStreamTimeout as timeout_exc:
                    return await self._smartpbx_handle_stream_timeout(
                        timeout_exc, provider="openai",
                        tool_executed=tool_executed, gen=gen, full_text=full_text,
                        tts_tasks=tts_tasks,
                    )

                if not self._current_smartpbx_runner_owns_shared_state():
                    return ""

                if text_content.strip() or tool_calls_data:
                    break
                if not smartpbx_direct:
                    break
                # Only retry when there IS a next attempt to take (i.e. this
                # turn had not yet started a tool when max_attempts was
                # computed) and the one retry has not already been spent.
                if attempt + 1 < max_attempts and not empty_retry_used:
                    empty_retry_used = True
                    if self._is_smartpbx_session():
                        logger.warning(
                            "smartpbx_media event=llm_empty_response provider=openai "
                            "attempt=1 retrying=true"
                        )
                    continue
                if self._is_smartpbx_session():
                    logger.warning(
                        "smartpbx_media event=llm_empty_response provider=openai "
                        "attempt=%d retrying=false", attempt + 1,
                    )
                return await self._smartpbx_speak_recovery_and_finish(
                    tool_executed=tool_executed, gen=gen, full_text=full_text,
                )

            if self._is_direct_smartpbx_english():
                full_text = _join_turn(full_text, text_content)
            else:
                full_text += text_content

            if tool_calls_data:
                tool_list = list(tool_calls_data.values())
                if self._is_smartpbx_session():
                    logger.info("smartpbx_media event=tool_batch count=%d", len(tool_list))
                else:
                    logger.info("Tools [%s]: %s", self.call_sid, [t["name"] for t in tool_list])
                tool_filler_task: asyncio.Task | None = None
                first_tool = tool_list[0]["name"]
                transfer_in_batch = (
                    self._is_direct_smartpbx_english()
                    and any(tool["name"] == "transfer_to_human" for tool in tool_list)
                )
                if tts_tasks:
                    if transfer_in_batch:
                        # Let a just-created model sentence enter its owned TTS
                        # lifecycle before the actual transfer boundary fences it.
                        await asyncio.sleep(0)
                    else:
                        await asyncio.gather(*tts_tasks)
                if self._is_direct_smartpbx_english():
                    preamble = sentence_buffer.strip()
                    if preamble and not tts_tasks and first_tool != "transfer_to_human":
                        await self._invoke_speak(preamble, generation=gen, sentence=preamble)
                    elif (
                        first_tool != "transfer_to_human"
                        and first_tool not in _SMARTPBX_CAPTURE_TOOLS
                        and not smartpbx_filler_sent
                        and not text_content.strip()
                        and not (
                            initial_filler is not None
                            and initial_filler.suppress_specialized_tool_filler
                        )
                    ):
                        filler_lease = self._reserve_smartpbx_tool_filler(first_tool)
                        tool_filler_task = self._start_smartpbx_tool_filler(
                            filler_lease.text, generation=gen, lease=filler_lease,
                        )
                        await asyncio.sleep(0)
                        smartpbx_filler_sent = True
                else:
                    filler = fillers.get(first_tool, fillers.get("_default", ""))
                    if filler:
                        await self._invoke_speak(filler, generation=gen, sentence=filler)

                # Build assistant message with tool_calls
                assistant_msg: dict[str, Any] = {
                    "role": "assistant",
                    "content": self._assistant_turn_text_for_history(text_content) if text_content else None,
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
                # Execute every tool first, then publish the provider's request
                # and result block together. A barge-in may not strand a request
                # in shared history while its awaited effect is still running.
                staged_results: list[
                    tuple[int, dict[str, str], Any, str, BaseException | None]
                ] = []
                for tool_index, tc in enumerate(tool_list):
                    if not self._current_smartpbx_runner_can_execute_tools():
                        return ""
                    if (
                        tc["name"] == "transfer_to_human"
                        and self._is_direct_smartpbx_english()
                    ):
                        prepared_generation = await self._prepare_smartpbx_transfer_handoff(
                            tts_tasks=tts_tasks, initial_filler=initial_filler,
                            tool_filler_task=tool_filler_task,
                            generation=gen,
                        )
                        tool_filler_task = None
                        if prepared_generation is None:
                            return ""
                        gen = prepared_generation
                    try:
                        parsed_input = json.loads(tc["arguments"]) if tc["arguments"] else {}
                    except json.JSONDecodeError:
                        logger.error("Bad tool JSON for %s", tc["name"])
                        parsed_input = {}
                    parsed_input, _ = _override_capture_spoken_argument(
                        tool_name=tc["name"],
                        tool_input=parsed_input,
                        override_spoken=self._smartpbx_runner_raw_utterance(),
                        source="smartpbx_media",
                    )
                    self._log_tool_execution(
                        tc["name"], parsed_input, capture_slots=False,
                    )
                    tool_error: BaseException | None = None
                    # Set BEFORE the await: a tool that raises half-way may
                    # already have had its effect, so this turn is no longer
                    # replayable — the shared empty-response policy must never
                    # retry it, only recover with the post-tool-start line.
                    tool_executed = True
                    try:
                        if tc["name"] == "collect_number_via_keypad":
                            result_str = await self._collect_number_via_keypad(parsed_input)
                        else:
                            result_str = await execute_tool(tc["name"], parsed_input)
                    except asyncio.CancelledError:
                        await self._cancel_smartpbx_round_tts(tts_tasks)
                        await self._finish_smartpbx_tool_filler(
                            tool_filler_task, cancel=True
                        )
                        raise
                    except Exception as exc:
                        tool_error = exc
                        if self._is_direct_smartpbx_english():
                            assistant_msg["tool_calls"][tool_index]["function"]["arguments"] = "{}"
                            result_str = json.dumps({"error": "tool_execution_failed"})
                        else:
                            result_str = json.dumps({"error": str(exc)})
                    if not self._current_smartpbx_runner_owns_shared_state(
                        tool_executed=tool_executed
                    ):
                        await self._cancel_smartpbx_round_tts(tts_tasks)
                        await self._finish_smartpbx_tool_filler(
                            tool_filler_task, cancel=True
                        )
                        return ""
                    if (
                        tc["name"] == "transfer_to_human"
                        and not self.transfer_pending
                    ):
                        self._smartpbx_transfer_audio_fenced = False
                    staged_results.append(
                        (tool_index, tc, parsed_input, result_str, tool_error)
                    )
                    if self.transfer_pending:
                        break

                await self._finish_smartpbx_tool_filler(
                    tool_filler_task, cancel=self.transfer_pending
                )
                if initial_filler is not None and first_tool != "transfer_to_human":
                    await initial_filler.wait()
                if not self._current_smartpbx_runner_owns_shared_state(
                    tool_executed=tool_executed
                ):
                    return ""
                if len(staged_results) != len(tool_list):
                    assistant_msg["tool_calls"] = assistant_msg["tool_calls"][:len(staged_results)]
                self._append_assistant_history(assistant_msg)
                for _tool_index, tc, _parsed_input, result_str, _tool_error in staged_results:
                    self.history.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": result_str,
                    })
                for _tool_index, tc, parsed_input, result_str, tool_error in staged_results:
                    self._capture_booking_slots(tc["name"], parsed_input)
                    try:
                        parsed_result = json.loads(result_str)
                    except (json.JSONDecodeError, TypeError):
                        parsed_result = None
                    if isinstance(parsed_result, dict):
                        self._record_capture_tool_completion(tc["name"], parsed_result)
                    _append_booking_confirmation_marker(
                        self.full_transcript,
                        tc["name"],
                        parsed_input,
                        result_str,
                    )
                    if tool_error is not None:
                        self._log_tool_failure(tc["name"], tool_error)
                    self._log_tool_result(tc["name"], result_str)
                if self.transfer_pending:
                    return full_text

                continue

            # No tools — flush remaining sentence buffer
            remaining = sentence_buffer.strip()
            if remaining:
                task = self._start_smartpbx_round_tts(
                    remaining, generation=gen, sentence=remaining,
                )
                if task is not None:
                    tts_tasks.append(task)
            if tts_tasks:
                await asyncio.gather(*tts_tasks)

            if not self._current_smartpbx_runner_owns_shared_state():
                return ""
            if text_content:
                self._append_assistant_history({
                    "role": "assistant",
                    "content": self._assistant_turn_text_for_history(text_content),
                })
            return full_text

        if self._is_smartpbx_session():
            logger.warning("smartpbx_media event=tool_round_limit provider=openai")
        else:
            logger.warning("Exhausted %d tool rounds [%s]", MAX_TOOL_ROUNDS, self.call_sid)
        return full_text

    # â”€â”€ Gemini native streaming with tool use + sentence-level TTS â”€â”€â”€â”€â”€â”€â”€

    def _gemini_failover_ready(self) -> bool:
        """True when this session can re-run a Gemini turn through Claude."""
        if not GEMINI_FAILOVER_TO_CLAUDE:
            return False
        if self.anthropic_client is not None:
            return True
        if not ANTHROPIC_API_KEY:
            return False
        try:
            self.anthropic_client = _get_anthropic_client()
        except RuntimeError:
            return False
        return self.anthropic_client is not None

    async def _run_claude_failover_turn(self) -> str:
        """Run this turn on Claude with Claude-shaped model AND tools, then restore.

        Every Gemini→Claude failover lands here — sticky and per-exception alike.
        Swapping the runner without swapping the tool shape is not a failover but
        a second failure: Anthropic 400s on Gemini's ``function_declarations``
        payload, so the caller would hear the error line instead of an answer.
        """
        if not self._current_smartpbx_runner_owns_shared_state():
            return ""
        # The failed round may have already armed its own initial filler
        # (round 0, before the exception hit). If it already spoke, retire
        # it now -- cancel-and-await, clearing the reference -- before
        # Claude gets a chance to start speaking, so a stray filler task is
        # never left running unattended. If it has NOT spoken yet,
        # deliberately leave it alone: _start_initial_smartpbx_filler adopts
        # a live not-yet-spoken filler for the same generation instead of
        # starting a second one, so ownership transfers cleanly to Claude's
        # own round 0 rather than being orphaned.
        stale_filler = self._smartpbx_initial_filler
        if stale_filler is not None and stale_filler.spoke:
            await self._finish_initial_smartpbx_filler(stale_filler)
        previous_model = self.model
        previous_tools = self.tools
        previous_provider = self.llm_provider
        self.model = CLAUDE_MODEL
        if previous_provider == "gemini":
            self.tools = _claude_tools_from_gemini(previous_tools)
        self.llm_provider = "claude"
        try:
            return await self._run_llm_claude()
        finally:
            self.model = previous_model
            self.tools = previous_tools
            self.llm_provider = previous_provider

    async def _run_llm_gemini(self) -> str:
        """Gemini-native streaming version of _run_llm for Media Streams.

        Sentence-level: every completed sentence is handed to TTS as it arrives,
        through the same ``_extract_sentences``  ``_invoke_speak`` pipeline (and
        therefore the same generation fence and delivered-sentence tracking) the
        Claude path uses. Lang-agnostic the Sinhala/Tamil/Arabic Media Streams
        calls and the direct SmartPBX English calls all run through here.
        """
        if not self._current_smartpbx_runner_owns_shared_state():
            return ""
        if self._gemini_failover_state.get("degraded") and ANTHROPIC_API_KEY:
            logger.info(
                "smartpbx_media event=llm_provider_failover from=gemini to=claude reason=sticky"
            )
            return await self._run_claude_failover_turn()

        # Threaded out of the _run_gemini_turn closure so the enclosing
        # except handler (below) can gate history-truncation + Claude replay
        # on whether a tool already executed this turn — a genuine exception
        # (e.g. a 429) in a later round must not truncate history and replay
        # a turn that already committed a tool side effect (create_booking).
        # Mirrors the `replayable` gate _run_gemini_turn already applies to
        # the empty-response case.
        turn_tool_executed = False
        turn_full_text = ""
        turn_gen = self._speak_generation

        async def _run_gemini_turn() -> str:
            nonlocal turn_tool_executed, turn_full_text, turn_gen
            full_text = ""
            fillers = MEDIA_STREAM_FILLERS.get(self.lang, {})
            smartpbx_filler_sent = False
            # One empty-response retry per guest turn, not per tool round.
            empty_retry_used = False
            # Any tool that has STARTED executing makes this turn unreplayable —
            # a Claude re-run would repeat its side effects (create_booking).
            tool_executed = False

            for round_idx in range(MAX_TOOL_ROUNDS):
                if self._is_smartpbx_session():
                    logger.info("smartpbx_media event=llm_round provider=gemini round=%d", round_idx + 1)
                else:
                    logger.info("Gemini round %d [%s]", round_idx + 1, self.call_sid)

                text_content = ""
                function_calls: list[dict] = []
                sentence_buffer = ""
                tts_tasks: list[asyncio.Task] = []
                has_tool_use = False
                finish_reason = None
                gen = self._speak_generation
                turn_gen = gen
                nudge: str | None = None
                initial_filler = self._start_initial_smartpbx_filler(
                    round_idx=round_idx, generation=gen
                )

                # Attempt 0 is the real turn. Attempt 1 only happens when attempt 0
                # streamed absolutely nothing (no text, no tool call, or a blocked
                # response) and this turn has not already spent its one retry.
                for attempt in range(2):
                    text_content = ""
                    function_calls = []
                    sentence_buffer = ""
                    has_tool_use = False
                    finish_reason = None

                    async def _acquire_gemini_stream():
                        return await _open_gemini_stream(
                            self.gemini_client,
                            model=self.model,
                            contents=_history_to_gemini(self.history),
                            config=_build_gemini_config(
                                system=self._active_system_prompt(),
                                tools=self.tools,
                                model=self.model,
                                nudge=nudge,
                                max_output_tokens=self._provider_max_tokens(),
                            ),
                        )

                    # Capture flows keep their pre-Phase-B logic (spec §5) —
                    # no stream-timeout guard while dictating a name/number.
                    smartpbx_direct_round = self._is_direct_smartpbx_english_non_capture()

                    try:
                        if smartpbx_direct_round:
                            acquire_deadline = (
                                time.monotonic() + SMARTPBX_LLM_INITIAL_RESPONSE_TIMEOUT_SECONDS
                            )
                            response = await _smartpbx_acquire_stream_within_deadline(
                                _acquire_gemini_stream,
                                timeout=SMARTPBX_LLM_INITIAL_RESPONSE_TIMEOUT_SECONDS,
                            )
                            response_iter = _smartpbx_timeout_guarded_stream(
                                response,
                                initial_timeout=max(acquire_deadline - time.monotonic(), 0.0),
                                stall_timeout=SMARTPBX_LLM_STALL_TIMEOUT_SECONDS,
                            )
                        else:
                            response_iter = await _acquire_gemini_stream()

                        async for kind, payload in _iter_gemini_stream(response_iter):
                            if kind == "finish":
                                finish_reason = payload
                                continue
                            if kind == "tool":
                                if initial_filler is not None:
                                    await initial_filler.on_tool_delta()
                                    if initial_filler._cleared_after_spoke:
                                        gen = self._speak_generation
                                self._mark_smartpbx_turn_once("llm_first_token")
                                has_tool_use = True
                                function_calls.append(payload)
                                continue

                            if initial_filler is not None:
                                await initial_filler.on_content_delta()
                                if initial_filler._cleared_after_spoke:
                                    gen = self._speak_generation
                            self._mark_smartpbx_turn_once("llm_first_token")
                            text_content += payload
                            # Once a function call has appeared the remaining text is a
                            # pre-tool aside, and a barge-in means the caller is talking
                            # over her right now in both cases stop scheduling speech,
                            # but keep draining so history records the full turn.
                            if has_tool_use or self._speak_generation != gen:
                                continue
                            sentence_buffer += payload
                            sentences, sentence_buffer = _extract_sentences(sentence_buffer)
                            if not sentences:
                                continue
                            for s in sentences:
                                task = self._start_smartpbx_round_tts(
                                    s, generation=gen, sentence=s,
                                )
                                if task is not None:
                                    tts_tasks.append(task)
                            # Hand control to those tasks NOW. Without this the first TTS
                            # request only started once the stream had been fully drained
                            # (create_task alone schedules nothing until this coroutine
                            # suspends), which is why this path used to emit a single TTS
                            # call after llm_round_complete instead of speaking sentence
                            # by sentence.
                            await asyncio.sleep(0)
                    except _SmartPBXStreamTimeout as timeout_exc:
                        return await self._smartpbx_handle_stream_timeout(
                            timeout_exc, provider="gemini",
                            tool_executed=tool_executed, gen=gen, full_text=full_text,
                            tts_tasks=tts_tasks,
                        )

                    if text_content.strip() or function_calls:
                        break

                    # Only retry when no tool has started yet this turn — a
                    # retry after a tool has run would replay the provider
                    # turn against history that already has a committed tool
                    # result, which the shared empty-response policy forbids.
                    retrying = (
                        attempt == 0 and not empty_retry_used and not tool_executed
                    )
                    _log_gemini_empty(
                        path="media_stream",
                        attempt=attempt + 1,
                        finish_reason=finish_reason,
                        retrying=retrying,
                        call_sid=self.call_sid,
                    )
                    if self._is_smartpbx_session():
                        logger.warning(
                            "smartpbx_media event=llm_empty_response provider=gemini "
                            "attempt=%d retrying=%s",
                            attempt + 1, str(retrying).lower(),
                        )
                    if not retrying:
                        break
                    empty_retry_used = True
                    nudge = GEMINI_EMPTY_RETRY_NUDGE

                if self._is_smartpbx_session():
                    logger.info("smartpbx_media event=llm_round_complete provider=gemini tools=%d", len(function_calls))
                else:
                    logger.info("Gemini round %d [%s] text=%d chars, tools=%d, finish=%s", round_idx + 1, self.call_sid, len(text_content), len(function_calls), finish_reason)

                if not self._current_smartpbx_runner_owns_shared_state():
                    return ""
                if self._is_direct_smartpbx_english():
                    full_text = _join_turn(full_text, text_content)
                else:
                    full_text += text_content
                turn_full_text = full_text

                # Still nothing after the retry. Two empty turns in a row is a
                # provider failure, not a short answer, so raise it into the same
                # failover path a quota error takes: Claude can answer the turn
                # properly. The canned line is what happens only when failover is
                # unavailable — see the handler below.
                if not text_content.strip() and not function_calls:
                    # Failover REPLAYS the whole turn from the same history, so it
                    # is only safe while this turn has no side effects yet: nothing
                    # spoken, no tool executed. An empty LATER round must take the
                    # canned line instead, or Claude re-runs the round that already
                    # ran create_booking and the guest is booked twice.
                    replayable = (
                        round_idx == 0
                        and not full_text.strip()
                        and not tool_executed
                        and not smartpbx_filler_sent
                    )
                    if replayable and self._gemini_failover_ready():
                        raise _GeminiEmptyTurnError()
                    if not replayable:
                        logger.warning(
                            "gemini_diagnostic event=empty_response_not_replayable "
                            "path=media_stream round=%d tools_executed=%s call=%s",
                            round_idx + 1, str(tool_executed).lower(),
                            self.call_sid or "-",
                        )
                    logger.warning(
                        "gemini_diagnostic event=empty_response_fallback path=media_stream "
                        "call=%s lang=%s", self.call_sid or "-", self.lang,
                    )
                    # Direct SmartPBX English uses the one shared recovery
                    # policy (same two lines OpenAI/Claude use, selected by
                    # tool_executed); every other language/path — including
                    # capture-name/number/keypad flows (spec §5 carve-out) —
                    # keeps the existing per-language canned fallback unchanged.
                    if self._is_direct_smartpbx_english_non_capture():
                        return await self._smartpbx_speak_recovery_and_finish(
                            tool_executed=tool_executed, gen=gen, full_text=full_text,
                        )
                    fallback = LLM_EMPTY_FALLBACKS.get(
                        self.lang, LLM_EMPTY_FALLBACKS["en"]
                    )
                    await self._invoke_speak(fallback, generation=gen, sentence=fallback)
                    if not self._current_smartpbx_runner_owns_shared_state():
                        return ""
                    self._append_assistant_history({
                        "role": "assistant",
                        "content": fallback
                    })
                    return full_text + fallback

                if function_calls:
                    if self._is_smartpbx_session():
                        logger.info("smartpbx_media event=tool_batch count=%d", len(function_calls))
                    else:
                        logger.info("Tools [%s]: %s", self.call_sid, [fc["name"] for fc in function_calls])
                    tool_filler_task: asyncio.Task | None = None
                    first_tool = function_calls[0]["name"]
                    transfer_in_batch = (
                        self._is_direct_smartpbx_english()
                        and any(
                            tool["name"] == "transfer_to_human"
                            for tool in function_calls
                        )
                    )
                    if tts_tasks:
                        if transfer_in_batch:
                            # Let a just-created model sentence enter its owned TTS
                            # lifecycle before the actual transfer boundary fences it.
                            await asyncio.sleep(0)
                        else:
                            await asyncio.gather(*tts_tasks)
                    if self._is_direct_smartpbx_english():
                        preamble = sentence_buffer.strip()
                        if preamble and not tts_tasks and first_tool != "transfer_to_human":
                            await self._invoke_speak(preamble, generation=gen, sentence=preamble)
                        elif (
                            first_tool != "transfer_to_human"
                            and first_tool not in _SMARTPBX_CAPTURE_TOOLS
                            and not smartpbx_filler_sent
                            and not text_content.strip()
                            and not (
                                initial_filler is not None
                                and initial_filler.suppress_specialized_tool_filler
                            )
                        ):
                            filler_lease = self._reserve_smartpbx_tool_filler(first_tool)
                            tool_filler_task = self._start_smartpbx_tool_filler(
                                filler_lease.text, generation=gen, lease=filler_lease,
                            )
                            await asyncio.sleep(0)
                            smartpbx_filler_sent = True
                    else:
                        filler = fillers.get(first_tool, fillers.get("_default", ""))
                        if filler:
                            await self._invoke_speak(filler, generation=gen, sentence=filler)

                    # Build assistant message in OpenAI format
                    tool_calls_openai = []
                    for i, fc in enumerate(function_calls):
                        tc_id = f"gemini_tc_{round_idx}_{i}"
                        entry: dict[str, Any] = {
                            "id": tc_id,
                            "type": "function",
                            "function": {
                                "name": fc["name"],
                                "arguments": json.dumps(fc["args"]),
                            },
                        }
                        if fc.get("thought_signature"):
                            entry["gemini_thought_signature"] = fc["thought_signature"]
                        tool_calls_openai.append(entry)

                    assistant_msg: dict[str, Any] = {
                        "role": "assistant",
                        "content": self._assistant_turn_text_for_history(text_content) if text_content else None,
                        "tool_calls": tool_calls_openai,
                    }
                    staged_results: list[
                        tuple[dict[str, Any], Any, str, BaseException | None]
                    ] = []
                    for tc in tool_calls_openai:
                        if not self._current_smartpbx_runner_can_execute_tools():
                            return ""
                        if (
                            tc["function"]["name"] == "transfer_to_human"
                            and self._is_direct_smartpbx_english()
                        ):
                            prepared_generation = await self._prepare_smartpbx_transfer_handoff(
                                tts_tasks=tts_tasks, initial_filler=initial_filler,
                                tool_filler_task=tool_filler_task,
                                generation=gen,
                            )
                            tool_filler_task = None
                            if prepared_generation is None:
                                return ""
                            gen = prepared_generation
                        parsed_input = json.loads(tc["function"]["arguments"]) if tc["function"]["arguments"] else {}
                        parsed_input, _ = _override_capture_spoken_argument(
                            tool_name=tc["function"]["name"],
                            tool_input=parsed_input,
                            override_spoken=self._smartpbx_runner_raw_utterance(),
                            source="smartpbx_media",
                        )
                        self._log_tool_execution(
                            tc["function"]["name"], parsed_input, capture_slots=False,
                        )
                        # Set BEFORE the await: a tool that raises half-way may
                        # already have had its effect, so the turn is no longer
                        # safe to replay on Claude.
                        tool_executed = True
                        turn_tool_executed = True
                        tool_error: BaseException | None = None
                        try:
                            if tc["function"]["name"] == "collect_number_via_keypad":
                                result_str = await self._collect_number_via_keypad(parsed_input)
                            else:
                                result_str = await execute_tool(tc["function"]["name"], parsed_input)
                        except asyncio.CancelledError:
                            await self._cancel_smartpbx_round_tts(tts_tasks)
                            await self._finish_smartpbx_tool_filler(
                                tool_filler_task, cancel=True
                            )
                            raise
                        except Exception as exc:
                            tool_error = exc
                            if self._is_direct_smartpbx_english():
                                tc["function"]["arguments"] = "{}"
                                result_str = json.dumps({"error": "tool_execution_failed"})
                            else:
                                result_str = json.dumps({"error": str(exc)})
                        if not self._current_smartpbx_runner_owns_shared_state(
                            tool_executed=tool_executed
                        ):
                            await self._cancel_smartpbx_round_tts(tts_tasks)
                            await self._finish_smartpbx_tool_filler(
                                tool_filler_task, cancel=True
                            )
                            return ""
                        if (
                            tc["function"]["name"] == "transfer_to_human"
                            and not self.transfer_pending
                        ):
                            self._smartpbx_transfer_audio_fenced = False
                        staged_results.append(
                            (tc, parsed_input, result_str, tool_error)
                        )
                        if self.transfer_pending:
                            break

                    await self._finish_smartpbx_tool_filler(
                        tool_filler_task, cancel=self.transfer_pending
                    )
                    if initial_filler is not None and first_tool != "transfer_to_human":
                        await initial_filler.wait()
                    if not self._current_smartpbx_runner_owns_shared_state(
                        tool_executed=tool_executed
                    ):
                        await self._finish_smartpbx_tool_filler(
                            tool_filler_task, cancel=True
                        )
                        return ""
                    if len(staged_results) != len(tool_calls_openai):
                        assistant_msg["tool_calls"] = assistant_msg["tool_calls"][:len(staged_results)]
                    self._append_assistant_history(assistant_msg)
                    for tc, _parsed_input, result_str, _tool_error in staged_results:
                        self.history.append({
                            "role": "tool",
                            "tool_call_id": tc["id"],
                            "content": result_str,
                        })
                    for tc, parsed_input, result_str, tool_error in staged_results:
                        try:
                            parsed_result = json.loads(result_str)
                        except (json.JSONDecodeError, TypeError):
                            parsed_result = None
                        if isinstance(parsed_result, dict):
                            self._record_capture_tool_completion(
                                tc["function"]["name"], parsed_result
                            )
                        _append_booking_confirmation_marker(
                            self.full_transcript,
                            tc["function"]["name"],
                            parsed_input,
                            result_str,
                        )
                        self._capture_booking_slots(tc["function"]["name"], parsed_input)
                        if tool_error is not None:
                            self._log_tool_failure(tc["function"]["name"], tool_error)
                        self._log_tool_result(tc["function"]["name"], result_str)
                    if self.transfer_pending:
                        return full_text

                    continue

                remaining = sentence_buffer.strip()
                if remaining:
                    task = self._start_smartpbx_round_tts(
                        remaining, generation=gen, sentence=remaining,
                    )
                    if task is not None:
                        tts_tasks.append(task)
                if tts_tasks:
                    await asyncio.gather(*tts_tasks)

                if not self._current_smartpbx_runner_owns_shared_state():
                    return ""
                if text_content:
                    self._append_assistant_history({
                        "role": "assistant",
                        "content": self._assistant_turn_text_for_history(text_content),
                    })
                return full_text

            if self._is_smartpbx_session():
                logger.warning("smartpbx_media event=tool_round_limit provider=gemini")
            else:
                logger.warning("Exhausted %d tool rounds (Gemini) [%s]", MAX_TOOL_ROUNDS, self.call_sid)
            return full_text

        gemini_history_len = len(self.history)
        try:
            response_text = await _run_gemini_turn()
            if not self._current_smartpbx_runner_owns_shared_state():
                return ""
            _note_gemini_success(self._gemini_failover_state)
            return response_text
        except Exception as exc:
            if not self._current_smartpbx_runner_owns_shared_state():
                return ""

            reason = (
                "empty_response" if isinstance(exc, _GeminiEmptyTurnError)
                else _classify_gemini_exception(exc)
            )

            if turn_tool_executed:
                # A tool already executed this turn (e.g. create_booking
                # committed). Truncating history and replaying via Claude
                # would duplicate that side effect, so failover is skipped
                # entirely here — same gate the empty-response `replayable`
                # check already applies inside _run_gemini_turn.
                logger.warning(
                    "gemini_diagnostic event=exception_not_replayable "
                    "path=media_stream tool_executed=true reason=%s call=%s",
                    reason, self.call_sid or "-",
                )
                if self._is_direct_smartpbx_english():
                    return await self._smartpbx_speak_recovery_and_finish(
                        tool_executed=True, gen=turn_gen, full_text=turn_full_text,
                    )
                fallback = LLM_EMPTY_FALLBACKS.get(self.lang, LLM_EMPTY_FALLBACKS["en"])
                await self._invoke_speak(fallback, generation=turn_gen, sentence=fallback)
                if not self._current_smartpbx_runner_owns_shared_state():
                    return ""
                self._append_assistant_history({
                    "role": "assistant",
                    "content": fallback,
                })
                return turn_full_text + fallback

            if len(self.history) > gemini_history_len:
                self.history = self.history[:gemini_history_len]

            if not GEMINI_FAILOVER_TO_CLAUDE:
                raise

            # A configured client is sufficient; the key only gates building one.
            if self.anthropic_client is None:
                if not ANTHROPIC_API_KEY:
                    raise
                try:
                    self.anthropic_client = _get_anthropic_client()
                except RuntimeError:
                    raise

            if self.anthropic_client is None:
                raise

            logger.warning(
                "smartpbx_media event=llm_provider_failover from=gemini to=claude reason=%s",
                reason,
            )

            _note_gemini_failover(self._gemini_failover_state)
            return await self._run_claude_failover_turn()


    # â”€â”€ Claude native streaming with tool use + sentence-level TTS â”€â”€â”€â”€â”€â”€â”€

    async def _run_llm_claude(self) -> str:
        """Anthropic Claude streaming for Media Streams with sentence-level TTS."""
        if not self._current_smartpbx_runner_owns_shared_state():
            return ""
        full_text = ""
        fillers = MEDIA_STREAM_FILLERS.get(self.lang, {})
        smartpbx_filler_sent = False
        # Capture-name/number/keypad flows are excluded here (spec §5): they
        # keep their pre-Phase-B specialised logic, not the new retry-nudge/
        # timeout-guard/shared-recovery policy this flag drives below.
        smartpbx_direct = self._is_direct_smartpbx_english_non_capture()
        # Turn-scoped (not round-scoped): one retry total, and only while no
        # tool/side effect has started this turn. Non-direct-SmartPBX callers
        # (Twilio Media Streams ar/si/ta) are untouched — max_attempts stays 1.
        empty_retry_used = False
        tool_executed = False

        for round_idx in range(MAX_TOOL_ROUNDS):
            if self._is_smartpbx_session():
                logger.info("smartpbx_media event=llm_round provider=claude round=%d", round_idx + 1)
            else:
                logger.info("Claude round %d [%s]", round_idx + 1, self.call_sid)

            gen = self._speak_generation
            initial_filler = self._start_initial_smartpbx_filler(
                round_idx=round_idx, generation=gen
            )
            max_attempts = 2 if (smartpbx_direct and not tool_executed) else 1

            for attempt in range(max_attempts):
                text_content = ""
                tool_use_blocks: list[dict[str, Any]] = []
                cur_tool_name: str | None = None
                cur_tool_id: str | None = None
                tool_json = ""
                # Round-outcome inputs, reset per attempt: a retry must be
                # classified on its own stream, never on the previous one's.
                stop_reason: str | None = None
                output_tokens: int | None = None
                malformed_tool_json = False
                # True once the stream reports the turn is OVER (`message_delta`
                # carries the stop reason, `message_stop` closes the message).
                # An abrupt EOF leaves this False and must never be read as a
                # clean empty turn.
                saw_terminal_metadata = False

                sentence_buffer = ""
                tts_tasks: list[asyncio.Task] = []
                has_tool_use = False
                # Claude may spend a long interval in adaptive thinking before
                # opening visible text. Keep only this closed progress enum;
                # never retain or log thinking content.
                stream_progress = "none"

                # Prompt caching: marking the system prompt with cache_control
                # caches the entire request prefix (tools + system) for ~5
                # min. Cuts input tokens and ITPM pressure dramatically on the
                # 2nd+ turn of every call.
                system_blocks = _build_claude_system_blocks(
                    self.system_prompt + self._smartpbx_rhythm_rule(),
                    self._booking_slots_note(),
                )
                if attempt > 0:
                    system_blocks = system_blocks + [
                        {"type": "text", "text": SMARTPBX_EMPTY_RETRY_NUDGE}
                    ]

                acquire_deadline = time.monotonic() + SMARTPBX_LLM_INITIAL_RESPONSE_TIMEOUT_SECONDS
                stream_cm = self.anthropic_client.messages.stream(
                    model=self.model,
                    max_tokens=self._provider_max_tokens("claude"),
                    system=system_blocks,
                    messages=self.history,
                    tools=self.tools if self.tools else NOT_GIVEN,
                )
                if smartpbx_direct:
                    # Entering this context manager is where the real network
                    # call happens — a hang there must trip the same
                    # initial-response deadline as a hanging first delta, not
                    # block forever before the guarded generator even starts.
                    stream_cm = _TimeoutGuardedAsyncCM(
                        stream_cm, timeout=SMARTPBX_LLM_INITIAL_RESPONSE_TIMEOUT_SECONDS
                    )
                try:
                    async with stream_cm as stream:
                        stream_iter = (
                            _smartpbx_timeout_guarded_stream(
                                stream,
                                initial_timeout=max(acquire_deadline - time.monotonic(), 0.0),
                                stall_timeout=SMARTPBX_LLM_STALL_TIMEOUT_SECONDS,
                                stall_timeout_getter=(
                                    lambda: (
                                        SMARTPBX_CLAUDE_THINKING_STALL_TIMEOUT_SECONDS
                                        if attempt == 0 and stream_progress == "thinking"
                                        else SMARTPBX_LLM_STALL_TIMEOUT_SECONDS
                                    )
                                ),
                            )
                            if smartpbx_direct
                            else stream
                        )
                        async for event in stream_iter:
                            if event.type == "message_start":
                                stream_progress = _advance_claude_stream_progress(
                                    stream_progress, "metadata"
                                )

                            elif event.type == "content_block_start":
                                if event.content_block.type == "tool_use":
                                    stream_progress = _advance_claude_stream_progress(
                                        stream_progress, "tool"
                                    )
                                    if initial_filler is not None:
                                        await initial_filler.on_tool_delta()
                                        if initial_filler._cleared_after_spoke:
                                            gen = self._speak_generation
                                    self._mark_smartpbx_turn_once("llm_first_token")
                                    has_tool_use = True
                                    cur_tool_id = event.content_block.id
                                    cur_tool_name = event.content_block.name
                                    tool_json = ""
                                elif event.content_block.type == "thinking":
                                    stream_progress = _advance_claude_stream_progress(
                                        stream_progress, "thinking"
                                    )

                            elif event.type == "content_block_delta":
                                if event.delta.type == "text_delta":
                                    if not has_tool_use and event.delta.text:
                                        stream_progress = _advance_claude_stream_progress(
                                            stream_progress, "text"
                                        )
                                    if initial_filler is not None:
                                        await initial_filler.on_content_delta()
                                        if initial_filler._cleared_after_spoke:
                                            gen = self._speak_generation
                                    self._mark_smartpbx_turn_once("llm_first_token")
                                    text_content += event.delta.text
                                    # A text delta after a tool_use block starts must
                                    # not speak (the tool round replies next turn) —
                                    # and `sentences` only binds inside this branch.
                                    if not has_tool_use:
                                        sentence_buffer += event.delta.text
                                        sentences, sentence_buffer = _extract_sentences(
                                            sentence_buffer
                                        )
                                        for s in sentences:
                                            task = self._start_smartpbx_round_tts(
                                                s, generation=gen, sentence=s,
                                            )
                                            if task is not None:
                                                tts_tasks.append(task)

                                elif event.delta.type == "input_json_delta":
                                    stream_progress = _advance_claude_stream_progress(
                                        stream_progress, "tool"
                                    )
                                    tool_json += event.delta.partial_json

                            elif event.type == "content_block_stop":
                                if cur_tool_name:
                                    try:
                                        parsed = json.loads(tool_json) if tool_json else {}
                                    except json.JSONDecodeError:
                                        if self._is_smartpbx_session():
                                            logger.error("smartpbx_media event=bad_tool_json")
                                        else:
                                            logger.error("Bad tool JSON for %s: %s",
                                                         cur_tool_name, tool_json[:200])
                                        # Discard, never execute. Running a
                                        # tool on arguments we failed to parse
                                        # invents a side effect the model never
                                        # actually asked for; the outcome
                                        # classification below carries the
                                        # failure instead.
                                        malformed_tool_json = True
                                        parsed = None
                                    if parsed is not None:
                                        tool_use_blocks.append({
                                            "id": cur_tool_id,
                                            "name": cur_tool_name,
                                            "input": parsed,
                                        })
                                    cur_tool_name = None
                                    cur_tool_id = None
                                    tool_json = ""

                            elif event.type == "message_delta":
                                # The only place Claude reports WHY the turn
                                # ended. Without it a budget-truncated tool
                                # turn is indistinguishable from a genuinely
                                # empty one.
                                saw_terminal_metadata = True
                                delta = getattr(event, "delta", None)
                                delta_stop = getattr(delta, "stop_reason", None)
                                if delta_stop:
                                    stop_reason = delta_stop
                                usage = getattr(event, "usage", None)
                                delta_output_tokens = getattr(usage, "output_tokens", None)
                                if delta_output_tokens is not None:
                                    output_tokens = delta_output_tokens

                            elif event.type == "message_stop":
                                # The message closed cleanly. Recorded even
                                # though it carries no payload: together with
                                # `message_delta` it is the ONLY evidence that
                                # the stream ended because the turn ended,
                                # rather than because the connection dropped.
                                saw_terminal_metadata = True
                except _SmartPBXStreamTimeout as timeout_exc:
                    # Only a direct SmartPBX Claude stall after metadata or
                    # thinking progress is safely retryable. Initial
                    # acquisition timeouts remain one request/one recovery;
                    # visible text, a tool-use start, a completed tool, and
                    # any ownership loss are all fail-closed.
                    retry_eligible = (
                        smartpbx_direct
                        and timeout_exc.phase == _SmartPBXStreamTimeout.PHASE_STALL
                        and stream_progress in {"metadata", "thinking"}
                        and not has_tool_use
                        and not tool_executed
                        and attempt + 1 < max_attempts
                        and self._current_smartpbx_runner_owns_shared_state()
                    )
                    timeout_result = await self._smartpbx_handle_stream_timeout(
                        timeout_exc,
                        provider="claude",
                        tool_executed=tool_executed,
                        gen=gen,
                        full_text=full_text,
                        tts_tasks=tts_tasks,
                        progress=stream_progress,
                        retrying=retry_eligible,
                        attempt=attempt + 1,
                        recover=not retry_eligible,
                    )
                    if not retry_eligible:
                        return timeout_result
                    if not self._current_smartpbx_runner_owns_shared_state():
                        return ""
                    empty_retry_used = True
                    initial_filler = None
                    gen = self._speak_generation
                    continue

                if not self._current_smartpbx_runner_owns_shared_state():
                    return ""

                # A tool block still open when the stream ended never reached
                # `content_block_stop`, so it was never accumulated and must
                # never be executed -- that is precisely the truncation bug.
                # Drop the partial JSON here so nothing downstream can see it.
                incomplete_tool_block = cur_tool_name is not None
                if incomplete_tool_block:
                    cur_tool_name = None
                    cur_tool_id = None
                    tool_json = ""
                outcome = _classify_claude_round_outcome(
                    text_content=text_content,
                    tool_use_blocks=tool_use_blocks,
                    incomplete_tool_block=incomplete_tool_block,
                    malformed_tool_json=malformed_tool_json,
                    stop_reason=stop_reason,
                    saw_terminal_metadata=saw_terminal_metadata,
                )
                if self._is_smartpbx_session():
                    # Privacy-safe by construction: an enum, a bounded stop
                    # reason enum, a clamped token count and a clamped attempt
                    # index -- no text, no tool arguments, no caller
                    # identifiers, and no unbounded field of any kind.
                    log_round_outcome = (
                        logger.info
                        if outcome is SmartPBXClaudeRoundOutcome.COMPLETED
                        else logger.warning
                    )
                    log_round_outcome(
                        "smartpbx_media event=llm_round_outcome provider=claude "
                        "outcome=%s stop_reason=%s output_tokens=%s attempt=%d",
                        outcome.value,
                        _normalized_claude_stop_reason(stop_reason),
                        _bounded_claude_output_tokens(output_tokens),
                        _bounded_claude_attempt(attempt + 1),
                    )

                # THE decision point. The classified outcome -- not a re-check
                # of what happens to be in `text_content`/`tool_use_blocks` --
                # decides whether this round proceeds or is retried/recovered.
                if outcome is SmartPBXClaudeRoundOutcome.COMPLETED:
                    break
                if not smartpbx_direct:
                    # Twilio Media Streams (ar/si/ta) never had the retry or
                    # the shared recovery line; it proceeds with whatever the
                    # round produced exactly as before. Only the direct
                    # SmartPBX English path is outcome-driven.
                    break
                if outcome in SMARTPBX_CLAUDE_DISCARD_ROUND_OUTCOMES:
                    # Nothing this round produced may be acted on. Dropping the
                    # COMPLETE tool blocks too is deliberate: a round that also
                    # truncated is not a batch we may half-execute, and because
                    # nothing has executed yet the retry below is a fresh ask,
                    # not a replay of a committed side effect.
                    text_content = ""
                    tool_use_blocks = []
                    sentence_buffer = ""
                    # Same atomic ordering the stall path uses (filler first so
                    # its in-flight TTS cannot still hold `_speak_lock`, then
                    # cancel/await this round's per-sentence tasks, one
                    # generation advance, runner context, then speak): a
                    # preamble sentence may already be streaming when the tool
                    # block truncated, and neither the retry nor the recovery
                    # line may talk over it or leave it half delivered.
                    await self._smartpbx_cancel_stalled_filler(gen)
                    gen = await self._smartpbx_fence_stalled_generation(
                        tts_tasks=tts_tasks, gen=gen
                    )
                    tts_tasks = []
                    # The filler is retired; do not let a later delta in this
                    # round's retry poke a controller this transition already
                    # finished.
                    initial_filler = None
                # Only retry when there IS a next attempt to take (i.e. this
                # turn had not yet started a tool when max_attempts was
                # computed) and the one retry has not already been spent.
                if attempt + 1 < max_attempts and not empty_retry_used:
                    empty_retry_used = True
                    if self._is_smartpbx_session():
                        logger.warning(
                            "smartpbx_media event=llm_empty_response provider=claude "
                            "attempt=1 retrying=true"
                        )
                    continue
                if self._is_smartpbx_session():
                    logger.warning(
                        "smartpbx_media event=llm_empty_response provider=claude "
                        "attempt=%d retrying=false",
                        _bounded_claude_attempt(attempt + 1),
                    )
                return await self._smartpbx_speak_recovery_and_finish(
                    tool_executed=tool_executed, gen=gen, full_text=full_text,
                )

            if self._is_direct_smartpbx_english():
                full_text = _join_turn(full_text, text_content)
            else:
                full_text += text_content

            if tool_use_blocks:
                if self._is_smartpbx_session():
                    logger.info("smartpbx_media event=tool_batch count=%d", len(tool_use_blocks))
                else:
                    logger.info("Tools [%s]: %s", self.call_sid, [t["name"] for t in tool_use_blocks])
                tool_filler_task: asyncio.Task | None = None
                first_tool = tool_use_blocks[0]["name"]
                transfer_in_batch = (
                    self._is_direct_smartpbx_english()
                    and any(tool["name"] == "transfer_to_human" for tool in tool_use_blocks)
                )
                if tts_tasks:
                    if transfer_in_batch:
                        # Let a just-created model sentence enter its owned TTS
                        # lifecycle before the actual transfer boundary fences it.
                        await asyncio.sleep(0)
                    else:
                        await asyncio.gather(*tts_tasks)
                if self._is_direct_smartpbx_english():
                    preamble = sentence_buffer.strip()
                    if preamble and not tts_tasks and first_tool != "transfer_to_human":
                        await self._invoke_speak(preamble, generation=gen, sentence=preamble)
                    elif (
                        first_tool != "transfer_to_human"
                        and first_tool not in _SMARTPBX_CAPTURE_TOOLS
                        and not smartpbx_filler_sent
                        and not text_content.strip()
                        and not (
                            initial_filler is not None
                            and initial_filler.suppress_specialized_tool_filler
                        )
                    ):
                        filler_lease = self._reserve_smartpbx_tool_filler(first_tool)
                        tool_filler_task = self._start_smartpbx_tool_filler(
                            filler_lease.text, generation=gen, lease=filler_lease,
                        )
                        await asyncio.sleep(0)
                        smartpbx_filler_sent = True
                else:
                    filler = fillers.get(first_tool, fillers.get("_default", ""))
                    if filler:
                        await self._invoke_speak(filler, generation=gen, sentence=filler)

                # Build assistant message with content blocks
                assistant_content: list[dict[str, Any]] = []
                if text_content:
                    text_block = self._assistant_turn_text_for_history(text_content)
                    assistant_content.append({"type": "text", "text": text_block})
                for tb in tool_use_blocks:
                    assistant_content.append({
                        "type": "tool_use",
                        "id": tb["id"],
                        "name": tb["name"],
                        "input": tb["input"],
                    })
                # Keep the Anthropic request and all tool results local until
                # every awaited effect has completed under the same ownership.
                staged_results: list[
                    tuple[int, dict[str, Any], Any, str, BaseException | None]
                ] = []
                for tool_index, tb in enumerate(tool_use_blocks):
                    if not self._current_smartpbx_runner_can_execute_tools():
                        return ""
                    if (
                        tb["name"] == "transfer_to_human"
                        and self._is_direct_smartpbx_english()
                    ):
                        prepared_generation = await self._prepare_smartpbx_transfer_handoff(
                            tts_tasks=tts_tasks, initial_filler=initial_filler,
                            tool_filler_task=tool_filler_task,
                            generation=gen,
                        )
                        tool_filler_task = None
                        if prepared_generation is None:
                            return ""
                        gen = prepared_generation
                    # The capture tools must parse what the caller actually SAID,
                    # not the model's paraphrase of it — and on this path the raw
                    # utterance is the whole combined dictation. This is the
                    # production runner (LLM_PROVIDER defaults to claude); without
                    # this override the fragment combining upstream would be
                    # thrown away here. Same call as the OpenAI/Gemini media and
                    # ConversationRelay paths.
                    tb["input"], _ = _override_capture_spoken_argument(
                        tool_name=tb["name"],
                        tool_input=tb["input"],
                        override_spoken=self._smartpbx_runner_raw_utterance(),
                        source="smartpbx_media",
                    )
                    self._log_tool_execution(
                        tb["name"], tb["input"], capture_slots=False,
                    )
                    tool_error: BaseException | None = None
                    # Set BEFORE the await: a tool that raises half-way may
                    # already have had its effect, so this turn is no longer
                    # replayable — the shared empty-response policy must never
                    # retry it, only recover with the post-tool-start line.
                    tool_executed = True
                    try:
                        if tb["name"] == "collect_number_via_keypad":
                            result_str = await self._collect_number_via_keypad(tb["input"])
                        else:
                            result_str = await execute_tool(tb["name"], tb["input"])
                    except asyncio.CancelledError:
                        await self._cancel_smartpbx_round_tts(tts_tasks)
                        await self._finish_smartpbx_tool_filler(
                            tool_filler_task, cancel=True
                        )
                        raise
                    except Exception as exc:
                        tool_error = exc
                        if self._is_direct_smartpbx_english():
                            assistant_content[tool_index + (1 if text_content else 0)]["input"] = {}
                            result_str = json.dumps({"error": "tool_execution_failed"})
                        else:
                            result_str = json.dumps({"error": str(exc)})
                    if not self._current_smartpbx_runner_owns_shared_state(
                        tool_executed=tool_executed
                    ):
                        await self._cancel_smartpbx_round_tts(tts_tasks)
                        await self._finish_smartpbx_tool_filler(
                            tool_filler_task, cancel=True
                        )
                        return ""
                    if (
                        tb["name"] == "transfer_to_human"
                        and not self.transfer_pending
                    ):
                        self._smartpbx_transfer_audio_fenced = False
                    staged_results.append(
                        (tool_index, tb, tb["input"], result_str, tool_error)
                    )
                    if self.transfer_pending:
                        break

                await self._finish_smartpbx_tool_filler(
                    tool_filler_task, cancel=self.transfer_pending
                )
                if initial_filler is not None and first_tool != "transfer_to_human":
                    await initial_filler.wait()
                if not self._current_smartpbx_runner_owns_shared_state(
                    tool_executed=tool_executed
                ):
                    return ""
                if len(staged_results) != len(tool_use_blocks):
                    assistant_content = assistant_content[:len(staged_results) + (1 if text_content else 0)]
                tool_results: list[dict[str, Any]] = []
                for _tool_index, tb, _tool_input, result_str, _tool_error in staged_results:
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tb["id"],
                        "content": result_str,
                    })
                self._append_assistant_history({
                    "role": "assistant",
                    "content": assistant_content,
                })
                self.history.append({"role": "user", "content": tool_results})
                for _tool_index, tb, tool_input, result_str, tool_error in staged_results:
                    try:
                        parsed_result = json.loads(result_str)
                    except (json.JSONDecodeError, TypeError):
                        parsed_result = None
                    if isinstance(parsed_result, dict):
                        self._record_capture_tool_completion(tb["name"], parsed_result)
                    _append_booking_confirmation_marker(
                        self.full_transcript,
                        tb["name"],
                        tool_input,
                        result_str,
                    )
                    self._capture_booking_slots(tb["name"], tool_input)
                    if tool_error is not None:
                        self._log_tool_failure(tb["name"], tool_error)
                    self._log_tool_result(tb["name"], result_str)
                if self.transfer_pending:
                    return full_text
                confirmation = self._direct_smartpbx_captured_number_confirmation(
                    tool_use_blocks, staged_results,
                )
                if confirmation is not None:
                    # The normal tool boundary above has already drained the
                    # initial/tool fillers and committed the native Anthropic
                    # request/result messages.  A valid deterministic parser
                    # result therefore needs no second Claude stream merely to
                    # read its own digits back.
                    confirmation_task = self._start_smartpbx_round_tts(
                        confirmation, generation=gen, sentence=confirmation,
                    )
                    if confirmation_task is None:
                        return ""
                    await confirmation_task
                    if not self._current_smartpbx_runner_owns_shared_state(
                        tool_executed=tool_executed
                    ):
                        return ""
                    self._append_assistant_history({
                        "role": "assistant",
                        "content": self._assistant_turn_text_for_history(confirmation),
                    })
                    return _join_turn(full_text, confirmation)
                continue

            # No tools — flush remaining sentence buffer
            remaining = sentence_buffer.strip()
            if remaining:
                task = self._start_smartpbx_round_tts(
                    remaining, generation=gen, sentence=remaining,
                )
                if task is not None:
                    tts_tasks.append(task)
            if tts_tasks:
                await asyncio.gather(*tts_tasks)

            if not self._current_smartpbx_runner_owns_shared_state():
                return ""
            if text_content:
                self._append_assistant_history({
                    "role": "assistant",
                    "content": self._assistant_turn_text_for_history(text_content),
                })
            return full_text

        if self._is_smartpbx_session():
            logger.warning("smartpbx_media event=tool_round_limit provider=claude")
        else:
            logger.warning("Exhausted %d tool rounds (Claude) [%s]", MAX_TOOL_ROUNDS, self.call_sid)
        return full_text

    # â”€â”€ TTS â†’ Twilio mulaw audio â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    async def _speak(self, text: str, generation: int = -1, sentence: str | None = None):
        """Route text to appropriate TTS provider.

        English        â†’ protected canonical ElevenLabs eleven_flash_v2_5 profile
        Tamil / Arabic â†’ retained ElevenLabs eleven_multilingual_v2 voices
        SmartPBX Sinhala â†’ Gemini gemini-3.1-flash-tts-preview
        Twilio Sinhala   â†’ existing OpenAI gpt-4o-mini-tts (nova)
        """
        if self.transfer_pending:
            return
        async with self._speak_lock:
            if self.transfer_pending:
                return
            if generation >= 0 and generation != self._speak_generation:
                return
            if sentence is not None and self._track_assistant_turn_delivery:
                self._record_generated_sentence(sentence)
            if self._is_smartpbx_session():
                _log_smartpbx_pilot_transcript("kavya", text)
            if self.lang in ("en", "ta", "ar"):
                await self._invoke_tts(
                    self._tts_elevenlabs,
                    text,
                    sentence=sentence,
                    turn_generation=generation,
                )
            elif self.lang == "si" and self._is_smartpbx_session():
                await self._invoke_tts(
                    self._tts_gemini_sinhala,
                    text,
                    sentence=sentence,
                    turn_generation=generation,
                )
            elif self.lang == "si":
                await self._invoke_tts(
                    self._tts_openai,
                    text,
                    sentence=sentence,
                    turn_generation=generation,
                )
            else:
                lang_code, voice_name = AZURE_VOICES[self.lang]
                await self._invoke_tts(
                    lambda txt, sentence=sentence, turn_generation=generation: self._tts_azure(
                        txt, lang_code, voice_name, sentence=sentence, turn_generation=turn_generation
                    ),
                    text,
                    sentence=sentence,
                    turn_generation=generation,
                )

    # â”€â”€ ElevenLabs TTS (Tamil) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    async def _tts_gemini_sinhala(
        self,
        text: str,
        *,
        sentence: str | None = None,
        turn_generation: int | None = None,
    ) -> None:
        """Stream Gemini 24 kHz PCM to SmartPBX as 8 kHz mu-law frames only."""
        text = text.strip()
        if not text:
            return
        if not GEMINI_API_KEY:
            self._log_tts_failure("gemini", "missing_api_key")
            self._emit_smartpbx_tts_diagnostic(
                DiagnosticFailureClass.TTS_MISSING_API_KEY
            )
            return

        expected_generation = self._speak_generation
        if turn_generation is not None and turn_generation >= 0:
            if turn_generation != expected_generation:
                return

        self._is_speaking = True
        self._speaking_since = time.monotonic()
        self._mark_smartpbx_turn_once("tts_request")
        ratecv_state = None
        pcm_tail = b""
        mulaw_buf = b""
        audio_emitted = False
        cancelled = False

        try:
            client = self._gemini_tts_client
            if client is None:
                client = _get_gemini_tts_client()
                self._gemini_tts_client = client
            stream = await client.aio.interactions.create(
                model=SMARTPBX_SINHALA_GEMINI_TTS_MODEL,
                input=text,
                stream=True,
                response_modalities=["audio"],
                response_mime_type="audio/l16",
                response_format={
                    "type": "audio",
                    "mime_type": "audio/l16",
                    "sample_rate": 24000,
                    "delivery": "inline",
                },
                generation_config={
                    "speech_config": [{
                        "voice": SMARTPBX_SINHALA_GEMINI_TTS_VOICE,
                    }],
                },
                timeout=SMARTPBX_SINHALA_GEMINI_TTS_TIMEOUT_SECONDS,
            )

            async for audio_b64, audio_delta in _iter_gemini_tts_audio_deltas(stream):
                if (
                    not self._is_speaking
                    or self._speak_generation != expected_generation
                    or not self._owns_smartpbx_tts_delivery(expected_generation)
                    or (
                        turn_generation is not None
                        and turn_generation >= 0
                        and turn_generation != self._speak_generation
                    )
                ):
                    cancelled = True
                    break
                if (
                    (
                        getattr(audio_delta, "mime_type", None) is not None
                        and getattr(audio_delta, "mime_type") != "audio/l16"
                    )
                    or (
                        getattr(audio_delta, "channels", None) is not None
                        and getattr(audio_delta, "channels") != 1
                    )
                    or (
                        getattr(audio_delta, "sample_rate", None) is not None
                        and getattr(audio_delta, "sample_rate") != 24000
                    )
                ):
                    self._log_tts_failure("gemini", "invalid_audio_metadata")
                    self._emit_smartpbx_tts_diagnostic(
                        DiagnosticFailureClass.TTS_EXCEPTION
                    )
                    return
                try:
                    chunk = base64.b64decode(audio_b64, validate=True)
                except (binascii.Error, ValueError, TypeError):
                    self._log_tts_failure("gemini", "malformed_audio")
                    self._emit_smartpbx_tts_diagnostic(
                        DiagnosticFailureClass.TTS_EXCEPTION
                    )
                    return
                if not chunk:
                    continue

                data = pcm_tail + chunk
                if len(data) % 2:
                    data, pcm_tail = data[:-1], data[-1:]
                else:
                    pcm_tail = b""
                if not data:
                    continue
                pcm8k, ratecv_state = audioop.ratecv(
                    data, 2, 1, 24000, 8000, ratecv_state
                )
                mulaw_buf += audioop.lin2ulaw(pcm8k, 2)

                while len(mulaw_buf) >= 640:
                    if (
                        not self._is_speaking
                        or self._speak_generation != expected_generation
                        or not self._owns_smartpbx_tts_delivery(expected_generation)
                    ):
                        cancelled = True
                        break
                    frame, mulaw_buf = mulaw_buf[:640], mulaw_buf[640:]
                    self._mark_smartpbx_turn_once("tts_first_chunk")
                    await self._send_media_audio(frame)
                    audio_emitted = True
                if cancelled:
                    break

            if pcm_tail and not cancelled:
                self._log_tts_failure("gemini", "malformed_audio")
                self._emit_smartpbx_tts_diagnostic(
                    DiagnosticFailureClass.TTS_EXCEPTION
                )
                return

            if (
                not cancelled
                and self._is_speaking
                and self._speak_generation == expected_generation
                and self._owns_smartpbx_tts_delivery(expected_generation)
                and mulaw_buf
            ):
                mulaw_buf += b"\xff" * (640 - len(mulaw_buf))
                self._mark_smartpbx_turn_once("tts_first_chunk")
                await self._send_media_audio(mulaw_buf)
                audio_emitted = True
            elif not cancelled and not self._owns_smartpbx_tts_delivery(
                expected_generation
            ):
                cancelled = True

            if (
                audio_emitted
                and not cancelled
                and self._is_speaking
                and self._speak_generation == expected_generation
                and self._owns_smartpbx_tts_delivery(expected_generation)
            ):
                await self._send_tts_done(
                    sentence=sentence,
                    turn_generation=expected_generation,
                )
            elif not audio_emitted and not cancelled:
                self._log_tts_failure("gemini", "empty_audio")
                self._emit_smartpbx_tts_diagnostic(
                    DiagnosticFailureClass.TTS_EXCEPTION
                )

        except httpx.HTTPStatusError as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if not isinstance(status, int) or isinstance(status, bool):
                status = None
            elif status < 100 or status > 599:
                status = max(100, min(status, 599))
            self._log_tts_failure("gemini", "http_status", status)
            self._emit_smartpbx_tts_diagnostic(
                DiagnosticFailureClass.TTS_HTTP_STATUS
            )
        except TimeoutError:
            self._log_tts_failure("gemini", "timeout")
            self._emit_smartpbx_tts_diagnostic(
                DiagnosticFailureClass.TTS_TIMEOUT
            )
        except Exception:
            self._log_tts_failure("gemini", "exception")
            self._emit_smartpbx_tts_diagnostic(
                DiagnosticFailureClass.TTS_EXCEPTION
            )
        finally:
            self._is_speaking = False

    async def _tts_elevenlabs(
        self,
        text: str,
        *,
        sentence: str | None = None,
        turn_generation: int | None = None,
    ):
        """Stream ElevenLabs TTS as 8 kHz mu-law through the active transport."""
        if not ELEVENLABS_API_KEY:
            self._emit_smartpbx_tts_diagnostic(DiagnosticFailureClass.TTS_MISSING_API_KEY)
            logger.warning("ElevenLabs API key not configured — skipping TTS")
            return
        if self.lang == "en":
            try:
                profile = load_kavya_english_voice_profile()
            except ValueError:
                self._emit_smartpbx_tts_diagnostic(DiagnosticFailureClass.TTS_PROFILE_FAILURE)
                logger.warning("Canonical Kavya English voice is not configured — skipping TTS")
                return
            voice_id = profile.voice_id
            model_id = profile.model_id
            voice_settings = profile.request_voice_settings
        else:
            if not ELEVENLABS_VOICE_ID:
                logger.warning("ElevenLabs general voice not configured — skipping retained-language TTS")
                return
            voice_id = (ELEVENLABS_VOICE_ID_AR or ELEVENLABS_VOICE_ID) if self.lang == "ar" else ELEVENLABS_VOICE_ID
            model_id = ELEVENLABS_MODEL_MULTILINGUAL
            voice_settings = {"stability": 0.5, "similarity_boost": 0.75, "style": 0.0, "use_speaker_boost": True}
        self._is_speaking = True
        self._mark_smartpbx_turn_once("tts_request")
        self._speaking_since = time.monotonic()
        url = _elevenlabs_stream_url(voice_id)
        headers = {"xi-api-key": ELEVENLABS_API_KEY, "Content-Type": "application/json"}
        payload: dict[str, Any] = {"text": text, "model_id": model_id, "voice_settings": voice_settings}

        if self._consume_smartpbx_welcome_audio_marker(text):
            cache_key = _smartpbx_welcome_audio_cache_key(
                text, url, model_id, voice_settings,
            )

            async def fetch_welcome_audio() -> bytes:
                async with httpx.AsyncClient() as http:
                    async with http.stream(
                        "POST", url, json=payload, headers=headers, timeout=15.0,
                    ) as resp:
                        if resp.status_code != 200:
                            await resp.aread()
                            self._log_tts_failure("elevenlabs", "http_status", resp.status_code)
                            self._emit_smartpbx_tts_diagnostic(
                                DiagnosticFailureClass.TTS_HTTP_STATUS
                            )
                            raise _ElevenLabsWelcomeHTTPStatus()
                        chunks: list[bytes] = []
                        async for chunk in resp.aiter_bytes(chunk_size=640):
                            if chunk:
                                chunks.append(chunk)
                        return b"".join(chunks)

            try:
                audio = await _get_cached_smartpbx_welcome_audio(cache_key, fetch_welcome_audio)
                if not self._is_speaking:
                    logger.info("smartpbx_media event=tts_interrupted provider=elevenlabs")
                    return
                self._mark_smartpbx_turn_once("tts_first_chunk")
                await self._send_media_audio(audio)
                if self._is_speaking:
                    await self._send_tts_done(
                        sentence=sentence,
                        turn_generation=turn_generation,
                    )
                else:
                    logger.info("smartpbx_media event=tts_interrupted provider=elevenlabs")
            except _ElevenLabsWelcomeHTTPStatus:
                self._is_speaking = False
            except _ElevenLabsWelcomeEmptyAudio:
                self._log_tts_failure("elevenlabs", "empty_audio")
                self._emit_smartpbx_tts_diagnostic(DiagnosticFailureClass.TTS_EXCEPTION)
                self._is_speaking = False
            except httpx.TimeoutException:
                self._log_tts_failure("elevenlabs", "timeout")
                self._emit_smartpbx_tts_diagnostic(DiagnosticFailureClass.TTS_TIMEOUT)
                self._is_speaking = False
            except Exception:
                self._log_tts_failure("elevenlabs", "exception")
                self._emit_smartpbx_tts_diagnostic(DiagnosticFailureClass.TTS_EXCEPTION)
                self._is_speaking = False
            return

        try:
            async with httpx.AsyncClient() as http:
                async with http.stream(
                    "POST", url, json=payload, headers=headers, timeout=15.0,
                ) as resp:
                    if resp.status_code != 200:
                        body = await resp.aread()
                        if self._is_smartpbx_session():
                            self._log_tts_failure("elevenlabs", "http_status", resp.status_code)
                            self._emit_smartpbx_tts_diagnostic(DiagnosticFailureClass.TTS_HTTP_STATUS)
                        else:
                            logger.error("ElevenLabs %d: %s", resp.status_code, body[:200])
                        self._is_speaking = False
                        return

                    async for chunk in resp.aiter_bytes(chunk_size=640):
                        if not self._is_speaking:
                            break
                        if chunk:
                            self._mark_smartpbx_turn_once("tts_first_chunk")
                        await self._send_media_audio(chunk)

            if self._is_speaking:
                await self._send_tts_done(
                    sentence=sentence,
                    turn_generation=turn_generation,
                )
            else:
                if self._is_smartpbx_session():
                    logger.info("smartpbx_media event=tts_interrupted provider=elevenlabs")
                else:
                    logger.info("ElevenLabs TTS interrupted by barge-in [%s]", self.call_sid)

        except httpx.TimeoutException:
            if self._is_smartpbx_session():
                self._log_tts_failure("elevenlabs", "timeout")
                self._emit_smartpbx_tts_diagnostic(DiagnosticFailureClass.TTS_TIMEOUT)
            else:
                logger.error("ElevenLabs timeout for: %s", text[:80])
            self._is_speaking = False
        except Exception:
            if self._is_smartpbx_session():
                self._log_tts_failure("elevenlabs", "exception")
                self._emit_smartpbx_tts_diagnostic(DiagnosticFailureClass.TTS_EXCEPTION)
            else:
                logger.exception("ElevenLabs TTS failed for: %s", text[:80])
            self._is_speaking = False

    # â”€â”€ Azure TTS (Sinhala) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    async def _tts_azure(
        self,
        text: str,
        lang_code: str,
        voice_name: str,
        *,
        sentence: str | None = None,
        turn_generation: int | None = None,
    ):
        """Stream Azure Cognitive Services TTS as mulaw 8 kHz to Twilio.
        Must only be called from _speak (lock already held).
        """
        if not AZURE_SPEECH_KEY:
            logger.warning("AZURE_SPEECH_KEY not set — skipping TTS")
            return

        self._is_speaking = True
        self._speaking_since = time.monotonic()
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
                        if self._is_smartpbx_session():
                            self._log_tts_failure("azure", "http_status", resp.status_code)
                        else:
                            logger.error("Azure TTS %d: %s", resp.status_code, body[:200])
                        self._is_speaking = False
                        return
                    async for chunk in resp.aiter_bytes(chunk_size=640):
                        if not self._is_speaking:
                            break
                        await self._send_media_audio(chunk)
            if self._is_speaking:
                await self._send_tts_done(
                    sentence=sentence,
                    turn_generation=turn_generation,
                )
            else:
                if self._is_smartpbx_session():
                    logger.info("smartpbx_media event=tts_interrupted provider=azure")
                else:
                    logger.info("Azure TTS interrupted by barge-in [%s]", self.call_sid)
        except httpx.TimeoutException:
            if self._is_smartpbx_session():
                self._log_tts_failure("azure", "timeout")
            else:
                logger.error("Azure TTS timeout for: %s", text[:80])
            self._is_speaking = False
        except Exception:
            if self._is_smartpbx_session():
                self._log_tts_failure("azure", "exception")
            else:
                logger.exception("Azure TTS failed for: %s", text[:80])
            self._is_speaking = False

    # â”€â”€ OpenAI TTS (Sinhala) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    async def _tts_openai(
        self,
        text: str,
        *,
        sentence: str | None = None,
        turn_generation: int | None = None,
    ):
        """Stream OpenAI gpt-4o-mini-tts as mulaw 8 kHz to Twilio (Sinhala).

        OpenAI returns raw 24 kHz 16-bit mono PCM; we downsample to 8 kHz and
        mulaw-encode on the fly so it drops straight into the same Twilio media
        framing the Tamil/Azure paths use.
        Must only be called from _speak (lock already held).
        """
        text = text.strip()
        if not OPENAI_API_KEY:
            logger.warning("OPENAI_API_KEY not set — skipping TTS")
            return
        if not text:
            return

        self._is_speaking = True
        self._speaking_since = time.monotonic()
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
                        if self._is_smartpbx_session():
                            self._log_tts_failure("openai", "http_status", resp.status_code)
                        else:
                            logger.error("OpenAI TTS %d: %s", resp.status_code, body[:200])
                        self._is_speaking = False
                        return

                    async for chunk in resp.aiter_bytes(chunk_size=4800):
                        if not self._is_speaking:
                            break
                        if not chunk:
                            continue
                        # PCM is 2 bytes/sample — keep sample alignment.
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
                            await self._send_media_audio(frame)

            # Flush any remaining tail of mulaw audio.
            if self._is_speaking and mulaw_buf:
                await self._send_media_audio(mulaw_buf)

            if self._is_speaking:
                await self._send_tts_done(
                    sentence=sentence,
                    turn_generation=turn_generation,
                )
            else:
                if self._is_smartpbx_session():
                    logger.info("smartpbx_media event=tts_interrupted provider=openai")
                else:
                    logger.info("OpenAI TTS interrupted by barge-in [%s]", self.call_sid)

        except httpx.TimeoutException:
            if self._is_smartpbx_session():
                self._log_tts_failure("openai", "timeout")
            else:
                logger.error("OpenAI TTS timeout for: %s", text[:80])
            self._is_speaking = False
        except Exception:
            if self._is_smartpbx_session():
                self._log_tts_failure("openai", "exception")
            else:
                logger.exception("OpenAI TTS failed for: %s", text[:80])
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
    transcript_sink: list[dict[str, str]] | None = None,
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
        full_response_text = _join_turn(full_response_text, text_content)

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
                parsed_input, _ = _override_capture_spoken_argument(
                    tool_name=tc["name"],
                    tool_input=parsed_input,
                    override_spoken=_extract_last_user_utterance(conversation_history),
                    source="conversation_relay",
                )
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
                _append_booking_confirmation_marker(
                    transcript_sink or [],
                    tc["name"],
                    parsed_input,
                    result_str,
                )
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
    lang: str = "en",
    generation_ref: list[int] | None = None,
    transcript_sink: list[dict[str, str]] | None = None,
    failover_state: dict[str, Any] | None = None,
) -> str:
    """Stream a Gemini response via the native SDK, handling tool use.

    Uses the same history format (OpenAI) internally, converting to Gemini
    format for each API call.  Tool results are appended in OpenAI format
    so _trim_history works unchanged.

    Parity with the Claude runner: tokens go out as they arrive behind the same
    barge-in generation fence and closed-socket guard, the slow-response filler
    covers a stalled first token, and a turn that streams nothing at all is
    retried once before falling back to a spoken apology rather than silence.
    """
    if failover_state is None:
        failover_state = _init_gemini_failover_state()

    if failover_state.get("degraded") and ANTHROPIC_API_KEY:
        logger.info("smartpbx_media event=llm_provider_failover from=gemini to=claude reason=sticky")
        return await _run_llm_streaming_claude(
            client=_get_anthropic_client(),
            system=system,
            conversation_history=conversation_history,
            # Same tools, Anthropic shape — Gemini's function_declarations payload
            # would 400. Converting keeps the caller's restriction intact (the
            # failsafe session must still offer ONLY notify_human_handover).
            tools=_claude_tools_from_gemini(tools),
            websocket=websocket,
            lang=lang,
            generation_ref=generation_ref,
            transcript_sink=transcript_sink,
            model=CLAUDE_MODEL,
        )

    conversation_history_len = len(conversation_history)
    # Threaded out of the _run_gemini_stream closure so the enclosing except
    # handler (below) can gate history-truncation + Claude replay on whether
    # a tool already executed this turn — a genuine exception in a later
    # round must not truncate history and replay a turn that already
    # committed a tool side effect (create_booking). Mirrors the
    # `replayable` gate _run_gemini_stream already applies to the
    # empty-response case.
    turn_tool_executed = False
    turn_full_text = ""
    turn_generation = generation_ref[0] if generation_ref else None

    async def _run_gemini_stream() -> str:
        nonlocal turn_tool_executed, turn_full_text, turn_generation
        full_response_text = ""
        filler_sent = False
        ws_closed = False
        empty_retry_used = False
        # Any tool that has STARTED executing makes this turn unreplayable — a
        # Claude re-run would repeat its side effects (create_booking).
        tool_executed = False
        generation = generation_ref[0] if generation_ref else None
        turn_generation = generation

        def _is_stale_generation() -> bool:
            return (
                generation_ref is not None
                and generation is not None
                and generation != generation_ref[0]
            )

        async def _safe_send(payload: dict) -> None:
            nonlocal ws_closed
            if _is_stale_generation():
                return
            if ws_closed:
                return
            try:
                await websocket.send_text(json.dumps(payload))
            except (WebSocketDisconnect, RuntimeError) as exc:
                ws_closed = True
                logger.warning(
                    "WebSocket closed mid-stream (%s) — draining Gemini silently",
                    type(exc).__name__,
                )

        for round_idx in range(MAX_TOOL_ROUNDS):
            logger.info("Gemini streaming round %d", round_idx + 1)

            text_content = ""
            function_calls: list[dict] = []  # [{name, args, thought_signature}, ...]
            finish_reason = None
            nudge: str | None = None

            # Attempt 0 is the real turn; attempt 1 only runs when attempt 0 streamed
            # nothing at all and this turn has not already spent its one retry.
            for attempt in range(2):
                text_content = ""
                function_calls = []
                finish_reason = None

                # Cover a stalled first token (thinking, a 429 retry inside the SDK,
                # a slow tool-heavy prompt) the same way the Claude path does.
                slow_task: asyncio.Task | None = None
                if not ws_closed:
                    slow_task = asyncio.create_task(_slow_response_filler(websocket, lang))

                def _cancel_slow(task: asyncio.Task | None = slow_task) -> None:
                    if task and not task.done():
                        task.cancel()

                try:
                    response = await _open_gemini_stream(
                        gemini_client,
                        model=MODEL,
                        contents=_history_to_gemini(conversation_history),
                        config=_build_gemini_config(
                            system=system, tools=tools, model=MODEL, nudge=nudge
                        ),
                    )

                    async for kind, payload in _iter_gemini_stream(response):
                        if kind == "finish":
                            finish_reason = payload
                            continue
                        if kind == "tool":
                            _cancel_slow()
                            function_calls.append(payload)
                            continue
                        _cancel_slow()
                        text_content += payload
                        await _safe_send({"type": "text", "token": payload})
                finally:
                    _cancel_slow()

                if text_content.strip() or function_calls:
                    break

                retrying = attempt == 0 and not empty_retry_used
                _log_gemini_empty(
                    path="conversation_relay",
                    attempt=attempt + 1,
                    finish_reason=finish_reason,
                    retrying=retrying,
                )
                if not retrying:
                    break
                empty_retry_used = True
                nudge = GEMINI_EMPTY_RETRY_NUDGE

            logger.info(
                "Gemini round %d done — text=%d chars, tools=%d, finish=%s",
                round_idx + 1, len(text_content), len(function_calls), finish_reason,
            )

            full_response_text = _join_turn(full_response_text, text_content)
            turn_full_text = full_response_text

            # Nothing survived the retry. Two empty turns in a row is a provider
            # failure, not a short answer, so hand the turn to Claude the same way
            # a quota error is handed over. Only when that is unavailable do we
            # speak a recoverable line: Twilio renders only what it is sent, so
            # without one the caller gets dead air and concludes the line is broken.
            if not text_content.strip() and not function_calls:
                # Failover REPLAYS the turn from the same history, so it is only
                # safe while nothing has been sent to the caller and no tool has
                # run. An empty LATER round takes the canned line instead — a
                # replay would re-execute create_booking.
                replayable = (
                    round_idx == 0
                    and not full_response_text.strip()
                    and not tool_executed
                    and not filler_sent
                )
                if replayable and _gemini_relay_failover_ready():
                    raise _GeminiEmptyTurnError()
                if not replayable:
                    logger.warning(
                        "gemini_diagnostic event=empty_response_not_replayable "
                        "path=conversation_relay round=%d tools_executed=%s",
                        round_idx + 1, str(tool_executed).lower(),
                    )
                fallback = LLM_EMPTY_FALLBACKS.get(lang, LLM_EMPTY_FALLBACKS["en"])
                logger.warning(
                    "gemini_diagnostic event=empty_response_fallback "
                    "path=conversation_relay lang=%s", lang,
                )
                await _safe_send({"type": "text", "token": fallback, "last": True})
                conversation_history.append({"role": "assistant", "content": fallback})
                full_response_text = _join_turn(full_response_text, fallback)
                if ws_closed:
                    raise WebSocketDisconnect()
                return full_response_text

            if function_calls:
                logger.info(
                    "Gemini requested %d tool(s): %s",
                    len(function_calls),
                    [fc["name"] for fc in function_calls],
                )

                # Only play the canned filler when Gemini produced NO pre-tool text
                # of its own — a second canned line would duplicate its own
                # announcement back to back. Capture tools are instant and local, so
                # they get no filler at all; the tools still run either way.
                if not filler_sent and not text_content.strip():
                    first_tool_name = function_calls[0]["name"]
                    if first_tool_name in _CONVERSATION_CLAUDE_CAPTURE_TOOLS:
                        filler = ""
                    elif first_tool_name in TOOL_FILLER_VARIANTS:
                        filler = _next_tool_filler(first_tool_name)
                    else:
                        filler = TOOL_FILLERS.get(first_tool_name, DEFAULT_FILLER)
                    if filler:
                        await _safe_send({"type": "text", "token": filler, "last": True})
                        logger.info("Sent filler: '%s'", filler)
                        filler_sent = True

                # Build assistant message in OpenAI format (for history storage)
                tool_calls_openai = []
                for i, fc in enumerate(function_calls):
                    tc_id = f"gemini_tc_{round_idx}_{i}"
                    entry: dict[str, Any] = {
                        "id": tc_id,
                        "type": "function",
                        "function": {
                            "name": fc["name"],
                            "arguments": json.dumps(fc["args"]),
                        },
                    }
                    if fc.get("thought_signature"):
                        entry["gemini_thought_signature"] = fc["thought_signature"]
                    tool_calls_openai.append(entry)

                assistant_msg: dict[str, Any] = {
                    "role": "assistant",
                    "content": text_content or None,
                    "tool_calls": tool_calls_openai,
                }
                conversation_history.append(assistant_msg)

                # Execute tools and add results (OpenAI format)
                for tc in tool_calls_openai:
                    try:
                        parsed_input = json.loads(tc["function"]["arguments"]) if tc["function"]["arguments"] else {}
                    except json.JSONDecodeError:
                        logger.error(
                            "Bad tool JSON for %s: %s",
                            tc["function"]["name"], tc["function"]["arguments"][:200],
                        )
                        parsed_input = {}
                    parsed_input, _ = _override_capture_spoken_argument(
                        tool_name=tc["function"]["name"],
                        tool_input=parsed_input,
                        override_spoken=_extract_last_user_utterance(conversation_history),
                        source="conversation_relay",
                    )
                    logger.info("Executing tool '%s' with input: %s", tc["function"]["name"], parsed_input)
                    # Set BEFORE the await: a tool that raises half-way may already
                    # have had its effect, so this turn can no longer be replayed.
                    tool_executed = True
                    turn_tool_executed = True
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
                    _append_booking_confirmation_marker(
                        transcript_sink or [],
                        tc["function"]["name"],
                        parsed_input,
                        result_str,
                    )
                    logger.info("Tool '%s' result: %s", tc["function"]["name"], result_str[:200])

                text_content = ""
                continue

            # No tool calls — done
            await _safe_send({"type": "text", "token": "", "last": True})

            if text_content:
                conversation_history.append({
                    "role": "assistant",
                    "content": text_content,
                })

            logger.info(
                "Gemini response complete (%d chars)%s",
                len(full_response_text),
                " [WS closed]" if ws_closed else "",
            )
            if ws_closed:
                raise WebSocketDisconnect()
            return full_response_text

        # Exhausted all tool rounds
        logger.warning("Exhausted %d tool rounds (Gemini)", MAX_TOOL_ROUNDS)
        await _safe_send({"type": "text", "token": "", "last": True})
        if full_response_text:
            conversation_history.append({
                "role": "assistant",
                "content": full_response_text,
            })
        if ws_closed:
            raise WebSocketDisconnect()
        return full_response_text

    try:
        response_text = await _run_gemini_stream()
        _note_gemini_success(failover_state)
        return response_text
    except Exception as exc:
        reason = (
            "empty_response" if isinstance(exc, _GeminiEmptyTurnError)
            else _classify_gemini_exception(exc)
        )

        if turn_tool_executed:
            # A tool already executed this turn (e.g. create_booking
            # committed). Truncating history and replaying via Claude would
            # duplicate that side effect, so failover is skipped entirely
            # here — same gate the empty-response `replayable` check already
            # applies inside _run_gemini_stream. This runner has no direct-
            # SmartPBX path (that's the Media Streams runner), so it always
            # takes the existing per-language canned fallback.
            logger.warning(
                "gemini_diagnostic event=exception_not_replayable "
                "path=conversation_relay tool_executed=true reason=%s",
                reason,
            )
            stale = (
                generation_ref is not None
                and turn_generation is not None
                and turn_generation != generation_ref[0]
            )
            fallback = LLM_EMPTY_FALLBACKS.get(lang, LLM_EMPTY_FALLBACKS["en"])
            if not stale:
                try:
                    await websocket.send_text(
                        json.dumps({"type": "text", "token": fallback, "last": True})
                    )
                except (WebSocketDisconnect, RuntimeError):
                    pass
                conversation_history.append({"role": "assistant", "content": fallback})
            return _join_turn(turn_full_text, fallback)

        if len(conversation_history) > conversation_history_len:
            conversation_history[:] = conversation_history[:conversation_history_len]

        if not GEMINI_FAILOVER_TO_CLAUDE:
            raise

        if not ANTHROPIC_API_KEY:
            raise exc

        logger.warning(
            "smartpbx_media event=llm_provider_failover from=gemini to=claude reason=%s",
            reason,
        )

        try:
            claude_client = _get_anthropic_client()
        except RuntimeError:
            raise

        _note_gemini_failover(failover_state)
        return await _run_llm_streaming_claude(
            client=claude_client,
            system=system,
            conversation_history=conversation_history,
            # Anthropic shape, same membership — see _claude_tools_from_gemini.
            tools=_claude_tools_from_gemini(tools),
            websocket=websocket,
            lang=lang,
            generation_ref=generation_ref,
            transcript_sink=transcript_sink,
            model=CLAUDE_MODEL,
        )


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
    generation_ref: list[int] | None = None,
    transcript_sink: list[dict[str, str]] | None = None,
    model: str = MODEL,
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
    generation = generation_ref[0] if generation_ref else None

    def _is_stale_generation() -> bool:
        return (
            generation_ref is not None
            and generation is not None
            and generation != generation_ref[0]
        )

    async def _safe_send(payload: dict) -> None:
        nonlocal ws_closed
        if _is_stale_generation():
            return
        if ws_closed:
            return
        try:
            await websocket.send_text(json.dumps(payload))
        except (WebSocketDisconnect, RuntimeError) as exc:
            ws_closed = True
            logger.warning(
                "WebSocket closed mid-stream (%s) — draining Claude silently",
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
            model=model,
            max_tokens=MAX_TOKENS,
            system=_split_claude_system_blocks(system),
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
        full_response_text = _join_turn(full_response_text, text_content)

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
                # Capture tools answer instantly — no filler, but the tool
                # must still execute below (never skip the round body).
                filler = None
                if first_tool_name not in _CONVERSATION_CLAUDE_CAPTURE_TOOLS:
                    if first_tool_name in {"check_availability", "create_booking", "transfer_to_human"}:
                        filler = _next_tool_filler(first_tool_name)
                    else:
                        filler = TOOL_FILLERS.get(first_tool_name, DEFAULT_FILLER)
                if filler:
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
                tb["input"], _ = _override_capture_spoken_argument(
                    tool_name=tb["name"],
                    tool_input=tb["input"],
                    override_spoken=_extract_last_user_utterance(conversation_history),
                    source="conversation_relay",
                )
                logger.info("Executing tool '%s' with input: %s", tb["name"], tb["input"])
                try:
                    result_str = await execute_tool(tb["name"], tb["input"])
                except Exception as exc:
                    logger.exception("Tool execution failed for '%s'", tb["name"])
                    result_str = json.dumps({"error": f"Tool execution failed: {exc}"})
                # Capture-mode endpointing is a MediaStreamSession concern;
                # ConversationRelay (this path) does its own endpointing on
                # Twilio's edge, so there is no session state to update here.
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tb["id"],
                    "content": result_str,
                })
                _append_booking_confirmation_marker(
                    transcript_sink or [],
                    tb["name"],
                    tb["input"],
                    result_str,
                )
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


async def _stream_llm_turn(
    *,
    system: str,
    conversation_history: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    websocket: WebSocket,
    lang: str,
    generation_ref: list[int] | None = None,
    anthropic_client: Any,
    gemini_client: Any,
    openai_client: Any,
    transcript_sink: list[dict[str, str]] | None = None,
    gemini_failover_state: dict[str, Any] | None = None,
) -> str:
    """Stream one agent turn over the ConversationRelay socket.

    Extracted so the normal prompt path and the handover-failsafe opening turn
    dispatch on LLM_PROVIDER identically — a second inline copy of this branch
    would silently drift the moment one provider's signature changed.
    """
    if LLM_PROVIDER == "claude":
        return await _run_llm_streaming_claude(
            client=anthropic_client,
            system=system,
            conversation_history=conversation_history,
            tools=tools,
            websocket=websocket,
            lang=lang,
            generation_ref=generation_ref,
            transcript_sink=transcript_sink,
        )
    if LLM_PROVIDER == "gemini":
        return await _run_llm_streaming_gemini(
            gemini_client=gemini_client,
            system=system,
            conversation_history=conversation_history,
            tools=tools,
            websocket=websocket,
            lang=lang,
            generation_ref=generation_ref,
            transcript_sink=transcript_sink,
            failover_state=gemini_failover_state,
        )
    return await _run_llm_streaming(
        client=openai_client,
        system=system,
        conversation_history=conversation_history,
        tools=tools,
        websocket=websocket,
        transcript_sink=transcript_sink,
    )


# The synthetic turn that makes Kavya OPEN the failsafe conversation instead of
# waiting for the guest. Twilio speaks the apology greeting, then
# ConversationRelay simply waits for guest speech — so without this the guest
# hears the greeting and then silence, and has to say something ("Okay") before
# Kavya asks anything. Observed live on 2026-07-31: 13 s of dead air.
#
# This is NOT added to full_transcript: the guest never said it, so it must not
# appear in the call log or the Google Sheet.
_FAILSAFE_KICKOFF: str = (
    "[SYSTEM: The guest has just heard the apology message and is waiting in "
    "silence. Speak first, right now. Do NOT greet them again and do NOT "
    "repeat the apology. Go straight to STEP 1 of your steps — ask for, or "
    "confirm, their name — in one short sentence.]"
)


# ---------------------------------------------------------------------------
# WebSocket — ConversationRelay handler
# ---------------------------------------------------------------------------

@app.websocket("/ws/conversation")
async def ws_conversation(websocket: WebSocket, lang: str = "en", mode: str = ""):
    """Handle a Twilio ConversationRelay WebSocket session.

    The ``lang`` query parameter is set by the IVR routing and determines
    which language-specific system prompt Claude receives.

    ``mode="handover_failsafe"`` marks the recovery session Twilio opens after a
    human agent failed to answer a transferred call: Kavya drops the booking
    flow and instead collects the guest's callback details for the manager.

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

    # The failsafe only exists on the English ConversationRelay path — that is
    # the only path with a live transfer to fail in the first place.
    is_failsafe: bool = mode == "handover_failsafe" and lang == "en"

    await websocket.accept()
    logger.info(
        "WebSocket connection accepted — language: %s%s",
        lang, " (handover failsafe)" if is_failsafe else "",
    )

    # -- Per-session state --
    conversation_history: list[dict] = []
    system_prompt: str = _build_system_prompt(lang)
    if LLM_PROVIDER == "claude":
        tools: list[dict] = get_tools()
    elif LLM_PROVIDER == "gemini":
        tools: list[dict] = get_tools_gemini()
    else:
        tools: list[dict] = get_tools_openai()
    if is_failsafe:
        # Only the notify tool. Leaving check_availability/create_booking in
        # reach would let Kavya wander back into the booking flow instead of
        # taking the callback details.
        tools = get_handover_tools(
            "claude" if LLM_PROVIDER == "claude"
            else "gemini" if LLM_PROVIDER == "gemini"
            else "openai"
        )
    call_sid: str = "unknown"
    caller_phone: str = "unknown"
    full_transcript: list[dict[str, str]] = []
    call_start_time: str = datetime.now().isoformat()
    # Set when this session ends because we handed the caller to a human. The
    # call is NOT over at that point — it either continues on the human's leg or
    # comes back as the failsafe session — so post-call processing is deferred
    # rather than run here. See the `finally` block.
    transfer_initiated: bool = False

    anthropic_client = None
    openai_client = None
    gemini_client = None
    generation_ref: list[int] = [0]
    gemini_failover_state = _init_gemini_failover_state()
    try:
        if LLM_PROVIDER == "claude":
            anthropic_client = _get_anthropic_client()
        elif LLM_PROVIDER == "gemini":
            gemini_client = _get_gemini_client()
        else:
            openai_client = _get_client()
    except RuntimeError:
        logger.error("LLM client not available — closing WebSocket")
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
                # Prefer the `from` field on the ConversationRelay setup message —
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
                    "Session setup — CallSid: %s, StreamSid: %s, Phone: %s",
                    call_sid,
                    message.get("streamSid", "n/a"),
                    caller_phone,
                )

                # Make the caller's own line reachable to the booking tool as a
                # WhatsApp fallback: if the guest's dictated number is unusable
                # (wrong length), create_booking uses this so the booking still
                # carries a reachable number instead of nothing. The failsafe
                # branch below installs its own richer context; this covers the
                # normal booking path, where handover_context is otherwise unset.
                if not is_failsafe:
                    from handover import _get_handover_context

                    ctx = _get_handover_context()
                    ctx["caller_phone"] = caller_phone
                    handover_context.set(ctx)

                if is_failsafe:
                    # Rebuild Kavya's context from the pre-transfer leg of this
                    # same call: the WebSocket is new, but the guest is not.
                    handoff_state = _handoff_state.setdefault(call_sid, {})
                    if caller_phone and caller_phone != "unknown":
                        handoff_state.setdefault("caller_phone", caller_phone)
                    else:
                        caller_phone = handoff_state.get("caller_phone") or caller_phone
                    system_prompt = _build_handoff_failsafe_prompt(handoff_state)
                    # Carry the pre-transfer transcript into the post-call
                    # record so the call log shows one continuous conversation.
                    prior = handoff_state.get("transcript") or []
                    if prior and not full_transcript:
                        full_transcript.extend(prior)
                    handoff_state["call_sid"] = call_sid
                    handoff_state["human_agent_whatsapp"] = HUMAN_AGENT_PHONE
                    handoff_state.setdefault("notified", False)
                    # Give the notify_human_handover handler the call metadata
                    # it cannot receive as a tool argument.
                    handover_context.set(handoff_state)
                    logger.info(
                        "[handover] failsafe session ready [%s] — prior turns: %d, "
                        "caller_phone=%s, dial_status=%s",
                        call_sid, len(prior), caller_phone,
                        handoff_state.get("dial_status", "n/a"),
                    )
                # Session state is already initialized above.
                # Log any additional setup metadata.
                logger.info(
                    "Session ready — system prompt length: %d chars, tools: %d",
                    len(system_prompt),
                    len(tools),
                )

                if is_failsafe:
                    # Open the conversation ourselves. Twilio has just spoken the
                    # apology greeting and ConversationRelay now waits for guest
                    # speech, so without this the guest sits in silence until they
                    # say something unprompted. The kickoff turn is seeded into
                    # conversation_history (the model needs it) but deliberately
                    # NOT into full_transcript (the guest never said it).
                    conversation_history.append(
                        {"role": "user", "content": _FAILSAFE_KICKOFF}
                    )
                    try:
                        generation_ref[0] += 1
                        opening = await _stream_llm_turn(
                            system=system_prompt,
                            conversation_history=conversation_history,
                            # NO TOOLS on the opening turn. This turn exists only
                            # to break the silence and ask for the name. With the
                            # handover tool in reach the model can — and in tests
                            # did — fire notify_human_handover immediately, paging
                            # the manager before it has the guest's name or
                            # number, and again after. All three providers accept
                            # an empty list (NOT_GIVEN / None).
                            tools=[],
                            websocket=websocket,
                            lang=lang,
                            generation_ref=generation_ref,
                            anthropic_client=anthropic_client,
                            gemini_client=gemini_client,
                            openai_client=openai_client,
                            transcript_sink=full_transcript,
                            gemini_failover_state=gemini_failover_state,
                        )
                        logger.info("Agent [%s] (failsafe opening): %s",
                                    call_sid, opening[:200])
                        if opening:
                            full_transcript.append(
                                {"role": "assistant", "text": opening}
                            )
                    except WebSocketDisconnect:
                        raise
                    except Exception:
                        # Never let the opening turn kill the session — the guest
                        # can still speak first and the reprompt below covers it.
                        logger.exception("[handover] failsafe opening turn failed")
                    _schedule_reprompt()
                else:
                    _schedule_reprompt()

            # ---------------------------------------------------------------
            # PROMPT — user speech transcribed
            # ---------------------------------------------------------------
            elif msg_type == "prompt":
                user_text = message.get("voicePrompt", "").strip()
                if not user_text:
                    logger.debug("Empty voicePrompt received — ignoring")
                    continue

                # Caller spoke — cancel any pending silence nudge and reset
                # the re-prompt counter.
                _cancel_reprompt()
                reprompt_count = 0

                logger.info("Guest [%s]: %s", call_sid, user_text)
                full_transcript.append({"role": "user", "text": user_text})

                # Retrieve KB context for this utterance. Skipped in the
                # failsafe session — Kavya is only taking a name and a phone
                # number there, so hotel facts are noise (and an embedding
                # lookup per turn the guest has to wait through).
                if is_failsafe:
                    kb_context = ""
                else:
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
                    response_text = await _stream_llm_turn(
                        system=system_prompt,
                        conversation_history=conversation_history,
                        tools=tools_for_session,
                        websocket=websocket,
                        lang=lang,
                        generation_ref=generation_ref,
                        anthropic_client=anthropic_client,
                        gemini_client=gemini_client,
                        openai_client=openai_client,
                        transcript_sink=full_transcript,
                        gemini_failover_state=gemini_failover_state,
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
                            # First non-tool user message — stop scanning
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
                            if (
                                isinstance(_parsed, dict)
                                and _parsed.get("status") == "transferring"
                            ):
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
                        # Stash everything the failsafe session will need if the
                        # human never picks up. Must happen BEFORE the REST
                        # update — after it, this WebSocket is on borrowed time.
                        transfer_initiated = True
                        _remember_handoff(
                            call_sid,
                            reason=pending_transfer_reason,
                            caller_phone=caller_phone,
                            transcript=list(full_transcript),
                            notified=False,
                            # Carried so whichever leg finishes the call can emit
                            # a post-call record covering the whole conversation.
                            call_start_time=call_start_time,
                            lang=lang,
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
                                "[handoff] cannot transfer — TWILIO_ACCOUNT_SID/"
                                "AUTH_TOKEN not configured; ending call"
                            )
                        elif not HUMAN_AGENT_PHONE:
                            logger.error(
                                "[handoff] cannot transfer — HUMAN_AGENT_PHONE "
                                "not set; ending call"
                            )
                        else:
                            from urllib.parse import quote as _quote
                            host = PUBLIC_HOSTNAME
                            reason_q = _quote(pending_transfer_reason)
                            # Present a number we own. Without this Twilio passes
                            # the guest's own number through as caller ID, and the
                            # destination carrier filters the leg as spoofing so
                            # the agent's handset never rings.
                            _cid = _transfer_caller_id(call_sid)
                            _cid_attr = f' callerId="{html_escape(_cid)}"' if _cid else ""
                            logger.info(
                                "[handoff] dialing %s for %s with callerId=%s",
                                HUMAN_AGENT_PHONE, call_sid, _cid or "(pass-through)",
                            )
                            twiml = (
                                '<?xml version="1.0" encoding="UTF-8"?>'
                                '<Response>'
                                '<Say voice="Polly.Joanna">Connecting you now. Please hold.</Say>'
                                f'<Dial action="https://{host}/voice/dial-result" method="POST" timeout="{HANDOFF_DIAL_TIMEOUT}"{_cid_attr} answerOnBridge="true">'
                                f'<Number url="https://{host}/voice/whisper?reason={reason_q}"'
                                f' statusCallback="https://{host}/voice/dial-status?parent={call_sid}"'
                                ' statusCallbackMethod="POST"'
                                ' statusCallbackEvent="initiated ringing answered completed">'
                                f'{HUMAN_AGENT_PHONE}</Number>'
                                '</Dial>'
                                '</Response>'
                            )
                            try:
                                # Twilio REST client is sync — run in executor
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
                        "WebSocket disconnected during Claude streaming [%s] — "
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
            # INTERRUPT — user interrupted agent speech
            # ---------------------------------------------------------------
            elif msg_type == "interrupt":
                logger.info(
                    "Speech interrupted by guest [%s] — utteranceUntilInterrupt: '%s'",
                    call_sid,
                    message.get("utteranceUntilInterrupt", ""),
                )
                generation_ref[0] += 1

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
        _cancel_reprompt()
        call_end_time = datetime.now().isoformat()
        logger.info(
            "Session ended — CallSid: %s, history: %d msgs, transcript: %d msgs",
            call_sid, len(conversation_history), len(full_transcript),
        )
        if is_failsafe:
            # Last line of defence: the guest hung up (or the line dropped)
            # before Kavya could send the details. Notify the manager anyway
            # with whatever we have — a callback to the number they rang from
            # beats the manager never hearing about the call at all.
            state = _handoff_state.pop(call_sid, {})
            if not state.get("notified"):
                asyncio.create_task(
                    _notify_handover_fallback(
                        call_sid=call_sid,
                        state=state,
                        caller_phone=caller_phone,
                        full_transcript=full_transcript,
                    )
                )
        if transfer_initiated:
            # Do NOT emit a post-call record here. This session is ending only
            # because the caller is being handed to a human — the conversation
            # continues on another leg, so anything written now describes a
            # truncated call. Live on 2026-07-31 this produced a spurious
            # "dropped" row in the Google Sheet at transfer time, followed by a
            # second, correct "callback_requested" row once the failsafe session
            # finished: two rows for one call, the first one misleading.
            #
            # The record is emitted instead by whichever leg actually ends the
            # call: /voice/dial-result when the human answers, or this same
            # `finally` on the failsafe session (is_failsafe, transfer_initiated
            # False) when they don't.
            logger.info(
                "[handoff] post-call deferred for %s — caller handed to a human",
                call_sid,
            )
        elif full_transcript:
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
# WebSocket — Media Streams handler (non-English languages)
# ---------------------------------------------------------------------------

@app.websocket("/ws/media-stream/{lang}")
async def ws_media_stream(websocket: WebSocket, lang: str):
    """Handle a Twilio Media Streams WebSocket session for a non-English call.

    Language is encoded in the URL path (e.g. /ws/media-stream/si) so it
    is always present — avoids unreliable query-string passing by Twilio.

    Receives raw mulaw 8 kHz audio from Twilio, runs Google Cloud STT,
    sends Claude responses through Azure TTS back as mulaw audio.

    Sinhala ("si") and Arabic ("ar") were removed from this guard on
    2026-07-28 along with their IVR digits, so those paths now refuse the
    connection instead of serving a call. Re-add them here to re-enable.
    """
    if lang not in ("ta",):
        logger.warning(
            "Rejecting Media Streams connection for disabled language %r", lang
        )
        await websocket.accept()
        await websocket.close(code=1008, reason="Language not available")
        return

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
        logger.error("LLM client unavailable — closing Media Streams WebSocket")
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
# Service-mode boundary
# ---------------------------------------------------------------------------
_twilio_app = app


async def _new_smartpbx_session(context, transport, diagnostic_sink=None):
    from smartpbx_session import KavyaSmartPBXSession

    return KavyaSmartPBXSession(context, transport, diagnostic_sink=diagnostic_sink)


def build_service_app(
    service_mode: str,
    environ: Any,
) -> FastAPI:
    """Select one ingress surface; never activate Twilio and Dialog together."""
    mode = service_mode.strip().lower() if isinstance(service_mode, str) else ""
    if mode == "twilio":
        return _twilio_app
    if mode != "smartpbx":
        raise ValueError("invalid KAVYA_SERVICE_MODE")

    from smartpbx_gateway import (
        SmartPBXGateway,
        SmartPBXSessionRegistry,
        SmartPBXSettings,
    )
    from smartpbx_mcp import DialogMCPSettings

    settings = SmartPBXSettings.from_env(environ)
    transfer_settings = DialogMCPSettings.from_env(environ)
    registry = SmartPBXSessionRegistry(settings.max_calls)
    gateway = SmartPBXGateway(settings, registry)
    smartpbx_app = FastAPI(
        title="Hatton Hills Voice Agent (Kavya) — SmartPBX",
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
        # admitted_total is a call-volume counter, so this needs the same shared
        # token as the media socket. Constant-time compare via token_matches.
        # When SmartPBX is unconfigured there is no token, so this fails closed;
        # /health stays open for liveness.
        if not settings.token_matches(
            request.headers.get(settings.auth_header_name, "")
        ):
            raise HTTPException(status_code=401)
        return {**gateway.snapshot(), "transfer_enabled": transfer_settings.enabled}

    async def smartpbx_media(websocket: WebSocket) -> None:
        await gateway.handle(websocket, _new_smartpbx_session)

    smartpbx_app.add_api_route("/health", smartpbx_health, methods=["GET"])
    smartpbx_app.add_api_route("/smartpbx/status", status, methods=["GET"])
    smartpbx_app.add_api_websocket_route(
        "/ws/v1/smartpbx/media",
        smartpbx_media,
    )
    smartpbx_app.state.smartpbx_gateway = gateway
    return smartpbx_app


app = build_service_app(KAVYA_SERVICE_MODE, os.environ)


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
