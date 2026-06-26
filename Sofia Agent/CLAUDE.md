# CLAUDE.md

This file provides guidance to Claude Code when working with code in this repository.

## What This Is

Sofia is a multilingual AI voice agent for **BuyAbans** (Abans, Sri Lanka). Handles inbound phone calls via Twilio, uses a configurable LLM (Claude, OpenAI, or Gemini) for conversation, and grounds answers in a ChromaDB-based RAG knowledge base about Abans products, services, and policies.

The agent persona is **Sofia** — a warm, bilingual (English, Tamil) customer service agent. Language is selected via IVR/DTMF menu (press 1/2) at call start.

Sofia is an **informational agent only** — no booking tools, no eZee PMS, no n8n integration. She answers common queries about home products, prices, store locations, warranties, delivery, and policies using the knowledge base.

**Single server mode:**
- `server.py` — Production server (IVR DTMF menu: English -> ConversationRelay+ElevenLabs, Tamil -> Media Streams+ElevenLabs multilingual)

## Project File Map

```
Sofia Agent/
├── server.py                  # Production server (IVR + ConversationRelay + Media Streams)
├── knowledge_base.py          # ChromaDB RAG — chunk, embed, query knowledge docs
├── knowledge_docs/            # Source documents for RAG
│   └── buyabans_info.txt      # BuyAbans product catalog, services, policies
├── chroma_db/                 # ChromaDB vector store (auto-generated, gitignored)
├── Dockerfile                 # Production image (python:3.11-slim), runs server:app
├── docker-compose.yml         # Docker orchestration — port 127.0.0.1:8001, mounts GCP creds
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
docker compose logs -f sofia

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
- `GOOGLE_APPLICATION_CREDENTIALS` — GCP service-account JSON for Google Cloud STT

**Telephony:**
- `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`

## Architecture

### IVR Flow
```
Incoming call
  -> POST /voice/incoming (returns TwiML with <Gather> DTMF menu: 1=EN, 2=TA)
  -> Caller presses 1 or 2
  -> POST /voice/language-selected
  -> English: ConversationRelay WebSocket /ws/conversation?lang=en
  -> Tamil: Media Streams WebSocket /ws/media-stream/ta
```

### No Tools
Sofia has NO tools. All responses are generated from KB context + LLM. No external API calls for booking, availability, or any actions.

### Knowledge Base
Files in `knowledge_docs/` chunked -> embedded with `all-MiniLM-L6-v2` -> stored in ChromaDB (`./chroma_db`). Supports `.txt`, `.md`, `.pdf`, `.json` files. Query embeddings LRU-cached. KB context injected as user message prefix per turn.

## Server Endpoints

- `POST /voice/incoming` — Returns TwiML with `<Gather>` DTMF language menu (1=EN, 2=TA)
- `POST /voice/language-selected` — Routes to ConversationRelay (EN) or Media Streams (TA)
- `WebSocket /ws/conversation` — English ConversationRelay
- `WebSocket /ws/media-stream/{lang}` — Tamil Media Streams (Google STT + TTS)
- `GET /health` — Health check

## Deployment

- **Server**: Same DigitalOcean VPS as Kavya (67.207.90.109)
- **Domain**: `abans.taskforceai.tech`
- **Docker port**: `127.0.0.1:8001:8000` (Kavya uses 8000, Sofia uses 8001)
- **Nginx**: Separate server block for `abans.taskforceai.tech` -> port 8001
- **SSL**: Certbot for `abans.taskforceai.tech`
- **Twilio**: Separate phone number, webhook -> `https://abans.taskforceai.tech/voice/incoming`

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
