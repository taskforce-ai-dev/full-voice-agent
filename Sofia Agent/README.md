# Sofia — BuyAbans Voice Agent

**Sofia** is a multilingual inbound phone agent for **BuyAbans** (Abans, Sri Lanka). She is
**informational only** — no booking, no PMS, no n8n — answering home-product, price, store-location,
warranty, delivery, and policy questions from a ChromaDB knowledge base.

- **Languages:** IVR/DTMF menu — `1` English, `2` Tamil
- **LLM:** configurable — Claude (default) / OpenAI / Gemini
- **Telephony/TTS:** Twilio; English → ConversationRelay + ElevenLabs, Tamil → Media Streams + ElevenLabs multilingual
- **STT:** Google Cloud Speech (Media Streams path)
- **Server:** FastAPI / uvicorn — host port `127.0.0.1:8001`

## Key files
| File | Purpose |
|---|---|
| `server.py` | Production server (IVR + ConversationRelay + Media Streams) |
| `knowledge_base.py` + `knowledge_docs/buyabans_info.txt` | ChromaDB RAG over Abans catalog/policies |

## Run locally
```bash
cp .env.example .env
pip install -r requirements.txt
python server.py
```

## Deploy
DigitalOcean VPS via `./deploy.sh` (`setup` | `deploy` | `logs` | `status`).

## Full context for AI sessions
See **[CLAUDE.md](./CLAUDE.md)** (also exposed as `AGENTS.md`). Part of the [`full-voice-agent`](../) monorepo.
