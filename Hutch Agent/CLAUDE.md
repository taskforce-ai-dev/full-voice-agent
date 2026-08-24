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

## Barge-in echo gating (Aug 2026)

Dialog SmartPBX has no echo cancellation on the media path, so Selina's own
TTS audio leaks back into the mic and STT reports it as caller speech. The
original `_on_stt_result`/`_on_stt_interim` callbacks called `_handle_bargein()`
unconditionally whenever `_is_speaking` was true — any echo blip, however
short, cleared her audio and dropped her mid-sentence, which surfaced as two
apparently separate bugs: unreliable "stop and listen" barge-in, and
sentences frequently cut off before finishing.

Ported Kavya's proven fix: `BARGEIN_MIN_CHARS` (default `12`, clamp
`[0, 200]`) and `BARGEIN_DEBOUNCE_SECONDS` (default `0.6`, clamp `[0.0, 5.0]`),
plus a `_speaking_since` timestamp set when TTS starts and cleared when it
stops. `_should_barge_in()` only allows a barge-in when the STT result is at
least `BARGEIN_MIN_CHARS` long AND arrives at least `BARGEIN_DEBOUNCE_SECONDS`
after TTS started — short/early results (echo) are silently dropped instead
of interrupting her. A genuine interruption ("wait", "stop", a real question)
still barges in correctly; it just has to clear this bar first.

**Related fix, same investigation:** `_is_speaking` was only ever reset to
`False` on a clean (non-barge-in) finish by Twilio's `mark`/`tts_done` event
being **echoed back** through `run()`'s inbound WebSocket loop. Dialog
SmartPBX has no such echo — `_TransportWebSocketAdapter.send_text()` forwards
the `mark` to the transport's own internal flag and never calls back into
`pipeline._is_speaking`. Since SmartPBX is Hutch's only live path, this left
`_is_speaking` stuck `True` after the first sentence of any answer, for the
rest of the call. Depending on timing this cut both ways: sometimes false
barge-in on echo (fixed above), and sometimes a **missed** real interruption
— because an earlier spurious flip had already forced `_is_speaking` back to
`False`, so the caller's new question was accumulated as an ordinary
utterance instead of a barge-in, and had to wait behind the `_speak_lock`
the still-playing paragraph was holding: the old answer played out in full,
then the new one started immediately after with no natural gap. Fixed by
setting `_is_speaking = False` / `_speaking_since = None` directly in
`_tts_elevenlabs()`'s success path, right after sending the mark, instead of
waiting on an echo that never arrives on the live path.

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

## Deployment — auto-deploy on push (Aug 2026)

Pushing to `main` with changes under `Hutch Agent/**` auto-deploys to the
live `hutch-smartpbx` container on `67.207.90.109` via
`.github/workflows/deploy-hutch.yml` — a dedicated workflow, **not** part of
the shared `deploy-on-push.yml`/`deploy.yml` matrix the other 8 agents use
(see "Pending operator setup" #5 below for why). No approval gate — same
policy as the rest of the fleet: the human gate is the review before the
push/merge, not a gate in CI.

- **fast** (code / `knowledge_docs` only): rsync to `/opt/hutch/Hutch Agent`
  + hot-swap the changed `.py` into the running container (`docker cp`) +
  `docker restart hutch-smartpbx`. Seconds, no rebuild.
- **build** (`requirements*.txt` / `Dockerfile` / `docker-compose.yml`
  changed): rsync + `docker builder prune -f` (Hutch has a history of disk
  exhaustion from torch/CUDA build cache — see the Aug 2026 incident notes in
  this file's git history) + `docker compose --env-file .env.smartpbx
  --profile smartpbx build hutch-smartpbx` + `up -d --force-recreate`.
- A `py_compile` syntax gate on changed `.py` files blocks an obviously
  broken push before it ever reaches the VPS.
- `.env.smartpbx` and the runtime `chroma_db_hutch_smartpbx/` store are never
  touched by the rsync (excluded explicitly, and no `--delete` flag is used
  regardless).
- Manual redeploy: re-run the "Auto-Deploy Hutch" workflow from the Actions
  tab (`workflow_dispatch` is not wired — trigger by pushing a no-op commit
  touching `Hutch Agent/`, or ask an operator with Actions access to re-run
  the last run).
- **Verifying a deploy landed**, since this container has no CI-based image
  tag to compare against: `docker inspect hutch-smartpbx --format='{{.Created}}'`
  (should be recent) and `docker exec hutch-smartpbx grep -n "<a string
  unique to the change>" server.py` (should match what's on `main`).

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
5. ~~Add this agent to `.github/workflows/deploy-on-push.yml`'s agent list~~
   — **done differently, Aug 2026.** Hutch is not in that shared matrix and
   never will be: its on-VPS shape (a full git clone at `/opt/hutch/Hutch
   Agent`, a Compose profile needing `--profile smartpbx --env-file
   .env.smartpbx`) doesn't fit the generic engine's flat-rsync-target model,
   the same reason Kavya has its own separate workflows instead of joining
   that matrix. See `.github/workflows/deploy-hutch.yml` — a dedicated
   workflow, triggered only by changes under `Hutch Agent/`, fast/build mode
   auto-chosen the same way deploy.yml does. It cannot affect any other
   agent's deploy path.

## graphify — GRAPH-FIRST, ALWAYS

This sub-project is part of the shared graphify knowledge graph at
`../graphify-out/` (project root). See the root `CLAUDE.md` for the full
graphify workflow. This agent is new as of this scaffold and has not yet been
picked up by a graph update — run the appropriate `graphify update` variant
(see root `CLAUDE.md`) after this change lands, from whichever machine you are
on.
