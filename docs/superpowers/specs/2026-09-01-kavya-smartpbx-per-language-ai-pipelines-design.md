# Kavya SmartPBX Per-Language AI Pipelines

## Decision

Resolve one complete, immutable AI pipeline profile when the caller makes the
SmartPBX IVR selection:

| IVR selection | STT | LLM | TTS |
| --- | --- | --- | --- |
| `1` English | Existing production English STT configuration | Existing Claude configuration | Existing ElevenLabs configuration |
| `2` Sinhala | Existing Azure `si-LK`; unsupported challengers remain offline-only | Gemini `gemini-3.7-flash` | Existing Gemini `gemini-3.1-flash-tts-preview`, voice `Vindemiatrix` |

Claude remains available to Sinhala only as an automatic technical-failure
fallback. It is not used for ordinary Sinhala turns. A Gemini failure can make
the current call sticky-degrade to Claude through the existing per-session
failover state, but it must never change another call or the English profile.

Gemini 3.5 Transcribe Live is not a production Sinhala candidate at this time.
It accepts raw mono 16-bit PCM at 16 kHz and offers interim/final events plus
custom vocabulary, but Google's published supported-language table does not
include Sinhala or `si-LK`. A SmartPBX mu-law harness would therefore need an
explicit decode/resample boundary. It may be evaluated offline against
controlled pilot audio; it must not receive live caller traffic until Google
documents Sinhala support or a separately approved exception defines sufficient
empirical evidence.

Google Cloud's general Speech-to-Text V2 language table lists Sinhala `si-LK`
for `chirp_2`, but the method-specific Chirp 2 documentation limits
`StreamingRecognize` to a language list that excludes Sinhala. The general
table therefore supports an offline `Recognize`/`BatchRecognize` evaluation,
not a live-call integration. Azure remains the only supported live Sinhala STT
in this design until the method-specific streaming list changes.

This design supersedes only the earlier design's decision to keep one LLM and
one STT provider selection for both SmartPBX languages. It preserves the
already-shipped SmartPBX IVR and Gemini Sinhala TTS design.

## Scope and invariants

- SmartPBX only. Do not change Twilio routes, ConversationRelay, Twilio media,
  or the dormant Twilio Sinhala path.
- Pressing `1`, an IVR timeout, or the second invalid IVR digit must preserve
  the reviewed English behavior: English prompt, English STT, Claude, current
  tools, ElevenLabs, capture behavior, handover, telemetry, timeouts, and
  latency controls.
- Pressing `2` must select a Sinhala session profile before STT starts and
  before conversation history exists.
- The existing bilingual pre-selection IVR menu remains intact. It temporarily
  uses `lang="si"` to speak the Sinhala menu line through Gemini TTS before a
  profile is selected; this is menu delivery, not a selected Sinhala LLM
  profile, and this LLM-only change must not alter it.
- The Sinhala profile names Azure explicitly and fails closed if Azure is not
  constructible because its SDK, `audioop`, or a whitespace-only
  `AZURE_SPEECH_KEY` is absent. It
  must never inherit the global factory's Azure-to-Google fallback; English
  retains that existing configured fallback behavior. An Azure stream that
  later cancels is also fatal to the selected Sinhala session: it must invoke
  the session-owned fatal callback once, not on normal shutdown and not merely
  log and leave the caller silent.
- Provider/model/tool selection and requested generation controls are
  session-owned. Lazy module-level SDK client singletons may remain shared, but
  no call may mutate process-wide provider/model/tool/thinking compatibility
  state that changes another concurrent call's request behavior.
- Every profile assignment receives a `copy.deepcopy()` of its provider-native
  tools, including English preservation. Equal values are required, but neither
  the outer list nor nested declarations may share identity with the original,
  another session, or future `get_tools*()` output.
- Sinhala Gemini TTS remains provider-exclusive. LLM failover to Claude must
  not imply a TTS fallback; all Sinhala text continues through Gemini TTS.
- Existing generation ownership, barge-in, filler, capture, transport, tool
  side-effect, history rollback, and delivered-sentence contracts remain in
  force.
- No prompt text, transcript, caller data, audio, response body, API key, or
  credential may appear in new diagnostics.

## Current source boundary

`KavyaSmartPBXSession._load_runtime_defaults()` currently resolves the global
`LLM_PROVIDER`, constructs `MediaStreamSession` before IVR selection, and
injects only the client for that global provider. Later,
`KavyaSmartPBXSession._activate_language()` changes `pipeline.lang`, the system
prompt, the non-English tool set, and the STT language. It does not change the
LLM provider, LLM model, client, or provider-native tool format. Consequently,
the live Press-2 path is Sinhala at the prompt and TTS boundaries but still
uses the globally selected Claude LLM.

The STT language is already correct. The Press-2 path passes `lang="si"` to
the STT factory, `STT_PRIMARY["si"]` is `si-LK`, and `AzureSTTStream.start()`
sets `speech_config.speech_recognition_language` to that value. Production
telemetry has independently shown `Azure STT stream started (lang=si,
primary=si-LK)`.

`MediaStreamSession._run_llm_gemini()` already provides the required execution
machinery: native streaming, provider-native tools, thought filtering,
sentence-level TTS scheduling, history conversion, empty-response retry,
side-effect-aware failover, and sticky per-session Gemini-to-Claude
degradation. The change must reuse this runner rather than create a Sinhala
copy.

## Approaches considered

### 1. Recommended: immutable per-language session profiles

Resolve a profile once at IVR activation and apply it to the existing
`MediaStreamSession` while the history is empty and STT is stopped. This gives
English and Sinhala independent STT/LLM/model/tool configuration while reusing
the common turn lifecycle, transport, tools, capture, and TTS code.

This is the smallest boundary that matches the product requirement and is safe
under concurrent English and Sinhala calls.

### 2. Change the global `LLM_PROVIDER` to Gemini

Rejected. It would move English to Gemini as well, discard the optimized and
reviewed English Claude path, and make rollback an all-language event.

### 3. Replace the pipeline with Gemini Live speech-to-speech

Rejected. It would combine audio understanding, reasoning, and speech output
inside a different session protocol. That would bypass or substantially alter
the explicit STT endpointing, booking tools, capture modes, recovery,
transcript ownership, and provider-level observability that Kavya depends on.

### 4. Use Gemini 3.5 Transcribe Live for production Sinhala immediately

Rejected for now because Sinhala is absent from Google's supported-language
table. Technical connectivity is not evidence of a supported or reliable
Sinhala production service.

## Profile resolution and call flow

1. The SmartPBX session starts in the existing pre-STT IVR state.
2. `1`, `2`, timeout, or invalid-selection fallback resolves a language code.
3. A small profile resolver returns only executable per-session provider,
   model, tool, STT-backend, and generation-control values. It returns values;
   it does not mutate globals. `lang` remains the existing execution key for
   both the STT factory and the TTS router: the profile must not duplicate
   descriptive TTS provider/model/voice fields or introduce a provider registry.
4. `_activate_language()` applies that profile exactly once before constructing
   and starting STT:
   - set `lang` and rebuild the language system prompt;
   - install the session's LLM provider, model, client, and matching tool
     declarations;
   - select the session's STT backend and language configuration;
   - retain the existing language-specific TTS routing;
   - start STT and speak the selected-language greeting.
5. All later turns dispatch through the existing provider switch in
   `_process_utterance_bound()` and therefore use `_run_llm_claude()` for
   English or `_run_llm_gemini()` for Sinhala.
6. Post-call processing continues to receive the selected language and only
   delivered assistant speech.

The resolver should be deliberately small. A frozen dataclass or another
immutable value object is acceptable if it makes the concurrency invariant
explicit; a new provider framework or registry is not.

## Sinhala LLM configuration

Use SmartPBX-Sinhala-specific settings so no new value can affect English or
Twilio:

- `SMARTPBX_SINHALA_LLM_PROVIDER`, default `gemini`, with `claude` as the
  operational rollback value.
- `SMARTPBX_SINHALA_GEMINI_LLM_MODEL`, default `gemini-3.7-flash`.
- `SMARTPBX_SINHALA_GEMINI_THINKING_LEVEL`, default `low` for a latency-critical
  voice path.
- `SMARTPBX_SINHALA_GEMINI_MAX_TOKENS`, default `600`, clamped to a reviewed
  range that can contain low-level thinking plus a complete structured tool
  call.
- Reuse `GEMINI_FAILOVER_TO_CLAUDE` and the existing sticky failover threshold
  unless implementation evidence shows that global reuse can change English.
  If so, add a Sinhala-specific failover enablement setting rather than
  broadening the global contract.

Gemini 3.7 Flash is stable, supports streaming, function calling, and
low/medium/high thinking. The current Gemini helper already selects the 3.x
`thinking_level` request shape and drops thought parts from spoken output.
The implementation must not add deprecated sampling parameters or migrate the
LLM runner to another API in the same change.

The Generate Content function-calling contract is a prerequisite, not a canary
detail. Gemini 3.7 returns an ID with each function call and requires the
matching ID and name on each `FunctionResponse`. The SmartPBX history adapter
must preserve that provider ID alongside the existing thought signature across
the tool-result follow-up; locally synthesized IDs are not sufficient.

The existing non-Claude direct-SmartPBX ceiling is at most 200 tokens. That
ceiling must not be assumed adequate for Gemini 3.7 tool turns: thinking tokens
count against output and a truncated function call cannot execute safely. The
Sinhala Gemini ceiling therefore needs its own resolver and production-shaped
truncation tests.

## Exact implementation boundaries

The module currently imports `audioop` unconditionally before the guarded
fallback. That first import must be guarded or removed so a Python/runtime
without `audioop` exposes `audioop is None`; option 2 then rejects before any
pipeline mutation, greeting, or STT start.

Option-2 activation validates the closed Sinhala provider set (`gemini` or
`claude`) and does all fallible client/tool preflight before changing the
pipeline. It catches `Exception` only around that bounded technical preflight;
`asyncio.CancelledError` and other `BaseException` values must propagate. An
invalid configured provider follows the same bounded unavailable/fallback path
and can never fall into OpenAI dispatch.

`AzureSTTStream` owns synchronized `_stop_requested` and `_fatal_notified`
flags. A genuine cancellation while running marks it non-running under the
lock, selects its session callback once, and invokes that callback outside the
lock. Normal `stop()` sets `_stop_requested` and suppresses the fatal callback.
The cancellation record contains only a bounded reason/outcome; it must never
log `evt.error_details` or provider error text.

The shared `_history_to_gemini()` converter needs an explicit, default-false
SmartPBX flag for provider function-call IDs. Direct SmartPBX Gemini uses that
flag to round-trip the real ID in both `function_call` and
`function_response`; ConversationRelay retains its present converted shape and
has a regression test proving no ID was added to its history.

`llm_round_outcome` is one provider-unified exact bounded schema, not separate
Claude and Gemini schemas. It emits exactly `provider`, `outcome`,
`stop_reason`, `output_tokens`, and `attempt`. Provider is `claude|gemini`;
outcome is `completed|max_tokens_truncated|true_empty|incomplete_tool_block|malformed_tool_json|stream_aborted`; stop reason is normalized to
`end_turn|max_tokens|tool_use|stop_sequence|refusal|unknown` (or `none` only
when absent); output tokens are clamped to `0..1000000` or `unknown`; attempt
is clamped to `1..9`. No content, tool data, raw provider string, exception, or
identifier beyond the established bounded event fields is emitted.

## Conversational Sinhala policy

Changing models alone does not prove natural conversation. The Sinhala prompt
must define voice-appropriate output without turning the model into a rigid
script:

- use contemporary conversational Sri Lankan Sinhala rather than formal
  written Sinhala;
- normally answer in one short sentence and ask at most one question;
- use a second sentence only when it carries necessary booking information;
- allow natural English code-switching for official room names, Hatton Hills,
  WhatsApp, and familiar hotel terms;
- never expose English-only internal recovery, keypad, validation, or tool
  language to the caller;
- avoid repeating the full room catalogue or booking summary unless required;
- preserve dates, prices, room names, guest counts, phone digits, and tool
  results exactly even when rephrasing conversationally.

Prompt examples must be reviewed by a fluent Sri Lankan Sinhala speaker. Model
output is evaluated for naturalness as well as correctness; a different model
name is not acceptance evidence.

## Sinhala STT strategy

### Production baseline

Keep Azure with the existing `si-LK` language setting for the first Gemini-LLM
canary. This changes one provider boundary at a time and gives an immediate
rollback point.

### Offline Chirp 2 challenger

Do not add a production STT selector for Chirp 2. Google Cloud Speech-to-Text
V2 documents Sinhala `si-LK` for `chirp_2` in `asia-southeast1`, including
automatic punctuation, model adaptation, and word-level confidence, but its
method-specific Chirp 2 documentation excludes Sinhala from
`StreamingRecognize`. Evaluate `Recognize` or `BatchRecognize` only against
controlled 8 kHz telephone recordings. Do not infer live streaming support
from the broader model/language table, and do not add `google_chirp2` to the
live session factory unless Google adds `si-LK` to the streaming list.

The English STT provider and its phrase-list/digit behavior stay unchanged.

### Evaluation-only Gemini Transcribe track

Gemini `gemini-3.5-transcribe-live` may be connected to a controlled offline
or explicit pilot harness, not the production Press-2 selector. The harness
must decode SmartPBX mu-law to raw mono 16-bit PCM at 16 kHz and may use a
narrow custom vocabulary for property and room names, but it must label the
result unsupported for Sinhala and must not make the production plan depend on
it.

## Failure handling

- A replay-safe Gemini failure before any tool side effect may use the existing
  history rollback and Claude replay path.
- Once a tool has started, no provider replay may duplicate it. Use the
  existing non-replayable recovery behavior.
- Repeated Gemini technical failures sticky-degrade only that Sinhala session
  to Claude and emit the existing bounded provider-failover/degraded events.
- A Gemini client-initialization failure during option-2 activation must be
  caught before STT starts. If a prepared Anthropic client is available, apply
  the transfer-free Claude Sinhala profile and continue. If neither LLM client
  is usable, emit one bounded fixed-field diagnostic, do not start STT, and
  terminate the session through the existing fatal-session path rather than
  leaving a selected but silent call.
- Claude fallback text remains Sinhala because the selected language prompt is
  retained; all resulting speech still uses Gemini Sinhala TTS.
- A Gemini TTS failure remains a TTS failure. It must not invoke OpenAI,
  ElevenLabs, Azure, or another speech provider.
- An STT provider failure must fail closed through the existing session fatal
  signal. Azure construction must reject missing SDK, `audioop`, and key before
  the welcome starts; `AzureSTTStream` must expose and call `on_fatal` from its
  cancellation callback. It must not silently change English or mix transcripts
  from two recognizers.
- The direct-SmartPBX non-capture predicate is widened only in the exact Sinhala
  Gemini deadline, exhausted-empty, and tool-executed-exception recovery
  branches. Capture behavior remains excluded, and production filler, transfer,
  and other English-only predicates remain unchanged.

## Verification design

### Provider isolation

- Press `1`, timeout, and invalid fallback construct the same English provider,
  model, tools, STT language, greeting, and TTS route as before.
- Press `2` constructs Gemini 3.7 Flash tools/model/client, Sinhala prompt,
  selected Sinhala STT, and Gemini TTS.
- Simultaneous English and Sinhala calls retain independent provider/model/tool
  state through normal turns, capture turns, tool rounds, barge-in, and
  teardown.
- The English IVR selection neither eagerly reads/validates an ElevenLabs voice
  secret nor changes the canonical `lang="en"` ElevenLabs `_speak` route; the
  Sinhala selection still routes `lang="si"` speech exclusively to Gemini TTS.
- TTS dispatch tests use a real `MediaStreamSession` with a
  `KavyaSmartPBXSession`/transport stub, not `RecordingPipeline` (whose
  `_speak` override cannot prove production routing). They separately prove
  option-1 ElevenLabs routing by wrapping the real ElevenLabs method with a
  fake response and nonblank test key (or by a separate canonical-profile test),
  option-2 Gemini routing, unchanged bilingual pre-selection menu delivery,
  and Gemini TTS retention after Gemini-to-Claude LLM fallback.

### LLM and tools

- Gemini produces streamed Sinhala text without thought leakage.
- Availability, rate, name, number, keypad, and booking tool rounds preserve
  arguments and side-effect rules.
- Empty, blocked, truncated, timed-out, malformed-tool, quota, server-error,
  and post-tool failure paths have the required retry/failover/recovery
  outcome.
- Sinhala uses the language-appropriate direct-SmartPBX recovery predicate in
  both the exhausted-empty and tool-executed-exception branches, so it receives
  the same bounded provider-safe recovery as English rather than a generic
  fallback path.
- Direct SmartPBX Sinhala Gemini turns use the same non-capture initial/stall
  deadline and atomic timeout recovery contract as direct SmartPBX Claude
  turns. Capture flows keep their existing timeout carve-out.
- A Gemini 3.7 tool round preserves each provider function-call ID into the
  matching function response; truncated or malformed tool rounds execute no
  tool and commit no partial assistant/tool history.
- No English recovery or keypad sentence reaches Sinhala TTS.

### STT benchmark

Use controlled, consented call audio covering conversational Sinhala,
Sinhala-English code-switching, room names, Sri Lankan names, dates, guest
counts, short confirmations, phone numbers, and repeated digits. Compare the
live Azure `si-LK` transcript with offline Chirp 2 results on:

- exact booking-slot accuracy;
- semantic utterance accuracy;
- name, room, date, and digit accuracy;
- interim-to-final and speech-end-to-final latency;
- duplicate, missing, or late-final events;
- false barge-ins and endpointing behavior.

Gemini Transcribe results may appear as an explicitly unsupported experimental
column only.

### End-to-end acceptance

- English behavior and latency show no material regression against the current
  stable baseline.
- A fluent reviewer accepts the Sinhala phrasing as conversational, concise,
  and appropriate for a Sri Lankan hotel call.
- Sinhala completes room inquiry, rate, availability, name, phone, and booking
  flows without English leakage or duplicated tools.
- Turn/session accounting reconciles, with no dropped transport frames,
  underruns, orphaned tasks, or stale audio after barge-in.
- First-media and tool-turn latency are reported by stage so LLM, STT, TTS, and
  transport costs remain distinguishable.

## Rollout and rollback

1. Land provider-isolation and Gemini-LLM behavior behind Sinhala-only settings.
2. Deploy with Sinhala STT still set to Azure.
3. Run controlled Press-1 English preservation calls and Press-2 Sinhala calls.
4. Accept or roll back the Sinhala LLM independently by switching
   `SMARTPBX_SINHALA_LLM_PROVIDER` to `claude` and recreating only
   `kavya-smartpbx`.
5. After the LLM/TTS path is accepted, benchmark Chirp 2 offline using
   controlled, consented recordings only.
6. Do not canary Chirp 2 on live calls unless Google's method-specific
   documentation adds Sinhala to `StreamingRecognize` and a new design is
   approved.
7. Keep immutable-image rollback available throughout the guarded Kavya
   probe/build/deploy process.

## Documentation and deployment-contract acceptance

Keep the existing runbook heading `## SmartPBX Sinhala menu and Gemini TTS`,
because the deployment test extracts it. Its protected `.env.smartpbx` template
contains exactly one active assignment for each of the four Sinhala LLM settings
and exactly one blank active `GEMINI_API_KEY=`; the parser ignores comments and
rejects duplicate, later, or nonblank key assignments. For activation, a
nonblank Gemini key means `bool(value.strip())`; whitespace-only is not a key.
The tests cover a blank value, a whitespace-only value, and a later nonblank
assignment without printing any value. The section preserves
menu/timeout/invalid-selection behavior, key-presence/no-secret-printing,
bounded diagnostics, two-language canary, and rollback.

Update the stale timing, token-budget, and Claude-only-retry prose in the
current runbook regions 264-268, 304-308, 345-346, and 518-522, together with
the diagnostics schema around 469-485. Preserve the startup pre-roll canary at
203-208. The 345-346 replacement distinguishes preserved English Gemini
budgeting from the Sinhala session-owned `600` ceiling. Deployment coverage
includes the dotenv helper tests (23-39), diagnostics tests (564-598),
protected-template tests (3141-3167), and section tests (3184-3213). Compose
rendering always uses `--profile smartpbx`.

## Explicit non-goals

- Changing the English LLM, STT, ElevenLabs voice, endpointing, fillers,
  handover, or capture policy.
- Changing SmartPBX audio format or transport framing.
- Replacing the explicit STT to LLM to TTS agent with speech-to-speech.
- Modifying Twilio or Flico.
- Sending live customer audio to an unsupported transcription model.
- Refactoring the existing provider runners or migrating Gemini APIs without a
  separate need and review.

## Official sources

- Google AI for Developers, [Gemini 3.7 Flash](https://ai.google.dev/gemini-api/docs/models/gemini-3.7-flash)
- Google AI for Developers, [Gemini 3.7 migration checklist](https://ai.google.dev/gemini-api/docs/generate-content/latest-model)
- Google AI for Developers, [Streaming text generation](https://ai.google.dev/gemini-api/docs/generate-content/text-generation#streaming-responses)
- Google AI for Developers, [Function calling](https://ai.google.dev/gemini-api/docs/function-calling)
- Google AI for Developers, [Gemini audio transcription](https://ai.google.dev/gemini-api/docs/transcribe)
- Google AI for Developers, [Live transcription](https://ai.google.dev/gemini-api/docs/live-api/live-transcribe)
- Google Cloud, [Speech-to-Text V2 supported languages](https://docs.cloud.google.com/speech-to-text/docs/speech-to-text-supported-languages)
- Google Cloud, [Chirp 2 method-specific language availability](https://docs.cloud.google.com/speech-to-text/docs/models/chirp-2#language-availability)
- Microsoft Learn, [Azure Speech language and voice support](https://learn.microsoft.com/en-us/azure/ai-services/speech-service/language-support)
