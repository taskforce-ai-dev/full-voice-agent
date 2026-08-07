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
- Modify `Kavya/.env.example`, `Kavya/docker-compose.yml`, and `Kavya/docs/smartpbx-runbook.md`: document safe configuration, disabled transfer, verification, drain, and rollback.
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
from pathlib import Path


def test_dockerfile_locks_dependencies_and_copies_every_smartpbx_runtime_module():
    dockerfile = Path("Kavya/Dockerfile").read_text()
    assert "requirements.lock" in dockerfile
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
        voice_id = env.get("KAVYA_ENGLISH_VOICE_ID", "").strip()
        if not voice_id:
            raise RuntimeError("protected English voice is not configured")
        return cls(voice_id=voice_id)


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

### Task 2: Preserve language configuration and route every English voice consumer through one helper

**Files:**
- Modify: `Kavya/server.py:118-190,1751,1816,2180`
- Test: `Kavya/tests/test_server.py`
- Test: `Kavya/tests/test_smartpbx_server.py`

**Interfaces:**
- Consumes: `EnglishVoiceProfile`
- Produces: `get_language_voice(lang: str, profile: EnglishVoiceProfile | None = None) -> str`
- Produces: `get_language_config(lang: str, profile: EnglishVoiceProfile | None = None) -> dict[str, object]`

- [ ] **Step 1: Add failing helper and Twilio recovery tests**

```python
# Add to Kavya/tests/test_server.py
import server
from english_voice_profile import EnglishVoiceProfile


def test_english_language_config_is_retained_and_resolved_from_profile(monkeypatch):
    profile = EnglishVoiceProfile(voice_id="protected-test-marker")
    monkeypatch.setattr(server, "ENGLISH_VOICE_PROFILE", profile)

    assert "en" in server.LANGUAGE_CONFIGS
    assert server.get_language_voice("en") == "protected-test-marker-flash_v2_5"
    assert server.get_language_config("en")["welcome_greeting"] == server.LANGUAGE_CONFIGS["en"]["welcome_greeting"]
    assert server.get_language_config("en")["voice"] == "protected-test-marker-flash_v2_5"


def test_retained_non_english_voice_is_unchanged():
    assert server.get_language_voice("hi") == server.LANGUAGE_CONFIGS["hi"]["voice"]


def test_english_voice_helper_fails_closed_without_profile(monkeypatch):
    monkeypatch.setattr(server, "ENGLISH_VOICE_PROFILE", None)
    with pytest.raises(RuntimeError, match="protected English voice is not configured"):
        server.get_language_voice("en")
```

Add endpoint-level assertions to the existing initial Twilio webhook, language-selection webhook, and Twilio handover-recovery tests:

```python
assert response.status_code == 200
assert "protected-test-marker-flash_v2_5" in response.text
assert "KAVYA_ENGLISH_VOICE_ID" not in response.text
```

- [ ] **Step 2: Run RED**

```bash
cd Kavya
pytest -q tests/test_server.py -k "english_language_config or retained_non_english or english_voice_helper or twilio"
```

Expected: `get_language_voice` and `get_language_config` are absent, or the three production paths still use the hardcoded English source.

- [ ] **Step 3: Keep `LANGUAGE_CONFIGS["en"]` and add exact resolver helpers**

Keep the complete English dictionary, including its existing greeting and language metadata, but replace its hardcoded `voice` literal with an empty sentinel:

```python
LANGUAGE_CONFIGS = {
    "en": {
        "name": "English",
        "voice": "",
        "welcome_greeting": "Welcome to KAVYA. How may I help you today?",
    },
    # Keep every currently supported non-English entry byte-for-byte.
}

ENGLISH_VOICE_PROFILE: EnglishVoiceProfile | None = None


def get_language_voice(
    lang: str,
    profile: EnglishVoiceProfile | None = None,
) -> str:
    config = LANGUAGE_CONFIGS[lang]
    if lang != "en":
        return str(config["voice"])
    selected = profile or ENGLISH_VOICE_PROFILE
    if selected is None:
        raise RuntimeError("protected English voice is not configured")
    return f"{selected.voice_id}-flash_v2_5"


def get_language_config(
    lang: str,
    profile: EnglishVoiceProfile | None = None,
) -> dict[str, object]:
    config = dict(LANGUAGE_CONFIGS[lang])
    config["voice"] = get_language_voice(lang, profile)
    return config
```

Load the protected profile once during application startup after environment loading:

```python
try:
    ENGLISH_VOICE_PROFILE = EnglishVoiceProfile.from_env(os.environ)
except RuntimeError:
    ENGLISH_VOICE_PROFILE = None
```

- [ ] **Step 4: Replace all three production English voice consumers**

At the initial Twilio response around line 1751:

```python
english_config = get_language_config("en")
voice = str(english_config["voice"])
greeting = str(english_config["welcome_greeting"])
```

At language selection around line 1816:

```python
selected_config = get_language_config(selected_language)
voice = str(selected_config["voice"])
greeting = str(selected_config["welcome_greeting"])
```

At Twilio handover recovery around line 2180:

```python
recovery_config = get_language_config("en")
recovery_voice = str(recovery_config["voice"])
recovery_greeting = str(recovery_config["welcome_greeting"])
```

Do not replace `smartpbx_session.py`'s lookup of `LANGUAGE_CONFIGS["en"]["welcome_greeting"]`; it consumes the retained greeting, not the voice source.

- [ ] **Step 5: Run the complete Twilio-focused suite and commit**

```bash
cd Kavya
pytest -q tests/test_server.py tests/test_smartpbx_server.py
git add server.py tests/test_server.py tests/test_smartpbx_server.py
git commit -m "refactor(kavya): centralize English voice selection"
```

Expected: all existing Twilio tests and new initial/selection/recovery assertions pass.

---

### Task 3: Put SmartPBX English direct TTS before the legacy voice guard

**Files:**
- Modify: `Kavya/server.py:2450-2525,3480-3565`
- Modify: `Kavya/smartpbx_session.py`
- Test: `Kavya/tests/test_smartpbx_server.py`
- Test: `Kavya/tests/test_english_voice_profile.py`

**Interfaces:**
- Consumes: `build_direct_stream_request(profile, text)`
- Produces: `MediaStreamSession(..., english_voice_profile: EnglishVoiceProfile | None, smartpbx_english: bool = False)`
- Produces: `_tts_elevenlabs(text: str) -> None` with separate SmartPBX-English and legacy guards

- [ ] **Step 1: Write failing guard-order and request-contract tests**

```python
# Add to Kavya/tests/test_smartpbx_server.py
@pytest.mark.asyncio
async def test_smartpbx_english_tts_works_without_legacy_voice_id(monkeypatch):
    posted = {}

    class Response:
        status = 200
        async def __aenter__(self): return self
        async def __aexit__(self, exc_type, exc, tb): return False
        async def read(self): return b"\xff" * 320

    class Client:
        def post(self, url, *, params, json, headers):
            posted.update(url=url, params=params, json=json, headers=headers)
            return Response()

    monkeypatch.setattr(server, "ELEVENLABS_API_KEY", "api-test-marker")
    monkeypatch.setattr(server, "ELEVENLABS_VOICE_ID", "")
    session = make_media_session(
        client=Client(),
        smartpbx_english=True,
        english_voice_profile=EnglishVoiceProfile("protected-test-marker"),
    )

    await session._tts_elevenlabs("Hello")

    assert posted["params"] == {"output_format": "ulaw_8000"}
    assert posted["json"] == {
        "text": "Hello",
        "model_id": "eleven_flash_v2_5",
        "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
    }
    assert "output_format" not in posted["json"]


@pytest.mark.asyncio
async def test_smartpbx_english_tts_fails_closed_without_profile(monkeypatch):
    monkeypatch.setattr(server, "ELEVENLABS_API_KEY", "api-test-marker")
    monkeypatch.setattr(server, "ELEVENLABS_VOICE_ID", "")
    session = make_media_session(
        smartpbx_english=True,
        english_voice_profile=None,
    )

    with pytest.raises(RuntimeError, match="protected English voice is not configured"):
        await session._tts_elevenlabs("Hello")


@pytest.mark.asyncio
async def test_legacy_non_english_tts_still_requires_legacy_voice(monkeypatch):
    monkeypatch.setattr(server, "ELEVENLABS_API_KEY", "api-test-marker")
    monkeypatch.setattr(server, "ELEVENLABS_VOICE_ID", "")
    session = make_media_session(smartpbx_english=False, language="hi")

    await session._tts_elevenlabs("Namaste")

    assert session.transport.audio_chunks == []
```

- [ ] **Step 2: Run RED**

```bash
cd Kavya
pytest -q tests/test_smartpbx_server.py -k "smartpbx_english_tts or legacy_non_english_tts"
```

Expected: SmartPBX English returns at the combined legacy guard or its request contract differs.

- [ ] **Step 3: Inject the profile through the SmartPBX session factory**

```python
# Kavya/smartpbx_session.py
class KavyaSmartPBXSession:
    def __init__(self, context, transport, *, english_voice_profile, diagnostic_sink):
        self._context = context
        self._transport = transport
        self._english_voice_profile = english_voice_profile
        self._diagnostic_sink = diagnostic_sink
        self._pipeline = MediaStreamSession(
            transport=transport,
            language="en",
            smartpbx_english=True,
            english_voice_profile=english_voice_profile,
            diagnostic_sink=diagnostic_sink,
        )
```

```python
# Kavya/server.py
async def _new_smartpbx_session(context, transport, diagnostic_sink):
    if ENGLISH_VOICE_PROFILE is None:
        raise RuntimeError("protected English voice is not configured")
    return KavyaSmartPBXSession(
        context,
        transport,
        english_voice_profile=ENGLISH_VOICE_PROFILE,
        diagnostic_sink=diagnostic_sink,
    )
```

- [ ] **Step 4: Implement the guard order and direct request branch**

```python
async def _tts_elevenlabs(self, text: str) -> None:
    if not ELEVENLABS_API_KEY:
        self._report_tts_failure("tts_unavailable")
        return

    if self.smartpbx_english:
        if self.english_voice_profile is None:
            self._report_tts_failure("tts_unavailable")
            raise RuntimeError("protected English voice is not configured")
        request = build_direct_stream_request(self.english_voice_profile, text)
        url = request.url
        params = request.params
        body = request.json_body
    else:
        if not ELEVENLABS_VOICE_ID:
            self._report_tts_failure("tts_unavailable")
            return
        url = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}/stream"
        params = {"output_format": "ulaw_8000"}
        body = {
            "text": text,
            "model_id": "eleven_multilingual_v2",
            "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
        }

    headers = {"xi-api-key": ELEVENLABS_API_KEY, "content-type": "application/json"}
    try:
        async with self.http_client.post(
            url,
            params=params,
            json=body,
            headers=headers,
        ) as response:
            if response.status != 200:
                self._report_tts_failure("tts_status")
                return
            audio = await asyncio.wait_for(response.read(), timeout=TTS_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        self._report_tts_failure("tts_timeout")
        return
    except Exception:
        self._report_tts_failure("tts_exception")
        return

    if not audio:
        self._report_tts_failure("tts_unavailable")
        return
    await self.transport.send_audio(audio)
```

Retain the existing audio framing/chunking code around `transport.send_audio`; only request construction and guard order change.

- [ ] **Step 5: Run GREEN and commit**

```bash
cd Kavya
pytest -q tests/test_english_voice_profile.py tests/test_smartpbx_server.py -k "voice or tts or smartpbx"
git add server.py smartpbx_session.py tests/test_smartpbx_server.py tests/test_english_voice_profile.py
git commit -m "fix(kavya): use protected voice for SmartPBX English TTS"
```

Expected: SmartPBX English succeeds with an empty legacy voice ID, missing protected profile fails closed, and retained callers preserve the legacy guard.

---

### Task 4: Parse Dialog protocol v0.6 without treating unsupported events as valid traffic

**Files:**
- Modify: `Kavya/smartpbx_protocol.py`
- Modify: `Kavya/smartpbx_gateway.py`
- Test: `Kavya/tests/test_smartpbx_protocol.py`
- Test: `Kavya/tests/test_smartpbx_gateway.py`

**Interfaces:**
- Produces: `StartEvent(account_id: str, stream_id: str, media_format: MediaFormat)`
- Produces: `UnsupportedEvent(event_name: str)` where `event_name` is validated then discarded before diagnostics
- Produces: `parse_dialog_event(payload: Mapping[str, object]) -> DialogEvent`

- [ ] **Step 1: Add failing v0.6 and unsupported-event tests**

```python
@pytest.mark.parametrize("key", ["accountId", "account_id"])
def test_v06_start_accepts_account_identifier_alias(key):
    payload = {
        "event": "start",
        "start": {
            key: "account-test-marker",
            "streamId": "stream-test-marker",
            "mediaFormat": {
                "encoding": "audio/x-mulaw",
                "sampleRate": 8000,
                "channels": 1,
            },
        },
    }
    event = parse_dialog_event(payload)
    assert event.account_id == "account-test-marker"
    assert event.media_format.encoding == "audio/x-mulaw"
    assert event.media_format.sample_rate == 8000
    assert event.media_format.channels == 1


def test_unknown_event_is_explicitly_unsupported():
    event = parse_dialog_event({"event": "future-event", "value": "must-not-log"})
    assert isinstance(event, UnsupportedEvent)
    assert event.event_name == "future-event"
```

- [ ] **Step 2: Run RED**

```bash
cd Kavya
pytest -q tests/test_smartpbx_protocol.py -k "v06 or unsupported"
```

Expected: current parsing returns `UnknownEvent` or misses the v0.6 aliases.

- [ ] **Step 3: Add exact protocol parsing**

```python
@dataclass(frozen=True)
class UnsupportedEvent:
    event_name: str


def _required_text(mapping: Mapping[str, object], *keys: str) -> str:
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    raise ProtocolViolation("required protocol field is missing")


def parse_dialog_event(payload: Mapping[str, object]) -> DialogEvent:
    event_name = _required_text(payload, "event")
    if event_name == "connected":
        return ConnectedEvent()
    if event_name == "start":
        start = require_mapping(payload.get("start"))
        media = require_mapping(start.get("mediaFormat") or start.get("media_format"))
        return StartEvent(
            account_id=_required_text(start, "accountId", "account_id"),
            stream_id=_required_text(start, "streamId", "stream_id"),
            media_format=MediaFormat(
                encoding=_required_text(media, "encoding"),
                sample_rate=require_int(media, "sampleRate", "sample_rate"),
                channels=require_int(media, "channels"),
            ),
        )
    if event_name == "media":
        return MediaEvent.from_payload(payload)
    if event_name == "stop":
        return StopEvent()
    return UnsupportedEvent(event_name=event_name)
```

- [ ] **Step 4: Make both gateway phases reject `UnsupportedEvent`**

```python
# pre-start inside _receive_start
if isinstance(event, UnsupportedEvent):
    raise ProtocolViolation("unsupported_event")

# post-start inside the media loop
if isinstance(event, UnsupportedEvent):
    raise ProtocolViolation("unsupported_event")
```

The gateway must never increment liveness counters for unsupported events.

- [ ] **Step 5: Run GREEN and commit**

```bash
cd Kavya
pytest -q tests/test_smartpbx_protocol.py tests/test_smartpbx_gateway.py
git add smartpbx_protocol.py smartpbx_gateway.py tests/test_smartpbx_protocol.py tests/test_smartpbx_gateway.py
git commit -m "fix(kavya): align SmartPBX protocol with Dialog v0.6"
```

Expected: v0.6 events parse, malformed media fails, and unsupported events are rejected before and after start.

---

### Task 5: Implement finite, privacy-safe lifecycle diagnostics including TTS attribution

**Files:**
- Modify: `Kavya/smartpbx_gateway.py`
- Modify: `Kavya/smartpbx_session.py`
- Modify: `Kavya/server.py`
- Test: `Kavya/tests/test_smartpbx_gateway.py`
- Test: `Kavya/tests/test_smartpbx_server.py`

**Interfaces:**
- Produces: `LifecycleStage` and `FailureClass` enums
- Produces: `DiagnosticSink = Callable[[LifecycleStage, FailureClass], None]`
- Changes: `SessionFactory(context, transport, diagnostic_sink)`
- Preserves failure classes: `authentication`, `disabled`, `capacity`, `account_mismatch`, `start_required`, `duplicate_start`, `connected_after_start`, `start_timeout`, `idle_timeout`, `unsupported_event`, `protocol`, `stt_unavailable`, `stt_queue_overflow`, `tts_unavailable`, `tts_status`, `tts_timeout`, `tts_exception`, `pipeline`, `internal_error`, `session_cleanup`, `transport_cleanup`, `lease_cleanup`

- [ ] **Step 1: Add failing finite-schema tests for every phase**

```python
EXPECTED_FAILURES = {
    "authentication", "disabled", "capacity", "account_mismatch",
    "start_required", "duplicate_start", "connected_after_start",
    "start_timeout", "idle_timeout", "unsupported_event", "protocol",
    "stt_unavailable", "stt_queue_overflow", "tts_unavailable",
    "tts_status", "tts_timeout", "tts_exception", "pipeline",
    "internal_error", "session_cleanup", "transport_cleanup", "lease_cleanup",
}


def assert_safe_diagnostic(record):
    assert set(record) <= {"event", "stage", "failure_class"}
    assert record["event"] == "smartpbx_call_lifecycle"
    assert record["stage"] in {member.value for member in LifecycleStage}
    assert record["failure_class"] in EXPECTED_FAILURES
    serialized = json.dumps(record)
    for forbidden in (
        "caller", "phone", "utterance", "account-test-marker",
        "stream-test-marker", "protected-test-marker", "api-test-marker",
    ):
        assert forbidden not in serialized


@pytest.mark.parametrize(
    ("wire_events", "expected"),
    [
        ([{"event": "future-before-start", "value": "caller"}], "unsupported_event"),
        ([valid_start(), {"event": "future-after-start", "value": "caller"}], "unsupported_event"),
        ([{"event": "media"}], "start_required"),
        ([valid_start(), valid_start()], "duplicate_start"),
        ([valid_start(), {"event": "connected"}], "connected_after_start"),
    ],
)
@pytest.mark.asyncio
async def test_gateway_emits_bounded_diagnostic_for_protocol_outcomes(
    wire_events, expected, diagnostic_records
):
    await run_gateway(wire_events, diagnostic_records)
    assert diagnostic_records[-1]["failure_class"] == expected
    assert_safe_diagnostic(diagnostic_records[-1])
```

Add focused tests using existing fakes for `authentication`, `disabled`, `capacity`, `account_mismatch`, `start_timeout`, `idle_timeout`, each cleanup failure, and each TTS callback class. Every test calls `assert_safe_diagnostic`.

- [ ] **Step 2: Run RED**

```bash
cd Kavya
pytest -q tests/test_smartpbx_gateway.py tests/test_smartpbx_server.py -k "diagnostic or protocol_outcomes or tts"
```

Expected: unsupported-event phases or TTS failures lack a finite stage/class, and existing logs contain unbounded identifiers.

- [ ] **Step 3: Define the complete finite diagnostics interface**

```python
# Kavya/smartpbx_gateway.py
from enum import Enum
from typing import Callable


class LifecycleStage(str, Enum):
    ADMISSION = "admission"
    START = "start"
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

_STAGE_BY_FAILURE = {
    FailureClass.AUTHENTICATION: LifecycleStage.ADMISSION,
    FailureClass.DISABLED: LifecycleStage.ADMISSION,
    FailureClass.CAPACITY: LifecycleStage.ADMISSION,
    FailureClass.ACCOUNT_MISMATCH: LifecycleStage.START,
    FailureClass.START_REQUIRED: LifecycleStage.START,
    FailureClass.DUPLICATE_START: LifecycleStage.ACTIVE,
    FailureClass.CONNECTED_AFTER_START: LifecycleStage.ACTIVE,
    FailureClass.START_TIMEOUT: LifecycleStage.START,
    FailureClass.IDLE_TIMEOUT: LifecycleStage.ACTIVE,
    FailureClass.UNSUPPORTED_EVENT: LifecycleStage.ACTIVE,
    FailureClass.PROTOCOL: LifecycleStage.ACTIVE,
    FailureClass.STT_UNAVAILABLE: LifecycleStage.STT,
    FailureClass.STT_QUEUE_OVERFLOW: LifecycleStage.STT,
    FailureClass.TTS_UNAVAILABLE: LifecycleStage.TTS,
    FailureClass.TTS_STATUS: LifecycleStage.TTS,
    FailureClass.TTS_TIMEOUT: LifecycleStage.TTS,
    FailureClass.TTS_EXCEPTION: LifecycleStage.TTS,
    FailureClass.PIPELINE: LifecycleStage.PIPELINE,
    FailureClass.INTERNAL_ERROR: LifecycleStage.PIPELINE,
    FailureClass.SESSION_CLEANUP: LifecycleStage.CLEANUP,
    FailureClass.TRANSPORT_CLEANUP: LifecycleStage.CLEANUP,
    FailureClass.LEASE_CLEANUP: LifecycleStage.CLEANUP,
}


def emit_diagnostic(logger, failure: FailureClass) -> None:
    logger.info(
        "smartpbx_call_lifecycle",
        extra={
            "event": "smartpbx_call_lifecycle",
            "stage": _STAGE_BY_FAILURE[failure].value,
            "failure_class": failure.value,
        },
    )
```

No diagnostic function accepts arbitrary details or identifiers.

- [ ] **Step 4: Wire every gateway outcome, including pre-start and post-start unsupported events**

Use one local sink and explicit failure assignment:

```python
def diagnostic_sink(stage: LifecycleStage, failure: FailureClass) -> None:
    if _STAGE_BY_FAILURE[failure] is not stage:
        raise ValueError("diagnostic stage does not match failure class")
    emit_diagnostic(logger, failure)
```

Before accepting the socket:

```python
if not settings.enabled:
    emit_diagnostic(logger, FailureClass.DISABLED)
    await websocket.close(code=4403)
    return
if not authenticate(websocket):
    emit_diagnostic(logger, FailureClass.AUTHENTICATION)
    await websocket.close(code=4401)
    return
lease = capacity.try_acquire()
if lease is None:
    emit_diagnostic(logger, FailureClass.CAPACITY)
    await websocket.close(code=4429)
    return
```

In `_receive_start`, set exact failures before raising:

```python
if isinstance(event, UnsupportedEvent):
    emit_diagnostic(logger, FailureClass.UNSUPPORTED_EVENT)
    raise ProtocolViolation("unsupported_event")
if isinstance(event, MediaEvent):
    emit_diagnostic(logger, FailureClass.START_REQUIRED)
    raise ProtocolViolation("start_required")
```

After start:

```python
if start.account_id != settings.account_id:
    emit_diagnostic(logger, FailureClass.ACCOUNT_MISMATCH)
    raise ProtocolViolation("account_mismatch")
if isinstance(event, StartEvent):
    emit_diagnostic(logger, FailureClass.DUPLICATE_START)
    raise ProtocolViolation("duplicate_start")
if isinstance(event, ConnectedEvent):
    emit_diagnostic(logger, FailureClass.CONNECTED_AFTER_START)
    raise ProtocolViolation("connected_after_start")
if isinstance(event, UnsupportedEvent):
    emit_diagnostic(logger, FailureClass.UNSUPPORTED_EVENT)
    raise ProtocolViolation("unsupported_event")
```

Timeout and cleanup branches must retain their distinct classes:

```python
except StartTimeout:
    emit_diagnostic(logger, FailureClass.START_TIMEOUT)
except IdleTimeout:
    emit_diagnostic(logger, FailureClass.IDLE_TIMEOUT)
except ProtocolViolation as error:
    if str(error) not in {
        "account_mismatch", "start_required", "duplicate_start",
        "connected_after_start", "unsupported_event",
    }:
        emit_diagnostic(logger, FailureClass.PROTOCOL)
except PipelineFailure:
    emit_diagnostic(logger, FailureClass.PIPELINE)
except Exception:
    emit_diagnostic(logger, FailureClass.INTERNAL_ERROR)
finally:
    try:
        if session is not None:
            await session.close()
    except Exception:
        emit_diagnostic(logger, FailureClass.SESSION_CLEANUP)
    try:
        await transport.close()
    except Exception:
        emit_diagnostic(logger, FailureClass.TRANSPORT_CLEANUP)
    try:
        lease.release()
    except Exception:
        emit_diagnostic(logger, FailureClass.LEASE_CLEANUP)
```

Update the exact factory contract and invocation:

```python
SessionFactory = Callable[
    [CallContext, SmartPBXMediaTransport, DiagnosticSink],
    Awaitable[GatewaySession],
]

session = await session_factory(context, transport, diagnostic_sink)
```

- [ ] **Step 5: Wire TTS attribution through the server pipeline**

```python
# Kavya/server.py, inside MediaStreamSession
_TTS_FAILURES = {
    "tts_unavailable": FailureClass.TTS_UNAVAILABLE,
    "tts_status": FailureClass.TTS_STATUS,
    "tts_timeout": FailureClass.TTS_TIMEOUT,
    "tts_exception": FailureClass.TTS_EXCEPTION,
}


def _report_tts_failure(self, name: str) -> None:
    failure = self._TTS_FAILURES[name]
    if self.diagnostic_sink is not None:
        self.diagnostic_sink(LifecycleStage.TTS, failure)
```

Pass `diagnostic_sink` from gateway factory to `KavyaSmartPBXSession`, then to `MediaStreamSession` as shown in Task 3. STT queue/unavailable branches call the same sink with `LifecycleStage.STT` and their respective enums. No pipeline log includes exception text or request data.

- [ ] **Step 6: Run GREEN, scan logs, and commit**

```bash
cd Kavya
pytest -q tests/test_smartpbx_gateway.py tests/test_smartpbx_server.py
rg -n "session_id|call_id|account_id|caller|utterance|voice_id|api_key" smartpbx_gateway.py smartpbx_session.py
```

Expected: all tests pass. Any `rg` hit is protocol processing or configuration only; none occurs in logger argument dictionaries or diagnostic payloads.

```bash
git add smartpbx_gateway.py smartpbx_session.py server.py tests/test_smartpbx_gateway.py tests/test_smartpbx_server.py
git commit -m "feat(kavya): add bounded SmartPBX lifecycle diagnostics"
```

---

### Task 6: Prove provider tools, RAG, booking, fillers, reprompt, and barge-in parity

**Files:**
- Modify: `Kavya/server.py:2594,3014,3370-3475`
- Test: `Kavya/tests/test_smartpbx_server.py`

**Interfaces:**
- Produces: `select_provider_tools(provider: str) -> list[dict[str, object]]`
- Produces: `ToolCall(name: str, arguments: dict[str, object])`
- Produces: `execute_english_tool_batch(calls, speak, execute) -> list[ToolOutcome]`
- Preserves: Claude `get_tools()`, Gemini `get_tools_gemini()`, OpenAI `get_tools_openai()`

- [ ] **Step 1: Add failing provider schema tests with concrete sentinels**

```python
@pytest.mark.parametrize(
    ("provider", "loader"),
    [
        ("claude", "get_tools"),
        ("gemini", "get_tools_gemini"),
        ("openai", "get_tools_openai"),
    ],
)
def test_provider_uses_its_native_tool_schema(monkeypatch, provider, loader):
    sentinel = [{"provider": provider}]
    monkeypatch.setattr(server, "get_tools", lambda: [{"provider": "wrong-claude"}])
    monkeypatch.setattr(server, "get_tools_gemini", lambda: [{"provider": "wrong-gemini"}])
    monkeypatch.setattr(server, "get_tools_openai", lambda: [{"provider": "wrong-openai"}])
    monkeypatch.setattr(server, loader, lambda: sentinel)
    assert server.select_provider_tools(provider) is sentinel
```

- [ ] **Step 2: Add genuine RAG and booking/filler behavior tests**

```python
@pytest.mark.asyncio
async def test_smartpbx_utterance_injects_retrieved_context(monkeypatch):
    captured = {}
    session = make_media_session(smartpbx_english=True)
    monkeypatch.setattr(server, "retrieve_context", lambda text: "Check-in begins at 3 PM")

    async def fake_run_llm(history, tools):
        captured["history"] = history
        captured["tools"] = tools
        return "You can check in from 3 PM"

    monkeypatch.setattr(session, "_run_selected_provider", fake_run_llm)
    await session.process_utterance("When is check-in?")

    assert "[Reference context: Check-in begins at 3 PM]" in captured["history"][-1]["content"]
    assert captured["tools"] == server.select_provider_tools(session.provider)


@pytest.mark.asyncio
async def test_booking_executes_after_intent_specific_filler():
    events = []

    async def speak(text):
        events.append(("speak", text))

    async def execute(name, arguments):
        events.append(("execute", name, arguments))
        return {"booking_id": "booking-test-marker"}

    outcomes = await server.execute_english_tool_batch(
        [server.ToolCall("create_booking", {"room_type": "deluxe"})],
        speak,
        execute,
    )

    assert events[0] == ("speak", server.TOOL_FILLERS["create_booking"])
    assert events[1][0:2] == ("execute", "create_booking")
    assert outcomes[0].ok is True
    assert outcomes[0].result == {"booking_id": "booking-test-marker"}


@pytest.mark.asyncio
async def test_tool_failure_uses_default_filler_then_recovers():
    spoken = []

    async def speak(text):
        spoken.append(text)

    async def execute(name, arguments):
        raise RuntimeError("provider detail must not reach caller")

    outcomes = await server.execute_english_tool_batch(
        [server.ToolCall("unknown_tool", {})], speak, execute
    )

    assert spoken == [server.DEFAULT_FILLER]
    assert outcomes == [server.ToolOutcome(
        name="unknown_tool",
        ok=False,
        result={"error": "I could not complete that request. Please try again."},
    )]
```

- [ ] **Step 3: Add genuine reprompt and barge-in assertions**

```python
@pytest.mark.asyncio
async def test_silence_reprompt_is_spoken_once_before_timeout():
    session = make_media_session(smartpbx_english=True)
    await session.handle_silence_timeout()
    assert session.transport.spoken == [server.REPROMPT_TEXT]
    assert session.reprompt_count == 1


@pytest.mark.asyncio
async def test_barge_in_cancels_tts_and_clears_outbound_audio():
    session = make_media_session(smartpbx_english=True)
    session.tts_task = asyncio.create_task(asyncio.sleep(60))
    await session.handle_barge_in()
    assert session.tts_task.cancelled()
    assert session.transport.clear_calls == 1
```

- [ ] **Step 4: Run RED**

```bash
cd Kavya
pytest -q tests/test_smartpbx_server.py -k "native_tool_schema or retrieved_context or booking_executes or tool_failure or reprompt or barge"
```

Expected: at least the provider selector and shared English tool executor are absent, and current generic filler behavior fails ordering assertions.

- [ ] **Step 5: Implement exact provider selection and shared English tool execution**

```python
from dataclasses import dataclass
from typing import Awaitable, Callable


def select_provider_tools(provider: str) -> list[dict[str, object]]:
    if provider == "claude":
        return get_tools()
    if provider == "gemini":
        return get_tools_gemini()
    if provider == "openai":
        return get_tools_openai()
    raise ValueError("unsupported LLM provider")


@dataclass(frozen=True)
class ToolCall:
    name: str
    arguments: dict[str, object]


@dataclass(frozen=True)
class ToolOutcome:
    name: str
    ok: bool
    result: dict[str, object]


async def execute_english_tool_batch(
    calls: list[ToolCall],
    speak: Callable[[str], Awaitable[None]],
    execute: Callable[[str, dict[str, object]], Awaitable[dict[str, object]]],
) -> list[ToolOutcome]:
    outcomes: list[ToolOutcome] = []
    for call in calls:
        await speak(TOOL_FILLERS.get(call.name, DEFAULT_FILLER))
        try:
            result = await execute(call.name, call.arguments)
        except Exception:
            outcomes.append(ToolOutcome(
                name=call.name,
                ok=False,
                result={"error": "I could not complete that request. Please try again."},
            ))
        else:
            outcomes.append(ToolOutcome(name=call.name, ok=True, result=result))
    return outcomes
```

Use `select_provider_tools(self.provider)` at the existing `MediaStreamSession` provider boundary. Normalize each provider's tool calls to `ToolCall`, invoke `execute_english_tool_batch` for SmartPBX English, and convert each `ToolOutcome` back to that provider's result envelope. The existing Twilio/general branch keeps its provider-specific executor.

Keep the existing retrieval path active before provider dispatch:

```python
kb_context = retrieve_context(text)
user_content = text
if kb_context:
    user_content = f"[Reference context: {kb_context}]\n\nGuest: {text}"
self.history.append({"role": "user", "content": user_content})
tools = select_provider_tools(self.provider)
response = await self._run_selected_provider(self.history, tools)
```

Keep the existing `handle_silence_timeout` and `handle_barge_in` implementations; change them only if the new behavior tests reveal an actual SmartPBX adapter gap.

- [ ] **Step 6: Run GREEN and the full behavioral suite**

```bash
cd Kavya
pytest -q tests/test_smartpbx_server.py
pytest -q tests/test_tools.py tests/test_booking_api.py tests/test_knowledge_base.py
```

Expected: provider schemas are exact, retrieved context reaches the provider, booking executes only after its intent-specific filler, failure recovery is safe, reprompt fires, and barge-in cancels/clears audio.

- [ ] **Step 7: Commit**

```bash
git add server.py tests/test_smartpbx_server.py
git commit -m "test(kavya): prove SmartPBX call behavior parity"
```

---

### Task 7: Lock transfer off and document safe deployment, drain, and rollback

**Files:**
- Modify: `Kavya/.env.example`
- Modify: `Kavya/docker-compose.yml`
- Modify: `Kavya/docs/smartpbx-runbook.md`
- Test: `Kavya/tests/test_smartpbx_deployment.py`
- Test: `Kavya/tests/test_smartpbx_mcp.py`

**Interfaces:**
- Consumes: existing `DialogMCPSettings.from_env`
- Guarantees: endpoint-only configuration produces `enabled is False`
- Guarantees: base destinations parse to `{}` and no protected MCP value is documented

- [ ] **Step 1: Add failing disabled-transfer configuration tests**

```python
# Kavya/tests/test_smartpbx_deployment.py
from pathlib import Path


def test_example_keeps_mcp_transfer_protected_fields_empty():
    example = Path("Kavya/.env.example").read_text()
    assert "SMARTPBX_API_KEY=" in example
    assert "SMARTPBX_MCP_ACCOUNT_HEADER=" in example
    assert "SMARTPBX_TRANSFER_DESTINATIONS={}" in example
    assert "SMARTPBX_TRANSFER_ENABLED=false" in example


def test_compose_does_not_default_transfer_on():
    compose = Path("Kavya/docker-compose.yml").read_text()
    assert "SMARTPBX_TRANSFER_ENABLED=${SMARTPBX_TRANSFER_ENABLED:-false}" in compose
    assert "SMARTPBX_TRANSFER_DESTINATIONS=${SMARTPBX_TRANSFER_DESTINATIONS:-{}}" in compose
```

```python
# Kavya/tests/test_smartpbx_mcp.py

def test_endpoint_alone_cannot_enable_transfer():
    settings = DialogMCPSettings.from_env({
        "SMARTPBX_MCP_ENDPOINT": "https://api.dialog.example/mcp",
        "SMARTPBX_API_KEY": "",
        "SMARTPBX_MCP_ACCOUNT_HEADER": "",
        "SMARTPBX_TRANSFER_DESTINATIONS": "{}",
        "SMARTPBX_TRANSFER_ENABLED": "false",
    })
    assert settings.enabled is False
    assert settings.destinations == {}
```

- [ ] **Step 2: Run RED**

```bash
cd Kavya
pytest -q tests/test_smartpbx_deployment.py tests/test_smartpbx_mcp.py -k "transfer or endpoint_alone or example_keeps"
```

Expected: one or more template/default/runbook contracts do not yet state the coherent disabled base state.

- [ ] **Step 3: Set the exact nonsecret base template and compose defaults**

```dotenv
# Kavya/.env.example
SMARTPBX_MCP_ENDPOINT=https://api.dialog.example/mcp
SMARTPBX_API_KEY=
SMARTPBX_MCP_ACCOUNT_HEADER=
SMARTPBX_TRANSFER_DESTINATIONS={}
SMARTPBX_TRANSFER_ENABLED=false
```

```yaml
# Kavya/docker-compose.yml, kavya-smartpbx.environment
SMARTPBX_MCP_ENDPOINT: ${SMARTPBX_MCP_ENDPOINT:-https://api.dialog.example/mcp}
SMARTPBX_API_KEY: ${SMARTPBX_API_KEY:-}
SMARTPBX_MCP_ACCOUNT_HEADER: ${SMARTPBX_MCP_ACCOUNT_HEADER:-}
SMARTPBX_TRANSFER_DESTINATIONS: ${SMARTPBX_TRANSFER_DESTINATIONS:-{}}
SMARTPBX_TRANSFER_ENABLED: ${SMARTPBX_TRANSFER_ENABLED:-false}
```

The endpoint is nonsecret documentation only. It cannot enable transfer without all protected fields and the explicit boolean.

- [ ] **Step 4: Replace the runbook activation section with an explicit MCP gate**

```markdown
## Transfer remains disabled

Do not populate `SMARTPBX_API_KEY`, `SMARTPBX_MCP_ACCOUNT_HEADER`, or
`SMARTPBX_TRANSFER_DESTINATIONS`, and do not set
`SMARTPBX_TRANSFER_ENABLED=true`, during this call-parity rollout. The
nonsecret endpoint may remain documented, but endpoint presence alone does
not enable transfer. `/smartpbx/status` must report `transfer_enabled=false`.

Transfer activation is blocked until
`docs/superpowers/plans/2026-08-07-kavya-smartpbx-mcp-handover.md` has
independently passed its carrier-outcome contract and failsafe gate. That
later rollout must supply protected values through the deployment secret
store; never paste them into this repository or this runbook.
```

- [ ] **Step 5: Replace rollback instructions with route-first drain order**

```markdown
## Withdraw and rollback without dropping calls

1. Withdraw the Kavya route in the Dialog dashboard/carrier routing layer, or
   restore the previously approved fallback route. Verify that new calls take
   the fallback before changing the Kavya service.
2. Poll the authenticated status endpoint until `active_sessions` is zero:

   ```bash
   while :; do
     status=$(curl --fail --silent --show-error \
       -H "Authorization: Bearer $SMARTPBX_STATUS_TOKEN" \
       https://kavya.example/smartpbx/status)
     active=$(printf '%s' "$status" | jq -er '.active_sessions')
     printf 'active_sessions=%s\n' "$active"
     [ "$active" -eq 0 ] && break
     sleep 5
   done
   ```

   Keep the withdrawn Kavya service running until the drain deadline. If the
   count is not zero at the deadline, do not stop it; escalate to the incident
   owner and Dialog carrier owner.
3. Only after `active_sessions=0`, stop the reviewed service version:

   ```bash
   set -euo pipefail
   cd /opt/kavya
   SMARTPBX_IMAGE_TAG="$REVIEWED_CI_SHORT_SHA" docker compose --env-file .env.smartpbx --profile smartpbx stop kavya-smartpbx
   ```
4. Revert or redeploy the last approved image only after the service is
   drained. Re-run status and fallback-route checks before reopening traffic.
```

- [ ] **Step 6: Run GREEN, scan for leaked values, and commit**

```bash
cd Kavya
pytest -q tests/test_smartpbx_deployment.py tests/test_smartpbx_mcp.py
rg -n "SMARTPBX_(API_KEY|MCP_ACCOUNT_HEADER|TRANSFER_DESTINATIONS)=" .env.example docker-compose.yml docs/smartpbx-runbook.md
```

Expected: tests pass; protected example values are blank, destinations are `{}`, and no actual key/header/destination appears.

```bash
git add .env.example docker-compose.yml docs/smartpbx-runbook.md tests/test_smartpbx_deployment.py tests/test_smartpbx_mcp.py
git commit -m "docs(kavya): lock transfer off and make rollback drain-safe"
```

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
rg -n "KAVYA_ENGLISH_VOICE_ID|SMARTPBX_API_KEY|SMARTPBX_MCP_ACCOUNT_HEADER" . --glob '!tests/**'
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
