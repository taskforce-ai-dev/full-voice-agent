# Horizon Agent — Vidya (aviation-academy inquiry demo)

**Vidya** is a multilingual, **inquiry-only** website-demo voice agent for
**Horizon Airline & Aviation Academy** — a de-identified aviation-training
academy used on the Taskforce AI "book a demo" page. She answers questions about
courses, fees, entry requirements, durations, schedules, campuses and how to
apply, grounded in a ChromaDB RAG knowledge base. She takes **no** bookings,
enrolments or payments.

Built on the HattonHills/Kavya codebase (Twilio + configurable LLM + ChromaDB
RAG) but reduced to inquiry-only and rebranded to Vidya/Horizon.

- **Languages (four):** English, Russian, Arabic, Sinhala — chosen on the
  website demo card (no phone IVR; reached via the shared
  `hattonhills.taskforceai.tech` demo router, agent id `horizon`).
- **Language stack** (mirrors Kavya's Dialog line where one exists; transport is
  Twilio because a website demo is a browser call):
  - English / Russian → **ConversationRelay** (Claude brain + ElevenLabs voice)
  - Arabic → **Media Streams** (Claude brain + Azure STT `ar-SA` + ElevenLabs)
  - Sinhala → **Media Streams** (**Gemini** brain + **Gemini TTS** `Vindemiatrix`
    + Azure STT `si-LK`) — the Kavya Dialog stack. Falls back to Claude + OpenAI
    TTS (`sage`) if Gemini is unavailable / quota-exhausted.
- **Inquiry-only by construction:** `tools.py` returns no tools unless the
  explicit `HORIZON_ENABLE_BOOKING_TOOLS=true` opt-in is set — an inherited PMS
  key can never arm booking tools. Post-call egress is fail-closed
  (`DEMO_SAFE_BOOKINGS=true`): no transcript extraction, no n8n, no dashboard.
- **Server:** FastAPI / uvicorn, container `horizon-voice-agent`, host port
  `127.0.0.1:8050`, domain `horizon.taskforceai.tech`.

## Key files
| File | Purpose |
|---|---|
| `server.py` | Production server (ConversationRelay + Media Streams); Vidya persona; `_tts_gemini` Sinhala voice |
| `tools.py` | Tool exporters — return `[]` by construction (inquiry-only) |
| `post_call.py` | Post-call extraction — fail-closed by default (no egress) |
| `knowledge_base.py` + `knowledge_docs/horizon_info.txt` | ChromaDB RAG over the academy KB |
| `tests/` | Horizon contracts: inquiry-only tools, demo routing, no post-call egress, Vidya/Horizon branding, Sinhala stack defaults |

## Run locally
```bash
cp .env.example .env      # fill in real keys
pip install -r requirements.txt
python server.py
```

## Test
```bash
pytest tests
```

## Deploy
Image-mode: CI builds `ghcr.io/taskforce-ai-dev/horizon` and the VPS pulls it.
See **[CLAUDE.md](./CLAUDE.md)** for the full deploy/runbook. Part of the
[`full-voice-agent`](../) monorepo.
