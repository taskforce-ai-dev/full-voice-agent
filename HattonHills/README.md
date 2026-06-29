# HattonHills — Hotel Reservations Voice Agent

A multilingual inbound **hotel reservations** voice agent for **Hatton Hills**, built on the same
codebase as the Kavya/Treehouse hotel agent (Twilio + configurable LLM + ChromaDB RAG, with
PMS/booking integration). Sinhala TTS uses **OpenAI `gpt-4o-mini-tts`** (voice `sage`).

- **Languages:** IVR/DTMF menu (English / Sinhala / Tamil — see `server.py`)
- **LLM:** configurable — Claude (default) / OpenAI / Gemini, with tool use
- **Telephony/TTS:** Twilio; English → ConversationRelay + ElevenLabs, Sinhala → Media Streams + OpenAI `gpt-4o-mini-tts` (`sage`), Tamil → Media Streams + ElevenLabs
- **Server:** FastAPI / uvicorn — host port in `docker-compose.yml`

> ⚠️ **Doc note:** this folder's `CLAUDE.md` is still largely inherited from Kavya and references
> "Treehouse Chalets". Treat the persona/business naming there as pending a rebrand to Hatton Hills.

## Key files
| File | Purpose |
|---|---|
| `server.py` | Unified production server (IVR + ConversationRelay + Media Streams); `OPENAI_TTS_VOICE` defaults to `sage` |
| `media_stream_server.py` | Standalone Media Streams variant |
| `tools.py`, `booking_api.py`, `post_call.py` | Tools, PMS integration, post-call extract |
| `knowledge_base.py` + `knowledge_docs/` | ChromaDB RAG over hotel info |

## Run locally
```bash
cp .env.example .env
pip install -r requirements.txt
python server.py
```

## Deploy
DigitalOcean VPS via `./deploy.sh`.

## Full context for AI sessions
See **[CLAUDE.md](./CLAUDE.md)** (also exposed as `AGENTS.md`). Part of the [`full-voice-agent`](../) monorepo.
