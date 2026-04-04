# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

Kavya is a multilingual AI voice agent for **Treehouse Chalets** (Belihuloya, Sri Lanka). Handles inbound phone calls via Twilio, uses a configurable LLM (OpenAI or Gemini via OpenAI-compatible API) for conversation and tool use, integrates with eZee Absolute PMS via n8n webhooks + browser extension for availability/booking, and grounds answers in a ChromaDB-based RAG knowledge base.

The agent persona is **Kavya** — a warm, trilingual (English, Sinhala, Tamil) reservations agent. Language is selected via IVR/DTMF menu (press 1/2/3) at call start.

**Two server modes:**
- `server.py` — **Unified production server** (IVR DTMF menu: English→ConversationRelay+ElevenLabs, Sinhala→Media Streams+Azure TTS, Tamil→Media Streams+ElevenLabs multilingual)
- `media_stream_server.py` — **Standalone Media Streams** (multilingual TTS via ElevenLabs `eleven_multilingual_v2`, Google Cloud STT, cloned voice, barge-in — kept as reference/alternative)

## Project File Map

```
Full Voice agent/
├── server.py                  # Unified production server (IVR + ConversationRelay + Media Streams)
├── media_stream_server.py     # Standalone Media Streams server (reference/alternative)
├── booking_api.py             # n8n webhook integration (availability polling, room type IDs)
├── tools.py                   # Tool definitions (Anthropic + OpenAI formats) + dispatch (4 tools)
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
├── requirements-prod.txt      # Production dependencies (openai, google-cloud-speech, no pyaudio)
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

Minimum to test locally: `OPENAI_API_KEY` (or `GEMINI_API_KEY` + `LLM_PROVIDER=gemini`) for text-only mode. Add `ELEVENLABS_API_KEY` + `ELEVENLABS_VOICE_ID` for voice output.

## Environment Setup

Copy `.env.example` to `.env`. Key groups:

**LLM provider** (pick one):
- `LLM_PROVIDER` — `"openai"` (default) or `"gemini"`
- `OPENAI_API_KEY`, `OPENAI_MODEL` — OpenAI (default model: `gpt-4o`)
- `GEMINI_API_KEY`, `GEMINI_MODEL` — Gemini via OpenAI-compatible endpoint (default model: `gemini-2.5-flash`)

**TTS/STT:**
- `ELEVENLABS_API_KEY`, `ELEVENLABS_VOICE_ID` — ElevenLabs TTS (English ConversationRelay + Tamil Media Streams)
- `AZURE_SPEECH_KEY`, `AZURE_SPEECH_REGION` — Azure TTS for Sinhala. Region: `southeastasia`
- `GOOGLE_APPLICATION_CREDENTIALS` — GCP service-account JSON for Google Cloud STT. File: `full-voice-agent-a8a245fb37cb.json`, mounted as `/app/gcp-credentials.json` in Docker

**Telephony & integrations:**
- `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN` — Production telephony
- `N8N_BASE_URL` — n8n webhook base URL (default: `https://automation.taskforceai.tech`)
- `N8N_POLL_INTERVAL`, `N8N_POLL_TIMEOUT` — Polling tuning (default: 2s interval, 60s timeout)

## Architecture

### LLM Integration

Both OpenAI and Gemini use the same `AsyncOpenAI` client. Gemini is accessed via Google's OpenAI-compatible endpoint (`https://generativelanguage.googleapis.com/v1beta/openai/`), so all streaming, tool calling, and history management code is shared. The `LLM_PROVIDER` env var controls which API key and base URL are used. Tool definitions are in OpenAI function-calling format (`get_tools_openai()` in `tools.py`).

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

Twilio manages the entire audio pipeline for English. Server only deals with text in/out. ConversationRelay does NOT support Sinhala/Tamil — Google Cloud TTS does not include `si-LK` or `ta-LK` voices in the ConversationRelay-supported set, so those languages are routed to Media Streams instead.

### Media Streams — `server.py` (Sinhala/Tamil, Press 2 or 3)

```
Sinhala/Tamil call
  -> POST /voice/incoming (returns TwiML with <Gather> DTMF menu: 1=EN, 2=SI, 3=TA)
  -> Caller presses 2 (Sinhala) or 3 (Tamil)
  -> POST /voice/language-selected (digit=2 or 3)
  -> returns TwiML with <Stream url="wss://{host}/ws/media-stream/{lang}">
  -> WebSocket /ws/media-stream/{lang}
  -> Twilio Media Streams bidirectional audio (mulaw 8kHz)
  -> Google Cloud STT (streaming, background thread, interim-based endpointing)
  -> KB retrieval + LLM streaming with tool use (native script system prompt)
  -> TTS: Sinhala → Azure (si-LK-SameeraNeural), Tamil → ElevenLabs (eleven_multilingual_v2)
  -> mulaw audio chunks streamed back to Twilio
```

TTS routing: Sinhala → Azure `si-LK-SameeraNeural` (male), Tamil → ElevenLabs `eleven_multilingual_v2` with cloned voice (`ulaw_8000` output, zero conversion). Azure called via REST API (httpx), outputs `raw-8khz-8bit-mono-mulaw` directly.

### Shared Components

**eZee PMS integration** (`booking_api.py` + `tools.py`):
Four tools: `check_availability`, `create_booking`, `retrieve_booking`, `cancel_booking`. Only `check_availability` is fully implemented via n8n async polling. Others return graceful fallback messages.

Availability flow: Kavya POSTs to n8n `/webhook/make-availability-request` → n8n queues in DataTable (`eezy-pending-requests`) → Firefox browser extension ("IPMS247 Extractor") polls `/webhook/pending-requests` → scrapes eZee web UI → POSTs result to `/webhook/availability-response` → n8n updates DataTable row (`checked=true`, `response=data`) → Kavya polls `/webhook/eezy-check-results` until response is ready.

**n8n webhook endpoints** (all under `N8N_BASE_URL = https://automation.taskforceai.tech`):
- `/webhook/make-availability-request` — Kavya submits availability check (POST)
- `/webhook/eezy-check-results` — Kavya polls for results (GET, query param `requestId`)
- `/webhook/pending-requests` — Extension polls for work (GET, filtered by `checked Is False`)
- `/webhook/availability-response` — Extension posts scraped results (POST)
- `/webhook/make-booking` — Kavya submits booking (POST, not fully implemented)

**Requires**: Browser extension running on a machine logged into `live.ipms247.com` in Firefox. Without it, availability checks timeout after `N8N_POLL_TIMEOUT` seconds.

**Knowledge base** (`knowledge_base.py`):
Files in `knowledge_docs/` chunked (500 chars, 50 overlap) -> embedded with `all-MiniLM-L6-v2` -> stored in ChromaDB (`./chroma_db`). Query embeddings LRU-cached. KB context injected as user message prefix per turn (not system prompt). Prewarm on startup. Chunk IDs are SHA-256 hashes (idempotent re-indexing).

**Local demos** (`test_voice_elevenlabs.py`, `test_voice.py`):
Typed input -> KB retrieval -> LLM tool-use loop -> text response -> TTS playback. ElevenLabs version sends full response as one TTS call (splitting into sentences causes prosody resets). Azure version splits by sentence with per-language voice selection.

## Key Design Decisions

- **Pluggable LLM**: `LLM_PROVIDER` env var switches between OpenAI and Gemini. Both use the `openai` Python library — Gemini via its OpenAI-compatible endpoint. Zero code changes needed to switch.
- **Two servers, one codebase**: `server.py` (unified production: ConversationRelay for English + Media Streams for Sinhala/Tamil) and `media_stream_server.py` (standalone Media Streams with ElevenLabs multilingual, kept as reference). Both share `booking_api.py`, `tools.py`, `knowledge_base.py`.
- **DTMF language menu**: IVR presents press 1/2/3. Press 1 routes to ConversationRelay (English + ElevenLabs). Press 2 routes to Media Streams (Sinhala + Azure TTS). Press 3 routes to Media Streams (Tamil + ElevenLabs multilingual cloned voice).
- **Interim-based STT endpointing**: Google Cloud STT often does not fire `is_final=True` results for conversational speech. The system drives endpointing from interim results: each interim overwrites `_pending_transcript` and resets a 1.5s silence timer. When the timer fires, the latest interim is treated as the complete utterance.
- **Tool gating via system prompt**: General info (rooms, rates, policies, activities) answered from KB context — no tool call. Tools only for date-specific booking operations.
- **Filler speech**: Spoken before tool execution to avoid silence during API calls.
- **max_tokens=300**: Forces concise voice-appropriate responses.
- **History trimming**: Max 20 messages. Skips orphaned `role: "tool"` messages and assistant messages with `tool_calls` at the start after trimming.
- **Native script**: LLM responds in native Sinhala/Tamil Unicode script. TTS handles native script directly.
- **Kavya persona**: Collects booking info in order: name → location (local vs foreign rates) → pax → dates → room. Mentions complimentary activities (2+ nights), April/December advance payment, honeymoon packages.
- **Hybrid TTS**: English → ElevenLabs turbo (cloned voice via ConversationRelay), Sinhala → Azure `si-LK-SameeraNeural`, Tamil → ElevenLabs `eleven_multilingual_v2` (cloned voice, `ulaw_8000` output). Azure via REST (httpx), mulaw output — no conversion.
- **Barge-in**: Media Streams only. When STT detects speech during TTS, sends `clear` event to Twilio, sets `_is_speaking = False`, increments `_speak_generation` to cancel queued TTS tasks.

## Server Endpoints

### server.py (Unified — ConversationRelay + Media Streams)
- `POST /voice/incoming` — Returns TwiML with `<Gather>` DTMF language menu (1=EN, 2=SI, 3=TA)
- `POST /voice/language-selected` — Handles DTMF result: digit=1 returns ConversationRelay TwiML, digit=2/3 returns Media Streams TwiML
- `WebSocket /ws/conversation` — Handles English ConversationRelay: `setup`, `prompt`, `dtmf`, `interrupt`
- `WebSocket /ws/media-stream/{lang}` — Handles Sinhala/Tamil Media Streams calls (Google STT + TTS)
- `GET /health` — `status`, `llm_provider`, `model`, `ezee_configured`, `kb_loaded`, `media_streams_stt`, `azure_tts`

### media_stream_server.py (Standalone Media Streams)
- `POST /voice/incoming` — Returns TwiML with `<Stream>`
- `WebSocket /ws/media-stream` — Handles: `start`, `media`, `mark`, `stop`
- `GET /health` — `status`, `mode`, `ezee_configured`, `kb_loaded`, `stt_available`, `tts_configured`, `model`

## Server Constants

- `MAX_TOKENS = 300`
- `MAX_HISTORY = 20`
- `MAX_TOOL_ROUNDS = 5`
- `ENDPOINTING_SILENCE = 1.5` (Media Streams only — seconds of silence before utterance is complete)

## System Prompt Structure

Built dynamically by `_build_system_prompt(lang)` with today's date injected and language parameter. Builds language-specific rules: native Sinhala/Tamil Unicode script for SI/TA, English-only for EN. Sections:
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
- **Media Streams TTS routing**: `_speak()` routes by language: Tamil → `_tts_elevenlabs()` (ElevenLabs `eleven_multilingual_v2`, `ulaw_8000`), Sinhala → `_tts_azure()` (Azure REST, `raw-8khz-8bit-mono-mulaw`, SSML). `_speak_lock` serializes TTS calls. `_ws_lock` serializes WebSocket writes.
- **Error handling**: LLM streaming failure sends fallback message. Missing LLM client closes WebSocket with code 1011.
- **Legacy**: `ezee_api.py` kept but not imported (used eZee Reservation API directly, requires API key we don't have). `media_stream_server.py` still uses Anthropic Claude — kept as reference but not the production server.
