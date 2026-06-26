# BSL Voice Agent — Build Plan

**Client:** Bank of Sri Lanka
**Reference architecture:** `SLIC Agent/` (Nimali) — English-only ConversationRelay flow with identity verification, mock JSON DB, tool dispatch, post-call webhook, verbal-only handoff
**Persona:** Generic "virtual assistant" (no name)
**Language:** English only
**Working directory:** `BSL Agent/`
**Source PDFs (in this directory):**
- `BankOfSriLanka_VoiceAgent_Script.pdf` — call flow, greetings, verification questions
- `BSL_VoiceAgent_KnowledgeBase.pdf` — 5 customer records with full account profiles

---

## Deployment Coordinates (locked in)

| Item | Value |
|---|---|
| Subdomain | `bsl.taskforceai.tech` |
| **Twilio webhook URL** | **`https://bsl.taskforceai.tech/voice/incoming`** (HTTP POST) |
| Twilio Account SID | _(see secrets manager)_ |
| Twilio Auth Token | _(see secrets manager — rotate; never commit)_ |
| Twilio phone number | _(see secrets manager)_ |
| Container internal port | `8000` (FastAPI default) |
| Host port (DigitalOcean VPS) | `127.0.0.1:8002` (Kavya=8000, Sofia/SLIC=8001 — 8002 is free) |
| VPS IP | `67.207.90.109` (same box as Kavya/Sofia/SLIC) |
| **DNS record needed (user action)** | **A-record `bsl` → `67.207.90.109`** in the `taskforceai.tech` zone |
| Post-call webhook path | `/webhook/bsl-transcript` (n8n) |

---

## Phase 0 — Documentation Discovery (parallel subagents, fact-gathering only)

**Goal:** Before any code is written, deploy parallel subagents to extract concrete API signatures, snippet locations, and architecture patterns from the existing graphify graph + reference agent. The orchestrator consolidates findings into an "Allowed APIs" catalogue.

**Critical rule per CLAUDE.md:** Use `graphify-out/` (graph.json, GRAPH_REPORT.md, wiki, `graphify query/path/explain` commands) instead of grep'ing Kavya/SLIC source files. Subagents MUST cite sources (file paths, graph node names, line numbers) for every finding.

### Subagents to deploy in parallel (8 total — exceeds the user's "5+" requirement)

| # | Subagent | Mission | Required outputs |
|---|---|---|---|
| 1 | **graphify-architecture** | Query the graph for SLIC's call-handling architecture: how `/voice/incoming` → ConversationRelay TwiML → `/ws/conversation` flow works. Use `graphify query "how does ws_conversation work in SLIC"` and `graphify path "MediaStreamSession" "execute_tool"`. | List of SLIC modules + responsibilities (server, tools, claim_api, active_session, post_call, knowledge_base). Cite graph nodes/edges. |
| 2 | **graphify-tools-pattern** | Query the graph for tool definition + dispatch pattern: how SLIC declares tools in Anthropic/OpenAI/Gemini formats and routes them via `execute_tool()`. Use `graphify explain "execute_tool"`. | Exact tool-schema shape per provider, dispatch table location, how `caller_phone` is injected without LLM seeing it. |
| 3 | **graphify-prompt-pattern** | Query the graph for `_build_system_prompt()` and `_build_greeting()` in SLIC. Identify how strict step ordering is enforced, how emotional adaptation is prompted, how identity verification is gated. | Skeleton of the system-prompt structure, prompt sections, anti-patterns the prompt forbids. |
| 4 | **graphify-postcall-handoff** | Query the graph for SLIC's `request_live_agent_handoff` + `_handoff_just_executed()` + `process_post_call_data()` flow. Confirm the handoff is verbal-only (no ConversationRelay `end` emitted). | Exact handoff mechanism, post-call extraction schema, n8n webhook contract. |
| 5 | **kb-pdf-extraction** | Read `BSL Agent/BSL_VoiceAgent_KnowledgeBase.pdf` page-by-page (no graphify — this is a fresh PDF). Extract all 5 customer records (6 accounts including Nimal Perera's two) into a structured intermediate format suitable for `mock_customers.json`. Note the multi-account flag for Nimal. | A normalized JSON-shaped dump of every field from every account: identity, summary, card, loans, standing orders, full transaction ledger. |
| 6 | **script-flow-extraction** | Read `BSL Agent/BankOfSriLanka_VoiceAgent_Script.pdf`. Extract: greeting wording, the 3 service paths, the 4 verification questions (with business-account variants), failure messages (with attempt-count interpolation), success message, Step-4 response templates (personal vs business), wrap-up wording. | Verbatim script lines mapped to FSM states. Note: account number is collected at Step 1, NOT verified — verification is the 4 spoken answers in Step 2. |
| 7 | **deploy-pattern** | Query graphify + read SLIC's `Dockerfile`, `docker-compose.yml`, `nginx.conf`, `deploy.sh` (graphify won't have nginx config in detail — read those 4 files directly). Map exactly what changes per-agent (port, container name, server-name, upstream block). | A "diff template" of the 4 deploy files — what to copy verbatim and what 4–6 strings need substituting for BSL. |
| 8 | **anthropic-tool-use-docs** | Use Context7 (`mcp__claude_ai_Context7`) to fetch current Anthropic SDK docs for `client.messages.create()` with `tools=` parameter — confirm tool_choice semantics, tool_result message shape, streaming behaviour. Also confirm `claude-sonnet-4-20250514` is the default model SLIC pins. | Exact import paths, signature, the `{type: "tool_use"}` / `{type: "tool_result"}` block shape. Flag any deprecations vs SLIC's usage. |

**Reporting contract (mandatory, applies to every subagent):**
1. Sources consulted (files + graph queries + URLs)
2. Concrete findings (exact symbols, signatures, file:line citations)
3. Copy-ready snippet locations (which file, which lines to copy)
4. Confidence + known gaps

If any subagent reports a conclusion without a source, the orchestrator rejects it and re-deploys.

**Phase 0 verification (orchestrator):**
- [ ] All 8 subagent reports cite at least one source per finding
- [ ] Anthropic SDK call shape matches what SLIC uses (no version skew)
- [ ] All 6 accounts (5 customers; Nimal has 2) extracted with every field populated
- [ ] Verbatim script wording captured — no paraphrasing

**Phase 0 output (orchestrator authors):**
A consolidated `PHASE0_FINDINGS.md` in `BSL Agent/` with: Allowed APIs list, file-to-copy table, KB intermediate JSON, script FSM, deploy diff template.

---

## Phase 1 — Project Scaffold (copy-from-SLIC, then trim)

**Goal:** Create the directory skeleton by copying SLIC files verbatim, then immediately delete domain-specific code that won't survive the rewrite.

### Tasks
1. Copy from `SLIC Agent/` into `BSL Agent/`:
   - `Dockerfile` (copy as-is, will be edited in Phase 5)
   - `docker-compose.yml` (copy, edit port 8001→8002 and `slic-agent`→`bsl-agent` and container name)
   - `nginx.conf` (copy as template — full edit in Phase 5)
   - `deploy.sh` (copy, replace string `slic` with `bsl` and any DigitalOcean paths)
   - `requirements-prod.txt` and `requirements.txt` (copy verbatim — same deps)
   - `full-voice-agent-a8a245fb37cb.json` (copy — same GCP creds, even though we won't use Google STT)
   - `.env.example` from SLIC's (copy, then add BSL-specific vars in Phase 5)
   - `test_voice_elevenlabs.py` (copy, will be retargeted in Phase 4 to BSL's tools)
2. Create empty placeholders to be filled in later phases:
   - `server.py` (stub — `from fastapi import FastAPI; app = FastAPI()`)
   - `tools.py` (empty)
   - `mock_db.py` (empty)
   - `mock_customers.json` (empty `{}`)
   - `account_state.py` (new — replaces `active_session.py`; tracks card-block state per session)
   - `post_call.py` (empty)
   - `knowledge_base.py` (copy from SLIC verbatim — generic RAG over `knowledge_docs/`)
   - `knowledge_docs/bsl_general_info.txt` (placeholder for general bank policies if needed; not required for tools to work)
3. Add `CLAUDE.md` to `BSL Agent/` documenting project intent (use SLIC's CLAUDE.md as the structural template — same headings, BSL-specific content)

### Anti-patterns (do NOT do)
- Do NOT bring over `claim_api.py`, `mock_customers.json`, `active_session.py` content from SLIC (those are SLIC-domain). Only the file *names/structures* are templates.
- Do NOT bring over `_pick_nearest_assessor` / `dispatch_*` / `verify_vehicle_policy` symbols.
- Do NOT bring over the IVR/Media-Streams code (SLIC already stripped it; double-check `server.py` has only `/voice/incoming` + `/ws/conversation` + `/health`).

### Verification checklist
- [ ] `BSL Agent/` exists with all files listed above
- [ ] `python -c "import server"` succeeds (stub imports cleanly)
- [ ] `docker compose config` parses without errors after the port/name edits
- [ ] No SLIC-specific symbol survives (`grep -r "vehicle\|policy\|reg_no\|assessor\|claim" BSL\ Agent/` returns nothing in `.py` files)

---

## Phase 2 — Knowledge Base: PDF → `mock_customers.json`

**Goal:** Materialize the 5 customer records (6 accounts) from the KB PDF into a structured JSON file that tools can do exact-match lookups against.

### Source
`BSL Agent/BSL_VoiceAgent_KnowledgeBase.pdf` (already read by Phase 0 subagent #5; use that intermediate JSON as the canonical extract).

### `mock_customers.json` schema (lock this — tools depend on it)

```json
{
  "accounts": {
    "1042-8837-9201": {
      "account_no": "1042-8837-9201",
      "account_no_last4": "9201",
      "account_holder": "Nimal Perera",
      "company_name": null,
      "account_type": "Current Account",
      "is_business": false,
      "branch": "Nugegoda",
      "opened_date": "14 March 2019",
      "verification": {
        "nic": "901234567V",
        "branch": "Nugegoda",
        "dob": "12 April 1990",
        "mothers_maiden_name": "Jayasinghe"
      },
      "summary": {
        "opening_balance": 285000.00,
        "closing_balance": 354217.00,
        "statement_period": "01 April 2026 — 30 April 2026",
        "internet_banking": true,
        "mobile_banking": true,
        "registered_mobile": "0771234567",
        "registered_email": "nimal.perera@gmail.com"
      },
      "card": {
        "number_masked": "**** **** **** 4412",
        "card_type": "Visa Debit",
        "expiry": "08/2027",
        "status": "Active",
        "daily_limit": 100000
      },
      "loans": [
        {
          "product_type": "Personal Loan",
          "reference": "PL-2023-00441",
          "original_amount": 500000,
          "outstanding": 187500,
          "monthly_payment": 15625,
          "next_due_date": "05 May 2026",
          "status": "Active"
        }
      ],
      "standing_orders": [
        {"payee": "Dialog Axiata", "amount": 1499, "execution_date": "1st of each month"},
        {"payee": "Sanasa Life Insurance", "amount": 3200, "execution_date": "5th of each month"}
      ],
      "transactions": [
        {"date": "01 Apr", "description": "Opening Balance", "dr_cr": null, "amount": 285000.00, "balance": 285000.00},
        {"date": "07 Apr", "description": "POS — Keells Super Nugegoda", "dr_cr": "DR", "amount": 3450.00, "balance": 281550.00}
      ]
    }
  },
  "_multi_account_holders": {
    "Nimal Perera": ["1042-8837-9201", "1098-5541-3367"]
  }
}
```

Required keys (account_no normalized as `XXXX-XXXX-XXXX` with hyphens):
- `1042-8837-9201` Nimal Perera (Personal Current)
- `1098-5541-3367` Nimal Perera / Perera Tech Solutions (Pvt) Ltd (Business)
- `2087-4412-6654` Dilani Wijesinghe (Savings)
- `3054-9960-1138` Ruwan Bandara (Current)
- `4901-2233-4477` Amara Dissanayake / Horizon Trading (Pvt) Ltd (Business)
- `5673-8800-2295` Sahan Mendis / ByteNest Solutions (Pvt) Ltd (Business)

### `mock_db.py` shape (port from SLIC, but rewritten — do not copy SLIC's vehicle logic)

Functions to expose (signatures locked):
- `lookup_account(account_no: str) -> Optional[dict]` — accepts full `XXXX-XXXX-XXXX` or last-4 `9201`; normalizes whitespace/hyphens
- `verify_identity(account_no: str, nic: str, branch: str, dob: str, mothers_maiden_name: str) -> dict` — returns `{verified: bool, mismatched_fields: list[str]}`. Uses **lenient** matching (case-insensitive, whitespace-collapsed, NIC tolerates trailing `V`/`v`, DOB tolerates "12 April 1990" / "12-04-1990" / "12/04/1990" via a date parser. Mother's maiden name uses fuzzy match — same lenient principle as SLIC's name match. **Branch** uses substring match (e.g. "Nugegoda" matches "Nugegoda Branch"). The **whole** PDF is the ground truth — copy values verbatim.)
- `_normalize_account_no(raw: str) -> str` — canonicalises `"1042 8837 9201"` / `"1042-8837-9201"` / `"9201"` / `"ending in 9201"` to the canonical hyphenated form (or returns the last-4 lookup key if input is only 4 digits).

### Anti-patterns
- Do NOT use ChromaDB / RAG for any of the verification or balance/transaction data. RAG over numeric fields is unreliable. KB content goes through `mock_db.py` only.
- Do NOT include the multi-account "Agent must clarify which account" warning as a tool response — the script's Step 1 ("Could you please state the account number?") naturally resolves this; the customer states the account, lookup is exact.

### Verification checklist
- [ ] All 6 accounts present with every field from the PDF
- [ ] `lookup_account("9201")` returns Nimal's personal account
- [ ] `lookup_account("1042-8837-9201")` returns the same record
- [ ] `verify_identity("1042-8837-9201", "901234567v", "nugegoda", "12 april 1990", "jayasinghe")` returns `{verified: true, mismatched_fields: []}` (case + whitespace tolerance)
- [ ] `verify_identity("1042-8837-9201", "wrong", "Nugegoda", "12 April 1990", "Jayasinghe")` returns `{verified: false, mismatched_fields: ["nic"]}`
- [ ] Multi-account check: `_multi_account_holders["Nimal Perera"]` lists both account numbers

---

## Phase 3 — Tools Implementation (`tools.py`)

**Goal:** Define 8 tools in Anthropic/OpenAI/Gemini formats and a single `execute_tool()` dispatcher. Copy the structural pattern from SLIC's `tools.py` verbatim — only the tool definitions differ.

### Tools to define (locked in by user during planning)

| # | Tool name | Inputs | Returns | Notes |
|---|---|---|---|---|
| 1 | `verify_customer_identity` | `account_no`, `nic`, `branch`, `dob`, `mothers_maiden_name` | `{verified, mismatched_fields, attempts_remaining}` | Server tracks attempt counter per WebSocket session; tool returns count from server-side dict. **Strict ordering rule:** all 4 fields collected before this tool fires (system prompt enforces). |
| 2 | `get_account_balance` | `account_no` | `{balance, last4}` | Reads `summary.closing_balance`. |
| 3 | `block_debit_card` | `account_no` | `{blocked, last4, status_was_already_blocked}` | Mutates `account_state.py` per-session card-block state (not the JSON file — session-scoped). |
| 4 | `get_account_details` | `account_no` | `{account_type, branch, opened_date, last4, is_business, company_name}` | Different response template per `is_business` (script Step 4 — Account Details). |
| 5 | `get_recent_transactions` | `account_no`, `n` (default 5) | `{transactions: [...]}` | Last N entries from `transactions[]` (excluding the synthetic Closing Balance row). |
| 6 | `get_loans` | `account_no` | `{loans: [...]}` or `{loans: [], message: "No active loans"}` |
| 7 | `get_standing_orders` | `account_no` | `{standing_orders: [...]}` |
| 8 | `request_live_agent_handoff` | `reason` (enum: `verification_failed`, `caller_request`) | `{handoff: true, message}` | Returns the verbatim transfer message ("I'm afraid I'm unable to verify your identity at this stage. For your security, I'll transfer you to one of our team members who can assist you further. Please hold."). Verbal-only — server logs to post-call webhook, does NOT emit ConversationRelay `end`. Same as SLIC. |

### Reference snippets to copy (from SLIC, identified in Phase 0)
- Tool-list builder for each provider (`get_tools()`, `get_tools_openai()`, `get_tools_gemini()`)
- `execute_tool(tool_name, tool_input, caller_phone, ...)` dispatcher signature — keep `caller_phone` parameter for symmetry even though BSL doesn't use it for SMS (server still passes it for audit logs)
- Anthropic native tool format: `{name, description, input_schema}` shape

### Anti-patterns
- Do NOT add a tool parameter for `caller_phone` in any of the 8 schemas — the LLM never sees or asks for it (same SLIC rule).
- Do NOT add an `account_number` collection tool — the script collects it at Step 1 conversationally; the LLM passes it as the `account_no` input to other tools.
- Do NOT have `block_debit_card` write to `mock_customers.json` (filesystem mutation across calls is a bug magnet — keep block state per-session in `account_state.py`).
- Do NOT make tools chain implicitly. The system prompt drives tool order. Each tool returns a clean dict the LLM speaks back.

### Verification checklist
- [ ] All 8 tools have Anthropic + OpenAI + Gemini schemas
- [ ] `execute_tool("get_account_balance", {"account_no": "9201"}, caller_phone="+...")` returns `{balance: 354217.0, last4: "9201"}`
- [ ] `execute_tool("verify_customer_identity", {wrong NIC}, ...)` decrements server attempt counter and returns `attempts_remaining: 2`
- [ ] After 3 failed verifies, the prompt instructs the LLM to call `request_live_agent_handoff(reason="verification_failed")`
- [ ] No tool has a `caller_phone` field in its schema (only the dispatcher receives it from the server)

---

## Phase 4 — Server, System Prompt, ConversationRelay (`server.py`)

**Goal:** Wire the FastAPI app: `/voice/incoming` returns ConversationRelay TwiML, `/ws/conversation` handles the LLM loop with tool dispatch.

### Copy from SLIC (Phase 0 subagent #1 identified the exact lines)
- FastAPI app + lifespan + `/health`
- `/voice/incoming` — read Twilio `CallSid` + `From`, store in module dict, return TwiML with `<ConversationRelay>` pointing at `/ws/conversation`
- `/ws/conversation` — handle `setup`, `prompt`, `interrupt` events; LLM streaming loop; tool-call dispatch loop
- `_get_anthropic_client()`, `_get_openai_client()`, `_get_gemini_client()` — shared client cache
- `_run_llm()` / `_run_llm_openai()` / `_run_llm_gemini()` — three streaming variants
- History trim helper (`_trim_history`)
- Orphaned tool-result detector (`_is_orphaned_tool_result`)
- `_handoff_just_executed()` — checks tail of history; logs but does NOT emit `end`
- Post-call task fire-off in WebSocket `finally` block

### `_build_greeting()` (BSL-specific)
Verbatim from script PDF:
> "Good day, and welcome to Bank of Sri Lanka. You're speaking with our virtual assistant. What can I assist you with today?"

No personalization (the user opted out of auto-identification by phone).

### `_build_system_prompt()` — strict FSM, verbatim wording from script

The prompt MUST enforce, in order:
1. **Step 1 — Customer Intention**: identify intent (Account Balance / Block Debit Card / Account Details / "other" → ask the recent-transactions / loans / standing-orders ad-libs). Then ask: "Could you please state the account number you'd like me to look into?" and "And just to confirm — is this a Personal or Current account, or is it a Business account?"
2. **Step 2 — Voice Verification (4 questions, ordered)**:
   - Q1 NIC, Q2 Branch, Q3 DOB, Q4 Mother's Maiden Name (verbatim wording from script). For business accounts, the prompt notes "of the primary account holder" wording.
3. **Step 3 — Identity Confirmation**: only AFTER all 4 collected, call `verify_customer_identity`. If `verified=true`, speak "Thank you for that. I've confirmed your identity successfully. Give me just a moment while I retrieve that for you." Then call the tool corresponding to Step-1 intent. If `verified=false`, speak the failure-with-attempts-remaining line and re-collect the failed fields. After 3 fails, speak the live-agent transfer line and call `request_live_agent_handoff(reason="verification_failed")`.
4. **Step 4 — Serve Request**: the response template branches on `is_business`. Verbatim per script.
5. **Step 5 — Wrap-Up / Loop**: ask "Is there anything else I can help you with today?" If yes, return to Step 1 (re-verification not required for the same verified account in the same session). If no, speak the wrap-up line: "It was a pleasure assisting you today, [Name / Company Name]. On behalf of Bank of Sri Lanka, we wish you a wonderful day. Goodbye!"

### Hard prompt rules (prevent off-script behaviour)
- "Account numbers are spoken as digits — repeat the last 4 digits back to the caller for confirmation before calling any lookup tool" (defends against STT mis-hearing).
- "Never read the full account number aloud — only the last 4 digits."
- "Never speak the NIC number, mother's maiden name, or DOB back to the caller — those are verification secrets."
- "Never read the full debit card number — only the last 4 digits ('ending in 4412')."
- "If you don't know something the customer asks (e.g. interest rates, branch addresses), say 'I don't have that information available right now — for that, our team can help you better' — do not invent."
- "All amounts are spoken in LKR. Format numbers naturally: 'three hundred fifty-four thousand, two hundred seventeen rupees'."

### Per-session state (kept in module dict keyed by Call SID — same SLIC pattern)
- `caller_phone`
- `account_no_under_discussion` (set after Step 1)
- `verification_attempts` (int, max 3)
- `verified_account` (set after success)
- `card_block_state` (session-local override of `card.status`; not persisted to JSON file — this prevents demo-2 caller from seeing demo-1 caller's mutation)

### `account_state.py` (replaces SLIC's `active_session.py`, much simpler)
Just an in-memory dict keyed by Call SID for the per-session state above. NO disk persistence (sessions die with the WebSocket; that matches the script's loop-back design). Far simpler than SLIC's `active_session.py` because BSL has no cross-call continuation requirement.

### Anti-patterns
- Do NOT bring SLIC's `verify_vehicle_policy` flow / digit-by-digit reg-number readback verbatim — BSL collects the account number conversationally at Step 1, not at verification time.
- Do NOT auto-identify by `From` phone (user explicitly chose pass-through-only).
- Do NOT emit a ConversationRelay `end` on handoff (verbal-only — same SLIC rule).
- Do NOT use Media Streams / Google STT / Azure TTS (English-only, ConversationRelay-only).
- Do NOT write the card-block to `mock_customers.json`. Per-session only.

### Verification checklist
- [ ] `python server.py` starts on port 8000 (container internal) without error
- [ ] `curl -X POST http://localhost:8000/voice/incoming -d "CallSid=test&From=+1234567890"` returns TwiML containing `<ConversationRelay url="wss://.../ws/conversation"`
- [ ] `curl http://localhost:8000/health` returns `{"status": "ok"}`
- [ ] `python test_voice_elevenlabs.py` (after retargeting) runs the verification flow end-to-end with typed input
- [ ] Manually walking the FSM with typed inputs produces the verbatim script lines (greeting, verification questions, success/failure messages, balance/block/details responses, wrap-up)

---

## Phase 5 — Post-Call Webhook + Dockerfile + nginx + Deploy

**Goal:** Wire the after-call data extraction, then make the agent reachable at `https://bsl.taskforceai.tech/voice/incoming`.

### `post_call.py` (copy from SLIC, retarget extraction schema)

LLM-extract structured call data from the full transcript. Schema for BSL:
```
{
  "caller_phone": str,
  "account_no_last4": str,
  "account_holder": str,
  "company_name": str | null,
  "intent": "balance" | "block_card" | "account_details" | "transactions" | "loans" | "standing_orders" | "multiple",
  "verified": bool,
  "verification_attempts": int,
  "actions_taken": [str],
  "outcome": "served" | "verification_failed" | "handed_off_to_live_agent" | "caller_hung_up",
  "summary": str
}
```
POST to `${N8N_BASE_URL}/webhook/bsl-transcript`. All errors caught; webhook 404 never affects the call.

### Dockerfile changes (vs SLIC)
- Container name: `bsl-agent`
- Internal port stays `8000` (FastAPI default — same as all other agents)

### `docker-compose.yml`
- `services.bsl-agent.ports`: `"127.0.0.1:8002:8000"` (8002 chosen because 8000 = Kavya, 8001 = Sofia/SLIC shared)
- `services.bsl-agent.container_name`: `bsl-agent`
- Env vars block — copy SLIC's, then add `N8N_POSTCALL_WEBHOOK=/webhook/bsl-transcript`

### `nginx.conf` — new server block
Copy the SLIC server block (port 8001 → 8002, server_name `slic.taskforceai.tech` → `bsl.taskforceai.tech`). Same SSL termination, WSS upgrade for `/ws/conversation`, rate-limit zone.

**Critical:** The deploy step is to `cat` the new server block into the VPS's `/etc/nginx/sites-available/bsl` (NOT replace nginx.conf wholesale — the existing Kavya/Sofia/SLIC blocks must stay). Then `ln -s` to `sites-enabled/`, `nginx -t`, `systemctl reload nginx`. Document this in `deploy.sh`.

### `.env` for production (the user must populate)
```
LLM_PROVIDER=claude
ANTHROPIC_API_KEY=<existing Anthropic key from Kavya/SLIC .env>
CLAUDE_MODEL=claude-sonnet-4-20250514
ELEVENLABS_API_KEY=<existing>
ELEVENLABS_CR_VOICE=<same as SLIC's default Nimali voice, e.g. bm3QvaZ3fUSCRBC3UV1f-flash_v2_5 — user can change>
TWILIO_ACCOUNT_SID=<your_twilio_account_sid>
TWILIO_AUTH_TOKEN=<your_twilio_auth_token>
TWILIO_PHONE_NUMBER=<your_twilio_phone_number>
N8N_BASE_URL=https://automation.taskforceai.tech
N8N_POSTCALL_WEBHOOK=/webhook/bsl-transcript
PORT=8000
```

### deploy.sh
- `setup` — first-time VPS provisioning: copy nginx server block, request Let's Encrypt cert for `bsl.taskforceai.tech`, build container
- `deploy` — rsync code, `docker compose up -d --force-recreate bsl-agent` (NOT `restart` — restart doesn't reload `.env`; documented in SLIC's CLAUDE.md)
- `logs` — `docker compose logs -f bsl-agent`
- `status` — `curl https://bsl.taskforceai.tech/health` + `docker ps --filter name=bsl-agent`

### User's external steps (document in CLAUDE.md and tell user)
1. **DNS:** Add A-record `bsl.taskforceai.tech` → `67.207.90.109` in the `taskforceai.tech` zone.
2. **Twilio Console:** Phone Numbers → `+19476669436` → Voice Configuration → "A call comes in" → Webhook → URL: `https://bsl.taskforceai.tech/voice/incoming`, HTTP POST. Status callback URL: `https://bsl.taskforceai.tech/voice/status` (optional).
3. **n8n:** Create webhook workflow at path `/webhook/bsl-transcript` and toggle to Active (draft mode returns 404 — same gotcha as SLIC).

### Anti-patterns
- Do NOT skip Let's Encrypt — `wss://` requires valid TLS for ConversationRelay.
- Do NOT commit `.env` (already in SLIC's `.gitignore` — verify it carries over).
- Do NOT use `docker compose restart` after `.env` edits — use `up -d --force-recreate`.

### Verification checklist
- [ ] `nginx -t` on VPS passes after the new block is added
- [ ] `https://bsl.taskforceai.tech/health` returns 200
- [ ] Twilio Debugger shows successful `/voice/incoming` POSTs (no 502s)
- [ ] Test call from a personal phone reaches the greeting line
- [ ] Walk through full Personal Current account flow with account `1042-8837-9201` (Nimal) — balance comes back as "three hundred fifty-four thousand, two hundred seventeen"
- [ ] Walk through Business account flow with account `1098-5541-3367` (Perera Tech) — balance comes back with the business-account response template
- [ ] Walk through wrong-NIC scenario 3 times → handoff line is spoken and call stays open
- [ ] After hang-up, n8n receives the `/webhook/bsl-transcript` POST with extracted data

---

## Phase 6 — Final Verification

**Goal:** Prove the agent matches both PDFs verbatim where required.

### Test matrix (every cell must pass)

| Account | Intent | Verification | Expected outcome |
|---|---|---|---|
| `9201` (Nimal personal) | Balance | All 4 correct | "The available balance on your account ending in 9201 is LKR 354,217." |
| `3367` (Perera Tech) | Balance | All 4 correct | "The current balance on your business account ending in 3367 stands at LKR 1,940,955." |
| `6654` (Dilani) | Block Card | All 4 correct | "I'm placing a block on the debit card associated with account 6654 now. That's done — your card has been successfully blocked and you're fully protected." |
| `1138` (Ruwan) | Account Details | All 4 correct | "Here are the details for account 1138: it's a Current Account, opened on 22 January 2017 at our Galle branch." |
| `4477` (Horizon) | Account Details | All 4 correct | Business template: "registered under Horizon Trading (Pvt) Ltd, opened on 11 August 2020." |
| `2295` (ByteNest) | Off-script: "what were my last 5 transactions" | All 4 correct | Reads last 5 from ledger, naturally phrased |
| `9201` | Balance | NIC wrong all 3 times | After 3rd attempt, transfer line spoken, line stays open, n8n webhook receives `outcome: handed_off_to_live_agent` |
| `9201` | Balance | NIC wrong once, correct on 2nd | "I'm sorry, I wasn't able to match that information. You have 2 attempts remaining — please try once more." then proceed |
| `9201` then loop to balance again on `3367` | Multi-account | Verify both | Single call serves both accounts; wrap-up only on caller's "no" |

### Anti-pattern grep (must return zero hits in `BSL Agent/`)
- `vehicle\|reg_no\|policy_no\|assessor\|claim_reference` (SLIC leftovers)
- `IVR\|DTMF\|language_select\|/ws/media-stream` (Kavya/Sofia leftovers)
- `caller_phone.*input_schema\|caller_phone.*parameters` (caller_phone must NOT be in tool schemas)
- `mock_customers.json.*write\|json\.dump.*mock_customers` (block_card must NOT mutate the file)

### Documentation cross-check
- [ ] Every line in the greeting/verification/success/failure/wrap-up appears verbatim in `BankOfSriLanka_VoiceAgent_Script.pdf`
- [ ] Every numeric balance the agent speaks matches the closing balance in `BSL_VoiceAgent_KnowledgeBase.pdf`
- [ ] Every account-opened-date the agent speaks matches the PDF
- [ ] Business account responses use the business template; Personal/Current use the personal template

### Run `graphify update .` from the project root
Per the project CLAUDE.md rule: after modifying code, regenerate the AST cache so the next session sees the new `BSL Agent/` module.

---

## Open items the user must own

1. **DNS:** A-record `bsl` → `67.207.90.109` in the `taskforceai.tech` zone. Without this, Twilio cannot reach the webhook.
2. **Twilio Console:** Set the Voice URL on `+19476669436` to `https://bsl.taskforceai.tech/voice/incoming` (HTTP POST).
3. **n8n:** Create + activate the workflow at `/webhook/bsl-transcript`. Until done, post-call extraction will 404 — but the call itself works fine.
4. **`.env` on VPS:** Populate with the values listed in Phase 5 (especially `ANTHROPIC_API_KEY` and `ELEVENLABS_API_KEY` — copy from existing `Kavya/.env` or `SLIC Agent/.env`).
5. **(Optional) ElevenLabs voice ID:** SLIC's default voice will be used unless the user provides a different `ELEVENLABS_CR_VOICE` value.
