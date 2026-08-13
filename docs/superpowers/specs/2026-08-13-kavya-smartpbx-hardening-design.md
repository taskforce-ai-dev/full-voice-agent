# Kavya SmartPBX Hardening Design

## Status and decision

Approved design for branch `Rakesh` at `9273721c040b2ff8362fc9d5c1bf059ca7478d73`.
The immutable pre-change reference is
`kavya-smartpbx-pre-stt-latency-20260813` (`9273721`).  The change is a
SmartPBX-only hardening cycle; it does not deploy, change protected runtime
configuration, or alter Twilio behavior.

Production evidence is deliberately aggregate-only.  The current image is
`962bba8`, which predates the capture-dictation buffering commit on this branch.
The observed 10.3 s median guest-to-agent proxy therefore identifies a caller
experience problem, but cannot apportion it to STT, endpointing, KB, LLM, tools,
TTS, or the paced transport.  This design closes that measurement gap before
tuning timing-sensitive behavior.

## Goals

1. Correlate every SmartPBX turn with privacy-safe, monotonic, bounded stage
   timings and one terminal summary.
2. Forward a syntactically valid DTMF digit despite a non-fatal Dialog leg
   mismatch, while retaining mismatch diagnostics and rejecting malformed input.
3. Establish whether Google interim results are cumulative before applying an
   exact, source-supported de-duplication rule.
4. Make the effective English digit-class STT state observable and document the
   guarded later production change from `0` to a reviewed nonzero value.
5. Improve caller rhythm only where evidence supports it: a concise SmartPBX
   output budget and one bounded initial-silence filler.
6. Keep generic CI from intentionally invoking the rejected Kavya image deploy
   route; retain the probe, publisher, and guarded runbook deployment path.
7. Retain only privacy-safe operational logs for a bounded repository-owned
   period and document how to verify that contract.
8. Test and, if CI evidence supports it, reframe SmartPBX μ-law outbound audio
   from current 640-byte/80 ms TTS chunks to steady 160-byte/20 ms media frames.

## Non-goals and invariants

- Do not log transcript text, caller/call/stream identifiers, phone numbers,
  tool arguments or results, provider payloads, headers, secrets, exception
  messages, or wall-clock call traces.
- Do not alter capture buffering, delivered-ask capture arming, transfer
  announcements, `send_mark()` queue-drain semantics, transport backpressure,
  echo suppression, the SmartPBX four-call cap, or either Twilio ingress mode.
- Do not change keypad or transfer timeout values, reorder side-effecting tools,
  or assert that Dialog has an acoustic playback acknowledgement.  Transport
  timestamps remain a wire-delivery proxy.
- Keep `nginx-smartpbx.conf` `proxy_read_timeout`/`proxy_send_timeout` at 120 s.
  Treat a proxy close as a separate watch item and change it only with evidence
  that it caused an observed call close.
- Do not add a canary deployment implementation, dashboard change, production
  route/configuration change, or provider dashboard mutation in this cycle. The
  immutable baseline tag and a future controlled test DID are sufficient.
- Do not silently deduplicate interims based on fuzzy matching, normalization,
  token overlap, or a provider assumption.
- Do not change production `STT_DIGIT_CLASS_BOOST` in this cycle and do not put
  a production value or any secret in the repository.

## Design

### 1. Turn telemetry contract

Add a call-local telemetry object owned by `MediaStreamSession` only when
`_is_direct_smartpbx_english()` is true.  `start_turn()` generates a fresh,
locally random opaque `turn_id` (for example, 128 bits from `secrets` encoded
as URL-safe text), never derived from Dialog, a call, a caller, a socket, or
wall-clock time.  Every stage and `turn_summary` event needed to analyze
concurrent calls carries this `turn_id`.  A monotonically increasing `turn_seq`
may remain in memory for aggregate accounting, but is never the log correlation
key.  The recorder stores only monotonic timestamps in memory.

The telemetry object exposes `start_turn()`, `mark(stage)`, `mark_once(stage)`,
and `finish(outcome)`.  It converts elapsed monotonic time to integer
milliseconds with `min(max(value, 0), 600000)`.  Missing stages are represented
by omission, never by a fabricated zero.  A `finally` path emits exactly one
terminal `smartpbx_media event=turn_summary` for each started turn, including
LLM failure, tool failure, TTS failure, interruption, transfer-pending
cancellation, and normal completion.  Separately,
`KavyaSmartPBXSession._finish_once()` emits one `smartpbx_media
event=session_summary` after pipeline cleanup.  Its fixed fields are only
`session_trace_id`, `outcome`, `turns_started`, `turns_summarized`,
`duration_ms`, `frames_dropped_total`, and `barge_ins`.  The session creates
`session_trace_id` as a separate local random opaque value; it is never derived
from or joined to a Dialog ID.

The allowed summary fields are:

```
event=turn_summary turn_id=<opaque-random> outcome=<fixed-enum>
endpoint_source=<final|interim|capture|unknown>
endpoint_ms=<0..600000> kb_ms=<0..600000>
llm_first_token_ms=<0..600000> llm_complete_ms=<0..600000>
tool_ms=<0..600000> tts_first_chunk_ms=<0..600000>
first_media_sent_ms=<0..600000> queue_drained_ms=<0..600000>
barge_clear_ms=<0..600000> generated_chars=<0..20000>
delivered_sentences=<0..100> dropped_frames=<0..100000>
frames_160b=<0..100000> frames_partial=<0..100000> frames_other=<0..100000>
inter_send_gap_p95_ms=<0..600000> inter_send_gap_max_ms=<0..600000>
queue_underruns=<0..100000>
```

Each field is optional when its boundary did not occur.  The implementation
will use a fixed outcome enum and fixed endpoint-source enum rather than logging
exception text, provider response status, tool input, or free-form labels.
`first_media_sent_ms` means the first successful Dialog WebSocket media send;
`queue_drained_ms` means `send_mark()` returned after queue drain.  Neither is
labelled or treated as caller-heard audio.

Stage ownership is explicit:

| Boundary | Owner |
| --- | --- |
| provider callback and endpoint timer choice | `MediaStreamSession._on_stt_result`, `_on_stt_interim`, `_flush_transcript` |
| KB start/finish and LLM first/completion | the three SmartPBX LLM runner paths in `server.py` |
| tool batch/execution/result | the existing SmartPBX tool-round logging path |
| TTS request/first non-empty generated audio | the existing SmartPBX TTS helpers/call site |
| first wire send, drain, clear, drops | `SmartPBXMediaTransport` generation state, mapped to `turn_id` |
| terminal outcome | the turn runner's `finally`; `KavyaSmartPBXSession._finish_once()` aggregates session outcome |

Before a SmartPBX TTS generation is allowed to enqueue audio, the session/pipeline
binds the live transport generation to the current opaque `turn_id` through the
narrow interface `bind_turn(generation: int, turn_id: str)`.  Transport owns an
internal `generation -> turn_id` map and emits callbacks of the form
`on_transport_event(turn_id, generation, stage, monotonic_ns, dropped_frames)`.
It receives neither `CallContext` nor transcript text, and it cannot synthesize
an ID.  `clear_audio()`, close, or a generation change invalidates the old map
entry and its pending timing exactly as it invalidates stale audio.  The session
creates `session_trace_id` in `_start_once()` and emits its aggregate
`session_summary` from `_finish_once()` after pipeline/STT/transport cleanup.

### 2. Media cadence evidence and 20 ms reframing

The pinned Client Connect reference
`ChanakaDev/ai-provider-example-websocket@d778fd5d9de46c2fe470f4e8e71167dd1aaa414f`
splits `g711_ulaw@8000` output into 160-byte (20 ms) media frames.  Kavya's
ElevenLabs/Azure readers currently request 640-byte chunks and
`SmartPBXMediaTransport` sends each queued bytes object as one media frame; at
8 kHz μ-law this is 80 ms.  The wire JSON shape is already compatible and
Kavya's realtime pacing means this is not proof of loss or playback failure.
It is, however, the strongest evidence-backed application/provider cadence
difference and merits a controlled A/B.

Implement the cadence change inside `SmartPBXMediaTransport`, before its
existing queue/backpressure policy: `send_audio()` accepts arbitrary μ-law
chunks, appends only current-generation bytes to a per-generation residual, and
enqueues each complete 160-byte slice in original byte order.  The residual is
strictly bounded below 160 bytes.  `send_mark()` flushes the live generation's
remaining 1--159 bytes as one final partial frame before its existing
`queue.join()` delivery barrier; no zero-length frame and no padding is sent.
`clear_audio()`, close, and generation mismatch discard that generation's
residual exactly as they discard stale queued audio.  This preserves generation
fencing, contiguous-prefix behavior, existing 200 ms backpressure, and
drop-newest accounting: an overflow may still remove only the newest queued
full/partial frame, never reorder or replace earlier bytes.

Add safe transport cadence accounting keyed by `turn_id`.  Frame sizes use only
the fixed buckets `160B`, `partial_1_159B`, and `other`; no payload or exact
unbounded size value is logged.  Keep a bounded per-turn inter-send-gap sample
(at most 256 monotonic gaps) and report p95 and maximum only in the terminal
summary, clamped as all other durations.  Increment `queue_underruns` only when
the sender reaches its next scheduled send instant while the same generation's
utterance remains open but has no queued frame; do not count normal utterance
completion, stale generation clearing, or close as an under-run.  These values
describe application-to-WebSocket cadence, not Client Connect acceptance or
acoustic playback.

### 3. DTMF mismatch is diagnostic, not digit loss

`SmartPBXGateway` already parses `DtmfEvent` before context validation.  Move
the existing `feed_dtmf(event.digit)` block out of the validation `else` so it
runs after either (a) normal validation or (b) an observed
`context_mismatch`.  Keep the normal diagnostic on a successful match and the
existing `CONTEXT_MISMATCH/OBSERVED` diagnostic on the mismatch.  Other
`ProtocolViolation` values still raise, and the protocol parser continues to
reject malformed event shapes and invalid digits before this branch.

### 4. Interim semantics evidence gate

First obtain a source-backed Google streaming-result shape and add a fixture for
both segment-only and cumulative-after-final cases.  The telemetry records only
`interim_shape=segment|exact_cumulative|unknown`, `committed_chars`, and
`interim_chars`; it never logs either string.

Only if the supported Google shape proves that an interim can repeat the exact
already committed prefix may `_set_transcript_interim` remove one exact leading
`committed + separator` sequence.  It must preserve spelling, punctuation,
spacing, and case; it may not use `strip`, case folding, fuzzy similarity, or
token matching to create a match.  If that provider semantics is not supported,
the implementation ships the fixture and telemetry only, leaving concatenation
unchanged.

### 5. Digit-class state and deployment gate

Retain the existing clamped `STT_DIGIT_CLASS_BOOST` parsing and English-only
`$OOV_CLASS_DIGIT_SEQUENCE` insertion.  At SmartPBX STT initialization log one
safe configuration-state event with `enabled=true|false` and the resolved,
clamped numeric boost; it contains no environment dump or credentials.  Add the
variable to the SmartPBX Compose allowlist exactly as
`STT_DIGIT_CLASS_BOOST: "${STT_DIGIT_CLASS_BOOST:-}"`.  A missing or blank
protected environment value therefore reaches the existing parser as blank and
falls back to its source default (`4.0`); no production value is committed.
Document that behavior in `.env.example` and the protected SmartPBX runbook
template.

The runbook must require, in a later separately reviewed deployment: choose a
nonzero value using the test/telemetry result, place it only in the protected
environment file, render `docker compose --env-file .env.smartpbx config` to
confirm the allowlist, recreate the pinned SmartPBX container, and verify the
safe enabled-state event.  It must not print the environment file or its
secrets.  This plan deliberately does not select the value.

### 6. Caller rhythm

Use a SmartPBX-specific output profile: `SMARTPBX_MAX_TOKENS` defaults to `120`
and is clamped to `[40, 200]`; non-SmartPBX paths retain the existing
`MAX_TOKENS=300`.  The SmartPBX system rule says to answer first in one or two
short sentences, then ask at most one necessary next question.  Booking policy,
tools, transfer instructions, capture wording, and all Twilio prompts are
unchanged.

For only the first SmartPBX LLM round, schedule one fixed neutral filler after
`SMARTPBX_INITIAL_FILLER_DELAY_SECONDS` (default `2.5`, clamp `[0.5, 5.0]`).
The model starts immediately and is never gated by filler send/drain.  Cancel
the timer on the first content delta **or** tool delta, barge-in, transfer
pending, session finish, or generation change.  Before that first delta, the
code cannot know whether the eventual result is capture, keypad, transfer, or
another tool: if the timer wins, the neutral filler may be spoken.  It must not
be retroactively suppressed on learning a later tool.  If a tool delta wins,
cancel the neutral filler and retain existing capture/keypad/transfer-specific
suppression; if the neutral filler already spoke, suppress the later specialized
tool filler to avoid double speech.  When a tool delta arrives while the neutral
filler is still speaking, cancel/clear that filler before the existing tool
execution sequence continues.  Later tool rounds and any turn already speaking
never schedule this initial filler.  Tools retain their existing ordering; in
particular `create_booking`, transfer, and every other side-effecting operation
never begins concurrently with filler delivery.

### 7. CI, retention, and operations

Change `.github/workflows/deploy-on-push.yml` and the matching
`.github/workflows/pr-deploy-impact.yml` reporting route so a changed `Kavya/**`
path is reported as deliberately excluded from generic `deploy.yml` image mode.
It must not dispatch `agent=kavya, mode=image`.  Keep
`build-kavya-image.yml`, `probe-kavya-image.yml`, the OCI revision gate, and the
runbook's reviewed image deployment route unchanged except for compatible
documentation/verification additions.

The SmartPBX Compose service already uses Docker `json-file` rotation with
`max-size: 10m` and `max-file: 3` (30 MB maximum).  Retain those settings and
document an operational verification that checks the rendered service logging
configuration plus the safe event schema.  Do not add raw-log collection,
longer retention, a dashboard payload, or a Dialog/caller-derived call
correlation identifier; the locally random `turn_id`/`session_trace_id` contract
above is the sole permitted correlation mechanism.

The runbook adds a controlled synthetic-tone A/B only: compare the immutable
baseline's prior 640-byte cadence with the reviewed 160-byte framing against a
new non-production test DID and Client Connect echo provider.  It uses a
synthetic tone, no caller recording, no dashboard/production route change, and
no canary deployment implementation.  The result package must contain provider
`callId`/CDR or case reference, UTC (`Z`) handshake/start/first-media/playback/
close timestamps, negotiated codec/rate, accepted/queued/played evidence,
frame-size bucket counts, p95/max gaps, and close reason.  It must not contain
caller/callee numbers, payloads, transcripts, credentials, or raw exception
bodies.  Provider `callId` is a controlled external-support artifact, never a
Kavya log field or a derivative for `turn_id`/`session_trace_id`.

Keep Nginx's 120 s proxy timeouts unchanged.  During the controlled test collect
proxy and provider close times; investigate that watch item separately only if
they prove a proxy timeout closed the connection.

## Verification and rollout

Tests are written first and exercised through targeted GitHub CI commits pushed
only to the existing `Rakesh` branch when local pytest is unsafe or hangs.  This
cycle explicitly authorizes those test-only and implementation pushes solely to
collect red/green CI evidence; it does not authorize another branch, merge,
deployment, runtime configuration mutation, or a production action.  Each CI
commit is red for its new regression, then green after the minimal
implementation.  Local validation
is limited to `python3 -m py_compile` for touched Python modules, workflow/static
inspection, `git diff --check`, and documentation checks; none of these is
behavioural proof.

The release candidate must demonstrate: terminal summaries for normal/tool/
interrupted/failed turns and one safe session summary; no forbidden log fields;
valid mismatched DTMF reaches
the collector; malformed DTMF remains rejected; the provider-semantics gate;
SmartPBX-only output/filler behavior; generic CI exclusion; and preserved probe
and publisher contracts.  Perform one consolidated final review after all
tasks—not separate per-task reviews—against these invariants and the immutable
pre-change tag.  Promotion remains a separate approved runbook operation.
