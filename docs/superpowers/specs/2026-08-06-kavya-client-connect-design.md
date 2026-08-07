# Kavya Dialog Client Connect Migration Design

**Date:** 2026-08-06
**Status:** Accepted — authorized by the project owner
**Owner:** Taskforce AI

## Objective

Replace Kavya's inbound Twilio phone path with Dialog Client Connect SmartPBX while preserving Kavya's hotel knowledge base, PMS tools, post-call processing, operational limits, and safe human handover. Flico stays online and unchanged until Dialog's dashboard is deliberately cut over to Kavya.

## Decision

Run a dedicated `kavya-smartpbx` service on the existing VPS. It will accept Dialog's direct WebSocket media contract at `/ws/v1/smartpbx/media`, process G.711 mu-law 8 kHz audio through a Kavya-specific media session, and return the same media format. The service shares the Kavya image and data volumes but uses its own WSS hostname, loopback port, header name, high-entropy token, capacity counter, configuration, logs, and health endpoint.

Kavya's current Twilio HTTP/TwiML/ConversationRelay and Twilio Media Streams routes remain in the legacy service during the rollout. They are not invoked by SmartPBX service mode. This makes rollback a Dialog dashboard routing change rather than a destructive replacement of the currently live path.

## Architecture

```text
Dialog DID / Client Connect workflow
        |
        | WSS + X-Kavya-SmartPBX-Token
        | g711_ulaw / 8000 Hz
        v
smartpbx-kavya.<approved-domain> Nginx TLS endpoint
        |
        | http://127.0.0.1:8006/ws/v1/smartpbx/media
        v
kavya-smartpbx container
        |
        +-- strict SmartPBX protocol and four-call admission gateway
        +-- KavyaSmartPBXMediaTransport
        +-- Kavya SmartPBX session adapter
        |     +-- STT -> existing KB/PMS tools -> LLM -> TTS
        |     +-- existing post-call extraction/webhook
        |
        +-- bounded Dialog MCP transfer_call, only to configured destinations
```

## Inbound Protocol and Security

The implementation adopts the verified Flico Dialog contract exactly:

- endpoint: `/ws/v1/smartpbx/media`;
- inbound token: `X-Kavya-SmartPBX-Token`, constant-time verified before WebSocket acceptance;
- required `start` context: `callId`, `otherLegCallId`, caller/callee numbers, `accountId`, and `mediaFormat`;
- exact media: `g711_ulaw` at `8000` Hz;
- supported events: `connected`, `start`, `media`, `dtmf`, `hangup`, and `stop`;
- outbound audio: a documented `media` envelope carrying the original `callId`, `accountId`, and base64 mu-law payload.

The gateway fail-closes on a missing/mismatched token, account mismatch, invalid event order, malformed JSON/base64, wrong codec/rate, and oversized messages. It reserves capacity before accepting the socket. Initial limits are the purchased Dialog capacity and proven Flico bounds: four active calls, 64 KiB messages, 32 KiB decoded audio, 128 queued outbound frames, ten-second start deadline, and 90-second idle deadline.

No Dialog API key is accepted as WSS authentication. Tokens, API keys, raw caller numbers, raw call IDs, media, transcripts, and MCP response bodies are excluded from logs/status output. Dialog source-IP filtering remains an optional Nginx defense once Dialog supplies production egress ranges.

## Kavya Session Adaptation

Kavya's existing `MediaStreamSession` is Twilio wire-protocol bound, so it will not be connected directly to the Dialog gateway. The migration adds a narrow SmartPBX session adapter that owns the Dialog lifecycle and uses a generic media transport for outbound audio. It reuses Kavya's existing STT selection, LLM/tool behavior, knowledge base, PMS calls, post-call process, and privacy controls.

The adapter creates post-call metadata from the validated Dialog `start` context: `otherLegCallId` is the telephony correlation ID and `callerIdNumber` is the caller phone. It must finish exactly once on hangup, stop, timeout, socket close, or internal failure; it schedules post-call work only for completed sessions and releases the gateway lease exactly once.

Azure STT credentials and provider configuration are passed explicitly to the isolated service. This avoids the invalid Google-credential mount problem previously observed in the Flico deployment. The service fails cleanly if no configured STT backend is usable.

## Human Handover

Twilio `<Dial>`, REST redirect, and dial-status callbacks cannot transfer a Dialog Client Connect call and are not reused in the SmartPBX adapter. Instead, the adapter exposes a single logical `transfer_to_human` action that invokes the Dialog MCP `transfer_call` tool against the active `otherLegCallId`.

The MCP client is fail-closed:

- MCP endpoint, API key, account ID, and account-header spelling are environment-only configuration;
- the implementation sends exactly one explicitly configured account header, never guesses `account_id` versus `X-Account-ID`;
- the LLM supplies a logical destination key only;
- `SMARTPBX_TRANSFER_DESTINATIONS_JSON` maps that key to one approved `tel:` or `sip:` target;
- an empty destination map disables transfer;
- validation/auth failures never retry; bounded network/server failures retry once; failure returns a safe in-call fallback rather than routing or disconnecting blindly.

The existing WhatsApp handover notification may remain as an operational notification after a failed handover, but it is not evidence of a successful Dialog transfer.

Live transfer requires Dialog to provide or approve all of the following before it is enabled: a least-privilege API key, the exact MCP account-header name, a non-production test destination, and the production queue/extension/ring-group URI. Until then, production code and tests prove the disabled path, not live transfer.

## Deployment and Cutover

`kavya-smartpbx` will be a Compose profile using a dedicated loopback port (`127.0.0.1:8006`) and a dedicated TLS Nginx virtual host. It shares only read-only Kavya data/knowledge volumes and explicitly named environment variables. Flico's port 8005, hostname, WSS token, dashboard workflow, and container remain untouched.

The operator creates a fresh Kavya WSS token and configures Dialog's AI Provider workflow with the new hostname, `X-Kavya-SmartPBX-Token`, and G.711 mu-law/8 kHz settings. The existing Flico workflow is retained as the rollback target. Cutover is accepted only after a real call proves audio in both directions, a post-call record, the four-call boundary, a WSS-auth rejection, and an unavailable-endpoint fallback.

## Alternatives Considered

### Keep Twilio and add a Dialog-to-Twilio bridge

Rejected. It adds a paid hop, codec/lifecycle translation, and another failure domain without using Client Connect directly.

### Reuse Flico's service and switch its persona to Kavya

Rejected. It risks Flico's active customer path and mixes two customers' prompts, knowledge, credentials, observability, and rollout controls.

### Retain Twilio handover inside the Dialog call

Rejected. Twilio holds no active Dialog call leg, so it cannot transfer the caller. Dialog MCP is the correct control plane.

## Acceptance Criteria

1. Kavya has a dedicated, token-authenticated Dialog WSS endpoint that accepts only G.711 mu-law/8 kHz calls and enforces four active calls.
2. An accepted Dialog session runs Kavya's STT, knowledge/PMS, LLM/tool, TTS, and post-call path without a Twilio dependency.
3. Invalid protocol/auth/account/media input cleans up safely without leaking secrets or PII.
4. Dialog transfer code accepts only configured logical destinations and is safely disabled with incomplete configuration.
5. Unit/integration coverage proves protocol, transport, lifecycle, post-call, transfer configuration, and isolated deployment contract.
6. Flico and existing Twilio Kavya service modes remain unchanged through the rollout.
7. A real Dialog dashboard call confirms the deployment. A real transfer is only accepted after Dialog supplies the approved destination/account-header configuration and the non-production transfer drill passes.
