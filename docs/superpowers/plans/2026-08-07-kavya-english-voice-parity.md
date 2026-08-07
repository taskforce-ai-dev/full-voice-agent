# Kavya English Voice Parity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Kavya's normal Twilio ConversationRelay, handover-recovery ConversationRelay, and direct Dialog SmartPBX English TTS select the same protected voice identity and `eleven_flash_v2_5` behavior.

**Architecture:** A standard-library profile module owns protected English selection and redaction. `server.py` will retain the language mappings but copy/inject a canonical Twilio composite for English, and its existing direct `MediaStreamSession` will use the same profile only when `lang == "en"`. Legacy non-English TTS stays on its existing general-voice/multilingual route.

**Tech Stack:** Python 3.11, FastAPI, httpx, pytest, Docker, Docker Compose, PyYAML.

## Global Constraints

- Start from `origin/Rakesh` commit `7a4daf5d1b538830eabaee0fa2365bc431639a7a`.
- The protected key is exactly `KAVYA_EN_ELEVENLABS_VOICE_ID`. Never put its real value in code, Git, tests, logs, status, diagnostics, comments, docs, or command output.
- Missing, blank, or whitespace-only protected configuration fails closed. English never uses `ELEVENLABS_VOICE_ID`, `ELEVENLABS_VOICE_ID_AR`, `eleven_multilingual_v2`, MP3, a resample, or any fallback voice.
- Direct English request values are exactly model `eleven_flash_v2_5`, URL query `output_format=ulaw_8000`, stability `0.5`, similarity boost `0.75`, style `0.0`, and speaker boost `True`; the body must not contain `output_format`. Keep Dialog `g711_ulaw` / 8000 unchanged.
- Keep `LANGUAGE_CONFIGS["en"]`, every retained language mapping, Flico, the legacy Twilio service, and `SMARTPBX_TRANSFER_DESTINATIONS_JSON={}` / MCP transfer-disabled behavior intact.
- Do not change protocol, diagnostics, RAG, provider, booking, re-prompt, MCP, handover, dashboard, deployment, or live-call behavior. Do not change `requirements-prod.lock.txt`.
- Run commands from `/home/dev/full-voice-agent/Kavya`; use only `unit-test-canonical-voice` as an opaque test identity.

---

## File Map

- Create `Kavya/english_voice_profile.py` and `Kavya/tests/test_english_voice_profile.py`.
- Modify `Kavya/server.py`, `Kavya/tests/test_call_quality_fixes.py`, `Kavya/tests/test_handover_server.py`, and `Kavya/tests/test_smartpbx_server.py`.
- Modify `Kavya/Dockerfile`, `Kavya/.env.example`, `Kavya/docker-compose.yml`, `Kavya/SMARTPBX_RUNBOOK.md`, and `Kavya/tests/test_smartpbx_deployment.py`.

### Task 1: Build the Redacted Canonical Profile

**Files:**
- Create: `Kavya/english_voice_profile.py`
- Create: `Kavya/tests/test_english_voice_profile.py`

**Interfaces:**
- Produces `KAVYA_EN_ELEVENLABS_VOICE_ID: str`, `ELEVEN_FLASH_V2_5: str`, `ULAW_8000: str`, `ElevenLabsVoiceSettings`, `KavyaEnglishVoiceProfile`, and `load_kavya_english_voice_profile(environment: Mapping[str, str] | None = None) -> KavyaEnglishVoiceProfile`.
- `KavyaEnglishVoiceProfile.twilio_composite_voice: str` is `voice_id` plus `-flash_v2_5`; `.request_voice_settings: dict[str, float | bool]` contains the nonsecret direct settings.

- [ ] **Step 1: Write failing tests**

Create `tests/test_english_voice_profile.py`:

```python
import pytest

from english_voice_profile import (
    ELEVEN_FLASH_V2_5,
    KAVYA_EN_ELEVENLABS_VOICE_ID,
    ULAW_8000,
    load_kavya_english_voice_profile,
)


def test_profile_selects_exact_semantics_and_redacts_voice_id():
    profile = load_kavya_english_voice_profile(
        {KAVYA_EN_ELEVENLABS_VOICE_ID: "unit-test-canonical-voice"}
    )
    assert profile.twilio_composite_voice == "unit-test-canonical-voice-flash_v2_5"
    assert profile.model_id == ELEVEN_FLASH_V2_5
    assert profile.output_format == ULAW_8000
    assert profile.request_voice_settings == {
        "stability": 0.5,
        "similarity_boost": 0.75,
        "style": 0.0,
        "use_speaker_boost": True,
    }
    assert KAVYA_EN_ELEVENLABS_VOICE_ID in repr(profile)
    assert "unit-test-canonical-voice" not in repr(profile)
    assert "unit-test-canonical-voice" not in str(profile)


@pytest.mark.parametrize("value", [None, "", "   "])
def test_profile_fails_closed_when_protected_value_is_absent_or_blank(value):
    environment = {}
    if value is not None:
        environment[KAVYA_EN_ELEVENLABS_VOICE_ID] = value
    with pytest.raises(ValueError, match=KAVYA_EN_ELEVENLABS_VOICE_ID):
        load_kavya_english_voice_profile(environment)
```

- [ ] **Step 2: Verify red**

Run: `pytest tests/test_english_voice_profile.py -q`

Expected: FAIL during collection with `ModuleNotFoundError: No module named 'english_voice_profile'`.

- [ ] **Step 3: Implement minimum profile**

Create `english_voice_profile.py`:

```python
from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field

KAVYA_EN_ELEVENLABS_VOICE_ID = "KAVYA_EN_ELEVENLABS_VOICE_ID"
ELEVEN_FLASH_V2_5 = "eleven_flash_v2_5"
ULAW_8000 = "ulaw_8000"
_TWILIO_FLASH_SUFFIX = "flash_v2_5"


@dataclass(frozen=True)
class ElevenLabsVoiceSettings:
    stability: float = 0.5
    similarity_boost: float = 0.75
    style: float = 0.0
    use_speaker_boost: bool = True

    def as_request_payload(self) -> dict[str, float | bool]:
        return {
            "stability": self.stability,
            "similarity_boost": self.similarity_boost,
            "style": self.style,
            "use_speaker_boost": self.use_speaker_boost,
        }


@dataclass(frozen=True)
class KavyaEnglishVoiceProfile:
    voice_id: str = field(repr=False)
    model_id: str = ELEVEN_FLASH_V2_5
    output_format: str = ULAW_8000
    settings: ElevenLabsVoiceSettings = field(default_factory=ElevenLabsVoiceSettings)

    @property
    def twilio_composite_voice(self) -> str:
        return f"{self.voice_id}-{_TWILIO_FLASH_SUFFIX}"

    @property
    def request_voice_settings(self) -> dict[str, float | bool]:
        return self.settings.as_request_payload()

    def __repr__(self) -> str:
        return (
            "KavyaEnglishVoiceProfile("
            f"env_key={KAVYA_EN_ELEVENLABS_VOICE_ID!r}, "
            f"model_id={self.model_id!r}, output_format={self.output_format!r}, "
            f"settings={self.settings!r})"
        )

    __str__ = __repr__


def load_kavya_english_voice_profile(
    environment: Mapping[str, str] | None = None,
) -> KavyaEnglishVoiceProfile:
    source = os.environ if environment is None else environment
    value = source.get(KAVYA_EN_ELEVENLABS_VOICE_ID, "")
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{KAVYA_EN_ELEVENLABS_VOICE_ID} must be configured")
    return KavyaEnglishVoiceProfile(voice_id=value.strip())
```

- [ ] **Step 4: Verify green**

Run: `pytest tests/test_english_voice_profile.py -q`

Expected: `4 passed`.

- [ ] **Step 5: Commit**

```bash
git add Kavya/english_voice_profile.py Kavya/tests/test_english_voice_profile.py
git commit -m "feat(kavya): add canonical English voice profile"
```

### Task 2: Wire Normal, Recovery, and SmartPBX English TTS

**Files:**
- Modify: `Kavya/server.py:135-140,489-527,1751,1816,2180,3485-3579`
- Modify: `Kavya/tests/test_call_quality_fixes.py:224-240`
- Modify: `Kavya/tests/test_handover_server.py:42-75,254-258`
- Modify: `Kavya/tests/test_smartpbx_server.py:1-100,218-230`

**Interfaces:**
- Produces `conversation_relay_config(language: str) -> dict[str, str]`; English uses Task 1 and retained languages return copied mappings.
- `MediaStreamSession._tts_elevenlabs(self, text: str) -> None` requires the protected profile for English and only requires general voice for retained routes.

- [ ] **Step 1: Write failing consumer tests**

Append to `tests/test_call_quality_fixes.py`:

```python
def test_normal_english_conversationrelay_uses_canonical_profile(client, monkeypatch):
    from english_voice_profile import load_kavya_english_voice_profile

    profile = load_kavya_english_voice_profile(
        {"KAVYA_EN_ELEVENLABS_VOICE_ID": "unit-test-canonical-voice"}
    )
    monkeypatch.setattr(server, "load_kavya_english_voice_profile", lambda: profile)
    incoming = client.post("/voice/incoming", headers={"host": "voice.example.test"})
    selected = client.post("/voice/language-selected", data={"Digits": "1"}, headers={"host": "voice.example.test"})
    for response in (incoming, selected):
        assert response.status_code == 200
        assert 'voice="unit-test-canonical-voice-flash_v2_5"' in response.text
```

Append to `tests/test_handover_server.py`:

```python
def test_handover_recovery_uses_canonical_english_profile(client, monkeypatch):
    from english_voice_profile import load_kavya_english_voice_profile

    profile = load_kavya_english_voice_profile(
        {"KAVYA_EN_ELEVENLABS_VOICE_ID": "unit-test-canonical-voice"}
    )
    monkeypatch.setattr(server, "load_kavya_english_voice_profile", lambda: profile)
    response = _dial_result(client, "no-answer")
    assert response.status_code == 200
    assert 'voice="unit-test-canonical-voice-flash_v2_5"' in response.text
```

Append these definitions and tests to `tests/test_smartpbx_server.py` after `FakePipeline`:

```python
class CapturingTTSResponse:
    status_code = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        return False

    async def aread(self):
        return b""

    async def aiter_bytes(self, chunk_size):
        assert chunk_size == 640
        yield b"ulaw-frame"


class CapturingTTSClient:
    def __init__(self):
        self.requests = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        return False

    def stream(self, method, url, *, json, headers, timeout):
        self.requests.append({"method": method, "url": url, "json": json, "headers": headers, "timeout": timeout})
        return CapturingTTSResponse()


@pytest.mark.asyncio
async def test_smartpbx_english_tts_uses_profile_without_general_voice(monkeypatch):
    import server
    from english_voice_profile import load_kavya_english_voice_profile

    client = CapturingTTSClient()
    profile = load_kavya_english_voice_profile({"KAVYA_EN_ELEVENLABS_VOICE_ID": "unit-test-canonical-voice"})
    monkeypatch.setattr(server, "ELEVENLABS_API_KEY", "unit-test-api-key")
    monkeypatch.setattr(server, "ELEVENLABS_VOICE_ID", "")
    monkeypatch.setattr(server, "load_kavya_english_voice_profile", lambda: profile)
    monkeypatch.setattr(server.httpx, "AsyncClient", lambda: client)
    pipeline = server.MediaStreamSession(websocket=None, lang="en", media_transport=FakeTransport())
    await pipeline._tts_elevenlabs("Hello from Kavya.")
    request = client.requests[0]
    assert request["url"] == "https://api.elevenlabs.io/v1/text-to-speech/unit-test-canonical-voice/stream?output_format=ulaw_8000"
    assert request["json"] == {"text": "Hello from Kavya.", "model_id": "eleven_flash_v2_5", "voice_settings": {"stability": 0.5, "similarity_boost": 0.75, "style": 0.0, "use_speaker_boost": True}}
    assert "output_format" not in request["json"]
    assert "mp3" not in request["url"]


@pytest.mark.asyncio
async def test_smartpbx_english_tts_fails_closed_when_profile_is_unavailable(monkeypatch):
    import server

    client = CapturingTTSClient()
    monkeypatch.setattr(server, "ELEVENLABS_API_KEY", "unit-test-api-key")
    monkeypatch.setattr(server, "ELEVENLABS_VOICE_ID", "unit-test-general-voice")
    monkeypatch.setattr(server, "load_kavya_english_voice_profile", lambda: (_ for _ in ()).throw(ValueError("KAVYA_EN_ELEVENLABS_VOICE_ID must be configured")))
    monkeypatch.setattr(server.httpx, "AsyncClient", lambda: client)
    pipeline = server.MediaStreamSession(websocket=None, lang="en", media_transport=FakeTransport())
    await pipeline._tts_elevenlabs("Hello from Kavya.")
    assert client.requests == []


@pytest.mark.asyncio
async def test_retained_non_english_tts_still_requires_general_voice(monkeypatch):
    import server

    client = CapturingTTSClient()
    monkeypatch.setattr(server, "ELEVENLABS_API_KEY", "unit-test-api-key")
    monkeypatch.setattr(server, "ELEVENLABS_VOICE_ID", "")
    monkeypatch.setattr(server.httpx, "AsyncClient", lambda: client)
    pipeline = server.MediaStreamSession(websocket=None, lang="ta", media_transport=FakeTransport())
    await pipeline._tts_elevenlabs("vanakkam")
    assert client.requests == []
```

- [ ] **Step 2: Verify red**

Run: `pytest tests/test_call_quality_fixes.py tests/test_handover_server.py tests/test_smartpbx_server.py -q`

Expected: FAIL because `server` has no profile loader, normal and recovery TwiML use a hardcoded English source, and direct English TTS requires general voice and selects multilingual model.

- [ ] **Step 3: Implement smallest green change**

Add this import in `server.py`:

```python
from english_voice_profile import load_kavya_english_voice_profile
```

Delete only the English `"voice"` entry from `LANGUAGE_CONFIGS["en"]`, retaining all other English values and every `"si"` and `"ta"` entry. Immediately after the mapping, add:

```python
def conversation_relay_config(language: str) -> dict[str, str]:
    config = dict(LANGUAGE_CONFIGS[language])
    if language == "en":
        config["voice"] = load_kavya_english_voice_profile().twilio_composite_voice
    return config
```

Replace exactly these current consumers:

```python
# voice_incoming
en = conversation_relay_config("en")

# voice_language_selected English branch
config = conversation_relay_config("en")

# dial_result recovery branch
recovery_config = conversation_relay_config("en")
```

Leave `recovery_config["welcome_greeting"] = HANDOFF_FAILSAFE_GREETING` after the last assignment so recovery mutates only a copy. Update all current test calls in `tests/test_call_quality_fixes.py` and `tests/test_handover_server.py` that pass `server.LANGUAGE_CONFIGS["en"]` into `_build_conversation_relay_twiml` to pass `server.conversation_relay_config("en")` instead.

Replace `_tts_elevenlabs` from its current initial guard through `payload` with:

```python
    async def _tts_elevenlabs(self, text: str):
        """Stream ElevenLabs TTS as 8 kHz mu-law through the active transport."""
        if not ELEVENLABS_API_KEY:
            logger.warning("ElevenLabs API key not configured — skipping TTS")
            return
        if self.lang == "en":
            try:
                profile = load_kavya_english_voice_profile()
            except ValueError:
                logger.warning("Canonical Kavya English voice is not configured — skipping TTS")
                return
            voice_id = profile.voice_id
            model_id = profile.model_id
            voice_settings = profile.request_voice_settings
        else:
            if not ELEVENLABS_VOICE_ID:
                logger.warning("ElevenLabs general voice not configured — skipping retained-language TTS")
                return
            voice_id = (ELEVENLABS_VOICE_ID_AR or ELEVENLABS_VOICE_ID) if self.lang == "ar" else ELEVENLABS_VOICE_ID
            model_id = ELEVENLABS_MODEL_MULTILINGUAL
            voice_settings = {"stability": 0.5, "similarity_boost": 0.75, "style": 0.0, "use_speaker_boost": True}
        self._is_speaking = True
        url = ELEVENLABS_TTS_URL.format(voice_id=voice_id) + "?output_format=ulaw_8000"
        headers = {"xi-api-key": ELEVENLABS_API_KEY, "Content-Type": "application/json"}
        payload: dict[str, Any] = {"text": text, "model_id": model_id, "voice_settings": voice_settings}
```

Keep the existing stream, interrupt, and error handling directly below `payload` unchanged.

- [ ] **Step 4: Verify green**

Run: `pytest tests/test_english_voice_profile.py tests/test_call_quality_fixes.py tests/test_handover_server.py tests/test_smartpbx_server.py -q`

Expected: PASS; the captured English URL is `ulaw_8000`, the body is flash settings/model with no output format, profile absence sends no request despite general voice, and retained Tamil still needs general voice.

- [ ] **Step 5: Commit**

```bash
git add Kavya/server.py Kavya/tests/test_call_quality_fixes.py Kavya/tests/test_handover_server.py Kavya/tests/test_smartpbx_server.py
git commit -m "fix(kavya): share canonical English voice selection"
```

### Task 3: Package the Module and Document Root-Only Preflight

**Files:**
- Modify: `Kavya/Dockerfile:50`
- Modify: `Kavya/.env.example:18-23`
- Modify: `Kavya/docker-compose.yml:35-43,114-167`
- Modify: `Kavya/SMARTPBX_RUNBOOK.md:42-100`
- Modify: `Kavya/tests/test_smartpbx_deployment.py:20-63`

**Interfaces:**
- Legacy `kavya` receives the key via its existing `.env` `env_file`; `kavya-smartpbx` receives exact `${KAVYA_EN_ELEVENLABS_VOICE_ID}` mapping.
- The image can execute `import english_voice_profile; import server`.

- [ ] **Step 1: Write failing packaging contract**

Append to `tests/test_smartpbx_deployment.py`:

```python
def test_canonical_english_voice_profile_is_packaged_and_server_side_only():
    example = read_text(".env.example")
    compose = yaml.safe_load(read_text("docker-compose.yml"))
    dockerfile = read_text("Dockerfile")
    runbook = read_text("SMARTPBX_RUNBOOK.md")
    assert re.search(r"^KAVYA_EN_ELEVENLABS_VOICE_ID=$", example, re.MULTILINE)
    assert compose["services"]["kavya"]["env_file"] == [".env"]
    assert compose["services"]["kavya-smartpbx"]["environment"]["KAVYA_EN_ELEVENLABS_VOICE_ID"] == "${KAVYA_EN_ELEVENLABS_VOICE_ID}"
    assert "english_voice_profile.py" in dockerfile
    for value in ("KAVYA_EN_ELEVENLABS_VOICE_ID=", "sudoedit /opt/kavya/.env", "sudoedit /opt/kavya/.env.smartpbx", "sha256sum", "config > /dev/null", "SMARTPBX_TRANSFER_DESTINATIONS_JSON={}", "transfer-disabled"):
        assert value in runbook
    assert "KAVYA_EN_ELEVENLABS_VOICE_ID=<" not in runbook
```

- [ ] **Step 2: Verify red**

Run: `pytest tests/test_smartpbx_deployment.py::test_canonical_english_voice_profile_is_packaged_and_server_side_only -q`

Expected: FAIL because the new protected key, image allowlist item, SmartPBX mapping, and provisioning instructions are absent.

- [ ] **Step 3: Implement configuration and image closure**

After `ELEVENLABS_API_KEY` in `.env.example`, add only:

```dotenv
KAVYA_EN_ELEVENLABS_VOICE_ID=
```

After `ELEVENLABS_API_KEY` in `services.kavya-smartpbx.environment`, add:

```yaml
KAVYA_EN_ELEVENLABS_VOICE_ID: "${KAVYA_EN_ELEVENLABS_VOICE_ID}"
```

Do not add an override to `services.kavya`: its existing `env_file: [.env]` is correct. Replace the Docker allowlist line with:

```dockerfile
COPY server.py media_stream_server.py tools.py booking_api.py post_call.py knowledge_base.py yanolja_client.py yanolja_service.py dashboard_client.py handover.py smartpbx_gateway.py smartpbx_handover.py smartpbx_mcp.py smartpbx_protocol.py smartpbx_session.py smartpbx_transport.py english_voice_profile.py ./
```

After `ELEVENLABS_API_KEY=` in the isolated runbook template, add `KAVYA_EN_ELEVENLABS_VOICE_ID=`. Then add this complete runbook section after that template:

````markdown
### Canonical English voice provisioning preflight

Retrieve the existing approved English identity only from a root-only secret source. Do not copy it from source code, a log, the dashboard, or this runbook. Do not rotate `ELEVENLABS_API_KEY` or alter `ELEVENLABS_VOICE_ID`. Put the same protected value into both files with `sudoedit`; never pass it as a command argument or paste it into a ticket, commit, or screen share.

```sh
set -euo pipefail
cd /opt/kavya
sudo test -f /opt/kavya/.env
sudo touch /opt/kavya/.env.smartpbx
sudo chown root:root /opt/kavya/.env /opt/kavya/.env.smartpbx
sudo chmod 600 /opt/kavya/.env /opt/kavya/.env.smartpbx
sudoedit /opt/kavya/.env
sudoedit /opt/kavya/.env.smartpbx
for protected_file in /opt/kavya/.env /opt/kavya/.env.smartpbx; do
  sudo test "$(stat -c '%U:%G:%a' "$protected_file")" = root:root:600
  sudo awk -F= '$1 == "KAVYA_EN_ELEVENLABS_VOICE_ID" { value = substr($0, index($0, "=") + 1); if (value ~ /[^[:space:]]/) ok = 1 } END { exit ok ? 0 : 1 }' "$protected_file"
  sudo awk -F= '$1 == "KAVYA_EN_ELEVENLABS_VOICE_ID" { value = substr($0, index($0, "=") + 1); if (value ~ /[^[:space:]]/) print value }' "$protected_file" | sha256sum
done
SMARTPBX_IMAGE_TAG="$REVIEWED_CI_SHORT_SHA" docker compose --env-file .env.smartpbx --profile smartpbx config > /dev/null
```

This prints only a hash and starts, stops, edits, or routes no service, so a failed preflight leaves running services unchanged. Keep `SMARTPBX_TRANSFER_DESTINATIONS_JSON={}` and MCP transfer-disabled; deployment and a live call require another approval.
````

- [ ] **Step 4: Verify focused/full tests, secret scan, build, and image import**

```bash
pytest tests/test_english_voice_profile.py tests/test_call_quality_fixes.py tests/test_handover_server.py tests/test_smartpbx_server.py tests/test_smartpbx_deployment.py -q
pytest tests -q
gitleaks detect --source .. --no-git --redact
docker build -t kavya-english-voice-parity:local .
docker run --rm --entrypoint python kavya-english-voice-parity:local -c "import english_voice_profile; import server"
```

Expected: pytest passes, gitleaks reports no introduced secret, build uses unchanged `requirements-prod.lock.txt`, and the container import exits `0`, proving the Docker runtime-module closure.

- [ ] **Step 5: Commit**

```bash
git add Kavya/Dockerfile Kavya/.env.example Kavya/docker-compose.yml Kavya/SMARTPBX_RUNBOOK.md Kavya/tests/test_smartpbx_deployment.py
git commit -m "chore(kavya): package protected English voice profile"
```

## Final Review Checklist

- [ ] Run `rg -n "KAVYA_EN_ELEVENLABS_VOICE_ID|ELEVENLABS_VOICE_ID|eleven_multilingual_v2|eleven_flash_v2_5|output_format" Kavya` and confirm one English profile, retained non-English branch, blank example values, Docker inclusion, and no real ID.
- [ ] Run `git diff origin/Rakesh...HEAD -- Kavya` and reject any protocol, MCP, handover, RAG, deployment, dashboard, or Flico change.
- [ ] Obtain independent review of redaction, fail-closed behavior, normal/recovery consumers, direct request shape, non-English retention, image closure, and transfer-disabled preservation. Do not deploy or live-test under this plan.