# BSL — Bank of Sri Lanka Voice Agent

A demo inbound **banking** voice agent for **Bank of Sri Lanka**. A caller states their request
(balance / block debit card / account details, plus ad-libs for transactions, loans, standing
orders), gives the account number, completes a **fixed 3-field voice verification** (NIC, DOB,
Mother's Maiden Name — asked together), then the agent reads back the requested data.

- **Language:** English only (no IVR menu, no Media Streams)
- **LLM:** configurable — Claude (default) / OpenAI / Gemini
- **Telephony/TTS:** Twilio `<ConversationRelay>` + ElevenLabs
- **Verification:** Mother's maiden name matched **phonetically** (Double Metaphone) to survive noisy Sinhala-name STT
- **Backend:** fully **mocked** (`mock_customers.json`) — replace before production
- **Server:** FastAPI / uvicorn — host port `127.0.0.1:8002`

## Key files
| File | Purpose |
|---|---|
| `server.py` | FastAPI app: `/voice/incoming`, `/ws/conversation`, FSM state |
| `tools.py` + `bsl_api.py` | 8 banking tools (verify, balance, block card, …) — mocked backend |
| `mock_db.py` + `mock_customers.json` | 6 demo accounts + lenient lookup/verify |
| `account_state.py` | Per-call session state (card-block is session-local) |
| `post_call.py` | Post-call structured extract → n8n webhook |
| `knowledge_base.py` | ChromaDB RAG (wired; demo data lives in mocks, not RAG) |

## Run locally
```bash
cp .env.example .env      # fill in API keys (TWILIO_* are placeholders — add your own)
pip install -r requirements.txt
python server.py
```

## Deploy
DigitalOcean VPS via `./deploy.sh`. For Python-only changes, `scp` the file + `docker restart bsl-agent` (don't rebuild — see CLAUDE.md).

## Full context for AI sessions
See **[CLAUDE.md](./CLAUDE.md)** (also exposed as `AGENTS.md`). Part of the [`full-voice-agent`](../) monorepo.
