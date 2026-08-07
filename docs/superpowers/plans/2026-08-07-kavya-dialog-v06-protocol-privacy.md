# Dialog v06 Protocol, Diagnostics, and Privacy Repair Plan

> Do not execute until separately approved. This is one five-task, ten-commit sequence; every RED is tests-only and every GREEN is production/docs-only.

## Scope and evidence

Authority: docs/superpowers/specs/2026-08-07-kavya-smartpbx-parity-repair-design.md and /tmp/dialog-pdf-audit-20260806/SmartPBX AI Provider - Version 06.txt (start 82-109, DTMF 151-194, hangup 196-225, flow 234-242). Preserve auth-before-accept, start account binding, g711_ulaw/8000, bounds, close reasons, wire format, transfer-disabled status, Twilio and Flico. Freeze Docker runtime behavior; only Task 2 may add the new module to Kavya/Dockerfile explicit COPY allowlist. Do not change voice/STT/LLM/RAG/booking/post-call/MCP/transfer/config/routing/deploy.

Diagnostics never contain identifiers, credentials, audio, transcript, voice data, raw/dynamic event names, payloads, exception text/stacks, session_id, or call_fingerprint. The required fixed event field is event=smartpbx_protocol_diagnostic. A locally random spx- plus secrets.token_hex(16) correlation_id is allowed, never external or Dialog-derived.

## Decision contract

Add neutral smartpbx_diagnostics.py: DiagnosticStage exactly SCHEMA_ADMISSION, CONTEXT_VALIDATION, SESSION_START, AUDIO_INGESTION, TTS, TERMINAL_CLEANUP; DiagnosticOutcome exactly REJECTED, OBSERVED, COMPLETED, DISCONNECTED, CANCELLED, FAILED, DEGRADED; DiagnosticFailureClass exactly NONE, DISABLED, AUTHENTICATION, CAPACITY, INVALID_MESSAGE, MESSAGE_TOO_BIG, UNSUPPORTED_MEDIA_FORMAT, INVALID_MEDIA, AUDIO_TOO_BIG, INVALID_DTMF, UNSUPPORTED_EVENT, START_REQUIRED, ACCOUNT_MISMATCH, CONTEXT_MISMATCH, DUPLICATE_START, CONNECTED_AFTER_START, START_TIMEOUT, IDLE_TIMEOUT, SESSION_FACTORY, SESSION_START, AUDIO_INGESTION, TTS_MISSING_API_KEY, TTS_PROFILE_FAILURE, TTS_HTTP_STATUS, TTS_TIMEOUT, TTS_EXCEPTION, TRANSPORT_DISCONNECT, CANCELLED, SESSION_CLEANUP, TRANSPORT_CLEANUP, LEASE_CLEANUP, WEBSOCKET_CLOSE, INTERNAL_ERROR. SmartPBXDiagnosticSink accepts only (stage, outcome, failure_class) enum values.

Exact seven fields: event, correlation_id, stage, outcome, failure_class, active_sessions, duration_ms. Table: disabled/auth/capacity are SCHEMA_ADMISSION/REJECTED; malformed/size/format/media/audio/invalid DTMF/unknown/prestart/start timeout are SCHEMA_ADMISSION/REJECTED; account/legs/duplicate/connected are CONTEXT_VALIDATION/REJECTED; valid DTMF is CONTEXT_VALIDATION/OBSERVED/NONE; idle is AUDIO_INGESTION/FAILED/IDLE_TIMEOUT and feed error AUDIO_INGESTION/FAILED/AUDIO_INGESTION; factory/start/success are SESSION_START/FAILED or COMPLETED; five TTS failures are TTS/FAILED; normal terminal/disconnect/cancel/cleanup faults/internal are TERMINAL_CLEANUP with COMPLETED, DISCONNECTED, CANCELLED, DEGRADED, FAILED respectively. Map unknown ProtocolViolation class to INTERNAL_ERROR.

## Task 1: parser conformance

Files: Kavya/smartpbx_protocol.py:80-155,189-230; Kavya/tests/test_smartpbx_protocol.py:8-122.

RED tests-only: module-import smartpbx_protocol and use getattr/type-name inside tests so absent UnsupportedEvent never causes collection error. Specify Hangup legs plus optional reason only; accountId extra ignored; missing/blank/non-string/boundary/over-limit legs; reason absent/empty/non-string/exact-max/over-max. Specify DTMF required legs, 0-9 star hash A-D, duration absent/0/10000 and bool/negative/fractional/10001, mismatch/extras; blank/nonstring events; strict start/media.

Command: cd Kavya && PYTHONDONTWRITEBYTECODE=1 PYTEST_ADDOPTS='-p no:cacheprovider' /home/dev/incoming/taskforce-ai/.venv/bin/python -m pytest tests/test_smartpbx_protocol.py -q
Expected RED: assertion failures for current Hangup account/reason requirement, DtmfEvent shape/A-D, and retained UnknownEvent name; no collection error. Commit only tests with failure output in body.

GREEN production-only: implement HangupEvent(legs, optional reason), DtmfEvent(legs,digit,duration), A-D, value-free UnsupportedEvent, Start three-field and Hangup/DTMF two-leg validation. Command: cd Kavya && /home/dev/incoming/taskforce-ai/.venv/bin/python -B -m py_compile smartpbx_protocol.py && PYTHONDONTWRITEBYTECODE=1 PYTEST_ADDOPTS='-p no:cacheprovider' /home/dev/incoming/taskforce-ai/.venv/bin/python -m pytest tests/test_smartpbx_protocol.py -q. Expected pass. Commit only protocol.py.

## Task 2: diagnostics and Docker packaging

Files: add Kavya/smartpbx_diagnostics.py; Kavya/Dockerfile:63; add Kavya/tests/test_smartpbx_diagnostics.py; Kavya/tests/test_smartpbx_deployment.py.

RED tests-only: imports of absent module stay inside test functions. Assert only exact enum sets, SmartPBXDiagnosticSink typed signature, import isolation, and Docker COPY/import allowlist. Command: cd Kavya && PYTHONDONTWRITEBYTECODE=1 PYTEST_ADDOPTS="-p no:cacheprovider" /home/dev/incoming/taskforce-ai/.venv/bin/python -m pytest tests/test_smartpbx_diagnostics.py tests/test_smartpbx_deployment.py -q. Expected assertion failures for absent module/COPY, never collection failure. Commit tests only.

GREEN production-only: add neutral module and add it to Dockerfile line 63 COPY in same commit. Command: cd Kavya && /home/dev/incoming/taskforce-ai/.venv/bin/python -B -m py_compile smartpbx_diagnostics.py && PYTHONDONTWRITEBYTECODE=1 PYTEST_ADDOPTS='-p no:cacheprovider' /home/dev/incoming/taskforce-ai/.venv/bin/python -m pytest tests/test_smartpbx_diagnostics.py tests/test_smartpbx_deployment.py -q. Expected pass. Commit module/Dockerfile only.

## Task 3: gateway cleanup and factory/session plumbing

Files: Kavya/smartpbx_gateway.py:143-314; Kavya/smartpbx_session.py:20-108; Kavya/server.py:4953-4997; Kavya/tests/test_smartpbx_gateway.py; Kavya/tests/test_smartpbx_gateway_transfer_pending.py; Kavya/tests/test_smartpbx_real_handover_lifecycle.py; Kavya/tests/test_smartpbx_server.py if direct-session setup is touched; every direct KavyaSmartPBXSession factory found by rg.

RED tests-only: transition-safe factories are (context, transport, sink=None), capture sink and assert it is None while diagnostics are absent, so TypeError cannot mask RED assertions. Direct-session tests retain constructor calls or use getattr/local capture. Cover seven-field record, correlation format/uniqueness/no-sentinel, table, unsupported pre/active, DTMF observed/mismatch, counter removal, first terminal later-media unread, cancellation. Inject finish/transport/release/close faults: every applicable operation exactly once, later cleanup continues, released_total exactly 1 when release works, single close. Command: cd Kavya && PYTHONDONTWRITEBYTECODE=1 PYTEST_ADDOPTS="-p no:cacheprovider" /home/dev/incoming/taskforce-ai/.venv/bin/python -m pytest tests/test_smartpbx_gateway.py tests/test_smartpbx_gateway_transfer_pending.py tests/test_smartpbx_real_handover_lifecycle.py tests/test_smartpbx_server.py -q. Expected assertion failures, no collection/type failures. Commit tests only.

GREEN production-only: change SessionFactory at Kavya/smartpbx_gateway.py:143 and server factory to three args; gateway supplies typed non-None sink. KavyaSmartPBXSession constructor accepts optional/no-op sink for unrelated direct handover compatibility and installs before welcome. Update every inventory site; implement correlation/table/counter removal/ordered cleanup. Command: cd Kavya && PYTHONDONTWRITEBYTECODE=1 PYTEST_ADDOPTS="-p no:cacheprovider" /home/dev/incoming/taskforce-ai/.venv/bin/python -m pytest tests -q. Expected pass. Commit production files only.

## Task 4: TTS attribution

Files: server.py:3517-3581; tests/test_smartpbx_server.py.

RED tests-only: separately test five English SmartPBX sites: missing API key 3519-3521, profile failure 3522-3527, HTTP status 3548-3555, timeout 3570-3575, generic exception 3576-3581. Assert exact finite tuple/no text,status,provider,body,voice,API,exception leakage and unchanged no-sink/non-SmartPBX branches. Command: cd Kavya && PYTHONDONTWRITEBYTECODE=1 PYTEST_ADDOPTS='-p no:cacheprovider' /home/dev/incoming/taskforce-ai/.venv/bin/python -m pytest tests/test_smartpbx_server.py -q. Expected assertions fail. Commit tests only.

GREEN production-only: add finite sink calls only; no propagated catch, return/state/voice change. Command: cd Kavya && PYTHONDONTWRITEBYTECODE=1 PYTEST_ADDOPTS='-p no:cacheprovider' /home/dev/incoming/taskforce-ai/.venv/bin/python -m pytest tests/test_smartpbx_server.py -q && /home/dev/incoming/taskforce-ai/.venv/bin/python -B -m py_compile server.py. Expected pass. Commit server.py only.

## Task 5: runbook

Files: SMARTPBX_RUNBOOK.md:200-278; tests/test_smartpbx_deployment.py.

RED tests-only: require the fixed event field plus other six fields, opaque local correlation, no raw/dynamic event name/payload/exception/fingerprint/session_id/counter, transfer-disabled and withdraw-drain-stop rollback. Command: cd Kavya && PYTHONDONTWRITEBYTECODE=1 PYTEST_ADDOPTS='-p no:cacheprovider' /home/dev/incoming/taskforce-ai/.venv/bin/python -m pytest tests/test_smartpbx_deployment.py -q. Expected failure on fingerprint wording. Commit tests only.

GREEN docs-only: alter Cutover gates only; preserve transfer-disabled/fallback/four-call/Docker/rollback. Command: cd Kavya && PYTHONDONTWRITEBYTECODE=1 PYTEST_ADDOPTS='-p no:cacheprovider' /home/dev/incoming/taskforce-ai/.venv/bin/python -m pytest tests/test_smartpbx_deployment.py -q. Expected pass. Commit runbook only.

## Publication and final verification

Exactly ten commits total. Before every commit run git status --short and git diff --check; record actual RED output. Read git ls-remote origin refs/heads/Rakesh, use that exact value as createCommitOnBranch expectedHeadOid, never force, then git fetch origin Rakesh and git merge --ff-only origin/Rakesh.

Final (fail-closed):
    set -euo pipefail
    PYTHONDONTWRITEBYTECODE=1 PYTEST_ADDOPTS="-p no:cacheprovider" /home/dev/incoming/taskforce-ai/.venv/bin/python -m pytest tests -q
    /home/dev/incoming/taskforce-ai/.venv/bin/python -B -m py_compile smartpbx_protocol.py smartpbx_diagnostics.py smartpbx_gateway.py smartpbx_session.py server.py
    /home/dev/incoming/taskforce-ai/.venv/bin/python -c 'from pathlib import Path; import yaml; c=yaml.safe_load(Path("docker-compose.yml").read_text()); e=c["services"]["kavya-smartpbx"]["environment"]; assert e["SMARTPBX_TRANSFER_DESTINATIONS_JSON"] == "${SMARTPBX_TRANSFER_DESTINATIONS_JSON}"; assert "SMARTPBX_TRANSFER_DESTINATIONS_JSON" in e'
    docker build -f Dockerfile -t kavya-smartpbx-plan-import .
    docker run --rm --entrypoint python kavya-smartpbx-plan-import -c 'import smartpbx_diagnostics, smartpbx_gateway, smartpbx_session, server'
    /home/dev/.cache/pre-commit/repoietpp3fj/golangenv-default/bin/gitleaks detect --source .. --no-git --redact
    git diff --check

Use gh pr view 209 --repo taskforce-ai-dev/full-voice-agent --json headRefOid,statusCheckRollup and gh run list --repo taskforce-ai-dev/full-voice-agent --branch Rakesh --limit 10 --json headSha,status,conclusion,url. Accept only when PR head and every required check SHA equal final GREEN SHA. No deploy. Rollback stays withdraw Dialog route, wait active_sessions zero, then stop kavya-smartpbx.