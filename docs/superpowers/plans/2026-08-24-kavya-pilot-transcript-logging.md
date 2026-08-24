# Kavya Pilot Transcript Logging Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an off-by-default, reversible SmartPBX pilot diagnostic that logs finalized guest turns and exact Kavya TTS phrases in real time.

**Architecture:** A single module-level configuration flag gates a dedicated logging helper in `server.py`. The finalized guest dispatch boundary and the guarded TTS submission boundary call the helper; SmartPBX's existing privacy-safe provider logging remains active. Compose and runbook changes make the switch explicit and reversible.

**Tech Stack:** Python 3.11, FastAPI media pipeline, pytest regression contracts in CI, Docker Compose.

## Global Constraints

- Work only on branch `Rakesh`; do not create another branch.
- Default remains private and disabled with value `0`.
- Never log interim STT text, identifiers, prompts, KB context, tools, credentials, or provider payloads.
- Do not change phone recognition, audio, TTS behavior, prompts, or Flico.
- Do not run pytest in the local Kavya sandbox; use CI for RED/GREEN behavior and local `python3 -m py_compile` only for syntax.

---

### Task 1: Specify and implement pilot phrase logging

**Files:**
- Modify: `Kavya/server.py`
- Modify: `Kavya/tests/test_smartpbx_server.py`

**Interfaces:**
- Consumes: `SMARTPBX_PILOT_TRANSCRIPT_LOGGING` from process environment.
- Produces: `_log_smartpbx_pilot_transcript(role: str, phrase: str) -> None` and dedicated `smartpbx_pilot_transcript` records.

- [ ] **Step 1: Add failing regression tests**

Add tests proving the disabled default emits no phrase text, the enabled flag
logs `role=guest` and `role=kavya`, and `%r` formatting escapes embedded newline
characters into one record.

- [ ] **Step 2: Prove RED in GitHub CI**

Push only the tests and open the Kavya PR. Expected result: the Kavya suite fails
because the pilot helper and enabled logging behavior do not exist.

- [ ] **Step 3: Implement the minimum behavior**

Parse the exact `1` flag, add the representation-safe logging helper, call it
after a finalized guest turn is accepted for dispatch, and call it inside
`_speak` after stale/transfer guards but before the provider invocation.

- [ ] **Step 4: Verify syntax and GREEN CI**

Run `python3 -m py_compile Kavya/server.py` locally. Push the implementation and
require the complete GitHub CI matrix to pass.

### Task 2: Make the diagnostic deployable and reversible

**Files:**
- Modify: `Kavya/docker-compose.yml`
- Modify: `Kavya/.env.example`
- Modify: `Kavya/SMARTPBX_RUNBOOK.md`
- Modify: `Kavya/tests/test_smartpbx_deployment.py`

**Interfaces:**
- Consumes: protected `.env.smartpbx` value.
- Produces: explicit Compose forwarding and a guarded enable/disable procedure.

- [ ] **Step 1: Add deployment contract tests**

Assert Compose forwards `${SMARTPBX_PILOT_TRANSCRIPT_LOGGING:-0}`, the example and
runbook commit only `0`, and the runbook documents local-only tailing, same-image
recreate, disable/rollback, and non-export of raw transcript logs.

- [ ] **Step 2: Implement configuration and runbook changes**

Add the one Compose allowlist entry, one documented example value, and the
bounded pilot procedure. Keep the finite privacy-safe telemetry allowlist intact;
the explicitly enabled pilot transcript record is a separate break-glass class.

- [ ] **Step 3: Verify and deploy guarded**

After CI is green and review is complete, merge the PR, build/probe the exact
main revision, deploy the pinned image, enable the flag only in the protected
SmartPBX env, recreate only `kavya-smartpbx`, verify image identity and service
isolation, and tail only `smartpbx_pilot_transcript` during the controlled call.

- [ ] **Step 4: Disable after diagnosis**

Restore `SMARTPBX_PILOT_TRANSCRIPT_LOGGING=0`, recreate the same pinned image,
and verify health, image identity, and Flico/legacy Kavya isolation.
