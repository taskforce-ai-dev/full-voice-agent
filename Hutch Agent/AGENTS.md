# CLAUDE.md

This file provides guidance to Claude Code when working with code in this repository.

## What This Is

Selina is an English-only AI voice agent for **Hutch**, a Sri Lankan mobile
network operator (part of CK Hutchison Holdings). She handles the **Hutch
Enterprise inquiries line**: a caller asks about Hutch prepaid/postpaid plans,
pricing, data allowances, activation methods, or policies, and Selina answers
from a ChromaDB-based RAG knowledge base scraped from Hutch.lk.

Selina is a **pure KB/inquiries agent** — no PMS, no bookings, no live call
transfer. She has exactly one tool, `notify_human_handover`: when a caller's
question genuinely cannot be answered from the knowledge base (account-specific
billing, technical faults, enterprise contract negotiation, etc.), she collects
the caller's name and a callback/WhatsApp number and POSTs a handover payload
to n8n so a Hutch operator can follow up later. There is no live transfer to
attempt first, unlike Kavya — the notify tool is a normal, always-available
tool, not a post-failed-transfer recovery mechanism.

**SmartPBX-only.** Hutch connects via **Dialog SmartPBX ("Client Connect")
ONLY** — there is no Twilio number provisioned for this agent, and the
operator is deliberately moving off Twilio for Hutch. The Twilio/
ConversationRelay code path in `server.py` is present and importable but
**inert** (unconfigured, no credentials, no phone number to route to it) —
kept for fleet consistency with the rest of the repo and to avoid risky
surgical deletion, the same way Kavya keeps both Twilio and SmartPBX modes in
one file. `HUTCH_SERVICE_MODE` defaults to `"smartpbx"` (Kavya/Flico default
to `"twilio"` — Hutch is the first agent in this fleet where SmartPBX is the
default, not an opt-in add-on, because there is no Twilio number to default to).

## Project File Map

```
Hutch Agent/
├── server.py                  # Production server -- SmartPBX (default) + inert Twilio/ConversationRelay paths
├── knowledge_base.py          # ChromaDB RAG -- chunk, embed, query knowledge docs
├── knowledge_docs/
│   └── hutch_info.txt         # Hutch plans, pricing, activation, FAQs (from Hutch.lk)
├── handover.py                # Phone/WhatsApp normalisation + n8n webhook POST
├── tools.py                   # notify_human_handover tool definition + dispatch
├── media_transport.py         # Generic MediaTransport Protocol (ported from Flico)
├── smartpbx_protocol.py       # Dialog SmartPBX wire-event parser (ported from Flico, verbatim)
├── smartpbx_gateway.py        # SmartPBX auth/admission/session-loop gateway (ported from Flico; token header renamed)
├── smartpbx_transport.py      # Bounded outbound audio transport for Dialog (ported from Flico, verbatim)
├── smartpbx_session.py        # Adapter binding one Dialog call into MediaStreamSession
├── chroma_db/                 # ChromaDB vector store (auto-generated, gitignored)
├── Dockerfile                 # Production image (python:3.11-slim), runs server:app
├── docker-compose.yml         # `hutch` (dev, :8040) + `hutch-smartpbx` (profile smartpbx, :8041)
├── nginx-smartpbx.conf        # TLS vhost for smartpbx-hutch.taskforceai.tech -> 127.0.0.1:8041
├── requirements-prod.txt      # Production dependencies
├── .env.example                # Template for .env with all required/optional vars
└── CLAUDE.md                  # This file
```

## Architecture

### Service modes

`server.py` builds **one of two mutually exclusive FastAPI apps**, selected by
`HUTCH_SERVICE_MODE` (env var, `"smartpbx"` default) via
`build_service_app(service_mode, environ)` at the bottom of the file — the
same pattern as Kavya's `KAVYA_SERVICE_MODE` / `build_service_app`.

**`smartpbx` (default) — the only real ingress.** A narrow FastAPI app
(`docs_url`/`redoc_url`/`openapi_url` all disabled) exposing exactly:
- `GET /health` — `{"status": "ok", "service_mode": "smartpbx"}`
- `GET /smartpbx/status` — session counters (`active_sessions`,
  `admitted_total`, `rejected_capacity_total`, `released_total`), `enabled`,
  `configured`, `protocol_version`. Requires the `X-Hutch-SmartPBX-Token`
  header (constant-time compare). `/health` stays unauthenticated for
  liveness probes.
- `WS /ws/v1/smartpbx/media` — the Dialog media socket, gated by the same
  `X-Hutch-SmartPBX-Token` header, checked before `websocket.accept()`.

Audio is exact `g711_ulaw` at `8000` Hz only — any other codec/rate is
rejected at the `start` event. Capacity defaults to 4 concurrent calls
(`SMARTPBX_MAX_CALLS`).

**`twilio` — present, inert.** Everything you'd expect (`/voice/incoming`,
`/ws/conversation` ConversationRelay, `/ws/media-stream/{lang}` raw Media
Streams) is still in `server.py` and would work if `HUTCH_SERVICE_MODE=twilio`
were set and Twilio credentials + a phone number were provisioned. None of
that exists today. Do not delete this path — see "What This Is" above for why
it stays.

### Dialog call flow

```
Dialog SmartPBX call
  -> WS /ws/v1/smartpbx/media (smartpbx_gateway.SmartPBXGateway.handle)
  -> smartpbx_session.HutchSmartPBXSession binds the call into
     server.MediaStreamSession (built for Twilio Media Streams' wire format,
     driven directly here instead of through its Twilio-shaped event loop)
  -> STT (Google or Azure) -> endpointing -> KB retrieval -> Claude
     (streaming, notify_human_handover tool-use loop) -> ElevenLabs TTS
     (eleven_turbo_v2_5, ulaw_8000) -> g711_ulaw audio back to Dialog
```

`smartpbx_session.HutchSmartPBXSession` is deliberately much thinner than
`Kavya/smartpbx_session.py`: no PMS/booking tool binding, no
`SmartPBXHandoverCoordinator` (that's for live-transfer fallback, which Hutch
has nothing to fall back FROM), no post-call dashboard pipeline. It also
carries a small `_TransportWebSocketAdapter` that translates
`MediaStreamSession`'s Twilio-shaped `ws.send_text(json...)` calls into
`SmartPBXMediaTransport`'s typed `send_audio`/`send_mark`/`clear_audio`
methods — the smallest translation layer that lets `MediaStreamSession` run
unmodified against the SmartPBX transport.

### Knowledge base

`knowledge_docs/hutch_info.txt` is a single retrieval-optimized document:
300-500 character paragraphs, blank-line separated, each one repeating "Hutch"
(and the specific plan name) so a paragraph pulled in isolation by RAG
retrieval still makes sense without its neighbours. Organized by topic:
company overview and contact info, then each plan family (Hutch 15, Level-up,
cliQ, One Load, Gaurawa, Hutch Ultimate, Postpaid Combo, etc.) with its price/
data/validity/FAQ facts, then standard rates and general policies. Sourced
from 16 scraped Hutch.lk pages; all prices, data allowances, and plan names
are real (not invented), reproduced exactly as scraped.

### notify_human_handover tool

Unlike Kavya (whose handover tool is restricted to a post-failed-transfer
recovery session, because a normal Kavya call always has a live transfer to
attempt first), Hutch/Selina has **no live transfer at all** — the notify
tool is a normal, always-available tool alongside KB retrieval, offered
whenever the system prompt determines a question is out of scope. It is wired
into both the ConversationRelay tool list and the SmartPBX/MediaStreamSession
tool list (same `tools.py` module, same `get_tools_for("claude")` call).

`handover.py` POSTs to `N8N_HANDOVER_WEBHOOK` (default
`/webhook/hutch-handover`). **This webhook does not exist in n8n yet** — see
"Pending operator setup" below. Until it is built, the POST simply fails
(connection error or 404) and is logged as a warning; this is fail-open by
design, matching Kavya's `send_handover_notification` philosophy: a
notification failure must never affect the live call.

## Known limitations

- **Tool calling is Claude-only.** `notify_human_handover` is wired into the
  Claude streaming paths (`_run_llm_streaming_claude` for ConversationRelay,
  `_run_llm_claude` for MediaStreamSession/SmartPBX). The OpenAI and Gemini
  provider paths remain KB-grounded text generation only, with no tool
  support — the same scope Sofia Agent's OpenAI/Gemini paths always had.
  Switching `LLM_PROVIDER` away from `"claude"` in production silently
  disables the handover tool; do not do this without adding tool support to
  those paths first.
- **No IVR/DTMF menu.** Since Hutch is English-only, `/voice/incoming`
  connects straight to ConversationRelay with no language `<Gather>` menu
  (mirrors Flico's `rodrigo` brand, which also has no IVR).

## Environment Setup

Copy `.env.example` to `.env`. Key groups:

**LLM provider** (pick one — Claude is default and the only one with tool support):
- `LLM_PROVIDER`, `ANTHROPIC_API_KEY`, `CLAUDE_MODEL`
- `OPENAI_API_KEY`, `OPENAI_MODEL` (alternative, no tool support)
- `GEMINI_API_KEY`, `GEMINI_MODEL` (alternative, no tool support)

**TTS/STT:**
- `ELEVENLABS_API_KEY`, `ELEVENLABS_VOICE_ID` — TODO: needs a distinct voice
  ID for Selina before going live
- `STT_PROVIDER` (`google` default | `azure`), `AZURE_SPEECH_KEY`,
  `AZURE_SPEECH_REGION`, `GOOGLE_APPLICATION_CREDENTIALS`

**Handover:**
- `N8N_BASE_URL`, `N8N_HANDOVER_WEBHOOK`, `WHATSAPP_COUNTRY_CODE`

**Service mode:**
- `HUTCH_SERVICE_MODE` (`smartpbx` default), `ENABLE_SMARTPBX_WSS`,
  `SMARTPBX_WS_TOKEN`, `SMARTPBX_ACCOUNT_ID`, `SMARTPBX_MAX_CALLS`, and the
  message-size/timeout knobs — see `.env.example` for the full list, mirrored
  from `Kavya/.env.example`'s SmartPBX block.

**Dashboard:**
- `DASHBOARD_API_URL`, `DASHBOARD_API_KEY`, `DASHBOARD_AGENT_ID=hutch`

No `TWILIO_*` variables are populated (the keys are commented out in
`.env.example`) — see "What This Is" above.

## Server Endpoints

### SmartPBX mode (`HUTCH_SERVICE_MODE=smartpbx`, the default)
- `GET /health` — `{status, service_mode}` only
- `GET /smartpbx/status` — session counters; requires `X-Hutch-SmartPBX-Token` header (401 without it)
- `WebSocket /ws/v1/smartpbx/media` — Dialog media socket, requires `X-Hutch-SmartPBX-Token` header, `g711_ulaw`/8000 Hz only

### Twilio mode (`HUTCH_SERVICE_MODE=twilio`, inert — no credentials/number exist)
- `POST /voice/incoming` — TwiML `<Connect><ConversationRelay>`, no DTMF menu
- `WebSocket /ws/conversation` — English ConversationRelay
- `WebSocket /ws/media-stream/{lang}` — raw Media Streams (coerced to English)
- `POST /kb-reload` — hot-reload the knowledge base (token-gated via `KB_RELOAD_SECRET`)
- `GET /health` — full health payload (LLM provider, model, KB loaded, STT availability)

Note: `/kb-reload` and the full `/health` payload only exist on the Twilio
app object; the SmartPBX app is a separate, narrower FastAPI instance (see
"Service modes" above) and does not expose them. If SmartPBX-side KB hot-reload
is needed later, it would need to be added to `build_service_app`'s SmartPBX
branch explicitly.

## Key Design Decisions

- **SmartPBX default, not opt-in.** Every other agent in this fleet defaults
  `*_SERVICE_MODE` to `"twilio"` and treats SmartPBX as an add-on profile.
  Hutch inverts this because there is no Twilio number to default to.
- **Ported, not reinvented, SmartPBX plumbing.** `smartpbx_protocol.py`,
  `smartpbx_gateway.py`, `smartpbx_transport.py` are verbatim copies from
  Flico Agent (only the `X-Flico-SmartPBX-Token` header string was renamed to
  `X-Hutch-SmartPBX-Token`) — this transport-layer code is documented as
  generic/non-business-specific in `Kavya/CLAUDE.md`'s Service Modes section.
  `smartpbx_mcp.py` (Dialog MCP live-transfer call control) was deliberately
  **not** ported — Hutch has no live transfer, only WhatsApp-notify.
- **MediaStreamSession reused, not rewritten.** Selina's raw-audio pipeline
  is Sofia Agent's `MediaStreamSession` class (STT -> KB retrieval -> LLM ->
  TTS), simplified to always run in English and given a tool-use loop for
  `notify_human_handover`. The general plumbing (interim-driven endpointing,
  barge-in via speak-generation fencing, sentence-boundary TTS dispatch) is
  unchanged from Sofia's Tamil implementation.
- **Single knowledge base document.** All 16 scraped Hutch.lk pages were
  consolidated into one `hutch_info.txt`, discarding repeated navigation
  menus, footers, and image alt-text that added no retrieval value.

## Deployment — MANUAL (not auto-deploy)

Hutch is **deliberately not in** `.github/workflows/deploy-on-push.yml`'s agent
list (see "Pending operator setup" #5), so **merging to `main` does NOT deploy
Hutch**. Production is updated by hand on the VPS (`67.207.90.109`), where the
repo is checked out at `/opt/hutch` and the SmartPBX container runs from
`/opt/hutch/Hutch Agent`.

```bash
ssh root@67.207.90.109
cd /opt/hutch && git pull origin main
cd "Hutch Agent"
docker compose --env-file .env.smartpbx --profile smartpbx up -d --force-recreate hutch-smartpbx
sleep 25 && curl http://127.0.0.1:8041/health   # -> {"status":"ok","service_mode":"smartpbx"}
```

> **The `--env-file .env.smartpbx` flag is MANDATORY.** The `hutch-smartpbx`
> service intentionally has **no `env_file:`** in `docker-compose.yml` — it uses
> an explicit `environment:` allowlist of `${VAR}` references (Kavya isolation
> pattern). Compose fills those `${VAR}` from the `--env-file` you pass (or the
> shell / a plain `.env`, neither of which exists here). Run a plain
> `docker compose ... up` without `--env-file .env.smartpbx` and **every**
> setting resolves to a blank string — you'll see a wall of
> `WARN ... variable is not set. Defaulting to a blank string`, the container
> starts with no API keys / token / voice ID, and `/health` returns an empty
> reply / connection reset. The fix is always to re-run **with** the flag.

After a KB or `KB_N_RESULTS` change, `--force-recreate` is enough (no `--build`
needed unless `requirements*.txt` / `Dockerfile` changed). On startup the
container re-ingests `knowledge_docs/hutch_info.txt` into ChromaDB — expect a
20-40s boot window where `/health` connection-resets before
`Application startup complete` appears in the logs. Watch it with:
`docker compose --env-file .env.smartpbx --profile smartpbx logs --tail=30 hutch-smartpbx`.

## Pending operator setup

Before this agent can go live, the operator still needs to:

1. **Dialog SmartPBX/Client Connect tenant** — provision the account with
   Dialog, obtain the `accountId` (-> `SMARTPBX_ACCOUNT_ID`) and confirm
   concurrent-call capacity (-> `SMARTPBX_MAX_CALLS`).
2. **DNS + TLS certificate** for `smartpbx-hutch.taskforceai.tech` (the
   hostname `nginx-smartpbx.conf` is written for).
3. **A distinct ElevenLabs voice ID for Selina** — `ELEVENLABS_VOICE_ID` is
   blank in `.env.example`; either a stock library voice or a newly cloned
   one, but not shared with another agent's persona.
4. **Build the `N8N_HANDOVER_WEBHOOK` workflow in n8n** — `/webhook/hutch-handover`
   does not exist yet. Until it does, `notify_human_handover` fails silently
   (fail-open by design) and no Hutch operator is actually notified.
5. **Add this agent to `.github/workflows/deploy-on-push.yml`'s agent list**
   — deliberately NOT done yet. No Dialog account or secrets exist to deploy
   against, so wiring auto-deploy now would just mean the workflow silently
   does nothing (or fails) on every push. This is a later, deliberate step
   for the operator once the above are ready.

## graphify — GRAPH-FIRST, ALWAYS

This sub-project is part of the shared graphify knowledge graph at
`../graphify-out/` (project root). See the root `CLAUDE.md` for the full
graphify workflow. This agent is new as of this scaffold and has not yet been
picked up by a graph update — run the appropriate `graphify update` variant
(see root `CLAUDE.md`) after this change lands, from whichever machine you are
on.
