# SmartPBX Sinhala IVR and Gemini TTS

## Decision

Add an initial language-selection state to the Dialog SmartPBX session only:

- `1` selects the existing English Kavya experience.
- `2` selects Sinhala.
- No selection defaults to English after a short, bounded wait.
- Any invalid first digit gets one menu replay; a second invalid digit defaults to
  English.

The selected-language session uses the existing Claude LLM and the existing STT
provider configuration.  Sinhala speech output uses Gemini
`gemini-3.1-flash-tts-preview`, streamed as 24 kHz PCM and converted through
the existing 24 kHz PCM to 8 kHz G.711 mu-law framing path.  It does not fall
back to OpenAI or any other TTS provider.

This is deliberately SmartPBX-only.  The Twilio IVR, Twilio routes, Twilio
codec behaviour, English voice path, Claude model, and STT selection are out
of scope.

## Current boundaries

`KavyaSmartPBXSession` currently creates `MediaStreamSession` with `lang="en"`
and directly starts its STT engine.  Dialog DTMF events are validated in
`smartpbx_gateway.py` and forwarded to the session.  After a call is active,
`MediaStreamSession.feed_dtmf` is the established keypad-capture path.

`MediaStreamSession._speak` routes English to ElevenLabs and Sinhala to the
streaming OpenAI 24 kHz PCM implementation.  That implementation already
handles generation cancellation, conversion to SmartPBX-compatible mu-law,
and sentence-delivery accounting.  Gemini must use the same media framing and
delivery contracts rather than create a second transport path.

## Approaches considered

1. **Recommended: a SmartPBX-only selection state plus streamed Gemini TTS.**
   The session consumes exactly the initial selection digit, then starts the
   normal pipeline in the selected language.  Gemini audio is streamed without
   a cross-provider fallback.  It keeps the existing English path unchanged
   and minimises first-audio delay.

2. **Batch Gemini synthesis.**  This would wait for an entire response before
   sending audio.  It is simpler at the HTTP boundary but would discard the
   sentence-level streaming behaviour that makes Kavya conversational, so it
   is rejected.

3. **Gemini Live speech-to-speech.**  This would change the agent architecture,
   provider semantics, and observability substantially.  It is not needed to
   add Sinhala output and is rejected for this change.

## Call flow

1. On SmartPBX start, the session enters `awaiting_language` before starting
   caller STT.  It plays one short bilingual menu as two ordered speech
   segments: English through the existing English output path, then Sinhala
   through the Sinhala output path.  This avoids asking a single voice engine
   to pronounce both languages poorly.
2. While awaiting selection, caller audio is intentionally not passed to STT
   and the first DTMF digit is consumed by the menu state.  A received `1` or
   `2` cancels any unplayed menu audio, selects `en` or `si`, and starts the
   normal pipeline once.
3. On timeout, or after the second invalid selection, the session selects
   English and starts the normal pipeline once.  A first invalid digit replays
   the menu once.  The caller is never left waiting indefinitely.
4. Once the pipeline starts, all later DTMF follows the existing
   `MediaStreamSession.feed_dtmf` behaviour unchanged.  This preserves phone
   number/keypad capture and prevents the IVR from consuming real caller
   digits.
5. The chosen language is used consistently for `MediaStreamSession`, STT
   construction, greeting, prompts, tool fillers, and post-call processing.
   The SmartPBX transfer tool remains English-only, matching the current
   prompt/tool contract; selecting Sinhala must not silently expose it.

## Gemini TTS boundary

The implementation will add one narrow Sinhala TTS adapter beside
`_tts_openai`:

- Call the Gemini Interactions streaming API with the model
  `gemini-3.1-flash-tts-preview` and a configured prebuilt voice.
- Accept only audio deltas, base64-decode them as 16-bit mono 24 kHz PCM, keep
  sample alignment across chunk boundaries, and reuse the existing rate
  conversion, mu-law conversion, 640-byte SmartPBX media frames, generation
  fencing, cancellation, and `_send_tts_done` semantics.
- Treat a missing API key, unavailable client, non-audio-only response, invalid
  audio, timeout, HTTP failure, or exception as a failed Gemini attempt.  End
  that sentence through the normal TTS failure path; do not retry it through
  OpenAI or any other provider, and do not mix providers in one call.
- Do not log prompt text, raw audio, API keys, caller data, or model response
  bodies.  A privacy-safe SmartPBX diagnostic may record provider and a closed
  failure class only.

The Gemini API credential remains exclusively in root-owned
`/opt/kavya/.env.smartpbx`.  It is never added to source, examples, tests, CI,
logs, or Git history.  The compose environment allowlist already carries
`GEMINI_API_KEY`; any new non-secret selector variables must be added
explicitly to that allowlist.

## Configuration and rollback

Use SmartPBX-specific Gemini configuration so this change cannot alter Twilio:

- `SMARTPBX_SINHALA_GEMINI_TTS_MODEL`, defaulting to
  `gemini-3.1-flash-tts-preview`.
- A bounded Gemini TTS timeout and a configured Gemini voice, with safe
  source defaults documented in `.env.example` without secrets.

The first production canary uses Gemini only from the root-owned SmartPBX
environment.  If it is not acceptable, roll back to the prior reviewed image;
there is no provider-switch rollback.  English is unaffected either way.

## Test and acceptance matrix

The implementation is test-first and must prove:

- SmartPBX `1`, `2`, timeout, and invalid-selection paths each start exactly
  one pipeline with the correct language.
- The initial selection digit is consumed, while a later DTMF number reaches
  the existing capture path unchanged.
- The selected language reaches STT construction, greeting/prompt selection,
  and post-call invocation.
- English SmartPBX remains on its current TTS path.
- Gemini PCM chunks preserve alignment/frame order, obey cancellation and
  generation fencing, and complete delivery exactly once.
- Each Gemini failure class has one privacy-safe, closed-class diagnostic and
  never invokes OpenAI or another TTS provider.
- Telemetry contains no transcript, phone number, audio, or secret.

CI will run the complete Kavya suite.  Local validation is limited to syntax
and diff checks; it must not be described as behavioural proof.

## Sources

- Google AI for Developers, [Speech generation](https://ai.google.dev/gemini-api/docs/speech-generation)
- Google AI for Developers, [Gemini 3.1 Flash TTS Preview](https://ai.google.dev/gemini-api/docs/models/gemini-3.1-flash-tts-preview)
- Google AI for Developers, [Interactions API migration notes](https://ai.google.dev/gemini-api/docs/interactions-breaking-changes-may-2026)
