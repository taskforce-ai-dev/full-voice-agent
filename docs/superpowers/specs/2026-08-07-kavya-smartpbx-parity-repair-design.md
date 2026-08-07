# Design: Kavya SmartPBX parity repair

## Objective

Make the English Dialog SmartPBX call path sound and behave like production Kavya
without changing the deployed Twilio path.  The immediate outcome is a stable,
privacy-safe Dialog call that uses Kavya's established English voice identity and
voice-model semantics.  It must preserve Dialog's `g711_ulaw` / 8000 Hz media
contract and Kavya's production conversation, booking, retrieval, handover, and
post-call behavior.

This is a parity repair, not a migration away from Twilio.  The Dialog dashboard
may temporarily route the sole DID to Kavya for a supervised verification call.

## Evidence and root cause

The original English route is Twilio ConversationRelay and configures its
established, hardcoded ElevenLabs voice with the `flash_v2_5` suffix.  The direct
SmartPBX route instead reads the general ElevenLabs environment voice and uses
`eleven_multilingual_v2`.  A comparison of non-secret configuration hashes proved
that the environment was copied correctly; the identities still differ because
the original path is intentionally hardcoded.  This explains why the corrected
Dialog call said “Kavya” but sounded like a different agent.  Do not put the
actual voice identifier in code comments, tests, documentation, logs, or Git.

The active parser also currently treats `hangup.accountId` and `hangup.reason` as
required.  The supplied Dialog v06 extraction shows that a hangup has no
`accountId` and its reason is optional.  That mismatch plausibly explains the
observed post-call `invalid_message` outcome; it is not proof that every Dialog
disconnect has the same cause.

## Authoritative anchors

### Repository anchors

- `Kavya/server.py`: `LANGUAGE_CONFIGS["en"]` is the original ConversationRelay
  English voice source; `MediaStreamSession`, `_make_stt`, `_speak`, re-prompt,
  filler, and barge-in behavior are the production-active English behavior to
  reuse.
- `Kavya/smartpbx_session.py`: adapters the Dialog call into the existing English
  media pipeline with privacy-safe STT and existing tools/state.
- `Kavya/smartpbx_protocol.py`: strict event admission currently requires the
  fields that need v06 correction and already protects the media boundary.
- `Kavya/smartpbx_gateway.py` and `Kavya/smartpbx_transport.py`: Dialog WSS
  lifecycle and outbound `g711_ulaw` / 8000 media transport.
- `Kavya/smartpbx_mcp.py`, `Kavya/smartpbx_handover.py`, and `Kavya/tools.py`:
  fail-closed call-control and the transfer state machine.
- `Kavya/tests/test_smartpbx_protocol.py`, `Kavya/tests/test_smartpbx_server.py`,
  `Kavya/tests/test_smartpbx_mcp.py`, and
  `Kavya/tests/test_smartpbx_provider_handover.py`: regression boundaries.
- `Kavya/SMARTPBX_RUNBOOK.md`: isolated deployment and rollback procedure.

### External source anchors

- ElevenLabs Text-to-Speech API reference: <https://elevenlabs.io/docs/api-reference/text-to-speech/convert>
- ElevenLabs output-format reference: <https://elevenlabs.io/docs/developers/voices/output-format>
- ElevenLabs model guide: <https://elevenlabs.io/docs/developers/models>
- Dialog SmartPBX v06 supplied extraction: reviewed protocol anchors for
  `start`, `media`, `dtmf`, and `hangup`; retain it only in the approved
  vendor-material location and do not copy account, call, phone, or credential
  values into this repository.

## Design

### 1. Canonical English TTS profile

Introduce a single, internal canonical English TTS profile that represents
Kavya's original English voice identity and model semantics.  Both the Twilio
ConversationRelay renderer and the direct Dialog renderer must select that
profile; neither path may silently fall back to the general multilingual voice.

The direct ElevenLabs request must use the official `eleven_flash_v2_5` model
name and `ulaw_8000` output.  The Dialog WebSocket transport remains
`g711_ulaw` at 8000 Hz; no resampling or encoding relaxation is allowed.  The
profile must be testable without exposing the voice identifier: tests assert a
redacted profile key, the selected model, output format, and selection path.

Retain the existing non-English model/voice routing code.  It is not evidence
that Dialog supports those routes.

### 2. Dialog v06 protocol conformance

Keep the parser bounded and closed-world, but change its schema to match the
v06 extraction:

- `start` retains required call/account/caller/callee identifiers and accepts
  only `g711_ulaw` with sample rate 8000.
- `hangup` requires only the documented identifiers; it does not require
  `accountId`, and `reason` is optional.  Context validation must therefore use
  only the identifiers documented on hangup.
- DTMF accepts `0-9`, `*`, `#`, and `A-D`, with the documented optional bounded
  duration.  Identifier handling must use the documented field names and keep
  all existing length/type limits.
- Unknown, malformed, oversized, mismatched, or non-ulaw events remain safely
  rejected.  No raw event name, raw payload, call ID, account ID, phone number,
  transcript, audio, or MCP value may appear in an error, metric label, or log.

Add privacy-safe diagnostics with a fixed discriminator and lifecycle stage,
such as parser stage plus a finite failure class.  These diagnostics must be
sufficient to distinguish v06 schema admission, call-context validation,
session start, audio ingestion, TTS, and terminal cleanup, while containing no
raw protocol field values or personally identifying information.

### 3. English behavior parity

The SmartPBX session is an adapter, not a second agent.  It must use the same
English persona/system prompt, LLM/tool definitions, RAG/knowledge retrieval,
booking services, call-scoped state, welcome behavior, post-call processing,
and error policy as the production-active English path.

Align these observable behaviors:

- Re-prompt scheduling begins after a completion signal that is meaningful on
  the direct transport; cancellation, maximum count, and transfer-pending
  suppression match the established English behavior.
- Claude/tool fillers use the same intent-specific wording and ordering as
  English production.  Tool execution and failed-tool recovery must not leave a
  caller in silent dead air.
- Use the existing English STT factory/configuration where the Dialog audio
  contract permits it.  Document and test any provider-side feature that cannot
  be reproduced by direct media (for example, ConversationRelay-managed
  recognition settings).
- Preserve caller barge-in: cancel queued direct audio, invalidate the active
  speech generation, and return to listening.  Document and test the transport
  limitation that already-buffered carrier audio cannot be recalled
  deterministically; do not claim byte-perfect interruption parity.
- Preserve SmartPBX privacy hardening, including bounded media/message sizes,
  no sensitive values in logs, scoped call context, and existing dashboard/post
  call privacy controls.

### 4. MCP and transfer lifecycle

MCP call control remains disabled until the voice and core call have passed the
stable-call gate below.  No credentialed MCP experiment occurs during a live
customer call.

After that gate, run one standalone, non-call probe: establish MCP, issue only
`initialize` and `list_tools`, then close it.  Send exactly one account header:
first lowercase `account_id`.  Only after a deterministic HTTP 4xx that the
probe records without sensitive content may a second standalone probe use
`X-Account-ID`.  Never send both headers, never use `call_tool` in either probe,
and never infer success from a timeout, TLS failure, 5xx, malformed response, or
connection loss.

Transfer activation is a separate gate.  It requires a configured destination
from the operator-controlled allowlist and a supervised drill using only that
destination.  The current state machine correctly distinguishes an immediate
failure from provider acknowledgement, but acknowledgement is not proof that a
carrier transfer completed.  Close the post-ack transfer-outcome gap only with
a Dialog carrier outcome contract or an explicit failsafe design and an
observed drill; until then, do not report a handover as completed merely because
MCP acknowledged it.

### 5. Non-English boundary

Current production IVR for this cutover is English-only.  Multilingual Dialog
routing is a separate, explicitly gated requirement that starts only after
Dialog provides a documented language-selection mechanism and it is designed,
implemented, and verified.  Do not delete retained multilingual code as part of
this repair.

## Delivery plan and TDD gates

1. **Protocol red** — add failing unit tests for optional `hangup.reason`, absent
   `hangup.accountId`, `A-D` DTMF, documented context matching, and no raw values
   in diagnostics.  Add tests that non-ulaw media remains rejected.
2. **Voice red** — add failing adapter/request tests that both English paths
   select the canonical redacted profile and that direct TTS sends
   `eleven_flash_v2_5` plus `ulaw_8000`; add a regression test preventing the
   multilingual default on the English Dialog path.
3. **Behavior red** — add focused session tests for English prompt/tool/RAG/
   booking/state reuse, direct completion re-prompt timing, filler delivery,
   STT wiring, transfer-pending suppression, and barge-in cancellation.
4. **Handover red** — add tests for MCP disabled-by-default, standalone header
   probe sequencing, one-header-only behavior, 4xx-only fallback, no `call_tool`,
   allowlist enforcement, immediate failure, acknowledgement, and the explicit
   post-ack unknown outcome.
5. **Green/refactor** — implement the smallest changes that make every new test
   pass, then consolidate duplicated English selection behavior behind the
   canonical profile without changing non-English or Flico behavior.
6. **Review/deploy** — run targeted tests and the relevant CI suite; obtain an
   independent review of protocol, voice selection, privacy, and handover state;
   deploy only the isolated `kavya-smartpbx` profile using the runbook.

## Validation and release gates

The work is not complete until all of the following have evidence:

1. Targeted unit/integration tests pass and include each red-green requirement
   above; existing Kavya SmartPBX protocol, server, deployment, MCP, transport,
   handover, privacy, and post-call tests remain green.
2. CI, code review, and image provenance gates pass for the reviewed commit.
3. A protected configuration validation proves the direct profile uses the
   canonical English voice selection, `eleven_flash_v2_5`, `ulaw_8000`, and
   Dialog `g711_ulaw`/8000 without disclosing secrets or the voice identifier.
4. A supervised live Dialog call reaches Kavya, has intelligible two-way audio,
   uses the expected Kavya voice, supports a normal question and a booking/RAG
   turn, permits interruption within the stated transport limitation, and ends
   without a protocol-admission error.
5. The Dialog dashboard route is restored or deliberately retained only after
   the call evidence is recorded.  Rollback remains the isolated profile
   rollback in `Kavya/SMARTPBX_RUNBOOK.md`; do not disturb Twilio.
6. Only after gate 4 does the standalone MCP header probe run.  Transfer stays
   disabled unless its independent carrier-contract and supervised-drill gate
   passes.

## Boundaries

Always:

- Keep Flico's container, configuration, and running path intact.
- Use TDD red-green evidence for behavior changes and review before deployment.
- Keep secrets, MCP keys, voice IDs, call identifiers, and customer data out of
  Git, diagnostics, dashboard events, and test fixtures.

Ask first:

- Secret rotation, DID routing beyond the temporary sole-DID verification,
  Dialog credential changes, carrier contract decisions, and any non-English
  Dialog language selection.

Never:

- Remove Twilio, enable MCP transfer before its gates, send both account headers,
  invoke `call_tool` during a header probe, or weaken the g711 ulaw admission
  contract.

## Out of scope

- Flico container/configuration changes.
- Permanent Dialog dashboard routing policy.
- Twilio removal or refactoring.
- Secret rotation unless specifically requested.
- Unapproved multilingual Dialog routing.

## Completion criteria

Completion is proven only when every validation gate is satisfied with current
test, CI, review, deployment, and supervised-call evidence.  A successful build,
an MCP acknowledgement, or a call that merely reaches a greeting is insufficient.
