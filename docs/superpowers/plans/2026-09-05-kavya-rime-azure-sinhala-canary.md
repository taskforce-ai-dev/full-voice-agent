# Kavya Rime and Azure Sinhala Canary Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stream Rime Arcana Sinhala speech directly as 8 kHz G.711 mu-law and canary an independently reversible 800 ms Azure Sinhala segmentation window.

**Architecture:** Direct SmartPBX Sinhala continues to use the existing Rime selector and Gemini fallback, but the Rime request returns an async PCMU byte stream that feeds the existing transport immediately instead of buffering MP3 and invoking ffmpeg. Azure receives one default-off, Sinhala-only segmentation property; every other provider, language and transport path remains unchanged.

**Tech Stack:** Python 3.11, asyncio, httpx 0.28.1, Azure Cognitive Services Speech SDK 1.51.1, pytest in GitHub Actions only, Docker Compose, GHCR guarded Kavya deployment.

## Global Constraints

- Scope is Direct SmartPBX Sinhala only.
- Direct SmartPBX English remains ElevenLabs and must not change.
- Twilio Sinhala remains OpenAI and both Twilio paths must not change.
- Cached Sinhala IVR and filler assets remain on their existing local/Gemini cache path.
- Rime uses `modelId=arcana`, `speaker=chandani`, `lang=si`, `Accept: audio/PCMU`, and `samplingRate=8000`.
- Gemini may run exactly once only when Rime fails before any audio is accepted; it must never replay after accepted Rime audio.
- `SMARTPBX_SINHALA_AZURE_SEGMENTATION_SILENCE_MS=0` means disabled; active values are clamped to `[100, 5000]`; production canary value is `800`.
- Do not add Azure phrase-list weighting, automatic language identification, a second recognizer, Rime WebSockets, or sentence-level Rime fan-out.
- Telemetry must never contain caller text, digits, names, provider bodies, audio, credentials or exception messages.
- Do not use Graphify.
- Do not run pytest locally. Use `python3 -m py_compile`, `bash -n` where applicable, and `git diff --check`; GitHub CI is the behavioral authority.
- Preserve the unrelated untracked plan and `graphify-out/.graphify_analysis.json`.
- Commit and push as `thiva2k <178917250+thiva2k@users.noreply.github.com>` on `Rakesh`.

---

### Task 1: Prove the exact Rime PCMU contract

**Files:**
- Modify: none

**Interfaces:**
- Consumes: protected `RIME_API_KEY` from `/opt/kavya/.env.smartpbx` without printing it.
- Produces: a content-free record of HTTP status, response content type, first-byte latency, total latency, chunk count and byte count for Arcana/Chandani/Sinhala at 8 kHz PCMU.

- [ ] **Step 1: Send one bounded static Sinhala probe**

Run a short remote Python or curl request that loads the key inside the production process, sends a fixed non-caller Sinhala greeting, requests `audio/PCMU` with `samplingRate: 8000`, discards the audio bytes, and prints only bounded numeric timings, status, content type, chunk count and byte count.

- [ ] **Step 2: Enforce the decision gate**

Expected: HTTP 200, an audio/PCMU-compatible content type, non-empty audio, and at least one response chunk. If this fails, stop the PCMU design and use the design's `audio/L16` fallback; do not deploy an assumed wire format.

---

### Task 2: Add RED behavior contracts

**Files:**
- Modify: `Kavya/tests/test_smartpbx_rime_tts.py`
- Modify: `Kavya/tests/test_azure_stt_phrase_list.py`
- Modify: `Kavya/tests/test_smartpbx_deployment.py`

**Interfaces:**
- Consumes: current `_request_rime_arcana_mp3`, `_decode_rime_arcana_mp3_to_mulaw`, `MediaStreamSession._tts_rime_sinhala`, `AzureSTTStream.start`, and Compose/runbook env contracts.
- Produces: failing tests for `_stream_rime_arcana_mulaw(text: str) -> AsyncIterator[bytes]` and `SMARTPBX_SINHALA_AZURE_SEGMENTATION_SILENCE_MS`.

- [ ] **Step 1: Write the Rime stream-first tests**

Add deterministic async fakes that yield one non-frame-aligned PCMU chunk, block before EOF, then yield the remainder. Assert that the first transport audio is accepted before EOF, bytes preserve exact order, `Accept` is `audio/PCMU`, payload sampling rate is 8000, and ffmpeg is never invoked.

- [ ] **Step 2: Write Rime ownership and fallback tests**

Cover empty/pre-first-byte failure falling back once, mid-stream failure after accepted audio never falling back, generation supersession dropping subsequent chunks and marks, incremental response-size enforcement, cancellation closing the response, and bounded privacy-safe timing telemetry.

- [ ] **Step 3: Write Azure configuration tests**

Extend the fake Azure `SpeechConfig` to capture `set_property`. Assert zero/omitted does not set `Speech_SegmentationSilenceTimeoutMs`, 800 sets it only for `lang == "si"`, out-of-range values clamp to 100 and 5000, and English remains untouched.

- [ ] **Step 4: Write deployment contract tests**

Assert the SmartPBX Compose service allowlists `${SMARTPBX_SINHALA_AZURE_SEGMENTATION_SILENCE_MS:-0}`, `.env.example` documents zero, and the runbook documents the 800 ms canary and zero rollback. Assert the Twilio service does not receive the variable.

- [ ] **Step 5: Verify syntax, commit and prove RED in GitHub CI**

Run:

```bash
python3 -m py_compile Kavya/tests/test_smartpbx_rime_tts.py Kavya/tests/test_azure_stt_phrase_list.py Kavya/tests/test_smartpbx_deployment.py
git diff --check
```

Expected: both commands exit 0. Commit only the three test files, push `Rakesh`, open or update the PR, and run CI. Expected CI: the new behavior tests fail because the stream helper and Azure property do not exist; unrelated suites remain green. Record the failing run URL and exact expected failures.

---

### Task 3: Implement native streaming Rime PCMU

**Files:**
- Modify: `Kavya/server.py`
- Modify: `Kavya/SMARTPBX_RUNBOOK.md`

**Interfaces:**
- Consumes: `_rime_arcana_request_payload`, `_RIME_ARCANA_TTS_MAX_RESPONSE_BYTES`, `_RimeArcanaTTSFailure`, `_send_media_audio`, `_owns_sinhala_tts_stream`, `_mark_tts_audible`, and `_send_tts_done`.
- Produces: `_stream_rime_arcana_mulaw(text: str) -> AsyncIterator[bytes]`; `_tts_rime_sinhala` consumes it directly.

- [ ] **Step 1: Replace the buffered request helper**

Implement an async generator that validates the key, requests `Accept: audio/PCMU`, sends the existing payload with `samplingRate=8000`, validates HTTP status and content type before yielding, counts bytes incrementally, raises `response_too_large` on the first byte above the ceiling, classifies timeout/HTTP/transport/empty outcomes, and closes its response on normal exit or cancellation.

- [ ] **Step 2: Remove live Rime ffmpeg decoding**

Delete `_decode_rime_arcana_mp3_to_mulaw` and all Rime call sites that buffer MP3 or launch ffmpeg. Do not remove ffmpeg from the Docker image because other established paths may still require it.

- [ ] **Step 3: Stream directly through the existing transport**

Update `_tts_rime_sinhala` to iterate provider bytes and call `_send_media_audio` without local 640-byte slicing, sleeps or padding. Check generation and delivery ownership before every send and after every await. Mark first chunk/audibility only after a successful send. Emit exactly one final `tts_done` only after clean stream completion and current-generation ownership.

- [ ] **Step 4: Preserve the fallback boundary**

Track whether any audio was accepted. Route classified failures to Gemini once only while false. After it becomes true, log a terminal Rime failure and return without Gemini. Propagate `CancelledError` after ensuring the HTTP stream is closed.

- [ ] **Step 5: Add privacy-safe Rime timing telemetry**

Extend the closed Rime event contract with bounded first-chunk milliseconds, total milliseconds, chunk count and total audio bytes. Update the runbook allowlist. Do not log text, caller data, audio, response bodies, keys or exception strings.

- [ ] **Step 6: Verify syntax and commit**

Run:

```bash
python3 -m py_compile Kavya/server.py Kavya/tests/test_smartpbx_rime_tts.py
git diff --check
```

Expected: both commands exit 0. Commit the Rime implementation and its runbook telemetry contract.

---

### Task 4: Implement the reversible Azure Sinhala segmentation canary

**Files:**
- Modify: `Kavya/server.py`
- Modify: `Kavya/docker-compose.yml`
- Modify: `Kavya/.env.example`
- Modify: `Kavya/SMARTPBX_RUNBOOK.md`

**Interfaces:**
- Consumes: `_parse_clamped_int`, `AzureSTTStream.start`, `azure_speech.PropertyId.Speech_SegmentationSilenceTimeoutMs`.
- Produces: integer `SMARTPBX_SINHALA_AZURE_SEGMENTATION_SILENCE_MS`; zero disables the property.

- [ ] **Step 1: Parse the default-off value**

Use the existing bounded integer parsing pattern, preserving zero as disabled and clamping nonzero configured values to `[100, 5000]`. Invalid and blank input must resolve to zero rather than changing Azure behavior.

- [ ] **Step 2: Apply it only to Sinhala Azure recognition**

Before constructing the recognizer, call `speech_config.set_property(azure_speech.PropertyId.Speech_SegmentationSilenceTimeoutMs, str(value))` only when `self._lang == "si"` and value is nonzero. Emit a content-free startup diagnostic containing only enabled/disabled and bounded milliseconds.

- [ ] **Step 3: Synchronize operations configuration**

Add the variable only to `kavya-smartpbx` with Compose default zero. Document zero in `.env.example`, the production canary value 800 in the runbook, and rollback by zero/omission. Do not add it to the legacy `kavya` service.

- [ ] **Step 4: Verify syntax and commit**

Run:

```bash
python3 -m py_compile Kavya/server.py Kavya/tests/test_azure_stt_phrase_list.py Kavya/tests/test_smartpbx_deployment.py
git diff --check
```

Expected: both commands exit 0. Commit the Azure/config implementation.

---

### Task 5: Integrate, review, merge and deploy

**Files:**
- Review: every file changed since `53848e2b8c0979bd3c232797ec403fcb87d0bc28`
- Modify only if review or CI finds a scoped defect.

**Interfaces:**
- Consumes: Tasks 1–4 and the guarded Kavya deployment workflows.
- Produces: a reviewed PR, green CI, immutable GHCR image, guarded production deployment, and a test-call handoff.

- [ ] **Step 1: Run Sol's independent review**

Inspect the complete diff for scope isolation, streamed-byte correctness, timeout/cancellation cleanup, generation ownership, exactly-once fallback, telemetry privacy, config drift, and English/Twilio preservation. Resolve every required finding before acceptance.

- [ ] **Step 2: Run static verification**

Run:

```bash
python3 -m py_compile Kavya/server.py Kavya/tests/test_smartpbx_rime_tts.py Kavya/tests/test_azure_stt_phrase_list.py Kavya/tests/test_smartpbx_deployment.py
git diff --check
```

Expected: exit 0 with no output from `git diff --check`.

- [ ] **Step 3: Prove GREEN in GitHub CI**

Push the corrected `Rakesh` head. Require all repository checks, including the complete Kavya suite, to pass. Record the exact head SHA and successful Actions run URL.

- [ ] **Step 4: Merge the PR**

Verify the PR head still equals the reviewed SHA, merge through GitHub, and record the resulting immutable `main` SHA.

- [ ] **Step 5: Run the guarded Kavya release**

Run the read-only GHCR probe for the merge SHA. After it passes, run the build-only publisher. Record the image tag and digest. On production, back up `.env.smartpbx`, set `SMARTPBX_SINHALA_TTS_PROVIDER=rime` and `SMARTPBX_SINHALA_AZURE_SEGMENTATION_SILENCE_MS=800`, then deploy only through `/opt/kavya/scripts/deploy_smartpbx_image.sh` using the exact tag, full SHA and digest.

- [ ] **Step 6: Verify production and hand off the call test**

Verify `/health`, `/smartpbx/status`, exact image revision/digest, SmartPBX isolation, zero active sessions, and healthy legacy Kavya/Flico services. Ping the user to place one ordinary Sinhala call and one phone-number capture call. Tail only the authorized call window and evaluate the acceptance targets from the design.
