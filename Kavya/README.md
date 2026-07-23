# Kavya — Mosvold Boutique Hotels Voice Agent

Kavya is a multilingual inbound phone agent for **Mosvold Boutique Hotels** (Sri Lanka), which
operates two properties: **Mosvold Villa** (Ahangama) and **Sundara by Mosvold** (Balapitiya).
She handles reservations and guest queries over Twilio, answers from a ChromaDB knowledge base,
and checks availability / takes bookings against **eZee Absolute PMS** via n8n webhooks.

- **Languages:** IVR/DTMF menu — `1` English, `2` Arabic, `3` Sinhala (Tamil implemented but unlisted)
- **LLM:** configurable — Claude (default) / OpenAI / Gemini, with tool use
- **Telephony/TTS:** Twilio; English → ConversationRelay + ElevenLabs, Arabic/Tamil → Media Streams + ElevenLabs, Sinhala → OpenAI `gpt-4o-mini-tts`
- **STT:** Google Cloud Speech (Media Streams paths)
- **Server:** FastAPI / uvicorn — host port `127.0.0.1:8000`

## Key files
| File | Purpose |
|---|---|
| `server.py` | Unified production server (IVR + ConversationRelay + Media Streams) |
| `media_stream_server.py` | Standalone Media Streams variant (reference) |
| `tools.py` | Tool defs (Anthropic/OpenAI/Gemini) + dispatch |
| `booking_api.py`, `kpms_service.py`, `yanolja_service.py` | PMS / booking integration |
| `knowledge_base.py` + `knowledge_docs/` | ChromaDB RAG over hotel info |
| `post_call.py` | Post-call summary → n8n → Google Sheets |

## Run locally
```bash
cp .env.example .env      # fill in API keys
pip install -r requirements.txt
python server.py
```

## Deploy
DigitalOcean VPS via `./deploy.sh` (`setup` | `deploy` | `logs` | `status`). Host port in `docker-compose.yml`.

## Full context for AI sessions
See **[CLAUDE.md](./CLAUDE.md)** (also exposed as `AGENTS.md`) for architecture, rules, and gotchas.
Part of the [`full-voice-agent`](../) monorepo.
