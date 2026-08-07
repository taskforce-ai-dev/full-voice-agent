# Kavya SmartPBX Call Parity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the English Dialog SmartPBX call path use Kavya's protected canonical English voice and existing English behavior while remaining private, bounded, and transfer-disabled.

**Architecture:** Extract English TTS selection into one protected immutable profile consumed by both ConversationRelay and direct SmartPBX rendering. Keep SmartPBX as an adapter over `MediaStreamSession`; correct v06 event admission and add only finite diagnostics. Deploy only the isolated profile after tests, review, CI/image provenance, and an approved stable-call gate.

**Tech Stack:** Python 3.11, FastAPI/WebSockets, pytest/pytest-asyncio, ElevenLabs streaming TTS, Twilio ConversationRelay, Dialog SmartPBX, Docker Compose, Nginx, GitHub Actions.

## Global Constraints

- Keep Flico's container, configuration, and running path intact.
- Use TDD red-green evidence for behavior changes and review before deployment.
- Keep secrets, MCP keys, voice IDs, call identifiers, and customer data out of Git, diagnostics, dashboard events, status output, and test fixtures.
- Secret rotation, DID routing beyond the temporary sole-DID verification, Dialog credential changes, carrier contract decisions, and any non-English Dialog language selection require asking first.
- Never remove Twilio, enable MCP transfer before its gates, send both account headers, invoke `call_tool` during the MCP diagnostic, switch headers without the specified deterministic 4xx, or weaken the g711 ulaw admission contract.

---

## File map

- Create: `Kavya/english_voice_profile.py`; `Kavya/tests/test_english_voice_profile.py`.
- Modify: `Kavya/server.py:134-140,489-530,1751-1752,1814-1817,2550-2565,2571-2648,3485-3577`.
- Modify: `Kavya/smartpbx_session.py:88-224`; `Kavya/smartpbx_protocol.py:15-155,189-209`; `Kavya/smartpbx_gateway.py:146-315,344-356`.
- Modify: `Kavya/.env.example:18-21,178-202`; `Kavya/docker-compose.yml:88-155`; `Kavya/SMARTPBX_RUNBOOK.md:76-130,154-257`.
- Modify tests: `Kavya/tests/test_smartpbx_protocol.py`, `test_smartpbx_gateway.py`, `test_smartpbx_server.py`, `test_smartpbx_transport.py`, `test_smartpbx_deployment.py`, `test_smartpbx_provider_handover.py`.

Do not modify `Kavya/smartpbx_mcp.py`, `smartpbx_handover.py`, `tools.py`, Flico, dashboard routing, or Twilio. The independent handover plan owns those paths.

### Task 1: Protected canonical English profile

**Files:** Create `Kavya/english_voice_profile.py`; test `Kavya/tests/test_english_voice_profile.py`.

**Interfaces:** `EnglishVoiceProfile(voice_id: str, model_id: str = "eleven_flash_v2_5", output_format: str = "ulaw_8000")`; `load_english_voice_profile(environ: Mapping[str, str]) -> EnglishVoiceProfile`; `VOICE_ENV_KEY = "KAVYA_EN_ELEVENLABS_VOICE_ID"`. `voice_id` is `repr=False`.

- [ ] **Step 1: Write RED tests**

~~~python
def test_profile_is_canonical_and_redacted():
    profile = load_english_voice_profile({VOICE_ENV_KEY: "test-only-voice-marker"})
    assert (profile.model_id, profile.output_format) == ("eleven_flash_v2_5", "ulaw_8000")
    assert "test-only-voice-marker" not in repr(profile)

@pytest.mark.parametrize("value", [None, "", "  "])
def test_profile_fails_closed_when_missing(value):
    with pytest.raises(ValueError, match="canonical English voice is required"):
        load_english_voice_profile({} if value is None else {VOICE_ENV_KEY: value})
~~~

- [ ] **Step 2: Verify RED** — Run `cd Kavya && pytest -q tests/test_english_voice_profile.py`; expect `ModuleNotFoundError`.
- [ ] **Step 3: Implement GREEN**

~~~python
@dataclass(frozen=True)
class EnglishVoiceProfile:
    voice_id: str = field(repr=False)
    model_id: str = "eleven_flash_v2_5"
    output_format: str = "ulaw_8000"

def load_english_voice_profile(environ):
    value = environ.get(VOICE_ENV_KEY, "")
    if not isinstance(value, str) or not value.strip():
        raise ValueError("canonical English voice is required")
    return EnglishVoiceProfile(value.strip())
~~~

No `ELEVENLABS_VOICE_ID` or multilingual fallback is accepted.

- [ ] **Step 4: Verify GREEN** — Run the same command; expect PASS.
- [ ] **Step 5: Commit** — `git add Kavya/english_voice_profile.py Kavya/tests/test_english_voice_profile.py && git diff --cached --check && git commit -m "feat(kavya): add protected English voice profile"`.

### Task 2: Shared Twilio English selection

**Files:** Modify `Kavya/server.py:134-140,489-530,1751-1752,1814-1817`; test `Kavya/tests/test_english_voice_profile.py`.

**Interfaces:** `english_conversation_relay_config(profile: EnglishVoiceProfile) -> dict[str, str]`; `_english_voice_profile() -> EnglishVoiceProfile`.

- [ ] **Step 1: Write RED test**

~~~python
def test_english_relay_config_uses_profile():
    cfg = server.english_conversation_relay_config(EnglishVoiceProfile("test-only-voice-marker"))
    assert cfg["voice"] == "test-only-voice-marker-flash_v2_5"
    assert cfg["tts_provider"] == "ElevenLabs"
    assert server.LANGUAGE_CONFIGS["si"]["voice"] == "si-LK-Standard-A"
~~~

- [ ] **Step 2: Verify RED** — `cd Kavya && pytest -q tests/test_english_voice_profile.py::test_english_relay_config_uses_profile`; expect missing factory.
- [ ] **Step 3: Implement GREEN** — import Task 1, make `_english_voice_profile()` load `os.environ`, and return the existing nonsecret English config plus `"voice": f"{profile.voice_id}-flash_v2_5"`. Remove only the hard-coded English source; call the factory at current Twilio sites 1751 and 1816. Non-English entries remain unchanged; blank key fails before English Twilio/SmartPBX call.
- [ ] **Step 4: Verify GREEN** — `cd Kavya && pytest -q tests/test_english_voice_profile.py tests/test_prompt_policy.py`; expect PASS/no fallback.
- [ ] **Step 5: Commit** — `git add Kavya/server.py Kavya/tests/test_english_voice_profile.py && git diff --cached --check && git commit -m "refactor(kavya): share English relay voice selection"`.

### Task 3: Direct SmartPBX flash μ-law request

**Files:** Modify `Kavya/server.py:3485-3577`, `Kavya/smartpbx_session.py:152-193`; tests `test_english_voice_profile.py`, `test_smartpbx_server.py:211-228`.

**Interfaces:** extend `MediaStreamSession(..., english_voice_profile: EnglishVoiceProfile | None = None)`; `_elevenlabs_request_settings(text: str) -> tuple[str, dict[str, object]]`.

- [ ] **Step 1: Write RED test**

~~~python
@pytest.mark.asyncio
async def test_smartpbx_english_tts_is_flash_ulaw(monkeypatch):
    pipeline = server.MediaStreamSession(None, "en", media_transport=FakeTransport(),
        english_voice_profile=EnglishVoiceProfile("test-only-voice-marker"))
    captured = await capture_elevenlabs_request(monkeypatch, pipeline, "hello")
    assert captured["json"]["model_id"] == "eleven_flash_v2_5"
    assert captured["json"]["output_format"] == "ulaw_8000"
    assert "eleven_multilingual_v2" not in str(captured["json"])
~~~

- [ ] **Step 2: Verify RED** — `cd Kavya && pytest -q tests/test_english_voice_profile.py::test_smartpbx_english_tts_is_flash_ulaw`; expect constructor/request-shape failure.
- [ ] **Step 3: Implement GREEN** — for SmartPBX English require injected profile and request `ELEVENLABS_TTS_URL.format(voice_id=profile.voice_id)` with `{text, model_id: profile.model_id, output_format: profile.output_format}`; inject `server._english_voice_profile()` at session construction. Keep non-English request logic and `smartpbx_transport.py:84-97` unchanged: outbound bytes remain opaque g711 μ-law/8 kHz, no resampling.
- [ ] **Step 4: Verify GREEN** — `cd Kavya && pytest -q tests/test_english_voice_profile.py tests/test_smartpbx_server.py::test_smartpbx_english_pipeline_uses_existing_elevenlabs_tts tests/test_smartpbx_transport.py`; expect PASS.
- [ ] **Step 5: Commit** — `git add Kavya/server.py Kavya/smartpbx_session.py Kavya/tests/test_english_voice_profile.py Kavya/tests/test_smartpbx_server.py && git diff --cached --check && git commit -m "fix(kavya): use canonical voice for SmartPBX English TTS"`.

### Task 4: Protected configuration migration

**Files:** Modify `Kavya/.env.example:18-21,178-202`, `Kavya/docker-compose.yml:88-155`, `Kavya/SMARTPBX_RUNBOOK.md:76-130,154-257`, `Kavya/tests/test_smartpbx_deployment.py`.

**Interfaces:** only root-owned `/opt/kavya/.env.smartpbx` supplies `KAVYA_EN_ELEVENLABS_VOICE_ID`; compose passes that name to `kavya-smartpbx`; missing value fails closed.

- [ ] **Step 1: Write RED tests**

~~~python
def test_smartpbx_receives_only_protected_voice_name():
    env = yaml.safe_load(read_text("docker-compose.yml"))["services"]["kavya-smartpbx"]["environment"]
    assert env["KAVYA_EN_ELEVENLABS_VOICE_ID"] == "${KAVYA_EN_ELEVENLABS_VOICE_ID}"
    assert "ELEVENLABS_VOICE_ID" not in env

def test_examples_never_store_voice_value():
    assert "KAVYA_EN_ELEVENLABS_VOICE_ID=" in read_text(".env.example")
    assert "root-only" in read_text("SMARTPBX_RUNBOOK.md")
~~~

- [ ] **Step 2: Verify RED** — `cd Kavya && pytest -q tests/test_smartpbx_deployment.py -k protected_voice`; expect missing key/general injection failure.
- [ ] **Step 3: Implement GREEN** — template contains exactly empty assignment; SmartPBX compose has only `${KAVYA_EN_ELEVENLABS_VOICE_ID}`; runbook says root operator migrates established value into protected file, runs `chmod 600`, and validates only redacted presence/model/format. Do not echo/cat/paste a value. Preserve blank MCP keys and `SMARTPBX_TRANSFER_DESTINATIONS_JSON={}`.
- [ ] **Step 4: Verify GREEN** — `cd Kavya && pytest -q tests/test_smartpbx_deployment.py`; expect PASS/transfer disabled.
- [ ] **Step 5: Commit** — `git add Kavya/.env.example Kavya/docker-compose.yml Kavya/SMARTPBX_RUNBOOK.md Kavya/tests/test_smartpbx_deployment.py && git diff --cached --check && git commit -m "chore(kavya): provision canonical SmartPBX voice safely"`.

### Task 5: Dialog v06 bounded protocol conformance

**Files:** Modify `Kavya/smartpbx_protocol.py:15-155,189-209`; test `Kavya/tests/test_smartpbx_protocol.py`.

**Interfaces:** `HangupEvent(call_id: str, other_leg_call_id: str, reason: str | None)`; `UnsupportedEvent(failure_class: str = "unsupported_event")`; `validate_event_context(event, context) -> None`.

- [ ] **Step 1: Write RED tests**

~~~python
def test_hangup_has_no_account_requirement_or_reason_requirement():
    event = parse('{"event":"hangup","hangup":{"callId":"call-marker","otherLegCallId":"leg-marker"}}')
    assert event.reason is None

@pytest.mark.parametrize("digit", list("ABCD"))
def test_dtmf_accepts_documented_letters(digit):
    assert parse(json.dumps({"event":"dtmf", "dtmf":{"digit":digit}})).digit == digit

def test_unknown_event_is_fixed_and_private():
    event = parse('{"event":"private-unrecognized-event"}')
    assert event.failure_class == "unsupported_event"
    assert "private-unrecognized-event" not in repr(event)
~~~

- [ ] **Step 2: Verify RED** — `cd Kavya && pytest -q tests/test_smartpbx_protocol.py`; expect mandatory account/reason, rejected A-D, raw UnknownEvent failures.
- [ ] **Step 3: Implement GREEN** — use `_ALLOWED_DTMF_DIGITS = frozenset("0123456789*#ABCD")`; parse optional bounded nonblank reason; compare only callId/otherLegCallId for hangup; replace raw-name `UnknownEvent` with data-free `UnsupportedEvent`. Keep `start` exact g711_ulaw/8000 and `connected`/`stop` known strict extensions.
- [ ] **Step 4: Verify GREEN** — `cd Kavya && pytest -q tests/test_smartpbx_protocol.py tests/test_smartpbx_gateway.py`; expect PASS/non-μ-law still rejected.
- [ ] **Step 5: Commit** — `git add Kavya/smartpbx_protocol.py Kavya/tests/test_smartpbx_protocol.py && git diff --cached --check && git commit -m "fix(kavya): align SmartPBX parser with Dialog v06"`.

### Task 6: Finite privacy-safe lifecycle diagnostics

**Files:** Modify `Kavya/smartpbx_gateway.py:146-315,344-356`; test `Kavya/tests/test_smartpbx_gateway.py`.

**Interfaces:** `_log_lifecycle(stage: Literal["admission","context","session_start","audio","tts","terminal_cleanup"], failure_class: str) -> None`.

- [ ] **Step 1: Write RED test**

~~~python
@pytest.mark.asyncio
async def test_gateway_logs_only_fixed_admission_discriminator(caplog):
    await run(['{"event":"private-unrecognized-event","callId":"private-id"}'])
    assert "stage=admission" in caplog.text and "unsupported_event" in caplog.text
    assert "private-unrecognized-event" not in caplog.text and "private-id" not in caplog.text
~~~

- [ ] **Step 2: Verify RED** — `cd Kavya && pytest -q tests/test_smartpbx_gateway.py::test_gateway_logs_only_fixed_admission_discriminator`; expect dynamic unknown event behavior.
- [ ] **Step 3: Implement GREEN** — allow only stated stages; map parser, context, start, feed, existing safe TTS, and cleanup failures; remove `unknown_events_total` and any raw payload/name diagnostic. No status history.
- [ ] **Step 4: Verify GREEN** — `cd Kavya && pytest -q tests/test_smartpbx_gateway.py tests/test_smartpbx_server.py::test_dialog_media_logs_never_contain_transcript_agent_text_or_call_id tests/test_smartpbx_transport.py`; expect PASS.
- [ ] **Step 5: Commit** — `git add Kavya/smartpbx_gateway.py Kavya/tests/test_smartpbx_gateway.py && git diff --cached --check && git commit -m "feat(kavya): add private SmartPBX lifecycle diagnostics"`.

### Task 7: English behavior parity through the existing adapter

**Files:** Modify `Kavya/smartpbx_session.py:88-224`, `Kavya/server.py:2550-2565,2631-2648,2764-2935,3337-3478,4073-4257`, `Kavya/tests/test_smartpbx_server.py`, `Kavya/tests/test_smartpbx_provider_handover.py`, runbook.

**Interfaces:** retain `_make_stt(on_final_result, on_interim_result, lang, privacy_safe)`, `_build_system_prompt`, provider `get_tools*`, retrieval/booking tools, `TOOL_FILLERS`, `process_post_call_data`, `_send_tts_done`, `_cancel_reprompt`, `enter_transfer_pending`.

- [ ] **Step 1: Write RED parity tests**

~~~python
@pytest.mark.asyncio
async def test_session_reuses_english_pipeline_and_stt():
    session, pipeline, stt, _ = make_session(post_call_processor=async_noop)
    await session.start()
    assert pipeline.lang == "en" and pipeline.system_prompt == server._build_system_prompt("en")
    assert stt.kwargs == {"lang":"en", "privacy_safe":True, **stt.callback_kwargs}

@pytest.mark.asyncio
async def test_direct_barge_in_clears_queue_and_pending_transfer_cancels_reprompt():
    pipeline = smartpbx_pipeline()
    before = pipeline._speak_generation
    await pipeline._on_stt_interim("speech")
    assert pipeline._speak_generation == before + 1 and pipeline._media_transport.clears == 1
    await pipeline._send_tts_done(); await pipeline.enter_transfer_pending()
    assert pipeline._reprompt_task is None
~~~

- [ ] **Step 2: Verify RED** — `cd Kavya && pytest -q tests/test_smartpbx_server.py tests/test_smartpbx_provider_handover.py`; expect profile adapter wiring assertion before Task 3 integration.
- [ ] **Step 3: Implement GREEN** — keep `_load_runtime_defaults` the sole SmartPBX constructor. It uses English prompt/welcome, existing provider tools/RAG/booking/post-call and `_make_stt(..., lang="en", privacy_safe=True)`. Do not fork LLM streams/fillers/tools. Preserve reprompt cap/cancel and transfer-pending suppression. Runbook states direct transport can clear queued local frames/invalidate generation but cannot recall carrier-buffered audio; it also identifies ConversationRelay-only recognition settings as non-reproducible.
- [ ] **Step 4: Verify GREEN** — `cd Kavya && pytest -q tests/test_smartpbx_server.py tests/test_smartpbx_provider_handover.py tests/test_smartpbx_gateway_transfer_pending.py tests/test_smartpbx_post_call.py`; expect PASS.
- [ ] **Step 5: Commit** — `git add Kavya/server.py Kavya/smartpbx_session.py Kavya/SMARTPBX_RUNBOOK.md Kavya/tests/test_smartpbx_server.py Kavya/tests/test_smartpbx_provider_handover.py && git diff --cached --check && git commit -m "test(kavya): lock SmartPBX English behavior parity"`.

### Task 8: Regression, security, review, CI, and image gate

**Files:** modify only an implementation defect proven by a failing listed test; no scope growth.

- [ ] **Step 1: Run targeted suite** — `cd Kavya && pytest -q tests/test_english_voice_profile.py tests/test_smartpbx_protocol.py tests/test_smartpbx_gateway.py tests/test_smartpbx_server.py tests/test_smartpbx_transport.py tests/test_smartpbx_deployment.py tests/test_smartpbx_provider_handover.py tests/test_smartpbx_handover.py tests/test_smartpbx_post_call.py`; expected PASS.
- [ ] **Step 2: Run full/security checks** — `cd Kavya && pytest -q && python -m compileall -q . && git diff origin/main...HEAD --check`; expected PASS. Inspect only diff/template names, never runtime protected files.
- [ ] **Step 3: Independent review** — require review of no fallback, flash/ulaw request, g711 admission, diagnostic privacy, adapter reuse, disabled transfer, Docker/runbook isolation. Fix any finding red-first, then rerun Steps 1-2.
- [ ] **Step 4: CI/image** — `gh pr checks 209 --watch --fail-fast`; expected all required checks PASS. Record reviewed full SHA/CI short SHA and prove image label `org.opencontainers.image.revision` equals full SHA before deployment.

### Task 9: Operator-gated isolated deployment and stable call

**Files:** modify runbook only if a prior task discovered a missing exact operator step.

- [ ] **Step 1: Protected provision/live approval** — operator approves temporary sole-DID action and, as root, migrates established voice to `/opt/kavya/.env.smartpbx`, `chmod 600`; verify only redacted presence/model/output-format. Never paste it anywhere.
- [ ] **Step 2: Isolated deploy** — run exact runbook `docker compose --env-file .env.smartpbx --profile smartpbx config > /dev/null`, `up -d --force-recreate --pull never kavya-smartpbx`, `wait_for_smartpbx_ready`; expected only SmartPBX recreates, Flico/Twilio unchanged, transfer disabled.
- [ ] **Step 3: Supervised stable-call proof** — approved live call must prove expected Kavya voice, intelligible bidirectional audio, normal question, RAG/booking turn, filler, interruption within stated limitation, normal end, no protocol admission error. Record redacted pass/fail only; greeting alone fails.
- [ ] **Step 4: Rollback/gate** — on failure perform isolated runbook rollback and restore route only with approval; do not touch Twilio/Flico. On pass retain redacted evidence outside Git, keep MCP transfer disabled, and only then unlock the separate handover plan.

## Self-review

- Spec coverage: Tasks 1-4 cover protected shared voice and direct flash/ulaw; 5-6 cover v06 and privacy; 7 covers prompt/tools/RAG/booking/STT/re-prompt/fillers/barge-in/post-call; 8-9 cover tests, review, CI, image, deploy/rollback/live proof.
- No placeholders: protected values/actions are explicitly operator-supplied and intentionally absent; no TODO/TBD exists.
- Type consistency: `EnglishVoiceProfile` flows Task 1 -> Tasks 2-3 -> Task 7; `HangupEvent.reason`/`UnsupportedEvent.failure_class` flow Task 5 -> Task 6.
- Scope: account-header/MCP/destination/transfer work is excluded to the next independent plan.

## Execution handoff

Plan saved at `docs/superpowers/plans/2026-08-07-kavya-smartpbx-call-parity.md`. Execute subagent-driven (fresh implementer/review per task) or inline with checkpoints after Tasks 3, 6, 8, and 9. Only Task 9 needs protected operator values and approved live action.
