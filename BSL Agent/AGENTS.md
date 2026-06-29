# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

This is a demo voice agent for **Bank of Sri Lanka (BSL)** that handles inbound personal and business banking calls. The persona is a **generic "virtual assistant"** — no name, neutral English voice, professional and concise. A caller dials in, states what they want (Account Balance / Block Debit Card / Account Details, plus ad-libs for Recent Transactions / Loans / Standing Orders), gives the account number conversationally, then completes a **fixed 3-field voice verification** (NIC, DOB, Mother's Maiden Name — asked all together in a single turn) before the agent reads back the requested data. Mother's maiden name is matched **phonetically via Double Metaphone** because telephony STT cannot reliably transcribe Sinhala names (e.g. "Jayasinghe" arrives as "giant singer" / "Jinger" / "Ja"). After serving the request the agent loops back to ask "Is there anything else I can help you with today?" until the caller is done. If verification fails 3 times the agent verbally hands off to a live representative — the line stays open, no real Twilio Dial fires.

The demo is **English only**. There is no IVR language menu, no Sinhala/Tamil code paths, no Media Streams. There is a single voice endpoint, `/voice/incoming`, that returns TwiML with a `<ConversationRelay>` element.

The backend is entirely **mocked**: 6 accounts (5 customers; Nimal Perera holds two — a personal Current and a business account under Perera Tech Solutions) live in `mock_customers.json`. Card-block state is per-WebSocket-session only and never mutates the JSON file, so demo-2 caller never sees demo-1 caller's mutation.

**External setup the user must do before this works:**
- DNS A-record `bsl.taskforceai.tech` → `67.207.90.109` in the `taskforceai.tech` zone
- Twilio Console → Phone Numbers → `+19476669436` → Voice → Webhook: `https://bsl.taskforceai.tech/voice/incoming` (HTTP POST)
- n8n workflow at `/webhook/bsl-transcript` toggled **Active** (draft mode returns 404)

## End-to-End Call Flow

1. Caller dials the Twilio number (`+19476669436`). Twilio POSTs to `/voice/incoming`.
2. The server captures the caller's phone (`From` param — used for **audit logs only**, never for auto-identification or SMS), normalizes it to E.164, and stashes it in a module-level dict keyed by Call SID. The script's Step 1 collects the account number conversationally — caller phone is strict pass-through.
3. TwiML response contains a `<ConversationRelay>` pointing Twilio at `/ws/conversation` with a generic greeting: *"Welcome to Bank of Sri Lanka. You're speaking with our virtual assistant. What can I assist you with today?"*
4. Twilio opens the WebSocket; the server receives `setup`, then `prompt` events as the caller speaks.
5. The agent drives the strict 5-step BSL FSM:
   - **Step 1 — Customer Intention.** Identify intent (Balance / Block Card / Account Details, or ad-libs for transactions / loans / standing orders). Ask in one combined question: *"Could you please state your account number and let me know if it's a Personal, Current, or Business account?"* Read back the **last 4 digits** of the account number for confirmation before any tool fires.
   - **Step 2 — Voice Verification.** Fixed 3 fields (NIC, DOB, Mother's Maiden Name) asked together in a single turn — never one-at-a-time. Business-account callers get the "of the primary account holder" wording variants. One `verify_customer_identity` call per attempt, never one per field.
   - **Step 3 — Identity Confirmation.** On `verified=true`, speak *"Thank you for that. I've confirmed your identity successfully. Give me just a moment while I retrieve that for you."* and call the tool corresponding to Step-1 intent. On `account_not_found=true` (the caller misspoke the account number), apologise and re-collect just the account number — **this does NOT burn a verification attempt**. On `verified=false`, speak *"I'm sorry, I wasn't able to match that information. You have [X] attempt(s) remaining — please try once more."* and re-ask the three security questions together. After 3 fails, speak the transfer line and call `request_live_agent_handoff(reason="verification_failed")`.
   - **Step 4 — Serve Request.** Different response template for Personal/Current vs Business (verbatim from script). Amounts spoken in LKR, naturally formatted (*"three hundred fifty-four thousand, two hundred seventeen rupees"*).
   - **Step 5 — Wrap-Up / Loop.** Ask *"Is there anything else I can help you with today?"* If yes, return to Step 1 (re-verification not required for the same already-verified account in the same session). If no, speak *"It was a pleasure assisting you today, [Name / Company Name]. On behalf of Bank of Sri Lanka, we wish you a wonderful day. Goodbye!"*
6. When the WebSocket closes (caller hangs up — handoff does not force a hang-up), the `finally` block fires `asyncio.create_task(process_post_call_data(...))` — extracts structured data (caller_phone, account_no_last4, account_holder, company_name, intent, verified, verification_attempts, actions_taken, outcome, summary) from the full transcript and POSTs to the n8n webhook at `/webhook/bsl-transcript`.

## Key Modules

- `server.py` — FastAPI app. `/voice/incoming` HTTP endpoint, `/ws/conversation` WebSocket for ConversationRelay, `/health`. Owns per-call state: conversation history, full (untrimmed) transcript, caller phone, `account_no_under_discussion`, `verification_attempts` counter (max 3), `verified_account` flag, `card_block_state` (session-local override of `card.status`), and a `handoff_already_fired` flag per WebSocket session. No IVR, no Media Streams, no language routing — English only. `_handoff_just_executed()` inspects the tail of conversation_history after each LLM turn; when it fires, the server logs the event but **does NOT** emit a ConversationRelay `end` message — the call stays open and the caller hangs up when ready. The ConversationRelay voice is chosen via `ELEVENLABS_CR_VOICE` (format `<voice_id>-<model>`); falls back to a hardcoded default when unset. The system prompt enforces strict step ordering and forbids the LLM from asking for a phone number.
- `tools.py` — Eight tool definitions exposed in Anthropic / OpenAI / Gemini formats: `verify_customer_identity`, `get_account_balance`, `block_debit_card`, `get_account_details`, `get_recent_transactions`, `get_loans`, `get_standing_orders`, `request_live_agent_handoff`. Dispatcher `execute_tool(...)` always receives `caller_phone` from the server for audit logs — the LLM never sees or asks for it. Tool schemas intentionally have no phone field.
- `bsl_api.py` — Mocked backend for the eight tools plus the shared aiohttp `get_session()` / `close_session()` helpers used by `post_call.py`. Holds `verify_customer_identity` (looks up the record first; if the account isn't found, short-circuits with `account_not_found: true` **without burning a verification attempt** so the agent re-asks the account number instead of the identity fields; otherwise increments the per-session attempt counter and runs `mock_db.verify_identity`), `get_account_balance`, `block_debit_card` (mutates session state via `account_state.py`, never the JSON file), `get_account_details` (branches on `is_business`), `get_recent_transactions` (returns most-recent-first via `reversed(...)[:limit]`), `get_loans`, `get_standing_orders`, and `request_live_agent_handoff` (returns `{handoff: true, reason, message}` with the verbatim transfer line; no ConversationRelay end is sent).
- `mock_db.py` — Loads `mock_customers.json` once at import time. Exposes `lookup_account(account_no)` (accepts full `XXXX-XXXX-XXXX`, 12-digit, 8-digit (falls back to trailing last-4 — STT often drops one 4-digit group), or last-4 lookup), `verify_identity(account_no, nic, dob, mothers_maiden_name)` (lenient: case/whitespace-insensitive, NIC tolerates trailing `V`/`B` toggle, DOB accepts multiple date formats with LK day-month-year canonical, mother's maiden name uses **Double Metaphone phonetic matching** via the `metaphone` package — tokenised, accepts set intersection of phonetic codes, Levenshtein ≤2 between code pairs, OR prefix-match — with Levenshtein ≤2 spelling-fallback for close typos like `Jayasingh` vs `Jayasinghe`), and `_normalize_account_no(raw)` (canonicalises `"1042 8837 9201"` / `"1042-8837-9201"` / `"104288379201"` / `"88379201"` / `"9201"` / `"ending in 9201"` to canonical hyphenated form or last-4).
- `mock_customers.json` — The 6 BSL demo accounts. Top-level keys are normalized account numbers (`XXXX-XXXX-XXXX`). Each value has `account_no`, `account_no_last4`, `account_holder`, `company_name` (null for personal), `account_type`, `is_business`, `branch` (used by `get_account_details` for the Step 4 readout, NOT for verification), `opened_date`, `verification` (NIC, DOB, Mother's Maiden Name — note: `branch` may still be present in the JSON but is ignored by `verify_identity`), `summary` (balances, channels, registered contact), `card`, `loans`, `standing_orders`, full 30-day `transactions` ledger (stored oldest-first; `get_recent_transactions` reverses before slicing). A `_multi_account_holders` block lists Nimal Perera's two accounts; the agent disambiguates implicitly by which account number the caller states at Step 1.
- `account_state.py` — Per-WebSocket-session state store keyed by Call SID. In-memory dict only — no disk persistence (sessions die with the WebSocket; matches the script's loop-back-within-call design). Holds `verification_attempts`, `verified_account`, and `card_block_state` (so a card "blocked" in this session reads as blocked for the rest of the same call but does NOT leak to the next caller).
- `post_call.py` — Runs after the WebSocket closes. LLM-extracts structured call data (caller_phone, account_no_last4, account_holder, company_name, intent, verified, verification_attempts, actions_taken, outcome ∈ `served | verification_failed | handed_off_to_live_agent | caller_hung_up`, summary) from the transcript and POSTs to `${N8N_BASE_URL}/webhook/bsl-transcript`. All errors caught and logged — webhook 404s (e.g. inactive n8n workflow) never affect the call.
- `knowledge_base.py` — ChromaDB-backed RAG over `knowledge_docs/`. Chunked at 500 chars with 50-char overlap, embedded with `all-MiniLM-L6-v2`. KB context is prepended to each user turn so it stays fresh. Wired and ready, but **the BSL demo's data lives in `mock_customers.json` consumed via tools, NOT in RAG** — RAG over numeric balances/transactions is unreliable. The pipeline is here so supplementary docs (branch addresses, interest-rate sheets) can be dropped into `knowledge_docs/` later.
- `knowledge_docs/` — Empty by default. Anything dropped in is auto-indexed on next startup.
- `test_voice_elevenlabs.py` — Local smoke test. Typed input → LLM with the 8 BSL tools → ElevenLabs TTS playback. Useful for testing tool wiring and prompt tweaks without needing Twilio.

## Environment Variables

Copy `.env.example` to `.env` and fill in keys. Required for a working call:

- `LLM_PROVIDER` — `"claude"` (default) / `"openai"` / `"gemini"`
- `ANTHROPIC_API_KEY`, `CLAUDE_MODEL` — **default model `claude-sonnet-4-6`** (NOT the older `claude-sonnet-4-20250514`, which retires June 15 2026)
- `OPENAI_API_KEY`, `OPENAI_MODEL` — only if switching provider
- `GEMINI_API_KEY`, `GEMINI_MODEL` — only if switching provider
- `ELEVENLABS_API_KEY` — used by `test_voice_elevenlabs.py` only (the TTS on a real call goes through Twilio ConversationRelay, which authenticates to ElevenLabs on its own)
- `ELEVENLABS_VOICE_ID` — used by the local smoke test only
- `ELEVENLABS_CR_VOICE` — the voice served to Twilio ConversationRelay, formatted as `<voice_id>-<model>` (e.g. `bm3QvaZ3fUSCRBC3UV1f-flash_v2_5`). Swap voices by changing this env var and recreating the container (`docker compose up -d --force-recreate bsl-agent`; `docker compose restart` does NOT re-read `.env`)
- `TWILIO_ACCOUNT_SID=ACd1be89b6888b7338afa0adb154d4e3ca`, `TWILIO_AUTH_TOKEN`, `TWILIO_PHONE_NUMBER=+19476669436` — telephony. No SMS is sent in BSL (the script doesn't require it); these are kept for telephony auth only
- `N8N_POSTCALL_WEBHOOK` — path the post-call webhook posts to (default `/webhook/bsl-transcript`). The n8n workflow at that path must be **toggled Active** — draft mode returns 404
- `N8N_BASE_URL` — n8n host; defaults to `https://automation.taskforceai.tech`
- `PORT` — FastAPI port, default 8000

## Important Rules

- **Fixed 3-field verification per call** — NIC, DOB, Mother's Maiden Name. Asked all together in a single turn, not one-at-a-time. One `verify_customer_identity` call per attempt, not one per field. Branch verification was removed (it added no security since `is_business` already gates the business-account flow, and STT-mangled Sri Lankan place names blew up the false-negative rate).
- **Phonetic matching for mother's maiden name.** Telephony STT cannot transcribe Sinhala names reliably — *Jayasinghe* arrives as *"giant singer"*, *"Jinger"*, *"Ja senior"*, *"J singha"*, sometimes just *"Ja"*. `mock_db.verify_identity` uses Double Metaphone (`from metaphone import doublemetaphone`) tokenised on both given and expected, accepts (a) set intersection of phonetic codes, (b) any pair of codes within Levenshtein ≤2, OR (c) any pair where one is a prefix of the other. A Levenshtein ≤2 spelling-fallback handles close typos like *Jayasingh*. The trade-off is wider false-positive surface, accepted because verification is 3-factor and NIC + DOB stay strict.
- **`account_not_found` short-circuits without burning an attempt.** When `lookup_account` returns `None` (caller misspoke the account number), `bsl_api.verify_customer_identity` returns `account_not_found: true` and the agent re-asks the account number only — keeping the already-collected NIC/DOB/maiden name. The attempt counter does not increment. Account-number mistakes are an STT problem, not an identity-failure signal.
- **Lenient field-matching is by design.** Telephony STT is noisy. Case/whitespace-insensitive everywhere, NIC accepts trailing `V`/`B` toggle (STT often confuses them) and expands spoken digits ("double five" → "55"), DOB accepts multiple date formats (LK day-month-year is canonical). Don't tighten without a better STT substitute.
- **Account-number normalisation is charitable.** `_normalize_account_no` accepts 12-digit (with or without hyphens/spaces), 8-digit (falls back to trailing last-4 — common STT failure mode where one 4-digit group is dropped), and 4-digit last-4 lookups. Spoken numbers ("one zero four two") are expanded before digit extraction.
- **Caller phone is injected by the server, never asked.** The server reads `From` from the Twilio POST and passes it as `caller_phone` on every `execute_tool(...)` call. Tool schemas intentionally have no phone parameter. Pass-through-only — used for audit logs, never for auto-identification or SMS.
- **Read back the last 4 digits of the account number for confirmation before any tool fires.** Primary defence against STT mis-hearing the account number.
- **Never speak NIC, DOB, or mother's maiden name back to the caller.** They're verification secrets. The system prompt forbids it.
- **Never read the full account number or full card number aloud — last 4 digits only** (e.g. *"ending in 4412"*).
- **After 3 failed verification attempts:** call `request_live_agent_handoff(reason="verification_failed")` → speak the transfer line (*"I'm afraid I'm unable to verify your identity at this stage. For your security, I'll transfer you to one of our team members who can assist you further. Please hold."*) → STOP. Line stays open; the caller hangs up. No real Twilio Dial.
- **Block-card mutates session state, never the JSON file.** `block_debit_card` writes to `account_state.py`'s per-Call-SID dict. Demo-2 caller never sees demo-1 caller's mutation. Filesystem mutation across calls is a bug magnet.
- **All amounts spoken in LKR, naturally formatted.** *"three hundred fifty-four thousand, two hundred seventeen rupees"* — not *"354217.00 LKR"*.
- **Multi-account customer (Nimal Perera) is disambiguated implicitly by which account number the caller states at Step 1.** No explicit branching code; the lookup is exact.
- **Different response template per `is_business`** for Step 4 (Balance / Block Card / Account Details). Verbatim from `_phase0_script_extract.md`.
- **Re-verification not required when looping in the same session.** If the caller asks about a different account at Step 5, treat it as a fresh Step 1 and re-verify. If they ask about the same already-verified account, skip verification.
- **English only.** All Sinhala/Tamil code paths, IVR logic, Media Streams session code, Azure TTS, and Google Cloud STT were stripped. Do not reintroduce.
- **Live-agent handoff is verbal only — the call does NOT hang up.** `request_live_agent_handoff` returns `{handoff: true, reason, message}`. The agent speaks the message, the server logs the event via `_handoff_just_executed()`, but NO ConversationRelay `end` is sent.
- **`docker compose restart` does NOT reload `.env`.** Use `docker compose up -d --force-recreate bsl-agent` after any `.env` change.
- **Post-call failures never affect the call.** Everything in `post_call.py` is wrapped in try/except and fired as `asyncio.create_task()`. Common failure: n8n workflow toggled OFF → 404 *"POST bsl-transcript is not registered"*. Fix in n8n, not in code.
- **Mocks to replace before production:** `mock_db.py` (account lookup against real BSL core banking), the 8 tool implementations in `bsl_api.py` (real BSL APIs), and `account_state.py` in-memory dict (should be Redis or a proper KV store for horizontal scale).

## Running Locally

```bash
pip install -r requirements.txt
python test_voice_elevenlabs.py    # local smoke test (Phase 4 retargets this for BSL)
python server.py                    # full server
```

Twilio smoke test: point your Twilio number's Voice webhook at `https://<tunnel>/voice/incoming` (POST), dial in, and walk through Step 1 → Step 5 with one of the accounts in `mock_customers.json` (e.g. `1042-8837-9201` for Nimal Perera personal).

## Docker / Deploy

**For Python/JSON file changes (fastest — ~10 seconds):**
```bash
# From the BSL Agent directory on WSL:
scp server.py root@67.207.90.109:/opt/bsl-agent/server.py
ssh root@67.207.90.109 "docker cp /opt/bsl-agent/server.py bsl-agent:/app/server.py && docker restart bsl-agent"
```
Repeat for any other changed files (`tools.py`, `mock_db.py`, `mock_customers.json`, etc.) before restarting.

**Never use `deploy.sh deploy` for Python-only changes** — it rebuilds the entire Docker image from scratch (PyTorch ~530MB, CUDA ~366MB) and takes 5+ minutes. Only use it when `Dockerfile`, `docker-compose.yml`, or `requirements-prod.txt` change.

```bash
./deploy.sh setup    # first-time VPS provisioning only
./deploy.sh logs     # tail container logs
./deploy.sh status   # health check
```

Targets a DigitalOcean VPS (`67.207.90.109`) behind nginx (SSL termination, WebSocket upgrade, rate-limited `/voice/incoming`). Container runs `uvicorn server:app` on container internal port `8000`, mapped to host `127.0.0.1:8002` (Kavya occupies 8000, Sofia/SLIC 8001, BSL 8002). nginx is the only thing bound to 443 and routes `bsl.taskforceai.tech` → `127.0.0.1:8002`.

**VPS SSH access:** `ssh root@67.207.90.109` (user is `root`, not `thiva`).

## Server Constants

- `MAX_TOKENS = 600` — caps reply length but with enough headroom for natural readouts of 5+ transactions / loans / standing orders. Was 300 originally; bumped after Gemini hit `FinishReason.MAX_TOKENS` mid-readout on transaction summaries.
- `MAX_HISTORY_MESSAGES = 20` — trimmed window, format-aware so orphaned tool results / tool calls are dropped together.
- `MAX_TOOL_ROUNDS = 5` — how many tool-use rounds per user turn before the loop bails.

## Adding a Mock Account

1. Open `mock_customers.json`.
2. Add a new top-level key with the canonical account number (`"7890-1234-5678"` — hyphens between 4-digit groups).
3. Fill in `account_no`, `account_no_last4`, `account_holder`, `company_name` (or `null`), `account_type`, `is_business`, `branch` (used by `get_account_details` readout only, not for verification), `opened_date`, `verification` (NIC/DOB/Mother's Maiden Name — these are the 3 things the agent will check; mother's maiden name is matched phonetically so the exact spelling matters less than the sound), `summary`, `card`, `loans`, `standing_orders`, `transactions` (oldest-first; runtime reverses before slicing).
4. If the holder already has another account in the file, add their name to `_multi_account_holders` with both account numbers — the agent disambiguates by which number the caller states at Step 1, no code change needed.
5. Restart `server.py` — `mock_db._load()` only runs at import time.
6. Dial in, give the new account number at Step 1, answer the 3 verification questions (NIC, DOB, mother's maiden name) with the values you put in `verification`, and the agent should serve the request.

## Notes for Future Edits

- The entrypoint is `server.py` (uvicorn loads `server:app`). Don't rename it.
- `tools.py`, `bsl_api.py`, `mock_db.py`, `account_state.py`, `post_call.py`, `mock_customers.json` are BSL-specific source of truth. Everything else (`Dockerfile`, `docker-compose.yml`, `nginx.conf`, `deploy.sh`, `.env.example`, `knowledge_base.py`, this file) was structurally rebranded from the SLIC source.
- `knowledge_base.py` is domain-agnostic. Add supplementary docs (branch lists, FAQ, rate sheets) to `knowledge_docs/` and they're auto-indexed on next startup.
- The script (`_phase0_script_extract.md`) is the verbatim source of truth for every spoken line. Keep `_build_system_prompt()` aligned to it; if the script wording changes, change the prompt, not the other way round.
- After modifying any code in this directory, run `graphify update .` from the project root so the next session sees the new module structure.

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
