# Dialog v06 Protocol Conformance and Privacy Diagnostics Plan

> Do not execute until separately approved. Every task is a tests-only RED commit followed by a separate GREEN implementation or documentation commit.

Goal: repair approved Dialog v06 Hangup, DTMF, and unsupported-event conformance and replace unsafe SmartPBX diagnostics with fixed privacy-safe four-call-correlatable records.

Authority: docs/superpowers/specs/2026-08-07-kavya-smartpbx-parity-repair-design.md is controlling. The reviewed Dialog v06 extraction is /tmp/dialog-pdf-audit-20260806/SmartPBX AI Provider - Version 06.txt: start 82-109, DTMF 151-194, Hangup 196-225, and flow 234-242. Relevant code is smartpbx_protocol.py:80-230, smartpbx_gateway.py:129-357, smartpbx_session.py:42-150, server.py:2669-2680/3493-3581, and SMARTPBX_RUNBOOK.md:200-278.

## Scope and invariants

- Preserve auth before accept, start-account binding, g711_ulaw/8000 admission, current bounds, public close codes/reasons, wire frames, and transfer-disabled status.
- Do not change voice/TTS profile, STT/LLM/RAG/booking/post-call/MCP/handover/transfer behavior, Docker/configuration/routing, Flico, Twilio, or deployment.
- Extra mapping fields remain ignored; only consumed fields are validated.
- Never log raw or derived Dialog/caller/account/token/audio/transcript/voice values, event names, payloads, exception text/stacks, session_id, or call_fingerprint. Remove UUID/fingerprint records and unknown_events_total.
- Use /home/dev/incoming/taskforce-ai/.venv/bin/python for tests/compile and python3 only for standard-library static checks. No deployment, secret/routing change, live call, merge, or main mutation.

## Protocol contract

| Existing behavior | Required behavior |
| --- | --- |
| Hangup requires accountId/reason. | HangupEvent has required bounded call_id/other_leg_call_id and optional bounded reason. It neither reads nor validates accountId. |
| Hangup context compares three values. | Compare only documented callId and otherLegCallId with start-bound CallContext. |
| DTMF has digit/duration and only 0-9, star, hash. | DtmfEvent has required bounded call_id/other_leg_call_id, digit 0-9/star/hash/A-D, optional bounded duration; validate both legs. |
| UnknownEvent stores a name and gateway counts it. | Nonblank unknown becomes value-free UnsupportedEvent(), fixed rejection pre-start/active; remove the counter from snapshot/status/tests. |

Start retains three-field matching. Blank/non-string event remains invalid_message. Retain strict media/byte bounds and connected/stop compatibility.

## Typed diagnostic contract

Add Kavya/smartpbx_diagnostics.py, a neutral module with no server/gateway/context/payload imports.

    DiagnosticStage:
      SCHEMA_ADMISSION, CONTEXT_VALIDATION, SESSION_START,
      AUDIO_INGESTION, TTS, TERMINAL_CLEANUP
    DiagnosticOutcome:
      REJECTED, OBSERVED, COMPLETED, DISCONNECTED, CANCELLED, FAILED, DEGRADED
    DiagnosticFailureClass:
      NONE, DISABLED, AUTHENTICATION, CAPACITY, INVALID_MESSAGE, MESSAGE_TOO_BIG,
      UNSUPPORTED_MEDIA_FORMAT, INVALID_MEDIA, AUDIO_TOO_BIG, INVALID_DTMF,
      UNSUPPORTED_EVENT, START_REQUIRED, ACCOUNT_MISMATCH, CONTEXT_MISMATCH,
      DUPLICATE_START, CONNECTED_AFTER_START, START_TIMEOUT, IDLE_TIMEOUT,
      SESSION_FACTORY, SESSION_START, AUDIO_INGESTION, TTS_MISSING_CONFIGURATION,
      TTS_HTTP_STATUS, TTS_TIMEOUT, TTS_EXCEPTION, TRANSPORT_DISCONNECT,
      CANCELLED, SESSION_CLEANUP, TRANSPORT_CLEANUP, LEASE_CLEANUP,
      WEBSOCKET_CLOSE, INTERNAL_ERROR

    class SmartPBXDiagnosticSink(Protocol):
        def __call__(self, stage: DiagnosticStage,
                     outcome: DiagnosticOutcome,
                     failure_class: DiagnosticFailureClass, /) -> None: ...

At SmartPBXGateway.handle entry make correlation_id equal to "spx-" plus secrets.token_hex(16). It is fixed spx-[0-9a-f]{32}, random 128-bit opaque data independent of Dialog/customer/account/token/audio/transcript/voice values, never external, and absent from status/session/transport/post-call data.

Gateway owns the synchronous typed closure: create before admission, pass only to the three-argument SessionFactory for that handle, retain through shielded cleanup, disable after final cleanup record, and make late calls no-ops. The sole emitter accepts enums only and logs exactly event=smartpbx_protocol_diagnostic, correlation_id, stage, outcome, failure_class, active_sessions, duration_ms. It accepts no raw string/event/context/payload/identifier/exception.

## Exhaustive decision table

Parameterized tests execute every row and every enum member.

| Path | stage/outcome/failure |
| --- | --- |
| disabled, auth mismatch, capacity | SCHEMA_ADMISSION/REJECTED/DISABLED, AUTHENTICATION, CAPACITY |
| invalid JSON, size, media format, media payload, audio size, DTMF | SCHEMA_ADMISSION/REJECTED/INVALID_MESSAGE, MESSAGE_TOO_BIG, UNSUPPORTED_MEDIA_FORMAT, INVALID_MEDIA, AUDIO_TOO_BIG, INVALID_DTMF |
| unknown event; known non-start pre-start; start timeout | SCHEMA_ADMISSION/REJECTED/UNSUPPORTED_EVENT, START_REQUIRED, START_TIMEOUT |
| start account mismatch; mismatched legs; duplicate start; connected after start | CONTEXT_VALIDATION/REJECTED/ACCOUNT_MISMATCH, CONTEXT_MISMATCH, DUPLICATE_START, CONNECTED_AFTER_START |
| matching DTMF | CONTEXT_VALIDATION/OBSERVED/NONE |
| idle timeout; feed_audio failure | AUDIO_INGESTION/REJECTED/IDLE_TIMEOUT; AUDIO_INGESTION/FAILED/AUDIO_INGESTION |
| factory, session.start, start success | SESSION_START/FAILED/SESSION_FACTORY; SESSION_START/FAILED/SESSION_START; SESSION_START/COMPLETED/NONE |
| TTS missing config, HTTP status, timeout, exception | TTS/FAILED/TTS_MISSING_CONFIGURATION, TTS_HTTP_STATUS, TTS_TIMEOUT, TTS_EXCEPTION |
| valid Hangup/stop/terminal future; disconnect; cancellation | TERMINAL_CLEANUP/COMPLETED/NONE; TERMINAL_CLEANUP/DISCONNECTED/TRANSPORT_DISCONNECT; TERMINAL_CLEANUP/CANCELLED/CANCELLED |
| finish, transport, lease, close fault; unknown exception | TERMINAL_CLEANUP/DEGRADED/SESSION_CLEANUP, TRANSPORT_CLEANUP, LEASE_CLEANUP, WEBSOCKET_CLOSE; TERMINAL_CLEANUP/FAILED/INTERNAL_ERROR |

Map ProtocolViolation classes exhaustively; missing mapping is INTERNAL_ERROR, never arbitrary data. First valid terminal wins: break before another receive, leave queued later frames unread, and finish/close/release once. DTMF is observed after validation; no digit/duration is retained.

## Task 1: parser RED then GREEN

RED (tests only): change only Kavya/tests/test_smartpbx_protocol.py. Add no-account/no-reason and optional-reason Hangup tests; ignored accountId sentinel; leg bounds/mismatch; DTMF legs/duration/all A-D values; value-free UnsupportedEvent; retain strict media/start/extras. Run focused protocol pytest and record expected failures in commit body: old Hangup fields, old DTMF, retained unknown name. Publish expected-head/non-force atomic commit.

GREEN (production only): change only Kavya/smartpbx_protocol.py. Implement documented Hangup fields only, DtmfEvent/parser legs/A-D, UnsupportedEvent, type-specific context validation. Run focused GREEN and compile.

## Task 2: gateway, cleanup, and feasible TTS attribution

RED (tests only): change only test_smartpbx_gateway.py/test_smartpbx_server.py. Test valid v06 Hangup once/one normal close/released_total one/later media unread; DTMF observe/mismatch; unknown rejection/no counter; every table tuple/exact fields/four unique fixed-format correlations/no sentinels. Fault-inject finish, transport close, lease release: each at most once, later cleanup runs, released_total one when release works, one close where applicable, fixed diagnostic. Cancellation shields cleanup then reraises.

At the real server catch seam use a fake SmartPBXDiagnosticSink and cover all four SmartPBX English paths: missing configuration, non-success HTTP response, timeout, generic exception. Assert finite tuple only, with no text/status/provider/body/voice/API/exception data; non-SmartPBX behavior stays unchanged. Assert session installs sink before welcome speak and server factory forwards it. Run focused RED and record intended failures: missing module/sink, UUID/fingerprint/counter, obsolete DTMF, absent cleanup tests.

GREEN (production only): add diagnostics module. Gateway removes hashlib/uuid/fingerprint/dynamic logs/counter, creates emitter/correlation, uses table, rejects UnsupportedEvent, validates/observes DTMF, passes sink to three-argument factory, exits first terminal. Cleanup takes emitter only and retains ordered finish then transport then lease continuation.

Change server._new_smartpbx_session and KavyaSmartPBXSession to forward/install the typed sink before welcome speech. The four existing SmartPBX TTS branches call finite sink values only, do not propagate caught errors, and preserve non-SmartPBX behavior/logging. Run protocol/gateway/server GREEN and compile changed modules.

## Task 3: runbook RED then GREEN

RED (tests only): change only test_smartpbx_deployment.py. Require seven fixed fields; local-random/opaque/not-Dialog-derived/not-external correlation wording; forbid fingerprint/session_id/unknown counter/raw or derived identifier/event/payload/exception wording. Retain transfer-disabled/four-call/Docker-import/withdraw-drain-stop checks. Record current fingerprint failure.

GREEN (docs only): change only SMARTPBX_RUNBOOK.md Cutover gates: seven fields, opaque local correlation for supervised four-call diagnostics, forbidden data, no fingerprint/counter wording; preserve transfer-disabled/fallback/Docker/capacity/rollback.

## Atomic publication and gates

For each of six commits: run status/diff check; RED tests only and GREEN production/docs only; record actual intended RED failure in commit body; read current remote Rakesh head; publish non-force atomic expectedHeadOid; fetch/fast-forward; rerun focused checks. Unexpected RED pass stops work.

Finally run focused protocol/gateway/server/deployment pytest with the verified interpreter, compile changed modules, existing transfer-disabled Compose assertion with python3, existing reviewed Docker import command, diff check, and gitleaks. Accept PR #209 CI only if cached PR head and every required check SHA equal final GREEN SHA; stale earlier green checks do not count. Do not mutate PR merely to refresh a docs-plan cache.

A later approved rollback remains: withdraw Dialog route, wait active_sessions zero, then stop kavya-smartpbx. Do not touch Twilio or Flico.

## Detailed execution addendum

### Collection-safe RED rules

Task 1 RED must import smartpbx_protocol as a module and use getattr/type(event).__name__ inside assertions; it must not import UnsupportedEvent at module scope. Cover Hangup missing/blank/non-string/over-limit legs, optional reason absent/empty/non-string/exact-boundary/over-boundary, ignored extra accountId, every DTMF digit 0-9 star hash A-D, duration absent/0/10000/bool/negative/fractional/10001, leg mismatch, extras, and blank/non-string event.

Task 2 RED adds a dedicated tests/test_smartpbx_diagnostics.py. Imports of the absent diagnostics module occur inside test functions, caught and asserted, so missing code is an assertion failure rather than collection failure. It asserts exact enum members, typed callback signature, seven field record/no extras, correlation format/uniqueness/no sentinels, and Docker explicit COPY/import allowlist. GREEN adds smartpbx_diagnostics.py and adds it to Kavya/Dockerfile line 63 in the same production commit.

Task 3 RED changes all factory fakes and direct session constructions. Inventory with rg before editing: tests/test_smartpbx_gateway.py, tests/test_smartpbx_gateway_transfer_pending.py, tests/test_smartpbx_real_handover_lifecycle.py, and every direct KavyaSmartPBXSession call. The coherent GREEN changes smartpbx_gateway.py:143 SessionFactory, gateway handle:157-245, cleanup:288-303, smartpbx_session.py constructor:20-50/start:88-108, and server.py _new_smartpbx_session:4953-4956 together. The three-argument factory is context, transport, typed sink.

Task 3 fault tests inject session.finish, transport.close, lease.release, and websocket.close independently. For each applicable path assert every operation is called exactly once, a later cleanup operation runs after earlier failure, released_total equals one when release succeeds, only one WebSocket close is attempted, and the exact terminal-cleanup tuple is captured. A cancelled handle shields cleanup and reraises cancellation. A valid Hangup or terminal future exits receive loop before queued later media is read.

Task 4 RED separately covers all five English SmartPBX sites in server.py: missing API key 3519-3521, profile ValueError 3522-3527, non-200 3548-3555, timeout 3570-3575, generic exception 3576-3581. It asserts finite TTS tuples, zero text/status/provider/body/voice/API/exception leakage, and for each changed conditional verifies no-sink and non-SmartPBX logging/control flow are unchanged. GREEN only invokes the typed sink; it never changes returns, speaking state, exception propagation, or voice behavior.

Task 5 RED allows literal fixed event=smartpbx_protocol_diagnostic while forbidding raw or dynamic event names, raw payload contents, exception text/stacks, fingerprint, session_id, and unknown counter. GREEN rewrites only runbook Cutover gates and retains transfer-disabled wording plus withdrawal, active_sessions drain, then stop rollback order.

### Exact per-task commit mechanics

Before every RED and GREEN: run git status --short and git diff --check; verify only the stated tests-only or production/docs-only paths. Run the task command from cd Kavya with PYTHONDONTWRITEBYTECODE=1 PYTEST_ADDOPTS='-p no:cacheprovider' and /home/dev/incoming/taskforce-ai/.venv/bin/python -m pytest. Record exact expected RED output in the RED commit body. An unexpected RED pass stops the task.

Immediately before each remote publication run git ls-remote origin refs/heads/Rakesh. Use that SHA as expectedHeadOid in createCommitOnBranch, omit force, and include only that commit's files. After success run git fetch origin Rakesh and git merge --ff-only origin/Rakesh, then rerun focused verification. There are ten commits total: RED and GREEN for each of five tasks.

### Final commands

cd Kavya
PYTHONDONTWRITEBYTECODE=1 PYTEST_ADDOPTS='-p no:cacheprovider' /home/dev/incoming/taskforce-ai/.venv/bin/python -m pytest tests -q
/home/dev/incoming/taskforce-ai/.venv/bin/python -B -m py_compile smartpbx_protocol.py smartpbx_diagnostics.py smartpbx_gateway.py smartpbx_session.py server.py
/home/dev/incoming/taskforce-ai/.venv/bin/python -c 'from pathlib import Path; import yaml; c=yaml.safe_load(Path("docker-compose.yml").read_text()); assert "kavya-smartpbx" in c["services"]; print("compose=ok")'
docker build -f Dockerfile -t kavya-smartpbx-plan-import .
docker run --rm kavya-smartpbx-plan-import /home/dev/incoming/taskforce-ai/.venv/bin/python -c 'import smartpbx_diagnostics; print("smartpbx_diagnostics_import=ok")'
/home/dev/.cache/pre-commit/repoietpp3fj/golangenv-default/bin/gitleaks detect --source .. --no-git --redact
git diff --check

Use gh pr view 209 --repo taskforce-ai-dev/full-voice-agent --json headRefOid,statusCheckRollup and gh run list --repo taskforce-ai-dev/full-voice-agent --branch Rakesh --limit 10 --json headSha,status,conclusion,url. Accept only if cached PR head and every required check SHA equals final GREEN SHA.