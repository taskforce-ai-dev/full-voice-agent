# SLIC — Sri Lanka Insurance Accident Hotline Agent

**Nimali** is a demo inbound **accident-hotline** agent for **Sri Lanka Insurance Corporation (SLIC)**.
A caller whose vehicle just had an accident dials in; Nimali takes the registration number (read back
digit-by-digit), verifies the policy, confirms the matched make/model, fuzzy-matches the caller's name,
asks only for the accident location, dispatches the nearest (mocked) assessor, gives the assessor name +
ETA, and sends the claim reference by **SMS** (never spoken aloud).

- **Language:** English only
- **LLM:** configurable — Claude (default) / OpenAI / Gemini
- **Telephony/TTS:** Twilio `<ConversationRelay>` + ElevenLabs
- **SMS:** real Twilio Programmable SMS when `TWILIO_*` set (mock SID otherwise)
- **Backend:** mocked (customers, assessor pool, claim refs) — replace before production
- **Continuity:** cross-call `active_session` (TTL default 5 min) for follow-up calls
- **Server:** FastAPI / uvicorn — host port in `docker-compose.yml`

## Key files
| File | Purpose |
|---|---|
| `server.py` | FastAPI app + emotion-aware intake FSM |
| `tools.py` + `claim_api.py` | 5 tools (verify policy, collect details, dispatch, SMS, handoff) |
| `mock_db.py` + `mock_customers.json` | Demo customers, reg-no + phone indexes |
| `active_session.py` + `active_sessions.json` | Cross-call session store (crash-safe snapshot) |
| `post_call.py` | Post-call claim extract → n8n webhook |

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
