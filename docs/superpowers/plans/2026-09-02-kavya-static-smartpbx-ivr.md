# Kavya Static SmartPBX IVR Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace per-call SmartPBX language-menu TTS with an immediate local μ-law asset containing exactly 300 ms of leading silence.

**Architecture:** `Kavya/smartpbx_session.py` validates and caches one immutable frame-aligned asset, then sends it through the existing generation-aware transport. Docker explicitly includes the asset. Production’s separate 100 ms preroll becomes zero so the only intentional lead is the asset’s 300 ms.

**Tech Stack:** Python 3.11, asyncio, SmartPBX G.711 μ-law at 8 kHz, pytest in GitHub CI, Docker Compose.

## Global Constraints

- SmartPBX only; do not alter Twilio, Flico, dynamic English TTS, or dynamic Sinhala Gemini TTS.
- Preserve DTMF interruption and language-profile ownership.
- Never commit or print provider credentials.
- Do not run local pytest; use GitHub CI for RED/GREEN evidence.
- Stage only named files because the worktree contains unrelated user-owned Graphify changes.

---

### Task 1: Specify the static menu contract

**Files:**
- Modify: `Kavya/tests/test_smartpbx_server.py`
- Modify: `Kavya/tests/test_smartpbx_sinhala_ivr.py`
- Modify: `Kavya/tests/test_smartpbx_deployment.py`

**Interfaces:**
- Consumes: `KavyaSmartPBXSession._speak_language_menu()` and `SmartPBXMediaTransport.send_audio/send_mark/clear_audio`.
- Produces: tests requiring one local asset send, zero `pipeline._speak` calls for the menu, 300 ms of leading silence, frame alignment, replay, and Docker inclusion.

- [ ] **Step 1: Add failing behavior tests**

Add a recording transport with `send_audio()` and `send_mark()`. Patch the loader to return a sentinel local payload and assert start sends that payload, marks delivery, and does not call live TTS. Assert DTMF selection clears the current transport generation and invalid selection replays the same bytes.

- [ ] **Step 2: Add failing asset/deployment tests**

Read `smartpbx_language_menu.ulaw` and assert it is nonempty, `len(asset) % 160 == 0`, and `asset[:2400] == b"\xff" * 2400`. Assert the Dockerfile copies the asset.

- [ ] **Step 3: Push the test-only commit and verify RED in GitHub CI**

Expected: only the new static-IVR contract tests fail because the loader/asset path does not exist and the current menu still calls provider TTS.

---

### Task 2: Implement and package the local IVR asset

**Files:**
- Create: `Kavya/smartpbx_language_menu.ulaw`
- Modify: `Kavya/smartpbx_session.py`
- Modify: `Kavya/Dockerfile`
- Modify: `Kavya/SMARTPBX_RUNBOOK.md`

**Interfaces:**
- Produces: `_load_language_menu_audio() -> bytes`, returning one cached immutable 8 kHz μ-law menu.

- [ ] **Step 1: Generate the immutable asset once**

Use the protected production provider credentials without printing them: canonical ElevenLabs English voice for “For English, press 1.” and Gemini `gemini-3.1-flash-tts-preview` / `Vindemiatrix` for “සිංහල සඳහා, 2 ඔබන්න.” Convert Sinhala PCM24k mono to μ-law8k, prepend `b"\xff" * 2400`, combine, and pad once to a 160-byte boundary.

- [ ] **Step 2: Add the minimal cached loader**

Use `functools.lru_cache(maxsize=1)` and `Path(__file__).with_name("smartpbx_language_menu.ulaw")`. Reject missing, empty, misaligned, or incorrectly prefixed assets before starting the menu task.

- [ ] **Step 3: Replace live menu TTS**

`_speak_language_menu()` sends the cached bytes directly with `transport.send_audio()` and awaits `transport.send_mark("language-menu")`. Retain all existing selection checks so cancellation and `clear_audio()` fence stale bytes.

- [ ] **Step 4: Package and document**

Add `COPY smartpbx_language_menu.ulaw ./` to the explicit Dockerfile allowlist. Document that the fixed menu is local, contains 300 ms lead, and must be regenerated when wording or voices change.

- [ ] **Step 5: Verify GREEN**

Run `python3 -m py_compile smartpbx_session.py`. Push implementation and require the complete GitHub CI matrix, image build/import guard, and all Kavya tests to pass.

---

### Task 3: Guarded production rollout

**Files:**
- Production-only protected change: `/opt/kavya/.env.smartpbx`

**Interfaces:**
- Consumes: exact merged image SHA and digest.
- Produces: healthy SmartPBX runtime using the static IVR with `SMARTPBX_STARTUP_PREROLL_MS=0`.

- [ ] **Step 1: Preflight and back up**

Require zero active calls, handover enabled, healthy SmartPBX/Flico/legacy services, root-only environment permissions, exact previous image identity, and a private environment backup.

- [ ] **Step 2: Deploy exact verified image**

Use the guarded image deployment script and verified digest. Recreate only `kavya-smartpbx`.

- [ ] **Step 3: Apply the timing override transactionally**

Replace the single active `SMARTPBX_STARTUP_PREROLL_MS` assignment with `0`, validate rendered Compose, recreate only SmartPBX, and roll back both image and environment on failure.

- [ ] **Step 4: Verify the caller path**

Verify health, handover, image identity, zero restart count, service isolation, asset hash/shape in the container, and no IVR provider request during one call. Confirm the complete opening word is audible before accepting.
