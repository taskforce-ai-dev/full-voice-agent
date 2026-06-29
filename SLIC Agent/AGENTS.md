# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

This is a demo voice agent for **Sri Lanka Insurance Corporation (SLIC)** that handles inbound accident-hotline calls. The persona is **Nimali** — a Sri Lankan / South Asian English voice, warm and professional with explicit emotional adaptation to the caller. A caller whose vehicle has just been in an accident dials in, Nimali asks for the vehicle registration number, reads it back digit-by-digit for confirmation, verifies it against the SLIC policy database, announces the make and model that the system found so the caller knows the right vehicle was matched, asks for the caller's name and fuzzy-matches it against the policyholder (lenient to STT noise), asks ONLY for the accident location, dispatches the nearest available (mocked) assessor, tells the caller the assessor's name and ETA, and sends the claim reference by SMS — the claim reference is NEVER spoken aloud. If the vehicle is not on any active policy, or the name clearly doesn't match, Nimali verbally tells the caller a SLIC rep will take over and stays on the line (no forced hang-up).

The demo is English-only. There is no IVR language menu and no Sinhala/Tamil code paths — those were stripped out when duplicating from the Kavya hotel agent. There is a single voice endpoint, `/voice/incoming`, that returns TwiML with a `<ConversationRelay>` element.

The backend is entirely **mocked** except for SMS (real Twilio Programmable SMS when `TWILIO_*` env vars are set, mock SID otherwise): customers live in a local JSON file, active sessions are persisted to another local JSON file, assessor dispatch picks from a 6-entry pool matched against the caller-supplied location, and claim references are generated locally as `SLIC-CLM-YYYYMMDD-XXXXXX`. Replace every mock before any real SLIC integration.

## End-to-End Call Flow

1. Caller dials the Twilio number. Twilio POSTs to `/voice/incoming`.
2. The server captures the caller's phone (`From` param — used for SMS delivery and session continuation), normalizes it to E.164, and stashes it in a module-level dict keyed by Call SID.
3. `/voice/incoming` checks `active_session` for a non-expired record keyed to that phone (TTL 5 min by default). If one exists, the greeting is personalized as a follow-up and Nimali answers the caller's question directly without running a new intake.
4. TwiML response contains a `<ConversationRelay>` pointing Twilio at `/ws/conversation` with the welcome greeting (generic by default, personalized on continuation).
5. Twilio opens the WebSocket; the server receives `setup`, then `prompt` events as the caller speaks.
6. Nimali drives the vehicle-first intake (strict ordering enforced by the system prompt):
   - **Step 1** — ask for the vehicle registration number.
   - **Step 2** — read it back digit-by-digit and wait for "yes that's correct" before calling any tool.
   - **Step 3** — `verify_vehicle_policy`. If `verified=false`, Nimali apologises and calls `request_live_agent_handoff` (the call does NOT hang up — Nimali just verbally says a rep will take over and the line stays open for the caller to hang up themselves).
   - **Step 3b** — announce the vehicle make and model returned by `verify_vehicle_policy` (e.g. *"Great — I have a BMW X5 registered under this policy"*) so the caller knows the right car was matched. The customer name is NEVER revealed here.
   - **Step 4 — Identity check.** Nimali asks *"For verification, may I have your full name please?"* and silently fuzzy-matches the caller's answer against `customer_name` returned by `verify_vehicle_policy`. The rule is deliberately LENIENT — first-name OR last-name phonetic match is enough; "when in doubt, lean toward match" to tolerate noisy telephony STT. One polite retry before declaring mismatch. On mismatch, same handoff as verified=false.
   - **Step 5** — ask ONE question: where did the accident happen? On vague answers, ask ONCE for a landmark/junction/town. Injuries and vehicle condition are NOT asked — the field assessor captures those on-site.
   - `collect_accident_details(reg_no, location)` — stashes the location. Injuries and vehicle_condition accept empty strings.
   - `dispatch_nearest_assessor` — picks the assessor whose `base_city` best matches the accident location; falls back to the lowest-ETA assessor. Generates the claim reference, writes the `active_session` record, returns ETA + claim ref + assessor name.
   - **Step 7** — Nimali tells the caller the assessor's name and ETA only. The claim reference is NEVER spoken aloud; it goes out via SMS.
   - `send_confirmation_sms` — real Twilio SMS when `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_PHONE_NUMBER` are set (and the number belongs to that account); mock SID otherwise. Sends to the Twilio `From` number (the live caller), not the DB phone. Never raises; SMS failure is caught and logged.
7. When the WebSocket closes (caller hangs up — handoff no longer forces a hang-up), the `finally` block fires `asyncio.create_task(process_post_call_data(...))` — extracts structured claim data (caller_name, policy_no, vehicle make/model/reg_no, location, injuries, vehicle_condition, accident_description, assessor_name, ETA, claim_reference, sms_sent, dispatch_status, outcome, follow_up_needed, summary) from the full transcript and POSTs to the n8n webhook at `/webhook/slic-transcript`.

## Key Modules

- `server.py` — FastAPI app. `/voice/incoming` HTTP endpoint, `/ws/conversation` WebSocket for ConversationRelay, `/health`. Owns per-call state: conversation history, full (untrimmed) transcript, caller phone, active-session continuation check at call start, and a `handoff_already_fired` flag per WebSocket session. No IVR, no Media Streams, no language routing — English only. Contains `_handoff_just_executed()` which inspects the tail of conversation_history after each LLM turn; when it fires, the server logs the event but **does NOT** emit a ConversationRelay `end` message — the call stays open and the caller hangs up when ready. The ConversationRelay voice is chosen via `ELEVENLABS_CR_VOICE` (format `<voice_id>-<model>`, e.g. `bm3QvaZ3fUSCRBC3UV1f-flash_v2_5`); falls back to a hardcoded default when unset. The system prompt enforces emotional adaptation — Nimali slows down and softens for panicked callers, escalates urgency on injuries, etc.
- `tools.py` — Five tool definitions exposed in Anthropic / OpenAI / Gemini formats: `verify_vehicle_policy`, `collect_accident_details`, `dispatch_nearest_assessor`, `send_confirmation_sms`, `request_live_agent_handoff`. Dispatcher `execute_tool(...)` always receives `caller_phone` from the server for SMS delivery — the LLM never sees or asks for it. Tool schemas intentionally have no phone field. `collect_accident_details` requires only `reg_no` + `location`; `injuries` and `vehicle_condition` are optional and captured on-site by the assessor. `request_live_agent_handoff`'s description tells the model to deliver the spoken farewell and then stop calling tools or asking questions.
- `claim_api.py` — Mocked backend for the five tools plus the shared aiohttp `get_session()` / `close_session()` helpers used by `post_call.py`. Holds the `ASSESSORS` pool (6 entries covering Colombo/Kandy/Galle/Negombo/Kurunegala/Anuradhapura) and `_pick_nearest_assessor()` routing logic. `dispatch_nearest_assessor` generates the claim reference, writes the active-session record atomically, and clears the pending accident-details stash. `send_confirmation_sms` uses the sync Twilio SDK wrapped in `asyncio.to_thread`, sending to the live caller phone (the Twilio `From`, not the DB phone) so callers dialing from an alt number still receive the confirmation. Falls back to a mock SID when credentials are missing. `request_live_agent_handoff` now returns a farewell message that tells the caller to stay on the line ("I'm transferring your call to a live SLIC agent now. A representative will be with you shortly."); no ConversationRelay end is sent.
- `mock_db.py` — Loads `mock_customers.json` once at import time and builds two indexes: a phone-keyed customer dict (`lookup_by_phone`, `get_vehicles`, `is_known`) AND a reg-no-keyed vehicle index (`lookup_by_reg_no`). `_normalize_reg_no` canonicalises input like `"cba 1175"`, `"CBA1175"`, `"CBA-1175"` → `"CBA-1175"`. Phone normalization strips non-digits except `+` and auto-prefixes `+` for numbers starting with `94`.
- `mock_customers.json` — The demo customer file. Keys are E.164 phones (e.g. `+94771234567`). Each value has `name`, `policy_no`, and a list of `vehicles` (`make`, `model`, `reg_no`, `policy_item`). The reg-no index is built from these vehicles at load time — no separate edit required. To add a customer: add a new top-level key, restart the server (file is loaded at import time), done.
- `active_session.py` — Cross-call state store keyed primarily by normalized caller phone, with a secondary `get_by_reg_no()` lookup. In-memory dict is authoritative at runtime; `active_sessions.json` on disk is a crash-safety snapshot written atomically via `.tmp` + `os.replace`. TTL is driven by `SLIC_SESSION_TTL_SECONDS` (**default 300 = 5 min**). Exposes `get`, `get_by_reg_no`, `set`, `clear`, `remaining_eta_minutes`. `SessionRecord` is a dataclass holding phone, name, policy, vehicle dict, reg_no, assessor name, dispatch timestamp, ETA, claim ref, accident description, location, injuries, vehicle_condition, and expiry. `_load()` tolerates old snapshots that predate the added fields.
- `post_call.py` — Runs after the WebSocket closes. LLM-extracts structured claim data (caller_name, policy_no, vehicle_make/model/reg_no, location, injuries, vehicle_condition, accident_description, assessor_name, assessor_eta_minutes, claim_reference, sms_sent, dispatch_status, outcome, follow_up_needed, summary) from the transcript and POSTs to the n8n webhook. `dispatch_status` is one of `dispatched | follow_up_call | handed_off_to_live_agent | not_dispatched`. Env vars `N8N_BASE_URL` + `N8N_POSTCALL_WEBHOOK`. All errors caught and logged — webhook 404s (e.g. inactive n8n workflow) never affect the call.
- `knowledge_base.py` — ChromaDB-backed RAG over `knowledge_docs/`. Chunked at 500 chars with 50-char overlap, embedded with `all-MiniLM-L6-v2`. KB context is prepended to each user turn (not baked into the system prompt) so it stays fresh. Prewarmed on startup. Chunk IDs are SHA-256 hashes for idempotent re-indexing.
- `knowledge_docs/slic_info.txt` — SLIC-specific claim-intake knowledge (motor policy basics, what to ask the caller, common accident scenarios). This is the only source document in the RAG.
- `test_voice_elevenlabs.py` — Local smoke test. Typed input → LLM with tools → ElevenLabs TTS playback. Useful for testing tool wiring and personality tweaks without needing Twilio.

## Environment Variables

Copy `.env.example` to `.env` and fill in keys. Required for a working call:

- `LLM_PROVIDER` — `"claude"` (default) / `"openai"` / `"gemini"`
- `ANTHROPIC_API_KEY`, `CLAUDE_MODEL` — default model `claude-sonnet-4-20250514`
- `OPENAI_API_KEY`, `OPENAI_MODEL` — only if switching provider
- `GEMINI_API_KEY`, `GEMINI_MODEL` — only if switching provider
- `ELEVENLABS_API_KEY` — used by the `test_voice_elevenlabs.py` local smoke test only (the TTS on a real call goes through Twilio ConversationRelay, which authenticates to ElevenLabs on its own)
- `ELEVENLABS_VOICE_ID` — used by the local smoke test only
- `ELEVENLABS_CR_VOICE` — the voice served to Twilio ConversationRelay, formatted as `<voice_id>-<model>` (e.g. `bm3QvaZ3fUSCRBC3UV1f-flash_v2_5`). Swap voices by changing this env var and recreating the container (use `docker compose up -d --force-recreate slic-agent`; `docker compose restart` does NOT re-read `.env`)
- `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_PHONE_NUMBER` — telephony. The `TWILIO_PHONE_NUMBER` MUST be a number owned by that SID (check via `incoming_phone_numbers.list()`); otherwise SMS returns Twilio error 21660 *"Mismatch between the 'From' number and the account"*
- `SLIC_SESSION_TTL_SECONDS` — how long an active session lingers after dispatch (**default 300 = 5 min**). Also pinned to 300 in `docker-compose.yml` so compose overrides take priority unless you edit both
- `N8N_POSTCALL_WEBHOOK` — path the post-call webhook posts to (default `/webhook/slic-transcript`). The n8n workflow at that path must be **toggled Active** — draft mode returns a 404 for the production URL
- `N8N_BASE_URL` — n8n host; defaults to `https://automation.taskforceai.tech`
- `PORT` — FastAPI port, default 8000

Removed from the Kavya config: Azure Speech (Sinhala TTS), Google Cloud STT / service-account JSON (Media Streams only), eZee PMS credentials, hotel-specific n8n booking endpoints.

## Important Rules

- **Identity is a two-factor check: reg number + name.** The LLM must (1) ask for the reg number, (2) read it back digit-by-digit, (3) call `verify_vehicle_policy`, (4) announce the make/model returned, (5) ask for the caller's full name and silently fuzzy-match it against `customer_name`. Only then does accident intake begin.
- **Name matching is LENIENT by design.** Telephony STT is noisy (e.g. "Chrys Fernando" → "Chris Cano"). The prompt tells the LLM to match on ANY of: first-name phonetic match, last-name phonetic match, or full-name similarity — and to LEAN TOWARD MATCH when in doubt. One polite "could you repeat?" retry before declaring mismatch. Don't tighten this without a better STT substitute.
- **Caller phone is injected into tools, never asked.** The server reads `From` from the Twilio POST and passes it as `caller_phone` on every `execute_tool(...)` call. Tool schemas intentionally have no phone parameter. The system prompt forbids the LLM from asking for a phone or policy number.
- **Read-back before verify.** The system prompt mandates a read-back confirmation step before `verify_vehicle_policy` is called. Primary defence against STT mis-hearing the reg number (e.g. B vs V, S vs F).
- **Claim reference is NEVER read aloud.** After `dispatch_nearest_assessor`, Nimali tells the caller only the assessor's name and ETA. The reference (`SLIC-CLM-YYYYMMDD-XXXXXX`) is delivered exclusively by SMS via `send_confirmation_sms`. The system prompt and the `dispatch_nearest_assessor` tool description both enforce this.
- **Only location is asked during intake.** Injuries and vehicle condition used to be collected here but were dropped — the field assessor captures both on-site. `collect_accident_details` requires `reg_no` + `location`; the other fields accept empty strings.
- **Required tool order.** `verify_vehicle_policy` → (name identity check in-prompt) → `collect_accident_details(location)` → `dispatch_nearest_assessor` → `send_confirmation_sms`. If verification fails, or names don't match, or the caller asks for a human, the LLM calls `request_live_agent_handoff` and stops.
- **Live-agent handoff is verbal only — the call does NOT hang up.** `request_live_agent_handoff` returns `{handoff: true, reason, message}`. Nimali speaks the message ("I'm transferring your call… please stay on the line"), the server logs the event via `_handoff_just_executed()`, but NO ConversationRelay `end` is sent. The caller can hang up when ready, or keep talking (the tool description tells the LLM to stay quiet). A `handoff_already_fired` flag prevents re-logging on subsequent turns. To make the "call-back" promise real, either wire Twilio `<Enqueue>`/`<Dial>` TwiML via ConversationRelay `statusCallback`, or fire an outbound callback from n8n when the post-call webhook lands with `dispatch_status=handed_off_to_live_agent`.
- **English only.** All Sinhala/Tamil code paths, IVR logic, Media Streams session code, Azure TTS, and Google Cloud STT were removed during the Kavya → SLIC rewrite. Do not reintroduce them unless the demo scope changes.
- **Emotional adaptation is prompted, not coded.** The system prompt tells Nimali to slow down for panicked callers, escalate urgency on injuries ("call 1-9-0 for emergency services"), acknowledge anger directly, and match pace to calm callers. There's no sentiment classifier — it's all in-prompt guidance to Claude.
- **Registration-number normalisation is shared.** `mock_db._normalize_reg_no` canonicalises whitespace/hyphens to a single `-` between the letter block and the digit block. `active_session.get_by_reg_no` and `claim_api` both rely on this. Keep them in sync if you touch one.
- **Assessor dispatch is mocked.** The `ASSESSORS` pool in `claim_api.py` has 6 entries; `_pick_nearest_assessor` does a case-insensitive substring match on `base_city` against the caller-supplied location, falling back to the lowest-ETA assessor. Claim reference is generated locally (`SLIC-CLM-YYYYMMDD-XXXXXX`). Replace `claim_api.dispatch_nearest_assessor` with the real SLIC backend before any production use.
- **SMS sends to the live caller, not the DB phone.** `send_confirmation_sms` uses `caller_phone` (Twilio's `From` field), so callers dialing from an alt number still get the confirmation. Twilio errors like 21660 (*"Mismatch between the 'From' number and the account"*) mean `TWILIO_PHONE_NUMBER` doesn't belong to the `TWILIO_ACCOUNT_SID`; list owned numbers via `incoming_phone_numbers.list()` to pick a valid sender. Sri Lankan destinations require the Geographic Permissions toggle for LK to be enabled in the Twilio Messaging console.
- **Post-call failures never affect the call.** Everything in `post_call.py` is caught and logged. The WebSocket handler fires it as a background task and moves on. Common failure: n8n workflow toggled OFF → 404 *"POST slic-transcript is not registered"*. Fix in n8n, not in code.
- **docker compose restart ≠ env reload.** `docker compose restart` only restarts the process with the container's already-baked environment; it does NOT re-read `.env`. Use `docker compose up -d --force-recreate slic-agent` after any `.env` change.
- **Mocks to replace before production:** `mock_db.py` (policy/vehicle lookup), `claim_api.dispatch_nearest_assessor` (real assessor routing against SLIC's dispatch system), the locally-generated claim reference (should come from SLIC's claims system), and `active_session.py` on-disk JSON (should be Redis or a proper KV store).

## Running Locally

```bash
pip install -r requirements.txt

# Smoke test the LLM + tools + ElevenLabs playback (no Twilio needed)
python test_voice_elevenlabs.py

# Run the full server
python server.py
```

Twilio smoke test: point your Twilio number's Voice webhook at `https://<tunnel>/voice/incoming` (POST), dial in from one of the numbers in `mock_customers.json`, and Nimali should greet you by name.

## Docker / Deploy

```bash
docker compose build
docker compose up -d
docker compose logs -f slic-agent

./deploy.sh setup    # first-time VPS provisioning
./deploy.sh deploy   # push code updates
./deploy.sh logs     # tail remote logs
./deploy.sh status   # health check
```

Targets a DigitalOcean VPS behind nginx (SSL termination, WebSocket upgrade, rate-limited `/voice/incoming`). Container runs `uvicorn server:app` on `127.0.0.1:8000`; nginx is the only thing bound to 443.

## Server Constants

- `MAX_TOKENS = 300` — forces voice-appropriate concise replies.
- `MAX_HISTORY_MESSAGES = 20` — trimmed window, format-aware so orphaned tool results / tool calls are dropped together.
- `MAX_TOOL_ROUNDS = 5` — how many tool-use rounds per user turn before the loop bails.

## Adding a Mock Customer

1. Open `mock_customers.json`.
2. Add a new top-level key with the E.164 phone (e.g. `"+94771234567"`).
3. Fill in `name`, `policy_no`, and a `vehicles` list (`make`, `model`, `reg_no`, `policy_item`).
4. Restart `server.py` — `mock_db._load()` only runs at import time. The reg-no index is rebuilt automatically from the vehicles list.
5. Dial in, give one of that customer's reg numbers, and verify Nimali verifies the policy and proceeds to the accident intake.

## Notes for Future Edits

- The entrypoint is still `server.py` (uvicorn loads `server:app`). Don't rename it.
- `tools.py`, `claim_api.py`, `mock_db.py`, `active_session.py`, `post_call.py`, `mock_customers.json` have already been rewritten for SLIC — treat them as source of truth. Everything else (Dockerfile, compose, nginx, deploy script, env template, this file) was rebranded from the Kavya source.
- `knowledge_base.py` was not SLIC-specific; it just indexes whatever is in `knowledge_docs/`. Adding new SLIC docs to that folder picks them up automatically on next startup.

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
