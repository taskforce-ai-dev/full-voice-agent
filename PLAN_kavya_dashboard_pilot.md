# Kavya → Agent Dashboard Pilot Integration Plan

**Goal:** Forward every Kavya voice call (start + completed with transcript & summary) to the new `agent-dashboard` API and see it appear in the dashboard UI. Existing n8n post-call POST stays in place.

**Out of scope (post-pilot):** per-agent API keys, recording upload, KB sync, multi-tenant workspaces, dashboard UI changes, BSL/SLIC/Sofia/Flico onboarding.

---

## Phase 0 — Allowed APIs & Anti-Patterns (read once, reference everywhere)

### Dashboard ingest contract — `POST /api/webhooks/agent-events`

- **Auth header:** `x-aether-secret: <INBOUND_WEBHOOK_SECRET>` — validated at `agent-dashboard/src/server.ts:821-846`. If env var is unset, validation is **skipped** (open ingest). Mismatch → HTTP 401.
- **Payload schema:** `agent-dashboard/src/services/ingest.ts:7-79` — top-level keys we will use: `id`, `eventType`, `occurredAt`, `summary`, `severity`, `channel`, `contact`, `agent{}`, `call{}`, `transcript{}`. We will NOT use `message{}` or `knowledgeBase{}` in the pilot.
- **Auto-upsert behavior:** agents/calls/transcripts are created on first event and updated on subsequent events keyed by their `id`. Transcript `thread` is **replaced** (not appended) on each ingest — send the full thread once at call end, not per turn.
- **Channel:** must be `"voice"` for Kavya.

### Allowed event types (pilot uses only these two)

1. `call.started` — fires when ConversationRelay / MediaStream session opens.
2. `call.completed` — fires after `process_post_call_data()` finishes; contains full `transcript.thread` + `transcript.summary` + `call.outcome` from the existing extractor.

### Kavya extracted dict — exact fields available (post_call.py lines 41-51, 361-369)

```
guest_name, num_guests, check_in, check_out, room_preference,
availability_result, call_outcome, follow_up_needed, summary
```

`call_outcome` is one of: `booking_confirmed | booking_inquiry | general_inquiry | callback_requested | no_availability | dropped | other`.

### Dashboard run/build commands (package.json:6-13)

- `npm install`
- `npm run prisma:generate`
- `npm run db:push` (runs `tsx prisma/bootstrap.ts`)
- `npm run db:seed` (optional — populates demo data)
- Dev: `npm run dev`
- Prod: `npm run build && npm start` (binds `HOST:PORT`, defaults `0.0.0.0:3000`)

### Anti-patterns (do NOT do)

- ❌ Send a separate webhook per user/agent turn — `thread` is replace-on-write; per-turn calls cause history loss. (Pilot uses one terminal `call.completed` event.)
- ❌ Invent fields not in `ingest.ts:7-79` (e.g. `recording_url` at top level — must go inside `call.metadata`).
- ❌ Hard-fail the call on dashboard error. The client must be **fire-and-forget with timeout**; n8n POST is the source of truth for the pilot.
- ❌ Remove or modify the existing `_post_to_n8n` call in `post_call.py`. Run both in parallel.
- ❌ Hardcode the agent id. Use `DASHBOARD_AGENT_ID` env var (Kavya's stable identifier).
- ❌ Use `requests` (sync) — Kavya is async; use `httpx.AsyncClient` or reuse the existing `aiohttp` session from `booking_api.get_session()`.

---

## Phase 1 — Stand up the dashboard on the VPS

**Goal:** Reachable, authenticated dashboard at `https://dashboard.taskforceai.tech` (or chosen subdomain), Swagger renders at `/docs`, ingest endpoint accepts a hand-rolled curl test.

### Tasks

1. **Decide subdomain & DNS** — recommend `dashboard.taskforceai.tech`. Add A record → `67.207.90.109`.
2. **Provision on VPS** under `/opt/agent-dashboard`:
   - `ssh root@67.207.90.109`
   - `git clone https://github.com/ChrysFernando/agent-dashboard.git /opt/agent-dashboard`
   - Install Node 20 if missing (`node -v` to check).
   - `cd /opt/agent-dashboard && npm install`
3. **Configure `.env`** — copy `.env.example` and fill every required key (full list in Phase 0 discovery). Critically:
   - `PORT=3100` (avoid clash with Kavya's 8000 and other agents 8001-8003)
   - `HOST=127.0.0.1` (nginx will proxy)
   - `DATABASE_URL=file:/opt/agent-dashboard/data/dashboard.db` (mkdir the `data/` folder first)
   - `INBOUND_WEBHOOK_SECRET=<openssl rand -hex 32>` — **save this; Kavya needs it**
   - `DASHBOARD_HTML_PATH=./Aether AI Agent Dashboard.html` (the bundled UI file)
4. **Initialize DB**: `npm run prisma:generate && npm run db:push && npm run db:seed`.
5. **Run under a process manager** — create `/etc/systemd/system/agent-dashboard.service` running `npm start` after `npm run build`, restart on failure. Verify with `systemctl status agent-dashboard`.
6. **nginx vhost** — proxy `dashboard.taskforceai.tech` → `127.0.0.1:3100`, reuse existing certbot setup (mirror the pattern from `voice.taskforceai.tech`).
7. **Smoke tests**:
   - `curl https://dashboard.taskforceai.tech/docs/openapi.json | jq .info.title` → "Agent Dashboard API"
   - Open `/docs` in browser — Swagger UI renders.
   - Hand-rolled ingest test:
     ```bash
     curl -X POST https://dashboard.taskforceai.tech/api/webhooks/agent-events \
       -H "x-aether-secret: $INBOUND_WEBHOOK_SECRET" \
       -H "Content-Type: application/json" \
       -d '{"eventType":"call.started","channel":"voice","agent":{"id":"kavya","name":"Kavya"},"call":{"id":"test-001","contact":"+9477xxx","status":"active","startedAt":"2026-05-23T05:00:00Z"}}'
     ```
     → returns `{eventId, agentId, callId, ...}`. Confirm row in dashboard UI agent list.

### Verification checklist

- [ ] `https://dashboard.taskforceai.tech/docs` loads.
- [ ] OpenAPI JSON accessible at `/docs/openapi.json`.
- [ ] curl ingest with secret returns 2xx + ids.
- [ ] curl ingest **without** secret returns 401.
- [ ] systemd unit survives `systemctl restart agent-dashboard`.

**Delegation:** Run as a single sub-task on the VPS. No parallelism here; sequential and short.

---

## Phase 2 — Build `Kavya/dashboard_client.py` (pure module, no Kavya wiring yet)

**Goal:** A standalone async module that, when env vars are set, POSTs `call.started` and `call.completed` events to the dashboard. When env vars are unset, every function is a silent no-op. Fully unit-testable without Kavya running.

### Files to create

- `Kavya/dashboard_client.py` (new)
- `Kavya/test_dashboard_client.py` (new, runnable manually with `python -m`)

### Module surface (exactly these functions, no extras)

```python
async def send_call_started(
    call_sid: str, caller_phone: str, lang: str, started_at_iso: str
) -> None: ...

async def send_call_completed(
    call_sid: str,
    caller_phone: str,
    lang: str,
    started_at_iso: str,
    ended_at_iso: str,
    duration_sec: int,
    full_transcript: list[dict[str, str]],   # [{"role": "user"|"assistant", "text": ...}]
    extracted: dict[str, Any],                # the post_call extractor output
) -> None: ...
```

### Implementation rules (copy from existing patterns)

- HTTP client: reuse `from booking_api import get_session` (aiohttp) — same pattern as `post_call.py:287-292`. Do NOT introduce httpx.
- Env vars (read once at module import, log a single line on startup):
  - `DASHBOARD_API_URL` (e.g. `https://dashboard.taskforceai.tech`)
  - `DASHBOARD_API_KEY` (the `INBOUND_WEBHOOK_SECRET` value from Phase 1)
  - `DASHBOARD_AGENT_ID` (e.g. `kavya`)
- If any of the three are missing → every function returns immediately. Log `"[dashboard] disabled (env not set)"` once at import.
- Per-call timeout: 5 seconds. On exception or non-2xx, log `"[dashboard] send failed: <reason>"` and return — never raise.
- Payload mapping for `call.started`:
  ```python
  {
    "eventType": "call.started",
    "occurredAt": started_at_iso,
    "channel": "voice",
    "contact": caller_phone,
    "agent": {"id": DASHBOARD_AGENT_ID, "name": "Kavya", "type": "booking"},
    "call": {
      "id": call_sid,
      "direction": "inbound",
      "status": "active",
      "contact": caller_phone,
      "startedAt": started_at_iso,
      "metadata": {"language": lang},
    },
  }
  ```
- Payload mapping for `call.completed` — derive `transcript.thread` from `full_transcript` by mapping `role==user → speaker:"user"` and `role==assistant → speaker:"agent"`, `timestampOffsetSec=0` for every entry (we don't track per-turn offsets yet; acceptable for pilot):
  ```python
  {
    "eventType": "call.completed",
    "occurredAt": ended_at_iso,
    "channel": "voice",
    "contact": caller_phone,
    "agent": {"id": DASHBOARD_AGENT_ID},
    "call": {
      "id": call_sid,
      "status": "completed",
      "outcome": extracted.get("call_outcome"),
      "startedAt": started_at_iso,
      "endedAt": ended_at_iso,
      "durationSec": duration_sec,
      "followUpRequired": extracted.get("follow_up_needed") == "Yes",
      "metadata": {
        "language": lang,
        "guest_name": extracted.get("guest_name"),
        "num_guests": extracted.get("num_guests"),
        "check_in": extracted.get("check_in"),
        "check_out": extracted.get("check_out"),
        "room_preference": extracted.get("room_preference"),
        "availability_result": extracted.get("availability_result"),
      },
    },
    "transcript": {
      "startedAt": started_at_iso,
      "endedAt": ended_at_iso,
      "durationSec": duration_sec,
      "outcome": extracted.get("call_outcome"),
      "summary": extracted.get("summary"),
      "followUpRequired": extracted.get("follow_up_needed") == "Yes",
      "thread": [
        {"speaker": "agent" if t["role"] == "assistant" else "user", "text": t["text"], "timestampOffsetSec": 0}
        for t in full_transcript
      ],
    },
  }
  ```
- Header on every POST: `x-aether-secret: <DASHBOARD_API_KEY>`.

### Verification checklist

- [ ] `python -m Kavya.test_dashboard_client` (or equivalent) — with env vars unset, both functions return without network calls.
- [ ] With env vars pointed at the live dashboard, sending a fake `call.started` for `call_sid="local-test-001"` returns 2xx and appears in the UI.
- [ ] Sending `call.completed` for the same `call_sid` updates the same row (no duplicate).
- [ ] Killing the dashboard mid-send → Kavya client logs the failure and returns within 5s; does not raise.

**Delegation:** One subagent writes the module + test; finishes before Phase 3.

---

## Phase 3 — Wire `dashboard_client` into Kavya's call lifecycle

**Goal:** Production Kavya emits `call.started` on session open and `call.completed` after post-call extraction, for **both** the ConversationRelay path and the Media Streams path. n8n POST is untouched.

### Edits (exact insertion points)

1. **ConversationRelay `call.started`** — `Kavya/server.py:2475` (right after `call_start_time` is set):
   ```python
   asyncio.create_task(dashboard_client.send_call_started(call_sid, caller_phone, lang, call_start_time))
   ```
2. **Media Streams `call.started`** — `Kavya/server.py:1160` (right after `self.call_sid` and phone assignment):
   ```python
   asyncio.create_task(dashboard_client.send_call_started(self.call_sid, self.caller_phone, self.lang, self.call_start_time))
   ```
3. **`call.completed` — single insertion in `post_call.py`** — line 373, immediately after `await _post_to_n8n(payload)` succeeds:
   ```python
   try:
       from dashboard_client import send_call_completed
       await send_call_completed(
           call_sid=call_sid,
           caller_phone=caller_phone,
           lang=lang,
           started_at_iso=call_start_time,
           ended_at_iso=call_end_time,
           duration_sec=int(...),  # compute from start/end
           full_transcript=full_transcript,
           extracted=extracted,
       )
   except Exception as exc:
       logger.warning("[dashboard] send_call_completed failed: %s", exc)
   ```
   (The dashboard client already swallows errors; the outer try is belt-and-suspenders so an import/typo failure here never breaks the n8n path.)
4. **Import** `dashboard_client` at the top of `server.py` and `post_call.py`. If the module is absent in any environment, both files must still import (use `try/except ImportError` and stub the functions to no-ops).

### Env wiring

- Add to `Kavya/.env.example`:
  ```
  DASHBOARD_API_URL=
  DASHBOARD_API_KEY=
  DASHBOARD_AGENT_ID=kavya
  ```
- Add to `Kavya/docker-compose.yml` under the `kavya` service `environment:` block (pass-through from VPS `.env`):
  ```yaml
  - DASHBOARD_API_URL=${DASHBOARD_API_URL:-}
  - DASHBOARD_API_KEY=${DASHBOARD_API_KEY:-}
  - DASHBOARD_AGENT_ID=${DASHBOARD_AGENT_ID:-kavya}
  ```

### Verification checklist

- [ ] `grep -n "send_call_started" Kavya/server.py` → 2 hits (both lifecycle paths).
- [ ] `grep -n "send_call_completed" Kavya/post_call.py` → 1 hit, after the n8n POST.
- [ ] `grep -n "_post_to_n8n" Kavya/post_call.py` → still present and unchanged.
- [ ] Local syntax: `python -m py_compile Kavya/server.py Kavya/post_call.py Kavya/dashboard_client.py`.

**Delegation:** One subagent edits both files; runs the grep checks itself before reporting done.

---

## Phase 4 — Deploy + live end-to-end test

**Goal:** A real inbound call to `+18157832822` appears in the dashboard UI with full transcript and extracted summary, while continuing to land in the existing n8n / Google Sheets flow.

### Tasks

1. On VPS, update `/opt/kavya/.env` with the three `DASHBOARD_*` vars (using the secret from Phase 1).
2. Redeploy Kavya:
   ```
   ssh root@67.207.90.109 "cd /opt/kavya && git pull && docker compose up -d --force-recreate kavya"
   ```
3. Tail Kavya logs and confirm at startup: `"[dashboard] enabled → https://dashboard.taskforceai.tech"`.
4. Place a test call from a Mobitel SIM to `+18157832822` (confirmed working in the 2026-05-16 incident). Speak a short booking inquiry that exercises a tool call (availability check).
5. Within 2s of "Hello" greeting, refresh dashboard UI → call row visible with status `active`.
6. Hang up. Within ~10s (post-call extraction time), the row updates to `completed` with the full transcript thread, summary, `call_outcome`, and `metadata.check_in/check_out`.
7. Confirm the same call also landed in n8n / Google Sheets (existing flow untouched).

### Verification checklist

- [ ] Dashboard `/api/transcripts` returns the new transcript via API.
- [ ] Transcript `thread` length matches turn count in Kavya container logs.
- [ ] `transcript.summary` is non-empty.
- [ ] Google Sheet row for the same `call_sid` exists.
- [ ] Kavya container logs show no `[dashboard] send failed` lines.

### Rollback

- Unset `DASHBOARD_API_URL` in `/opt/kavya/.env` and restart Kavya. Module reverts to no-op. No code revert needed.

---

## Phase 5 — Sign-off & next-pilot prep

- Document the agent_id convention (`kavya`, future: `bsl`, `slic`, `sofia`, `flico`) in `Kavya/CLAUDE.md` and root `CLAUDE.md`.
- File a follow-up issue list (do NOT implement in pilot): per-agent API keys, recording upload, transcript append-mode, KB sync, BSL rollout using the same `dashboard_client` pattern.
- Run `graphify update .` so the new `dashboard_client.py` lands in the knowledge graph.

---

## Parallelization summary

| Phase | Parallelizable? | Subagents |
|-------|-----------------|-----------|
| 1 — VPS deploy of dashboard | No (sequential VPS work) | 1 |
| 2 — `dashboard_client.py` module | Yes — independent of Phase 1 wiring | 1 (can run alongside Phase 1) |
| 3 — Kavya hook wiring | Depends on Phase 2 module surface | 1 |
| 4 — Live verification | Depends on 1, 2, 3 | 1 |

Phases 1 and 2 can run in parallel. Phase 3 starts once Phase 2's function signatures are committed. Phase 4 is the join point.
