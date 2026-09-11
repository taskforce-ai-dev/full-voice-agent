# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

Kavya is an AI voice agent for **Hatton Hills** — a luxury boutique eco retreat in an eight-acre private forest in Sri Lanka's central hill country. Handles inbound phone calls via Twilio, uses a configurable LLM (Claude by default, or OpenAI / Gemini) for conversation and tool use, integrates with the **Yanolja PMS** for availability/booking, and grounds answers in a ChromaDB-based RAG knowledge base.

> **Hatton Hills is an INVENTED property for client demonstrations.** The rate card (USD 700–1,400 per room per night, half board), the room descriptions and the reservations number (+94 77 220 4400) are all fictional.

**Brand lineage — read this before trusting any older section below.** This agent has been rebranded twice and the change history further down still describes the earlier states: it was **Treehouse Chalets** (Belihuloya) until Jul 2026, then **Mosvold Boutique Hotels** (two properties, Ahangama + Balapitiya) from 2026-07-20, then **Hatton Hills** (single property) from 2026-07-30. Sections written before v0.20 may name Treehouse or Mosvold, cite the old room types, the old reservations number, or claim rates are never quoted. **v0.20 is authoritative.**

**Single property.** Hatton Hills has exactly five room types, all distinct: Forest Escape Suite, Eco Harmony Suite, Sunrise Vista Premium Suite (each up to 2 guests), Mount Luxe Chalet, Mount Monarch Chalet (each up to 5 guests). Kavya must NEVER ask which property or location the caller means. The two-property disambiguation machinery from the Mosvold era is retained but inert — see v0.20.

The agent persona is **Kavya** — a reservations agent. **Twilio service historical note (2026-07-28):** that service's line was English only: Sinhala and Arabic were removed from its IVR — `DIGIT_TO_LANG = {"1": "en"}`, the Arabic/Sinhala `<Say>` prompts are gone from `/voice/incoming`, and `/ws/media-stream/{lang}` now accepts only `ta` (so `si`/`ar` connections are refused). The dormant Twilio Sinhala path uses **OpenAI `gpt-4o-mini-tts`** (voice `nova`) as of v0.16 (was Azure `si-LK-SameeraNeural`). Tamil remains implemented in the Twilio Media Streams code but is not surfaced in that menu. This history does not describe, enable, or alter Direct SmartPBX.

**Direct SmartPBX language boundary:** the Dialog SmartPBX menu presents `1` for English and `2` for Sinhala. A selection timeout, invalid selection, or replayed invalid selection resolves to English. This is a direct SmartPBX-only choice: Twilio ConversationRelay and Twilio Media Streams behavior remain unchanged.

**Two server modes:**
- `server.py` — **Unified production server**: the Twilio service retains its historical ConversationRelay/Media Streams routes; the opt-in Direct SmartPBX service has its own `1` English / `2` Sinhala call-local profiles.
- `media_stream_server.py` — **Standalone Media Streams** (Anthropic Claude, ElevenLabs multilingual TTS, Google Cloud STT, barge-in — kept as reference/alternative)

This "two server modes" split is orthogonal to a second, more recent gate: `server.py` itself now builds one of **two mutually exclusive service-mode apps** (Twilio vs Dialog SmartPBX) via `KAVYA_SERVICE_MODE` — see **Service Modes** below.

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

> **WARNING -- Twilio service only.** Everything below (`deploy.sh`, `docker compose build`, the VPS at `treehouse.taskforceai.tech`) targets the `kavya` (Twilio) service. **None of it touches `kavya-smartpbx`.** `deploy.sh`'s `PROD_FILES` list does not even include `scripts/`, `nginx-smartpbx*.conf`, or `SMARTPBX_RUNBOOK.md`, and building on the VPS is the documented 2026-08-02 resource-starvation failure mode -- see the compose file's own header. SmartPBX has its own reviewed image pipeline (`build-kavya-image.yml` -> `scripts/deploy_smartpbx_image.sh`); follow `SMARTPBX_RUNBOOK.md`, never this section, for any SmartPBX change.

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
- `ANTHROPIC_API_KEY`, `CLAUDE_MODEL` — Claude/Anthropic (default model: `claude-sonnet-4-5-20250929`)
- `OPENAI_API_KEY`, `OPENAI_MODEL` — OpenAI (default model: `gpt-4o`)
- `GEMINI_API_KEY`, `GEMINI_MODEL` — Gemini via native google-genai SDK (default model: `gemini-2.5-flash`)

### Direct SmartPBX Sinhala profile

Direct SmartPBX settings belong only in protected, root-owned
`/opt/kavya/.env.smartpbx`. Its `kavya-smartpbx` Compose service uses an
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
- **Fallback-model usability fixes (2026-09-04), same fallback chain above.**
  Live probes against `gemini-2.5-flash-preview-tts` (the first fallback)
  found it fails short Sinhala utterances (zero audio deltas with no error,
  or an outright `invalid_request`) and is non-streaming (one terminal delta
  holding the whole reply, ~12 s for a 154-token reply). Three fixes, all
  scoped to a NON-primary model -- the primary's existing behavior (never
  retries a non-quota error) is unchanged:
  - **Text-specific retry.** `empty_audio`/`invalid_request` on a fallback
    model retries the same text once on the next available model, bounded by
    chain length, without marking the model exhausted (`smartpbx_media
    event=sinhala_tts_model_retry from=<model> to=<model>
    reason=empty_audio|invalid_request`) -- length-agnostic; a long reply
    that hits either shape retries the same way a short one does. Only
    exhausting the whole chain for this text falls through to the existing
    apology. An `empty_audio` occurrence also logs `smartpbx_media
    event=sinhala_tts_stream_empty model=<model> events=<kind>=<count> ...`,
    a bounded (<=8 distinct kinds, overflow folded into `other`) histogram
    of the SDK protocol event kinds the stream actually carried -- never
    content, so it is privacy-safe as-is.
  - **Short-utterance cache bank.** `SMARTPBX_SINHALA_SHORT_UTTERANCE_BANK`
    (`ඔව්.`, `හරි.`, `ඔව්, හරි.`, `හොඳයි.`, `ස්තූතියි.`, `සමාවෙන්න.`,
    `ඔව්, ඒ හරි.`, `නැහැ.`) joins the prewarmed `SMARTPBX_SINHALA_CACHED_PHRASES`
    allowlist, with cache lookup now canonicalizing whitespace/trailing
    punctuation so a near-exact model utterance still hits the cache.
    `SMARTPBX_SINHALA_TTS_MIN_CHARS` (default `12`) documents the length
    below which a fallback model is disproportionately likely to fail this
    way; it never blocks synthesis of an uncached short text.
  - **Per-sentence synthesis on a non-streaming model.**
    `SMARTPBX_SINHALA_TTS_NON_STREAMING_MODELS` (comma list, default
    `gemini-2.5-flash-preview-tts,gemini-2.5-pro-preview-tts`, same
    validation as the fallback list) marks models that cannot stream. When
    the model that would be tried first is one of these, a no-tool Sinhala
    reply is synthesised per sentence (not batched into one request) so the
    first sentence's audio starts in seconds, not after the whole reply
    renders -- capped at 4 live TTS requests per turn (sentences beyond the
    cap merge into the final request). The streaming primary keeps batching;
    capture-mode turns are unaffected either way.
  See `SMARTPBX_RUNBOOK.md`'s Monitoring section for the full contract.

**TTS/STT:**
- `ELEVENLABS_API_KEY`, `ELEVENLABS_VOICE_ID` — ElevenLabs TTS (English ConversationRelay + Tamil Media Streams)
- `OPENAI_API_KEY` — OpenAI key; used by the historical Twilio Sinhala `gpt-4o-mini-tts` path and, if `LLM_PROVIDER=openai`, the LLM
- `OPENAI_TTS_MODEL` / `OPENAI_TTS_VOICE` / `OPENAI_TTS_INSTRUCTIONS` — historical Twilio Sinhala TTS config (defaults: `gpt-4o-mini-tts`, `nova`, a warm Kavya/Treehouse Sinhala-tone instruction)
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
- `/webhook/post-call-data` — Kavya POSTs call summary + transcript after each call (POST, → Google Sheets)

**Requires**: Browser extension running on a machine logged into `live.ipms247.com` in Firefox. Without it, availability checks timeout after `N8N_POLL_TIMEOUT` seconds.

**Knowledge base** (`knowledge_base.py`):
Files in `knowledge_docs/` chunked (500 chars, 50 overlap) -> embedded with `all-MiniLM-L6-v2` -> stored in ChromaDB (`./chroma_db`). Query embeddings LRU-cached. KB context injected as user message prefix per turn (not system prompt). Prewarm on startup. Chunk IDs are SHA-256 hashes (idempotent re-indexing).

**Post-call data capture** (`post_call.py`):
When a call ends (WebSocket disconnect), `server.py` fires `asyncio.create_task(process_post_call_data(...))` to run post-call processing in the background. The flow: format the full transcript → call LLM (same provider as the conversation) to extract structured booking details (guest name, dates, room preference, outcome, follow-up needed, summary) → POST JSON payload to n8n webhook `/webhook/post-call-data` → n8n appends a row to Google Sheet "Kavya Call Log". Caller phone number is captured from Twilio HTTP POST params (`From`) via a module-level `_call_phone` dict bridge between HTTP handlers and WebSocket handlers. A separate `full_transcript` list (never trimmed) accumulates all user/assistant messages alongside the trimmed `conversation_history`. All errors are caught and logged — post-call failures never affect the call or server stability. Env var: `N8N_POSTCALL_WEBHOOK` (default: `/webhook/post-call-data`).

**Google Sheet columns** (n8n workflow "Post-Call Data to Google Sheets"): Date/Time, Call SID, Language, Caller Phone, Guest Name, Location, Guests, Check-In, Check-Out, Room Preference, Availability, Outcome, Follow-Up Needed, Summary, Transcript.

**Local demos** (`test_voice_elevenlabs.py`, `test_voice.py`):
Typed input -> KB retrieval -> LLM tool-use loop -> text response -> TTS playback. ElevenLabs version sends full response as one TTS call (splitting into sentences causes prosody resets). Azure version splits by sentence with per-language voice selection.

## Service Modes

`server.py` builds **one of two mutually exclusive FastAPI apps**, selected by `KAVYA_SERVICE_MODE` (env var, `"twilio"` default) via `build_service_app(service_mode, environ)` at the bottom of the file. The two modes never run in the same process — `build_service_app` returns exactly one `FastAPI` instance, and `lifespan()` skips all Twilio REST-client / handoff startup when the mode is `smartpbx`.

**`twilio` (default) — production.** Everything documented above (ConversationRelay, Media Streams, `/voice/*`, Twilio `<Dial>` handover) is `app` unchanged. This is what serves real Hatton Hills calls today and **remains production default until a deliberate Dialog cutover is decided and executed** via `SMARTPBX_RUNBOOK.md`.

**`smartpbx` — Dialog SmartPBX ("Client Connect") ingress, opt-in.** A second, narrower FastAPI app is built instead (`docs_url`/`redoc_url`/`openapi_url` all disabled), exposing exactly three routes:
- `GET /health` — `{"status": "ok", "service_mode": "smartpbx"}`
- `GET /smartpbx/status` — session counters (`active_sessions`, `admitted_total`, `rejected_capacity_total`, `released_total`, `frames_dropped_total`), `enabled`, `configured`, `protocol_version`, `transfer_enabled` — no secrets, no PII. **Requires the same `X-Kavya-SmartPBX-Token` header as the media socket** (constant-time compare): the counters are a live occupancy oracle and a call-volume signal, so they are not publicly readable. `/health` stays unauthenticated for liveness probes; point uptime monitoring there.
- `WS /ws/v1/smartpbx/media` — the Dialog media socket, gated by a required `X-Kavya-SmartPBX-Token` header (constant-time compare) checked before `websocket.accept()`

Protocol version is `smartpbx-ai-provider-v06`. Audio is exact `g711_ulaw` at `8000` Hz only — any other codec/rate is rejected at the `start` event. Capacity is hard-capped at **4 concurrent calls** (a 5th is rejected before the socket is even accepted; `SmartPBXSessionRegistry` cannot be constructed outside 1–4).

**Module map** (`Kavya/smartpbx_*.py`):
- `smartpbx_protocol.py` — strict, transport-independent parser for the Dialog wire events (`connected`/`start`/`media`/`dtmf`/`hangup`/`stop`, else `Unsupported`) into a closed dataclass union; fail-closed on anything malformed.
- `smartpbx_gateway.py` — `SmartPBXSettings` (env validation), `SmartPBXSessionRegistry` (the 4-call admission counter), and `SmartPBXGateway` (auth → admit → start session → event loop → cleanup-once, emitting the `smartpbx_protocol_diagnostic` log line).
- `smartpbx_transport.py` — `SmartPBXMediaTransport`, the bounded outbound audio queue serializing `media` frames back to Dialog. Frames are **paced at realtime** so barge-in has queued audio left to cancel (Dialog defines no `clear` wire event); on overflow it **refuses the newest frame**, cutting the tail of a reply rather than decimating it; generation-fenced so barge-in can't leak stale audio; and a dead sender raises a failure signal so the gateway ends the call instead of leaving the guest in silence.
- `smartpbx_session.py` — `KavyaSmartPBXSession`, the adapter that first resolves a call-local English or Sinhala language profile, then wires it into one Dialog call's `MediaStreamSession` pipeline (STT → KB/PMS tools → LLM → TTS) and binds transfer/handover context. It does not mutate process-global provider or model state.
- `smartpbx_mcp.py` — fail-closed Dialog MCP call control: `DialogMCPSettings.from_env()` and `DialogMCPCallControl.transfer_call()`, restricted to operator-configured `tel:`/`sip:` destinations.
- `smartpbx_handover.py` — `SmartPBXHandoverCoordinator`, the call-local state machine that attempts the MCP transfer and, on failure, falls back to the existing WhatsApp handover notification.
- `smartpbx_diagnostics.py` — the enum vocabulary (`DiagnosticStage`/`DiagnosticOutcome`/`DiagnosticFailureClass`) for the seven-field diagnostic log line.

**Handover in SmartPBX mode.** Twilio `<Dial>`/REST redirect/dial-status callbacks do not exist on a Dialog call, so they are not reused. `transfer_to_human` instead invokes the Dialog MCP `transfer_call` tool against the call's `otherLegCallId`. The path is fail-closed end to end: MCP endpoint/API key/account ID/account-header spelling are environment-only, `SMARTPBX_TRANSFER_DESTINATIONS_JSON={}` disables transfer entirely (the base/default state), validation/auth failures never retry, and bounded network/server failures retry once. If the MCP transfer does not succeed, `SmartPBXHandoverCoordinator` reuses the existing `handover.py` WhatsApp notification path (same `normalize_whatsapp`, same n8n webhook shape) as an operational fallback — it notifies the manager but is never treated as evidence of a successful live transfer.

**Deployment shape.** SmartPBX runs as a separate Compose profile (`docker-compose.yml`, profile `smartpbx`, service `kavya-smartpbx`) alongside — not instead of — the existing `kavya` Twilio service: its own container, its own loopback port `127.0.0.1:8006`, its own Chroma volume (`./chroma_db_smartpbx`, never shared with the Twilio service's writable store), and an explicit environment allowlist (no `env_file: .env` — Twilio credentials and `HUMAN_AGENT_PHONE` must never reach this container). The image tag is pinned via `SMARTPBX_IMAGE_TAG` (default `disabled`, which cannot pull anything); secrets live in root-only `/opt/kavya/.env.smartpbx` (`chmod 600`), never `.env`. Public TLS terminates at a dedicated Nginx vhost, `smartpbx-kavya.taskforceai.tech`, in front of the loopback port. Image provenance is enforced in CI: `.github/workflows/build-kavya-image.yml` (publisher) and `probe-kavya-image.yml` (read-only probe) gate which image tag/digest is trustworthy to deploy — the runbook's guarded deploy script cross-checks the reviewed short SHA against the image's OCI revision label before recreating the container.

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
  instructing Kavya to confirm by name rather than guess when unsure.
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
- **Twilio historical DTMF language menu**: the v0.16 menu was Press 1 → ConversationRelay (English + ElevenLabs); Press 2 → Media Streams (Arabic + ElevenLabs multilingual); Press 3 → Media Streams (Sinhala + OpenAI `gpt-4o-mini-tts`). `DIGIT_TO_LANG = {"1": "en", "2": "ar", "3": "si"}`; no input → English. Tamil (Media Streams + ElevenLabs) remains fully coded but is not mapped to any menu digit. This does not govern Direct SmartPBX, whose menu/profile boundary is documented above.
- **Interim-based STT endpointing**: Google Cloud STT rarely fires `is_final=True` for conversational speech. Each interim result overwrites `_pending_transcript` (not appends) and resets a `STT_ENDPOINTING_SILENCE_SECONDS` timer. `STT_ENDPOINTING_SILENCE_SECONDS` defaults to `1.0` and clamps to `[0.2, 5.0]`. `STT_FINAL_GRACE_SECONDS` defaults to `0.5` and clamps to `[0.05, 5.0]`.
- **Tool gating via system prompt**: General info (rooms, rates, policies, activities) answered from KB context — no tool call. Tools only for date-specific booking operations.
- **Filler speech**: Spoken before tool execution to avoid silence during API calls (language-specific fillers for Sinhala/Tamil).
- **max_tokens=300**: Forces concise voice-appropriate responses.
- **History trimming**: Max `MAX_HISTORY_MESSAGES` messages (`60`). `_trim_history()` is format-aware — detects and skips orphaned tool result messages at the start of trimmed history for both Anthropic format (user messages containing `tool_result` content blocks) and OpenAI format (`role: "tool"` messages). Also skips orphaned assistant `tool_use`/`tool_calls` messages.
- **Native script**: LLM responds in native Sinhala/Tamil Unicode script. TTS handles native script directly.
- **Kavya persona**: Collects booking info in order: name → location (local vs foreign rates) → pax → dates → room. Mentions complimentary activities (2+ nights), April/December advance payment, honeymoon packages.
- **Twilio historical hybrid TTS**: English → ElevenLabs turbo (cloned voice via ConversationRelay), Sinhala → OpenAI `gpt-4o-mini-tts` (voice `nova`, 24 kHz PCM → 8 kHz μ-law via `audioop`), Tamil → ElevenLabs `eleven_multilingual_v2` (cloned voice, `ulaw_8000` output). Legacy Azure `si-LK-SameeraNeural` Sinhala path (`_tts_azure`) is wired but no longer that live Twilio route. Direct SmartPBX Sinhala instead uses its call-local Gemini TTS route.
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

### server.py — SmartPBX service mode (`KAVYA_SERVICE_MODE=smartpbx`, opt-in — see Service Modes above)
- `GET /health` — `{status, service_mode}` only (no LLM/KB/STT flags — those belong to the Twilio app)
- `GET /smartpbx/status` — session counters + `transfer_enabled`; requires the `X-Kavya-SmartPBX-Token` header (401 without it)
- `WebSocket /ws/v1/smartpbx/media` — Dialog media socket, requires `X-Kavya-SmartPBX-Token` header, `g711_ulaw`/8000 Hz only, events `connected`/`start`/`media`/`dtmf`/`hangup`/`stop`

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
1. **Persona**: Kavya, reservations agent for Treehouse Chalets
2. **Language rules**: Language-specific (determined by IVR selection), native script for Sinhala/Tamil
3. **Voice rules**: Short sentences, no markdown/bullets/URLs, numbers as words, no abbreviations
4. **Booking rules**: Answer general info from KB (no tool needed), tools only for date-specific operations, collect info in order, mention complimentary activities, advance payment, honeymoon packages

## Operational Details

> **WARNING -- Twilio service only.** The deployment details below (`deploy.sh`, VPS builds, `treehouse.taskforceai.tech`) are for the `kavya` (Twilio) service and **must not be used for `kavya-smartpbx`**. SmartPBX deploys only through the reviewed image pipeline described in `SMARTPBX_RUNBOOK.md`.

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
- **Twilio/SmartPBX env loading asymmetry:** `kavya` uses `env_file: .env`; `kavya-smartpbx` uses an explicit allowlist under `environment:` and should not use `env_file` in compose. This is a deliberate trap-prevention design: any var only present in `.env.smartpbx` is ignored unless copied into the `kavya-smartpbx` allowlist.
- **How to verify the allowlist trap:** from repo root: `cd Kavya && docker compose config | rg -n "kavya-smartpbx:|env_file|STT_ENDPOINTING_SILENCE_SECONDS|DTMF_INTERDIGIT_TIMEOUT_SECONDS|BARGEIN_MIN_CHARS"` . `kavya-smartpbx` must show values from `environment`, and no `env_file` stanza.
- **Compose/dockerfile safety:** `Dockerfile` uses explicit module `COPY` manifest and a build-time `RUN python -c "import server"` guard. `Kavya/tests/test_dockerfile_manifest.py` enforces closure coverage and the presence of the import guard so missing modules fail fast at build-time or pre-build CI.
- **Deploy gate sequence:** `Kavya/SMARTPBX_RUNBOOK.md` requires read-only probe + immutable image identity checks and a reviewed helper script for deployment. The repo-level gates are:
  - `probe-kavya-image.yml` (`repository_dispatch`, event type `kavya_image_read_only_probe`, requires `github.ref_protected`, and validates payload keys `existing_tag`, `expected_revision`, optional `bootstrap: "true"`).
  - `build-kavya-image.yml` (`workflow_dispatch` with `ref` + `expected_sha`, requires a fresh successful read-only probe on same `head_sha`, and verifies image label revision before publishing).
- **GHCR auth note:** on VPS, `docker login` frequently expires; authenticate with `--password-stdin` (or equivalent token stdin flow) before the deploy/review helper run.

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
- Added `ANTHROPIC_API_KEY`, `CLAUDE_MODEL` env vars (default model: `claude-sonnet-4-5-20250929`)
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
- Extracted data + full transcript POSTed to n8n webhook `/webhook/post-call-data`
- n8n workflow appends a row to Google Sheet "Kavya Call Log" (15 columns)
- Added `_call_phone` module-level dict in `server.py` — bridges caller phone number from Twilio HTTP POST params to WebSocket sessions
- Added `full_transcript` list (separate from trimmed `conversation_history`) — accumulates all user/assistant messages, never trimmed
- `finally` blocks in both ConversationRelay and MediaStreamSession fire `asyncio.create_task(process_post_call_data(...))` — fully async, fire-and-forget
- Supports all three LLM providers (Claude, OpenAI, Gemini) for extraction
- All errors caught and logged — post-call failures never crash the server
- Added `N8N_POSTCALL_WEBHOOK` env var (default: `/webhook/post-call-data`)

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

### v0.17 — Handover failsafe: WhatsApp the manager when the human doesn't pick up
**Problem:** `transfer_to_human` dials `HUMAN_AGENT_PHONE` with a 20 s timeout. If
nobody answered, `/voice/dial-result` dropped the caller back into Kavya with a bare
"Sorry, no agent was available" and **nobody was told the call had happened** — the
guest's request evaporated.

**New behaviour:** an unanswered dial re-enters Kavya in *failsafe mode*. She confirms
(or asks for) the guest's name and WhatsApp number, POSTs them to n8n, and tells the
guest a team member will call back. n8n WhatsApps the property manager.

**Changes:**
- New `handover.py` — payload assembly, phone normalisation, and the n8n POST.
  `normalize_whatsapp()` strips punctuation, converts local trunk form
  (`0771234567`) and bare NSN (`771234567`) to `94771234567`, and leaves genuine
  international numbers alone. Numbers are sent as **bare digits, no
  `@s.whatsapp.net`** — the n8n workflow owns JID formatting.
  Config: `N8N_HANDOVER_WEBHOOK` (default `/webhook/kavya-handover`),
  `WHATSAPP_COUNTRY_CODE` (default `94`).
- `tools.py` — new `notify_human_handover` tool (`customer_name`,
  `customer_whatsapp`, `call_summary`). Deliberately **NOT** in `TOOL_DEFINITIONS`:
  it is offered only in the failsafe session via `get_handover_tools(fmt)`, so a
  normal call can never promise a callback instead of transferring. `call_sid` and
  `human_agent_whatsapp` reach the handler through a `ContextVar`
  (`handover.handover_context`), not tool arguments — `execute_tool()` has no call
  context and threading one through every provider's streaming loop was not worth it.
- `server.py`:
  - `_handoff_state` (+ `_remember_handoff`, capped at 200 entries) carries
    `reason` / `caller_phone` / `transcript` across the relay restart. The failsafe
    session is a **brand-new WebSocket with empty history**, so without this Kavya
    would not know the guest's name or what they wanted.
  - `/voice/dial-result`: answered → `<Hangup/>` + drop carry-over; anything else →
    `<Connect><ConversationRelay …?lang=en&amp;mode=handover_failsafe>` with the
    apology greeting. `_build_conversation_relay_twiml()` gained a `mode` arg (the
    `&` **must** be XML-escaped or Twilio rejects the TwiML).
  - `ws_conversation(…, mode="")`: in failsafe mode swaps in
    `_build_handoff_failsafe_prompt()` (prior transcript inlined, caller ID offered
    as the default WhatsApp number), restricts tools to the notify tool, seeds
    `full_transcript` with the pre-transfer turns so the call log stays continuous,
    and skips KB retrieval.
  - **Safety net:** if the failsafe session ends without a successful tool call
    (guest hung up), `_notify_handover_fallback()` messages the manager anyway using
    the caller ID and a transcript-derived summary. It skips only when there is no
    number at all — a notification the manager can't act on is noise.
- Failsafe is English/ConversationRelay only; that's the only path with a live
  transfer to fail.

**n8n side:** workflow `YmeWVEUR54A8o8Tb` "Kavya — Handover WhatsApp Notify (No
Answer)" on `automation.taskforceai.tech` (webhook → Set → WasenderAPI → respond).
It reads `$json.body.*`. WasenderAPI accepts the bare-digit `to` here and resolves
the JID itself (verified: `msgId 67653931`, `jid 94711754668`).

**How to test:** `pytest tests/test_handover.py tests/test_handover_server.py
tests/test_handover_session.py` (52 tests, no network — the n8n POST is stubbed at
the aiohttp boundary). Live webhook check: POST the sample payload to
`https://automation.taskforceai.tech/webhook/kavya-handover` — this delivers a real
WhatsApp to `HUMAN_AGENT_PHONE`. Full call test: ask Kavya for a human, let the
agent phone ring out, then give her a name and number.
### v0.20 — Rebrand to Hatton Hills (single property) + data-security answers
**Reason:** A demo property was needed that Kavya could quote confident luxury prices for, without
the two-property disambiguation friction of the Mosvold setup. Hatton Hills already existed in this
monorepo as a separate agent (`../HattonHills/`) with an established identity and exactly five room
types, so that identity was reused rather than inventing a conflicting one.

**Hatton Hills is invented.** Rates, descriptions and the reservations number (+94 77 220 4400) are
all fictional, for client demonstrations.

**Room types and rates** (USD per room per night, half board, taxes included):
Forest Escape Suite 700 (2 pax) · Eco Harmony Suite 800 (2) · Sunrise Vista Premium Suite 950 (2) ·
Mount Luxe Chalet 1,150 (5) · Mount Monarch Chalet 1,400 (5, the only plunge pool, single unit).

**SINGLE-PROPERTY MODE — the load-bearing change.** The Mosvold code was deliberately *fail-closed*:
room names collided across the two properties, so `resolve_property()` returned `None` to force an
"ask which property" turn, and the `property` tool argument was REQUIRED. Left as-is, that would
have **blocked every booking** — the model would ask a question with no valid answer. So:
- `yanolja_service.resolve_property()` and `tools.normalise_property()` now ALWAYS resolve to
  `"Hatton Hills"`, never `None`. Return type narrowed `str | None` → `str`.
- The `if property_name else None` short-circuits wrapping those calls in `derive_availability()` and
  `book()` were REMOVED — they would have re-introduced the `None` and failed closed anyway.
- `property` removed from every tool's `required` array; schema description tells the model not to ask.
- `_property_required_error()` kept but **unreachable**; ditto the `property_name is None` guards in
  `execute_tool`. Retained as the restore point for a second property.
- `yanolja_service._property_of()` no longer falls back to `resolve_property()` on a PMS-supplied
  property field. Now that the resolver always resolves, that fallback would launder ANY string into
  a match and drag the retired ex-Mosvold room types and the `Default Unmapped Room` fallback back
  into availability. It now derives **solely** from the canonical room name and returns `""` for
  anything unrecognised — that `""` is what filters those rows out, so it is load-bearing.
- `_match_room_type()` gained a final fallback for a query that EXTENDS a canonical name
  ("mount monarch chalet with plunge pool" → "Mount Monarch Chalet"). That direction was previously
  forbidden because a longer cross-property name could collapse onto a shorter same-property one;
  with one property and five non-prefixing names the hazard is gone.
- `post_call._normalize_property_and_room()` no longer NULLs `room_preference` when `property` is
  unresolved — it forces `"Hatton Hills"`. The old behaviour would have silently dropped the room
  from most Google Sheet rows, since nothing asks the guest for a property any more.

**PMS.** `ops/hattonhills-pms/rename_to_hattonhills.sql` rebrands `yanolja_pms` in place by UPDATE
only (no INSERT/DELETE, so reservation FKs survive): renames types ids 1–5, spreads all 9 rooms
across them (2 each except Mount Monarch's 1), renumbers rooms to `HH-*`, and retires ids 7–10
(`is_active=0`, neutral names). Type 6 (`DUR`) untouched. Needs **root** on `198.211.114.60` — the
`dev` user has no MySQL grant and the PMS REST API has no room-type write endpoint.
`ops/hattonhills-pms/verify_live.py` checks the result through Kavya's own code path.

**ORDERING GOTCHA:** the SQL must be applied BEFORE the code deploys. Until it runs, the PMS holds
Mosvold names, `_property_of()` returns `""` for all of them, and **availability returns zero room
types** — Kavya truthfully reports no rooms. Verified: `verify_live.py` fails exactly this way
pre-migration.

**Data security answers (new prompt section).** Hotel prospects evaluating the system ask about data
security and are easily spooked, so Kavya now answers it confidently instead of deflecting to a
handoff. Added a `DATA SECURITY` block to `_build_system_prompt` plus matching KB paragraphs:
encryption in transit and at rest; **role-based access control** — access to sensitive data is
restricted to a named few at TaskForce AI, no other employee can reach it, and there is no shared
admin account; the hotel owns its data and it is never sold or shared. Kavya must also admit plainly
to being an AI agent built by TaskForce AI, and is explicitly forbidden from inventing
certifications, audits, compliance standards or data-centre locations — naming a fabricated
SOC 2 / ISO 27001 to a hotel's IT reviewer is worse than having none to cite.

> **Updated 2026-08-05:** as originally shipped this block named two individuals as the only people
> with access. **Those names have since been removed from the prompt** — verified with
> `grep -c "Chrys\|Rakesh"` returning 0 in `server.py`, 0 in `knowledge_docs/`, and 0 inside the
> running production container. The `DATA SECURITY` block itself is still live and every rule above
> still applies; it simply no longer names anyone. Do not re-add personal names here — Kavya says
> this out loud to prospects.

**Also in this change:**
- Global **one-question-at-a-time** rule promoted into `VOICE RULES` (it previously sat only inside
  the booking-details section, so compound clarification/handoff questions produced ambiguous
  `"Yes."` answers and forced re-asks).
- Fixed **114 mojibake em-dashes** (`â€"`) in `server.py` — pre-existing encoding damage that the
  LLM was reading verbatim in its system prompt.
- Reservations number changed everywhere to +94 77 220 4400, including all six spelled-out
  ("plus nine four, seven seven, two two zero, four four zero zero") renderings.
- `kpms_service.py` is **dead code** — nothing imports it, so it was left Mosvold-era.

**Rollback:** SQL `REVERT` block in `rename_to_hattonhills.sql`, plus backups in
`/home/dev/backups/kavya-kb/` (`hotel_info.txt.pre-hattonhills-20260730`,
`server.py.pre-hattonhills-20260730`). Reverting the database alone is not enough — the KB,
`yanolja_service.py`, `tools.py`, `server.py` and `post_call.py` all changed together.

### v0.18 — Transfers: own the caller ID, and stop trusting "completed"

> **CORRECTION (2026-08-05) — read this before trusting the diagnosis below.**
> This section blames the intercepts on presenting a foreign caller ID to a Sri
> Lankan mobile. **That is wrong**, and it was disproved by Twilio's own call
> records: a leg on 2026-08-03 10:09 was intercepted while presenting a **Sri
> Lankan** caller ID, and three legs on the US number rang normally. The
> intercepted legs answer with SIP `200 OK` at ~333 ms PDD and never send `180
> Ringing`; Twilio's side is clean (no error codes, no notifications,
> geo-permissions fine). The real cause is an **intermittent international
> termination route into SLT-Mobitel** — a carrier problem, not a config one.
> Setting `TWILIO_CALLER_ID` is still correct and should stay, but it does not
> prevent intercepts. See v0.21. The changes listed in this section are all
> still live and still right; only the *explanation* was wrong.

**Problem (two incidents, thought at the time to share one root):** `<Dial>`
without `callerId` makes Twilio pass the GUEST's number through. The theory was
that dialling a Sri Lankan mobile from an international gateway while claiming a
local CLI gets filtered as spoofing:

- **2026-07-31** — carrier returned `no-answer`, duration 0. Handset silent, but
  the failsafe fired correctly, so it looked like "the manager didn't pick up".
- **2026-08-03** (live demo) — carrier **answered instantly** with a recorded
  intercept, played it at the guest for 52 s, and reported
  `DialCallStatus=completed`. Indistinguishable from a real pickup, so the code
  logged "human answered", skipped the failsafe, and the lead vanished silently.
  The guest heard a recorded message after asking for a human.

`TWILIO_CALLER_ID` already existed as an escape hatch but was **never set in
production**, because a comment claimed that leaving it unset "falls back to the
Twilio number the guest dialled". It does not — `_transfer_caller_id()` returns
the env var verbatim. That wrong comment is the actual defect.

**Changes:**
- Corrected both caller-ID comments; the docstring now says SET THIS IN
  PRODUCTION rather than describing pass-through as the deliberate default.
- `.env.example` gained `TWILIO_CALLER_ID`, `HANDOFF_DIAL_TIMEOUT` and
  `HANDOFF_MIN_ANSWER_SECONDS` with the failure modes spelled out, so a
  from-scratch deploy no longer inherits the broken default silently.
- New `HANDOFF_MIN_ANSWER_SECONDS` (default 2.0) + `_answer_looks_intercepted()`.
- New `POST /voice/dial-status` records Twilio's initiated/ringing/answered/
  completed timestamps per transfer leg; both `<Number>` tags now carry
  `statusCallback` + `statusCallbackEvent`.
- `/voice/dial-result` re-labels a `completed` that was answered in under
  `HANDOFF_MIN_ANSWER_SECONDS` as `intercepted` and runs the WhatsApp failsafe.
  **Fails open** — missing timing is treated as a genuine answer, because a
  false positive would bounce a guest who really did reach a human back into
  the failsafe, which is worse than one missed notification.

**Prod config:** `TWILIO_CALLER_ID=+15187503185` was set in `/opt/kavya/.env` on
2026-08-03 and verified live — the leg then rang properly and was answered by a
human ~8 s in. Note `docker compose restart` does NOT reload `.env`; it needs
`up -d --force-recreate`, and `IMAGE_TAG` must be pinned or compose resolves
`:latest`.

**How to test:** `pytest tests/test_handoff_intercept.py` (24 tests, no network)
covers the instant-answer replay, the threshold boundary, callback retries, and
both dial-result branches.

### v0.21 — The failsafe had never fired. Three faults, all silent.
**Context:** a customer demo was scheduled for 2026-08-05. Live testing the night
before found that asking Kavya for a human still did not reliably reach anyone,
*despite v0.18 having shipped specifically to fix that*. Three distinct faults,
none of which produced an error anywhere.

#### Fault 1 — the manager was never notified. Not once, in five weeks.

The n8n handover workflow (`YmeWVEUR54A8o8Tb`) had **zero executions** between
its creation on 2026-07-31 and 2026-08-05. The failsafe had never run.

Both notification paths lived inside the **failsafe recovery session** — the new
ConversationRelay WebSocket that `/voice/dial-result` opens after a failed dial:
the `notify_human_handover` tool, and the end-of-session `_notify_handover_fallback`.
Both require that WebSocket to open.

On a carrier intercept it does not. The guest spends the whole dial listening to
a recorded message instead of ringing and hangs up before Kavya returns. So the
two notify paths were unreachable *precisely* in the case they existed for.
Observed live twice in seven minutes: both calls logged `entering failsafe` and
told nobody.

**Fix (PR #177):** notify from `/voice/dial-result` itself, at the moment the
transfer is judged failed, depending on nothing further happening. The existing
`notified` flag makes it idempotent — it suppresses only the duplicate
end-of-session net, so if the guest *does* stay on the line
`notify_human_handover` still sends its richer follow-up with the name and
number Kavya collects. Two messages beat none.

`notified` is set **optimistically before** the POST (that is what stops two
racing paths both sending) and **cleared again if the POST fails**, mutating the
state entry in place. Without that rollback the flag would mean "attempted"
rather than "delivered", and a swallowed n8n 5xx would stand every remaining
path down permanently — losing the lead, which is the one outcome this path
exists to prevent.

#### Fault 2 — the guest was held through ~50 s of the intercept recording.

`answerOnBridge="true"` bridges the guest the instant the leg is "answered". When
that answer is an intercept, the recording plays **at the guest** for as long as
the carrier talks — 49 s and 52 s in the incidents — and only when it ends does
`<Dial>` return and the failsafe get its turn. By then the guest is gone. This is
*why* Fault 1 was never survivable.

**Fix (PR #178):** hang the leg up as soon as it is identified, via the Twilio
REST API from `/voice/dial-status`. Collapses the wait to about a second.

The trigger is deliberately **stricter** than `_answer_looks_intercepted`: it
also requires that **no `ringing` event arrived**. dial-result only reclassifies
a call that has already ended; this cuts off a **live** one, and a genuine
handset essentially always rings first. New `HANDOFF_KILL_INTERCEPT` (default
true) disables it at runtime without a deploy. A failed REST hang-up is
non-fatal — `<Dial>` still ends on its own, so the failsafe is delayed, not lost.

#### Fault 3 — voicemail, which no detection logic can catch.

The manager's handset diverted to voicemail after exactly **20 s of ringing**
(31 s from dial). Voicemail **rings first and answers slowly**, so by timing it
is *indistinguishable from a human*:

| | rings first? | answers at | detectable? |
|---|---|---|---|
| Carrier intercept | no | 0.5 s | yes — caught |
| Real human | yes | 18.6 s | yes — correctly accepted |
| **Voicemail** | **yes** | **31 s** | **no — identical to a human** |

Twilio's Answering Machine Detection would separate them, but **AMD is not
available on TwiML `<Dial><Number>`** — it requires restructuring the call
through the REST API.

**Fix: config, not code.** `HANDOFF_DIAL_TIMEOUT` 40 → 25, so Twilio gives up
*before* the divert and returns a clean `no-answer`, which already triggers the
failsafe correctly. See `.env.example` for the trade-off in both directions —
too low cuts off genuine slow answers, too high lets voicemail win. Measured on
this route: post-dial delay is 11–13 s before ringing even starts, and genuine
answers landed at 18.6 s / 19.7 s / 20.65 s from dial.

**Prod config (2026-08-05) — SETTLED, not a demo-day workaround.** Both values
below were made permanent by the repo owner on 2026-08-05, reviewed with the
measurements above in hand. Treat them as decisions, not leftovers:

- `HUMAN_AGENT_PHONE` moved to a **Dialog (07x)** number. The previous
  SLT-Mobitel 071 line sits on the route that intermittently intercepts —
  three consecutive transfers were intercepted before the switch and none
  after. That handset also diverted to voicemail after 20s of ringing.
  If this number ever moves: pick a non-SLT-Mobitel network, confirm it either
  has no voicemail or diverts LATER than `HANDOFF_DIAL_TIMEOUT`, and re-test an
  unanswered transfer end to end including the manager's WhatsApp.
- `HANDOFF_DIAL_TIMEOUT=25`. **Do NOT "restore" this to the old 40.** Raising it
  hands unanswered transfers to voicemail, and because voicemail is
  undetectable the failsafe then never fires and the lead is lost silently —
  the exact failure this value exists to prevent. The margin is tight (the
  20.65s genuine answer had 4.3s to spare) and that is understood and accepted.
  If real answers start getting cut off, the fix is a faster-ringing
  destination or a better route, not a higher number.

Both set in `/opt/kavya/.env`. Backup at
`/opt/kavya/.env.bak.handover-20260805-043451`. Env-only; same image. Note
`docker compose restart` does NOT reload `.env` — it needs
`up -d --force-recreate` with `IMAGE_TAG` pinned.

**Verified live**, not just in tests: genuine pickup answered 18.6 s in with the
early hang-up correctly standing down; an unanswered transfer produced
`no-answer` → failsafe → **manager notified 0.93 s later** with a real WhatsApp
delivered (`msgId 68884788`), and the guest was recovered and asked for a
callback number.

**How to test:** `pytest tests/test_handoff_intercept.py` — now 36 tests. The
three added for #177 and the six for #178 assert at the n8n and Twilio
boundaries rather than restating the implementation. Two of them **fail against
the pre-#177 code** with `manager was NOT notified when the transfer was
intercepted`; that failure is the point of the test, and any future test for
this path should clear the same bar. The original 24 all passed against a
completely broken detector because they hand-built `dial_events` with an
`"answered"` key Twilio never sends.

**Known remaining:** the carrier route itself. Intercepted legs answer SIP
`200 OK` at ~333 ms PDD with no `180 Ringing`. Twilio's side is clean, and it is
**not** caller-ID filtering (see the correction at the top of v0.18). Needs a
Twilio carrier-ops ticket, not a code change.

### v0.22 — Dialog SmartPBX ("Client Connect") service mode (opt-in, Twilio unchanged)
**Reason:** Dialog needed a direct WebSocket media integration as an alternative ingress to
Twilio, modeled on Flico's existing Dialog integration and adapted to Kavya's English-only
pipeline and Yanolja PMS tools. See
`docs/superpowers/specs/2026-08-06-kavya-client-connect-design.md` (migration design) and
`docs/superpowers/specs/2026-08-07-kavya-smartpbx-parity-repair-design.md` (voice/protocol
corrections — the direct route was initially using the wrong ElevenLabs voice/model and an
over-strict `hangup` parser; both fixed to match Kavya's established English identity and the
supplied Dialog v06 extraction).

**Changes:**
- `server.py`: new `KAVYA_SERVICE_MODE` env var (`"twilio"` default | `"smartpbx"`) and
  `build_service_app()`, returning one of two mutually exclusive FastAPI apps — never both.
  `lifespan()` skips Twilio REST-client/handoff startup entirely when the mode is `smartpbx`.
  The SmartPBX app exposes exactly `/health`, `/smartpbx/status`, and `WS /ws/v1/smartpbx/media`,
  with `docs_url`/`redoc_url`/`openapi_url` disabled.
- Seven new modules: `smartpbx_protocol.py`, `smartpbx_gateway.py`, `smartpbx_transport.py`,
  `smartpbx_session.py`, `smartpbx_mcp.py`, `smartpbx_handover.py`, `smartpbx_diagnostics.py` —
  see **Service Modes** above for what each does.
- `MediaStreamSession` gained `_is_smartpbx_session()` / `_is_direct_smartpbx_english()` checks
  threaded through its STT/LLM/TTS/tool-execution paths, purely for SmartPBX-flavored structured
  logging (`smartpbx_media event=...`) and to gate the pre-tool filler differently on the direct
  English path; the underlying Twilio behavior is unchanged.
- `docker-compose.yml`: new `kavya-smartpbx` service under Compose profile `smartpbx` — loopback
  `127.0.0.1:8006`, explicit env allowlist (no `env_file`, no Twilio creds, no
  `HUMAN_AGENT_PHONE`), dedicated `./chroma_db_smartpbx` volume, `SMARTPBX_IMAGE_TAG` (default
  `disabled`), `mem_limit`/`cpus`/`pids_limit` caps.
- `SMARTPBX_RUNBOOK.md`: full cutover/rollback runbook — immutable image identity verification,
  `.env.smartpbx` provisioning, TLS bootstrap for `smartpbx-kavya.taskforceai.tech`, the five
  cutover gates, optional transfer drill with compulsory revoke, drain-before-stop rollback, and
  a guarded image-deploy script.
- `.github/workflows/build-kavya-image.yml` + `probe-kavya-image.yml`: publisher/probe pair
  gating which image tag+digest is trustworthy to deploy to the SmartPBX profile.
- Dialog MCP `transfer_call` replaces Twilio `<Dial>` for human handover on this path; the
  existing WhatsApp handover notification (`handover.py`) is reused as the fail-closed fallback.

**Not changed:** the Twilio `kavya` service, `/voice/*` routes, ConversationRelay, Media Streams,
and Twilio-based handover all remain the production default, untouched by this mode. The two
service modes are architecturally incapable of running in one process.

**How to test:** `pytest Kavya/tests/test_smartpbx_*.py`. Live cutover: follow
`SMARTPBX_RUNBOOK.md` in full — do not shortcut its gates.

### v0.23 — SmartPBX migration hardening: env knobs, deploy gates, and number-capture contract
**Reason:** Consolidate operational facts for the Dialog SmartPBX path and prevent repeatable incidents from docs drift during on-call.

**Changes:**
- Documented verified `server.py` defaults and clamp windows for new migration knobs:
  - `STT_ENDPOINTING_SILENCE_SECONDS` (`1.0`, clamp `[0.2, 5.0]`)
  - `STT_FINAL_GRACE_SECONDS` (`0.5`, clamp `[0.05, 5.0]`)
  - `DTMF_INTERDIGIT_TIMEOUT_SECONDS` (`6.0`, clamp `[1.0, 30.0]`)
  - `DTMF_OVERALL_TIMEOUT_SECONDS` (`30.0`, clamp `[5.0, 120.0]`)
  - `DTMF_MAX_DIGITS` (`15`, clamp `[1, 40]`)
  - `BARGEIN_MIN_CHARS` (`12`, clamp `[0, 200]`)
  - `BARGEIN_DEBOUNCE_SECONDS` (`0.6`, clamp `[0.0, 5.0]`)
- Added explicit compose allowlist guidance: `kavya-smartpbx` has no `env_file` and is an explicit environment allowlist; values in `.env.smartpbx` are ignored unless copied into its block.
- Added doc hooks for Dockerfile hardening rails: explicit module `COPY` manifest and build-time `RUN python -c "import server"` guard, plus `test_dockerfile_manifest.py` assertions.
- Added deploy-gate contract: read-only probe + publish workflow chain, immutable image + revision checks before deployment, and GHCR token-in/stdin workflow for private GHCR pull.
- Added explicit spoken-number capture contract: `capture_spoken_number` primary/default, `collect_number_via_keypad` fallback-only, deterministic normalization path through `expand_spoken_repeats`, `spoken_number_to_digits`, and `normalize_whatsapp`.

**Not changed:** regular Twilio call behavior, existing `server.py` primary routes, and normal handoff success path remain unchanged by this doc-only update.

**How to test:** `pytest Kavya/tests/test_dockerfile_manifest.py` and `pytest Kavya/tests/test_smartpbx_*.py`, then dry-run `SMARTPBX_RUNBOOK.md` gate command blocks against reviewed tooling only.

### Spoken-number capture contract
- Primary path for phone/WhatsApp/callback numbers remains spoken capture: use `capture_spoken_number` first and pass caller phrases exactly as spoken.
- `collect_number_via_keypad` is fallback-only and should be offered only after repeated spoken capture failures or when the caller explicitly requests keypad entry.
- Deterministic parser path lives in `Kavya/handover.py`: `expand_spoken_repeats` and `spoken_number_to_digits` expand double/triple/treble and zero variants, then `normalize_whatsapp` validates and normalizes length. This path is the single normalization source for live readback, booking phone, and WhatsApp payload so model arithmetic cannot drift from it.
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
    `KavyaSmartPBXSession._finish_once`) — a half-dictated number must reach the
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
- `SMARTPBX_TRANSFER_PENDING_TIMEOUT_SECONDS` is validated in `smartpbx_gateway.py` as an integer setting: default `300`, clamp `[30, 1800]`. An omitted line in `.env.smartpbx` falls back to this default via the compose allowlist's own `${SMARTPBX_TRANSFER_PENDING_TIMEOUT_SECONDS:-300}` default (every `kavya-smartpbx` passthrough carries one -- see the env-var drift table); a key present but blank falls back the same way, because `smartpbx_gateway._parse_bounded_integer`'s caller treats a blank value as absent too. Neither path is "absent" at the Python-process level under compose -- an omitted `.env.smartpbx` line still arrives as a real value, just the compose default rather than a truly missing key.
- `SMARTPBX_MAX_CALL_SECONDS` (Sep 2026): a hard per-call ceiling independent of both idleness and transfer-pending activity -- a call that never idles and never transfers must still end eventually. Default `3600`, clamp `[300, 7200]`. Closes the socket with code `1000` (an expected, polite close, not a policy violation) and the seven-field diagnostic `failure_class=max_call_duration`; the post-call/session-summary `close_reason` field carries `max_call_duration` too. `SmartPBXGateway.__init__` takes an injectable `clock` (default `time.monotonic`) so both ceilings are testable without a real multi-minute wait; it is deliberately not the process-global `time.monotonic` patched in place, since that would also perturb `SmartPBXMediaTransport`'s real-time audio pacing.
- `smartpbx_transport.py` has no env-driven knobs; transport behavior is bounded by internal backpressure constants (`_SEND_BACKPRESSURE_SECONDS=0.2`, `_SEND_BACKPRESSURE_POLL=0.005`).
- Missing `KAVYA_EN_ELEVENLABS_VOICE_ID` is a hard failure path in `english_voice_profile.load_kavya_english_voice_profile()` for English TTS: it raises `ValueError`, logged as a skip path for `_tts_elevenlabs()` rather than fabricated/fallback speech.
- Profile/credential preflight failures (Sep 2026, audit #10): a missing `GEMINI_API_KEY` or any `_preflight_language_profile` failure (`smartpbx_session.py`, `_end_call_without_language_profile`) emits `SESSION_START/FAILED/<class>` on the seven-field diagnostic (`DiagnosticFailureClass.GEMINI_API_KEY_MISSING` or `.PROFILE_UNAVAILABLE`) before resolving the terminal future, and the gateway's "raw is None" completion branch (`smartpbx_gateway.py`, reads `session.close_reason`) now closes `1011` instead of `1000`/`completed_normally` for that case and the pre-existing fatal-STT one -- previously both looked exactly like an ordinary completed hangup, with no diagnostic and no warning.
- Late tool completion after hangup (Sep 2026, audit #3): when a runner loses ownership (`_current_smartpbx_runner_owns_shared_state(tool_executed=True)` returns False) after `execute_tool` already ran -- both the per-tool bail immediately after `execute_tool` and the post-loop bail before history commit, in all three provider runners -- it now calls `self._record_smartpbx_late_tool_completion(...)` (for the current tool and, at the per-tool bail, every already-staged one) before discarding the round. This appends into `MediaStreamSession._smartpbx_late_tool_results`, a session-owned list separate from `full_transcript`, reusing `_append_booking_confirmation_marker`'s own success/`create_booking` filtering so every other tool stays a no-op. `_arm_endpointing` now tracks the dispatched endpointing→LLM→tool round as `self._smartpbx_active_runner_task` (previously fire-and-forget, audit #11) so `KavyaSmartPBXSession._finish_once_locked` can `asyncio.wait_for(asyncio.shield(...), timeout=smartpbx_session.LATE_TOOL_RESULT_WAIT_SECONDS)` (10s) for it to settle before snapshotting the post-call transcript, then merges `_smartpbx_late_tool_results` in. The wait is shielded so a timeout never cancels the tool call itself (consistent with the pre-existing "never cancel an in-flight tool" policy). `SmartPBXGateway._cleanup`'s per-operation timeout for the "session" step was raised from a flat 5s to 20s (`cleanup_timeouts` dict) to comfortably cover the up-to-5s STT-stop join plus the up-to-10s late-tool wait; transport/lease stay at 5s.
- Event-loop hygiene (Sep 2026, audits #5/#8/#9):
  - `tools._await_turn_delivery` (the `transfer_to_human` announcement-delivery wait) no longer busy-spins `await asyncio.sleep(0)` once per loop tick. `server.py`'s `_send_tts_done` and `_handle_bargein` now `.set()` `MediaStreamSession._smartpbx_delivery_event` on every progress event (a delivered sentence, or a generation bump that makes the wait moot); the waiter blocks on that event instead, falling back to the old busy-spin only for a pipeline stand-in that lacks the attribute.
  - `MediaStreamSession._handle_bargein` is now idempotent per speak generation: it captures `self._speak_generation` synchronously as its first statement and returns immediately if that same generation was already claimed by another (concurrent or prior) call, via `self._smartpbx_bargein_claimed_generation`. Without this, two STT callbacks racing in while `_is_speaking` is still True (an interim and a final, or two interims) both ran the full cancel/bump/retain cycle, and the second run could supersede the caller's own new utterance and drop it.
  - English/ElevenLabs TTS now has its own pre-audio window, mirroring Sinhala/Gemini's. `_pre_audio_synthesis_active()` gained a second branch: Gemini's existing "in flight, not yet speaking" shape, plus a new `_smartpbx_en_pre_audio_active`/`_smartpbx_en_pre_audio_generation` pair that `_tts_elevenlabs` sets at request start (English sets `_is_speaking` True immediately, unlike Gemini, so a genuine >=`BARGEIN_MIN_CHARS` interruption can still barge in during TTFB) and clears via `_smartpbx_end_en_pre_audio_window()` at the exact moment the first frame reaches the transport -- not when the whole utterance finishes, which would silently disable ordinary barge-in for the rest of the reply. A sub-threshold/debounced STT result arriving in that narrow window now routes through the existing `_handle_pre_audio_stt` buffering (and is flushed via `_flush_pre_audio_stt` in `_tts_elevenlabs`'s `finally` if the request fails before ever emitting audio) instead of being silently dropped.

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
