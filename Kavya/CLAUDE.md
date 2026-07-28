# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

Kavya is a multilingual AI voice agent for **Treehouse Chalets** (Belihuloya, Sri Lanka). Handles inbound phone calls via Twilio, uses a configurable LLM (Claude by default, or OpenAI / Gemini) for conversation and tool use, integrates with eZee Absolute PMS via n8n webhooks + browser extension for availability/booking, and grounds answers in a ChromaDB-based RAG knowledge base.

The agent persona is **Kavya** — a reservations agent. **As of 2026-07-28 the line is English only**: Sinhala and Arabic were removed from the IVR — `DIGIT_TO_LANG = {"1": "en"}`, the Arabic/Sinhala `<Say>` prompts are gone from `/voice/incoming`, and `/ws/media-stream/{lang}` now accepts only `ta` (so `si`/`ar` connections are refused). The Sinhala/Arabic/Tamil TTS, STT, prompt and filler code below all remain in place and dormant; re-enable by restoring the digit, the `<Say>`, and the guard entry. Sinhala TTS now uses **OpenAI `gpt-4o-mini-tts`** (voice `nova`) as of v0.16 (was Azure `si-LK-SameeraNeural`). Tamil is fully implemented in code (Media Streams path below) but is still **NOT surfaced in the menu** — add `"4": "ta"` (and a `<Say>` prompt) to expose it.

**Two server modes:**
- `server.py` — **Unified production server** (IVR DTMF menu: English→ConversationRelay+ElevenLabs, Arabic→Media Streams+ElevenLabs multilingual, Sinhala→Media Streams+OpenAI `gpt-4o-mini-tts`; Tamil→Media Streams+ElevenLabs is implemented but unlisted in the menu)
- `media_stream_server.py` — **Standalone Media Streams** (Anthropic Claude, ElevenLabs multilingual TTS, Google Cloud STT, barge-in — kept as reference/alternative)

## Project File Map

```
Full Voice agent/
├── server.py                  # Unified production server (IVR + ConversationRelay + Media Streams)
├── media_stream_server.py     # Standalone Media Streams server (reference/alternative, uses Anthropic)
├── booking_api.py             # n8n webhook integration (availability polling, room type IDs)
├── post_call.py               # Post-call data extraction (LLM summary) + n8n webhook to Google Sheets
├── tools.py                   # Tool definitions (Anthropic + OpenAI + Gemini formats) + dispatch
├── knowledge_base.py          # ChromaDB RAG — chunk, embed, query knowledge docs
├── knowledge_docs/            # Source documents for RAG
│   └── hotel_info.txt         # Hotel info (rooms, rates, policies, activities)
├── chroma_db/                 # ChromaDB vector store (auto-generated, gitignored)
├── ezee_api.py                # LEGACY — direct eZee API (not imported, kept for reference)
├── test_voice_elevenlabs.py   # Local demo — typed input → LLM → ElevenLabs TTS playback
├── test_voice.py              # Local demo — typed input → LLM → Azure TTS playback (backup)
├── Dockerfile                 # Production image (python:3.11-slim), runs server:app
├── docker-compose.yml         # Docker orchestration — port 127.0.0.1:8000, mounts GCP creds
├── nginx.conf                 # Reverse proxy — SSL termination, WSS upgrade, rate limiting
├── requirements.txt           # Full dependencies (local dev, includes pyaudio/azure)
├── requirements-prod.txt      # Production dependencies (anthropic, openai, google-cloud-speech)
├── deploy.sh                  # Deployment script (setup/deploy/logs/status) for DigitalOcean VPS
├── full-voice-agent-a8a245fb37cb.json  # GCP service account JSON (Google Cloud STT credentials)
├── .env                       # Secrets — never committed (API keys, voice IDs, etc.)
├── .env.example               # Template for .env with all required/optional vars
└── CLAUDE.md                  # This file — project context for Claude Code
```

**Browser extension** (separate from this repo):
```
C:/Users/mrdar/Downloads/ezeey-addon-extracted/ezeey-addon/
├── content.js       # Main logic — polls n8n for pending requests, scrapes eZee, posts results
├── manifest.json    # Firefox extension manifest
├── popup.html       # Extension popup UI
└── popup.js         # Popup logic
```

## Commands

```bash
# Install dependencies
pip install -r requirements.txt

# Local demo with ElevenLabs TTS
python test_voice_elevenlabs.py

# Local demo with Azure TTS (backup)
python test_voice.py

# Production server (unified)
python server.py

# Docker
docker compose build
docker compose up -d
docker compose logs -f kavya

# Deploy to DigitalOcean VPS
./deploy.sh setup    # first-time provisioning
./deploy.sh deploy   # push code updates
./deploy.sh logs     # tail remote logs
./deploy.sh status   # health check
```

Minimum to test locally: `ANTHROPIC_API_KEY` + `LLM_PROVIDER=claude` for text-only mode. Add `ELEVENLABS_API_KEY` + `ELEVENLABS_VOICE_ID` for voice output.

## Environment Setup

Copy `.env.example` to `.env`. Key groups:

**LLM provider** (pick one — Claude is default):
- `LLM_PROVIDER` — `"claude"` (default), `"openai"`, or `"gemini"`
- `ANTHROPIC_API_KEY`, `CLAUDE_MODEL` — Claude/Anthropic (default model: `claude-sonnet-4-20250514`)
- `OPENAI_API_KEY`, `OPENAI_MODEL` — OpenAI (default model: `gpt-4o`)
- `GEMINI_API_KEY`, `GEMINI_MODEL` — Gemini via native google-genai SDK (default model: `gemini-2.5-flash`)

**TTS/STT:**
- `ELEVENLABS_API_KEY`, `ELEVENLABS_VOICE_ID` — ElevenLabs TTS (English ConversationRelay + Tamil Media Streams)
- `OPENAI_API_KEY` — OpenAI key; used for `gpt-4o-mini-tts` (Sinhala voice) and, if `LLM_PROVIDER=openai`, the LLM
- `OPENAI_TTS_MODEL` / `OPENAI_TTS_VOICE` / `OPENAI_TTS_INSTRUCTIONS` — Sinhala TTS config (defaults: `gpt-4o-mini-tts`, `nova`, a warm Kavya/Treehouse Sinhala-tone instruction)
- `AZURE_SPEECH_KEY`, `AZURE_SPEECH_REGION` — Azure TTS (legacy Sinhala path `_tts_azure`, no longer live) + Azure STT backend. Region: `southeastasia`
- `GOOGLE_APPLICATION_CREDENTIALS` — GCP service-account JSON for Google Cloud STT. File: `full-voice-agent-a8a245fb37cb.json`, mounted as `/app/gcp-credentials.json` in Docker

**Telephony & integrations:**
- `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN` — Production telephony
- `N8N_BASE_URL` — n8n webhook base URL (default: `https://automation.taskforceai.tech`)
- `N8N_POLL_INTERVAL`, `N8N_POLL_TIMEOUT` — Polling tuning (default: 2s interval, 60s timeout)

## Architecture

### LLM Integration

`server.py` supports three LLM providers, switched via `LLM_PROVIDER` env var:

| Provider | `LLM_PROVIDER` | Client | Tool format | History format |
|---|---|---|---|---|
| Anthropic Claude | `"claude"` (default) | `AsyncAnthropic` | `input_schema` (native) | content blocks (`tool_use`/`tool_result`) |
| OpenAI | `"openai"` | `AsyncOpenAI` | `parameters` (function-calling) | `role: "tool"` messages |
| Gemini | `"gemini"` | `google.genai.Client` | `function_declarations` (native) | converted via `_history_to_gemini()` |

Each provider has its own streaming function pair:
- ConversationRelay: `_run_llm_streaming_claude()` / `_run_llm_streaming()` / `_run_llm_streaming_gemini()`
- Media Streams: `_run_llm_claude()` / `_run_llm()` / `_run_llm_gemini()`

`tools.py` exposes `get_tools()` (Anthropic), `get_tools_openai()`, and `get_tools_gemini()`.

### ConversationRelay — `server.py` (English, Press 1)

```
Incoming call
  -> POST /voice/incoming (returns TwiML with <Gather> DTMF menu: 1=EN, 2=SI, 3=TA)
  -> Caller presses 1
  -> POST /voice/language-selected (digit=1, returns TwiML with <ConversationRelay>)
  -> WebSocket /ws/conversation?lang=en
  -> LLM streaming with tool use
  -> text tokens streamed back -> Twilio converts to speech
  -> TTS routing: English → ElevenLabs flash_v2_5 via ConversationRelay
```

Twilio manages the entire audio pipeline for English. Server only deals with text in/out. ConversationRelay does NOT support Arabic/Sinhala/Tamil — those languages are routed to Media Streams instead.

### Media Streams — `server.py` (Arabic = Press 2; Sinhala/Tamil implemented but unlisted)

```
Non-English call
  -> POST /voice/incoming (returns TwiML with <Gather> DTMF menu: 1=EN, 2=Arabic, 3=Sinhala)
  -> Caller presses 2 (Arabic) or 3 (Sinhala)   [Tamil code path exists but no menu digit maps to it]
  -> POST /voice/language-selected -> DIGIT_TO_LANG.get(digit, "en")  (digit 2 -> "ar", digit 3 -> "si")
  -> returns TwiML with <Stream url="wss://{host}/ws/media-stream/{lang}">
  -> WebSocket /ws/media-stream/{lang}   ({lang} guard accepts si/ta/ar)
  -> Twilio Media Streams bidirectional audio (mulaw 8kHz)
  -> Google Cloud STT (streaming, background thread, interim-based endpointing)
  -> KB retrieval + LLM streaming with tool use (native script system prompt)
  -> TTS: Arabic → ElevenLabs (eleven_multilingual_v2); Sinhala → OpenAI (gpt-4o-mini-tts, nova); Tamil → ElevenLabs
  -> mulaw audio chunks streamed back to Twilio
```

TTS routing (`_speak`): `lang in ("ta", "ar")` → ElevenLabs `eleven_multilingual_v2` (cloned voice, `ulaw_8000`, zero conversion); `lang == "si"` → OpenAI `gpt-4o-mini-tts` (`_tts_openai`, voice `nova`) — returns 24 kHz PCM, downsampled on the fly to 8 kHz μ-law via `audioop`; the legacy Azure `si-LK-SameeraNeural` path (`_tts_azure`) is still wired but no longer the live Sinhala route.

### Shared Components

**eZee PMS integration** (`booking_api.py` + `tools.py`):
Four tools: `check_availability`, `create_booking`, `retrieve_booking`, `cancel_booking`. `check_availability` and `create_booking` are fully implemented via n8n async polling (same submit-and-poll helper, different submit webhook). `retrieve_booking` and `cancel_booking` return graceful fallback messages.

Availability flow: Kavya POSTs to n8n `/webhook/make-availability-request` → n8n queues in DataTable (`eezy-pending-requests`) → Firefox browser extension ("IPMS247 Extractor") polls `/webhook/pending-requests` → scrapes eZee web UI → POSTs result to `/webhook/availability-response` → n8n updates DataTable row (`checked=true`, `response=data`) → Kavya polls `/webhook/eezy-check-results` until response is ready.

**n8n webhook endpoints** (all under `N8N_BASE_URL = https://automation.taskforceai.tech`):
- `/webhook/make-availability-request` — Kavya submits availability check (POST)
- `/webhook/eezy-check-results` — Kavya polls for results (GET, query param `requestId`)
- `/webhook/pending-requests` — Extension polls for work (GET, filtered by `checked Is False`)
- `/webhook/availability-response` — Extension posts scraped results (POST)
- `/webhook/make-booking` — Kavya submits booking (POST). Same async-polling pattern as availability: extension picks up the row, creates the reservation in eZee, writes the confirmation number back into the `response` field of the same DataTable row. Kavya polls `/webhook/eezy-check-results` until populated.
- `/webhook/transcript` — Kavya POSTs call summary + transcript after each call (POST, → Google Sheets)

**Requires**: Browser extension running on a machine logged into `live.ipms247.com` in Firefox. Without it, availability checks timeout after `N8N_POLL_TIMEOUT` seconds.

**Knowledge base** (`knowledge_base.py`):
Files in `knowledge_docs/` chunked (500 chars, 50 overlap) -> embedded with `all-MiniLM-L6-v2` -> stored in ChromaDB (`./chroma_db`). Query embeddings LRU-cached. KB context injected as user message prefix per turn (not system prompt). Prewarm on startup. Chunk IDs are SHA-256 hashes (idempotent re-indexing).

**Post-call data capture** (`post_call.py`):
When a call ends (WebSocket disconnect), `server.py` fires `asyncio.create_task(process_post_call_data(...))` to run post-call processing in the background. The flow: format the full transcript → call LLM (same provider as the conversation) to extract structured booking details (guest name, dates, room preference, outcome, follow-up needed, summary) → POST JSON payload to n8n webhook `/webhook/transcript` → n8n appends a row to Google Sheet "Kavya Call Log". Caller phone number is captured from Twilio HTTP POST params (`From`) via a module-level `_call_phone` dict bridge between HTTP handlers and WebSocket handlers. A separate `full_transcript` list (never trimmed) accumulates all user/assistant messages alongside the trimmed `conversation_history`. All errors are caught and logged — post-call failures never affect the call or server stability. Env var: `N8N_POSTCALL_WEBHOOK` (default: `/webhook/transcript`).

**Google Sheet columns** (n8n workflow "Post-Call Data to Google Sheets"): Date/Time, Call SID, Language, Caller Phone, Guest Name, Location, Guests, Check-In, Check-Out, Room Preference, Availability, Outcome, Follow-Up Needed, Summary, Transcript.

**Local demos** (`test_voice_elevenlabs.py`, `test_voice.py`):
Typed input -> KB retrieval -> LLM tool-use loop -> text response -> TTS playback. ElevenLabs version sends full response as one TTS call (splitting into sentences causes prosody resets). Azure version splits by sentence with per-language voice selection.

## Key Design Decisions

- **Pluggable LLM**: `LLM_PROVIDER` env var switches between Claude (default), OpenAI, and Gemini. Each has its own client singleton, streaming functions, and tool format. History is stored in provider-native format; Gemini uses a converter `_history_to_gemini()` since it stores in OpenAI format internally.
- **Claude as default**: Anthropic Claude (`claude-sonnet-4-20250514`) is the default and primary tested provider. Uses native `AsyncAnthropic` SDK with `messages.stream()`, content block events (`content_block_start`, `content_block_delta`, `content_block_stop`), and Anthropic tool format (`input_schema`).
- **Two servers, one codebase**: `server.py` (unified production) and `media_stream_server.py` (standalone, Anthropic-only, kept as reference). Both share `booking_api.py`, `tools.py`, `knowledge_base.py`.
- **DTMF language menu**: live menu (v0.16) is Press 1 → ConversationRelay (English + ElevenLabs); Press 2 → Media Streams (Arabic + ElevenLabs multilingual); Press 3 → Media Streams (Sinhala + OpenAI `gpt-4o-mini-tts`). `DIGIT_TO_LANG = {"1": "en", "2": "ar", "3": "si"}`; no input → English. Tamil (Media Streams + ElevenLabs) remains fully coded but is not mapped to any menu digit — add `"4": "ta"` plus a matching `<Say>` prompt to re-expose it.
- **Interim-based STT endpointing**: Google Cloud STT rarely fires `is_final=True` for conversational speech. Each interim result overwrites `_pending_transcript` (not appends) and resets a 1.5s silence timer. When the timer fires, the latest interim is treated as the complete utterance.
- **Tool gating via system prompt**: General info (rooms, rates, policies, activities) answered from KB context — no tool call. Tools only for date-specific booking operations.
- **Filler speech**: Spoken before tool execution to avoid silence during API calls (language-specific fillers for Sinhala/Tamil).
- **max_tokens=300**: Forces concise voice-appropriate responses.
- **History trimming**: Max 20 messages. `_trim_history()` is format-aware — detects and skips orphaned tool result messages at the start of trimmed history for both Anthropic format (user messages containing `tool_result` content blocks) and OpenAI format (`role: "tool"` messages). Also skips orphaned assistant `tool_use`/`tool_calls` messages.
- **Native script**: LLM responds in native Sinhala/Tamil Unicode script. TTS handles native script directly.
- **Kavya persona**: Collects booking info in order: name → location (local vs foreign rates) → pax → dates → room. Mentions complimentary activities (2+ nights), April/December advance payment, honeymoon packages.
- **Hybrid TTS**: English → ElevenLabs turbo (cloned voice via ConversationRelay), Sinhala → OpenAI `gpt-4o-mini-tts` (voice `nova`, 24 kHz PCM → 8 kHz μ-law via `audioop`), Tamil → ElevenLabs `eleven_multilingual_v2` (cloned voice, `ulaw_8000` output). Legacy Azure `si-LK-SameeraNeural` Sinhala path (`_tts_azure`) is wired but no longer the live route.
- **Barge-in**: Media Streams only. When STT detects speech during TTS, sends `clear` event to Twilio, sets `_is_speaking = False`, increments `_speak_generation` to cancel queued TTS tasks.

## Server Endpoints

### server.py (Unified — ConversationRelay + Media Streams)
- `POST /voice/incoming` — Returns TwiML with `<Gather>` DTMF language menu (1=EN, 2=SI, 3=TA)
- `POST /voice/language-selected` — Handles DTMF result: digit=1 returns ConversationRelay TwiML, digit=2/3 returns Media Streams TwiML
- `WebSocket /ws/conversation` — Handles English ConversationRelay: `setup`, `prompt`, `dtmf`, `interrupt`
- `WebSocket /ws/media-stream/{lang}` — Handles Sinhala/Tamil Media Streams calls (Google STT + TTS)
- `GET /health` — `status`, `llm_provider`, `model`, `ezee_configured`, `kb_loaded`, `media_streams_stt`, `azure_tts`

### media_stream_server.py (Standalone Media Streams — reference only)
- `POST /voice/incoming` — Returns TwiML with `<Stream>`
- `WebSocket /ws/media-stream` — Handles: `start`, `media`, `mark`, `stop`
- `GET /health` — `status`, `mode`, `ezee_configured`, `kb_loaded`, `stt_available`, `tts_configured`, `model`

## Server Constants

- `MAX_TOKENS = 300`
- `MAX_HISTORY_MESSAGES = 20`
- `MAX_TOOL_ROUNDS = 5`
- `ENDPOINTING_SILENCE = 1.5` (Media Streams only — seconds of silence before utterance is complete)

## System Prompt Structure

Built dynamically by `_build_system_prompt(lang)` with today's date injected and language parameter. Sections:
1. **Persona**: Kavya, reservations agent for Treehouse Chalets
2. **Language rules**: Language-specific (determined by IVR selection), native script for Sinhala/Tamil
3. **Voice rules**: Short sentences, no markdown/bullets/URLs, numbers as words, no abbreviations
4. **Booking rules**: Answer general info from KB (no tool needed), tools only for date-specific operations, collect info in order, mention complimentary activities, advance payment, honeymoon packages

## Operational Details

- **Deployment**: Dockerfile (`python:3.11-slim`), `docker-compose.yml`, `nginx.conf` (SSL + WSS + rate limiting), `requirements-prod.txt`, `deploy.sh`. Target: DigitalOcean VPS at `67.207.90.109` (`treehouse.taskforceai.tech`). Docker CMD runs `server:app`. Docker port `127.0.0.1:8000` (nginx-only). Single uvicorn worker (sentence-transformers uses ~400MB-1GB RAM).
- **Deployment DNS**: Cloudflare proxy OFF / DNS only for direct SSL. Call routing: hotel's Sri Lankan mobile → unconditional forward → Twilio US number → Kavya.
- **Room type IDs** (full eZee IDs as used in `booking_api.py` `ROOM_TYPE_NAMES`):
  - `3020000000000000003` = Mount Monarch
  - `3020000000000000004` = Mount Luxe
  - `3020000000000000005` = Sunrise Vista
  - `3020000000000000006` = Eco Harmony
  - `3020000000000000007` = Forest Escape Suite
- **ConversationRelay WebSocket protocol**: Send `{"type": "text", "token": "<token>"}` per LLM token. Send `{"type": "text", "token": "", "last": true}` to signal end-of-utterance. Filler messages sent with `last: true` before tool execution.
- **Media Streams WebSocket protocol**: Receive `{"event": "media", "media": {"payload": "<base64 mulaw>"}}`. Send audio back as `{"event": "media", "streamSid": "...", "media": {"payload": "<base64 mulaw>"}}`. Barge-in: send `{"event": "clear", "streamSid": "..."}`. TTS completion: send `{"event": "mark", ...}`.
- **Media Streams STT**: Google Cloud Speech-to-Text streaming runs in a daemon thread (sync gRPC client). Accepts mulaw 8kHz directly. Language-specific primary: `si-LK` for Sinhala, `ta-IN` for Tamil. Alternatives: `en-US` + the other regional language. `interim_results=True` — endpointing driven by interims, not finals. Callbacks into async event loop via `asyncio.run_coroutine_threadsafe`.
- **Media Streams TTS routing**: `_speak()` routes by language: Tamil/Arabic → `_tts_elevenlabs()` (ElevenLabs `eleven_multilingual_v2`, `ulaw_8000`), Sinhala → `_tts_openai()` (OpenAI `gpt-4o-mini-tts`, `response_format=pcm` 24 kHz → 8 kHz μ-law via `audioop.ratecv`/`lin2ulaw`, flushed in 640-byte frames). The legacy `_tts_azure()` (Azure REST, `raw-8khz-8bit-mono-mulaw`, SSML) is wired but unused. `_speak_lock` serializes TTS calls. `_ws_lock` serializes WebSocket writes.
- **Error handling**: LLM streaming failure sends language-appropriate fallback message. Missing LLM client closes WebSocket with code 1011.
- **Legacy**: `ezee_api.py` kept but not imported. `media_stream_server.py` uses Anthropic Claude directly — kept as reference.

---

## Change History

This section documents the major changes made to the project since initial development.

---

### v0.1 — Initial Build
**What it was:** Single-server Media Streams agent using Anthropic Claude directly. English only. Text-to-speech via Azure Cognitive Services. Basic tool calling for eZee availability check via direct API.

---

### v0.2 — STT Integration
**Changes:**
- Added Google Cloud Speech-to-Text for Media Streams audio input
- Integrated GCP service account credentials (`full-voice-agent-a8a245fb37cb.json`)
- STT ran in a background daemon thread (sync gRPC client) feeding into async event loop via `asyncio.run_coroutine_threadsafe`
- Initial implementation relied on `is_final=True` results for endpointing

**Problem discovered:** Google Cloud STT rarely fires `is_final=True` for natural conversational speech — calls would often hang waiting for a final result that never came.

---

### v0.3 — IVR Language Menu + Unified Server
**Changes:**
- Built `server.py` as a unified production server replacing `media_stream_server.py` as the primary
- Added DTMF IVR menu: press 1 = English, press 2 = Sinhala, press 3 = Tamil
- English routed to **Twilio ConversationRelay** (server handles text only, Twilio does TTS/STT)
- Sinhala/Tamil routed to **Twilio Media Streams** (server handles full audio pipeline)
- `media_stream_server.py` kept as reference/alternative
- Added `POST /voice/incoming` and `POST /voice/language-selected` endpoints

---

### v0.4 — n8n Webhook Integration (Replaces Direct eZee API)
**Changes:**
- Replaced direct eZee Reservation API calls (`ezee_api.py`) with n8n webhook-based async polling
- Added `booking_api.py` with polling loop: POST request → poll for result → return
- Built Firefox browser extension ("IPMS247 Extractor") to scrape eZee web UI and post results to n8n
- `ezee_api.py` kept as legacy reference
- Added `N8N_BASE_URL`, `N8N_POLL_INTERVAL`, `N8N_POLL_TIMEOUT` env vars

---

### v0.5 — ChromaDB Knowledge Base
**Changes:**
- Added `knowledge_base.py` with ChromaDB vector store
- Hotel info chunked (500 chars, 50 overlap), embedded with `all-MiniLM-L6-v2`
- KB context injected as user message prefix each turn (not in system prompt, to avoid stale context)
- LRU cache on query embeddings
- Prewarm on server startup
- Chunk IDs are SHA-256 hashes for idempotent re-indexing

---

### v0.6 — STT Fix: Interim-Based Endpointing
**Problem:** Google Cloud STT `is_final=True` results rarely fired for conversational speech. Calls were hanging, never processing guest speech.

**Fix:**
- Added `on_interim_result` callback to `GoogleSTTStream`
- Each interim result **overwrites** (not appends to) `_pending_transcript`
- Each interim resets a 1.5-second silence timer (`ENDPOINTING_SILENCE`)
- When the timer fires, the latest interim is flushed as the complete utterance
- `is_final=True` results still handled but no longer relied upon

---

### v0.7 — Sinhala TTS Voice Change
**Change:** Switched Sinhala Azure TTS voice from `si-LK-ThiliniNeural` (female) to `si-LK-SameeraNeural` (male).

**Reason:** Better voice quality and more appropriate persona match for the Kavya agent speaking Sinhala.

---

### v0.8 — Tamil TTS: Azure → ElevenLabs Multilingual
**Problem investigated:** Could Google Cloud TTS support Sinhala and Tamil? Answer: No — neither `si-LK` nor `ta-LK` are in Twilio ConversationRelay's supported voice set.

**Change:**
- Tamil TTS switched from Azure (`ta-LK-SaranyaNeural`) to ElevenLabs `eleven_multilingual_v2` with the cloned voice
- ElevenLabs output format set to `ulaw_8000` — direct mulaw output, no conversion needed
- Sinhala stays on Azure (`si-LK-SameeraNeural`) — ElevenLabs multilingual does not support Sinhala
- `_speak()` now routes: `lang == "ta"` → `_tts_elevenlabs()`, otherwise → `_tts_azure()`
- Added `_tts_elevenlabs()` method to `MediaStreamSession`

---

### v0.9 — LLM Switch: Anthropic → OpenAI
**Reason:** Exploring alternatives; Claude was having issues in some areas.

**Changes:**
- Replaced `anthropic` SDK with `openai` SDK in `requirements-prod.txt`
- Rewrote LLM streaming to use `AsyncOpenAI` with `chat.completions.create(stream=True)`
- Streaming now uses `chunk.choices[0].delta` events instead of content block events
- Tool history format changed to OpenAI format: `role: "tool"` messages with `tool_call_id`
- Added `get_tools_openai()` to `tools.py` — converts Anthropic `input_schema` to OpenAI `parameters`
- `_trim_history()` updated to handle OpenAI format (skip leading `role: "tool"` messages and assistant messages with `tool_calls`)

**Bug fixed:** History trimming was cutting assistant `tool_calls` messages but leaving the following `role: "tool"` result messages, causing a 400 error ("orphaned tool_result"). Fixed by also skipping leading `role: "tool"` messages after trimming.

---

### v0.10 — Gemini Support Added (via OpenAI Compat Layer)
**Change:** Added Gemini as a switchable LLM provider.

- Added `LLM_PROVIDER` env var (`"openai"` default, `"gemini"` option)
- `_get_client()` returns `AsyncOpenAI` pointed at Google's OpenAI-compatible endpoint for Gemini
- No streaming code changes needed — same `AsyncOpenAI` client handles both
- Added `GEMINI_API_KEY`, `GEMINI_MODEL` env vars

**Problems discovered with Gemini via compat layer:**
- Responses truncating mid-sentence (e.g., stopping at "I can check" — 53 chars — instead of calling the tool)
- Tool calling inconsistent — sometimes worked, sometimes the model stopped without calling any tool
- Root cause: Gemini's OpenAI compatibility layer has bugs in streaming tool call handling

---

### v0.11 — Native Gemini SDK Integration
**Change:** Replaced Gemini-via-OpenAI-compat with native `google-genai` SDK to fix truncation and tool calling issues.

**Changes:**
- Added `google-genai>=1.0.0` to `requirements-prod.txt`
- Added `_get_gemini_client()` using `google.genai.Client`
- Added `_history_to_gemini()` converter — transforms OpenAI-format history to Gemini-native contents (handles `tool_result` → `function_response` in user role, `tool_calls` → `function_call` parts in model role)
- Added `_run_llm_streaming_gemini()` for ConversationRelay (native streaming, function_call parts come complete per chunk — not split like OpenAI deltas)
- Added `_run_llm_gemini()` for Media Streams
- Added `get_tools_gemini()` to `tools.py` — `function_declarations` format
- History stored in OpenAI format internally; converted on-the-fly for each Gemini API call

---

### v0.12 — LLM Switch Back to Claude (Current State)
**Reason:** Claude tools work correctly and reliably. Gemini and OpenAI had various issues (truncation, inconsistent tool triggering). Claude is the primary provider.

**Changes:**
- Added `anthropic>=0.40.0` back to `requirements-prod.txt`
- `LLM_PROVIDER` default changed to `"claude"`
- Added `ANTHROPIC_API_KEY`, `CLAUDE_MODEL` env vars (default model: `claude-sonnet-4-20250514`)
- Added `_get_anthropic_client()` — `AsyncAnthropic` singleton
- Added `_run_llm_streaming_claude()` for ConversationRelay — uses `client.messages.stream()` context manager with `content_block_start` / `content_block_delta` / `content_block_stop` event handling
- Added `_run_llm_claude()` for Media Streams — same event-based streaming with sentence-level TTS dispatch
- Claude tool format uses existing `get_tools()` (Anthropic native `input_schema` — no conversion needed)
- Claude history uses Anthropic native format: assistant content blocks (`tool_use`), user content blocks (`tool_result`)
- `_trim_history()` made fully format-aware:
  - Added `_is_tool_result_msg()` — detects orphaned tool results in both Anthropic format (user message with `tool_result` content blocks) and OpenAI format (`role: "tool"`)
  - Added `_is_tool_call_msg()` — detects tool calls in both Anthropic format (assistant content with `tool_use` blocks) and OpenAI format (`tool_calls` key)
- All three providers (Claude, OpenAI, Gemini) remain available and switchable via `LLM_PROVIDER`
- `MediaStreamSession.__init__` accepts `anthropic_client`, `openai_client`, `gemini_client` separately
- `.env.example` updated with all three provider options documented

---

### v0.13 — Post-Call Data Capture + Google Sheets Dashboard
**Reason:** Property manager had no visibility into calls. All conversation data was discarded when calls ended.

**Changes:**
- Added `post_call.py` — new module handling post-call LLM extraction and n8n webhook POST
- At call end, LLM extracts structured booking details from the full transcript: guest name, location, pax, dates, room preference, availability result, call outcome, follow-up needed, summary
- Extracted data + full transcript POSTed to n8n webhook `/webhook/transcript`
- n8n workflow appends a row to Google Sheet "Kavya Call Log" (15 columns)
- Added `_call_phone` module-level dict in `server.py` — bridges caller phone number from Twilio HTTP POST params to WebSocket sessions
- Added `full_transcript` list (separate from trimmed `conversation_history`) — accumulates all user/assistant messages, never trimmed
- `finally` blocks in both ConversationRelay and MediaStreamSession fire `asyncio.create_task(process_post_call_data(...))` — fully async, fire-and-forget
- Supports all three LLM providers (Claude, OpenAI, Gemini) for extraction
- All errors caught and logged — post-call failures never crash the server
- Added `N8N_POSTCALL_WEBHOOK` env var (default: `/webhook/transcript`)

---

### v0.14 — Pluggable Media Streams STT (Azure alternative for Sinhala)
**Reason:** Google Cloud STT was the documented weak point for spoken Sinhala — it rarely fires `is_final=True` for conversational Sinhala, forcing the 1.5 s interim-endpointing workaround (v0.2/v0.6). Testing Azure STT to optimize Sinhala recognition.

**Changes:**
- Added `AzureSTTStream` — a drop-in alternative to `GoogleSTTStream` with the identical interface (`start()`/`stop()`/`feed()` + `on_final_result`/`on_interim_result` callbacks fired from background threads).
  - Decodes Twilio mulaw 8 kHz → PCM16 (`audioop.ulaw2lin`) and writes to an Azure `PushAudioInputStream` (8 kHz/16-bit/mono), avoiding the GStreamer dependency Azure's native compressed-mulaw input requires.
  - Continuous recognition via `recognizing` (interim) / `recognized` (final) / `canceled` event callbacks. Uses a **fixed** language per call (`si-LK`/`ta-IN`) instead of Google's `alternative_language_codes` code-switching — better accuracy for a single-language line and it commits real finals, so endpointing no longer depends solely on the interim timer.
- Added `_make_stt()` factory selecting the backend from `STT_PROVIDER` (`"google"` default | `"azure"`); falls back to Google with a logged error if Azure is selected but its SDK/`audioop` is missing. `MediaStreamSession.run()` now builds STT via the factory.
- **Live-call audio capture** for offline benchmarking: when `STT_DEBUG_DUMP=1`, each Media Streams session buffers the raw mulaw payloads and `_write_audio_dump()` writes an 8 kHz PCM16 wav to `STT_DEBUG_DIR` (`{call_sid}_{lang}.wav`) at session end — real telephony clips to A/B Google vs Azure (vs Whisper/Scribe).
- Azure reuses the existing `AZURE_SPEECH_KEY` / `AZURE_SPEECH_REGION` (already set for Sinhala TTS) — no new credentials.
- New env vars: `STT_PROVIDER`, `STT_DEBUG_DUMP`, `STT_DEBUG_DIR` (documented in `.env.example`).
- `requirements-prod.txt`: added `azure-cognitiveservices-speech>=1.34.0` and `audioop-lts; python_version >= "3.13"` (audioop is stdlib on the 3.11 image; backported only where removed).
- `/health` now reports `stt_provider` and `azure_stt`.

**How to test:** set `STT_PROVIDER=azure` and restart the container; place a Sinhala call (IVR press 2). To benchmark, run a batch with `STT_DEBUG_DUMP=1` on both providers, then compare the captured wavs/transcripts.

### v0.15 — Arabic Language Path (MSA, Media Streams)
**Reason:** Add Arabic understand+speak alongside English. Twilio ConversationRelay's supported language set has **no Arabic** (`ar-*` absent — same limitation as Sinhala/Tamil), so Arabic could not mirror the English CR path and instead rides the **Media Streams** path like Tamil.

**Changes (all in `server.py`):**
- IVR menu re-enabled at `/voice/incoming`: `<Gather>` with `Polly.Joanna` "press 1 English" + `Polly.Zeina` Arabic "press 2". No input → falls through to English ConversationRelay (preserves prior straight-to-English for silent callers).
- `DIGIT_TO_LANG = {"1": "en", "2": "ar"}` (Sinhala/Tamil code retained but unlisted in the menu).
- Arabic routes through the existing `else` branch of `/voice/language-selected` → `<Stream …/ws/media-stream/ar>`.
- `ws_media_stream` guard widened to `("si", "ta", "ar")`.
- STT: `STT_PRIMARY["ar"] = "ar-SA"` (MSA proxy), `STT_ALTERNATIVES["ar"] = ["en-US"]` — Google Cloud STT (supports Arabic).
- TTS: `_speak` routes `lang in ("ta", "ar")` → `_tts_elevenlabs` (`eleven_multilingual_v2`, confirmed supports Arabic) using the existing `ELEVENLABS_VOICE_ID` (the Media Streams voice `IHidXiNlNy77IIuhYr6a` — **not** the English CR voice). Dedicated native Arabic voice to be swapped in later.
- Added Arabic `MEDIA_STREAM_WELCOME`, `MEDIA_STREAM_FILLERS`, `SLOW_RESPONSE_FILLERS`, `REPROMPT_MESSAGES` entries (MSA).
- `_build_system_prompt`: added `elif lang == "ar"` MSA language rules (native Arabic script, never romanized) and made the "already heard greeting" note language-aware via a `greeting_note` variable (previously hard-coded the English greeting for all languages).
- No human-handoff for Arabic (Media Streams has no `transfer_to_human` tool — same as Sinhala/Tamil).

**How to test:** call the number → press 2 → speak Arabic. Post-deploy smoke test without a phone: `POST Digits=2` to `/voice/language-selected` should return `<Stream …/ws/media-stream/ar>`; `GET/POST /voice/incoming` should render the menu with both `<Say>` blocks. Note: the Arabic menu prompt depends on Amazon Polly (`Polly.Zeina`) being enabled on the Twilio account.

### v0.16 — Sinhala re-exposed in IVR + Sinhala TTS → OpenAI gpt-4o-mini-tts
**Reason:** A client needed to test Sinhala live. Sinhala was fully coded but hidden from the IVR (since v0.15), and the operator wanted OpenAI TTS for Sinhala instead of Azure (matching Flico's Sinhala path, which handles code-switched English better).

**Changes (all in `server.py`):**
- `DIGIT_TO_LANG = {"1": "en", "2": "ar", "3": "si"}` — Sinhala re-mapped to digit 3.
- `/voice/incoming` `<Gather>` menu: added a Sinhala `<Say voice="Google.si-LK-Standard-A" language="si-LK">සිංහල සඳහා, තුන ඔබන්න.</Say>` prompt.
- Added `import audioop` (stdlib on the 3.11 image) for 24 kHz→8 kHz resample.
- Added OpenAI TTS constants: `OPENAI_TTS_URL`, `OPENAI_TTS_MODEL` (`gpt-4o-mini-tts`), `OPENAI_TTS_VOICE` (`nova`), `OPENAI_TTS_INSTRUCTIONS` (warm Kavya/Treehouse Sinhala tone) — all env-overridable.
- Added `MediaStreamSession._tts_openai()` — ported from Flico, adapted to Kavya's inline `self.ws.send_text` + `_ws_lock` style and `_is_speaking` barge-in flag (no `_active`/`_send_audio` helpers). Streams `response_format=pcm`, downsamples via `audioop.ratecv`, μ-law-encodes via `audioop.lin2ulaw`, flushes 640-byte frames, then a `mark` event.
- `_speak()` now routes `lang == "si"` → `_tts_openai`; Tamil/Arabic still → `_tts_elevenlabs`; `_tts_azure` retained but unused.
- Tamil (`"4": "ta"`) remains coded but still not in the menu.

**Deploy note:** `server.py` is baked into the image via Dockerfile `COPY` (only `chroma_db` is volume-mounted), so code changes require `docker compose up -d --build kavya` — `--force-recreate` alone does NOT pick up a new `server.py`.

**How to test:** call the number → press 3 → speak Sinhala. No-phone smoke test: `POST Digits=3` to `/voice/language-selected` returns `<Stream …/ws/media-stream/si>`; `GET/POST /voice/incoming` renders the Sinhala `<Say>`. Verified `gpt-4o-mini-tts` access with the production key (Sinhala sample → HTTP 200, ~285 KB PCM). Rollback backup: `/opt/kavya/server.py.bak.sinhala-20260623-120351`.

## graphify — GRAPH-FIRST, ALWAYS

This sub-project is part of the shared graphify knowledge graph at `../graphify-out/`
(project root). The graph covers all 5 voice agents (BSL, Kavya, SLIC, Sofia, Flico)
plus SinhalaVITS-TTS. **Use it instead of scanning the codebase** — it is faster and
consumes ~83x fewer tokens per question.

MANDATORY at the start of EVERY session, before any code exploration:
1. Read `../graphify-out/GRAPH_REPORT.md` first — god nodes, communities, and
   architecture in one read. Do NOT grep or read source files just to "get oriented".
2. For any how/where/what/why question about the code, query the graph from the project
   root BEFORE touching raw files:
   - `graphify query "<question>"`   — broad context, what connects to what
   - `graphify path "<A>" "<B>"`     — how concept A reaches concept B
   - `graphify explain "<concept>"`  — everything connected to one node
3. Open raw source files only when the graph points to a specific file/symbol and you
   need line-level detail to edit it. Never read files just to understand structure.

After modifying any code in this directory, run `graphify update .` from the project
root to keep the graph current (AST-only, no API cost).
