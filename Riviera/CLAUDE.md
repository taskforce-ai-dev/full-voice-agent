# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this
directory.

## What This Is

**Riya** is the AI voice agent for **Riviera Resort** — a real client: a lagoon-front resort on
the Kallady lagoon, New Dutch Bar Road, Batticaloa, on Sri Lanka's east coast. She handles inbound
calls in **English, Sinhala and Tamil** over two ingresses (Twilio and the Dialog SmartPBX
"Client Connect" WebSocket), uses a configurable LLM (Claude by default, or OpenAI / Gemini) with
tool use, checks availability and takes bookings against a **dedicated Yanolja-style PMS
instance**, and grounds answers in a ChromaDB RAG knowledge base
(`knowledge_docs/riviera_info.txt`, Chroma collection `riviera_kb`).

> **Provenance — read this first.** `Riviera/` is a **full clone of `Kavya/`** taken at monorepo
> commit `6258d10` (Sep 2026), carrying every protection Kavya has (guarded immutable-image
> SmartPBX deploy, fail-closed Dialog gateway, handover failsafe, deterministic rate catalogue,
> the full test suite). It is **fully isolated**: own folder, image
> (`ghcr.io/taskforce-ai-dev/riviera`), containers (`riviera-voice-agent`, `riviera-smartpbx`),
> loopback ports (`8060` / `8061`), Chroma collection, PMS instance, hostnames
> (`riviera.taskforceai.tech`, `smartpbx-riviera.taskforceai.tech`), env prefix (`RIVIERA_*`),
> auth header (`X-Riviera-SmartPBX-Token`) and CI workflows (`build-riviera-image.yml`,
> `probe-riviera-image.yml`). Nothing here imports from, deploys with, or health-gates on Kavya
> or any other agent. **Changes to Kavya do not propagate here** and vice versa — port fixes
> deliberately, by commit, when they apply.
>
> The inherited engineering history (v0.1–v0.23: Twilio IVR, Media Streams, handover failsafe,
> intercept detection, SmartPBX hardening, capture mode, Gemini failover…) lives in
> **`../Kavya/CLAUDE.md`** and applies to this code verbatim. It is not duplicated here.

**What differs from Kavya (the Riviera delta):**
- **Identity:** persona Riya (Sinhala රියා, Tamil ரியா); property Riviera Resort; reservations
  `065 222 2164` (spoken "zero six five, two two two, two one six four"), WhatsApp `077 842 2223`.
  Voice: the client asked for a **female British** English voice — `RIVIERA_EN_ELEVENLABS_VOICE_ID`
  carries it and English TTS fails closed without it.
- **Seven room types**, single property: Family Chalet (4), Basic Room Single (2), Wooden Cabana
  (2, fan only — no A/C), Lagoon View Steel Cabana (2), Double Lagoon or Garden View (3), Triple
  Garden View (3), Family Cottage (5). The names live in **four** places that must match
  byte-for-byte: `yanolja_service.ROOM_TYPES_BY_PROPERTY`, `tools.ROOM_TYPES_BY_PROPERTY`,
  `post_call.ROOM_TYPES_BY_PROPERTY`, and `room_types.name` in the PMS. `_match_room_type` gained a
  final "all query words in exactly one name" stage so "steel cabana" resolves and a bare "cabana"
  is ambiguous (Riya asks which).
- **Rate model** (`yanolja_service.py` RATE CARD block + `rate_catalog.py`): Sri Lankan
  **resident** rates only, **LKR, room only, inclusive of all taxes**, in two **date-range
  seasons** (mid 2026-09-01→2027-06-30, high 2027-07-01→2027-08-31; `RATE_SEASONS`,
  `season_for_stay`). Meal plans are per-person per-day supplements (`MEAL_PLANS_LKR`: BB 2,000 /
  child 1,500; HB 4,500 / child 3,000). **No foreign rate card exists** —
  `resolve_rate(residency="foreign")` returns `reason="foreign_rates_unpublished"` and Riya refers
  the guest to reservations; mixed-season stays and dates outside the published periods are
  likewise non-quotable. Tool results carry `rate_per_night_lkr` / `total_lkr` / `season`.
  Kill switch `RATES_ENABLED` (legacy alias `DEMO_RATES_ENABLED` still honoured; the Python
  constant is still `DEMO_RATES_ENABLED`).
- **Languages on both ingresses.** Twilio: `IVR_MENU_ENABLED` defaults to `true`,
  `DIGIT_TO_LANG = {"1": "en", "2": "si", "3": "ta"}`, three `<Say>` prompts, and
  `/ws/media-stream/{lang}` accepts `si`/`ta` (Arabic stays coded but unlisted). Dialog SmartPBX:
  `RivieraSmartPBXSession.feed_dtmf` maps `3` → a **Tamil profile** (`_resolve_language_profile`:
  Google `ta-IN` STT, primary LLM, ElevenLabs multilingual TTS via the shared `_speak` route) in
  addition to Kavya's English (`1`) and Sinhala (`2`) profiles; `_resolve_language_profile` now
  raises on any other language instead of silently building the Sinhala profile.
  **The committed `smartpbx_language_menu.ulaw` still only announces English and Sinhala** —
  regenerate it with `scripts/generate_smartpbx_language_menu.py` (needs provider credentials,
  run off-box) before the first Tamil canary call.
- **Prompt facts** (`_build_system_prompt`): lagoon/Batticaloa persona; seven-room section with
  capacities and the no-A/C warning for the Wooden Cabana; rates section rewritten for
  room-only LKR + meal plans + foreign-guest referral; ACTIVITIES rule (kayaking, cycling, lagoon
  cruise, singing fish ride — prices confirmed by the resort); STAY BASICS and DEPOSIT /
  CANCELLATION say "reservations will confirm" because the client has not supplied those terms
  yet — **never invent check-in times, deposits or cancellation percentages.**
- **Deploy script isolation:** `scripts/deploy_smartpbx_image.sh` checks only
  `SMARTPBX_ISOLATION_NEIGHBOURS` (default `riviera-voice-agent`) before/after a recreate, so a
  Riviera deploy never depends on Kavya's or Flico's container being healthy.
- **PMS:** Riviera needs its **own** PMS instance (the schema has no property column) —
  `ops/riviera-pms/` has the seed SQL, runbook and live verifier. Until it exists, availability
  returns zero room types (by design: `_property_of` filters unknown names).
- **Dropped from the clone:** `legacy_pms/`, `ops/mosvold-pms/`, `ops/hattonhills-pms/`, the
  Vercel dashboard demo files, the Hatton `.firecrawl` cache and the Treehouse logo.
  `kpms_service.py`, `media_stream_server.py` and the local `test_voice*.py` demo CLIs are
  inherited dead/reference code and still carry Kavya's Mosvold-era demo prompts inside — exactly
  as they do in `Kavya/`; nothing imports them.

**Knowledge sources.** The KB (`knowledge_docs/riviera_info.txt`) merges the client's
*HOSPITALITY BOOKING AGENT REQUIREMENTS* + *RATES* sheets (Sep 2026) with a full scrape of
riviera-online.com (15 pages, 2026-09-14): fact sheet (check-in 2 pm / check-out 12 noon,
one-night deposit, 48-hour cancellation, late-departure fees, accepted cards), dining, pool and
facilities, activities, sustainability, Batticaloa sights and contact details. The prompt's STAY
BASICS and DEPOSIT AND CANCELLATION rules state those terms. Room-name reconciliation: the
website's Standard Double / Standard Triple / AC Lagoon View Unit / Budget Single map to the
rate sheet's Double Lagoon or Garden View / Triple Garden View / Lagoon View Steel Cabana /
Basic Room Single; the website's Budget Double Room and Riviera Residence are NOT on the rate
sheet and are referred to reservations, not booked.

**Still to obtain from the client:** extra-adult / extra-child supplements, activity and menu
prices, foreign-guest rates, the room number of the Family Cottage (the sheet lists "Rm 9"
twice), and confirmation that the rate sheet's "Basic Room - Single" is the website's fan-only
Budget Single Room (the KB and prompt currently say fan-cooled, from the website).

**Two server modes:**
- `server.py` — **Unified production server**: the Twilio service (IVR `<Gather>` → English
  ConversationRelay, Sinhala/Tamil Media Streams) and the opt-in Direct SmartPBX service with its
  call-local `1` English / `2` Sinhala / `3` Tamil profiles.
- `media_stream_server.py` — **Standalone Media Streams** (Anthropic Claude, ElevenLabs
  multilingual TTS, Google Cloud STT, barge-in — kept as reference/alternative)

This "two server modes" split is orthogonal to a second gate: `server.py` builds one of **two
mutually exclusive service-mode apps** (Twilio vs Dialog SmartPBX) via `RIVIERA_SERVICE_MODE` —
see **Service Modes** below.

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

> **WARNING -- Twilio service only.** Everything below (`deploy.sh`, `docker compose build`, the VPS at `riviera.taskforceai.tech`) targets the `riviera` (Twilio) service. **None of it touches `riviera-smartpbx`.** `deploy.sh`'s `PROD_FILES` list does not even include `scripts/`, `nginx-smartpbx*.conf`, or `SMARTPBX_RUNBOOK.md`, and building on the VPS is the documented 2026-08-02 resource-starvation failure mode -- see the compose file's own header. SmartPBX has its own reviewed image pipeline (`build-riviera-image.yml` -> `scripts/deploy_smartpbx_image.sh`); follow `SMARTPBX_RUNBOOK.md`, never this section, for any SmartPBX change.

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
docker compose logs -f riviera

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
- `ANTHROPIC_API_KEY`, `CLAUDE_MODEL` — Claude/Anthropic (default model: `claude-sonnet-4-5-20250929`)
- `OPENAI_API_KEY`, `OPENAI_MODEL` — OpenAI (default model: `gpt-4o`)
- `GEMINI_API_KEY`, `GEMINI_MODEL` — Gemini via native google-genai SDK (default model: `gemini-2.5-flash`)

### Direct SmartPBX Sinhala profile

Direct SmartPBX settings belong only in protected, root-owned
`/opt/riviera/.env.smartpbx`. Its `riviera-smartpbx` Compose service uses an
explicit environment allowlist and must not receive Twilio credentials or
`HUMAN_AGENT_PHONE`.

- Press `1` retains the existing English profile exactly: its configured
  provider/model, English prompt, tools, timing, capture, handover behavior,
  configured English STT, and canonical ElevenLabs route. Populating Sinhala
  settings does not build a Gemini client or mutate that English call profile.
- Press `2` is call-local: Azure STT at `si-LK`, Gemini 3.7 Flash LLM with
  `low` thinking and a bounded 1024-token output ceiling, and Gemini 3.1 Flash
  TTS (`gemini-3.1-flash-tts-preview`, `Vindemiatrix`).
- The Compose-rendered defaults are
  `SMARTPBX_SINHALA_LLM_PROVIDER=gemini`,
  `SMARTPBX_SINHALA_GEMINI_LLM_MODEL=gemini-3.7-flash`,
  `SMARTPBX_SINHALA_GEMINI_THINKING_LEVEL=low`, and
  `SMARTPBX_SINHALA_GEMINI_MAX_TOKENS=1024`. At runtime, a blank provider
  resolves to `gemini`; an invalid nonblank provider resolves to `claude`;
  thinking accepts `low`, `medium`, or `high` and otherwise resolves to `low`;
  and the token ceiling defaults to 1024 and clamps to `[200, 1024]`.
- The only operator rollback is
  `SMARTPBX_SINHALA_LLM_PROVIDER=claude`. It changes the Sinhala LLM only; it
  does not alter global `LLM_PROVIDER`, the Twilio service, or an English
  SmartPBX call. Azure `si-LK` STT and Gemini TTS remain selected.
- A nonblank `GEMINI_API_KEY` is a protected SmartPBX Sinhala provisioning
  concern for the default Gemini LLM and Gemini TTS. The bilingual menu is
  deliberately withheld when that key is missing or whitespace-only, including
  from a caller who would have selected Press `1`. Never put it in tracked files
  or print it. Chirp and Gemini Transcribe are not part of this rollout.
- Gemini Sinhala TTS has a quota-aware model fallback chain (2026-09-04):
  `SMARTPBX_SINHALA_GEMINI_TTS_MODEL` (primary) then
  `SMARTPBX_SINHALA_GEMINI_TTS_FALLBACK_MODELS` (comma list, default
  `gemini-2.5-flash-preview-tts,gemini-2.5-pro-preview-tts`), same client and
  voice, tried in order only on a classified `quota_exceeded`/`rate_limited`
  error, never for any other failure. A model that hits quota/rate-limit is
  skipped (sticky per process) for the rest of that quota day and restored at
  `SMARTPBX_SINHALA_TTS_MODEL_RESET_UTC_HOUR` (default `7`, i.e. `07:00` UTC).
  `/smartpbx/status` exposes the currently active model as `sinhala_tts_model`;
  see `SMARTPBX_RUNBOOK.md`'s Monitoring section for the full contract.

**TTS/STT:**
- `ELEVENLABS_API_KEY`, `ELEVENLABS_VOICE_ID` — ElevenLabs TTS (English ConversationRelay + Tamil Media Streams)
- `OPENAI_API_KEY` — OpenAI key; used by the historical Twilio Sinhala `gpt-4o-mini-tts` path and, if `LLM_PROVIDER=openai`, the LLM
- `OPENAI_TTS_MODEL` / `OPENAI_TTS_VOICE` / `OPENAI_TTS_INSTRUCTIONS` — historical Twilio Sinhala TTS config (defaults: `gpt-4o-mini-tts`, `nova`, a warm Riya-at-Riviera Sinhala-tone instruction)
- `AZURE_SPEECH_KEY`, `AZURE_SPEECH_REGION` — Azure STT backend and legacy Twilio Sinhala Azure TTS path (`_tts_azure`, no longer live). Region: `southeastasia`
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

### Media Streams — `server.py` (Sinhala = Press 2, Tamil = Press 3)

```
Non-English call
  -> POST /voice/incoming (returns TwiML with <Gather> DTMF menu: 1=EN, 2=SI, 3=TA)
  -> Caller presses 2 (Sinhala) or 3 (Tamil)   [Arabic code path exists but no menu digit maps to it]
  -> POST /voice/language-selected -> DIGIT_TO_LANG.get(digit, "en")  (digit 2 -> "si", digit 3 -> "ta")
  -> returns TwiML with <Stream url="wss://{host}/ws/media-stream/{lang}">
  -> WebSocket /ws/media-stream/{lang}   ({lang} guard accepts si/ta; ar is refused)
  -> Twilio Media Streams bidirectional audio (mulaw 8kHz)
  -> Google Cloud STT (streaming, background thread, interim-based endpointing)
  -> KB retrieval + LLM streaming with tool use (native script system prompt)
  -> TTS: Sinhala → OpenAI (gpt-4o-mini-tts, nova); Tamil → ElevenLabs (eleven_multilingual_v2); [Arabic → ElevenLabs, dormant]
  -> mulaw audio chunks streamed back to Twilio
```

TTS routing (`_speak`): `lang in ("ta", "ar")` → ElevenLabs `eleven_multilingual_v2` (cloned voice, `ulaw_8000`, zero conversion); `lang == "si"` → OpenAI `gpt-4o-mini-tts` (`_tts_openai`, voice `nova`) — returns 24 kHz PCM, downsampled on the fly to 8 kHz μ-law via `audioop`; the legacy Azure `si-LK-SameeraNeural` path (`_tts_azure`) is still wired but no longer the live Sinhala route.

### Shared Components

**eZee PMS integration** (`booking_api.py` + `tools.py`):
Four tools: `check_availability`, `create_booking`, `retrieve_booking`, `cancel_booking`. `check_availability` and `create_booking` are fully implemented via n8n async polling (same submit-and-poll helper, different submit webhook). `retrieve_booking` and `cancel_booking` return graceful fallback messages.

Availability flow: Riya POSTs to n8n `/webhook/make-availability-request` → n8n queues in DataTable (`eezy-pending-requests`) → Firefox browser extension ("IPMS247 Extractor") polls `/webhook/pending-requests` → scrapes eZee web UI → POSTs result to `/webhook/availability-response` → n8n updates DataTable row (`checked=true`, `response=data`) → Riya polls `/webhook/eezy-check-results` until response is ready.

**n8n webhook endpoints** (all under `N8N_BASE_URL = https://automation.taskforceai.tech`):
- `/webhook/make-availability-request` — Riya submits availability check (POST)
- `/webhook/eezy-check-results` — Riya polls for results (GET, query param `requestId`)
- `/webhook/pending-requests` — Extension polls for work (GET, filtered by `checked Is False`)
- `/webhook/availability-response` — Extension posts scraped results (POST)
- `/webhook/make-booking` — Riya submits booking (POST). Same async-polling pattern as availability: extension picks up the row, creates the reservation in eZee, writes the confirmation number back into the `response` field of the same DataTable row. Riya polls `/webhook/eezy-check-results` until populated.
- `/webhook/post-call-data` — Riya POSTs call summary + transcript after each call (POST, → Google Sheets)

**Requires**: Browser extension running on a machine logged into `live.ipms247.com` in Firefox. Without it, availability checks timeout after `N8N_POLL_TIMEOUT` seconds.

**Knowledge base** (`knowledge_base.py`):
Files in `knowledge_docs/` chunked (500 chars, 50 overlap) -> embedded with `all-MiniLM-L6-v2` -> stored in ChromaDB (`./chroma_db`). Query embeddings LRU-cached. KB context injected as user message prefix per turn (not system prompt). Prewarm on startup. Chunk IDs are SHA-256 hashes (idempotent re-indexing).

**Post-call data capture** (`post_call.py`):
When a call ends (WebSocket disconnect), `server.py` fires `asyncio.create_task(process_post_call_data(...))` to run post-call processing in the background. The flow: format the full transcript → call LLM (same provider as the conversation) to extract structured booking details (guest name, dates, room preference, outcome, follow-up needed, summary) → POST JSON payload to n8n webhook `/webhook/post-call-data` → n8n appends a row to Google Sheet "Riya Call Log". Caller phone number is captured from Twilio HTTP POST params (`From`) via a module-level `_call_phone` dict bridge between HTTP handlers and WebSocket handlers. A separate `full_transcript` list (never trimmed) accumulates all user/assistant messages alongside the trimmed `conversation_history`. All errors are caught and logged — post-call failures never affect the call or server stability. Env var: `N8N_POSTCALL_WEBHOOK` (default: `/webhook/post-call-data`).

**Google Sheet columns** (n8n workflow "Post-Call Data to Google Sheets"): Date/Time, Call SID, Language, Caller Phone, Guest Name, Location, Guests, Check-In, Check-Out, Room Preference, Availability, Outcome, Follow-Up Needed, Summary, Transcript.

**Local demos** (`test_voice_elevenlabs.py`, `test_voice.py`):
Typed input -> KB retrieval -> LLM tool-use loop -> text response -> TTS playback. ElevenLabs version sends full response as one TTS call (splitting into sentences causes prosody resets). Azure version splits by sentence with per-language voice selection.

## Service Modes

`server.py` builds **one of two mutually exclusive FastAPI apps**, selected by `RIVIERA_SERVICE_MODE` (env var, `"twilio"` default) via `build_service_app(service_mode, environ)` at the bottom of the file. The two modes never run in the same process — `build_service_app` returns exactly one `FastAPI` instance, and `lifespan()` skips all Twilio REST-client / handoff startup when the mode is `smartpbx`.

**`twilio` (default) — production.** Everything documented above (ConversationRelay, Media Streams, `/voice/*`, Twilio `<Dial>` handover) is `app` unchanged. This serves Riviera's Twilio number and **remains the default until a deliberate Dialog cutover is decided and executed** via `SMARTPBX_RUNBOOK.md`.

**`smartpbx` — Dialog SmartPBX ("Client Connect") ingress, opt-in.** A second, narrower FastAPI app is built instead (`docs_url`/`redoc_url`/`openapi_url` all disabled), exposing exactly three routes:
- `GET /health` — `{"status": "ok", "service_mode": "smartpbx"}`
- `GET /smartpbx/status` — session counters (`active_sessions`, `admitted_total`, `rejected_capacity_total`, `released_total`, `frames_dropped_total`), `enabled`, `configured`, `protocol_version`, `transfer_enabled` — no secrets, no PII. **Requires the same `X-Riviera-SmartPBX-Token` header as the media socket** (constant-time compare): the counters are a live occupancy oracle and a call-volume signal, so they are not publicly readable. `/health` stays unauthenticated for liveness probes; point uptime monitoring there.
- `WS /ws/v1/smartpbx/media` — the Dialog media socket, gated by a required `X-Riviera-SmartPBX-Token` header (constant-time compare) checked before `websocket.accept()`

Protocol version is `smartpbx-ai-provider-v07`. Audio is exact `g711_ulaw` at `8000` Hz only — any other codec/rate is rejected at the `start` event. Capacity is hard-capped at **4 concurrent calls** (a 5th is rejected before the socket is even accepted; `SmartPBXSessionRegistry` cannot be constructed outside 1–4).

**Module map** (`Riviera/smartpbx_*.py`):
- `smartpbx_protocol.py` — strict, transport-independent parser for the Dialog wire events (`connected`/`start`/`media`/`dtmf`/`hangup`/`stop`, else `Unsupported`) into a closed dataclass union; fail-closed on anything malformed.
- `smartpbx_gateway.py` — `SmartPBXSettings` (env validation), `SmartPBXSessionRegistry` (the 4-call admission counter), and `SmartPBXGateway` (auth → admit → start session → event loop → cleanup-once, emitting the `smartpbx_protocol_diagnostic` log line).
- `smartpbx_transport.py` — `SmartPBXMediaTransport`, the bounded outbound audio queue serializing `media` frames back to Dialog. Frames are **paced at realtime** so barge-in has queued audio left to cancel (Dialog defines no `clear` wire event); on overflow it **refuses the newest frame**, cutting the tail of a reply rather than decimating it; generation-fenced so barge-in can't leak stale audio; and a dead sender raises a failure signal so the gateway ends the call instead of leaving the guest in silence.
- `smartpbx_session.py` — `RivieraSmartPBXSession`, the adapter that first resolves a call-local English, Sinhala or Tamil language profile (static menu digits `1`/`2`/`3`), then wires it into one Dialog call's `MediaStreamSession` pipeline (STT → KB/PMS tools → LLM → TTS) and binds transfer/handover context. It does not mutate process-global provider or model state.
- `smartpbx_mcp.py` — fail-closed Dialog MCP call control: `DialogMCPSettings.from_env()` and `DialogMCPCallControl.transfer_call()`, restricted to operator-configured `tel:`/`sip:` destinations.
- `smartpbx_handover.py` — `SmartPBXHandoverCoordinator`, the call-local state machine that attempts the MCP transfer and, on failure, falls back to the existing WhatsApp handover notification.
- `smartpbx_diagnostics.py` — the enum vocabulary (`DiagnosticStage`/`DiagnosticOutcome`/`DiagnosticFailureClass`) for the seven-field diagnostic log line.

**Handover in SmartPBX mode.** Twilio `<Dial>`/REST redirect/dial-status callbacks do not exist on a Dialog call, so they are not reused. `transfer_to_human` instead invokes the Dialog MCP `transfer_call` tool against the call's `otherLegCallId`. The path is fail-closed end to end: MCP endpoint/API key/account ID/account-header spelling are environment-only, `SMARTPBX_TRANSFER_DESTINATIONS_JSON={}` disables transfer entirely (the base/default state), validation/auth failures never retry, and bounded network/server failures retry once. If the MCP transfer does not succeed, `SmartPBXHandoverCoordinator` reuses the existing `handover.py` WhatsApp notification path (same `normalize_whatsapp`, same n8n webhook shape) as an operational fallback — it notifies the manager but is never treated as evidence of a successful live transfer.

**Deployment shape.** SmartPBX runs as a separate Compose profile (`docker-compose.yml`, profile `smartpbx`, service `riviera-smartpbx`) alongside — not instead of — the existing `riviera` Twilio service: its own container, its own loopback port `127.0.0.1:8061`, its own Chroma volume (`./chroma_db_smartpbx`, never shared with the Twilio service's writable store), and an explicit environment allowlist (no `env_file: .env` — Twilio credentials and `HUMAN_AGENT_PHONE` must never reach this container). The image tag is pinned via `SMARTPBX_IMAGE_TAG` (default `disabled`, which cannot pull anything); secrets live in root-only `/opt/riviera/.env.smartpbx` (`chmod 600`), never `.env`. Public TLS terminates at a dedicated Nginx vhost, `smartpbx-riviera.taskforceai.tech`, in front of the loopback port. Image provenance is enforced in CI: `.github/workflows/build-riviera-image.yml` (publisher) and `probe-riviera-image.yml` (read-only probe) gate which image tag/digest is trustworthy to deploy — the runbook's guarded deploy script cross-checks the reviewed short SHA against the image's OCI revision label before recreating the container.

**Direct SmartPBX Sinhala LLM recovery.** Gemini-to-Claude fallback is
technical recovery only and remains call-local while preserving provider and
tool state. A round that has delivered audio or produced a tool side effect is
fenced and is not replayed; recovery never authorizes repeat booking
operations. The technical failure classification is deliberately narrow, not
an assertion that every Gemini exception falls back. Diagnostics and
acceptance evidence are privacy-safe metadata only: they do not retain caller
transcript text, prompts, tool arguments/results, audio, API keys, headers, or
raw provider exceptions.

**Direct SmartPBX Sinhala conversational polish (2026-09-04 tester feedback).**
Three fixes from live pilot calls, all Sinhala-only:
- **Filler variety.** The single fixed initial-filler phrase (and the fixed
  per-tool `MEDIA_STREAM_FILLERS["si"]` phrases) are now each a small bank
  of 2-4 short, warm, colloquial variants (`SMARTPBX_SINHALA_INITIAL_FILLER_BANK`,
  `SMARTPBX_SINHALA_TOOL_FILLER_BANKS`, `SMARTPBX_SINHALA_DEFAULT_FILLER_BANK`),
  rotated per turn without an immediate repeat by the same per-session
  `_CallFillerRotation` the English SmartPBX path already uses. Every variant
  is on the `SMARTPBX_SINHALA_CACHED_PHRASES` prewarm allowlist — the initial
  filler only ever offers phrases whose audio is already cached (a live
  Gemini TTS round trip would hold the speak lock through the 2-5 s it takes);
  the tool filler is not gated on cache readiness since it already runs
  concurrently with its tool, not in front of it. The `check_availability`
  filler's leading word typo (`ඇ දිනවල` → `ඒ දිනවල`, "those dates") is fixed.
- **Filler frequency.** `SMARTPBX_SINHALA_INITIAL_FILLER_DELAY_SECONDS`
  (default `2.2`, clamp `[0.5, 5.0]`) replaces the shared English delay for
  the Sinhala profile only — Gemini's first token is typically 1.2-1.5 s
  (3.9 s throttled), so the shared 1.5 s English delay fired on most turns.
  A per-session "last filler spoke at" timestamp additionally suppresses a
  second initial filler within 15 s of the last one UNLESS the configured
  delay itself exceeds 3.5 s (`_smartpbx_sinhala_filler_suppressed_by_repeat`)
  — a long configured wait is trusted to be genuinely slow and always speaks.
- **Keypad wording.** `SMARTPBX_SINHALA_KEYPAD_PROMPTS` now says "keypad" in
  English alongside the Sinhala phrase (testers found the plain Sinhala word
  unfamiliar); the hash-key instruction and the prewarm allowlist membership
  are unchanged.
- **Room-name recognition.** `SI_STT_PHRASE_LIST` gained the five room names,
  their component English words, and common Sinhala transliterations, biasing
  Azure `si-LK` STT toward them (mirroring the existing number-word bias).
  The Sinhala system prompt gained a compact "ROOM NAME HINTS" block mapping
  likely mis-hearings (e.g. `ස්විෆ්ට්/ස්වීට් = Suite`) to the five room types,
  instructing Riya to confirm by name rather than guess when unsure.
English profiles, Twilio Media Streams, and every other Sinhala policy are
untouched by this change.

**Direct SmartPBX Sinhala fixed-phrase prewarm: persistent cache + pacing
(2026-09-04, rate-limit incident).** Live evidence at an 11:00 UTC container
start showed `sinhala_phrase_prewarm rendered=13 total=19 ready=false` after
19 back-to-back Gemini TTS requests within ~1 minute -- Gemini TTS on this
project has a 100 requests/day cap and ~10 requests/minute cap, so the burst
both tripped the per-minute limit (the 6 failures) and spent ~19% of the
daily budget on every container restart, with no per-phrase failure reason
logged. Two fixes:
- **Persistent cache.** Rendered mu-law audio is now written to
  `SMARTPBX_SINHALA_PHRASE_CACHE_DIR` (default `/app/smartpbx_phrase_cache`,
  bind-mounted `./smartpbx_phrase_cache` in `docker-compose.yml` -- same
  ownership pattern as `chroma_db_smartpbx`), keyed by a sha256 hash of
  `(model, voice, text)`; file contents are raw mu-law bytes only, never the
  phrase text. Startup loads every allowlisted phrase from disk first and
  only synthesises the misses. Blank disables disk persistence (in-memory
  only, the pre-2026-09 behaviour); deleting the directory just costs one
  re-render per phrase.
- **Paced, classified prewarm.** Misses render sequentially with a minimum
  spacing (`SMARTPBX_SINHALA_PREWARM_INTERVAL_SECONDS`, default `7.0`, clamp
  `[0, 60]`), keeping a cold start under ~9 requests/minute. A classified
  `rate_limited` error backs off (doubling the spacing, capped at 60 s, up to
  3 retries per model before moving to the next one in the fallback chain).
  A classified `quota_exceeded` error stops the whole run immediately, marks
  that model exhausted via the existing `SMARTPBX_SINHALA_TTS_MODEL_RESET_UTC_HOUR`
  chain state, and leaves the rest to the next scheduled prewarm. Prewarm now
  uses the same model fallback chain as live calls -- a phrase rendered on a
  fallback model is cached under that model's key and is just as servable at
  playback (`_get_cached_smartpbx_sinhala_phrase_audio` searches the whole
  chain) since the voice is identical.
- **Observability.** The summary line gained
  `loaded_from_disk=N synthesised=N failed=N failure_codes=quota_exceeded:1,rate_limited:2 elapsed_ms=…`;
  each failed phrase logs its allowlist INDEX and bounded code, never its
  text. `/smartpbx/status` gained `sinhala_phrases_ready`/`sinhala_phrases_total`.
- **Re-prewarm.** The existing "retry on next Sinhala activation" behaviour
  is now debounced to at most once per 10 minutes (since with a persistent
  cache "not ready" can mean "quota exhausted for the day", not just "cold
  process"), plus one forced re-prewarm attempt at the daily quota reset
  boundary (reusing `SMARTPBX_SINHALA_TTS_MODEL_RESET_UTC_HOUR`).
See `SMARTPBX_RUNBOOK.md`'s "Sinhala fixed-phrase prewarm" section for the
full operational contract. The initial-filler and tool-filler banks remain
usable as soon as at least one variant of that bank is cached -- `ready`
stays true only once every phrase is rendered.

**History is rendered per provider at the request boundary.** `self.history` is
written in whichever provider's shape ran the round, so after one Gemini tool
round it holds OpenAI-shaped `assistant.tool_calls` / `role: "tool"` entries —
which Anthropic 400s on, and (before this) turned one transient Gemini error
into a dead call, because the sticky counter then routed every later turn to the
provider that was rejecting our payload. `_claude_messages_from_history()`
renders any mixed history into valid Anthropic Messages input at both Claude
request sites (`_run_llm_claude`, `_run_llm_streaming_claude`); it is an
identity pass for an already-Anthropic history, preserves tool_use/tool_result
pairing and ids, drops an unanswered tool call rather than **ever** fabricating a
result, and never carries a Gemini thought signature across providers.
`_history_to_gemini()` reads Anthropic content-block entries for the reverse
direction, so the turn after a Claude failover tool round still runs on Gemini.
A failover turn that fails for our own reason speaks the shared localized
recovery line (post-tool variant when that turn already committed a tool round,
so recovery never invites a repeat booking) and **rolls back the recorded
failover** (`_rollback_gemini_failover`) — our own errors must never latch
`degraded` and pin the call to the provider that just failed.

**Full cutover/rollback procedure:** `SMARTPBX_RUNBOOK.md` — preconditions and immutable image identity, `.env.smartpbx` provisioning, TLS bootstrap, the five cutover gates (bad/missing auth rejected, bidirectional audio + LLM turn, KB/PMS answer + post-call record, 4-accepted/5th-rejected capacity, endpoint-down fallback), optional transfer drill with compulsory revoke, and drain-before-stop withdrawal/rollback.

## Key Design Decisions

- **Pluggable LLM**: `LLM_PROVIDER` env var switches between Claude (default), OpenAI, and Gemini. Each has its own client singleton, streaming functions, and tool format. History is stored in provider-native format; Gemini uses a converter `_history_to_gemini()` since it stores in OpenAI format internally.
- **Claude as default**: Anthropic Claude (`claude-sonnet-4-5-20250929`) is the default and primary tested provider. Uses native `AsyncAnthropic` SDK with `messages.stream()`, content block events (`content_block_start`, `content_block_delta`, `content_block_stop`), and Anthropic tool format (`input_schema`).
- **Two servers, one codebase**: `server.py` (unified production) and `media_stream_server.py` (standalone, Anthropic-only, kept as reference). Both share `booking_api.py`, `tools.py`, `knowledge_base.py`.
- **Twilio DTMF language menu**: Press 1 → ConversationRelay (English + ElevenLabs); Press 2 → Media Streams (Sinhala + OpenAI `gpt-4o-mini-tts`); Press 3 → Media Streams (Tamil + ElevenLabs multilingual). `DIGIT_TO_LANG = {"1": "en", "2": "si", "3": "ta"}`; no input → English. Arabic (Media Streams + ElevenLabs) remains coded but unmapped. This does not govern Direct SmartPBX, whose menu/profile boundary is documented above.
- **Interim-based STT endpointing**: Google Cloud STT rarely fires `is_final=True` for conversational speech. Each interim result overwrites `_pending_transcript` (not appends) and resets a `STT_ENDPOINTING_SILENCE_SECONDS` timer. `STT_ENDPOINTING_SILENCE_SECONDS` defaults to `1.0` and clamps to `[0.2, 5.0]`. `STT_FINAL_GRACE_SECONDS` defaults to `0.5` and clamps to `[0.05, 5.0]`.
- **Tool gating via system prompt**: General info (rooms, rates, policies, activities) answered from KB context — no tool call. Tools only for date-specific booking operations.
- **Filler speech**: Spoken before tool execution to avoid silence during API calls (language-specific fillers for Sinhala/Tamil).
- **max_tokens=300**: Forces concise voice-appropriate responses.
- **History trimming**: Max `MAX_HISTORY_MESSAGES` messages (`60`). `_trim_history()` is format-aware — detects and skips orphaned tool result messages at the start of trimmed history for both Anthropic format (user messages containing `tool_result` content blocks) and OpenAI format (`role: "tool"` messages). Also skips orphaned assistant `tool_use`/`tool_calls` messages.
- **Native script**: LLM responds in native Sinhala/Tamil Unicode script. TTS handles native script directly.
- **Riya persona**: Collects booking info in order: dates → pax → room → residency (asked once, only when quoting) → full name → mobile. Mentions lagoon activities (kayaking, cycling, lagoon cruise, singing fish ride) when asked; never invents times, deposits, cancellation terms or supplements the client has not supplied.
- **Twilio historical hybrid TTS**: English → ElevenLabs turbo (cloned voice via ConversationRelay), Sinhala → OpenAI `gpt-4o-mini-tts` (voice `nova`, 24 kHz PCM → 8 kHz μ-law via `audioop`), Tamil → ElevenLabs `eleven_multilingual_v2` (cloned voice, `ulaw_8000` output). Legacy Azure `si-LK-SameeraNeural` Sinhala path (`_tts_azure`) is wired but no longer that live Twilio route. Direct SmartPBX Sinhala instead uses its call-local Gemini TTS route; Direct SmartPBX Tamil uses the same ElevenLabs multilingual route as Twilio.
- **Barge-in**: Media Streams only. When STT detects speech during TTS, sends `clear` event to Twilio, sets `_is_speaking = False`, increments `_speak_generation` to cancel queued TTS tasks. Thresholds are driven by `BARGEIN_MIN_CHARS` (default `12`, clamp `[0, 200]`) and `BARGEIN_DEBOUNCE_SECONDS` (default `0.6`, clamp `[0.0, 5.0]`).

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

### server.py — SmartPBX service mode (`RIVIERA_SERVICE_MODE=smartpbx`, opt-in — see Service Modes above)
- `GET /health` — `{status, service_mode}` only (no LLM/KB/STT flags — those belong to the Twilio app)
- `GET /smartpbx/status` — session counters + `transfer_enabled`; requires the `X-Riviera-SmartPBX-Token` header (401 without it)
- `WebSocket /ws/v1/smartpbx/media` — Dialog media socket, requires `X-Riviera-SmartPBX-Token` header, `g711_ulaw`/8000 Hz only, events `connected`/`start`/`media`/`dtmf`/`hangup`/`stop`

## Server Constants

- `MAX_TOKENS = 300`
- `MAX_HISTORY_MESSAGES = 60`
- `MAX_TOOL_ROUNDS = 5`
- `STT_ENDPOINTING_SILENCE_SECONDS = 1.0` (clamp `[0.2, 5.0]`)
- `STT_FINAL_GRACE_SECONDS = 0.5` (clamp `[0.05, 5.0]`)
- `DTMF_INTERDIGIT_TIMEOUT_SECONDS = 6.0` (clamp `[1.0, 30.0]`)
- `DTMF_OVERALL_TIMEOUT_SECONDS = 30.0` (clamp `[5.0, 120.0]`)
- `DTMF_MAX_DIGITS = 15` (clamp `[1, 40]`)
- `BARGEIN_MIN_CHARS = 12` (clamp `[0, 200]`)
- `BARGEIN_DEBOUNCE_SECONDS = 0.6` (clamp `[0.0, 5.0]`)

## System Prompt Structure

Built dynamically by `_build_system_prompt(lang)` with today's date injected and language parameter. Sections:
1. **Persona**: Riya, reservations agent for Riviera Resort (seven room types, reservations number, today's date)
2. **Language rules**: Language-specific (determined by IVR selection), native script for Sinhala/Tamil, Sinhala room-name hints for the Riviera rooms
3. **Voice rules**: Short sentences, no markdown/bullets/URLs, numbers as words, one question at a time
4. **Room types / rates / data security / booking rules**: KB is the source of truth for room facts; room-only LKR rates by season with meal plans separate; foreign guests referred to reservations; single-call availability rule; spoken-number capture contract; stay basics and cancellation deferred to reservations

## Operational Details

> **WARNING -- Twilio service only.** The deployment details below (`deploy.sh`, VPS builds, `riviera.taskforceai.tech`) are for the `riviera` (Twilio) service and **must not be used for `riviera-smartpbx`**. SmartPBX deploys only through the reviewed image pipeline described in `SMARTPBX_RUNBOOK.md`.

- **Deployment**: Dockerfile (`python:3.11-slim`), `docker-compose.yml`, `nginx.conf` (SSL + WSS + rate limiting), `requirements-prod.txt`, `deploy.sh`. Target: DigitalOcean VPS at `67.207.90.109` (`riviera.taskforceai.tech`). Docker CMD runs `server:app`. Docker port `127.0.0.1:8060` (nginx-only). Single uvicorn worker (sentence-transformers uses ~400MB-1GB RAM).
- **Deployment DNS**: Cloudflare proxy OFF / DNS only for direct SSL. Call routing (to be set up): resort line → Twilio number → Riya, and/or Dialog Client Connect → `smartpbx-riviera`.
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
- **Twilio/SmartPBX env loading asymmetry:** `riviera` uses `env_file: .env`; `riviera-smartpbx` uses an explicit allowlist under `environment:` and should not use `env_file` in compose. This is a deliberate trap-prevention design: any var only present in `.env.smartpbx` is ignored unless copied into the `riviera-smartpbx` allowlist.
- **How to verify the allowlist trap:** from repo root: `cd Riviera && docker compose config | rg -n "riviera-smartpbx:|env_file|STT_ENDPOINTING_SILENCE_SECONDS|DTMF_INTERDIGIT_TIMEOUT_SECONDS|BARGEIN_MIN_CHARS"` . `riviera-smartpbx` must show values from `environment`, and no `env_file` stanza.
- **Compose/dockerfile safety:** `Dockerfile` uses explicit module `COPY` manifest and a build-time `RUN python -c "import server"` guard. `Riviera/tests/test_dockerfile_manifest.py` enforces closure coverage and the presence of the import guard so missing modules fail fast at build-time or pre-build CI.
- **Deploy gate sequence:** `Riviera/SMARTPBX_RUNBOOK.md` requires read-only probe + immutable image identity checks and a reviewed helper script for deployment. The repo-level gates are:
  - `probe-riviera-image.yml` (`repository_dispatch`, event type `riviera_image_read_only_probe`, requires `github.ref_protected`, and validates payload keys `existing_tag`, `expected_revision`, optional `bootstrap: "true"`).
  - `build-riviera-image.yml` (`workflow_dispatch` with `ref` + `expected_sha`, requires a fresh successful read-only probe on same `head_sha`, and verifies image label revision before publishing).
- **GHCR auth note:** on VPS, `docker login` frequently expires; authenticate with `--password-stdin` (or equivalent token stdin flow) before the deploy/review helper run.

---

## Change History

The v0.1–v0.23 history of the inherited code is in **`../Kavya/CLAUDE.md`** — every one of those
sections describes this code too. Only Riviera-specific changes are recorded here.

### r0.1 — Clone of Kavya for Riviera Resort (Sep 2026)
Cloned `Kavya/` at monorepo commit `6258d10`; rebranded identifiers, persona and property;
replaced the room catalogue and rate model (seven rooms, LKR room-only, two date-range seasons,
meal-plan supplements, no foreign sheet); enabled Sinhala and Tamil on the Twilio IVR and added
the Tamil profile to the Dialog SmartPBX menu; new KB from the client's requirements and rate
sheets; dedicated PMS seed + verifier under `ops/riviera-pms/`; own loopback ports 8060/8061;
isolation-neighbour check in the deploy script; registered in CI, deploy exclusions, PR impact,
dependabot, dep-audit, Sentry triage and the revert script; `build-riviera-image.yml` /
`probe-riviera-image.yml` mirror Kavya's guarded pipeline. See the delta list at the top.
Post-call capture is unchanged from Kavya: the summary is POSTed to the
`N8N_POSTCALL_WEBHOOK` env var (default: `/webhook/post-call-data`) on `N8N_BASE_URL`;
point Riviera at its own n8n workflow / Google Sheet before go-live so its call log does not
land in Kavya's.

The four inherited contracts below are kept here because they are load-bearing for any edit to
`server.py` / `smartpbx_session.py`:

### Spoken-number capture contract
- Primary path for phone/WhatsApp/callback numbers remains spoken capture: use `capture_spoken_number` first and pass caller phrases exactly as spoken.
- `collect_number_via_keypad` is fallback-only and should be offered only after repeated spoken capture failures or when the caller explicitly requests keypad entry.
- Deterministic parser path lives in `Riviera/handover.py`: `expand_spoken_repeats` and `spoken_number_to_digits` expand double/triple/treble and zero variants, then `normalize_whatsapp` validates and normalizes length. This path is the single normalization source for live readback, booking phone, and WhatsApp payload so model arithmetic cannot drift from it.
- **Fragment combining (capture mode).** Callers dictate numbers in 2-4 digit
  groups and the recognizer commits a final at every pause. `MediaStreamSession`
  therefore treats a dictation as ONE utterance:
  - Capture mode is armed by the **delivered ask** (`_maybe_enter_capture_mode_from_ask`,
    module patterns `_CAPTURE_ASK_PATTERNS` / `_CAPTURE_ASK_SUPPRESS_PATTERNS`),
    so the first fragment is already patient — it no longer waits for the first
    capture tool call. A read-back is not an ask; the successful-capture turn
    also suppresses re-entry.
  - While armed, every provider FINAL refreshes the capture-silence window
    instead of dispatching (`_capture_turn_timeout` returns the more patient of
    `CAPTURE_FINAL_GRACE_SECONDS`/`CAPTURE_ENDPOINTING_SILENCE_SECONDS`), the
    combined text is capped at `CAPTURE_BUFFER_MAX_CHARS` (600, head kept), and
    only the combined utterance is dispatched. Finals still pass the `_is_echo`
    gate before they can enter the buffer.
  - The silence re-prompt cannot fire while a capture buffer or capture timer is
    pending (`_capture_dispatch_pending`) — a nudge mid-number talks over the
    caller.
  - The episode is bounded: a `status=captured` result, a combined utterance
    below `CAPTURE_DICTATION_MIN_RATIO` (0.3) digit/letter-like tokens, or
    `CAPTURE_MODE_MAX_TURNS` all end it. The allowance is spent AFTER the turn
    and `_enter_capture_mode` never refills a live episode, so `needs_more`
    cannot reset an exhausted one.
  - Teardown forces the buffer into `full_transcript` (`_retain_pending_speech`,
    formerly `_force_pending_capture_dispatch`, called from
    `MediaStreamSession.run()` teardown, `enter_transfer_pending`, the
    `_flush_transcript` transfer branch, `_handle_bargein`, and
    `RivieraSmartPBXSession._finish_once`) — a half-dictated number must reach the
    call log and post-call extraction. **It is no longer gated on capture mode:**
    since the post-dispatch predicate was narrowed (below), ordinary speech
    admitted while a turn held the dispatch guard is routinely pending too, and
    it is retained on the same terms.
  - Capture mode deliberately survives a barge-in (the buffer does not): a
    caller talking over the tail of the ask is a dictation starting. The
    superseded buffer is still written to `full_transcript` first — barge-in
    ownership is SUPERSEDED for dispatch, RETAINED for the record.
  - Both knobs are env-tunable and clamped: `CAPTURE_BUFFER_MAX_CHARS`
    (600, clamp `[60, 4000]`) and `CAPTURE_DICTATION_MIN_RATIO`
    (0.3, clamp `[0.0, 1.0]`).
  - **The capture parsers only ever see the caller's own words** because every
    runner overrides the model's `spoken` argument with the raw utterance
    (`_override_capture_spoken_argument`) — media Claude/OpenAI/Gemini and all
    three ConversationRelay runners. The combined dictation reaches the parser
    through that override and nowhere else, so a runner that skips it silently
    discards the fragment combining.
  - **Sinhala spoken numbers (Direct SmartPBX Sinhala only, Sep 2026).**
    Sinhala callers say numbers tens+units combined ("හැට පහ" = sixty-five),
    not digit-by-digit like English callers dictate, and Azure `si-LK` STT
    returns Sinhala number words (sometimes mixed with ASCII digits) that
    `handover.py`'s English-only `spoken_number_to_digits` cannot parse — its
    token splitter treats every Sinhala character as a separator, so a bare
    Sinhala number was silently dropped. `server._normalize_sinhala_spoken_digits`
    is a pure, word-boundary-matched text transform (units 0–9, teens 11–19,
    tens 10/20/…/90 standalone or combined with a following unit) that runs
    in `_flush_transcript`, gated on `_is_direct_smartpbx_sinhala()`, BEFORE
    the dictation-ratio check and before the turn dispatches — so the same
    normalised digits reach `_capture_dictation_ratio`,
    `_process_utterance`'s history, and (via `_last_guest_utterance_raw` /
    `_smartpbx_runner_raw_utterance`) the `capture_spoken_number` override.
    `_CAPTURE_DICTATION_WORDS` also carries the raw Sinhala number-word
    vocabulary as defense in depth. `AzureSTTStream` applies a matching
    `SI_STT_PHRASE_LIST` PhraseListGrammar for `lang == "si"` (mirroring the
    existing English-only `EN_STT_PHRASE_LIST` bias) so Azure is biased
    toward the same words the normaliser understands. English behaviour
    (`expand_spoken_repeats`, `spoken_number_to_digits`) and the Twilio
    Sinhala Media Streams path are both untouched.

### Post-dispatch STT results: refuse only the empty ones (Aug 2026)
A dispatched turn owns the STT endpoint (`_utterance_dispatched`), and results
still arrive after it claims that guard — during pre-TTS LLM/tool latency and in
the gaps between delivered sentences, where `_is_speaking` is already False.
`_reject_post_dispatch_result` decides what happens to them:

- **Refused:** a result with no material characters (nothing alphanumeric —
  empty, whitespace or punctuation only). Provable by construction: there is no
  caller speech in it, so discarding it cannot discard any. Refused before any
  counter, buffer or timer moves; bounded, privacy-safe
  `stt_post_dispatch_result` telemetry (see `SMARTPBX_RUNBOOK.md`).
- **Admitted:** everything else, **including a verbatim repeat of the dispatched
  utterance, a prefix of it, or a punctuation variation of it.** Those may be a
  provider tail — or the caller repeating/correcting themselves, which is what a
  caller does when the agent goes quiet mid-turn. Nothing available here
  separates the two: `GoogleSTTStream` and `AzureSTTStream` both hand their
  callbacks a bare `str` (no result id, no segment id, no audio-time span), and
  `_stream_epoch` is an internal gRPC-swap fence identical in both cases.
  **Do not reintroduce a text-relationship or elapsed-time predicate here** —
  matching text plus a short delay is not proof of provider ownership, and the
  earlier `POST_DISPATCH_STALE_WINDOW_SECONDS` version of this gate deleted
  genuine caller speech. Admitted speech buffers, cancels and resets the silence
  re-prompt, and dispatches as the NEXT turn (`_deferred_flush_pending`
  re-arms the flush when the turn releases the guard).
- **Ownership of pending speech is explicit at every boundary** — DISPATCHED,
  RETAINED (`_retain_pending_speech` → `full_transcript`), or TRANSFERRED, never
  a silent clear. Barge-in: SUPERSEDED for dispatch, RETAINED for the record.
  Transfer-pending (both `enter_transfer_pending` and the `_flush_transcript`
  branch), SmartPBX `_finish_once`, and Twilio `run()` teardown: RETAINED before
  the post-call snapshot.

Shared with the Twilio Media Streams path on purpose (ConversationRelay has its
own handler and never reaches this accumulator); only the SmartPBX log
vocabulary is gated by `_is_smartpbx_session`.

### Gemini double-empty is a failover, not an outcome (Aug 2026)
A Gemini turn that streams no text and no tool call twice in a row now raises
`_GeminiEmptyTurnError` into the existing Gemini→Claude failover path
(`reason=empty_response`, sticky counter advances) instead of always speaking the
canned `LLM_EMPTY_FALLBACKS` line — dead air to the caller is the same symptom as
a quota error. The canned line remains for the cases where failover cannot run
(`GEMINI_FAILOVER_TO_CLAUDE=false`, or no Anthropic client/key), and for any
empty round that is **not replayable**: failover re-runs the whole turn from the
truncated history, so it is gated on `round_idx == 0` with nothing spoken and no
tool executed. A later empty round takes the canned line rather than risk running
`create_booking` twice. Every failover (sticky and per-exception, both runners)
converts the tool list to Anthropic shape via `_claude_tools_from_gemini` —
Anthropic 400s on Gemini's `function_declarations` payload, and substituting
`get_tools()` would hand the restricted handover-failsafe session the full
booking tool set.

### SmartPBX migration operational hardening
- `SMARTPBX_TRANSFER_PENDING_TIMEOUT_SECONDS` is validated in `smartpbx_gateway.py` as an integer setting: default `300`, clamp `[30, 1800]`. An omitted line in `.env.smartpbx` falls back to this default via the compose allowlist's own `${SMARTPBX_TRANSFER_PENDING_TIMEOUT_SECONDS:-300}` default (every `riviera-smartpbx` passthrough carries one -- see the env-var drift table); a key present but blank falls back the same way, because `smartpbx_gateway._parse_bounded_integer`'s caller treats a blank value as absent too. Neither path is "absent" at the Python-process level under compose -- an omitted `.env.smartpbx` line still arrives as a real value, just the compose default rather than a truly missing key.
- `SMARTPBX_MAX_CALL_SECONDS` (Sep 2026): a hard per-call ceiling independent of both idleness and transfer-pending activity -- a call that never idles and never transfers must still end eventually. Default `3600`, clamp `[300, 7200]`. Closes the socket with code `1000` (an expected, polite close, not a policy violation) and the seven-field diagnostic `failure_class=max_call_duration`; the post-call/session-summary `close_reason` field carries `max_call_duration` too. `SmartPBXGateway.__init__` takes an injectable `clock` (default `time.monotonic`) so both ceilings are testable without a real multi-minute wait; it is deliberately not the process-global `time.monotonic` patched in place, since that would also perturb `SmartPBXMediaTransport`'s real-time audio pacing.
- `smartpbx_transport.py` has no env-driven knobs; transport behavior is bounded by internal backpressure constants (`_SEND_BACKPRESSURE_SECONDS=0.2`, `_SEND_BACKPRESSURE_POLL=0.005`).
- Missing `RIVIERA_EN_ELEVENLABS_VOICE_ID` is a hard failure path in `english_voice_profile.load_riviera_english_voice_profile()` for English TTS: it raises `ValueError`, logged as a skip path for `_tts_elevenlabs()` rather than fabricated/fallback speech.
- Profile/credential preflight failures (Sep 2026, audit #10): a missing `GEMINI_API_KEY` or any `_preflight_language_profile` failure (`smartpbx_session.py`, `_end_call_without_language_profile`) emits `SESSION_START/FAILED/<class>` on the seven-field diagnostic (`DiagnosticFailureClass.GEMINI_API_KEY_MISSING` or `.PROFILE_UNAVAILABLE`) before resolving the terminal future, and the gateway's "raw is None" completion branch (`smartpbx_gateway.py`, reads `session.close_reason`) now closes `1011` instead of `1000`/`completed_normally` for that case and the pre-existing fatal-STT one -- previously both looked exactly like an ordinary completed hangup, with no diagnostic and no warning.
- Late tool completion after hangup (Sep 2026, audit #3): when a runner loses ownership (`_current_smartpbx_runner_owns_shared_state(tool_executed=True)` returns False) after `execute_tool` already ran -- both the per-tool bail immediately after `execute_tool` and the post-loop bail before history commit, in all three provider runners -- it now calls `self._record_smartpbx_late_tool_completion(...)` (for the current tool and, at the per-tool bail, every already-staged one) before discarding the round. This appends into `MediaStreamSession._smartpbx_late_tool_results`, a session-owned list separate from `full_transcript`, reusing `_append_booking_confirmation_marker`'s own success/`create_booking` filtering so every other tool stays a no-op. `_arm_endpointing` now tracks the dispatched endpointing→LLM→tool round as `self._smartpbx_active_runner_task` (previously fire-and-forget, audit #11) so `RivieraSmartPBXSession._finish_once_locked` can `asyncio.wait_for(asyncio.shield(...), timeout=smartpbx_session.LATE_TOOL_RESULT_WAIT_SECONDS)` (10s) for it to settle before snapshotting the post-call transcript, then merges `_smartpbx_late_tool_results` in. The wait is shielded so a timeout never cancels the tool call itself (consistent with the pre-existing "never cancel an in-flight tool" policy). `SmartPBXGateway._cleanup`'s per-operation timeout for the "session" step was raised from a flat 5s to 20s (`cleanup_timeouts` dict) to comfortably cover the up-to-5s STT-stop join plus the up-to-10s late-tool wait; transport/lease stay at 5s.
- Event-loop hygiene (Sep 2026, audits #5/#8/#9):
  - `tools._await_turn_delivery` (the `transfer_to_human` announcement-delivery wait) no longer busy-spins `await asyncio.sleep(0)` once per loop tick. `server.py`'s `_send_tts_done` and `_handle_bargein` now `.set()` `MediaStreamSession._smartpbx_delivery_event` on every progress event (a delivered sentence, or a generation bump that makes the wait moot); the waiter blocks on that event instead, falling back to the old busy-spin only for a pipeline stand-in that lacks the attribute.
  - `MediaStreamSession._handle_bargein` is now idempotent per speak generation: it captures `self._speak_generation` synchronously as its first statement and returns immediately if that same generation was already claimed by another (concurrent or prior) call, via `self._smartpbx_bargein_claimed_generation`. Without this, two STT callbacks racing in while `_is_speaking` is still True (an interim and a final, or two interims) both ran the full cancel/bump/retain cycle, and the second run could supersede the caller's own new utterance and drop it.
  - English/ElevenLabs TTS now has its own pre-audio window, mirroring Sinhala/Gemini's. `_pre_audio_synthesis_active()` gained a second branch: Gemini's existing "in flight, not yet speaking" shape, plus a new `_smartpbx_en_pre_audio_active`/`_smartpbx_en_pre_audio_generation` pair that `_tts_elevenlabs` sets at request start (English sets `_is_speaking` True immediately, unlike Gemini, so a genuine >=`BARGEIN_MIN_CHARS` interruption can still barge in during TTFB) and clears via `_smartpbx_end_en_pre_audio_window()` at the exact moment the first frame reaches the transport -- not when the whole utterance finishes, which would silently disable ordinary barge-in for the rest of the reply. A sub-threshold/debounced STT result arriving in that narrow window now routes through the existing `_handle_pre_audio_stt` buffering (and is flushed via `_flush_pre_audio_stt` in `_tts_elevenlabs`'s `finally` if the request fails before ever emitting audio) instead of being silently dropped.

## graphify — GRAPH-FIRST, ALWAYS

This sub-project is part of the shared graphify knowledge graph at `../graphify-out/`
(project root). The graph covers the fleet's agents (BSL, Kavya, SLIC, Sofia, Flico,
HattonHills…) plus SinhalaVITS-TTS; Riviera is new and needs a `graphify update .` from the
project root. **Use it instead of scanning the codebase** — it is faster and
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
