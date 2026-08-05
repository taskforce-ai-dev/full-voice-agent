# Flico Direct SmartPBX WSS Integration Design

**Date:** 2026-08-04
**Status:** Approved for implementation
**Owner:** Taskforce AI

## Objective

Connect Dialog Client Connect SmartPBX directly to Flico over a secure WebSocket so inbound Dialog calls use Flico's existing STT -> knowledge base -> LLM -> TTS pipeline. This path must not traverse Asterisk, Twilio, or an additional paid host.

## Authoritative Inputs

- Client-provided `SmartPBX AI Provider - Version 06.pdf` in `/home/dev/dialog-technical-clarifications/`.
- Client-provided queue, CDR, campaign, callflow, and Embedded UCP PDFs in the same directory.
- Dialog's public `ChanakaDev/ai-provider-example-websocket` sample, inspected at commit checked out locally on 2026-08-04.
- Existing Flico interfaces in `Flico Agent/media_transport.py` and `Flico Agent/server.py`.

The PDFs are authoritative for documented fields. The public sample is compatibility evidence only. In particular, its `connected` and `stop` events are not in the PDF and must be accepted defensively without becoming required protocol events.

## Scope

### Included

- One additive FastAPI WebSocket endpoint: `/ws/v1/smartpbx/media`.
- SmartPBX `g711_ulaw` 8 kHz media input and output.
- Strict parsing and lifecycle enforcement for `start`, `media`, `dtmf`, and `hangup`.
- Defensive handling for the public sample's `connected` and `stop` events.
- Reuse of `MediaStreamSession` through a SmartPBX implementation of `MediaTransport`.
- Four-call admission control, matching the purchased simultaneous-call capacity.
- A small outbound Dialog MCP client limited to `transfer_call` and `hangup_call`.
- Code-enforced transfer destination allowlisting.
- Structured, privacy-safe lifecycle telemetry and a SmartPBX status endpoint.
- A separate `flico-smartpbx` container on the existing VPS, loopback-only behind Nginx.
- Operator configuration, deployment, rollback, and dashboard onboarding instructions.

### Excluded

- Any Asterisk routing or configuration change.
- Replacing or modifying existing Twilio ConversationRelay or Media Streams routes.
- Dialog campaigns, CDR ingestion, recordings ingestion, Embedded UCP, or beta MCP tools.
- Automatic DNS, production deployment, or SmartPBX dashboard changes. Those are operator actions after code review and a valid rotated credential is available.
- New paid infrastructure.

## Architecture

```text
Dialog DID / SmartPBX callflow
        |
        | WSS, g711_ulaw/8000, authenticated custom header
        v
Nginx: smartpbx-flico.taskforceai.tech
        |
        | http://127.0.0.1:8005/ws/v1/smartpbx/media
        v
flico-smartpbx container
        |
        +-- SmartPBX protocol/session boundary
        +-- SmartPBXMediaTransport
        +-- existing MediaStreamSession
        +-- existing STT / KB / LLM / TTS code and data
        |
        +-- outbound HTTPS MCP -> dialog.cybergate.lk:9443
                transfer_call / hangup_call only
```

The existing `flico-voice-agent` container remains responsible for the current Twilio and optional Asterisk paths. The new container uses the same image and knowledge data but starts with SmartPBX enabled and listens on a distinct loopback port. This isolates deployment, logs, health, and rollback while staying on one VPS.

## WebSocket Contract

### Authentication

SmartPBX must send a generated, high-entropy shared token in the custom header `X-Flico-SmartPBX-Token`. The service compares it using a constant-time comparison before accepting media. The Dialog API key is never used for inbound WebSocket authentication.

The endpoint fails closed when `SMARTPBX_WS_TOKEN` is absent. The token is stored only in the VPS environment and SmartPBX dashboard. It is never included in source, examples, logs, close reasons, health output, or metrics.

Dialog egress IP allowlisting is an additional Nginx control when Dialog supplies the production source IPs. It is not a substitute for the token because the source list has not yet been provided.

### Negotiated media

The SmartPBX dashboard must be configured for:

- encoding: `g711_ulaw`
- sample rate: `8000`

This is the existing Flico telephony media format. PCM16 24 kHz and Opus 48 kHz are rejected for this endpoint rather than transcoded or silently misinterpreted.

### Inbound events

The first state-changing event must be exactly one `start` event. Its nested `start` object must provide non-empty:

- `callId`
- `otherLegCallId`
- `callerIdNumber`
- `calleeIdNumber`
- `accountId`
- `mediaFormat.encoding`
- `mediaFormat.sampleRate`

After `start`, the endpoint accepts:

- `media` with a bounded base64 `media.payload`.
- `dtmf` with a single permitted DTMF digit and optional bounded duration.
- `hangup` with call identifiers and a bounded reason.
- `stop` as an undocumented compatibility terminal event.
- `connected` as an undocumented compatibility no-op before `start`.

Unknown events are counted and ignored only when their total JSON size is within the boundary limit. Malformed JSON, duplicate `start`, media before `start`, identifier mismatch, unsupported media format, invalid base64, or oversized messages terminate the session with a generic WebSocket policy/protocol close reason.

### Outbound media

Flico sends only the documented envelope:

```json
{
  "event": "media",
  "callId": "<start.callId>",
  "accountId": "<start.accountId>",
  "media": {"payload": "<base64 g711_ulaw audio>"}
}
```

The transport serializes writes under one async lock. SmartPBX does not document Twilio-style `mark` or `clear` messages, so:

- `send_mark()` is a local playback-completion signal and does not emit an undocumented wire event.
- `clear_audio()` advances a generation counter and drops queued stale outbound audio. It does not emit an undocumented wire event.

Outbound audio uses a bounded queue and one sender task. The queue must never grow without limit; stale audio is dropped on barge-in and all queued audio is discarded on terminal events or socket closure.

## Session Lifecycle

1. Validate endpoint configuration and the inbound token.
2. Reserve one of four capacity slots before accepting the socket.
3. Accept the WebSocket and wait for `start` within a short timeout.
4. Validate account identity, identifiers, and exact media format.
5. Create `SmartPBXMediaTransport` and start an existing `MediaStreamSession` with caller/call metadata.
6. Feed decoded u-law frames to the existing STT path.
7. Send Flico TTS frames through the SmartPBX transport.
8. Treat `hangup`, `stop`, WebSocket disconnect, timeout, protocol error, or server error as terminal.
9. Stop the media session, cancel sender/timers, release capacity exactly once, and schedule existing post-call processing where appropriate.

Every resource owner must support idempotent cleanup. A terminal event and a near-simultaneous WebSocket disconnect must not double-stop STT, double-release capacity, or send after close.

## Language

The initial production configuration uses the existing English Flico persona because the current paying-customer phone path is English-only. DTMF events are parsed and observed but do not create a new language menu in this scope. Tamil/Sinhala routing can be added later only after Dialog confirms the desired SmartPBX callflow and native-language acceptance testing is scheduled.

## MCP Call Control

### Transport and authentication

- Endpoint: `https://dialog.cybergate.lk:9443/ucp/v2/mcp`.
- Transport: MCP HTTP Streamable.
- Credential: rotated Dialog API key from environment only.
- Context uses the WebSocket `start.otherLegCallId` as `call_id`.
- The supplied PDF conflicts between `account_id` and `X-Account-ID`. Configuration must explicitly select one confirmed spelling through `SMARTPBX_MCP_ACCOUNT_HEADER`; there is no default and the client must never guess by sending both.

The implementation must fail closed if the configured account does not match `start.accountId`. MCP response bodies are treated as untrusted and size-limited. API keys, authorization headers, raw bodies, caller numbers, and full call IDs are never logged.

### Allowed tools

Only stable tools are exposed to application code:

- `transfer_call(destination)`
- `hangup_call()`

No beta/global tool is implemented.

`transfer_call` accepts a logical destination key, not an arbitrary URI from an LLM. Configuration maps that key to an exact URI from `SMARTPBX_TRANSFER_DESTINATIONS_JSON`. The caller/LLM cannot provide or modify the final URI. This prevents premium-number and arbitrary-SIP transfer abuse.

The MCP tool argument is exactly `destination_number`. The initial fallback destination is configured by the operator after Dialog confirms the live queue or extension URI. An unavailable or unconfigured destination or account-header mode leaves transfer disabled rather than guessing from the PDF conflicts.

## Failure Policy

- Invalid/missing authentication: reject; SmartPBX callflow must route to the live-agent fallback.
- Capacity exhausted: close as temporarily unavailable; SmartPBX must route to the live-agent fallback.
- Missing/invalid `start`: close without starting the AI pipeline.
- STT, LLM, or TTS failure after start: attempt one allowlisted live-agent transfer if configured; otherwise terminate so the carrier fallback can act.
- MCP transfer failure: one bounded retry for a retryable transport/server error, then terminate/fallback. Never retry validation/auth failures.
- Dialog REST/CDR API failure: does not affect active media.
- Socket loss or terminal event: immediate idempotent local cleanup.
- Process/container/VPS failure: SmartPBX-side callflow must be configured to send the caller to the live queue.

The application cannot prove carrier fallback behavior. Production readiness therefore requires an observed test where the WSS endpoint is deliberately unavailable and the caller reaches a live-agent queue.

## Threat Model and Controls

### Trust boundaries

- Public WSS client -> Nginx -> FastAPI.
- Untrusted SmartPBX JSON/base64 -> session/media pipeline.
- LLM decision -> transfer control.
- Flico -> Dialog MCP over HTTPS.
- Runtime environment -> secrets/configuration.

### Primary abuse cases

- Spoofed WebSocket client sends fabricated audio/call IDs.
- Oversized JSON/base64 or connection floods consume memory/call slots.
- Replayed/mismatched events cross-wire two calls.
- Prompt injection coerces a transfer to an attacker-controlled number.
- Logs or status endpoints disclose credentials or caller PII.
- Hanging external calls/tasks leak call capacity.

### Required controls

- Constant-time token validation, optional source IP allowlist, TLS only.
- Four-session cap, handshake/start timeout, idle timeout, message/frame size limits, bounded queues.
- Strict event order and identifier/account consistency.
- Code-level transfer destination allowlist and active-call binding.
- Short outbound timeouts, bounded retry, generic client close reasons.
- Structured allowlisted telemetry fields only; hashed/truncated correlation IDs and no media payloads/transcripts.
- Secrets supplied only through a gitignored environment file and rotated after exposure.

## Observability

The on-call engineer must be able to answer:

1. How many SmartPBX calls are active, admitted, rejected, or terminated by failure class?
2. Did a specific call reach `start`, receive media, produce outbound media, and clean up?
3. Are transfers succeeding, failing, or timing out, and to which logical allowlisted destination?
4. Are sessions leaking capacity or queues becoming saturated?

The service emits stable structured lifecycle events with a generated session correlation ID and redacted call fingerprint. It exposes `GET /smartpbx/status` with no secrets or PII, including enabled/readiness state, active/max sessions, aggregate counters, and protocol version. Metrics labels must come only from bounded enums such as event type, outcome, or failure class.

## Deployment and Rollback

- Add a `flico-smartpbx` Compose service using the same immutable Flico image, a distinct container name, loopback port `127.0.0.1:8005:8000`, and no Asterisk environment variables or network dependency.
- Mount/reuse only the existing Flico code and knowledge data required by the image's established deployment mode.
- Add a dedicated Nginx example for `smartpbx-flico.taskforceai.tech` that proxies only the WSS path and health/status endpoints, enforces TLS, has conservative connection limits, and does not log custom auth headers.
- The WSS feature is disabled by default. Existing Flico/Twilio behavior remains unchanged when `ENABLE_SMARTPBX_WSS=false`.
- Rollback is removal/disablement of the SmartPBX AI Provider in the Dialog dashboard followed by stopping the separate container. The existing Flico and Asterisk services do not change.

No code from this branch is deployed by the implementation task. Merge to `main` is a production deployment and requires the repository PR/review process plus an operator-approved launch window.

## Acceptance Criteria

- Existing Flico tests remain green.
- Contract tests prove all documented event shapes and failure cases.
- A local WebSocket integration test proves SmartPBX `start` + inbound media -> existing session adapter -> outbound documented media envelope -> cleanup.
- Tests prove a fifth concurrent call is rejected and a released slot can be reused.
- Tests prove duplicate start, media-before-start, ID/account mismatch, invalid base64, unsupported codec, oversize, idle timeout, and simultaneous terminal/disconnect cleanup.
- Tests prove outbound queue bounds and barge-in stale-audio clearing.
- MCP tests prove use of `otherLegCallId`, one explicitly selected account header, exact `destination_number` arguments, timeouts/retry policy, redaction, and destination allowlisting.
- Secret scanning finds no credential value in tracked changes.
- Python 3.11 compilation passes.
- Nginx and Compose configuration validate in an environment with those binaries; when the dev rig lacks Docker/Nginx, CI or the production preflight must supply that evidence before launch.
- A manual carrier test proves endpoint-unavailable fallback reaches the intended live-agent queue before production traffic is enabled.

## Operator Inputs Still Required Before Launch

- Rotate the Dialog API key that was exposed in chat and place only the replacement in the VPS secret environment.
- Confirm Dialog's WebSocket source IP ranges.
- Confirm the production MCP account header spelling and live queue/extension transfer URI with correct SIP domain.
- Confirm SmartPBX's failure/timeout branch to the live-agent queue.
- Create DNS/TLS for `smartpbx-flico.taskforceai.tech` or approve another dedicated hostname.
- Configure SmartPBX with the final WSS URL, `g711_ulaw`/`8000`, and generated custom token only after the endpoint passes preflight.
