# Kavya SmartPBX Call-Parity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Migrate Kavya's SmartPBX call path to Kavya's protected English voice while preserving the existing Twilio path, provider tools, RAG, booking, reprompt, and barge-in behavior.

**Architecture:** One immutable `EnglishVoiceProfile` is injected into both Twilio language selection and the SmartPBX media pipeline. SmartPBX English TTS builds a provider request from that profile, while retained non-English/general callers keep the legacy voice path. The gateway accepts Dialog protocol v0.6, emits only bounded lifecycle diagnostics, and keeps transfer disabled until the independent MCP carrier-outcome gate is complete.

**Tech Stack:** Python 3, asyncio, aiohttp, FastAPI/WebSocket, Dialog SmartPBX protocol v0.6, ElevenLabs streaming TTS, pytest/pytest-asyncio, Docker Compose, GitHub Actions.

## Global Constraints

- This plan changes only the call-parity implementation; `docs/superpowers/plans/2026-08-07-kavya-smartpbx-mcp-handover.md` remains unchanged.
- Preserve `LANGUAGE_CONFIGS["en"]`, every retained non-English language, and Twilio behavior.
- The protected English voice ID is never committed, logged, returned by status, or copied into fixtures.
- SmartPBX English TTS uses `eleven_flash_v2_5`, `output_format=ulaw_8000` in the query string only, and JSON containing `text`, `model_id`, and `voice_settings`.
- SmartPBX transfer remains disabled: protected API key and account header are blank, destinations are `{}`, and endpoint presence alone cannot enable transfer.
- Protocol diagnostics contain only finite enums, counts, booleans, and durations; never raw caller names, phone numbers, utterances, provider payloads, account values, call IDs, session IDs, voice IDs, credentials, or destinations.
- Rollback withdraws/restores the Dialog route first, drains `active_sessions` to zero, and only then stops the Kavya service.
- Every production change begins with a failing test, passes its focused suite, and is committed independently.

## File map

- Create `Kavya/english_voice_profile.py`: immutable protected English voice profile and SmartPBX direct-stream request builder.
- Modify `Kavya/Dockerfile`: include the new runtime module in the explicit copy closure.
- Modify `Kavya/server.py`: inject the protected profile, preserve Twilio language behavior, select provider tool schemas, and keep RAG/tool/filler/reprompt/barge behavior reachable.
- Modify `Kavya/smartpbx_protocol.py`: parse Dialog v0.6 metadata and represent unsupported events explicitly.
- Modify `Kavya/smartpbx_gateway.py`: apply protocol validation and finite lifecycle diagnostics before and after `start`.
- Modify `Kavya/smartpbx_session.py`: accept the diagnostic callback and inject the protected profile into `MediaStreamSession`.
- Modify `Kavya/.env.example`, `Kavya/docker-compose.yml`, and `Kavya/SMARTPBX_RUNBOOK.md`: document safe configuration, disabled transfer, verification, drain, and rollback.
- Modify focused tests under `Kavya/tests/`: prove behavior rather than source-text resemblance.

---

### Task 1: Add the protected English voice profile to the runtime image

**Files:**
- Create: `Kavya/english_voice_profile.py`
- Modify: `Kavya/Dockerfile`
- Test: `Kavya/tests/test_english_voice_profile.py`
- Test: `Kavya/tests/test_smartpbx_deployment.py`

**Interfaces:**
- Produces: `EnglishVoiceProfile.from_env(env: Mapping[str, str]) -> EnglishVoiceProfile`
- Produces: `build_direct_stream_request(profile: EnglishVoiceProfile, text: str) -> DirectStreamRequest`
- Produces: `DirectStreamRequest.url`, `.params`, and `.json_body`

- [ ] **Step 1: Write failing profile and Docker-closure tests**

```python
# Kavya/tests/test_english_voice_profile.py
import pytest

from english_voice_profile import EnglishVoiceProfile, build_direct_stream_request


def test_profile_requires_protected_voice_without_exposing_value():
    with pytest.raises(RuntimeError, match="protected English voice is not configured") as error:
        EnglishVoiceProfile.from_env({})
    assert "voice_id" not in str(error.value).lower()


def test_direct_request_keeps_ulaw_format_out_of_json():
    profile = EnglishVoiceProfile(voice_id="protected-test-marker")
    request = build_direct_stream_request(profile, "Welcome")

    assert request.url.endswith("/protected-test-marker/stream")
    assert request.params == {"output_format": "ulaw_8000"}
    assert request.json_body == {
        "text": "Welcome",
        "model_id": "eleven_flash_v2_5",
        "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
    }
    assert "output_format" not in request.json_body
```

```python
# Add to Kavya/tests/test_smartpbx_deployment.py
# test_smartpbx_deployment.py already provides read_text relative to Kavya.
def test_dockerfile_locks_dependencies_and_copies_every_smartpbx_runtime_module():
    dockerfile = read_text("Dockerfile")
    assert "requirements-prod.lock.txt" in dockerfile
    for module in (
        "english_voice_profile.py",
        "smartpbx_gateway.py",
        "smartpbx_handover.py",
        "smartpbx_mcp.py",
        "smartpbx_protocol.py",
        "smartpbx_session.py",
        "smartpbx_transport.py",
    ):
        assert module in dockerfile
```

- [ ] **Step 2: Run the tests and verify RED**

Run:

```bash
cd Kavya
pytest -q tests/test_english_voice_profile.py tests/test_smartpbx_deployment.py::test_dockerfile_locks_dependencies_and_copies_every_smartpbx_runtime_module
```

Expected: collection fails because `english_voice_profile` does not exist, or the Docker closure assertion fails.

- [ ] **Step 3: Add the complete profile module**

```python
# Kavya/english_voice_profile.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping
from urllib.parse import quote


@dataclass(frozen=True)
class EnglishVoiceProfile:
    voice_id: str
    model_id: str = "eleven_flash_v2_5"
    output_format: str = "ulaw_8000"
    stability: float = 0.5
    similarity_boost: float = 0.75

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "EnglishVoiceProfile":
        voice_id = env.get("KAVYA_EN_ELEVENLABS_VOICE_ID", "").strip()
        if not voice_id:
            raise RuntimeError("protected English voice is not configured")
        return cls(voice_id=voice_id)

    @property
    def conversation_relay_voice(self) -> str:
        return f"{self.voice_id}-flash_v2_5"


@dataclass(frozen=True)
class DirectStreamRequest:
    url: str
    params: dict[str, str]
    json_body: dict[str, object]


def build_direct_stream_request(
    profile: EnglishVoiceProfile,
    text: str,
) -> DirectStreamRequest:
    voice_segment = quote(profile.voice_id, safe="")
    return DirectStreamRequest(
        url=f"https://api.elevenlabs.io/v1/text-to-speech/{voice_segment}/stream",
        params={"output_format": profile.output_format},
        json_body={
            "text": text,
            "model_id": profile.model_id,
            "voice_settings": {
                "stability": profile.stability,
                "similarity_boost": profile.similarity_boost,
            },
        },
    )
```

- [ ] **Step 4: Add the module to the existing explicit Docker COPY allowlist**

Replace the allowlist line with:

```dockerfile
COPY server.py media_stream_server.py tools.py booking_api.py post_call.py knowledge_base.py yanolja_client.py yanolja_service.py dashboard_client.py handover.py english_voice_profile.py smartpbx_gateway.py smartpbx_handover.py smartpbx_mcp.py smartpbx_protocol.py smartpbx_session.py smartpbx_transport.py ./
```

- [ ] **Step 5: Run GREEN and commit**

```bash
cd Kavya
pytest -q tests/test_english_voice_profile.py tests/test_smartpbx_deployment.py::test_dockerfile_locks_dependencies_and_copies_every_smartpbx_runtime_module
git add english_voice_profile.py Dockerfile tests/test_english_voice_profile.py tests/test_smartpbx_deployment.py
git commit -m "feat(kavya): add protected English voice profile"
```

Expected: all selected tests pass; the commit contains no real voice value.

---

### Task 2: Preserve complete language configuration and route English voice selection through one helper

**Files:** `Kavya/server.py`, `Kavya/tests/test_handover_server.py`, and `Kavya/tests/test_smartpbx_server.py`.

- [ ] Add red resolver tests that install `EnglishVoiceProfile("protected-test-marker")`, assert English resolves to `protected-test-marker-flash_v2_5`, assert missing English profile fails closed, and assert Sinhala/Tamil return exact retained voices. Add initial, language-selection, and handover-recovery Twilio route tests through the existing `test_handover_server.py` `TestClient` seam.
- [ ] Keep this complete mapping; the blank English field is an internal sentinel and is never rendered directly:

```python
LANGUAGE_CONFIGS: dict[str, dict[str, str]] = {
    "en": {
        "tts_provider": "ElevenLabs",
        "voice": "",
        "language": "en-US",
        "transcription_language": CR_TRANSCRIPTION_LANGUAGE_EN,
        "hints": CR_HINTS_EN,
        "welcome_greeting": "Welcome to Hatton Hills! I'm Kavya, how can I help you today?",
        "extra_attrs": '        elevenlabsTextNormalization="on"\n',
    },
    "si": {
        "tts_provider": "google",
        "voice": "si-LK-Standard-A",
        "language": "si-LK",
        "welcome_greeting": (
            "\u0D86\u0DBA\u0DD4\u0DB6\u0DDD\u0DC0\u0DB1\u0DCA! Hatton Hills \u0DC0\u0DD9\u0DAD "
            "\u0DC3\u0DCF\u0DAF\u0DBB\u0DBA\u0DD9\u0DB1\u0DCA \u0DB4\u0DD2\u0DC5\u0DD2\u0D9C\u0DB1\u0DD2\u0DB8\u0DD4. "
            "\u0DB8\u0DA7 \u0D94\u0DB6\u0DA7 \u0D9A\u0DD9\u0DC3\u0DDA \u0D8B\u0DAF\u0DC0\u0DCA \u0D9A\u0DC5 \u0DC4\u0DD0\u0D9A\u0DD2\u0DAF?"
        ),
        "extra_attrs": "",
    },
    "ta": {
        "tts_provider": "google",
        "voice": "ta-IN-Standard-A",
        "language": "ta-IN",
        "welcome_greeting": (
            "\u0BB5\u0BA3\u0B95\u0BCD\u0B95\u0BAE\u0BCD! Hatton Hills \u0B95\u0BCD\u0B95\u0BC1 "
            "\u0BB5\u0BB0\u0BB5\u0BC7\u0BB1\u0BCD\u0B95\u0BBF\u0BB1\u0BCB\u0BAE\u0BCD. \u0BA8\u0BBE\u0BA9\u0BCD "
            "\u0B89\u0B99\u0BCD\u0B95\u0BB3\u0BC1\u0B95\u0BCD\u0B95\u0BC1 \u0B8E\u0BAA\u0BCD\u0BAA\u0B9F\u0BBF "
            "\u0B89\u0BA4\u0BB5\u0BB2\u0BBE\u0BAE\u0BCD?"
        ),
        "extra_attrs": "",
    },
}

ENGLISH_VOICE_PROFILE: EnglishVoiceProfile | None = None


def get_language_voice(lang: str, profile: EnglishVoiceProfile | None = None) -> str:
    if lang != "en":
        return LANGUAGE_CONFIGS[lang]["voice"]
    selected = ENGLISH_VOICE_PROFILE if profile is None else profile
    if selected is None:
        raise RuntimeError("protected English voice is not configured")
    return selected.conversation_relay_voice


def get_language_config(lang: str, profile: EnglishVoiceProfile | None = None) -> dict[str, str]:
    config = dict(LANGUAGE_CONFIGS[lang])
    config["voice"] = get_language_voice(lang, profile)
    return config
```

- [ ] Load the profile from `KAVYA_EN_ELEVENLABS_VOICE_ID`. Use `get_language_config("en")` at incoming and recovery, `get_language_config(lang)` at selection, and replace only recovery greeting. SmartPBX may retain its greeting lookup.
- [ ] Run `pytest -q tests/test_smartpbx_server.py tests/test_handover_server.py`, then commit `server.py` and these existing test files.

---

### Task 3: Put SmartPBX English direct TTS before the legacy voice guard

**Files:** `Kavya/server.py`, `Kavya/smartpbx_session.py`, `Kavya/tests/test_smartpbx_server.py`, and `Kavya/tests/test_english_voice_profile.py`.

- [ ] Add red tests using `FakeTransport` and a patched `httpx.AsyncClient` stream: a SmartPBX English session with `EnglishVoiceProfile("protected-test-marker")` works when the legacy ID is blank; a missing profile fails closed; a retained non-English session preserves its existing legacy behavior.
- [ ] Extend `MediaStreamSession` with optional `english_voice_profile` and `diagnostic_sink`, and extend `KavyaSmartPBXSession` plus the actual `_new_smartpbx_session(context, transport, diagnostic_sink)` factory to pass both. Existing Twilio construction keeps defaults.
- [ ] Before the legacy guard, SmartPBX English calls `build_direct_stream_request(self._english_voice_profile, text)` and passes its URL, query params, and JSON body to `httpx.AsyncClient.stream`. It uses `eleven_flash_v2_5` with `ulaw_8000` only in query params. Retain existing chunks, barge-in, timeout, and exception handling; non-English routes retain existing voice/model behavior.
- [ ] Run `pytest -q tests/test_english_voice_profile.py tests/test_smartpbx_server.py` and commit the listed runtime and test files.

---

### Task 4: Align the existing strict parser with Dialog v0.6 and reject unsupported events safely

**Files:** `Kavya/smartpbx_protocol.py`, `Kavya/smartpbx_gateway.py`, `Kavya/tests/test_smartpbx_protocol.py`, and `Kavya/tests/test_smartpbx_gateway.py`.

- [ ] Add red tests with the existing `parse` helper for hangup without account identifier/reason, optional bounded reason, context comparison using documented hangup IDs only, DTMF `A` through `D`, and a future event yielding `UnsupportedEvent()` with no stored attributes.
- [ ] Use `HangupEvent(call_id: str, other_leg_call_id: str, reason: str | None)` and `UnsupportedEvent()`. Add `_optional_text`; require only call and other-leg IDs for hangup; compare those two fields on hangup context validation; accept `0123456789*#ABCD`; retain full start context, bounded media, close codes, and strict `g711_ulaw`/8000 admission.
- [ ] `parse_smartpbx_event` returns `UnsupportedEvent()` after bounded JSON admission. In `_receive_start`, reject it with `LifecycleStage.PRE_START`; in the active loop, reject it with `LifecycleStage.ACTIVE`. Connected remains pre-start compatibility only. No unsupported input identifier/value/payload or counter is retained.
- [ ] Run `pytest -q tests/test_smartpbx_protocol.py tests/test_smartpbx_gateway.py` and commit these four files.

---

### Task 5: Implement finite, privacy-safe lifecycle diagnostics with distinct pre-start and active attribution

**Files:** `Kavya/smartpbx_gateway.py`, `Kavya/smartpbx_session.py`, `Kavya/server.py`, `Kavya/tests/test_smartpbx_gateway.py`, and `Kavya/tests/test_smartpbx_server.py`.

```python
class LifecycleStage(str, Enum):
    ADMISSION = "admission"
    PRE_START = "pre_start"
    ACTIVE = "active"
    STT = "stt"
    TTS = "tts"
    PIPELINE = "pipeline"
    CLEANUP = "cleanup"


class FailureClass(str, Enum):
    AUTHENTICATION = "authentication"
    DISABLED = "disabled"
    CAPACITY = "capacity"
    ACCOUNT_MISMATCH = "account_mismatch"
    START_REQUIRED = "start_required"
    DUPLICATE_START = "duplicate_start"
    CONNECTED_AFTER_START = "connected_after_start"
    START_TIMEOUT = "start_timeout"
    IDLE_TIMEOUT = "idle_timeout"
    UNSUPPORTED_EVENT = "unsupported_event"
    PROTOCOL = "protocol"
    STT_UNAVAILABLE = "stt_unavailable"
    STT_QUEUE_OVERFLOW = "stt_queue_overflow"
    TTS_UNAVAILABLE = "tts_unavailable"
    TTS_STATUS = "tts_status"
    TTS_TIMEOUT = "tts_timeout"
    TTS_EXCEPTION = "tts_exception"
    PIPELINE = "pipeline"
    INTERNAL_ERROR = "internal_error"
    SESSION_CLEANUP = "session_cleanup"
    TRANSPORT_CLEANUP = "transport_cleanup"
    LEASE_CLEANUP = "lease_cleanup"


DiagnosticSink = Callable[[LifecycleStage, FailureClass], None]


def emit_diagnostic(logger: logging.Logger, stage: LifecycleStage, failure: FailureClass) -> None:
    logger.info("smartpbx_call_lifecycle", extra={"event": "smartpbx_call_lifecycle", "stage": stage.value, "failure_class": failure.value})
```

- [ ] Extend existing gateway test fakes for `(context, transport, diagnostic_sink)`. Assert `[{"event": "future-before-start"}]` emits `(LifecycleStage.PRE_START, FailureClass.UNSUPPORTED_EVENT)` and `[START, {"event": "future-after-start"}]` emits `(LifecycleStage.ACTIVE, FailureClass.UNSUPPORTED_EVENT)`. Also cover admission, start required, account mismatch, duplicate start, connected-after-start, both timeouts, cleanup, STT, and TTS classes. Diagnostics contain only the three fixed fields.
- [ ] Do not map a failure class to one stage: unsupported traffic is valid in two finite stages. Emit admission, pre-start, active, STT, TTS, pipeline, and cleanup stages at their corresponding exact branches. A catch branch must not duplicate a class emitted by a prior branch.
- [ ] Replace lifecycle use of identifiers, fingerprints, dynamic outcomes, exception text, and raw event data with the enum interface. Pass the sink into both sessions; fixed local STT/TTS outcomes map only to enum values. Preserve all listed failure classes and close outcomes.
- [ ] Run `pytest -q tests/test_smartpbx_gateway.py tests/test_smartpbx_server.py`, perform the lifecycle privacy scan, and commit the five files.

---

### Task 6: Prove provider tools, RAG, booking, fillers, reprompt, and barge-in parity

**Files:** `Kavya/server.py` and `Kavya/tests/test_smartpbx_server.py`.

- [ ] Add behavior tests with existing SmartPBX test fakes for native Claude/Gemini/OpenAI schemas, retrieval before dispatch, existing booking filler ordering, safe failed-tool filler/recovery, re-prompt after a direct completion signal, and barge-in cancellation plus direct audio clear.
- [ ] Implement explicit provider selection and normalize only SmartPBX English tool calls at the existing boundary. Reuse existing English retrieval, tools, fillers, re-prompt, transfer-pending, and interruption behavior; preserve Twilio branches.
- [ ] Run `pytest -q tests/test_smartpbx_server.py tests/test_handover_server.py` and commit `server.py` plus `tests/test_smartpbx_server.py`.

---

### Task 7: Lock transfer off and document safe deployment, drain, and rollback

**Files:** `Kavya/.env.example`, `Kavya/docker-compose.yml`, `Kavya/SMARTPBX_RUNBOOK.md`, `Kavya/tests/test_smartpbx_deployment.py`, and `Kavya/tests/test_smartpbx_mcp.py`.

- [ ] Add a deployment test through existing `read_text` for `SMARTPBX_MCP_URL`, `SMARTPBX_API_KEY`, `SMARTPBX_MCP_ACCOUNT_HEADER`, and `SMARTPBX_TRANSFER_DESTINATIONS_JSON`, with blank API key/account header and `{}` destinations.
- [ ] Add this MCP test:

```python
def test_endpoint_alone_cannot_enable_transfer():
    settings = DialogMCPSettings.from_env({
        "SMARTPBX_MCP_URL": "https://dialog.example:9443/ucp/v2/mcp",
        "SMARTPBX_API_KEY": "",
        "SMARTPBX_ACCOUNT_ID": "",
        "SMARTPBX_MCP_ACCOUNT_HEADER": "",
        "SMARTPBX_TRANSFER_DESTINATIONS_JSON": "{}",
    })
    assert settings.enabled is False
    assert settings.transfer_destinations == {}
```

- [ ] Configure only these actual names:

```dotenv
SMARTPBX_MCP_URL=https://dialog.cybergate.lk:9443/ucp/v2/mcp
SMARTPBX_API_KEY=
SMARTPBX_MCP_ACCOUNT_HEADER=
SMARTPBX_TRANSFER_DESTINATIONS_JSON={}
```

```yaml
SMARTPBX_MCP_URL: "${SMARTPBX_MCP_URL:-https://dialog.cybergate.lk:9443/ucp/v2/mcp}"
SMARTPBX_API_KEY: "${SMARTPBX_API_KEY:-}"
SMARTPBX_MCP_ACCOUNT_HEADER: "${SMARTPBX_MCP_ACCOUNT_HEADER:-}"
SMARTPBX_TRANSFER_DESTINATIONS_JSON: "${SMARTPBX_TRANSFER_DESTINATIONS_JSON:-{}}"
```

The endpoint may remain documented, but `DialogMCPSettings.from_env().enabled` also requires a valid API key, account ID, account header, and nonempty validated destination map.
- [ ] Update `Kavya/SMARTPBX_RUNBOOK.md`: transfer stays disabled with blank protected fields and `{}` destinations until the independent MCP carrier-outcome gate. Rollback withdraws/restores the Dialog route, verifies fallback, drains authenticated `active_sessions` to zero, then stops the reviewed service; expiry leaves the service up and escalates.
- [ ] Run `pytest -q tests/test_smartpbx_deployment.py tests/test_smartpbx_mcp.py`, scan only the real setting names, and commit the five files.

---

### Task 8: Run the release gate and attach evidence to the actual implementation PR

**Files:**
- Modify only if a command exposes a defect: files owned by Tasks 1-7
- Do not modify: `docs/superpowers/plans/2026-08-07-kavya-smartpbx-mcp-handover.md`

**Interfaces:**
- Consumes: commits from Tasks 1-7
- Produces: passing tests, clean secret scans, container smoke evidence, and a live Dialog call matrix on the dynamically resolved implementation PR

- [ ] **Step 1: Run focused suites from a clean checkout**

```bash
cd Kavya
pytest -q \
  tests/test_english_voice_profile.py \
  tests/test_smartpbx_protocol.py \
  tests/test_smartpbx_gateway.py \
  tests/test_smartpbx_server.py \
  tests/test_smartpbx_deployment.py \
  tests/test_smartpbx_mcp.py
```

Expected: all focused tests pass.

- [ ] **Step 2: Run the full Kavya regression suite**

```bash
cd Kavya
pytest -q
```

Expected: all tests pass, including existing Twilio, tool, booking, knowledge-base, and handover-recovery tests.

- [ ] **Step 3: Run source, configuration, and image scans**

```bash
cd Kavya
rg -n "output_format.*json|json.*output_format" server.py english_voice_profile.py tests
rg -n "KAVYA_EN_ELEVENLABS_VOICE_ID|SMARTPBX_API_KEY|SMARTPBX_MCP_ACCOUNT_HEADER" . --glob '!tests/**'
docker compose --env-file .env.example --profile smartpbx config
docker build -t kavya-smartpbx-parity:verify .
docker run --rm --entrypoint python kavya-smartpbx-parity:verify -c "import english_voice_profile, smartpbx_gateway, smartpbx_session"
```

Expected: no JSON-body `output_format`; no committed protected values; compose renders with transfer disabled; the image imports every runtime module.

- [ ] **Step 4: Resolve the current implementation PR dynamically**

```bash
PR_NUMBER=$(gh pr view --json number --jq '.number')
PR_URL=$(gh pr view --json url --jq '.url')
test -n "$PR_NUMBER"
test -n "$PR_URL"
printf 'implementation_pr=%s %s\n' "$PR_NUMBER" "$PR_URL"
```

Expected: both values resolve from the checked-out branch. Do not hardcode a PR number.

- [ ] **Step 5: Execute and record the live Dialog acceptance matrix**

With the reviewed image SHA deployed and transfer still disabled, place authenticated Dialog calls and record only pass/fail plus bounded durations for:

- inbound connection and valid v0.6 `start`;
- Kavya English greeting with the approved protected voice;
- caller barge-in during greeting;
- RAG-backed property question;
- booking request that invokes `create_booking` after the booking filler;
- silence reprompt and resumed conversation;
- normal stop and session cleanup;
- unsupported event before `start` and after `start` in a non-production protocol probe;
- `/smartpbx/status` showing `active_sessions=0` after drain and `transfer_enabled=false`.

Do not record caller names, numbers, utterances, account/call/session IDs, protected voice values, keys, headers, destinations, or raw provider payloads.

- [ ] **Step 6: Post evidence and require review before merge**

```bash
gh pr comment "$PR_NUMBER" --body-file - <<'EOF'
Kavya SmartPBX call-parity release gate:

- Focused tests: PASS
- Full Kavya regression: PASS
- Compose render and container import smoke: PASS
- Protected-value/source scan: PASS
- Dialog v0.6 start and greeting: PASS
- Barge-in: PASS
- RAG response: PASS
- Booking tool and intent filler ordering: PASS
- Silence reprompt: PASS
- Unsupported pre-start/post-start diagnostics: PASS
- Drain reached active_sessions=0: PASS
- transfer_enabled=false: PASS

Evidence contains no caller, account, call/session, voice, credential, destination, utterance, or provider-payload values.
EOF

gh pr view "$PR_NUMBER" --json number,url,headRefName,baseRefName,statusCheckRollup,reviews
```

Expected: evidence is attached to the actual implementation PR, required checks are green, and approval is present before merge.

- [ ] **Step 7: Commit only test-driven corrections, if any**

If verification exposed a defect, return to the owning task, add a reproducing failing test, make the smallest correction, rerun Steps 1-6, and commit only those reviewed files. If no defect was exposed, create no empty commit.

---

## Final self-review checklist

- [ ] `Kavya/english_voice_profile.py` is in `Kavya/Dockerfile` and the Docker closure regression test.
- [ ] `LANGUAGE_CONFIGS["en"]` remains present; only its hardcoded voice source is removed.
- [ ] Initial Twilio, language selection, and Twilio handover recovery use `get_language_config`/`get_language_voice`.
- [ ] SmartPBX English branches before the legacy voice guard and works without `ELEVENLABS_VOICE_ID`.
- [ ] Missing protected profile fails closed; no protected value is logged or committed.
- [ ] `output_format=ulaw_8000` is query-only and the direct JSON body is exact.
- [ ] Unsupported events are rejected before and after `start`.
- [ ] Every required failure class remains distinct, including all three cleanup failures and four TTS failures.
- [ ] Diagnostics expose only finite stage/failure enums and no raw identifiers or PII.
- [ ] Claude, Gemini, and OpenAI receive their current native tool schemas.
- [ ] RAG, booking, intent filler ordering/recovery, reprompt, and barge-in have behavioral assertions.
- [ ] Transfer stays disabled with blank protected fields, `{}` destinations, and an endpoint that cannot enable it alone.
- [ ] Runbook transfer activation is blocked on the independent MCP carrier-outcome/failsafe gate.
- [ ] Rollback withdraws the route, drains to zero, then stops the service.
- [ ] The implementation PR is resolved dynamically and the MCP plan is unchanged.