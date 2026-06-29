# Fiona — Flico Voice Agent

**Fiona** is a multilingual inbound phone agent for **Flico** (flico.lk), a Sri Lankan technology and
electronics retailer. She is **informational only** — no booking tools, no external APIs — answering
product, price, store-location, delivery, and policy questions from a ChromaDB knowledge base.

- **Languages:** IVR/DTMF menu — `1` English, `2` Tamil, `3` Sinhala
- **LLM:** configurable — Claude (default) / OpenAI / Gemini
- **Telephony/TTS:** Twilio; English → ConversationRelay + ElevenLabs, Tamil → Media Streams + ElevenLabs multilingual, Sinhala → Media Streams + OpenAI `gpt-4o-mini-tts`
- **STT:** Google Cloud Speech (Media Streams paths)
- **Server:** FastAPI / uvicorn — host port `127.0.0.1:8003`

> Also contains an **additive Asterisk/SIP pilot** path (see the repo-root `asterisk-flico/`). It does
> not replace the Twilio/ConversationRelay path and is off by default (`ENABLE_ASTERISK_ARI=false`).

## Key files
| File | Purpose |
|---|---|
| `server.py` | Production server (IVR + ConversationRelay + Media Streams) |
| `knowledge_base.py` + `knowledge_docs/flico_info.txt` | ChromaDB RAG over Flico catalog/policies |
| `asterisk_ari.py`, `asterisk_rtp.py`, `media_transport.py` | Asterisk/SIP pilot transport |

## Run locally
```bash
cp .env.example .env
pip install -r requirements.txt
python server.py
```

## Deploy
DigitalOcean VPS via `./deploy.sh`. `GET /health` (and `/asterisk/status` when the SIP pilot is enabled) for health checks.

## Full context for AI sessions
See **[CLAUDE.md](./CLAUDE.md)** (also exposed as `AGENTS.md`). Part of the [`full-voice-agent`](../) monorepo.
