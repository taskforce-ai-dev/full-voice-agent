# Kavya Rime and Azure Sinhala Canary Design

**Date:** 2026-09-05
**Status:** Approved for implementation
**Scope:** Direct SmartPBX Sinhala only

## Goal

Reduce Sinhala response latency and make caller turns less fragmented without changing the stable Direct SmartPBX English path, either Twilio path, cached Sinhala IVR/fillers, or the existing Gemini safety fallback.

The canary must leave two independent rollback controls: switch Sinhala TTS back to Gemini, or disable the Azure segmentation override while keeping the same image.

## Production evidence

The inspected Sinhala call completed normally with 35 started and summarized turns. SmartPBX audio delivery was healthy while audio was available: zero dropped frames, zero queue underruns, and approximately 20 ms frame cadence.

Rime returned successful HTTP responses and never invoked Gemini fallback. Its first-media latency nevertheless grew with reply length, ranging from roughly 3 seconds for short text to 12.4 seconds for a long reply. The implementation explains the relationship: it buffers the entire MP3 response, waits for ffmpeg to decode the whole file, and only then starts enqueueing audio.

Azure `si-LK` produced many short or fragmented interim/final hypotheses, especially for mixed Sinhala and English number dictation. Microsoft lists Sinhala real-time transcription but does not list `si-LK` phrase-list support. The existing phrase list is therefore not a documented accuracy control and must not be treated as one.

## Considered approaches

### 1. Streaming native G.711 mu-law over HTTP — selected

Request `audio/PCMU` with an 8 kHz sampling rate from Rime and forward response chunks incrementally into the existing SmartPBX transport. This removes both the full-response MP3 buffer and ffmpeg from the live Rime path. It retains the existing one-request-per-sentence lifecycle, making it substantially smaller than a WebSocket rewrite.

Rime documents Arcana's HTTP endpoint as streaming and explicitly supports headerless G.711 mu-law. Rime's latency guidance recommends streaming and requesting the final telephony format directly.

### 2. Streaming linear PCM with in-process mu-law conversion — fallback

If the exact Arcana, Chandani, Sinhala tuple rejects `audio/PCMU`, request `audio/L16`, preserve sample alignment across chunks, and encode it to mu-law incrementally. This still removes ffmpeg and full-body buffering but introduces byte-alignment and codec-conversion responsibilities.

### 3. Persistent Rime WebSocket — deferred

WebSockets could eventually accept incremental LLM text and support explicit interruption commands, but they add per-call connection ownership, reconnect, flush, clear, and teardown behavior. That is too large for this latency canary and would create unnecessary risk to the stable voice lifecycle.

## TTS design

The Direct SmartPBX Sinhala Rime request will use the existing endpoint, model, speaker, language and synthesis controls. Only the output contract changes to `Accept: audio/PCMU` and `samplingRate: 8000`.

The response body will be consumed as an asynchronous byte stream. Received bytes will be forwarded in order to the existing `SmartPBXMediaTransport`, which already owns 160-byte wire framing, 20 ms pacing, generation fencing, queue clearing and delivery marks. The Rime layer must not add its own playback sleeps or pad intermediate network chunks.

The existing response-byte ceiling and request timeout remain bounded. Cancellation closes the HTTP response and propagates. Every chunk rechecks the current speak generation and delivery ownership before enqueueing.

The fallback boundary remains audio acceptance:

- A missing key, timeout, bad status, wrong content type, empty response, oversized response, or transport failure before any accepted Rime audio invokes Gemini exactly once.
- After any Rime audio is accepted, Gemini must never replay the sentence. A later Rime failure terminates that generation safely.
- A stale generation, barge-in, transfer or teardown must prevent subsequent Rime bytes and the final delivery mark from escaping.

Cached Sinhala IVR and filler assets continue using their existing local/Gemini cache path. Direct English remains ElevenLabs. Twilio Sinhala remains OpenAI. No English or Twilio routing code is changed.

## STT design

Add `SMARTPBX_SINHALA_AZURE_SEGMENTATION_SILENCE_MS` as a Direct SmartPBX Sinhala-only Azure setting. `0` or omission means disabled and preserves Azure's service default. Active values are clamped to Azure's documented 100–5000 ms range.

The first production canary value will be 800 ms. The purpose is to give short Sinhala and mixed-language number phrases slightly more acoustic context before Azure commits a final result. This may improve fragmented number capture at the cost of a small endpoint delay, so it must remain independently reversible and measured rather than becoming an unconditional global default.

The property is set before recognizer construction only when the selected language is Sinhala. It does not alter shared endpointing timers, final grace, capture grace, barge-in thresholds, English, Google, or Twilio behavior.

No phrase-list weight is added. Microsoft does not document phrase lists for `si-LK`; increasing an unsupported control would create false confidence rather than a reliable fix. No automatic language identification or second recognizer is introduced in this canary because either would add latency and substantially expand lifecycle and selection complexity.

## Observability

Rime telemetry will remain bounded and privacy-safe. It may record provider, closed-enum outcome, HTTP status, total audio bytes, first-chunk latency, total request duration and chunk count. It must never include text, caller digits, response bodies, credentials or exception messages.

The Azure startup diagnostic may record only whether the Sinhala segmentation override is enabled and its bounded millisecond value. Existing final/interim, endpointing, turn-summary, capture and DTMF diagnostics remain authoritative.

## Verification

Tests must demonstrate:

- The first Rime frame is accepted before response EOF.
- The request uses `audio/PCMU` at 8 kHz and ffmpeg is not invoked.
- Arbitrary network chunk boundaries preserve exact byte order; only the transport performs wire framing.
- Silent pre-audio failures fall back to Gemini exactly once.
- Mid-stream failures after accepted audio never fall back or replay speech.
- Generation supersession, barge-in, transfer, teardown and cancellation stop stale streaming and do not emit `tts_done`.
- Response-size and timeout boundaries remain enforced incrementally.
- Telemetry is bounded and contains no caller text, digits, audio or secrets.
- The Azure property is absent at zero, clamped when enabled, and applied only to Sinhala.
- Direct English and both Twilio paths retain their existing provider and timing behavior.
- Compose, example environment, runbook and deployment tests remain synchronized.

Local verification is limited to `python3 -m py_compile`, `bash -n` where applicable, and `git diff --check`. Kavya pytest is intentionally not run in the local sandbox; GitHub CI is the behavioral authority.

## Guarded rollout

1. Confirm the exact Rime Arcana/Chandani/Sinhala request accepts `audio/PCMU` at 8 kHz without logging credentials or audio.
2. Open a PR from `Rakesh` and require the complete Kavya CI suite to pass.
3. Merge only after Sol's independent review.
4. Run the read-only GHCR probe, publish the immutable image, and record its full revision and digest.
5. Deploy with Rime selected and the Azure Sinhala segmentation override set to 800 ms.
6. Verify health, exact image identity, service isolation and zero active sessions before the test call.
7. Run one ordinary Sinhala conversation and one deliberate phone-number capture.

Acceptance targets:

- Rime first media no longer scales with total response length; target p95 below 4 seconds from TTS request and materially better than the 12.4-second observed worst case.
- Zero transport drops and queue underruns.
- No duplicated Rime/Gemini speech and no Gemini fallback after accepted Rime audio.
- One coherent caller turn for an ordinary sentence.
- A complete phone number is captured without repeated fragmented prompts in the controlled test, or the logs clearly demonstrate Azure recognition—not endpointing—as the remaining limit.

Rollback does not require a code rollback:

- Set `SMARTPBX_SINHALA_TTS_PROVIDER=gemini` to remove Rime.
- Set `SMARTPBX_SINHALA_AZURE_SEGMENTATION_SILENCE_MS=0` or remove it to restore Azure's default segmentation.
- Guarded-recreate the same reviewed image and reverify its revision and health.

## Authoritative sources

- Rime Arcana streaming HTTP and supported formats: https://docs.rime.ai/api-reference/arcana/http
- Rime latency guidance: https://docs.rime.ai/docs/latency
- Microsoft Azure Speech language support: https://learn.microsoft.com/en-us/azure/ai-services/speech-service/language-support
- Azure Speech segmentation property: https://learn.microsoft.com/en-us/cpp/cognitive-services/speech/microsoft-cognitiveservices-speech-namespace
