# Dialog v06 Hangup and Protocol-Privacy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: use `subagent-driven-development` or `executing-plans` and complete each checked RED → GREEN task in order. Do not execute this plan until it is separately approved.

**Goal:** Accept a documented Dialog v06 `hangup` for an already authenticated, start-bound SmartPBX call without the observed `invalid_message` close, and make protocol diagnostics fixed and privacy-safe.

**Architecture:** `Kavya/smartpbx_protocol.py` remains the bounded parser. `Kavya/smartpbx_gateway.py` remains the only call-state/lifecycle boundary. `HangupEvent` will retain optional `account_id` and `reason` as `str | None`; call-leg IDs always bind to the established context and a supplied account additionally must match. The gateway will replace UUID/fingerprint/dynamic logging with a finite enum diagnostic record containing only stage, outcome, failure class, active-session count, and duration.

**Tech Stack:** Python 3.11+, asyncio, Starlette WebSocket, pytest/pytest-asyncio, Docker, GitHub Actions, Dialog SmartPBX AI Provider v06.

## Global constraints

- Scope is only the valid-call v06 `hangup` parser/state defect and privacy-safe protocol diagnostics. Do not change voice, ElevenLabs, STT, LLM/provider, RAG, booking, post-call contents, MCP, handover, transfer, media format, Docker configuration, dashboard routing, or Flico.
- Preserve WSS authentication before `accept()`, strict `start.accountId == SMARTPBX_ACCOUNT_ID`, `g711_ulaw`/8000-only start admission, all message/audio/outbound-frame/time/call bounds. Current anchors: `Kavya/smartpbx_gateway.py:157-190`, `Kavya/smartpbx_protocol.py:115-186`, `Kavya/smartpbx_transport.py:24-57`.
- Dialog v06 requires `start.accountId` (`/tmp/dialog-pdf-audit-20260806/SmartPBX AI Provider - Version 06.txt:82-109`) but documents hangup with `callId`, `otherLegCallId`, and optional `reason`, not `accountId` (`:196-225`). Its prescribed flow is one start, media/DTMF, hangup, then WebSocket close (`:234-242`).
- `hangup.accountId` is optional **and preserved**. When absent, account binding is supplied only by the authenticated/start-bound `CallContext`; when present, it must be nonblank, bounded, and equal to that context. `callId` and `otherLegCallId` are always required and matching.
- Absence of hangup reason is represented by `None`. If supplied, it must be a nonblank string no longer than `_MAX_HANGUP_REASON_CHARS`; it never changes control flow and is never logged or returned.
- No diagnostic may contain raw or derived live identifiers: call/account/other-leg/caller/callee/session IDs, call fingerprints, WSS/MCP tokens, credentials, audio, transcript, payload, voice ID, event name, or exception text. Do not retain a correlation ID; this plan removes current UUID/fingerprint diagnostics rather than introducing one.
- MCP/transfer stay disabled. Do not modify `Kavya/smartpbx_mcp.py`, `Kavya/smartpbx_handover.py`, `.env.example`, `docker-compose.yml`, or `Kavya/server.py:4975-4995` transfer status behavior.
- Plan execution never deploys, restarts a container, changes a DID/dashboard route, reads a credential, places a live call, merges, or pushes to `main`.

## Evidence and decision table

| Requirement | Evidence | Planned behavior |
| --- | --- | --- |
| Defect | `HangupEvent` and `_parse_hangup` require account/reason at `Kavya/smartpbx_protocol.py:86-90,202-209`. | Parse an otherwise valid no-account/no-reason v06 hangup as `HangupEvent(call_id, other_leg_call_id, None, None)`. |
| Strict start | Start requires every identifier and ulaw/8k at `Kavya/smartpbx_protocol.py:158-172`; gateway verifies the configured account at `Kavya/smartpbx_gateway.py:185-188`. | Leave unchanged. |
| Context | Existing validator compares three values for both start/hangup at `Kavya/smartpbx_protocol.py:142-155`. | Start stays three-value. Hangup requires both call-leg values; optional account, if supplied, adds a third check. |
| Terminal | Gateway exits on hangup at `Kavya/smartpbx_gateway.py:194-217`; session finish, transport close, and lease release are already once-guarded at `Kavya/smartpbx_session.py:79-86`, `Kavya/smartpbx_transport.py:71-82`, and `Kavya/smartpbx_gateway.py:288-303`. | First valid terminal event wins; no later queued inbound frame is read; finish/close/release happen once. |
| Diagnostics | Current gateway logs UUID/fingerprint at `Kavya/smartpbx_gateway.py:158-168,188,305-314,356-357`; runbook requests fingerprints at `Kavya/SMARTPBX_RUNBOOK.md:200-211`. | Replace with fixed, value-free diagnostics and matching runbook wording. |

## File map

- Modify `Kavya/smartpbx_protocol.py:15-20,86-105,142-155,202-230`: v06 optional hangup fields, type-specific context validation, and no-name `UnsupportedEvent`.
- Modify `Kavya/smartpbx_gateway.py:17-21,146-245,247-314,348-357`: finite diagnostics, deterministic pre-start/active/terminal outcomes, and no UUID/fingerprint/error text.
- Modify `Kavya/tests/test_smartpbx_protocol.py:8-122`: parser/context RED/GREEN tests.
- Modify `Kavya/tests/test_smartpbx_gateway.py:13-186`: gateway/session-factory-boundary terminal and privacy tests.
- Modify `Kavya/SMARTPBX_RUNBOOK.md:200-211` and `Kavya/tests/test_smartpbx_deployment.py`: fixed diagnostics guidance and static contract.
- Read-only integration anchors: `Kavya/smartpbx_session.py:61-150`, `Kavya/smartpbx_transport.py:43-102`, `Kavya/server.py:4953-5005`.

---

### Task 1: Parse and validate the documented v06 hangup shape

**Files:**
- Modify: `Kavya/smartpbx_protocol.py:86-105,142-155,202-230`
- Modify: `Kavya/tests/test_smartpbx_protocol.py:8-122`

**Interfaces:**

```python
@dataclass(frozen=True)
class HangupEvent:
    call_id: str
    other_leg_call_id: str
    account_id: str | None
    reason: str | None

@dataclass(frozen=True)
class UnsupportedEvent:
    pass
```

`validate_event_context(StartEvent, context)` compares call/other-leg/account. `validate_event_context(HangupEvent, context)` compares call/other-leg, then compares account only when `event.account_id is not None`.

- [ ] **Step 1: Write failing parser and context tests**

Replace the old four-required-field hangup assertion at `tests/test_smartpbx_protocol.py:92-99` with these exact cases, using synthetic test markers only:

```python
def test_v06_hangup_allows_absent_account_and_reason_after_start_context():
    context = parse(START).context
    event = parse({
        "event": "hangup",
        "hangup": {"callId": "call-1", "otherLegCallId": "other-1"},
    })

    assert event == HangupEvent("call-1", "other-1", None, None)
    validate_event_context(event, context)


def test_v06_hangup_preserves_and_validates_supplied_account_and_reason():
    context = parse(START).context
    event = parse({
        "event": "hangup",
        "hangup": {
            "callId": "call-1", "otherLegCallId": "other-1",
            "accountId": "account-1", "reason": "NORMAL_CLEARING",
        },
    })

    assert event == HangupEvent("call-1", "other-1", "account-1", "NORMAL_CLEARING")
    validate_event_context(event, context)


@pytest.mark.parametrize("hangup", [
    {"otherLegCallId": "other-1"},
    {"callId": "call-1"},
    {"callId": "call-1", "otherLegCallId": "other-1", "accountId": " "},
    {"callId": "call-1", "otherLegCallId": "other-1", "reason": " "},
    {"callId": "call-1", "otherLegCallId": "other-1", "reason": 1},
    {"callId": "different", "otherLegCallId": "other-1"},
    {"callId": "call-1", "otherLegCallId": "different"},
    {"callId": "call-1", "otherLegCallId": "other-1", "accountId": "different"},
])
def test_v06_hangup_rejects_invalid_or_mismatched_documented_context(hangup):
    context = parse(START).context
    with pytest.raises(ProtocolViolation) as raised:
        event = parse({"event": "hangup", "hangup": hangup})
        validate_event_context(event, context)
    assert raised.value.close_code == POLICY_VIOLATION
```

Add an `A-D` DTMF parametrization while retaining `test_kavya_start_rejects_non_ulaw_media`; v06 documents the former at vendor lines `:163-171`, but this repair must not relax the latter.

- [ ] **Step 2: Run RED**

```bash
cd Kavya
PYTHONDONTWRITEBYTECODE=1 PYTEST_ADDOPTS='-p no:cacheprovider' \
  /home/dev/incoming/taskforce-ai/.venv/bin/python -m pytest tests/test_smartpbx_protocol.py -q
```

Expected: the missing-field v06 hangup fails as `invalid_message`; the old `HangupEvent` shape disagrees with the new assertion; all existing strict-start/media checks still pass.

- [ ] **Step 3: Implement minimal parser changes**

```python
def _optional_text(message: Mapping[object, object], field: str, max_chars: int) -> str | None:
    if field not in message:
        return None
    return _required_text(message, field, max_chars)


def _parse_hangup(message: Mapping[object, object]) -> HangupEvent:
    hangup = _required_mapping(message, "hangup")
    return HangupEvent(
        call_id=_required_text(hangup, "callId", _MAX_IDENTIFIER_CHARS),
        other_leg_call_id=_required_text(hangup, "otherLegCallId", _MAX_IDENTIFIER_CHARS),
        account_id=_optional_text(hangup, "accountId", _MAX_IDENTIFIER_CHARS),
        reason=_optional_text(hangup, "reason", _MAX_HANGUP_REASON_CHARS),
    )


def validate_event_context(event: SmartPBXEvent, context: CallContext) -> None:
    if isinstance(event, StartEvent):
        if (event.context.call_id, event.context.other_leg_call_id, event.context.account_id) != (context.call_id, context.other_leg_call_id, context.account_id):
            raise ProtocolViolation(POLICY_VIOLATION, "event context mismatch", "context_mismatch")
    elif isinstance(event, HangupEvent):
        if (event.call_id, event.other_leg_call_id) != (context.call_id, context.other_leg_call_id):
            raise ProtocolViolation(POLICY_VIOLATION, "event context mismatch", "context_mismatch")
        if event.account_id is not None and event.account_id != context.account_id:
            raise ProtocolViolation(POLICY_VIOLATION, "event context mismatch", "context_mismatch")
```

Replace `UnknownEvent(name: str)` with `UnsupportedEvent()` and return it after bounded parsing. Do not change byte limits, JSON parsing, `connected`/`stop` compatibility, media validation, start schema, or close messages.

- [ ] **Step 4: Run GREEN and commit**

```bash
cd Kavya
PYTHONDONTWRITEBYTECODE=1 PYTEST_ADDOPTS='-p no:cacheprovider' \
  /home/dev/incoming/taskforce-ai/.venv/bin/python -m pytest tests/test_smartpbx_protocol.py -q
git add smartpbx_protocol.py tests/test_smartpbx_protocol.py
git commit -m "fix(kavya): accept documented v06 hangup shape"
```

---

### Task 2: Enforce first-terminal-wins and fixed privacy-safe diagnostics

**Files:**
- Modify: `Kavya/smartpbx_gateway.py:17-21,146-245,247-314,348-357`
- Modify: `Kavya/tests/test_smartpbx_gateway.py:13-186`

**Interfaces:**

```python
class DiagnosticStage(str, Enum):
    ADMISSION = "admission"
    PRE_START = "pre_start"
    ACTIVE = "active"
    TERMINAL = "terminal"
    CLEANUP = "cleanup"

class DiagnosticOutcome(str, Enum):
    REJECTED = "rejected"
    COMPLETED = "completed"
    DISCONNECTED = "disconnected"
    FAILED = "failed"

class DiagnosticFailureClass(str, Enum):
    NONE = "none"
    DISABLED = "disabled"
    AUTHENTICATION = "authentication"
    CAPACITY = "capacity"
    INVALID_MESSAGE = "invalid_message"
    MESSAGE_TOO_BIG = "message_too_big"
    UNSUPPORTED_MEDIA_FORMAT = "unsupported_media_format"
    INVALID_MEDIA = "invalid_media"
    AUDIO_TOO_BIG = "audio_too_big"
    INVALID_DTMF = "invalid_dtmf"
    UNSUPPORTED_EVENT = "unsupported_event"
    START_REQUIRED = "start_required"
    ACCOUNT_MISMATCH = "account_mismatch"
    CONTEXT_MISMATCH = "context_mismatch"
    DUPLICATE_START = "duplicate_start"
    CONNECTED_AFTER_START = "connected_after_start"
    START_TIMEOUT = "start_timeout"
    IDLE_TIMEOUT = "idle_timeout"
    TRANSPORT_DISCONNECT = "transport_disconnect"
    INTERNAL_ERROR = "internal_error"
    SESSION_CLEANUP = "session_cleanup"
    TRANSPORT_CLEANUP = "transport_cleanup"
    LEASE_CLEANUP = "lease_cleanup"
```

The sole diagnostic JSON must be exactly:

```python
{
    "event": "smartpbx_protocol_diagnostic",
    "stage": stage.value,
    "outcome": outcome.value,
    "failure_class": failure_class.value,
    "active_sessions": registry.snapshot()["active_sessions"],
    "duration_ms": max(0, round((time.monotonic() - started_at) * 1000)),
}
```

- [ ] **Step 1: Write failing gateway/session-boundary tests**

Add through the existing real `SmartPBXGateway` → `Factory` → `FakeSession` seam (`tests/test_smartpbx_gateway.py:50-95`):

```python
@pytest.mark.asyncio
async def test_gateway_finishes_valid_v06_hangup_once_and_leaves_later_frames_unread():
    hangup = {"event": "hangup", "hangup": {"callId": "call-1", "otherLegCallId": "other-1"}}
    later_media = {"event": "media", "media": {"payload": "YQ=="}}
    _, registry, socket, factory = await run([START, hangup, later_media])

    assert factory.sessions[0].finishes == [True]
    assert factory.sessions[0].audio == []
    assert socket.messages == [later_media]
    assert socket.close_calls == [(1000, "call ended")]
    assert registry.snapshot()["active_sessions"] == 0


@pytest.mark.asyncio
async def test_gateway_pre_start_terminal_and_malformed_input_are_safe(caplog):
    sentinels = ("call-secret", "account-secret", "caller-secret", "token-secret")
    with caplog.at_level(logging.INFO):
        _, _, malformed_socket, _ = await run(['{"event":"hangup","hangup":{"callId":"call-secret"}}'])
        _, _, early_socket, _ = await run([{"event": "hangup", "hangup": {"callId": "call-secret", "otherLegCallId": "other-secret"}}])

    assert malformed_socket.close_calls == [(1008, "invalid SmartPBX message")]
    assert early_socket.close_calls == [(1008, "start required")]
    rendered = "\n".join(record.message for record in caplog.records)
    assert all(value not in rendered for value in sentinels)
```

Add a parametrized log test for disabled, bad authentication, capacity, malformed parser input, unsupported input before/after start, pre-start media/DTMF/hangup/stop, account mismatch, active duplicate start, active connected, context mismatch, start/idle timeout, disconnect, and cleanup failure. For each captured record, JSON-decode `record.message` and assert exactly the six diagnostic keys and enum members above; assert no synthetic call/account/caller/token/audio/transcript/voice sentinels and no `session_id` or `call_fingerprint` key.

- [ ] **Step 2: Run RED**

```bash
cd Kavya
PYTHONDONTWRITEBYTECODE=1 PYTEST_ADDOPTS='-p no:cacheprovider' \
  /home/dev/incoming/taskforce-ai/.venv/bin/python -m pytest tests/test_smartpbx_gateway.py -q
```

Expected: current valid no-account/no-reason hangup is rejected; current gateway logs UUID/fingerprint; unknown input is silently consumed; fixed-six-key assertions fail.

- [ ] **Step 3: Implement the finite state/diagnostic mapping**

1. Import `Enum`, remove `hashlib`/`uuid`, and import `UnsupportedEvent` instead of `UnknownEvent`.
2. Replace `_log_event`, `_log_lifecycle`, and `_fingerprint` with one `_emit_diagnostic` accepting only the enum types and emitting exactly the six-key JSON schema. Map `ProtocolViolation.failure_class` through an explicit fixed dictionary; unknown exceptions become `INTERNAL_ERROR`. Never pass exception text, event names, input, or identifiers.
3. Use `PRE_START` until `_receive_start` returns and `ACTIVE` only after session start. Keep pre-start `ConnectedEvent` compatibility. Pre-start media/DTMF/hangup/stop retain `start required`; pre-start `UnsupportedEvent` closes with fixed `unsupported event`.
4. In the active loop, a valid hangup first validates documented context, emits `(TERMINAL, COMPLETED, NONE)`, sets the existing fixed `1000/call ended` close, and breaks before any further receive. Stop and completed `terminal_future` use the same first-terminal-wins contract. Later queued frames are deliberately unread.
5. Active `UnsupportedEvent` rejects with fixed `unsupported event`; duplicate `StartEvent` validates first, then rejects as `DUPLICATE_START`; `ConnectedEvent` rejects as `CONNECTED_AFTER_START`. Media behavior stays unchanged. DTMF logging becomes an enum-only active diagnostic with no digit/duration.
6. Preserve the existing idempotent lease release (`smartpbx_gateway.py:80-93`), finish lock, transport close, close codes, capacity/time/size bounds, and start account binding. Do not edit session factory or transport wire payload.

- [ ] **Step 4: Run GREEN and commit**

```bash
cd Kavya
PYTHONDONTWRITEBYTECODE=1 PYTEST_ADDOPTS='-p no:cacheprovider' \
  /home/dev/incoming/taskforce-ai/.venv/bin/python -m pytest \
  tests/test_smartpbx_gateway.py tests/test_smartpbx_protocol.py -q
git add smartpbx_gateway.py tests/test_smartpbx_gateway.py
git commit -m "fix(kavya): emit private v06 protocol diagnostics"
```

---

### Task 3: Align the isolated runbook and release gates with the privacy contract

**Files:**
- Modify: `Kavya/SMARTPBX_RUNBOOK.md:200-211`
- Modify: `Kavya/tests/test_smartpbx_deployment.py`

- [ ] **Step 1: Write the failing runbook privacy test**

```python
def test_runbook_requires_fixed_protocol_diagnostics_without_call_fingerprints():
    runbook = read_text("SMARTPBX_RUNBOOK.md")

    assert "call fingerprint" not in runbook.lower()
    for required in (
        "smartpbx_protocol_diagnostic",
        "stage",
        "outcome",
        "failure_class",
        "active_sessions",
        "duration_ms",
        "never raw or derived call, account, caller, callee, token, audio, transcript, or voice identifiers",
    ):
        assert required in runbook
```

- [ ] **Step 2: Run RED**

```bash
cd Kavya
PYTHONDONTWRITEBYTECODE=1 PYTEST_ADDOPTS='-p no:cacheprovider' \
  /home/dev/incoming/taskforce-ai/.venv/bin/python -m pytest \
  tests/test_smartpbx_deployment.py::test_runbook_requires_fixed_protocol_diagnostics_without_call_fingerprints -q
```

Expected: FAIL because `SMARTPBX_RUNBOOK.md:200-203` currently asks for call fingerprints.

- [ ] **Step 3: Change only cutover diagnostic wording**

Replace `SMARTPBX_RUNBOOK.md:200-203` with:

```markdown
Before enabling the Dialog route, record only fixed `smartpbx_protocol_diagnostic`
fields: `stage`, `outcome`, `failure_class`, `active_sessions`, and `duration_ms`.
They never contain raw or derived call, account, caller, callee, token, audio,
transcript, or voice identifiers. Do not use call fingerprints.
```

Leave KB/PMS, four-call, fallback, transfer-disabled, and withdrawal commands unchanged. Existing rollback stays route withdrawal → `active_sessions == 0` drain → only then stop `kavya-smartpbx` (`SMARTPBX_RUNBOOK.md:266-278`).

- [ ] **Step 4: Run GREEN, verify, and commit**

```bash
cd Kavya
PYTHONDONTWRITEBYTECODE=1 PYTEST_ADDOPTS='-p no:cacheprovider' \
  /home/dev/incoming/taskforce-ai/.venv/bin/python -m pytest \
  tests/test_smartpbx_protocol.py \
  tests/test_smartpbx_gateway.py \
  tests/test_smartpbx_deployment.py -q
/home/dev/incoming/taskforce-ai/.venv/bin/python -B -m py_compile \
  smartpbx_protocol.py smartpbx_gateway.py smartpbx_session.py smartpbx_transport.py
python3 - <<'PYCODE'
from pathlib import Path
import yaml
compose = yaml.safe_load(Path("docker-compose.yml").read_text(encoding="utf-8"))
assert compose["services"]["kavya-smartpbx"]["environment"]["SMARTPBX_TRANSFER_DESTINATIONS_JSON"] == "${SMARTPBX_TRANSFER_DESTINATIONS_JSON}"
print("transfer_packaging_unchanged=ok")
PYCODE
/home/dev/.cache/pre-commit/repoietpp3fj/golangenv-default/bin/gitleaks detect --source .. --no-git --redact
git diff --check
git add SMARTPBX_RUNBOOK.md tests/test_smartpbx_deployment.py
git commit -m "docs(kavya): document private protocol diagnostics"
```

Expected: focused tests and compilation pass; transfer remains packaged unchanged; gitleaks reports no leak.

## Final verification, CI, Docker, and rollback

After the three atomic RED/GREEN sequences:

```bash
git fetch origin Rakesh
git merge --ff-only origin/Rakesh
cd Kavya
PYTHONDONTWRITEBYTECODE=1 PYTEST_ADDOPTS='-p no:cacheprovider' \
  /home/dev/incoming/taskforce-ai/.venv/bin/python -m pytest tests -q
python -B -m py_compile smartpbx_protocol.py smartpbx_gateway.py smartpbx_session.py smartpbx_transport.py
docker build -t kavya-dialog-v06-protocol-privacy:verify .
docker run --rm --entrypoint python kavya-dialog-v06-protocol-privacy:verify -c \
  "import smartpbx_protocol, smartpbx_gateway, smartpbx_session, smartpbx_transport"
git diff --check origin/Rakesh...HEAD
/home/dev/.cache/pre-commit/repoietpp3fj/golangenv-default/bin/gitleaks detect --source .. --no-git --redact
```

Expected: full Kavya tests, protocol privacy regressions, and strict media/start admission pass; image build/import succeeds; no secret or identifier reaches tracked code; transfer remains disabled. Monitor the exact-head CI until `test (Kavya)` has passed both **Run pytest in "Kavya"** and **Build and import Kavya image**, and the secret scan is green. Do not deploy when CI passes.

If a later, separately approved deployment must be rolled back, use the existing route-withdraw → active-session-drain → only-then-stop sequence at `Kavya/SMARTPBX_RUNBOOK.md:266-278`. During implementation, revert only the failing isolated task commit; never force-push.

## Non-goals

- No change to MCP request/header behavior, transfer destinations, transfer acknowledgement, or transfer enablement.
- No voice/profile/ElevenLabs/RAG/booking/provider/STT/greeting/filler/re-prompt/barge-in/post-call/dashboard/Flico behavior.
- No acceptance of non-ulaw media, pre-start media, unbound hangup, duplicate start, dynamic event names, raw payloads, exception text, or raw/derived identifiers in diagnostics.
- No live test, secret read/rotation, dashboard/DID change, container restart, production deployment, or merge in this work.

## Plan self-review

- [x] Exact v06 hangup and start source anchors are included.
- [x] Optional account/reason semantics preserve supplied values and validate them; call-leg IDs remain required/matching.
- [x] Malformed, pre-start, duplicate, and post-terminal handling is defined and tested at parser plus gateway/session-factory boundaries.
- [x] Diagnostics have one fixed schema and prohibit raw/derived identifiers, including fingerprints and correlations.
- [x] Auth, limits, codec, transfer-disabled packaging, CI/Docker verification, rollback, and non-goals are explicit.
