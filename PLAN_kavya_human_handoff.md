# Kavya Human-Agent Handoff Plan (English / ConversationRelay)

**Goal:** When an English caller asks to speak to a human, Kavya transfers the live call to a single configured human phone number, plays a brief whisper to the human on pickup, falls back to Kavya if the human doesn't answer, and logs a `call.transferred` event to the dashboard.

**Out of scope:** Sinhala/Tamil (Media Streams) handoff, round-robin, queues, SMS context, Flex, sentiment-based auto-transfer, voicemail recording.

---

## Phase 0 — Allowed APIs & Anti-Patterns (read once, reference everywhere)

### Twilio mechanisms confirmed from official docs

1. **End the ConversationRelay session from server-side WebSocket** — send `{"type": "end", "handoffData": "<json-string>"}`. Twilio then POSTs to the `action` URL on `<Connect action="...">` with the relay's `SessionStatus`, `SessionDuration`, and the verbatim `HandoffData` string. ([WebSocket messages docs](https://www.twilio.com/docs/voice/conversationrelay/websocket-messages), [Connect/ConversationRelay docs](https://www.twilio.com/docs/voice/twiml/connect/conversationrelay))

2. **`<Connect action="https://...">`** — required on the parent `<Connect>` to receive the relay-ended callback. Without it, the call proceeds to the next TwiML verb or hangs up if none. We will use this as our handoff branch point.

3. **`<Dial>` with whisper** — `<Dial><Number url="https://...">+9477...</Number></Dial>`. The `url` returns TwiML the called party hears BEFORE bridge. Caller does not hear it.

4. **`<Dial action="...">` callback** — Twilio POSTs `DialCallStatus` (`completed`, `answered`, `busy`, `no-answer`, `failed`, `canceled`), `DialCallSid`, `DialCallDuration`, `DialBridged`. We use this to detect no-answer and re-enter Kavya.

5. **Re-enter ConversationRelay after failed dial** — return fresh `<Response><Connect action="..."><ConversationRelay url="wss://..." welcomeGreeting="Sorry, no agent was available..."/></Connect></Response>` from the dial-action URL.

6. **`welcomeGreeting` attribute on `<ConversationRelay>`** — TTS string spoken at session start. Used for the "no agent available" recovery greeting.

### Architecture (Path A — preferred)

```
Caller asks for human
  → LLM calls transfer_to_human(reason)
  → tools.py returns {"status":"transferring","reason":"..."}
  → server captures reason on the session, sends a brief "Connecting you now" via relay text
  → server sends {"type":"end","handoffData":"{json reason}"} over the WebSocket
  → Twilio ends relay, POSTs to /voice/relay-action with HandoffData
  → /voice/relay-action returns:
      <Response>
        <Dial action="/voice/dial-result" timeout="20">
          <Number url="/voice/whisper?reason=...">{HUMAN_AGENT_PHONE}</Number>
        </Dial>
      </Response>
  → Human picks up, hears whisper, then bridges to caller
  → On dial end, Twilio POSTs DialCallStatus to /voice/dial-result
  → If completed → <Response><Hangup/></Response>
  → If no-answer/busy/failed → fresh <Connect><ConversationRelay welcomeGreeting="Sorry, no agent was available, how can I help?"/></Connect>
```

### Anti-patterns (do NOT do)

- ❌ Send unidentified WebSocket message types — 10+ malformed messages triggers error 64105 and Twilio kills the session.
- ❌ Forget the `action` attribute on `<Connect>` — without it we can't intercept the relay-end.
- ❌ Use REST `client.calls(sid).update(twiml=...)` for the handoff. Path A via `{"type":"end"}` is cleaner. Keep REST API as fallback only.
- ❌ Pass the human's phone number through TwiML query strings unencoded.
- ❌ Send `transfer_to_human` tool to the Sinhala/Tamil sessions — they use Media Streams, not ConversationRelay. The relay-end mechanism does not exist there.
- ❌ Add per-turn dashboard pings or recording. Out of scope.
- ❌ Bridge cold without whisper. We promised whisper.
- ❌ Read `HUMAN_AGENT_PHONE` inside tool handler. Read once at module/lifespan startup, log presence/absence.

---

## Phase 1 — Tool definition + handler (English-only gating)

**Goal:** New `transfer_to_human(reason: str)` tool registered for ConversationRelay sessions only. Handler returns a structured signal; the actual TwiML/end-session is wired in Phase 2.

### Edits

1. **`Kavya/tools.py`** — add to `TOOL_DEFINITIONS` (Anthropic schema, around line 184):
   ```python
   {
       "name": "transfer_to_human",
       "description": (
           "Transfer the live phone call to a human agent. Call this ONLY when "
           "the caller explicitly asks to speak to a human, agent, manager, or "
           "real person. Do not use for routine questions you can answer yourself."
       ),
       "input_schema": {
           "type": "object",
           "properties": {
               "reason": {
                   "type": "string",
                   "description": "One short sentence summarising why the caller is being transferred (e.g. 'caller wants to discuss a special booking request').",
               },
           },
           "required": ["reason"],
       },
   },
   ```
   `get_tools_openai()` / `get_tools_gemini()` already auto-convert. No change needed.

2. **`Kavya/tools.py` `execute_tool` dispatch** (~line 297) — add elif after `cancel_booking`:
   ```python
   elif tool_name == "transfer_to_human":
       reason = (tool_input.get("reason") or "").strip() or "Caller requested human assistance."
       return {"status": "transferring", "reason": reason}
   ```
   No handler function call — the LLM's tool result is just an in-band signal that the server's relay loop will detect.

3. **Gating by language** — in `server.py` `_build_system_prompt(lang, ...)` and wherever tools are passed to the LLM, only include `transfer_to_human` when `lang == "en"`. Cleanest path: filter in the LLM-streaming code paths (one place inside `ws_conversation`), since the Sinhala/Tamil Media Streams handler is a separate file/code path that we don't touch. Add a one-line filter:
   ```python
   tools_for_session = tools if lang == "en" else [t for t in tools if t["name"] != "transfer_to_human"]
   ```
   Pass `tools_for_session` to `_run_llm_streaming_claude/openai/gemini` instead of `tools`. (For ConversationRelay only `lang == "en"` reaches this code today, but the filter future-proofs.)

4. **System prompt** — `server.py:_build_system_prompt` around line 580. Insert:
   ```
   - If the guest explicitly asks to speak to a human, agent, manager, or real person, immediately call the transfer_to_human tool with a one-sentence reason. Do NOT promise a callback; the tool handles the live transfer.
   - Do not offer to transfer proactively. Only transfer when the guest asks.
   ```

### Verification

- [ ] `grep -n "transfer_to_human" Kavya/tools.py` — at least 2 hits (schema + dispatch).
- [ ] `grep -n "transfer_to_human" Kavya/server.py` — at least 2 hits (tools filter + system prompt).
- [ ] `python -m py_compile Kavya/tools.py Kavya/server.py` — clean.
- [ ] Manual: invoke `execute_tool("transfer_to_human", {"reason": "test"})` from a Python REPL → returns `{"status": "transferring", "reason": "test"}`.

**Anti-patterns guard**: do NOT also include `transfer_to_human` in Media Streams system prompts (`media_stream_server.py` if separate, or Sinhala/Tamil prompt branches in `server.py`).

---

## Phase 2 — End-session + handoff TwiML endpoints

**Goal:** Detect the `transfer_to_human` tool result in the LLM streaming loop, send a brief "Connecting you now" message, end the ConversationRelay session with `handoffData`, then handle the `<Connect action>` POST and dial the human with whisper + fallback.

### Edits

1. **Env + config** — `.env.example` and `docker-compose.yml`:
   ```
   HUMAN_AGENT_PHONE=
   ```
   Pass-through in compose: `- HUMAN_AGENT_PHONE=${HUMAN_AGENT_PHONE:-}`. Read once in `server.py` near the existing env reads (line ~82):
   ```python
   HUMAN_AGENT_PHONE: str = os.getenv("HUMAN_AGENT_PHONE", "").strip()
   ```
   On startup, log `"[handoff] enabled → <phone>"` or `"[handoff] disabled (HUMAN_AGENT_PHONE not set)"`.

2. **Detect the tool result in the LLM streaming functions** (`_run_llm_streaming_claude`, `_run_llm_streaming_openai`, `_run_llm_streaming_gemini` — same pattern in all three). After `result_str = await execute_tool(...)` (~line 2405), parse the result. If it parses as JSON and `status == "transferring"`:
   - Speak a brief line: send `{"type":"text","token":"Of course, connecting you to a human agent now. One moment please.","last":True}` over the WebSocket.
   - Set a flag on the session (e.g. `pending_transfer_reason = result["reason"]`) and break out of the tool loop.
   - After the loop, if `pending_transfer_reason` is set, send the end-session message:
     ```python
     import json as _json
     await websocket.send_text(_json.dumps({
         "type": "end",
         "handoffData": _json.dumps({
             "action": "transfer_to_human",
             "reason": pending_transfer_reason,
             "caller_phone": caller_phone,
         }),
     }))
     ```
   - Then `break` out of the outer `while True` receive loop so the server stops sending more messages. The `finally` block then runs post-call processing as usual (transcript still complete up to the transfer point).

   **Important**: keep this logic in `ws_conversation` only. Do NOT add it to Media Streams handlers.

3. **`server.py:/voice/incoming`** — modify the existing TwiML so the `<Connect>` has an `action` attribute pointing at a new endpoint:
   ```xml
   <Connect action="https://{host}/voice/relay-action" method="POST">
     <ConversationRelay ... />
   </Connect>
   ```
   (Use the same `host` variable already in scope.)
   Apply this in **every** place that currently builds `<Connect><ConversationRelay/></Connect>` — `/voice/incoming` AND `/voice/language-selected` English branch. Search-and-pair.

4. **New endpoint `POST /voice/relay-action`** in `server.py` — handles the relay-end callback. Reads `HandoffData` form field, decides:
   ```python
   @app.post("/voice/relay-action")
   async def relay_action(request: Request) -> Response:
       form = await request.form()
       handoff_raw = form.get("HandoffData") or ""
       call_sid = form.get("CallSid", "")
       try:
           handoff = json.loads(handoff_raw) if handoff_raw else {}
       except json.JSONDecodeError:
           handoff = {}

       if handoff.get("action") == "transfer_to_human" and HUMAN_AGENT_PHONE:
           reason = handoff.get("reason", "Caller requested assistance.")
           caller_phone = handoff.get("caller_phone", "")
           # Fire-and-forget dashboard event
           if dashboard_client is not None:
               asyncio.create_task(dashboard_client.send_call_transferred(
                   call_sid=call_sid,
                   caller_phone=caller_phone,
                   reason=reason,
                   human_phone=HUMAN_AGENT_PHONE,
               ))
           whisper_url = f"https://{request.url.hostname}/voice/whisper?reason={quote(reason)}"
           dial_action_url = f"https://{request.url.hostname}/voice/dial-result"
           twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Dial action="{dial_action_url}" method="POST" timeout="20" answerOnBridge="true">
    <Number url="{whisper_url}">{HUMAN_AGENT_PHONE}</Number>
  </Dial>
</Response>"""
           return Response(content=twiml, media_type="application/xml")

       # Default: relay ended for another reason (caller hung up etc.) — just hang up.
       return Response(
           content='<?xml version="1.0" encoding="UTF-8"?><Response><Hangup/></Response>',
           media_type="application/xml",
       )
   ```

5. **New endpoint `POST /voice/whisper`** in `server.py`:
   ```python
   @app.post("/voice/whisper")
   async def whisper(request: Request) -> Response:
       reason = request.query_params.get("reason", "Incoming caller.")
       # Keep it short and natural — TTS happens on the human's leg only
       text = f"Incoming caller. {reason}. Connecting now."
       safe = escape(text)  # html.escape; reason is user/LLM-derived — sanitize
       twiml = f'<?xml version="1.0" encoding="UTF-8"?><Response><Say voice="Polly.Joanna">{safe}</Say></Response>'
       return Response(content=twiml, media_type="application/xml")
   ```
   Use `html.escape` from stdlib.

6. **New endpoint `POST /voice/dial-result`** in `server.py`:
   ```python
   @app.post("/voice/dial-result")
   async def dial_result(request: Request) -> Response:
       form = await request.form()
       status = form.get("DialCallStatus", "")
       host = request.url.hostname
       if status in ("completed", "answered"):
           # Human took the call. Whether they hung up or caller did, we're done.
           return Response(
               content='<?xml version="1.0" encoding="UTF-8"?><Response><Hangup/></Response>',
               media_type="application/xml",
           )
       # no-answer / busy / failed / canceled → put caller back into Kavya
       greeting = "Sorry, no agent was available. I'm Kavya, how can I help?"
       twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Connect action="https://{host}/voice/relay-action" method="POST">
    <ConversationRelay url="wss://{host}/ws/conversation?lang=en&amp;recovered=1"
        ttsProvider="elevenlabs"
        welcomeGreeting="{escape(greeting)}"
        interruptible="true"
        dtmfDetection="true"/>
  </Connect>
</Response>"""
       return Response(content=twiml, media_type="application/xml")
   ```
   **Note:** copy the exact `<ConversationRelay>` attributes from the existing `/voice/incoming` TwiML — don't drop transcriptionProvider, voice, language, speechModel, etc. The plan snippet above is abbreviated.

7. **`dashboard_client.py`** — add `send_call_transferred` modeled on `send_call_started` (per Phase 0 mapping):
   ```python
   async def send_call_transferred(
       call_sid: str,
       caller_phone: str,
       reason: str,
       human_phone: str,
   ) -> None:
       _announce_once()
       if not _ENABLED:
           return
       payload = {
           "eventType": "call.transferred",
           "occurredAt": datetime.now(timezone.utc).isoformat(),
           "summary": f"Transferred to human: {reason}",
           "severity": "info",
           "channel": "voice",
           "contact": caller_phone,
           "agent": {"id": DASHBOARD_AGENT_ID, "name": "Kavya", "type": "booking"},
           "call": {
               "id": call_sid,
               "status": "transferred",
               "contact": caller_phone,
               "metadata": {
                   "transfer_reason": reason,
                   "human_phone": human_phone,
               },
           },
       }
       await _post(payload)  # reuse the existing private POST helper
   ```
   (Wrap the actual POST helper name to match whatever `dashboard_client.py` already uses — likely an internal `_post(payload)` or inlined `aiohttp.post`.)

### Verification

- [ ] `grep -n "type.*end" Kavya/server.py` — confirms WebSocket end-message is sent.
- [ ] `grep -n "action=" Kavya/server.py` shows `<Connect action=` in every `/voice/*` endpoint that builds a ConversationRelay TwiML.
- [ ] `python -m py_compile Kavya/server.py Kavya/dashboard_client.py` — clean.
- [ ] Local TwiML preview: hit each new endpoint with `curl -X POST localhost:8000/voice/whisper?reason=test` — returns valid XML containing the expected `<Say>`.
- [ ] Unit-ish: from a Python REPL, call `send_call_transferred("CAtest", "+9477x", "test reason", "+9477y")` with env unset → exits silently; with env set → returns 2xx from the live dashboard.

### Anti-patterns

- Don't hardcode the dashboard URL in the new endpoints — use `dashboard_client`.
- Don't include the actual `HUMAN_AGENT_PHONE` in the whisper URL query string — it's already in `<Number>...</Number>`.
- Don't forget `answerOnBridge="true"` on `<Dial>` — without it, Twilio considers the call "in progress" on machine pickup too.

---

## Phase 3 — Deploy + live test

### Tasks

1. **Set env var** on VPS: append to `/opt/kavya/.env`:
   ```
   HUMAN_AGENT_PHONE=+9477xxxxxxx     # user-supplied
   ```
   The user will tell us the number before we deploy. Do NOT pick a number or test without one.
2. **Rsync** the changed files: `tools.py`, `server.py`, `dashboard_client.py`, `.env.example`, `docker-compose.yml` → `root@67.207.90.109:/opt/kavya/`. Back up first.
3. **Rebuild + restart**: `ssh root@67.207.90.109 "cd /opt/kavya && docker compose build kavya && docker compose up -d --force-recreate kavya"`.
4. **Confirm startup logs** show both `[dashboard] enabled → ...` and `[handoff] enabled → +9477...`.

### Live test (human places the call)

A. **Happy path**: caller dials `+18157832822`, presses 1 for English, says *"I'd like to speak to a human please"*. Expect:
   - Kavya replies "Of course, connecting you to a human agent now. One moment please."
   - Within 1–2 seconds, the configured human phone rings.
   - On pickup, human hears: *"Incoming caller. Caller wants to speak with a human. Connecting now."*
   - Bridge happens; caller and human can talk.
   - Either party hangs up → call ends cleanly.
   - Dashboard `/api/transcripts` shows the original transcript; `/api/overview.liveFeed` shows a `call.transferred` event.

B. **No-answer fallback**: don't answer the human phone for 20+ seconds. Expect:
   - Caller hears "Sorry, no agent was available. I'm Kavya, how can I help?"
   - Conversation continues. Caller can ask another question or hang up.
   - SQLite has the original call row and the transferred event; the recovered Kavya leg creates a separate call row (since CallSid is the same Twilio call, the row should UPSERT, not duplicate — verify this behavior).

### Verification subagent

- Pull last 5 minutes of `kavya-voice-agent` logs, grep for `handoff|transfer|dashboard|relay-action|dial-result|whisper`.
- Query SQLite: `SELECT id, eventType, occurredAt FROM AgentEvent ORDER BY createdAt DESC LIMIT 6;` — must show `call.transferred` for the test CallSid.
- Confirm n8n post-call POST still fires (existing behavior unchanged).

### Anti-patterns / rollback

- If transfer breaks the call (caller hears silence, immediate hangup), `ssh root@67.207.90.109 "cd /opt/kavya && cp server.py.bak.<ts> server.py && docker compose up -d --force-recreate kavya"` reverts to pre-handoff code. Backups taken in Phase 3 step 2.
- If `<Connect action>` doesn't fire as expected, fall back to **Path B**: use Twilio REST `client.calls(call_sid).update(twiml=...)` directly inside the tool handler. This requires `TWILIO_ACCOUNT_SID` and `TWILIO_AUTH_TOKEN` (already in env per Phase 0 mapping). Document but do not implement unless Path A fails.

---

## Phase 4 — Sign-off + handoff to senior engineer

- Update `Kavya/CLAUDE.md` with one paragraph documenting the handoff flow and the four new endpoints.
- Update root `CLAUDE.md` with one line: "Kavya supports live human handoff via `transfer_to_human` tool (English only)."
- Tell the senior engineer: dashboard now receives `call.transferred` events alongside `call.started` / `call.completed`. UI can render handoff stats from `/api/overview.liveFeed` and `/api/agents/:id` (new event types appear automatically — no backend schema change needed).
- `graphify update .` to refresh the knowledge graph.

---

## Parallelization summary

| Phase | Parallelizable? | Subagents |
|-------|----------------|-----------|
| 1 — Tool + system prompt + gating | No (small, sequential) | 1 |
| 2 — End-session + TwiML endpoints + dashboard event | Yes — endpoint code + dashboard_client edit can run in parallel | 2 |
| 3 — Deploy + live test | Sequential, gated on human placing the call | 1 + 1 verification |
| 4 — Docs/handoff | Trivial | 1 |

Total: ~4-5 subagents.
