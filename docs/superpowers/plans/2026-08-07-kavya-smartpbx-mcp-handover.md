# Kavya SmartPBX MCP Handover Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** After a separately proven stable SmartPBX call, add a root-provisioned, supervised MCP diagnostic that resolves the account-header conflict without call control, then activate human handover only after allowlisted destination, carrier-outcome, failsafe, and observed-drill gates pass.

**Architecture:** Keep transfer disabled by default. A new call-scoped diagnostic uses the validated `start.accountId` and exact `start.otherLegCallId`, opens a bounded official MCP session, performs only initialize/list_tools, and records a finite non-sensitive outcome. It tries lowercase `account_id` once, retrying once with `X-Account-ID` only after deterministic 4xx authentication/context rejection. Transfer remains a separate allowlisted state machine whose provider acknowledgement is explicitly non-terminal until a carrier contract/failsafe and observed drill prove the outcome.

**Tech Stack:** Python 3.11, pytest/pytest-asyncio, httpx, official MCP Streamable HTTP SDK, FastAPI/WebSockets, Docker Compose, GitHub Actions.

## Global Constraints

- Keep Flico's container, configuration, and running path intact.
- Use TDD red-green evidence for behavior changes and review before deployment.
- Keep secrets, MCP keys, voice IDs, call identifiers, and customer data out of Git, diagnostics, dashboard events, status output, and test fixtures.
- Secret rotation, DID routing beyond the temporary sole-DID verification, Dialog credential changes, carrier contract decisions, and any non-English Dialog language selection require asking first.
- Never remove Twilio, enable MCP transfer before its gates, send both account headers, invoke `call_tool` during the MCP diagnostic, switch headers without the specified deterministic 4xx, or weaken the g711 ulaw admission contract.

---

## Entry gate and file map

This plan is blocked until call-parity Task 9 has current evidence: reviewed commit, green CI/image, protected voice validation, and one supervised Dialog call with expected voice, two-way audio, RAG/booking turn, interruption within stated limit, and no protocol-admission error. A greeting, build, or MCP acknowledgement alone is not evidence.

- Modify: `Kavya/smartpbx_mcp.py:21-245,311-402` — diagnostic settings, bounded session protocol, header decision, safe outcome classification; retain existing transfer control only behind its later gate.
- Modify: `Kavya/smartpbx_session.py:88-224` and `Kavya/server.py:4965-5004` — call-scoped diagnostic scheduling/status with no dashboard values or call control.
- Modify: `Kavya/smartpbx_handover.py:14-169`, `Kavya/tools.py` SmartPBX transfer context, and dashboard sender boundary only to distinguish acknowledgement from verified completion.
- Modify: `Kavya/.env.example:193-202`, `Kavya/docker-compose.yml:138-155`, `Kavya/SMARTPBX_RUNBOOK.md:76-130,197-257` — protected root-only MCP provisioning and exact operator gates.
- Modify: `Kavya/tests/test_smartpbx_mcp.py`, `test_smartpbx_server.py`, `test_smartpbx_provider_handover.py`, `test_smartpbx_handover.py`, `test_smartpbx_handover_timeout_lifecycle.py`, `test_smartpbx_deployment.py`.

No Dialog credential change, dashboard routing change, carrier contract decision, destination value, caller value, account value, API key, or permanent transfer enablement is included in source control. The operator supplies protected values outside Git only after explicit live-action approval.

### Task 1: Make diagnostic configuration independently fail closed

**Files:** Modify `Kavya/smartpbx_mcp.py:21-175`; tests `Kavya/tests/test_smartpbx_mcp.py:23-132`; template/compose/runbook paths above.

**Interfaces:**

- `DialogMCPDiagnosticSettings.from_env(environ: Mapping[str, str]) -> DialogMCPDiagnosticSettings`.
- Fields: endpoint, api_key, connect_timeout_seconds, read_timeout_seconds, max_response_bytes, failure; protected fields have `repr=False`.
- `enabled` is true only for complete bounded diagnostic configuration. It does not consume destination JSON, account header, or transfer enablement.
- Existing `DialogMCPSettings.enabled` remains false until the separate transfer task.

- [ ] **Step 1: Write RED tests**

~~~python
def test_empty_or_partial_diagnostic_config_is_disabled_and_redacted():
    assert DialogMCPDiagnosticSettings.from_env({}).enabled is False
    settings = DialogMCPDiagnosticSettings.from_env({"SMARTPBX_MCP_URL": "https://dialog.example/mcp"})
    assert settings.enabled is False
    assert "https://dialog.example/mcp" not in repr(settings)


def test_diagnostic_does_not_enable_transfer_without_allowlisted_transfer_config():
    diagnostic = diagnostic_settings()
    transfer = DialogMCPSettings.from_env(diagnostic_environment())
    assert diagnostic.enabled is True
    assert transfer.enabled is False
~~~

- [ ] **Step 2: Verify RED** — `cd Kavya && pytest -q tests/test_smartpbx_mcp.py -k diagnostic`; expected import/missing setting failure.
- [ ] **Step 3: Implement GREEN** — validate the same HTTPS/bounds/identity-response protections as `DialogMCPSettings`, but do not require or parse destinations/account header; return fixed failures `not_configured`, `incomplete_configuration`, `invalid_endpoint`, `invalid_api_key`, `invalid_limits`. Store no account/call value in settings. `.env.example` names remain blank; compose passes names only; runbook says root writes values to mode-0600 `/opt/kavya/.env.smartpbx` and no one pastes them into Git/chat/logs.
- [ ] **Step 4: Verify GREEN** — `cd Kavya && pytest -q tests/test_smartpbx_mcp.py -k diagnostic tests/test_smartpbx_deployment.py`; expected PASS and `.transfer_enabled == false`.
- [ ] **Step 5: Commit** — `git add Kavya/smartpbx_mcp.py Kavya/.env.example Kavya/docker-compose.yml Kavya/SMARTPBX_RUNBOOK.md Kavya/tests/test_smartpbx_mcp.py Kavya/tests/test_smartpbx_deployment.py && git diff --cached --check && git commit -m "feat(kavya): add disabled MCP diagnostic configuration"`.

### Task 2: Implement the standalone no-call-tool probe

**Files:** Modify `Kavya/smartpbx_mcp.py:64-70,176-245,311-402`; tests `Kavya/tests/test_smartpbx_mcp.py`.

**Interfaces:**

- Extend protocol with `async def list_tools(self) -> object`.
- `MCPDiagnosticOutcome` is one of `attempted`, `authenticated_context_rejected`, `inconclusive`, `admitted`.
- `DialogMCPDiagnostic.probe(context: CallContext) -> MCPDiagnosticOutcome`.
- Session headers are exactly `X-API-Key`, one account header, and `call_id=context.other_leg_call_id`; no `call_tool` method is reachable from probe.

- [ ] **Step 1: Write RED probe tests**

~~~python
@pytest.mark.asyncio
async def test_probe_uses_exact_other_leg_and_only_initialize_list_tools():
    factory = FakeSessionFactory(FakeListToolsResult())
    outcome = await DialogMCPDiagnostic(diagnostic_settings(), factory).probe(context())
    assert outcome == MCPDiagnosticOutcome.ADMITTED
    assert factory.calls[0]["headers"]["call_id"] == context().other_leg_call_id
    assert set(factory.calls[0]["headers"]) == {"X-API-Key", "account_id", "call_id"}
    assert factory.sessions[0].events == [("initialize",), ("list_tools",)]
    assert not any(event[0] == "call_tool" for event in factory.sessions[0].events)
~~~

- [ ] **Step 2: Verify RED** — `cd Kavya && pytest -q tests/test_smartpbx_mcp.py::test_probe_uses_exact_other_leg_and_only_initialize_list_tools`; expected missing list_tools/probe failure.
- [ ] **Step 3: Implement GREEN** — add `list_tools`; open `_open_session` with the existing bounded transport, `Accept-Encoding: identity`, no redirects, timeouts, response cap, and SDK log filter. Implement `_probe_once(context, account_header)` as initialize then list_tools then close. It sends lowercase `account_id` on first call. `call_id` must be the validated other leg, never media leg. Use only fixed outcome values in logs.
- [ ] **Step 4: Verify GREEN** — `cd Kavya && pytest -q tests/test_smartpbx_mcp.py -k 'probe or bounded_transport or sdk_log_filter'`; expected PASS/no network real call.
- [ ] **Step 5: Commit** — `git add Kavya/smartpbx_mcp.py Kavya/tests/test_smartpbx_mcp.py && git diff --cached --check && git commit -m "feat(kavya): add supervised MCP admission probe"`.

### Task 3: Restrict X-Account-ID retry to deterministic 4xx

**Files:** Modify `Kavya/smartpbx_mcp.py:205-245,341-402`; tests `Kavya/tests/test_smartpbx_mcp.py`.

**Interfaces:**

- `_is_deterministic_auth_context_4xx(error: BaseException) -> bool` returns true only for exposed HTTP status 400-499 specifically classified by the bounded MCP lifecycle as authentication/context rejection; false for cancellation, timeout, TLS/transport, malformed response, connection loss, redirects, and 5xx.
- A retry creates a fresh session and reuses the identical validated `CallContext`; never sends both headers.

- [ ] **Step 1: Write RED decision-matrix tests**

~~~python
@pytest.mark.asyncio
async def test_probe_retries_with_x_account_id_only_after_deterministic_4xx():
    factory = FakeSessionFactory(http_status_error(401), FakeListToolsResult())
    outcome = await DialogMCPDiagnostic(diagnostic_settings(), factory).probe(context())
    assert outcome == MCPDiagnosticOutcome.ADMITTED
    assert factory.calls[0]["headers"].get("account_id") is not None
    assert "X-Account-ID" not in factory.calls[0]["headers"]
    assert factory.calls[1]["headers"].get("X-Account-ID") is not None
    assert "account_id" not in factory.calls[1]["headers"]
    assert factory.calls[0]["headers"]["call_id"] == factory.calls[1]["headers"]["call_id"]

@pytest.mark.parametrize("error", [asyncio.TimeoutError(), httpx.ConnectError("x"), http_status_error(503)])
async def test_probe_never_switches_header_for_inconclusive_errors(error):
    factory = FakeSessionFactory(error)
    assert await DialogMCPDiagnostic(diagnostic_settings(), factory).probe(context()) == MCPDiagnosticOutcome.INCONCLUSIVE
    assert len(factory.calls) == 1
~~~

- [ ] **Step 2: Verify RED** — `cd Kavya && pytest -q tests/test_smartpbx_mcp.py -k 'retries_with_x_account or never_switches_header'`; expected failure/no classifier.
- [ ] **Step 3: Implement GREEN** — first attempt is lowercase `account_id`; only diagnostic-specific deterministic 4xx invokes new `_probe_once(context, "X-Account-ID")`; that second attempt is a fresh session and returns admitted/rejected/inconclusive. Never use a timeout, 5xx, malformed body, TLS failure, connection failure, cancellation, or retry count as a switching signal. Never log headers, status body, IDs, key, or endpoint.
- [ ] **Step 4: Verify GREEN** — `cd Kavya && pytest -q tests/test_smartpbx_mcp.py`; expected PASS including existing transfer retries unchanged.
- [ ] **Step 5: Commit** — `git add Kavya/smartpbx_mcp.py Kavya/tests/test_smartpbx_mcp.py && git diff --cached --check && git commit -m "fix(kavya): constrain MCP account header fallback"`.

### Task 4: Bind the diagnostic to a supervised live SmartPBX call

**Files:** Modify `Kavya/smartpbx_session.py:88-224`, `Kavya/server.py:4965-5004`; tests `Kavya/tests/test_smartpbx_server.py` and `test_smartpbx_mcp.py`.

**Interfaces:**

- `KavyaSmartPBXSession.run_mcp_diagnostic() -> MCPDiagnosticOutcome | None` starts only after validated `start` context and a server-side explicit diagnostic-enabled flag.
- It uses `self._context.account_id` only in-memory for account header and `self._context.other_leg_call_id` as `call_id`; neither crosses dashboard/status/logs.
- Diagnostic cannot call transfer, does not alter `transfer_pending`, and always closes before `finish` completes.

- [ ] **Step 1: Write RED session tests**

~~~python
@pytest.mark.asyncio
async def test_live_diagnostic_receives_validated_context_and_never_changes_transfer_state():
    probe = RecordingProbe()
    session = make_session_with_probe(probe, diagnostic_enabled=True)
    await session.start(); outcome = await session.run_mcp_diagnostic()
    assert outcome == MCPDiagnosticOutcome.ADMITTED
    assert probe.context is session._context
    assert session.transfer_pending is False
    assert probe.call_tool_count == 0

@pytest.mark.asyncio
async def test_diagnostic_disabled_or_prestart_is_a_noop():
    assert await make_session_with_probe(RecordingProbe(), diagnostic_enabled=False).run_mcp_diagnostic() is None
~~~

- [ ] **Step 2: Verify RED** — `cd Kavya && pytest -q tests/test_smartpbx_server.py -k diagnostic`; expected missing method.
- [ ] **Step 3: Implement GREEN** — use a task owned/awaited by session; invoke only from an operator-approved supervised action after stable-call entry gate. Status exposes only fixed outcome (`attempted`, `authenticated_context_rejected`, `inconclusive`, `admitted`) and an enabled boolean; never expose raw IDs/header/key/payload. Disable diagnostic after the observed probe unless operator explicitly schedules another supervised call. Keep `/smartpbx/status` transfer false.
- [ ] **Step 4: Verify GREEN** — `cd Kavya && pytest -q tests/test_smartpbx_server.py tests/test_smartpbx_mcp.py`; expected PASS/no transfer path.
- [ ] **Step 5: Commit** — `git add Kavya/smartpbx_session.py Kavya/server.py Kavya/tests/test_smartpbx_server.py Kavya/tests/test_smartpbx_mcp.py && git diff --cached --check && git commit -m "feat(kavya): bind MCP probe to supervised SmartPBX call"`.

### Task 5: Activate transfer only with carrier outcome/failsafe proof

**Files:** Modify `Kavya/smartpbx_handover.py:14-169`, `Kavya/tools.py` SmartPBX path, relevant dashboard sender boundary; tests `Kavya/tests/test_smartpbx_handover.py`, `test_smartpbx_provider_handover.py`, `test_smartpbx_handover_timeout_lifecycle.py`, `test_smartpbx_deployment.py`.

**Interfaces:**

- Extend `HandoverPhase` with `OUTCOME_UNKNOWN`; provider ack maps directly to `OUTCOME_UNKNOWN`, never completed.
- `record_carrier_outcome(outcome: CarrierTransferOutcome) -> None` is permitted only after Dialog supplies a documented carrier outcome contract; otherwise acknowledged transfer remains `OUTCOME_UNKNOWN` and starts explicit approved failsafe behavior.
- `DialogMCPCallControl.transfer_call(destination_key: str) -> TransferResult` accepts only the existing operator allowlist key `human_support`; configuration is disabled until one approved destination is root-provisioned.

- [ ] **Step 1: Write RED lifecycle tests**

~~~python
@pytest.mark.asyncio
async def test_provider_acknowledgement_is_not_completed_handover():
    coordinator = acknowledged_coordinator()
    result = json.loads(await coordinator.attempt("guest requested human"))
    assert result["confirmation"] == "provider_acknowledged"
    assert coordinator.phase is HandoverPhase.OUTCOME_UNKNOWN
    assert coordinator.transfer_pending is True

@pytest.mark.asyncio
async def test_immediate_failure_notifies_once_and_unallowlisted_destination_never_dispatches():
    coordinator = failed_coordinator()
    assert json.loads(await coordinator.attempt("guest requested human"))["status"] == "unavailable"
    with pytest.raises(TransferDisabled, match="destination_not_allowed"):
        await disabled_control.transfer_call("unapproved")
~~~

- [ ] **Step 2: Verify RED** — `cd Kavya && pytest -q tests/test_smartpbx_handover.py tests/test_smartpbx_provider_handover.py tests/test_smartpbx_handover_timeout_lifecycle.py`; expected current ACKNOWLEDGED semantic mismatch.
- [ ] **Step 3: Implement GREEN** — preserve immediate failure notification/retry and transfer-pending suppression. Acknowledgement cannot emit dashboard “completed/transferred” state; use provider_acknowledged/unknown outcome only. Do not add a carrier outcome parser until Dialog supplies its contract; until then run the documented failsafe and require observed drill. Enforce exact allowlisted logical key/destination format; no caller/LLM can supply a URI. Operator must explicitly approve/provision one non-production allowed destination and later the production target; values are protected and never committed.
- [ ] **Step 4: Verify GREEN** — `cd Kavya && pytest -q tests/test_smartpbx_handover.py tests/test_smartpbx_provider_handover.py tests/test_smartpbx_handover_timeout_lifecycle.py tests/test_smartpbx_handover_cancellation.py tests/test_smartpbx_deployment.py`; expected PASS and no claim that ack equals completed carrier transfer.
- [ ] **Step 5: Commit** — `git add Kavya/smartpbx_handover.py Kavya/tools.py Kavya/tests/test_smartpbx_handover.py Kavya/tests/test_smartpbx_provider_handover.py Kavya/tests/test_smartpbx_handover_timeout_lifecycle.py Kavya/tests/test_smartpbx_deployment.py && git diff --cached --check && git commit -m "fix(kavya): keep Dialog handover pending until outcome proof"`.

### Task 6: Review, deploy, probe, drill, fallback, and final audit

**Files:** Modify `Kavya/SMARTPBX_RUNBOOK.md` only for exact validated procedures; no dashboard/credential/destination values in Git.

- [ ] **Step 1: Run full regression/security evidence** — `cd Kavya && pytest -q tests/test_smartpbx_mcp.py tests/test_smartpbx_server.py tests/test_smartpbx_handover.py tests/test_smartpbx_provider_handover.py tests/test_smartpbx_handover_timeout_lifecycle.py tests/test_smartpbx_deployment.py && pytest -q && python -m compileall -q . && git diff origin/main...HEAD --check`; expected PASS. Inspect diff for key/value leaks without reading protected runtime files.
- [ ] **Step 2: Independent review/CI/image** — review probe no-call-tool guarantee, exact other-leg binding, one-header policy, deterministic-4xx-only retry, bounded network/logging, transfer phases/failsafe/allowlist. Run `gh pr checks 209 --watch --fail-fast`; require green CI and reviewed image SHA before host action.
- [ ] **Step 3: Root-only provision and standalone supervised probe** — after Task 9 of call-parity passes and user approves live action, root adds diagnostic key/endpoint and exact account value to `/opt/kavya/.env.smartpbx` mode 0600. Deploy only `kavya-smartpbx` using existing runbook. During a live active SmartPBX call, run diagnostic once: initialize/list_tools only, `call_id=<otherLegCallId>`, lowercase account_id first. If deterministic 4xx authentication/context rejection occurs, fresh second attempt uses only X-Account-ID with identical active context. Never switch on timeout/TLS/5xx/malformed/connection loss; never invoke call_tool. Record only finite outcome.
- [ ] **Step 4: Fallback/escalation packet** — if both header variants deterministically reject, disable diagnostic/transfer and prepare a redacted Dialog escalation packet: reviewed version/timestamp, finite outcome for each attempt, statement that exact active-context other leg was used, methods initialize/list_tools only, one header per attempt, no request/response/IDs/key. Ask Dialog for authoritative header, account context, and carrier outcome contract. Do not guess further variants.
- [ ] **Step 5: Separate transfer activation/drill** — only after Dialog provides the carrier contract or an explicitly approved failsafe design, root provisions one exact operator allowlisted supervised destination. Run one observed non-production drill, proving immediate failure, provider acknowledgement, post-ack outcome handling, fallback notification, no duplicate dispatch, and dashboard privacy. Restore destination map to `{}` immediately after drill until a separately approved production activation.
- [ ] **Step 6: Independent final audit** — verify each requirement against current test/CI/review/image/deploy/live evidence: stable call preceded probe; correct call ID source; no call_tool; header sequence exactly as required; no sensitive diagnostics; exact allowlist; acknowledgement not completion; observed carrier/failsafe handling; Flico/Twilio untouched. If any evidence is missing, leave transfer disabled and report the missing external contract/action.

## Self-review

- Spec coverage: Tasks 1-4 cover disabled root-provisioned supervised diagnostic, exact active call context, initialize/list_tools only, lower header then deterministic-4xx-only alternate; Task 5 covers allowlist, acknowledgement, post-ack unknown, immediate-failure/failsafe; Task 6 covers CI/review/deploy/probe/drill/escalation/audit.
- No placeholders: every code change provides named interface, RED assertion, command, expected failure/pass, and atomic commit. Protected values are named but never represented.
- Type consistency: `DialogMCPDiagnostic.probe(CallContext)` yields `MCPDiagnosticOutcome`; Task 4 session owns it; Task 5 uses `TransferResult` and `HandoverPhase.OUTCOME_UNKNOWN` independently.
- Independence: this plan cannot start until call-parity live gate succeeds; call-parity remains complete and transfer disabled if this plan is blocked.

## Execution handoff

Plan saved at `docs/superpowers/plans/2026-08-07-kavya-smartpbx-mcp-handover.md`. Execute subagent-driven with review per task. Tasks 1-5 remain code/test work; Task 6 contains all protected operator and user-approved live actions.
