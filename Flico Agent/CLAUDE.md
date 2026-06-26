# CLAUDE.md

## What This Is

Fiona is a multilingual AI voice agent for **Flico** (flico.lk), Sri Lanka's trusted technology and electronics retailer. Handles inbound phone calls via Twilio, uses a configurable LLM (Claude, OpenAI, or Gemini) for conversation, and grounds answers in a ChromaDB-based RAG knowledge base about Flico products, services, and policies.

The agent persona is **Fiona** — a warm, trilingual (English, Tamil, Sinhala) customer service agent. Language is selected via IVR/DTMF menu (press 1/2/3) at call start.

Fiona is an **informational agent only** — no booking tools, no external APIs. She answers queries about products, prices, store locations, delivery, and policies using the knowledge base.

**Single server mode:**
- `server.py` — Production server (IVR DTMF menu: English -> ConversationRelay+ElevenLabs, Tamil -> Media Streams+ElevenLabs multilingual, Sinhala -> Media Streams+OpenAI `gpt-4o-mini-tts`)

## Project File Map

```
Flico Agent/
├── server.py                  # Production server (IVR + ConversationRelay + Media Streams)
├── knowledge_base.py          # ChromaDB RAG — chunk, embed, query knowledge docs
├── knowledge_docs/            # Source documents for RAG
│   └── flico_info.txt         # Flico product catalog, services, policies (crawled from flico.lk)
├── chroma_db/                 # ChromaDB vector store (auto-generated, gitignored)
├── Dockerfile                 # Production image (python:3.11-slim), runs server:app
├── docker-compose.yml         # Docker orchestration — port 127.0.0.1:8003, mounts GCP creds
├── nginx.conf                 # Reverse proxy — SSL termination, WSS upgrade, rate limiting
├── requirements-prod.txt      # Production dependencies
├── deploy.sh                  # Deployment script (setup/deploy/logs/status) for DigitalOcean VPS
├── full-voice-agent-a8a245fb37cb.json  # GCP service account JSON (Google Cloud STT credentials)
├── .env                       # Secrets — never committed (API keys, voice IDs, etc.)
├── .env.example               # Template for .env with all required/optional vars
└── CLAUDE.md                  # This file
```

## Commands

```bash
# Production server
python server.py

# Docker
docker compose build
docker compose up -d
docker compose logs -f flico

# Deploy to DigitalOcean VPS
./deploy.sh setup    # first-time provisioning
./deploy.sh deploy   # push code updates
./deploy.sh logs     # tail remote logs
./deploy.sh status   # health check
```

## Environment Setup

Copy `.env.example` to `.env`. Key groups:

**LLM provider** (pick one):
- `LLM_PROVIDER` — `"claude"` (default), `"openai"`, or `"gemini"`
- `ANTHROPIC_API_KEY`, `CLAUDE_MODEL`
- `OPENAI_API_KEY`, `OPENAI_MODEL`
- `GEMINI_API_KEY`, `GEMINI_MODEL`

**TTS/STT:**
- `ELEVENLABS_API_KEY`, `ELEVENLABS_VOICE_ID` — ElevenLabs TTS (English + Tamil)
- `AZURE_SPEECH_KEY`, `AZURE_SPEECH_REGION` — Azure TTS for Tamil (backup)
- `OPENAI_API_KEY` — OpenAI key; used for `gpt-4o-mini-tts` (Sinhala voice) and, if `LLM_PROVIDER=openai`, the LLM
- `OPENAI_TTS_MODEL` / `OPENAI_TTS_VOICE` / `OPENAI_TTS_INSTRUCTIONS` — Sinhala TTS config (defaults: `gpt-4o-mini-tts`, `coral`, a warm Sinhala-tone instruction)
- `SINHALA_TTS_URL` — (legacy) self-hosted Sinhala VITS base URL; only used by the dormant `_tts_sinhala` fallback
- `GOOGLE_APPLICATION_CREDENTIALS` — GCP service-account JSON for Google Cloud STT

**Telephony:**
- `TWILIO_ACCOUNT_SID` — AC996e3a70ee086a201167cba5fee782e9
- `TWILIO_AUTH_TOKEN`

## Architecture

### IVR Flow
```
Incoming call
  -> POST /voice/incoming (returns TwiML with <Gather> DTMF menu: 1=EN, 2=TA, 3=SI)
  -> Caller presses 1, 2, or 3 (no digit within timeout -> Redirect, defaults to EN)
  -> POST /voice/language-selected
  -> English: ConversationRelay WebSocket /ws/conversation?lang=en
  -> Tamil:   Media Streams WebSocket /ws/media-stream/ta
  -> Sinhala: Media Streams WebSocket /ws/media-stream/si
```

DIGIT map (`DIGIT_TO_LANG` in `server.py`): `1`->`en`, `2`->`ta`, `3`->`si`.

### TTS / STT routing by language
| Lang | Digit | Transport          | STT (Google)        | TTS                                     |
|------|-------|--------------------|---------------------|-----------------------------------------|
| en   | 1     | ConversationRelay  | google (telephony)  | ElevenLabs flash_v2_5 (cloned voice)     |
| ta   | 2     | Media Streams      | ta-IN (+en-US alt)  | ElevenLabs eleven_multilingual_v2        |
| si   | 3     | Media Streams      | si-LK (+en-US alt)  | OpenAI `gpt-4o-mini-tts` (24k PCM -> 8k μ-law) |

Sinhala TTS: the Media Streams path routes `lang="si"` to OpenAI
`gpt-4o-mini-tts` via `_tts_openai` (24 kHz PCM downsampled on the fly to 8 kHz
μ-law for Twilio framing). The self-hosted Sinhala VITS service (`_tts_sinhala`,
POSTs to `SINHALA_TTS_URL/tts?format=mulaw8k`) is **implemented but no longer
invoked** — it is dead/legacy code kept only for possible revival. `_tts_azure`
is likewise wired but unused for Sinhala. NOTE: `_speak()` dispatches
`en`/`ta` -> ElevenLabs and everything else (i.e. `si`) -> `_tts_openai`; there
is no live call path to `_tts_sinhala` despite its name.

### No Tools
Fiona has NO tools. All responses are generated from KB context + LLM. No external API calls.

### Knowledge Base
Files in `knowledge_docs/` chunked -> embedded with `all-MiniLM-L6-v2` -> stored in ChromaDB (`./chroma_db`). Supports `.txt`, `.md`, `.pdf`, `.json` files. Query embeddings LRU-cached. KB context injected as user message prefix per turn.

`retrieve_context()` filters listings by `property_type` + Colombo `zone` parsed
from the utterance. **Sticky constraints (Jun 25 2026):** it takes an optional
per-session `sticky` dict (owned by `MediaStreamSession.sticky_filters` and the
ConversationRelay handler's local `sticky_filters`) that REMEMBERS the last
non-null `property_type`/`zone` across turns — a value stated this turn overrides,
otherwise it inherits. This stops retrieval from losing "apartment" when a later
utterance only names an area (the old bug surfaced a house for "I'd love Colombo
5"). The carried constraint is also appended to the embedding query (single embed
per turn). `bedrooms` is in metadata but deliberately NOT a filter — occupancy
("4 people") is handled in the system prompt, never as a bedroom-count filter.

### Jun 25 2026 — persona + retrieval hardening (deployed)
- System prompt recast from passive "lettings consultant" to a confident,
  consultative real-estate **sales** persona (qualify -> build value -> handle
  objections -> advance to a viewing + lead capture), strictly KB-grounded.
- New **OCCUPANCY vs BEDROOMS** rule: "N people/family/guests" = occupancy, never
  a bedroom count (fixes the "4 people -> only 4-bedroom units" drift). Apartments
  start at 3 bedrooms; agent states that floor as a benefit, never invents a 1-/2-bed.
- New **SALES APPROACH** block + honour-the-requested-property-TYPE rule (don't
  drift apartment->house on area match).
- Rent must be read with its exact period ("per day" vs "per month" — e.g. P03 is
  per day), never assumed monthly.
- `/ws/conversation` now logs the WebSocket `close_code` on disconnect.
- **5-minute "cutoff" investigated & cleared:** no timeLimit in TwiML/`<Connect>`,
  nginx is 86400s, no server-side timer; Twilio call record for CAf8b519 was
  `duration=301s, status=completed` (caller hangup after lead captured). No Twilio
  5-min limit exists (trial=10min, prod default=4hr).
- Deploy note: `/opt/flico` docker-compose **volume-mounts** `server.py` /
  `knowledge_base.py`, so `docker compose up -d --force-recreate flico` ships code
  changes without an image rebuild. (`docker compose build` currently fails on the
  VPS with `No space left on device` — disk at ~89%, images ~56GB; unrelated to code.)

## Server Endpoints

- `POST /voice/incoming` — Returns TwiML with `<Gather>` DTMF language menu (1=EN, 2=TA, 3=SI)
- `POST /voice/language-selected` — Routes to ConversationRelay (EN) or Media Streams (TA, SI)
- `WebSocket /ws/conversation` — English ConversationRelay
- `WebSocket /ws/media-stream/{lang}` — Tamil/Sinhala Media Streams (Google STT + TTS); `{lang}` is `ta` or `si`
- `GET /health` — Health check

## Deployment

- **Server**: DigitalOcean VPS (67.207.90.109)
- **Domain**: `flico.taskforceai.tech`
- **Docker port**: `127.0.0.1:8003:8000` (Kavya=8000, Sofia/SLIC=8001, BSL=8002, Flico=8003)
- **Nginx**: Separate server block for `flico.taskforceai.tech` -> port 8003
- **SSL**: Certbot for `flico.taskforceai.tech`
- **Twilio**: Separate phone number, webhook -> `https://flico.taskforceai.tech/voice/incoming`
- **Twilio Account SID**: AC996e3a70ee086a201167cba5fee782e9
- **Sinhala TTS**: OpenAI `gpt-4o-mini-tts` (`_tts_openai`) is the active path.
  The self-hosted Sinhala VITS service (`sinhala-tts` container on the shared
  `taskforceai-net`, host port 8004 -> container 8000) is deployed but currently
  **unused** — `_tts_sinhala` and `_tts_azure` are wired but no live code path
  calls them. Revive by routing `si` to `_tts_sinhala` in `_speak()` if desired.

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
