# Kavya SmartPBX Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `subagent-driven-development` or `executing-plans` task by task.  Steps use checkbox syntax for tracking.

**Goal:** Make SmartPBX turn latency diagnosable without PII, repair valid mismatched DTMF, gate any interim de-duplication on provider evidence, expose safe STT state, improve only measured caller rhythm, and stop generic CI from selecting the intentionally rejected Kavya image deploy route.

**Architecture:** `MediaStreamSession` owns a SmartPBX-only recorder with a fresh opaque random `turn_id` per dispatched turn; STT, KB/LLM/tool/TTS, and the paced Dialog transport contribute fixed numeric boundaries using that key. `KavyaSmartPBXSession` owns a separate opaque `session_trace_id` and terminal aggregate. Protocol and deployment changes remain narrow: parsed DTMF forwarding is separated from diagnostic context matching, and the generic deploy dispatcher excludes Kavya while the existing immutable-image probe/publisher/runbook flow remains authoritative.

**Tech Stack:** Python 3.11, asyncio, FastAPI pipeline, Google Cloud Speech streaming fixtures, Dialog SmartPBX transport, Docker Compose, GitHub Actions YAML, targeted GitHub CI.

## Global Constraints

- Work from `Rakesh`; pre-change comparison tag is `kavya-smartpbx-pre-stt-latency-20260813` at `9273721`.
- Generate `turn_id` and `session_trace_id` locally at random; never derive either from Dialog/call/caller/socket data. Every concurrent-call stage/turn-summary event uses `turn_id`; `turn_seq` may remain internal only.
- Never log transcript text, identifiers for caller/call/stream, phone numbers, provider payloads, tool arguments/results, headers, secrets, environment dumps, or exception messages.
- All elapsed values use monotonic time, integer milliseconds, and clamp to `0..600000`; transport values are wire proxies, never playback acknowledgement.
- Preserve capture buffering, transfer announcement/delivery behavior, queue pacing/backpressure, echo rejection, four-call admission, and Twilio paths.
- Do not change keypad or transfer timeouts, reorder side-effecting tools, mutate production configuration, or hardcode a production digit-class boost.
- Local pytest may hang. Write focused tests first, then push test-only and implementation commits only to the existing `Rakesh` branch for targeted GitHub CI red/green evidence. This authorizes neither other branches, merge, deployment, production mutation, nor a push outside that CI purpose. Call local `python3 -m py_compile` syntax-only.
- Do one consolidated final review after all tasks.  Do not perform a review gate after each task.

## File Map

- Modify: `Kavya/server.py` — SmartPBX turn recorder, endpoint/provider-shape telemetry, output profile, initial filler, and digit-class state event.
- Modify: `Kavya/smartpbx_transport.py` — generation-scoped first-send/drain/clear/drop timing callbacks with no payload logging.
- Modify: `Kavya/smartpbx_session.py` — locally random `session_trace_id`, generation-to-turn binding handoff, and one post-cleanup aggregate `session_summary`.
- Modify: `Kavya/smartpbx_gateway.py` — forward valid parsed DTMF after an observed mismatch.
- Modify: `Kavya/docker-compose.yml`, `Kavya/.env.example`, and `Kavya/SMARTPBX_RUNBOOK.md` — explicit non-secret knobs, rendered-config checks, and bounded retention procedure.
- Modify: `.github/workflows/deploy-on-push.yml` and `.github/workflows/pr-deploy-impact.yml` — exclude Kavya from generic image deployment and report that truthfully.
- Modify/add focused tests in `Kavya/tests/test_smartpbx_gateway.py`, `Kavya/tests/test_bug_a_smartpbx_gateway_fail_open.py`, `Kavya/tests/test_stt_endpointing.py`, `Kavya/tests/test_bug_b_stt_digit_sequence_context.py`, `Kavya/tests/test_smartpbx_runtime_seam.py`, `Kavya/tests/test_smartpbx_server.py`, and `Kavya/tests/test_smartpbx_deployment.py`.

## Phase 0: Documentation and baseline

### Task 1: Freeze evidence and contracts

**Read:** `Kavya/AGENTS.md` service-mode/retention sections; `Kavya/SMARTPBX_RUNBOOK.md`; `.github/workflows/build-kavya-image.yml`; `.github/workflows/probe-kavya-image.yml`; the three 2026-08-13 forensic/audit reports.

- [ ] Verify `git rev-parse kavya-smartpbx-pre-stt-latency-20260813` is `9273721c040b2ff8362fc9d5c1bf059ca7478d73` and record current `HEAD`.
- [ ] Record that current measured guest-to-agent timing is a response-preparation proxy, not an audio-heard measurement; use that wording in test/CI summaries.
- [ ] Create a source table in the implementation PR description mapping `MediaStreamSession._flush_transcript`, LLM runners, `_invoke_speak`/TTS, `SmartPBXMediaTransport.send_mark`, and `_send_queued_audio` to the proposed boundaries.
- [ ] Assert before edits that `kavya-smartpbx` uses `json-file`, `max-size: "10m"`, and `max-file: "3"`; retain those exact values.

**Anti-pattern guards:** Do not parse or export Docker logs; do not reinterpret the 48-hour aggregate as a per-caller trace; do not replace the protected probe/publisher path with generic deployment.

## Phase 1: Test the privacy and protocol contracts first

### Task 2: Add telemetry contract tests

**Files:**
- Modify: `Kavya/tests/test_smartpbx_runtime_seam.py`
- Modify: `Kavya/tests/test_stt_endpointing.py`

**Produces:** fixtures that capture structured log arguments and transport callbacks without real WebSockets, provider traffic, transcripts, or IDs.

- [ ] Add a test that starts a SmartPBX turn, captures a random opaque `turn_id`, marks endpoint/KB/LLM/TTS/transport, and asserts every correlation event plus exactly one `turn_summary` uses that same ID, fixed enums, integer bounded millisecond fields, counts, and no negative duration. Assert `turn_seq` is absent from log fields.
- [ ] Add parameterized terminal-outcome tests for normal completion, tool failure, TTS failure, interruption/barge-in, and transfer-pending cancellation; every started turn must yield exactly one summary.
- [ ] Add a `KavyaSmartPBXSession._finish_once()` test proving one safe `session_summary` after cleanup with only outcome, aggregate counts, and bounded duration; it must not contain a Dialog context ID, caller number, or transcript.
- [ ] Add a forbidden-key/value test: captured records must not contain `transcript`, `text`, `call_id`, `callSid`, `caller`, `phone`, `payload`, `authorization`, `token`, `secret`, `headers`, tool input, or exception message fields.
- [ ] Add a transport-generation test: first send is recorded once per live generation; `clear_audio()` prevents an old-generation send/drain mark; dropped/cleared frames do not become a false queue-drained completion.
- [ ] Commit only these failing tests as `test(kavya): define SmartPBX privacy-safe turn telemetry` and run their targeted GitHub CI command.  Expected initial result: red because the recorder/events do not exist.

### Task 3: Add DTMF and provider-shape regressions

**Files:**
- Modify: `Kavya/tests/test_smartpbx_gateway.py`
- Modify: `Kavya/tests/test_bug_a_smartpbx_gateway_fail_open.py`
- Modify: `Kavya/tests/test_stt_endpointing.py`

- [ ] Add a gateway fixture with a fully parsed `DtmfEvent` containing a valid digit and one mismatched leg identifier.  Assert `feed_dtmf` receives that exact digit and diagnostic sink records `CONTEXT_VALIDATION/OBSERVED/CONTEXT_MISMATCH`.
- [ ] Add malformed-frame and invalid-digit controls asserting parser/protocol failure and no `feed_dtmf` call.
- [ ] Add separate interim fixtures for: segment-only interim after a committed final; exact cumulative interim beginning with the committed text and one separator; a near match differing only in case/space/punctuation.  Assert no rule may alter the near match.
- [ ] Add a safe telemetry assertion that records only shape classification and character counts.
- [ ] Commit only the failing regressions as `test(kavya): cover DTMF mismatch and interim shapes` and run their targeted GitHub CI command.  Expected initial result: DTMF forwarding regression red; de-dup test remains pending provider evidence.

## Phase 2: Implement telemetry and protocol repair

### Task 4: Implement the SmartPBX-only turn recorder

**Files:**
- Modify: `Kavya/server.py`
- Modify: `Kavya/smartpbx_transport.py`
- Modify: `Kavya/smartpbx_session.py`

**Interfaces:**

```python
class SmartPBXTurnTelemetry:
    def start_turn(self, endpoint_source: str) -> str: ...  # opaque turn_id
    def mark(self, turn_id: str, stage: str) -> None: ...
    def mark_once(self, turn_id: str, stage: str) -> None: ...
    def finish(self, turn_id: str, outcome: str, **counts: int) -> None: ...

class SmartPBXMediaTransport:
    def bind_turn(self, generation: int, turn_id: str) -> None: ...
```

- [ ] Construct this object only for direct SmartPBX English sessions; generate each `turn_id` with local cryptographic randomness, use `time.monotonic_ns()`, and convert/clamp at log emission.
- [ ] Start a turn at guarded `_flush_transcript` dispatch, record `final`, `interim`, or `capture` endpoint source, and ensure every early return before LLM start emits the appropriate terminal outcome.
- [ ] Mark KB start/finish around the existing retrieval await; mark LLM request, first produced model token/tool delta, and completed round in all SmartPBX provider runners without changing their provider/history contracts.
- [ ] Mark tool batch start/result around the existing execution path; record only fixed tool name enum/category and elapsed time, never arguments/result.
- [ ] Mark TTS request and first non-empty generated audio chunk at the existing SmartPBX TTS boundary; keep sentence delivery bookkeeping untouched.
- [ ] Before current-turn audio queues, call `bind_turn(live_generation, turn_id)`; transport stores only `generation -> turn_id` and calls back with `(turn_id, generation, stage, monotonic_ns, dropped_frames)` for first successful `send_text`, `send_mark()` drain, `clear_audio`, and bounded overflow. It must receive neither `CallContext` nor transcript.
- [ ] In `KavyaSmartPBXSession._start_once()`, generate the separate local opaque `session_trace_id`; in `_finish_once()`, after pipeline/STT/transport cleanup, emit exactly one aggregate `session_summary` carrying that trace ID and only the specified aggregate numeric/outcome fields. Emit exactly one turn summary in the turn-owner `finally`. Do not alter the seven-field `smartpbx_protocol_diagnostic` contract.
- [ ] Run local `python3 -m py_compile Kavya/server.py Kavya/smartpbx_transport.py Kavya/smartpbx_session.py` and label the result syntax-only.
- [ ] Run the Task 2 targeted GitHub CI command; expected result: green, proving the new contract in CI.

### Task 5: Repair valid mismatched DTMF forwarding

**Files:**
- Modify: `Kavya/smartpbx_gateway.py`

- [ ] Keep `validate_event_context(event, context)` and its diagnostics exactly as today.
- [ ] After either successful validation or caught `failure_class == "context_mismatch"`, look up `feed_dtmf` and await it with the already parsed `event.digit`.
- [ ] Preserve `raise` for every other `ProtocolViolation`; do not catch parser errors or broaden accepted protocol input.
- [ ] Run local `python3 -m py_compile Kavya/smartpbx_gateway.py` syntax-only.
- [ ] Run the Task 3 targeted GitHub CI command; expected result: the mismatched-leg valid-digit test is green and malformed controls remain green.

## Phase 3: Gate STT semantics and make configuration observable

### Task 6: Decide interim de-duplication from provider evidence

**Files:**
- Modify: `Kavya/server.py`
- Modify: `Kavya/tests/test_stt_endpointing.py`

- [ ] Read the Google streaming recognition-result documentation/version used by `requirements-prod.txt` and preserve a link/quoted semantic statement in the PR description.
- [ ] If it confirms that the relevant interim is cumulative after a final, implement only this exact branch: when `pending.startswith(committed + separator)`, remove that one leading byte-for-byte prefix; otherwise preserve the pending text untouched.
- [ ] If it does not confirm that contract, do not change concatenation.  Keep the fixtures and emit only safe `segment|exact_cumulative|unknown` classification plus lengths.
- [ ] In both outcomes, run the targeted CI test from Task 3.  The exact-cumulative fixture must pass, and the near-match fixture must prove no fuzzy de-duplication.

### Task 7: Expose digit-class state and document the later rollout

**Files:**
- Modify: `Kavya/server.py`
- Modify: `Kavya/docker-compose.yml`
- Modify: `Kavya/.env.example`
- Modify: `Kavya/SMARTPBX_RUNBOOK.md`
- Modify: `Kavya/tests/test_bug_b_stt_digit_sequence_context.py`
- Modify: `Kavya/tests/test_smartpbx_deployment.py`

- [ ] At English Google STT configuration construction, emit one SmartPBX-safe event with the clamped boost and boolean `digit_class_enabled`; retain the existing `0` disables only the OOV digit class behavior.
- [ ] Add `STT_DIGIT_CLASS_BOOST: "${STT_DIGIT_CLASS_BOOST:-}"` to the SmartPBX explicit Compose allowlist and an empty documented key to `.env.example`. The missing/blank value reaches `_parse_clamped_float` as blank and retains its `4.0` source default—never add an `env_file` or a committed production value.
- [ ] Extend tests for enabled/disabled state, clamp behavior, English-only OOV context, and rendered Compose allowlist presence.
- [ ] Add a runbook subsection: later reviewed nonzero selection, protected `.env.smartpbx` update, `docker compose --env-file .env.smartpbx config` allowlist inspection, pinned recreate, and safe-state-event verification.  Explicitly prohibit printing the file, its value, or any secret.
- [ ] Run `python3 -m py_compile Kavya/server.py` syntax-only and the Task 7 targeted GitHub CI command.  Do not edit live configuration.

## Phase 4: Evidence-supported caller rhythm

### Task 8: Add the SmartPBX concise output profile

**Files:**
- Modify: `Kavya/server.py`
- Modify: focused provider/SmartPBX tests under `Kavya/tests/`

- [ ] Add `SMARTPBX_MAX_TOKENS`, default `120`, clamped `[40, 200]`; select it only in direct SmartPBX English provider calls.  Existing `MAX_TOKENS=300` remains the Twilio/default value.
- [ ] Add SmartPBX-only prompt wording: answer first in one or two concise sentences and ask no more than one next question.  Do not alter booking policy, capture tool wording, transfer language, or non-SmartPBX prompt blocks.
- [ ] Test every provider call receives `120` by default in SmartPBX mode and `300` outside it; test out-of-range values clamp; test the policy text appears only in SmartPBX prompt construction.
- [ ] Run the focused GitHub CI test and `python3 -m py_compile Kavya/server.py` syntax-only.

### Task 9: Add one cancellable initial-silence filler

**Files:**
- Modify: `Kavya/server.py`
- Modify: `Kavya/tests/test_smartpbx_runtime_seam.py`

- [ ] Add `SMARTPBX_INITIAL_FILLER_DELAY_SECONDS`, default `2.5`, clamped `[0.5, 5.0]`; create at most one fixed neutral filler timer for the first SmartPBX LLM round. The model request starts immediately and is never gated by filler send/drain.
- [ ] Cancel the timer on the first content delta or first tool delta, barge-in, transfer pending, session finish, or generation change; await/suppress cancellation without logging an exception message. Do not infer a future tool before its delta arrives.
- [ ] If the tool delta wins, preserve existing capture/keypad/transfer specialized-filler suppression. If the timer wins and the neutral filler has spoken, suppress the later specialized filler; when a tool delta arrives while it is still speaking, cancel/clear it before the existing tool execution sequence continues. Do not retroactively claim that the neutral filler could not run for a future capture/keypad/transfer tool. Later tool rounds and an already-speaking turn never schedule this timer.
- [ ] Add deterministic race tests: timer wins before tool delta -> exactly one neutral filler and no later specialized filler; tool delta wins before timer -> no neutral filler and existing capture/keypad/transfer suppression applies; barge-in or generation change before timer -> no filler; delayed content still delivers once.
- [ ] Add a regression that verifies `create_booking` and transfer retain their existing sequential ordering after any active neutral filler is cancelled/cleared. Do not add concurrent filler/tool execution optimization in this cycle.
- [ ] Run the focused GitHub CI test and syntax-only compile.

## Phase 5: CI routing and bounded operations

### Task 10: Exclude Kavya from generic image deployment

**Files:**
- Modify: `.github/workflows/deploy-on-push.yml`
- Modify: `.github/workflows/pr-deploy-impact.yml`
- Modify: `Kavya/tests/test_smartpbx_deployment.py`

- [ ] Trace the path classifier that presently detects the GHCR-backed Kavya Compose file and selects `mode=image`.
- [ ] Add an explicit Kavya exclusion before the reusable `deploy.yml` call and mirror it in PR deploy-impact reporting; write a fixed job summary stating that SmartPBX deployment uses the reviewed probe → publisher → runbook path.
- [ ] Keep non-Kavya routing unchanged.  Do not weaken `deploy.yml`'s rejection of `agent=kavya && mode=image`, and do not change `build-kavya-image.yml` or `probe-kavya-image.yml` privileges, triggers, or OCI revision checks.
- [ ] Add static YAML tests proving a `Kavya/**` change cannot call generic deploy with image mode, PR impact reporting also calls it excluded, a non-Kavya image-backed agent preserves prior routing, and the publisher/probe workflow files still exist with their guarded trigger/permissions contract.
- [ ] Run the targeted GitHub CI test.  Expected result: generic push no longer produces the intentional Kavya red deployment.

### Task 11: Document retention and operational verification

**Files:**
- Modify: `Kavya/SMARTPBX_RUNBOOK.md`
- Modify: `Kavya/tests/test_smartpbx_deployment.py`

- [ ] Preserve `json-file`, `max-size: "10m"`, `max-file: "3"` (30 MB maximum) in Compose; do not add a raw-log export or central payload collector.
- [ ] Document a read-only verification: render the protected Compose configuration, verify the three logging fields and the absence of `env_file` on `kavya-smartpbx`, then inspect only fixed event names/field names and numeric aggregates.
- [ ] Document that investigation exports aggregates only and that turn summaries are wire-latency proxies.
- [ ] Add static documentation/config assertions for the retention values and forbidden raw/secret terms in the event contract.
- [ ] Run the focused GitHub CI test.

## Phase 6: Consolidated verification and promotion handoff

### Task 12: One final review and CI evidence package

**Files:** all modified files above; no new feature scope.

- [ ] Compare the final diff with `kavya-smartpbx-pre-stt-latency-20260813`; confirm capture buffering, transfer announcements, `send_mark()` delivery barrier, paced contiguous-prefix transport, echo suppression, four-call cap, and Twilio route code are unchanged except for telemetry seams explicitly required above.
- [ ] Run `git diff --check`, static workflow/config tests, and `python3 -m py_compile` for every changed Python module.  Report each as static/syntax evidence only.
- [ ] Run the selected GitHub CI workflow(s) for the focused tests and retain links/results for red→green proof.  Do not claim local pytest passed.
- [ ] Inspect every new structured event literal and confirm it has only the fixed allowed keys; inspect logging paths for exception messages/payload leakage.
- [ ] Perform this single consolidated review after all tasks; do not add per-task reviewer gates.
- [ ] Create a small conventional commit series during implementation, then a final docs/runbook commit if needed. Push only test-only and implementation commits to existing `Rakesh` when needed for the authorized GitHub CI red/green evidence; do not create another branch, merge, deploy, or push for any other purpose.
- [ ] Hand off deployment as a separate reviewed action: choose the nonzero boost only after evidence, follow probe/publisher/runbook, verify pinned OCI revision, then verify safe state/aggregate events without reading or printing secrets.
