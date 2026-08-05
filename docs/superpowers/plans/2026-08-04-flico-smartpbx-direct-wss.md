# Flico Direct SmartPBX WSS Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a production-hardened, direct Dialog SmartPBX WSS call path into Flico with native G.711 u-law media, safe MCP handoff, four-call admission control, and no Asterisk dependency.

**Architecture:** A strict SmartPBX protocol/gateway layer adapts Dialog events to Flico's existing `MediaStreamSession` through the existing `MediaTransport` protocol. Dialog MCP call control is isolated behind an allowlisted interface. A feature-gated second Compose service and dedicated Nginx configuration keep SmartPBX operationally separate from current Flico/Twilio and optional Asterisk paths on the same VPS.

**Tech Stack:** Python 3.11, FastAPI 0.141.1, Starlette 1.3.1, Pydantic 2.13.4, httpx 0.28.1, MCP Python SDK 1.x, pytest, Docker Compose, Nginx.

## Global Constraints

- The SmartPBX production call path MUST NOT traverse or depend on Asterisk or Twilio.
- Existing `/voice/*`, `/ws/conversation`, `/ws/media-stream/{lang}`, and optional Asterisk behavior must remain backward compatible.
- The new WSS endpoint is exactly `/ws/v1/smartpbx/media` and is disabled by default.
- Only `g711_ulaw` at sample rate `8000` is accepted.
- Maximum simultaneous SmartPBX sessions is `4` by default and must be enforced before media resources start.
- Inbound WSS authentication uses only `X-Flico-SmartPBX-Token`; the Dialog API key is outbound-only.
- The endpoint must fail closed when required token/account configuration is missing.
- SmartPBX `start.otherLegCallId` is the Dialog MCP `call_id`; never substitute `start.callId`.
- MCP exposes only `transfer_call` and `hangup_call`; transfer destinations are code-enforced logical-key allowlists.
- No live credential value may enter source, examples, tests, logs, status output, close reasons, or metrics.
- No media payload, transcript, full caller number, or full call identifier may be logged.
- All queues, messages, timeouts, retries, and external response sizes must be bounded.
- Python code must compile on Python 3.11.
- No production deployment, DNS mutation, SmartPBX dashboard mutation, or credential use occurs in this implementation branch.
- Source decisions follow the client-provided SmartPBX PDF, the Dialog public example only for undocumented compatibility events, [FastAPI WebSockets](https://fastapi.tiangolo.com/advanced/websockets/), [Starlette WebSockets](https://www.starlette.io/websockets/), and the [MCP Python SDK client documentation](https://py.sdk.modelcontextprotocol.io/client/).

---

## File Map

- Create `Flico Agent/smartpbx_protocol.py`: strict, dependency-light parsing and validated event dataclasses.
- Create `Flico Agent/smartpbx_transport.py`: bounded outbound media queue and SmartPBX `MediaTransport` implementation.
- Create `Flico Agent/smartpbx_mcp.py`: production MCP settings, destination allowlist, and stable call-control client.
- Create `Flico Agent/smartpbx_gateway.py`: authentication, admission control, WebSocket lifecycle, telemetry, and status snapshot.
- Modify `Flico Agent/server.py`: feature-gated route, session factory, SmartPBX handoff tool wiring, and idempotent post-call completion.
- Modify `Flico Agent/docker-compose.yml`: opt-in `smartpbx` profile service on loopback port 8005.
- Create `Flico Agent/nginx-smartpbx.conf`: dedicated public WSS virtual host example.
- Modify `Flico Agent/.env.example`: keyless SmartPBX configuration contract.
- Modify `Flico Agent/requirements-prod.txt` and regenerate `requirements-prod.lock.txt`: stable MCP SDK v1 (`mcp>=1.28,<2`).
- Create `Flico Agent/tests/test_smartpbx_protocol.py`.
- Create `Flico Agent/tests/test_smartpbx_transport.py`.
- Create `Flico Agent/tests/test_smartpbx_mcp.py`.
- Create `Flico Agent/tests/test_smartpbx_gateway.py`.
- Create `Flico Agent/tests/test_smartpbx_server.py`.
- Create `Flico Agent/SMARTPBX_RUNBOOK.md`.
- Modify `Flico Agent/CLAUDE.md`, then regenerate `Flico Agent/AGENTS.md` with `bash ops/sync-agent-docs.sh`.

---

### Task 1: Strict SmartPBX Protocol Contract

**Files:**
- Create: `Flico Agent/smartpbx_protocol.py`
- Create: `Flico Agent/tests/test_smartpbx_protocol.py`

**Interfaces:**
- Produces: `ProtocolViolation(close_code: int, public_reason: str, failure_class: str)`.
- Produces: immutable `MediaFormat`, `CallContext`, `ConnectedEvent`, `StartEvent`, `MediaEvent`, `DtmfEvent`, `HangupEvent`, `StopEvent`, `UnknownEvent` dataclasses.
- Produces: `parse_smartpbx_event(raw: str, *, max_message_chars: int, max_audio_bytes: int) -> SmartPBXEvent`.
- Produces: `validate_event_context(event: SmartPBXEvent, context: CallContext) -> None`.

- [ ] **Step 1: Write failing contract tests**

Write tests that use this canonical start event and vary one property per test:

```python
START = {
    "event": "start",
    "start": {
        "callId": "call-main",
        "otherLegCallId": "call-other-leg",
        "callerIdNumber": "+94110000000",
        "calleeIdNumber": "+94114698850",
        "accountId": "account-1",
        "mediaFormat": {"encoding": "g711_ulaw", "sampleRate": "8000"},
    },
}
```

Cover: valid start; integer/string sample rate normalization; missing/blank required fields; unsupported encoding/rate; valid media base64; invalid/non-canonical base64; decoded audio above limit; valid DTMF digit/duration; invalid digit; valid hangup/stop/connected; bounded unknown event; message over limit; non-object JSON; duplicate context ID mismatch through `validate_event_context`. Assert generic `public_reason` strings contain no input data.

- [ ] **Step 2: Run tests and verify RED**

Run:

```bash
cd "Flico Agent"
/home/dev/full-voice-agent/.venv/bin/python -m pytest tests/test_smartpbx_protocol.py -q
```

Expected: collection fails because `smartpbx_protocol` does not exist.

- [ ] **Step 3: Implement the closed event union**

Use `json.loads`, `base64.b64decode(..., validate=True)`, frozen dataclasses, bounded strings, and explicit field access. Do not deserialize with `dict.get()` defaults for required fields. `parse_smartpbx_event` must reject before decoding when `len(raw) > max_message_chars`.

Use these close classes consistently:

```python
POLICY_VIOLATION = 1008
MESSAGE_TOO_BIG = 1009
INTERNAL_ERROR = 1011
TRY_AGAIN_LATER = 1013
```

`MediaEvent` stores decoded `audio: bytes`, not the base64 string. Unknown events store only a bounded event name, never the raw body.

- [ ] **Step 4: Run focused and existing tests**

Run the focused test, then:

```bash
/home/dev/full-voice-agent/.venv/bin/python -m pytest tests/test_smartpbx_protocol.py tests/test_invariants.py -q
```

Expected: all selected tests pass.

- [ ] **Step 5: Commit**

```bash
git add "Flico Agent/smartpbx_protocol.py" "Flico Agent/tests/test_smartpbx_protocol.py"
git commit -m "feat(flico): define strict SmartPBX event contract"
```

---

### Task 2: Bounded SmartPBX Media Transport and Admission Registry

**Files:**
- Create: `Flico Agent/smartpbx_transport.py`
- Create: `Flico Agent/tests/test_smartpbx_transport.py`
- Create: `Flico Agent/smartpbx_gateway.py` (registry/settings/status only in this task)
- Create: `Flico Agent/tests/test_smartpbx_gateway.py` (registry/settings subset)

**Interfaces:**
- Consumes: `CallContext` from Task 1.
- Produces: `SmartPBXMediaTransport(websocket, context, *, max_queue_frames: int)` implementing `MediaTransport`.
- Produces: transport methods `start()`, `send_audio(bytes)`, `send_mark(str)`, `clear_audio()`, `close()` and property `is_active`.
- Produces: `SmartPBXSettings.from_env(environ: Mapping[str, str])`.
- Produces: `SmartPBXSessionRegistry(max_sessions: int)` with async `try_acquire() -> SessionLease | None`, idempotent `SessionLease.release()`, and `snapshot()`.

- [ ] **Step 1: Write failing transport tests**

Use a fake WebSocket whose `send_text` appends JSON strings. Cover exact outbound envelope, serialized sender order, queue overflow dropping the oldest generation's audio without blocking, `clear_audio` dropping queued stale frames, `send_mark` emitting no wire event and clearing the local speaking acknowledgement, close cancellation, send-after-close no-op, and double-close idempotence.

The exact envelope assertion is:

```python
assert json.loads(ws.sent[0]) == {
    "event": "media",
    "callId": "call-main",
    "accountId": "account-1",
    "media": {"payload": base64.b64encode(b"audio").decode("ascii")},
}
```

- [ ] **Step 2: Write failing registry/settings tests**

Cover: feature disabled by default; required token/account when enabled; constant integer bounds; malformed integer rejection; four successful leases; fifth returns `None`; releasing one admits another; double release cannot underflow; snapshot contains only `enabled`, `configured`, `active_sessions`, `max_sessions`, bounded counters, and protocol version.

- [ ] **Step 3: Run tests and verify RED**

```bash
cd "Flico Agent"
/home/dev/full-voice-agent/.venv/bin/python -m pytest tests/test_smartpbx_transport.py tests/test_smartpbx_gateway.py -q
```

Expected: missing module failures.

- [ ] **Step 4: Implement the transport and registry**

Use `asyncio.Queue(maxsize=max_queue_frames)` and one sender task. Queue entries carry a monotonically increasing generation. `clear_audio()` increments the generation and drains without sending undocumented `clear`. Never log payloads.

Settings names and defaults are exact:

```python
ENABLE_SMARTPBX_WSS=false
SMARTPBX_WS_TOKEN=
SMARTPBX_ACCOUNT_ID=
SMARTPBX_MAX_CALLS=4
SMARTPBX_MAX_MESSAGE_CHARS=65536
SMARTPBX_MAX_AUDIO_BYTES=32768
SMARTPBX_MAX_OUTBOUND_FRAMES=128
SMARTPBX_START_TIMEOUT_SECONDS=10
SMARTPBX_IDLE_TIMEOUT_SECONDS=90
```

Use `secrets.compare_digest` in `settings.token_matches(candidate)` and never expose the configured token from the object representation or snapshot.

- [ ] **Step 5: Run focused tests and commit**

```bash
/home/dev/full-voice-agent/.venv/bin/python -m pytest tests/test_smartpbx_transport.py tests/test_smartpbx_gateway.py -q
git add "Flico Agent/smartpbx_transport.py" "Flico Agent/smartpbx_gateway.py" "Flico Agent/tests/test_smartpbx_transport.py" "Flico Agent/tests/test_smartpbx_gateway.py"
git commit -m "feat(flico): add bounded SmartPBX media runtime"
```

---

### Task 3: Allowlisted Dialog MCP Call Control

**Files:**
- Create: `Flico Agent/smartpbx_mcp.py`
- Create: `Flico Agent/tests/test_smartpbx_mcp.py`
- Modify: `Flico Agent/requirements-prod.txt`
- Regenerate: `Flico Agent/requirements-prod.lock.txt`

**Interfaces:**
- Consumes: `CallContext.other_leg_call_id` and `CallContext.account_id`.
- Produces: `CallControl` protocol with `transfer_call(destination_key: str) -> bool` and `hangup_call() -> bool`.
- Produces: `DialogMCPSettings.from_env(environ)` with endpoint, key, expected account, parsed logical destination map, connect/read timeout, and retry count.
- Produces: `DialogMCPCallControl(settings, context, session_factory=...)`.

- [ ] **Step 1: Write failing settings and safety tests**

Cover: disabled/unconfigured state; endpoint must be `https`; API key never appears in `repr`; `SMARTPBX_MCP_ACCOUNT_HEADER` must be explicitly one of `account_id` or `X-Account-ID`; no default and sending both is impossible; destination JSON must be an object of bounded logical keys to `tel:+...` or configured SIP URIs; arbitrary runtime URI rejected; account mismatch rejected; blank `otherLegCallId` rejected; no beta tool method exists.

Example safe configuration:

```python
env = {
    "SMARTPBX_MCP_URL": "https://dialog.example:9443/ucp/v2/mcp",
    "SMARTPBX_API_KEY": "test-secret-never-log",
    "SMARTPBX_ACCOUNT_ID": "account-1",
    "SMARTPBX_TRANSFER_DESTINATIONS_JSON": '{"live_agent":"tel:+94110000000"}',
}
```

- [ ] **Step 2: Write failing client tests around an injected fake session factory**

Assert initialization occurs before `call_tool`. For transfer, assert exact call:

```python
await session.call_tool(
    "transfer_call",
    arguments={"destination_number": "tel:+94110000000"},
)
```

For hangup, assert `hangup_call` with `{}`. Assert outbound headers include `X-API-Key`, exactly the configured account header, and `call_id=otherLegCallId`, and do not include the alternate header or use `callId`. Cover timeout, MCP `isError`, malformed result, non-retryable auth/validation failure, one retry for transport/5xx only, and structured logs containing neither key, URI, full call IDs, nor response bodies.

- [ ] **Step 3: Run tests and verify RED**

```bash
cd "Flico Agent"
/home/dev/full-voice-agent/.venv/bin/python -m pytest tests/test_smartpbx_mcp.py -q
```

- [ ] **Step 4: Implement using the stable official MCP SDK**

Add this source constraint:

```text
mcp>=1.28,<2
```

Use `mcp.client.streamable_http.streamable_http_client` with an injected `httpx.AsyncClient(headers=..., timeout=..., follow_redirects=False)`, then `ClientSession`, `initialize()`, and `call_tool()`. Keep SDK creation behind a small `_open_session()` async context manager so tests inject a fake and no test reaches the network.

Do not hand-roll Streamable HTTP JSON-RPC. Do not accept tool names or destinations from the model.

- [ ] **Step 5: Regenerate the lock reproducibly**

Use the repository's documented Python 3.11 locking command when Docker is available. On this Docker-less dev rig, use a temporary Python 3.11 environment or let CI generate/verify the lock; never hand-edit a partial dependency set. The committed lock must contain `mcp` v1 and all resolved transitives, and `pip check` must pass in Python 3.11 before merge.

- [ ] **Step 6: Run tests and commit**

```bash
/home/dev/full-voice-agent/.venv/bin/python -m pytest tests/test_smartpbx_mcp.py -q
git add "Flico Agent/smartpbx_mcp.py" "Flico Agent/tests/test_smartpbx_mcp.py" "Flico Agent/requirements-prod.txt" "Flico Agent/requirements-prod.lock.txt"
git commit -m "feat(flico): add allowlisted Dialog MCP call control"
```

---

### Task 4: WebSocket Gateway and Existing Flico Session Integration

**Files:**
- Modify: `Flico Agent/smartpbx_gateway.py`
- Modify: `Flico Agent/server.py`
- Modify: `Flico Agent/tests/test_smartpbx_gateway.py`
- Create: `Flico Agent/tests/test_smartpbx_server.py`

**Interfaces:**
- Consumes: protocol, transport, registry, settings, and MCP call control from Tasks 1-3.
- Produces: `SmartPBXGateway.handle(websocket, session_factory)` and `snapshot()`.
- Produces: feature-gated FastAPI route `/ws/v1/smartpbx/media`.
- Extends: `MediaStreamSession(..., call_control: CallControl | None = None)` and idempotent `finish(schedule_post_call: bool = False)`.

- [ ] **Step 1: Write failing pure gateway lifecycle tests**

Use fake WebSocket/session factories. Cover: missing/wrong token rejected before accept; disabled/unconfigured rejected; fifth call rejected with 1013; start deadline; `connected` before start; exactly one start; account mismatch; media-before-start; invalid base64; context ID mismatch; media fed to session; `dtmf` observed without action; `hangup`, `stop`, disconnect, and idle timeout terminal behavior; simultaneous terminal/disconnect calls `finish` and releases lease exactly once; sender/transport closed; slot reusable after cleanup; unknown bounded event ignored and counted.

- [ ] **Step 2: Write failing server integration tests**

Using `TestClient` and monkeypatched SmartPBX settings/session factory, assert:

- route is registered at exactly `/ws/v1/smartpbx/media`;
- existing Flico routes still return their prior responses;
- a canonical start/media/stop exchange produces the documented outbound media envelope;
- `/smartpbx/status` never contains token, API key, account ID, caller number, or call IDs;
- server imports with SmartPBX disabled and without an MCP credential;
- Asterisk remains disabled/default and no SmartPBX code imports `asterisk_ari` or `asterisk_rtp`.

- [ ] **Step 3: Run tests and verify RED**

```bash
cd "Flico Agent"
/home/dev/full-voice-agent/.venv/bin/python -m pytest tests/test_smartpbx_gateway.py tests/test_smartpbx_server.py -q
```

- [ ] **Step 4: Implement gateway state machine**

The state transition is exact:

```text
NEW -> ACCEPTED -> STARTED -> TERMINAL
```

Authentication and lease acquisition happen before `accept()`. The start wait uses `asyncio.wait_for`; each later receive uses the idle timeout. Catch `WebSocketDisconnect` as normal terminal behavior. Catch `ProtocolViolation` and close with its generic code/reason. Catch unexpected exceptions, emit a redacted `smartpbx_session_failed` event, and close 1011. Cleanup lives in one `finally` block.

Structured events contain only: `event`, generated `session_id`, hashed/truncated `call_fingerprint`, bounded `outcome`/`failure_class`, active count, and duration. Never include raw messages.

- [ ] **Step 5: Integrate the existing media pipeline and transfer tool**

Add `call_control` only for SmartPBX sessions. When present and `lang == "en"`, expose the existing logical `transfer_to_human` tool to Claude. In `MediaStreamSession._run_llm_claude`, inspect the final message exactly as the ConversationRelay implementation does, but invoke only:

```python
await self.call_control.transfer_call("live_agent")
```

Speak a short hold message before the MCP request. The tool input `reason` may be logged only as a bounded failure-free category or omitted; never send it as a destination. A successful transfer marks the session inactive. On LLM/STT/TTS pipeline failure, attempt the same allowlisted fallback once when configured.

Refactor the duplicated post-call scheduling into a private idempotent method, called by Twilio `run()` and SmartPBX `finish(schedule_post_call=True)`. Asterisk `stop()` retains its current behavior and does not gain post-call side effects.

- [ ] **Step 6: Run focused tests, then full Flico suite**

```bash
/home/dev/full-voice-agent/.venv/bin/python -m pytest tests/test_smartpbx_gateway.py tests/test_smartpbx_server.py tests/test_demo_endpoint.py -q
/home/dev/full-voice-agent/.venv/bin/python -m pytest tests -q
```

Expected: all tests pass; the existing single `audioop` deprecation warning is unchanged.

- [ ] **Step 7: Commit**

```bash
git add "Flico Agent/server.py" "Flico Agent/smartpbx_gateway.py" "Flico Agent/tests/test_smartpbx_gateway.py" "Flico Agent/tests/test_smartpbx_server.py"
git commit -m "feat(flico): connect SmartPBX WSS to voice pipeline"
```

---

### Task 5: Feature-Gated Deployment, Nginx Hardening, and Operator Runbook

**Files:**
- Modify: `Flico Agent/docker-compose.yml`
- Create: `Flico Agent/nginx-smartpbx.conf`
- Modify: `Flico Agent/.env.example`
- Create: `Flico Agent/SMARTPBX_RUNBOOK.md`
- Modify: `Flico Agent/CLAUDE.md`
- Generate: `Flico Agent/AGENTS.md`
- Create: `Flico Agent/tests/test_smartpbx_deployment.py`

**Interfaces:**
- Produces: opt-in Compose profile `smartpbx`, service `flico-smartpbx`, container `flico-smartpbx`, loopback port `127.0.0.1:8005:8000`.
- Produces: public candidate URL `wss://smartpbx-flico.taskforceai.tech/ws/v1/smartpbx/media` as an operator value, not a claim that DNS/TLS already exists.

- [ ] **Step 1: Write failing deployment contract tests**

Parse YAML if PyYAML is available; otherwise inspect normalized text. Assert the SmartPBX service has profile `smartpbx`, uses the same immutable image expression, has no UDP ports, has no Asterisk environment variables, sets `ENABLE_ASTERISK_ARI=false`, sets `ENABLE_SMARTPBX_WSS=true`, uses the separate port, reuses required read-only code/KB mounts, has bounded logs and healthcheck, and is absent from a default profile selection.

Assert `.env.example` contains keys only, never values matching the exposed credential. Assert Nginx exposes only the WSS route plus `/health` and `/smartpbx/status`, uses Upgrade headers, TLS, `proxy_buffering off`, body/connection limits, long but finite timeouts, and no header logging. Assert the runbook explicitly marks DNS, TLS, rotated API key, transfer URI, source IPs, dashboard update, and carrier fallback test as operator gates.

- [ ] **Step 2: Run tests and verify RED**

```bash
cd "Flico Agent"
/home/dev/full-voice-agent/.venv/bin/python -m pytest tests/test_smartpbx_deployment.py -q
```

- [ ] **Step 3: Add the opt-in Compose service**

Use `profiles: ["smartpbx"]`. The service must not be started by the repository's normal `docker compose up -d` auto-deploy. It shares the immutable image but has its own `container_name`, loopback port, healthcheck, restart policy, and log rotation. It must not publish RTP/SIP ports or join an Asterisk-specific path.

- [ ] **Step 4: Add Nginx and environment examples**

Add keyless values:

```dotenv
ENABLE_SMARTPBX_WSS=false
SMARTPBX_WS_TOKEN=
SMARTPBX_ACCOUNT_ID=
SMARTPBX_MAX_CALLS=4
SMARTPBX_MAX_MESSAGE_CHARS=65536
SMARTPBX_MAX_AUDIO_BYTES=32768
SMARTPBX_MAX_OUTBOUND_FRAMES=128
SMARTPBX_START_TIMEOUT_SECONDS=10
SMARTPBX_IDLE_TIMEOUT_SECONDS=90
SMARTPBX_MCP_URL=https://dialog.cybergate.lk:9443/ucp/v2/mcp
SMARTPBX_API_KEY=
SMARTPBX_TRANSFER_DESTINATIONS_JSON={}
SMARTPBX_MCP_ACCOUNT_HEADER=
SMARTPBX_MCP_CONNECT_TIMEOUT_SECONDS=3
SMARTPBX_MCP_READ_TIMEOUT_SECONDS=8
SMARTPBX_MCP_RETRIES=1
```

The Nginx example must use a dedicated `limit_conn_zone`, HSTS, no permissive CORS, and the exact loopback upstream.

- [ ] **Step 5: Write the operator runbook and update mirrored docs**

Document preflight, secret generation/rotation, DNS/TLS, dashboard values, start/stop/rollback commands, health/status checks, a synthetic WSS test using fake non-production IDs, log queries, capacity testing, MCP transfer testing, and the mandatory endpoint-down carrier fallback drill. State explicitly that the exposed old Dialog key must be revoked and never reused.

Update `Flico Agent/CLAUDE.md`, then run:

```bash
bash ops/sync-agent-docs.sh
```

- [ ] **Step 6: Validate and commit**

```bash
/home/dev/full-voice-agent/.venv/bin/python -m pytest tests/test_smartpbx_deployment.py -q
docker compose -f "Flico Agent/docker-compose.yml" config
nginx -t -c "$PWD/Flico Agent/nginx-smartpbx.conf"
```

On the Docker-less dev rig, record those two commands as environment-unavailable rather than claiming they passed; CI/launch preflight must execute them. Then:

```bash
git add "Flico Agent/docker-compose.yml" "Flico Agent/nginx-smartpbx.conf" "Flico Agent/.env.example" "Flico Agent/SMARTPBX_RUNBOOK.md" "Flico Agent/CLAUDE.md" "Flico Agent/AGENTS.md" "Flico Agent/tests/test_smartpbx_deployment.py"
git commit -m "ops(flico): add isolated SmartPBX service runbook"
```

---

### Task 6: Graph Refresh, Security Audit, and End-to-End Verification

**Files:**
- Modify generated graph artifacts under `graphify-out/` only through the required graphify command if the repository tracks their changes.
- Create no production feature code unless a failing verification test first proves the gap.

**Interfaces:**
- Consumes: all prior tasks and the design spec.
- Produces: fresh evidence for every acceptance criterion.

- [ ] **Step 1: Run the SmartPBX test matrix**

```bash
cd "Flico Agent"
/home/dev/full-voice-agent/.venv/bin/python -m pytest \
  tests/test_smartpbx_protocol.py \
  tests/test_smartpbx_transport.py \
  tests/test_smartpbx_mcp.py \
  tests/test_smartpbx_gateway.py \
  tests/test_smartpbx_server.py \
  tests/test_smartpbx_deployment.py -q
```

- [ ] **Step 2: Run full regression and Python 3.11 gates**

```bash
/home/dev/full-voice-agent/.venv/bin/python -m pytest tests -q
python3.11 -m py_compile server.py smartpbx_protocol.py smartpbx_transport.py smartpbx_mcp.py smartpbx_gateway.py
python3.11 -m pip check
```

If local Python 3.11 is unavailable, the matching CI job is required evidence before completion.

- [ ] **Step 3: Audit secrets and forbidden coupling**

From repository root:

```bash
git diff --check
git diff --cached --check
pre-commit run gitleaks --all-files
rg -n "asterisk|twilio" "Flico Agent/smartpbx_"*.py
```

The credential search must return no matches in tracked changes. Any Asterisk/Twilio match in SmartPBX modules must be a test asserting absence, not a runtime import/reference.

- [ ] **Step 4: Refresh graphify after code changes**

On this native Linux dev rig, from repository root:

```bash
graphify update .
GRAPHIFY_VIZ_NODE_LIMIT=9000 graphify cluster-only .
```

Never delete `graphify-out/graph.json`.

- [ ] **Step 5: Independently review the whole branch**

Generate a review package from the merge base and dispatch a fresh senior reviewer. Resolve every Critical/Important finding with a failing regression test first, rerun the covering tests, and re-review until both spec compliance and code quality are approved.

- [ ] **Step 6: Verify branch state**

```bash
git status --short
git log --oneline --decorate "$(git merge-base main HEAD)..HEAD"
```

Expected: only intentional tracked graph outputs may remain uncommitted; otherwise the feature branch is clean. Do not push, open a PR, merge, or deploy without explicit user authorization.

---

## Self-Review Record

- **Spec coverage:** Every design requirement maps to Tasks 1-6, including no-Asterisk isolation, four-call capacity, strict codec, token auth, MCP `otherLegCallId`, allowlisted transfer, idempotent cleanup, deployment isolation, observability, rollback, and operator-only launch gates.
- **Placeholder scan:** The plan contains no `TBD`, `TODO`, “implement later,” or unspecified error-handling steps. Values intentionally awaiting Dialog confirmation are represented as fail-closed environment configuration and explicit launch gates.
- **Type consistency:** `CallContext`, `SmartPBXMediaTransport`, `SmartPBXSettings`, `SmartPBXSessionRegistry`, `CallControl`, `DialogMCPCallControl`, and `SmartPBXGateway` have one spelling and one producer/consumer chain throughout.
