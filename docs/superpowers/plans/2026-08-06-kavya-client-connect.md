# Kavya Dialog Client Connect Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move Kavya's inbound voice path from Twilio to an isolated Dialog Client Connect WSS service while keeping Flico and legacy Kavya traffic safe and adding fail-closed Dialog MCP handover.

**Architecture:** Port the proven strict SmartPBX wire, gateway, transport, and MCP boundary into Kavya with a Kavya-specific configuration namespace and token header. Add one Kavya SmartPBX session adapter instead of reusing its Twilio-bound wire loop. Run it as an isolated `kavya-smartpbx` Compose profile behind a new loopback-only Nginx WSS endpoint.

**Tech Stack:** Python 3.11, FastAPI/Starlette WebSockets, G.711 mu-law 8 kHz, Azure or Google STT, Claude/OpenAI/Gemini provider support, Dialog MCP HTTP Streamable, Docker Compose, Nginx, pytest.

## Global Constraints

- Do not modify Flico code, its deployment, its WSS token/header, or its Dialog workflow.
- SmartPBX endpoint is exactly `/ws/v1/smartpbx/media` and accepts only `g711_ulaw` at `8000` Hz.
- Kavya header is exactly `X-Kavya-SmartPBX-Token`; compare `SMARTPBX_WS_TOKEN` constant-time before accepting the socket.
- Capacity is four active calls; start deadline is ten seconds; idle timeout is 90 seconds; maximum message is 64 KiB; decoded audio maximum is 32 KiB; outbound queue maximum is 128 frames.
- SmartPBX service mode must not expose or activate Twilio HTTP/TwiML/WebSocket ingress.
- No secret, raw caller number, full call ID, media payload, transcript, or MCP response body may enter source, logs, status, tests, examples, or commits.
- Transfer calls may use only code-configured logical destinations from `SMARTPBX_TRANSFER_DESTINATIONS_JSON`; empty/missing/partial MCP configuration disables transfer.
- MCP uses `otherLegCallId` as the active Dialog call ID and exactly one configured account-header spelling.
- Azure STT credentials/provider selection are explicitly forwarded to the isolated SmartPBX service.
- Every code task follows red-green-refactor and ends in an atomic commit.

---

### Task 1: Port the strict Dialog protocol, transport, and gateway boundary

**Files:**
- Create: `Kavya/smartpbx_protocol.py`
- Create: `Kavya/smartpbx_transport.py`
- Create: `Kavya/smartpbx_gateway.py`
- Create: `Kavya/tests/test_smartpbx_protocol.py`
- Create: `Kavya/tests/test_smartpbx_transport.py`
- Create: `Kavya/tests/test_smartpbx_gateway.py`

**Interfaces:**
- Consumes: FastAPI `WebSocket`, raw Dialog JSON events, `SMARTPBX_WS_TOKEN`, `SMARTPBX_ACCOUNT_ID`, `SMARTPBX_AUTH_HEADER_NAME`.
- Produces: immutable validated `CallContext`, `SmartPBXMediaTransport`, one admitted lifecycle callback with `start()`, `feed_audio(bytes)`, `finish(schedule_post_call: bool)`, and `terminal_future`.

- [ ] **Step 1: Write failing protocol tests**

```python
def test_kavya_start_accepts_only_ulaw_8khz():
    context = parse_start_event(valid_start(media_encoding="g711_ulaw", sample_rate=8000))
    assert context.call_id == "call-1"

def test_kavya_start_rejects_non_ulaw_media():
    with pytest.raises(ProtocolError):
        parse_start_event(valid_start(media_encoding="pcm16", sample_rate=8000))
```

- [ ] **Step 2: Run the focused tests and confirm they fail**

Run: `PYTHONPATH=. python -m pytest tests/test_smartpbx_protocol.py -q`
Expected: import failure because the SmartPBX protocol module does not exist.

- [ ] **Step 3: Implement the validated protocol and bounded transport**

```python
@dataclass(frozen=True)
class CallContext:
    call_id: str
    other_leg_call_id: str
    caller_number: str
    callee_number: str
    account_id: str

def parse_start_event(event: Mapping[str, object]) -> CallContext:
    # Require non-empty Dialog identifiers and g711_ulaw/8000 exactly.
    ...
```

Implement only documented outbound `media` events, a single serialized sender, stale-audio clearing, and idempotent close.

- [ ] **Step 4: Add admission/lifecycle tests and implementation**

```python
async def test_gateway_rejects_fifth_call_before_websocket_accept():
    gateway = SmartPBXGateway(settings(max_calls=4), session_factory)
    await occupy_four_slots(gateway)
    await gateway.handle(websocket)
    websocket.close.assert_awaited_once_with(code=1013)
```

Implement pre-accept token/account checks, exact start ordering, bounded size checks, deadlines, and exactly-once slot release. Parameterize `SMARTPBX_AUTH_HEADER_NAME` and default it to `X-Kavya-SmartPBX-Token`.

- [ ] **Step 5: Run the Task 1 suite**

Run: `PYTHONPATH=. python -m pytest tests/test_smartpbx_protocol.py tests/test_smartpbx_transport.py tests/test_smartpbx_gateway.py -q`
Expected: all SmartPBX protocol, transport, and gateway tests pass.

- [ ] **Step 6: Commit Task 1**

```bash
git add Kavya/smartpbx_protocol.py Kavya/smartpbx_transport.py Kavya/smartpbx_gateway.py Kavya/tests/test_smartpbx_protocol.py Kavya/tests/test_smartpbx_transport.py Kavya/tests/test_smartpbx_gateway.py
git commit -m "feat(kavya): add strict Dialog SmartPBX gateway"
```

### Task 2: Add the Kavya SmartPBX media-session adapter

**Files:**
- Modify: `Kavya/server.py`
- Create: `Kavya/smartpbx_session.py`
- Create: `Kavya/tests/test_smartpbx_server.py`

**Interfaces:**
- Consumes: `CallContext`, `SmartPBXMediaTransport`, existing Kavya STT/LLM/TTS/KB/PMS/post-call helpers.
- Produces: `KavyaSmartPBXSession.start()`, `feed_audio(bytes)`, `finish(schedule_post_call: bool)`, `terminal_future`.

- [ ] **Step 1: Write failing lifecycle and post-call tests**

```python
@pytest.mark.asyncio
async def test_dialog_hangup_finishes_once_and_schedules_kavya_post_call(monkeypatch):
    session = make_session(context(other_leg_call_id="safe-call", caller_number="+94000000000"))
    await session.start()
    await asyncio.gather(session.finish(True), session.finish(True))
    assert post_call_calls == 1
```

- [ ] **Step 2: Run the focused tests and confirm failure**

Run: `PYTHONPATH=. python -m pytest tests/test_smartpbx_server.py -q`
Expected: import failure because the SmartPBX session adapter does not exist.

- [ ] **Step 3: Implement the narrow adapter**

```python
class KavyaSmartPBXSession:
    async def start(self) -> None: ...
    async def feed_audio(self, payload: bytes) -> None: ...
    async def finish(self, schedule_post_call: bool) -> None: ...
    @property
    def terminal_future(self) -> asyncio.Future[None]: ...
```

Reuse Kavya's STT provider selection and tool/LLM/PMS/KB behavior. Route output only through `SmartPBXMediaTransport`; do not emit Twilio `clear`, `mark`, `streamSid`, or TwiML. Populate post-call metadata from validated Dialog context and redact observable identifiers.

- [ ] **Step 4: Add the SmartPBX service-mode boundary and route**

```python
if KAVYA_SERVICE_MODE == "smartpbx":
    app.add_api_websocket_route("/ws/v1/smartpbx/media", smartpbx_gateway.handle)
    # Legacy Twilio ingress remains unavailable in this mode.
```

Expose only `/health` and a bounded `/smartpbx/status` alongside the WSS route in SmartPBX mode.

- [ ] **Step 5: Run the Task 2 suite**

Run: `PYTHONPATH=. python -m pytest tests/test_smartpbx_server.py tests/test_smartpbx_gateway.py -q`
Expected: lifecycle, privacy, status, and service-mode boundary tests pass.

- [ ] **Step 6: Commit Task 2**

```bash
git add Kavya/server.py Kavya/smartpbx_session.py Kavya/tests/test_smartpbx_server.py
git commit -m "feat(kavya): run Dialog calls through Kavya voice pipeline"
```

### Task 3: Add fail-closed Dialog MCP handover

**Files:**
- Create: `Kavya/smartpbx_mcp.py`
- Modify: `Kavya/smartpbx_session.py`
- Modify: `Kavya/tools.py`
- Modify: `Kavya/requirements-prod.txt`
- Modify: `Kavya/requirements-prod.lock.txt`
- Create: `Kavya/tests/test_smartpbx_mcp.py`

**Interfaces:**
- Consumes: logical `transfer_to_human` tool call, validated `other_leg_call_id`, `SMARTPBX_TRANSFER_DESTINATIONS_JSON`, Dialog MCP environment.
- Produces: `DialogMCPCallControl.transfer_call(destination_key, call_id)` with bounded result/failure classification.

- [ ] **Step 1: Write failing fail-closed handover tests**

```python
async def test_transfer_rejects_destination_not_in_operator_allowlist():
    control = control_with_destinations({"human_support": "tel:+94110000000"})
    with pytest.raises(TransferDisabled):
        await control.transfer_call("attacker_uri", "safe-call")

async def test_missing_account_header_disables_transfer_without_network_call():
    control = control_with_missing_account_header()
    await control.transfer_call("human_support", "safe-call")
    assert fake_mcp.calls == []
```

- [ ] **Step 2: Run the focused tests and confirm failure**

Run: `PYTHONPATH=. python -m pytest tests/test_smartpbx_mcp.py -q`
Expected: import failure because the MCP client does not exist.

- [ ] **Step 3: Implement bounded MCP transfer control**

```python
async def transfer_call(self, destination_key: str, call_id: str) -> TransferResult:
    destination = self._destinations.get(destination_key)
    if not destination or not self._enabled:
        raise TransferDisabled("handover is unavailable")
    return await self._call_tool("transfer_call", {
        "destination_number": destination,
        "call_id": call_id,
    })
```

> **CORRECTED 2026-08-11:** `destination_number` is not the real argument name and this sketch
> never transferred a call. Dialog's live `transfer_call` schema is `{"number": <string>}`
> (required), taking the bare dialable number with the `tel:` scheme stripped. See
> `Kavya/smartpbx_mcp.py::_wire_destination` and the live-observed contract block in
> `Kavya/tests/test_smartpbx_mcp.py`.

Use one explicit configured account header, MCP HTTPS timeouts, one retry only for retryable transport/server failures, response bounds, and no secret/PII logging. Add `mcp>=1.28,<2` and regenerate the lock using the repository's Python 3.11 resolver.

- [ ] **Step 4: Wire the logical Kavya handover tool only in SmartPBX sessions**

```python
if tool_name == "transfer_to_human" and self.call_control is not None:
    await self.call_control.transfer_call("human_support", self.context.other_leg_call_id)
```

Keep Twilio `<Dial>` behavior confined to legacy mode. On unavailable/failed transfer, return a safe response and optionally invoke the existing notification fallback without claiming a transfer succeeded.

- [ ] **Step 5: Run the Task 3 suite**

Run: `PYTHONPATH=. python -m pytest tests/test_smartpbx_mcp.py tests/test_smartpbx_server.py -q`
Expected: all transfer allowlist, auth, retry, tool, and fallback tests pass.

- [ ] **Step 6: Commit Task 3**

```bash
git add Kavya/smartpbx_mcp.py Kavya/smartpbx_session.py Kavya/tools.py Kavya/requirements-prod.txt Kavya/requirements-prod.lock.txt Kavya/tests/test_smartpbx_mcp.py
git commit -m "feat(kavya): add allowlisted Dialog handover"
```

### Task 4: Add isolated deployment, operator contract, and deployment tests

**Files:**
- Modify: `Kavya/.env.example`
- Modify: `Kavya/docker-compose.yml`
- Modify: `Kavya/Dockerfile`
- Create: `Kavya/nginx-smartpbx.conf`
- Create: `Kavya/SMARTPBX_RUNBOOK.md`
- Create: `Kavya/tests/test_smartpbx_deployment.py`

**Interfaces:**
- Consumes: `KAVYA_SERVICE_MODE=smartpbx`, `ENABLE_SMARTPBX_WSS=true`, a fresh `SMARTPBX_WS_TOKEN`, `SMARTPBX_ACCOUNT_ID`, optional MCP configuration.
- Produces: profile `kavya-smartpbx`, loopback `127.0.0.1:8006`, WSS-only Nginx virtual host and operator cutover/rollback procedure.

- [ ] **Step 1: Write failing deployment contract tests**

```python
def test_kavya_smartpbx_is_loopback_only_and_uses_its_own_port():
    compose = yaml.safe_load(read_text("docker-compose.yml"))
    assert "127.0.0.1:8006:8000" in compose["services"]["kavya-smartpbx"]["ports"]

def test_examples_never_enable_or_populate_live_transfer():
    example = read_text(".env.example")
    assert "SMARTPBX_TRANSFER_DESTINATIONS_JSON={}" in example
    assert "SMARTPBX_API_KEY=" in example
```

- [ ] **Step 2: Run the deployment tests and confirm failure**

Run: `PYTHONPATH=. python -m pytest tests/test_smartpbx_deployment.py -q`
Expected: missing SmartPBX deployment artifacts.

- [ ] **Step 3: Implement the isolated profile**

```yaml
kavya-smartpbx:
  profiles: ["smartpbx"]
  ports: ["127.0.0.1:8006:8000"]
  environment:
    KAVYA_SERVICE_MODE: smartpbx
    ENABLE_SMARTPBX_WSS: "true"
    STT_PROVIDER: ${STT_PROVIDER:-azure}
    AZURE_SPEECH_KEY: ${AZURE_SPEECH_KEY:-}
    AZURE_SPEECH_REGION: ${AZURE_SPEECH_REGION:-}
```

Copy every new Python module in the Dockerfile's explicit allowlist. Nginx exposes only WSS, health, and status, uses TLS 1.2+, loopback upstream, 120-second proxy timeouts, and `access_log off`.

- [ ] **Step 4: Write the operator runbook**

Document unique-token generation, dashboard fields, test call procedure, transfer-disabled state, non-production transfer drill prerequisites, Flico-preserving rollback, and the required Dialog data: exact account header and approved test/production destinations.

- [ ] **Step 5: Run the Task 4 suite**

Run: `PYTHONPATH=. python -m pytest tests/test_smartpbx_deployment.py -q`
Expected: all Dockerfile, Compose, Nginx, environment-example, and runbook contract tests pass.

- [ ] **Step 6: Commit Task 4**

```bash
git add Kavya/.env.example Kavya/docker-compose.yml Kavya/Dockerfile Kavya/nginx-smartpbx.conf Kavya/SMARTPBX_RUNBOOK.md Kavya/tests/test_smartpbx_deployment.py
git commit -m "ops(kavya): add isolated Dialog SmartPBX service"
```

### Task 5: Verify the integrated migration and prepare review

**Files:**
- Modify: `docs/superpowers/specs/2026-08-06-kavya-client-connect-design.md` only if verification changes an accepted constraint.

**Interfaces:**
- Consumes: complete Tasks 1-4 and configured test environment.
- Produces: reproducible verification evidence and a review-ready branch.

- [ ] **Step 1: Run all Kavya tests**

Run: `PYTHONPATH=. python -m pytest tests -q`
Expected: all existing and SmartPBX tests pass.

- [ ] **Step 2: Run dependency and container verification**

Run: `python -m pip install --dry-run --ignore-installed -r requirements-prod.lock.txt`
Expected: success on Python 3.11, including the MCP dependency.

Run: `docker compose --profile smartpbx config`
Expected: valid isolated `kavya-smartpbx` configuration with no exposed non-loopback port.

- [ ] **Step 3: Inspect the complete diff for credentials and scope drift**

Run: `git diff --check origin/main...HEAD && git diff origin/main...HEAD`
Expected: no whitespace errors, no token/API-key literals, no Flico changes, and only planned Kavya/docs files.

- [ ] **Step 4: Perform final code review and commit any review fixes**

Review correctness, security, privacy, protocol compliance, bounded resources, Twilio-mode isolation, and transfer allowlisting. Re-run all affected tests after any fix.

- [ ] **Step 5: Commit the design/plan documentation if not already committed**

```bash
git add docs/superpowers/specs/2026-08-06-kavya-client-connect-design.md docs/superpowers/plans/2026-08-06-kavya-client-connect.md
git commit -m "docs(kavya): define Dialog Client Connect migration"
```
