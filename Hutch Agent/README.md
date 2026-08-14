# Selina — Hutch Enterprise Inquiries Agent

**Selina** is an English-only inbound voice agent for **Hutch** (a Sri Lankan mobile
network operator). She is a **pure KB/inquiries agent** — no booking, no PMS, no
live call transfer — answering questions about Hutch plans, pricing, data
allowances, activation methods, and policies from a ChromaDB knowledge base.
When a question is genuinely outside the knowledge base, she collects the
caller's name and a callback/WhatsApp number and hands off to a WhatsApp-notify
tool so a Hutch operator can follow up later.

- **Language:** English only
- **Telephony:** **Dialog SmartPBX ("Client Connect") ONLY** — there is no
  Twilio number for this agent
- **LLM:** configurable — Claude (default, with tool calling) / OpenAI / Gemini
  (KB-only, no tool calling — see CLAUDE.md)
- **TTS:** ElevenLabs (`eleven_turbo_v2_5`)
- **STT:** Google Cloud Speech (default) or Azure Speech, via `STT_PROVIDER`
- **Server:** FastAPI / uvicorn — dev port `127.0.0.1:8040`, SmartPBX port `127.0.0.1:8041`

## Key files
| File | Purpose |
|---|---|
| `server.py` | Production server — Dialog SmartPBX ingress (default) + inert Twilio/ConversationRelay code paths kept for fleet consistency |
| `knowledge_base.py` + `knowledge_docs/hutch_info.txt` | ChromaDB RAG over Hutch plans, pricing, and policies |
| `handover.py` + `tools.py` | WhatsApp-notify handover — the `notify_human_handover` tool, phone normalisation, n8n webhook POST |
| `smartpbx_protocol.py` / `smartpbx_gateway.py` / `smartpbx_transport.py` | Dialog SmartPBX wire protocol, session admission/auth, outbound audio transport (ported from Flico Agent) |
| `smartpbx_session.py` | Adapter binding one Dialog call into `MediaStreamSession` |

## Run locally
```bash
cp .env.example .env
pip install -r requirements-prod.txt
python server.py
```

## Deploy
This agent is **not yet wired into CI/CD auto-deploy** — see CLAUDE.md's
"Pending operator setup" section. Docker Compose profiles: `hutch` (local
dev) and `hutch-smartpbx` (the real SmartPBX target, profile `smartpbx`).

## Full context for AI sessions
See **[CLAUDE.md](./CLAUDE.md)**. Part of the [`full-voice-agent`](../) monorepo.
