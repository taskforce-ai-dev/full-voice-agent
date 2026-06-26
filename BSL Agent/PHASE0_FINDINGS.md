# Phase 0 — Consolidated Findings

Sources: 5 successful subagents + 3 orchestrator-handled (graphify + targeted reads). All conclusions cite source file:line.

---

## CRITICAL — Model upgrade required

The plan originally pinned `claude-sonnet-4-20250514`. Per Anthropic deprecations doc (verified April 2026): that model **retires June 15, 2026**. Use **`claude-sonnet-4-6`** instead. All other API shapes (streaming, tool_use blocks, tool_result content blocks) are unchanged. Update env + plan accordingly.

Source: docs.anthropic.com/en/about-claude/model-deprecations (fetched by Phase 0 subagent).

---

## Allowed APIs (verified, copy-from-SLIC verbatim)

### Anthropic streaming + tools
```python
from anthropic import AsyncAnthropic, NOT_GIVEN
client = AsyncAnthropic(api_key=...)

async with client.messages.stream(
    model=MODEL,                     # "claude-sonnet-4-6"
    max_tokens=MAX_TOKENS,            # SLIC uses 300 — voice-sized replies
    system=system_prompt,
    messages=conversation_history,
    tools=tools if tools else NOT_GIVEN,
) as stream:
    async for event in stream:
        # event.type ∈ {content_block_start, content_block_delta, content_block_stop, ...}
        # event.delta.type ∈ {text_delta, input_json_delta}
```

### Tool block shapes
- Assistant tool call: `{"type": "tool_use", "id": "toolu_...", "name": "...", "input": {...}}`
- Tool result (sent back as user message content list): `{"type": "tool_result", "tool_use_id": "toolu_...", "content": "<JSON string>"}`

### Tool definition (Anthropic native — base format)
```python
{
  "name": "verify_customer_identity",
  "description": "...",
  "input_schema": {"type": "object", "properties": {...}, "required": [...]}
}
```

### Provider format wrappers
- OpenAI: `{"type": "function", "function": {"name": ..., "description": ..., "parameters": <input_schema>}}`
- Gemini: `{"function_declarations": [{"name": ..., "description": ..., "parameters": <input_schema>}]}`

### `execute_tool()` dispatcher signature (SLIC tools.py:235)
```python
async def execute_tool(tool_name: str, tool_input: dict, *, caller_phone: str = "") -> str:
    # returns JSON-encoded string of the tool result
```
**Critical:** `caller_phone` is keyword-only. **NOT in any tool schema.** Server injects it on every call.

---

## Reference snippets — exact file:line locations to copy from

| What | Source file | Lines | Notes |
|---|---|---|---|
| Module imports + globals | `SLIC Agent/server.py` | 23–114 | Per-call dicts `_call_phone`, `_call_session`, env-var reads, `TOOL_FILLERS` |
| `lifespan()` | `SLIC Agent/server.py` | 421–470 | Prewarms KB + Anthropic client at startup |
| `/voice/incoming` | `SLIC Agent/server.py` | 493–535 | Reads `CallSid` + `From`, builds TwiML |
| `_build_conversation_relay_twiml()` | `SLIC Agent/server.py` | 554–571 | TwiML XML generation; reads `EN_CONFIG` dict at lines 120–128 |
| `_build_greeting()` | `SLIC Agent/server.py` | 538–551 | Returns generic or session-personalized greeting |
| `_build_system_prompt()` | `SLIC Agent/server.py` | 142–295 | **Full body in my context.** See "System prompt skeleton" below. |
| `/ws/conversation` WebSocket | `SLIC Agent/server.py` | 1151–1386 | Setup + prompt + interrupt event loop, post-call task fire-off |
| `_run_llm_streaming_claude()` | `SLIC Agent/server.py` | 1005–1144 | Streaming + tool-use loop; `MAX_TOOL_ROUNDS = 5` |
| `_handoff_just_executed()` | `SLIC Agent/server.py` | 612–670 | Walks history; returns bool. Does NOT emit ConversationRelay end. |
| Client singletons | `SLIC Agent/server.py` | 306–338 | Lazy-init Anthropic/OpenAI/Gemini |
| `_trim_history()` + `_is_tool_result_msg()` | `SLIC Agent/server.py` | 673–696 | Format-aware trim; drops orphaned tool exchanges |
| `/health` | `SLIC Agent/server.py` | 477–486 | Returns status + config snapshot |
| `request_live_agent_handoff` impl | `SLIC Agent/claim_api.py` | 331–353 | Returns `{handoff: true, reason, message}` |
| Tool builders (3 formats) | `SLIC Agent/tools.py` | 194–232 | `get_tools()`, `get_tools_openai()`, `get_tools_gemini()` |
| Tool dispatcher | `SLIC Agent/tools.py` | 235–286 | `execute_tool()` routing |
| `mock_db.py` normalize + load | `SLIC Agent/mock_db.py` | 19–101 | `_normalize_phone`, JSON load at import, public lookups |
| `knowledge_base.py` | `SLIC Agent/knowledge_base.py` | 300–498 | Domain-agnostic; copy verbatim, point at `knowledge_docs/` |
| `post_call.py` extraction + n8n POST | `SLIC Agent/post_call.py` | 43–406 | Multi-provider LLM extract, error suppression, n8n webhook POST |

### Files NOT to copy verbatim (SLIC domain — rewrite for BSL)
- `claim_api.py` — replace with `bsl_api.py` containing the 8 BSL tools
- `mock_customers.json` — replace with BSL accounts JSON (Phase 2)
- `active_session.py` — replace with much simpler `account_state.py` (no cross-call continuation needed; per-WebSocket dict only)
- `knowledge_docs/slic_info.txt` — empty for BSL (data is in mock_customers.json, not RAG)
- `_build_system_prompt()` body — rewrite for BSL FSM (skeleton/structure copyable)

### Constants (copy as-is from SLIC)
- `MAX_TOKENS = 300`
- `MAX_HISTORY_MESSAGES = 20`
- `MAX_TOOL_ROUNDS = 5`

---

## System prompt skeleton (copy structure, replace BSL content)

SLIC's `_build_system_prompt()` (server.py:142-295) is structured as:

1. **Persona block** — who the agent is, voice style
2. **Today's date** — `today = date.today().isoformat()`
3. **Active session block** (conditional) — for returning callers; **BSL doesn't need this** (no cross-call continuation)
4. **Emotional adaptation rules** — DEFAULT/PANICKED/INJURED/ANGRY/CALM/HAPPY tone variants. **BSL adapts but is less emotionally charged** (banking ≠ accident hotline)
5. **Voice rules** — pace, contractions, no markdown, digit-by-digit reading, abbreviation expansion
6. **REQUIRED FLOW** — numbered steps with strict ordering. **BSL replaces SLIC's vehicle-first 8 steps with the 5-step BSL script**
7. **HANDOFF TRIGGERS** — explicit list of when to call `request_live_agent_handoff`
8. **Final constraint** — "Keep call under N minutes"

For BSL, sections 1, 2, 5, 7 are copy-and-tweak. Sections 3, 4 are simplified. Section 6 is fully replaced with BSL FSM. Section 8 — keep but adjust target.

---

## Verbatim BSL script wording (already extracted)

See `_phase0_script_extract.md` in this directory. Confirmed sections: greeting, Step 1 (intent + account-no + type), Step 2 (4 verification questions with personal/business variants), Step 3 (failure with [X] attempts, 3-strike failure, success), Step 4 (Balance / Block Card / Account Details — personal vs business templates), Step 5 (loop trigger + final goodbye).

**Wording-flag carry-forward (from script extractor):**
- Business addenda are formatted differently than personal lines — treat as "use this phrasing variant when account is Business"
- `[X] attempt(s)` placeholder has parenthetical "(s)" — render "1 attempt" / "2 attempts" not literal "attempt(s)"
- `[Name / Company Name]` in goodbye — render `name` for personal, `company_name` for business

---

## KB data (handled by orchestrator in Phase 2)

The KB PDF subagent failed due to sandbox PDF restrictions. Orchestrator already read the full PDF during planning — all 6 accounts (5 customers; Nimal has 2) are in context. JSON construction will happen in Phase 2 via `Write` directly. Account inventory:

| Account No | Holder | Company | Type | Branch | Closing Balance (LKR) |
|---|---|---|---|---|---|
| 1042-8837-9201 | Nimal Perera | — | Current | Nugegoda | 354,217 |
| 1098-5541-3367 | Nimal Perera | Perera Tech Solutions (Pvt) Ltd | Business Current | Nugegoda | 1,940,955 |
| 2087-4412-6654 | Dilani Wijesinghe | — | Savings | Kandy | 106,502 |
| 3054-9960-1138 | Ruwan Bandara | — | Current | Galle | 1,084,231 |
| 4901-2233-4477 | Amara Dissanayake | Horizon Trading (Pvt) Ltd | Business Current | Colombo 03 | 5,720,400 |
| 5673-8800-2295 | Sahan Mendis | ByteNest Solutions (Pvt) Ltd | Business Current | Colombo 07 | 927,607 |

Each account has identity (NIC/Branch/DOB/Mother's Maiden Name), card details, loans (4 active loans across the 6 accounts; 2 accounts have none), standing orders, full 30-day transaction ledger.

---

## Deploy diff template (apply in Phase 5)

### Dockerfile — no changes (port 8000 internal)

### docker-compose.yml changes
| Line | Find | Replace |
|---|---|---|
| `slic-agent:` (service) | → `bsl-agent:` |
| `container_name: slic-voice-agent` | → `container_name: bsl-agent` |
| `- "127.0.0.1:8001:8000"` | → `- "127.0.0.1:8002:8000"` |
| `SLIC_SESSION_TTL_SECONDS=300` | (drop entirely — BSL has no cross-call sessions) |

### nginx.conf changes
SLIC's nginx.conf is a standalone server block (Sofia and Kavya each have their own). Pattern: `/etc/nginx/sites-available/<name>` symlinked to `sites-enabled/`. For BSL, copy SLIC's nginx.conf and change:
- Header comment paths: `slic-agent` → `bsl`
- Rate-limit zone: `twilio_webhook` → `bsl_webhook` (must be unique across all server blocks)
- `server_name YOUR_DOMAIN;` (×2, HTTP + HTTPS) → `server_name bsl.taskforceai.tech;`
- SSL cert paths: `YOUR_DOMAIN` → `bsl.taskforceai.tech`
- Log files: `slic_access.log` / `slic_error.log` → `bsl_access.log` / `bsl_error.log`
- Upstream proxy_pass (×3): `127.0.0.1:8000` → `127.0.0.1:8002`
- `limit_req zone=twilio_webhook` → `limit_req zone=bsl_webhook`

### deploy.sh changes
| Line | Find | Replace |
|---|---|---|
| `DOMAIN="abans.taskforceai.tech"` (this is wrong in SLIC — copy-paste leftover) | → `DOMAIN="bsl.taskforceai.tech"` |
| `REMOTE_DIR="/opt/slic-agent"` | → `REMOTE_DIR="/opt/bsl-agent"` |
| `info "Setting up $SERVER_IP for SLIC Agent..."` | → `info "Setting up $SERVER_IP for BSL Agent..."` |
| `/etc/nginx/sites-available/slic-agent` | → `/etc/nginx/sites-available/bsl` |
| `docker compose logs -f slic-agent` | → `docker compose logs -f bsl-agent` |
| Keep `SERVER_IP="67.207.90.109"` and `EMAIL="info@taskforceai.tech"` |

### requirements files — no changes (universal stack)

---

## Key SLIC behaviours to preserve verbatim in BSL

1. **`caller_phone` injected by server, never in tool schemas** (security/UX)
2. **Verbal-only handoff** — `_handoff_just_executed()` logs but doesn't emit `end` (caller controls hangup)
3. **Read-back digit-by-digit** before any lookup tool fires (defends against STT noise) — applies to BSL account number AND verification fields
4. **Lenient fuzzy matching** for verification — STT noise tolerance. BSL's mother's maiden name field needs same treatment.
5. **Per-call state via module-level dict keyed by Call SID** — `_call_phone[call_sid]` pattern
6. **`MAX_TOOL_ROUNDS = 5`** — bail out of tool-use loop if exceeded
7. **`MAX_HISTORY_MESSAGES = 20`** + format-aware trim
8. **Post-call extraction in `finally` block as `asyncio.create_task()`** — failures never affect the call
9. **`docker compose up -d --force-recreate`** after `.env` change (NOT `restart`)
10. **`ELEVENLABS_CR_VOICE` format `<voice_id>-<model>`** — same default for BSL initially

---

## Anti-patterns to grep for (Phase 6 verification)

- `vehicle\|reg_no\|policy_no\|assessor\|claim_reference` — SLIC leftovers
- `IVR\|DTMF\|language_select\|/ws/media-stream` — Kavya/Sofia leftovers
- `caller_phone` inside any tool's `input_schema.properties` — leaking the injected param
- `mock_customers.json.*write` / `json\.dump.*mock_customers` — block_card mutating the file (must be per-session)

---

## Phase 0 verification

- [x] All 8 subagent reports back (5 succeeded; 3 handled by orchestrator)
- [x] Anthropic SDK shape matches SLIC usage — only model ID needs upgrade
- [x] All 6 accounts inventoried with closing balances
- [x] Verbatim script wording captured to `_phase0_script_extract.md`
- [x] Deploy diff template ready
- [x] System prompt skeleton mapped
- [x] Tool dispatcher pattern locked
