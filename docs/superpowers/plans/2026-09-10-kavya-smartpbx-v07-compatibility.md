# Kavya SmartPBX V07 Compatibility Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Update Kavya's externally visible authenticated SmartPBX compatibility marker and lock the Version 07 vendor contract into tests and operator documentation without changing the WebSocket audio codec, media rate, event payloads, or live transfer behavior.

**Architecture:** Keep `Kavya/smartpbx_protocol.py` as the strict, bounded parser for the V07 `start`, `media`, `dtmf`, and `hangup` events, with the existing `connected` and `stop` compatibility extensions. Change only the externally visible authenticated status marker in `Kavya/smartpbx_gateway.py`; keep the already source-verified live MCP wire key `destination number` and `tier=BYPASS` in `Kavya/smartpbx_mcp.py`, and make the PDF's `destination_number` spelling an explicitly rejected documentation alternative. `hangup_call` remains absent because the current product has no flow that owns the required closing question and caller confirmation.

**Tech Stack:** Python 3.11, FastAPI/Starlette WebSockets, `pytest`/`pytest-asyncio` in GitHub CI, Dialog SmartPBX AI Provider V07 PDF, Dialog Streamable HTTP MCP.

## Global Constraints

- Preserve SmartPBX audio as `g711_ulaw` at `8000` Hz; do not add PCM16, Opus, resampling, or codec conversion.
- Preserve the existing WebSocket URL `/ws/v1/smartpbx/media`, token header authentication, account binding, bounded parser limits, and privacy-safe diagnostics.
- Treat the supplied `/home/dev/incoming/SmartPBX AI Provider - Version 07.pdf` as vendor documentation, not as proof that its inconsistent MCP parameter spelling is the live server schema.
- The authenticated live `tools/list` contract currently uses the exact transfer argument key `destination number`; do not rename it to the PDF table's `destination_number`.
- Every transfer request must include the non-empty disposition argument `tier` with the existing production value `BYPASS`.
- Do not add `hangup_call` until a reviewed product flow owns a clear closing question, explicit caller confirmation, and the no-silence/no-pending-transfer guard required by V07.
- Do not change `Kavya/tools.py`, `Kavya/smartpbx_handover.py`, the MCP endpoint, provider credentials, nginx, Compose, Docker, deployment scripts, or production configuration.
- Do not perform a production provisioning, dashboard mutation, deployment, live transfer, or live call from this plan.

---

## Evidence map and file map

The V07 PDF documents `start` with `callId`, `otherLegCallId`, `callerIdNumber`, `calleeIdNumber`, `accountId`, and `mediaFormat`; `media` with base64 `payload`; `dtmf` with the two leg IDs, `digit`, and optional `duration`; and `hangup` with the two leg IDs and optional `reason`. The current source already validates exactly those shapes in `Kavya/smartpbx_protocol.py`. The only source behavior change in this plan is the internal status label in `Kavya/smartpbx_gateway.py`.

Files to modify:

- `Kavya/smartpbx_gateway.py:29` — set `SMARTPBX_PROTOCOL_VERSION` to the internal V07 marker.
- `Kavya/smartpbx_mcp.py:39-50` — retain the source-verified live MCP spelling/tier constants; no duplicate MCP test is added.
- `Kavya/tests/test_smartpbx_gateway.py:387-412` — assert the V07 marker and the absence of the obsolete unknown-event counter/alias.
- `Kavya/tests/test_smartpbx_mcp.py:377-528` — inspect the existing origin/main live-discovery assertions for exact `destination number` and `tier=BYPASS`; do not duplicate them.
- `Kavya/tests/test_smartpbx_protocol.py:1-300` — retain the existing strict parser tests; add no duplicate V07 parser fixtures unless the source audit identifies a genuinely uncovered PDF field.
- `Kavya/CLAUDE.md` and `Kavya/AGENTS.md` — update the synchronized V06 compatibility declaration to V07.
- `Kavya/SMARTPBX_RUNBOOK.md:545-556` — document the externally visible authenticated V07 marker, the exact Client Connect fields, and the wire/MCP naming distinction.

No new runtime module, dependency, route, event field, audio path, or secret is introduced.

## Dependency and review order

1. Task 1 is the red contract test for the internal marker.
2. Task 2 changes only the marker; it records the already-existing live MCP evidence without adding duplicate tests.
3. Task 3 synchronizes the V06 documentation declarations and updates the runbook; parser tests remain source-verified existing coverage unless a gap is found.
4. Task 4 performs local syntax/static checks and hands behavioral pytest execution to GitHub CI.

The existing MCP and parser suites are the behavioral regression coverage. They run in GitHub CI after the marker/docs changes; this Kavya sandbox does not run local pytest.

### Task 1: Lock the internal V07 status marker (RED)

**Files:**
- Modify: `Kavya/tests/test_smartpbx_gateway.py:387-412`
- No production file changes in the RED commit.

**Interfaces:**
- Consumes: `smartpbx_gateway.SMARTPBX_PROTOCOL_VERSION` and `SmartPBXGateway.snapshot()`.
- Produces: a failing assertion that the internal compatibility marker is exactly `smartpbx-ai-provider-v07` and appears in the status snapshot as `protocol_version`.

- [ ] **Step 1: Add the failing test.**

Append this test next to the existing protocol-export tests, retaining the existing no-unknown-counter assertions:

```python
def test_status_exports_the_v07_internal_compatibility_marker():
    import smartpbx_gateway

    gateway = SmartPBXGateway(settings(), SmartPBXSessionRegistry(4))

    assert smartpbx_gateway.SMARTPBX_PROTOCOL_VERSION == "smartpbx-ai-provider-v07"
    assert gateway.snapshot()["protocol_version"] == "smartpbx-ai-provider-v07"
```

- [ ] **Step 2: Record the RED expectation without running local pytest.**

The test is intentionally expected to fail before the marker change because the current constant is `smartpbx-ai-provider-v06`. Do not run pytest in this Kavya sandbox. Use only a static source check:

```bash
cd Kavya
rg -n 'SMARTPBX_PROTOCOL_VERSION|smartpbx-ai-provider-v0[67]' smartpbx_gateway.py tests/test_smartpbx_gateway.py
python3 -B -m py_compile smartpbx_gateway.py tests/test_smartpbx_gateway.py
```

Expected: the source still contains `smartpbx-ai-provider-v06`; compilation exits 0; no production file change. GitHub CI will execute the red behavioral test after the RED commit.

- [ ] **Step 3: Commit only the RED test.**

```bash
git add Kavya/tests/test_smartpbx_gateway.py
git commit -m "test(kavya): require SmartPBX V07 status marker"
```

### Task 2: Change the marker and preserve the live MCP transfer contract

**Files:**
- Modify: `Kavya/smartpbx_gateway.py:29`
- Inspect only: `Kavya/smartpbx_mcp.py:39-50`, `Kavya/tests/test_smartpbx_mcp.py:377-528`
- Test: `Kavya/tests/test_smartpbx_gateway.py::test_status_exports_the_v07_internal_compatibility_marker`

**Interfaces:**
- Consumes: `DialogMCPSettings.from_env()`, `DialogMCPCallControl.transfer_call(destination_key)`, and the injected `FakeSessionFactory` already used by `test_smartpbx_mcp.py`.
- Produces: `SMARTPBX_PROTOCOL_VERSION == "smartpbx-ai-provider-v07"`, while preserving the already source-verified live MCP payload and its existing tests.

- [ ] **Step 1: Confirm the existing live contract before the production edit.**

Read the origin/main assertions around `Kavya/tests/test_smartpbx_mcp.py:377-528`. They already exercise authenticated live discovery, the exact outbound `destination number` key, and non-empty `tier=BYPASS`. Do not add another parser/MCP test, rename the live key, or turn the PDF's inconsistent `destination_number` spelling into a client assertion. The existing negative tool registry coverage remains the reason `hangup_call` is absent; do not duplicate it here.

- [ ] **Step 2: Change only the internal marker.**

In `Kavya/smartpbx_gateway.py`, replace the current constant with:

```python
SMARTPBX_PROTOCOL_VERSION = "smartpbx-ai-provider-v07"
```

Do not modify `Kavya/smartpbx_mcp.py` unless the source audit finds an accidental drift from the already-live values. If a comment is needed, state that the authenticated live contract uses `destination number`, the PDF's `destination_number` is not authoritative, and the required tier remains `BYPASS`. Do not rename `_TRANSFER_DESTINATION_ARGUMENT`, alter `_wire_destination`, add a retry path, or add `hangup_call`.

- [ ] **Step 3: Run static checks; defer behavior to CI.**

Run:

```bash
cd Kavya
python3 -B -m py_compile smartpbx_gateway.py smartpbx_mcp.py tests/test_smartpbx_gateway.py tests/test_smartpbx_mcp.py
rg -n 'smartpbx-ai-provider-v07|destination number|_TRANSFER_TIER_BYPASS|BYPASS|hangup_call' smartpbx_gateway.py smartpbx_mcp.py tools.py tests/test_smartpbx_mcp.py
git diff --check
```

Expected: compilation and diff checks exit 0; source still contains exact `destination number`, `BYPASS`, and no `hangup_call` definition. GitHub CI runs the existing gateway/MCP behavioral tests after the PR; local output must remain metadata-only.

- [ ] **Step 5: Commit the marker/MCP compatibility change.**

```bash
git add Kavya/smartpbx_gateway.py Kavya/tests/test_smartpbx_gateway.py
git commit -m "fix(kavya): mark SmartPBX runtime as V07"
```

### Task 3: Synchronize V07 operator documentation

**Files:**
- Inspect only: `Kavya/tests/test_smartpbx_protocol.py:1-300` and its existing V06 parser coverage
- Modify: `Kavya/CLAUDE.md`
- Modify: `Kavya/AGENTS.md`
- Modify: `Kavya/SMARTPBX_RUNBOOK.md:545-556`
- No changes to `Kavya/smartpbx_protocol.py` or duplicate parser tests are expected; its existing parser/tests already cover the PDF event fields and preserve `connected`/`stop`.

**Interfaces:**
- Consumes: the existing parser tests and the two synchronized Kavya agent instruction files.
- Produces: V06-to-V07 documentation parity and runbook instructions that distinguish the externally visible authenticated status marker from vendor dashboard fields and the MCP wire key.

- [ ] **Step 1: Verify existing parser coverage and synchronize declarations.**

Read the existing `Kavya/tests/test_smartpbx_protocol.py` tests for `start`, `media`, `dtmf`, `hangup`, optional reason, `g711_ulaw`/8000, malformed fields, and the value-free `connected`/`stop` compatibility events. Do not add duplicate fixtures. Replace the V06 compatibility declaration in both `Kavya/CLAUDE.md` and `Kavya/AGENTS.md` with the same V07 wording, preserving the repository's generated-doc synchronization rule; after editing, assert the two files are byte-identical with `cmp` (or run the repository's documented sync helper if that is the source-of-truth workflow).

- [ ] **Step 2: Add the exact runbook contract.**

Immediately below the existing dashboard fields table in `Kavya/SMARTPBX_RUNBOOK.md`, add:

```markdown
### SmartPBX AI Provider V07 compatibility boundary

The authenticated `/smartpbx/status` field `protocol_version` is our externally
visible SmartPBX compatibility marker and is `smartpbx-ai-provider-v07`. It is
our client/provider compatibility label, not a vendor-negotiated wire field, a
value pasted into the Dialog dashboard, or an extra WebSocket event.

The dashboard media fields remain `g711_ulaw` and `8000` Hz. The WebSocket
event shapes remain V07 `start`, `media`, `dtmf`, and `hangup`; the parser also
accepts the existing value-free `connected` and `stop` compatibility events.
No codec, sample rate, URL, header authentication, or media framing changes in
this compatibility update.

The V07 PDF table prints `destination_number`, but the authenticated live
Dialog `tools/list` contract used by Kavya requires the literal MCP argument
key `destination number`. Kavya sends the configured `tel:` or `sip:` URI under
that key and always sends the non-empty `tier=BYPASS` disposition. Do not copy
the PDF's snake-case spelling into the client configuration.

Kavya does not expose `hangup_call`. The PDF requires
`caller_confirmed_no_further_help="true"` only after a clear closing question
and explicit caller confirmation, and forbids disconnecting during a pending
transfer or unfinished step. Until a product-owned flow records those facts,
adding the tool would turn a model decision into an unsafe caller disconnect.
```

- [ ] **Step 3: Run static documentation checks and diff hygiene.**

Run:

```bash
cd Kavya
python3 -B -m py_compile smartpbx_protocol.py tests/test_smartpbx_protocol.py
rg -n 'smartpbx-ai-provider-v07|g711_ulaw|8000|destination number|tier.?=.?BYPASS|connected|stop|hangup_call' CLAUDE.md AGENTS.md SMARTPBX_RUNBOOK.md smartpbx_protocol.py tests/test_smartpbx_protocol.py tests/test_smartpbx_mcp.py
cmp CLAUDE.md AGENTS.md
git diff --check
```

Expected: compilation, `cmp`, and diff checks exit 0; both docs declare V07 consistently; the runbook contains neither a real token nor a real destination credential. GitHub CI executes the existing protocol/deployment tests; no local pytest claim is made.

- [ ] **Step 5: Commit the V07 evidence/docs change.**

```bash
git add Kavya/CLAUDE.md Kavya/AGENTS.md Kavya/SMARTPBX_RUNBOOK.md
git commit -m "docs(kavya): record SmartPBX V07 compatibility boundary"
```

### Task 4: Full verification and fail-closed handoff

**Files:**
- Inspect only: `Kavya/smartpbx_protocol.py`, `Kavya/smartpbx_gateway.py`, `Kavya/smartpbx_mcp.py`, `Kavya/tools.py`, `Kavya/smartpbx_handover.py`, `Kavya/SMARTPBX_RUNBOOK.md`.
- No further edits unless a focused test identifies a contract regression; any such edit requires a new red test and a separate commit.

**Interfaces:**
- Consumes: all V07 parser, gateway, MCP, handover, deployment and server test suites.
- Produces: a redacted evidence report stating CI behavioral-test results when available, local syntax/static results, unchanged codec/WSS settings, exact MCP argument spelling/tier, absent `hangup_call`, and no production mutation.

- [ ] **Step 1: Run local syntax/static gates and specify the CI behavioral gate.**

Run:

```bash
cd Kavya
python3 -B -m py_compile smartpbx_protocol.py smartpbx_gateway.py smartpbx_mcp.py smartpbx_session.py smartpbx_transport.py server.py tools.py tests/test_smartpbx_protocol.py tests/test_smartpbx_gateway.py tests/test_smartpbx_mcp.py
git diff --check
```

Expected: compilation and diff checks exit 0. After the PR, GitHub CI runs `python -m pytest Kavya/tests --timeout=300 -q` with the repository's pinned dependencies; if CI cannot install/import dependencies, report that exact blocker and do not claim behavioral success locally.

- [ ] **Step 2: Run the static contract audit.**

Run:

```bash
cd Kavya
rg -n 'smartpbx-ai-provider-v0[67]|destination number|destination_number|_TRANSFER_TIER_BYPASS|hangup_call|g711_ulaw|8000|ws/v1/smartpbx/media' smartpbx_gateway.py smartpbx_mcp.py smartpbx_protocol.py tools.py SMARTPBX_RUNBOOK.md tests/test_smartpbx_gateway.py tests/test_smartpbx_mcp.py tests/test_smartpbx_protocol.py
```

Expected: the V07 marker appears in gateway/runbook/docs/tests; `destination number` and `BYPASS` remain in MCP source/existing tests; `destination_number` appears only in the explanatory PDF distinction; `hangup_call` remains absent from the tool registry; `g711_ulaw`, `8000`, `connected`, `stop`, and `/ws/v1/smartpbx/media` remain unchanged.

- [ ] **Step 3: Verify the change boundary.**

Run:

```bash
git status --short
git diff origin/main...HEAD --stat
git diff origin/main...HEAD -- Kavya/smartpbx_protocol.py Kavya/smartpbx_gateway.py Kavya/smartpbx_mcp.py Kavya/tools.py Kavya/smartpbx_handover.py Kavya/docker-compose.yml Kavya/nginx-smartpbx.conf
```

Expected: no `Kavya/tools.py`, `Kavya/smartpbx_handover.py`, Compose, nginx, deployment, secret, or audio-codec changes; only the approved gateway/docs paths are changed. Existing unrelated worktree files remain untouched.

- [ ] **Step 4: Stop before live operations.**

Do not call Kavya, change Dialog Client Connect, push, open a PR, or deploy from this plan. The parent workflow may separately request a supervised live call after Terra review and Luna implementation; that is outside this compatibility plan.

## Self-review checklist

- Existing V07 field-name, optional hangup-reason, and parser compatibility coverage is retained without duplicate tests or parser changes.
- `g711_ulaw`/8000 and `/ws/v1/smartpbx/media` are explicitly protected.
- The externally visible authenticated status label is unambiguously `smartpbx-ai-provider-v07`; it is our compatibility marker, not a vendor-negotiated wire field or dashboard field.
- The live tools/list spelling `destination number` is preserved despite the PDF's `destination_number` inconsistency.
- `tier=BYPASS` remains required and is asserted on the actual outbound payload.
- `hangup_call` is intentionally absent, with the confirmation-owner reason recorded in tests and runbook.
- No audio, WSS, MCP endpoint, credentials, deployment, or production changes are smuggled into the compatibility work.
- No unresolved template marker or unspecified file/path remains in the task steps.

## Execution handoff

Plan complete and saved to `docs/superpowers/plans/2026-09-10-kavya-smartpbx-v07-compatibility.md`. Two execution options:

1. **Subagent-Driven (recommended)** — dispatch a fresh implementation worker per task with review between tasks.
2. **Inline Execution** — execute the tasks in this session using executing-plans with checkpoints.
