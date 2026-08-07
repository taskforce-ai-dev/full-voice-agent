# Kavya SmartPBX Call Parity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the English Dialog SmartPBX call path use Kavya's protected canonical English voice and production English behavior while preserving Dialog `g711_ulaw`/8000, privacy boundaries, Twilio, Flico, and disabled transfer.

**Architecture:** A focused `english_voice_profile.py` owns the protected English voice identity and constructs both renderer selections. ConversationRelay consumes the profile voice plus flash suffix; direct ElevenLabs TTS consumes the same profile with `output_format=ulaw_8000` in the URL query and `model_id=eleven_flash_v2_5` in the JSON body. The existing `MediaStreamSession` remains the only English conversation pipeline. Dialog v06 parsing stays closed-world, and lifecycle diagnostics use finite, non-sensitive classes.

**Tech Stack:** Python 3.11, FastAPI/WebSockets, pytest/pytest-asyncio, httpx, ElevenLabs streaming TTS, Twilio ConversationRelay, Dialog SmartPBX, Docker Compose, Nginx, GitHub Actions.

## Global Constraints

- Keep Flico's container, configuration, and running path intact.
- Use TDD red-green evidence for behavior changes and review before deployment.
- Keep secrets, MCP keys, voice IDs, call identifiers, and customer data out of Git, diagnostics, dashboard events, status output, and test fixtures.
- Secret rotation, DID routing beyond the temporary sole-DID verification, Dialog credential changes, carrier contract decisions, and any non-English Dialog language selection require asking first.
- Never remove Twilio, enable MCP transfer before its gates, send both account headers, invoke `call_tool` during the MCP diagnostic, switch headers without the specified deterministic 4xx, or weaken the g711 ulaw admission contract.

---

## File map and task order

- Create `Kavya/english_voice_profile.py` and `Kavya/tests/test_english_voice_profile.py`.
- Modify `Kavya/server.py:134-140,489-530,1751-1752,1814-1817,2571-2648,3485-3577`.
- Modify `Kavya/smartpbx_session.py:17-224`, `Kavya/smartpbx_protocol.py:15-245`, and `Kavya/smartpbx_gateway.py:146-356`.
- Modify `Kavya/.env.example:18-21,178-202`, `Kavya/docker-compose.yml:88-155`, `Kavya/SMARTPBX_RUNBOOK.md:76-257`.
- Modify `Kavya/tests/test_smartpbx_protocol.py`, `test_smartpbx_gateway.py`, `test_smartpbx_server.py`, `test_smartpbx_transport.py`, `test_smartpbx_deployment.py`, and `test_smartpbx_provider_handover.py`.

Tasks are ordered RED then GREEN. No test is described as failing “before” an already-completed later task. `Kavya/smartpbx_mcp.py`, `smartpbx_handover.py`, `tools.py`, all Flico paths, dashboard routing, and the deployed Twilio service are out of scope. Transfer remains disabled throughout.

### Task 1: Canonical protected profile and correct direct request builder

**Files:**
- Create: `Kavya/english_voice_profile.py`
- Create: `Kavya/tests/test_english_voice_profile.py`

**Interfaces:**
- `VOICE_ENV_KEY: Final[str] = "KAVYA_EN_ELEVENLABS_VOICE_ID"`
- `EnglishVoiceProfile(voice_id: str, model_id: str, output_format: str, voice_settings: Mapping[str, float | bool])`
- `load_english_voice_profile(environ: Mapping[str, str]) -> EnglishVoiceProfile`
- `build_direct_stream_request(profile: EnglishVoiceProfile, text: str) -> tuple[str, dict[str, object]]`
- Voice identifiers are protected values; tests use only the literal marker `test-only-voice-marker`, which is not a deployable identifier.

- [ ] **Step 1: Write the complete failing tests**

~~~python
# Kavya/tests/test_english_voice_profile.py
from urllib.parse import parse_qs, urlparse
import pytest

from english_voice_profile import (
    VOICE_ENV_KEY,
    build_direct_stream_request,
    load_english_voice_profile,
)


def test_profile_is_fail_closed_fixed_and_redacted():
    profile = load_english_voice_profile({VOICE_ENV_KEY: "test-only-voice-marker"})
    assert profile.voice_id == "test-only-voice-marker"
    assert profile.model_id == "eleven_flash_v2_5"
    assert profile.output_format == "ulaw_8000"
    assert profile.voice_settings == {
        "stability": 0.5,
        "similarity_boost": 0.75,
        "style": 0.0,
        "use_speaker_boost": True,
    }
    assert "test-only-voice-marker" not in repr(profile)


@pytest.mark.parametrize("value", [None, "", "   "])
def test_profile_rejects_missing_or_blank_protected_value(value):
    environ = {} if value is None else {VOICE_ENV_KEY: value}
    with pytest.raises(ValueError, match="canonical English voice is required"):
        load_english_voice_profile(environ)


def test_direct_request_puts_format_only_in_query_and_model_only_in_body():
    profile = load_english_voice_profile({VOICE_ENV_KEY: "test-only-voice-marker"})
    url, body = build_direct_stream_request(profile, "Hello")
    parsed = urlparse(url)
    assert parsed.path.endswith("/test-only-voice-marker/stream")
    assert parse_qs(parsed.query) == {"output_format": ["ulaw_8000"]}
    assert body == {
        "text": "Hello",
        "model_id": "eleven_flash_v2_5",
        "voice_settings": dict(profile.voice_settings),
    }
    assert "output_format" not in body
~~~

- [ ] **Step 2: Run RED**

Run: `cd Kavya && pytest -q tests/test_english_voice_profile.py`

Expected: FAIL during collection with `ModuleNotFoundError: No module named 'english_voice_profile'`.

- [ ] **Step 3: Add the complete minimal implementation**

~~~python
# Kavya/english_voice_profile.py
from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Final, Mapping
from urllib.parse import quote, urlencode

VOICE_ENV_KEY: Final[str] = "KAVYA_EN_ELEVENLABS_VOICE_ID"
ELEVENLABS_STREAM_ROOT: Final[str] = "https://api.elevenlabs.io/v1/text-to-speech"


@dataclass(frozen=True)
class EnglishVoiceProfile:
    voice_id: str = field(repr=False)
    model_id: str = "eleven_flash_v2_5"
    output_format: str = "ulaw_8000"
    voice_settings: Mapping[str, float | bool] = field(
        default_factory=lambda: MappingProxyType({
            "stability": 0.5,
            "similarity_boost": 0.75,
            "style": 0.0,
            "use_speaker_boost": True,
        })
    )


def load_english_voice_profile(environ: Mapping[str, str]) -> EnglishVoiceProfile:
    value = environ.get(VOICE_ENV_KEY, "")
    if not isinstance(value, str) or not value.strip():
        raise ValueError("canonical English voice is required")
    return EnglishVoiceProfile(voice_id=value.strip())


def build_direct_stream_request(
    profile: EnglishVoiceProfile, text: str
) -> tuple[str, dict[str, object]]:
    path = f"{ELEVENLABS_STREAM_ROOT}/{quote(profile.voice_id, safe='')}/stream"
    url = f"{path}?{urlencode({'output_format': profile.output_format})}"
    body: dict[str, object] = {
        "text": text,
        "model_id": profile.model_id,
        "voice_settings": dict(profile.voice_settings),
    }
    return url, body
~~~

- [ ] **Step 4: Run GREEN**

Run: `cd Kavya && pytest -q tests/test_english_voice_profile.py`

Expected: `5 passed`; the URL query is `ulaw_8000`, the body contains `eleven_flash_v2_5`, and the body excludes `output_format`.

- [ ] **Step 5: Commit atomically**

Run: `git add Kavya/english_voice_profile.py Kavya/tests/test_english_voice_profile.py && git diff --cached --check && git commit -m "feat(kavya): add canonical English voice profile"`

Expected: one commit containing only the module and its tests.

### Task 2: ConversationRelay derives English voice from the canonical profile

**Files:**
- Modify: `Kavya/server.py:134-140,489-530,1751-1752,1814-1817`
- Modify: `Kavya/tests/test_english_voice_profile.py`

**Interfaces:**
- Consumes Task 1 `EnglishVoiceProfile` and `load_english_voice_profile`.
- Produces `english_conversation_relay_config(profile: EnglishVoiceProfile) -> dict[str, str]`.
- Existing `_build_conversation_relay_twiml(host: str, lang: str, config: dict[str, str]) -> str` is consumed unchanged.

- [ ] **Step 1: Append the complete failing test**

~~~python
# append to Kavya/tests/test_english_voice_profile.py
import server
from english_voice_profile import EnglishVoiceProfile


def test_relay_config_uses_profile_flash_voice_without_changing_non_english():
    before = dict(server.LANGUAGE_CONFIGS["si"])
    config = server.english_conversation_relay_config(
        EnglishVoiceProfile(voice_id="test-only-voice-marker")
    )
    assert config == {
        "tts_provider": "ElevenLabs",
        "voice": "test-only-voice-marker-flash_v2_5",
        "language": "en-US",
        "transcription_language": server.CR_TRANSCRIPTION_LANGUAGE_EN,
        "hints": server.CR_HINTS_EN,
        "welcome_greeting": "Welcome to Hatton Hills! I'm Kavya, how can I help you today?",
        "extra_attrs": '        elevenlabsTextNormalization="on"\n',
    }
    assert server.LANGUAGE_CONFIGS["si"] == before
~~~

- [ ] **Step 2: Run RED**

Run: `cd Kavya && pytest -q tests/test_english_voice_profile.py::test_relay_config_uses_profile_flash_voice_without_changing_non_english`

Expected: FAIL with `AttributeError` because `english_conversation_relay_config` does not exist.

- [ ] **Step 3: Add the complete minimal selection code and replace both call sites**

~~~python
# server.py imports/constants section
from english_voice_profile import EnglishVoiceProfile, load_english_voice_profile


def _english_voice_profile() -> EnglishVoiceProfile:
    return load_english_voice_profile(os.environ)


def english_conversation_relay_config(
    profile: EnglishVoiceProfile,
) -> dict[str, str]:
    return {
        "tts_provider": "ElevenLabs",
        "voice": f"{profile.voice_id}-flash_v2_5",
        "language": "en-US",
        "transcription_language": CR_TRANSCRIPTION_LANGUAGE_EN,
        "hints": CR_HINTS_EN,
        "welcome_greeting": "Welcome to Hatton Hills! I'm Kavya, how can I help you today?",
        "extra_attrs": '        elevenlabsTextNormalization="on"\n',
    }

# voice_incoming, replacing `en = LANGUAGE_CONFIGS["en"]`
en = english_conversation_relay_config(_english_voice_profile())

# voice_language_selected, replacing `config = LANGUAGE_CONFIGS["en"]`
config = english_conversation_relay_config(_english_voice_profile())
~~~

Delete only the hard-coded English `LANGUAGE_CONFIGS["en"]` entry and legacy English voice constant. Retain every non-English entry and route unchanged. Missing profile raises the fixed Task 1 error; no general/multilingual fallback is added.

- [ ] **Step 4: Run GREEN**

Run: `cd Kavya && pytest -q tests/test_english_voice_profile.py tests/test_prompt_policy.py`

Expected: PASS; the test proves shared selection and unchanged retained non-English configuration.

- [ ] **Step 5: Commit atomically**

Run: `git add Kavya/server.py Kavya/tests/test_english_voice_profile.py && git diff --cached --check && git commit -m "refactor(kavya): share English relay voice selection"`

Expected: one commit containing the relay selection change and test.

### Task 3: Direct SmartPBX TTS uses query-format flash requests

**Files:**
- Modify: `Kavya/server.py:2579-2632,3485-3577`
- Modify: `Kavya/smartpbx_session.py:152-193`
- Modify: `Kavya/tests/test_smartpbx_server.py:13-228`

**Interfaces:**
- Extends `MediaStreamSession(..., english_voice_profile: EnglishVoiceProfile | None = None)`.
- Consumes Task 1 `build_direct_stream_request`.
- Existing `FakeTransport` in `test_smartpbx_server.py:13-27` records `audio`, `clears`, and `marks`.
- The task defines all HTTP fakes below.

- [ ] **Step 1: Add the complete failing integration test and fakes**

~~~python
# Kavya/tests/test_smartpbx_server.py
from urllib.parse import parse_qs, urlparse
from english_voice_profile import EnglishVoiceProfile


class _FakeElevenLabsResponse:
    status_code = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def aiter_bytes(self, chunk_size: int):
        assert chunk_size == 640
        yield b"encoded-audio"


class _FakeElevenLabsClient:
    def __init__(self, captured: dict[str, object]):
        self._captured = captured

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    def stream(self, method: str, url: str, **kwargs):
        self._captured.update(method=method, url=url, kwargs=kwargs)
        return _FakeElevenLabsResponse()


@pytest.mark.asyncio
async def test_smartpbx_english_tts_sends_ulaw_query_flash_body(monkeypatch):
    import server

    captured: dict[str, object] = {}
    transport = FakeTransport()
    pipeline = server.MediaStreamSession(
        websocket=None,
        lang="en",
        media_transport=transport,
        english_voice_profile=EnglishVoiceProfile("test-only-voice-marker"),
    )
    pipeline._smartpbx_transfer_context = object()
    monkeypatch.setattr(server, "ELEVENLABS_API_KEY", "test-only-api-marker")
    monkeypatch.setattr(
        server.httpx, "AsyncClient", lambda: _FakeElevenLabsClient(captured)
    )

    await pipeline._tts_elevenlabs("Hello")

    assert captured["method"] == "POST"
    assert parse_qs(urlparse(captured["url"]).query) == {
        "output_format": ["ulaw_8000"]
    }
    body = captured["kwargs"]["json"]
    assert body["model_id"] == "eleven_flash_v2_5"
    assert "output_format" not in body
    assert transport.audio == [b"encoded-audio"]
~~~

- [ ] **Step 2: Run RED**

Run: `cd Kavya && pytest -q tests/test_smartpbx_server.py::test_smartpbx_english_tts_sends_ulaw_query_flash_body`

Expected: FAIL with `TypeError` because `MediaStreamSession` has no `english_voice_profile` parameter; current English direct TTS also selects the general voice/multilingual model.

- [ ] **Step 3: Add the complete SmartPBX branch while preserving the existing non-English branch**

~~~python
# server.py imports
from english_voice_profile import (
    EnglishVoiceProfile,
    build_direct_stream_request,
)

# add to MediaStreamSession.__init__ signature
english_voice_profile: EnglishVoiceProfile | None = None,

# add inside __init__
self._english_voice_profile = english_voice_profile

# replace request selection at the top of _tts_elevenlabs after API-key guard
if self._is_smartpbx_session() and self.lang == "en":
    profile = self._english_voice_profile
    if profile is None:
        raise RuntimeError("canonical English voice is unavailable")
    url, payload = build_direct_stream_request(profile, text)
else:
    voice_id = (
        ELEVENLABS_VOICE_ID_AR or ELEVENLABS_VOICE_ID
    ) if self.lang == "ar" else ELEVENLABS_VOICE_ID
    url = ELEVENLABS_TTS_URL.format(voice_id=voice_id) + "?output_format=ulaw_8000"
    payload = {
        "text": text,
        "model_id": ELEVENLABS_MODEL_MULTILINGUAL,
        "voice_settings": {
            "stability": 0.5,
            "similarity_boost": 0.75,
            "style": 0.0,
            "use_speaker_boost": True,
        },
    }

# smartpbx_session.py inside _load_runtime_defaults MediaStreamSession call
self._pipeline = server.MediaStreamSession(
    websocket=None,
    lang="en",
    anthropic_client=anthropic_client,
    openai_client=openai_client,
    gemini_client=gemini_client,
    media_transport=self._transport,
    english_voice_profile=server._english_voice_profile(),
)
~~~

Keep the existing `http.stream("POST", url, json=payload, headers=headers, timeout=15.0)` and audio loop unchanged. `output_format` is never inserted into `payload`; `SmartPBXMediaTransport` receives μ-law bytes without resampling.

- [ ] **Step 4: Run GREEN**

Run: `cd Kavya && pytest -q tests/test_smartpbx_server.py::test_smartpbx_english_tts_sends_ulaw_query_flash_body tests/test_smartpbx_transport.py tests/test_english_voice_profile.py`

Expected: PASS; captured query/body prove the corrected request boundary and transport envelope.

- [ ] **Step 5: Commit atomically**

Run: `git add Kavya/server.py Kavya/smartpbx_session.py Kavya/tests/test_smartpbx_server.py && git diff --cached --check && git commit -m "fix(kavya): send canonical SmartPBX English TTS"`

Expected: one commit containing only direct TTS wiring and integration coverage.

### Task 4: Dialog v06 parser conformance remains closed-world

**Files:**
- Modify: `Kavya/smartpbx_protocol.py:15-245`
- Modify: `Kavya/tests/test_smartpbx_protocol.py:1-122`

**Interfaces:**
- `HangupEvent(call_id: str, other_leg_call_id: str, reason: str | None)`.
- `UnsupportedEvent(failure_class: str = "unsupported_event")`; it stores no event name.
- `validate_event_context(event: SmartPBXEvent, context: CallContext) -> None`.

- [ ] **Step 1: Add complete RED coverage**

~~~python
# append to Kavya/tests/test_smartpbx_protocol.py
@pytest.mark.parametrize("reason", [None, "normal clearing"])
def test_v06_hangup_has_no_account_requirement_and_optional_reason(reason):
    hangup = {"callId": "call-marker", "otherLegCallId": "leg-marker"}
    if reason is not None:
        hangup["reason"] = reason
    event = parse({"event": "hangup", "hangup": hangup})
    assert event == HangupEvent("call-marker", "leg-marker", reason)


@pytest.mark.parametrize("digit", list("0123456789*#ABCD"))
def test_v06_dtmf_accepts_documented_digits(digit):
    event = parse({"event": "dtmf", "dtmf": {"digit": digit}})
    assert event == DtmfEvent(digit, None)


@pytest.mark.parametrize("name, expected_type", [
    ("connected", ConnectedEvent),
    ("stop", StopEvent),
])
def test_known_compatibility_extensions_remain_strict(name, expected_type):
    assert isinstance(parse({"event": name}), expected_type)


def test_unknown_event_has_fixed_private_discriminator():
    event = parse({"event": "private-event-marker"})
    assert event == UnsupportedEvent()
    assert "private-event-marker" not in repr(event)


def test_hangup_context_uses_only_documented_identifiers():
    context = parse(START).context
    event = HangupEvent(context.call_id, context.other_leg_call_id, None)
    validate_event_context(event, context)
~~~

The imports at the top of the test add `HangupEvent`, `DtmfEvent`, and `UnsupportedEvent`; `parse`, `START`, `ConnectedEvent`, `StopEvent`, and `validate_event_context` are existing helpers/interfaces in this file.

- [ ] **Step 2: Run RED**

Run: `cd Kavya && pytest -q tests/test_smartpbx_protocol.py`

Expected: FAIL because current hangup requires account/reason, A-D are rejected, and `UnknownEvent` retains the raw event name.

- [ ] **Step 3: Add complete minimal parser implementation**

~~~python
_ALLOWED_DTMF_DIGITS = frozenset("0123456789*#ABCD")


@dataclass(frozen=True)
class HangupEvent:
    call_id: str
    other_leg_call_id: str
    reason: str | None


@dataclass(frozen=True)
class UnsupportedEvent:
    failure_class: str = "unsupported_event"


SmartPBXEvent: TypeAlias = (
    ConnectedEvent | StartEvent | MediaEvent | DtmfEvent |
    HangupEvent | StopEvent | UnsupportedEvent
)


def _parse_hangup(message: Mapping[object, object]) -> HangupEvent:
    hangup = _required_mapping(message, "hangup")
    reason = hangup.get("reason")
    if reason is not None:
        if not isinstance(reason, str) or not reason.strip() or len(reason) > _MAX_HANGUP_REASON_CHARS:
            raise _invalid_message()
        reason = reason.strip()
    return HangupEvent(
        call_id=_required_text(hangup, "callId", _MAX_IDENTIFIER_CHARS),
        other_leg_call_id=_required_text(hangup, "otherLegCallId", _MAX_IDENTIFIER_CHARS),
        reason=reason,
    )


def validate_event_context(event: SmartPBXEvent, context: CallContext) -> None:
    if isinstance(event, StartEvent):
        actual = (event.context.call_id, event.context.other_leg_call_id, event.context.account_id)
        expected = (context.call_id, context.other_leg_call_id, context.account_id)
    elif isinstance(event, HangupEvent):
        actual = (event.call_id, event.other_leg_call_id)
        expected = (context.call_id, context.other_leg_call_id)
    else:
        return
    if actual != expected:
        raise ProtocolViolation(POLICY_VIOLATION, "event context mismatch", "context_mismatch")
~~~

In `parse_smartpbx_event`, keep existing cases for `connected`, `start`, `media`, `dtmf`, `hangup`, and `stop`, then return `UnsupportedEvent()`. Delete `UnknownEvent`. Do not change `_parse_context`, media bounds, or the `g711_ulaw`/8000 check.

- [ ] **Step 4: Run GREEN**

Run: `cd Kavya && pytest -q tests/test_smartpbx_protocol.py tests/test_smartpbx_gateway.py`

Expected: PASS; the existing parameterized non-μ-law test remains green.

- [ ] **Step 5: Commit atomically**

Run: `git add Kavya/smartpbx_protocol.py Kavya/tests/test_smartpbx_protocol.py && git diff --cached --check && git commit -m "fix(kavya): conform parser to Dialog v06"`

Expected: one protocol/test commit.

### Task 5: Privacy-safe finite gateway diagnostics

**Files:**
- Modify: `Kavya/smartpbx_gateway.py:146-356`
- Modify: `Kavya/tests/test_smartpbx_gateway.py:1-175`

**Interfaces:**
- `LifecycleStage = Literal["admission", "context", "session_start", "audio", "tts", "terminal_cleanup"]`.
- `log_lifecycle(stage: LifecycleStage, failure_class: str) -> None` accepts only failure classes from `_SAFE_FAILURE_CLASSES`.
- Existing test `run(messages, *, configuration=None, registry=None, token="test-token", header="X-Kavya-SmartPBX-Token")` is reused.

- [ ] **Step 1: Add the complete failing privacy test**

~~~python
# append to Kavya/tests/test_smartpbx_gateway.py
@pytest.mark.asyncio
async def test_unsupported_event_logs_only_finite_admission_class(caplog):
    private_name = "private-event-marker"
    private_id = "private-call-marker"
    with caplog.at_level(logging.INFO):
        _, _, websocket, _ = await run([
            START,
            json.dumps({"event": private_name, "callId": private_id})
        ])
    assert websocket.close_code == 1008
    assert "stage=admission failure_class=unsupported_event" in caplog.text
    assert private_name not in caplog.text
    assert private_id not in caplog.text
~~~

Add `import json` and `import logging`; `run` and its tuple return are defined earlier in the same existing test file.

- [ ] **Step 2: Run RED**

Run: `cd Kavya && pytest -q tests/test_smartpbx_gateway.py::test_unsupported_event_logs_only_finite_admission_class`

Expected: FAIL because the current gateway counts `UnknownEvent` instead of rejecting it with the fixed lifecycle discriminator.

- [ ] **Step 3: Add complete finite diagnostic code and dispatch**

~~~python
from typing import Literal

LifecycleStage = Literal[
    "admission", "context", "session_start", "audio", "tts", "terminal_cleanup"
]
_SAFE_FAILURE_CLASSES = frozenset({
    "unsupported_event", "invalid_message", "message_too_big",
    "unsupported_media_format", "invalid_media", "audio_too_big",
    "invalid_dtmf", "context_mismatch", "session_start_failed",
    "audio_ingestion_failed", "tts_failed", "cleanup_failed",
})


def log_lifecycle(stage: LifecycleStage, failure_class: str) -> None:
    safe_class = failure_class if failure_class in _SAFE_FAILURE_CLASSES else "cleanup_failed"
    logger.info("smartpbx_lifecycle stage=%s failure_class=%s", stage, safe_class)

# inside the post-start event loop, before MediaEvent handling
if isinstance(event, UnsupportedEvent):
    log_lifecycle("admission", event.failure_class)
    raise ProtocolViolation(POLICY_VIOLATION, "unsupported SmartPBX event", event.failure_class)
~~~

Replace the dynamic `UnknownEvent` counter/status field entirely. At existing exception boundaries call `log_lifecycle`: parser schema=`admission`; `context_mismatch`=`context`; session factory/start=`session_start`; `feed_audio`=`audio`; fixed TTS failure signal=`tts`; finish/transport/close=`terminal_cleanup`. Pass only the existing fixed class strings shown above; never pass exception text, raw fields, payload, IDs, transcript, or audio. `/smartpbx/status` gains no history or identifiers.

- [ ] **Step 4: Run GREEN**

Run: `cd Kavya && pytest -q tests/test_smartpbx_gateway.py tests/test_smartpbx_server.py::test_dialog_media_logs_never_contain_transcript_agent_text_or_call_id tests/test_smartpbx_transport.py`

Expected: PASS; logs distinguish finite lifecycle stages without sensitive markers.

- [ ] **Step 5: Commit atomically**

Run: `git add Kavya/smartpbx_gateway.py Kavya/tests/test_smartpbx_gateway.py && git diff --cached --check && git commit -m "feat(kavya): add private SmartPBX lifecycle diagnostics"`

Expected: one gateway/test commit.

### Task 6: Explicit English adapter parity contract

**Files:**
- Modify: `Kavya/smartpbx_session.py:14-224`
- Modify: `Kavya/server.py:2550-3057,3337-3478,4073-4257`
- Modify: `Kavya/tests/test_smartpbx_server.py:80-527`
- Modify: `Kavya/tests/test_smartpbx_provider_handover.py:190-297`

**Interfaces:**
- New immutable `EnglishSessionDependencies(system_prompt: str, tools: list[dict[str, object]], welcome_text: str, stt_factory: Callable[..., Any], post_call_processor: PostCallProcessor)`.
- `load_english_session_dependencies(server_module: Any) -> EnglishSessionDependencies` returns references/values from the production English pipeline; it does not copy prompt/tools/RAG/booking/fillers.
- Existing `FakePipeline`, `FakeSTT`, `FakeTransport`, `context`, and `make_session` are defined in `test_smartpbx_server.py:13-107`.

- [ ] **Step 1: Add genuinely failing contract and observable-behavior tests before wiring**

~~~python
# append to Kavya/tests/test_smartpbx_server.py
from smartpbx_session import load_english_session_dependencies


def test_dependency_loader_reuses_production_english_objects():
    import server
    deps = load_english_session_dependencies(server)
    assert deps.system_prompt == server._build_system_prompt("en")
    assert deps.tools == server.get_tools()
    assert deps.welcome_text == server.english_conversation_relay_config(
        server._english_voice_profile()
    )["welcome_greeting"]
    assert deps.stt_factory is server._make_stt
    assert deps.post_call_processor is server.process_post_call_data


@pytest.mark.asyncio
async def test_direct_completion_reprompt_and_barge_in_are_transport_local():
    import server
    transport = FakeTransport()
    pipeline = server.MediaStreamSession(None, "en", media_transport=transport,
        english_voice_profile=server._english_voice_profile())
    pipeline._smartpbx_transfer_context = object()
    pipeline._is_speaking = True
    generation = pipeline._speak_generation
    pipeline._event_loop = asyncio.get_running_loop()
    pipeline._on_stt_interim("speech-marker")
    await asyncio.sleep(0)
    assert pipeline._speak_generation == generation + 1
    assert transport.clears == 1
    await pipeline._send_tts_done()
    assert pipeline._reprompt_task is not None
    await pipeline.enter_transfer_pending()
    assert pipeline._reprompt_task is None
~~~

The test process supplies only `KAVYA_EN_ELEVENLABS_VOICE_ID=test-only-voice-marker`; it never uses a production value. The first test is guaranteed RED because the loader/type do not exist. The second locks current observable behavior after Task 3 wiring.

- [ ] **Step 2: Run RED**

Run: `cd Kavya && KAVYA_EN_ELEVENLABS_VOICE_ID=test-only-voice-marker pytest -q tests/test_smartpbx_server.py -k 'dependency_loader or direct_completion_reprompt'`

Expected: collection FAIL with missing `load_english_session_dependencies`.

- [ ] **Step 3: Add the complete dependency contract and consume it**

~~~python
# Kavya/smartpbx_session.py
from dataclasses import dataclass
from typing import Any, Awaitable, Callable


@dataclass(frozen=True)
class EnglishSessionDependencies:
    system_prompt: str
    tools: list[dict[str, object]]
    welcome_text: str
    stt_factory: Callable[..., Any]
    post_call_processor: PostCallProcessor


def load_english_session_dependencies(server_module: Any) -> EnglishSessionDependencies:
    return EnglishSessionDependencies(
        system_prompt=server_module._build_system_prompt("en"),
        tools=server_module.get_tools(),
        welcome_text=server_module.english_conversation_relay_config(
            server_module._english_voice_profile()
        )["welcome_greeting"],
        stt_factory=server_module._make_stt,
        post_call_processor=server_module.process_post_call_data,
    )

# in KavyaSmartPBXSession._load_runtime_defaults after `import server`
dependencies = load_english_session_dependencies(server)
if self._stt_factory is None:
    self._stt_factory = dependencies.stt_factory
if self._post_call_processor is None:
    self._post_call_processor = dependencies.post_call_processor
if self._welcome_text is None:
    self._welcome_text = dependencies.welcome_text
if self._pipeline is not None:
    self._pipeline.system_prompt = dependencies.system_prompt
    self._pipeline.tools = dependencies.tools
~~~

Keep existing provider-client selection, `retrieve_context`, booking dispatch, `TOOL_FILLERS`, history/full transcript, privacy-safe post-call, `_send_tts_done`, `_cancel_reprompt`, `_on_stt_interim`, and `enter_transfer_pending`; do not duplicate them. Existing provider-handover tests prove filler ordering, failed-tool continuation, and transfer-pending suppression.

- [ ] **Step 4: Run GREEN**

Run: `cd Kavya && KAVYA_EN_ELEVENLABS_VOICE_ID=test-only-voice-marker pytest -q tests/test_smartpbx_server.py tests/test_smartpbx_provider_handover.py tests/test_smartpbx_post_call.py`

Expected: PASS; the loader contract proves shared prompt/tools/STT/welcome/post-call, and existing tests prove RAG/booking/tool/filler/state behavior.

- [ ] **Step 5: Commit atomically**

Run: `git add Kavya/smartpbx_session.py Kavya/server.py Kavya/tests/test_smartpbx_server.py Kavya/tests/test_smartpbx_provider_handover.py && git diff --cached --check && git commit -m "test(kavya): enforce English SmartPBX behavior parity"`

Expected: one adapter-contract/test commit.

### Task 7: Protected configuration, isolation, runbook, and rollback

**Files:**
- Modify: `Kavya/.env.example:18-21,178-202`
- Modify: `Kavya/docker-compose.yml:88-155`
- Modify: `Kavya/SMARTPBX_RUNBOOK.md:76-257`
- Modify: `Kavya/tests/test_smartpbx_deployment.py:1-264`

**Interfaces:**
- Only root-owned `/opt/kavya/.env.smartpbx` supplies `KAVYA_EN_ELEVENLABS_VOICE_ID` with mode 0600.
- `kavya-smartpbx` receives the key name; templates contain an empty assignment only.
- `SMARTPBX_TRANSFER_DESTINATIONS_JSON={}` and blank MCP configuration remain mandatory for this plan.

- [ ] **Step 1: Add complete failing deployment tests**

~~~python
# append to Kavya/tests/test_smartpbx_deployment.py
def test_smartpbx_uses_only_protected_canonical_english_voice_key():
    compose = yaml.safe_load(read_text("docker-compose.yml"))
    environment = compose["services"]["kavya-smartpbx"]["environment"]
    assert environment["KAVYA_EN_ELEVENLABS_VOICE_ID"] == "${KAVYA_EN_ELEVENLABS_VOICE_ID}"
    assert "ELEVENLABS_VOICE_ID" not in environment


def test_voice_migration_is_root_only_redacted_and_transfer_stays_disabled():
    example = read_text(".env.example")
    runbook = read_text("SMARTPBX_RUNBOOK.md")
    assert "KAVYA_EN_ELEVENLABS_VOICE_ID=" in example
    assert "chmod 600 /opt/kavya/.env.smartpbx" in runbook
    assert "voice_configured" in runbook
    assert "KAVYA_EN_ELEVENLABS_VOICE_ID=<" not in runbook
    assert "SMARTPBX_TRANSFER_DESTINATIONS_JSON={}" in runbook
~~~

- [ ] **Step 2: Run RED**

Run: `cd Kavya && pytest -q tests/test_smartpbx_deployment.py -k 'canonical_english_voice or voice_migration'`

Expected: FAIL because the protected key/migration instructions do not exist and compose still injects the general voice key.

- [ ] **Step 3: Apply complete configuration snippets and exact runbook block**

~~~dotenv
# Kavya/.env.example
# Root-only canonical production English selection; never commit a value.
KAVYA_EN_ELEVENLABS_VOICE_ID=
~~~

~~~yaml
# Kavya/docker-compose.yml, kavya-smartpbx environment
KAVYA_EN_ELEVENLABS_VOICE_ID: "${KAVYA_EN_ELEVENLABS_VOICE_ID}"
~~~

~~~markdown
<!-- Kavya/SMARTPBX_RUNBOOK.md -->
## Protected canonical English voice migration

As root, copy the established Kavya English selection directly into
`/opt/kavya/.env.smartpbx`; do not print, paste into chat, or commit it.
Run `chmod 600 /opt/kavya/.env.smartpbx`.
The redacted validation may report only:
`voice_configured=true model=eleven_flash_v2_5 output=ulaw_8000 media=g711_ulaw/8000`.
It must never report the selected value.
Keep `SMARTPBX_TRANSFER_DESTINATIONS_JSON={}` and all MCP activation values blank.

Direct barge-in clears queued local frames and invalidates speech generation.
Audio already buffered by the carrier cannot be recalled deterministically;
this is not byte-perfect interruption parity. ConversationRelay-managed
recognition options do not exist on direct media; direct English uses `_make_stt`
with `en-US` and privacy-safe provider logging.
~~~

Remove `ELEVENLABS_VOICE_ID` only from the `kavya-smartpbx` service environment; retain it wherever dormant non-English/Twilio behavior still consumes it. Do not change Flico or other Compose services.

- [ ] **Step 4: Run GREEN**

Run: `cd Kavya && pytest -q tests/test_smartpbx_deployment.py`

Expected: PASS; isolation, TLS, image pinning, disabled transfer, and rollback tests remain green.

- [ ] **Step 5: Commit atomically**

Run: `git add Kavya/.env.example Kavya/docker-compose.yml Kavya/SMARTPBX_RUNBOOK.md Kavya/tests/test_smartpbx_deployment.py && git diff --cached --check && git commit -m "docs(kavya): secure SmartPBX voice migration"`

Expected: one configuration/runbook/test commit containing no protected value.

### Task 8: Verification, dynamic implementation PR, isolated release, and stable-call gate

**Files:**
- Modify only code already named in Tasks 1-7 when a failing test proves a defect.
- Do not modify MCP/transfer files, Flico, Twilio service configuration, or dashboard routing in repository code.

**Interfaces:**
- The implementation branch/PR is discovered at execution time; this docs PR number is never assumed.
- Deployment consumes the reviewed immutable commit and matching OCI revision.
- Live actions require explicit user/operator approval.

- [ ] **Step 1: Run targeted and full local evidence**

Run:

~~~bash
cd Kavya
KAVYA_EN_ELEVENLABS_VOICE_ID=test-only-voice-marker pytest -q \
  tests/test_english_voice_profile.py \
  tests/test_smartpbx_protocol.py \
  tests/test_smartpbx_gateway.py \
  tests/test_smartpbx_server.py \
  tests/test_smartpbx_transport.py \
  tests/test_smartpbx_deployment.py \
  tests/test_smartpbx_provider_handover.py \
  tests/test_smartpbx_post_call.py
KAVYA_EN_ELEVENLABS_VOICE_ID=test-only-voice-marker pytest -q
python -m compileall -q .
git diff origin/main...HEAD --check
~~~

Expected: every command exits 0; no skipped/failing SmartPBX boundary; diff contains no actual voice ID/key/account/caller/destination value.

- [ ] **Step 2: Obtain independent review and fix findings RED-first**

Review exactly: profile has no fallback; format is query-only; flash is body model; body excludes format; Twilio/direct share selection; g711 admission unchanged; v06 hangup/DTMF/extensions correct; diagnostics finite/private; English adapter reuses production dependencies; transfer disabled; Flico/Twilio/non-English boundaries retained. For any finding, add a failing test to the owning task, run it to prove RED, implement the smallest correction, then rerun Step 1.

Expected: no unresolved Critical or Important finding.

- [ ] **Step 3: Push implementation branch, create/find its PR dynamically, and wait for CI**

~~~bash
git push -u origin HEAD
implementation_pr_url="$(gh pr view --json url --jq .url 2>/dev/null || true)"
if [ -z "$implementation_pr_url" ]; then
  gh pr create --draft --fill
fi
implementation_pr_number="$(gh pr view --json number --jq .number)"
implementation_pr_url="$(gh pr view --json url --jq .url)"
gh pr checks "$implementation_pr_number" --watch --fail-fast
~~~

Expected: current branch resolves to one implementation PR URL/number and every required check passes. Do not use a hard-coded PR number.

- [ ] **Step 4: Verify image provenance and deploy only the isolated profile**

Use the exact reviewed full SHA and CI-produced short tag. Follow `Kavya/SMARTPBX_RUNBOOK.md` to verify `org.opencontainers.image.revision` equals the reviewed full SHA, validate `docker compose --env-file .env.smartpbx --profile smartpbx config`, recreate only `kavya-smartpbx` with `--pull never`, and run `wait_for_smartpbx_ready`.

Expected: Flico and Twilio Kavya remain untouched; `/smartpbx/status` is healthy and reports transfer disabled. If provenance/readiness fails, stop and execute only the isolated runbook rollback.

- [ ] **Step 5: Run the approved supervised stable-call gate**

After explicit approval for temporary sole-DID routing, one supervised Dialog call must prove: expected Kavya voice; intelligible two-way audio; a normal question; one RAG/booking turn; correct filler without dead air; caller interruption within documented carrier-buffer limit; orderly hangup; no protocol-admission error. Record only redacted pass/fail observations outside Git; a greeting alone fails.

Expected: all observations pass. On any failure, stop only `kavya-smartpbx`, restore the previously approved dashboard route, leave Twilio/Flico untouched, and keep transfer disabled. Only a passing stable-call gate permits the independent MCP plan to begin.

## Final self-review checklist

- Spec coverage: Tasks 1-3 cover canonical protected selection and correct ElevenLabs query/body contract; Tasks 4-5 cover v06 and privacy diagnostics; Task 6 covers English prompt/tools/STT/RAG/booking/filler/re-prompt/barge-in/post-call reuse; Task 7 covers secure migration/isolation/rollback; Task 8 covers full tests, review, dynamic PR/CI, image, deployment, stable call, and rollback.
- Writing-plans completeness: every task identifies exact paths/interfaces, contains a complete RED test, exact RED/GREEN commands and expectations, minimal code/config snippets, and an atomic commit. Runtime-only release steps contain exact commands/gates instead of repository implementation.
- Identifier audit: every non-repository helper/type used by a test is defined in the same task; reused helpers include exact existing file anchors/signatures. No `capture_elevenlabs_request`, `RecordingProbe`, `FakeListToolsResult`, `diagnostic_settings`, `acknowledged_coordinator`, or `CarrierTransferOutcome` appears.
- Request audit: `output_format=ulaw_8000` appears only in the stream URL query; JSON contains `text`, `model_id`, and `voice_settings`; tests assert query and body separately and assert body exclusion.
- Scope audit: transfer remains disabled, no MCP/account-header work occurs, no actual protected value appears, Flico/Twilio/non-English paths remain intact, and the stable-call gate precedes the independent MCP plan.

## Execution handoff

Plan saved at `docs/superpowers/plans/2026-08-07-kavya-smartpbx-call-parity.md`. Execute with subagent-driven development and a fresh implementer/reviewer per task, or inline with review checkpoints after Tasks 3, 5, 7, and 8.
