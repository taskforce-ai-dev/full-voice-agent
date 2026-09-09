# CLAUDE.md

This file provides guidance to Claude Code when working with code in this repository.

## What This Is

Vidya is a bilingual (English + Sinhala) AI voice agent for **IAAC — the
International Airline and Aviation College**, an aviation college in Sri Lanka.
She handles the **IAAC inquiries line**: a caller asks about courses, entry
requirements, fees, intakes, campus/contact details, or general information,
and Vidya answers from a ChromaDB-based RAG knowledge base of IAAC material.

Vidya is a **pure inquiry / KB agent — nothing transactional**:
- **NO PMS / booking** — no Yanolja, no availability or reservation tools.
- **NO live human transfer** — no Dialog MCP call control.
- **NO handover / n8n** — no WhatsApp notify, no post-call webhook.
- **NO dashboard** — no post-call push.
- **NO post-call automation** of any kind.

She is a clone of the **Kavya** Dialog stack with all of the above dropped **by
configuration, not by deleting code**. The cloned Kavya modules (`yanolja_*`,
`booking_api.py`, `smartpbx_mcp.py`, `smartpbx_handover.py`, `handover.py`,
`post_call.py`, `dashboard_client.py`, `rate_catalog.py`) are still present for
fleet consistency and low-risk cloning, but are **inert**: with no Yanolja
credentials `get_tools()` returns `[]`, so the model has no booking or transfer
tool to call; with `SMARTPBX_TRANSFER_DESTINATIONS_JSON={}` and no MCP creds
live transfer is disabled; and with no n8n/dashboard creds those paths never
fire. Inquiry-only is thus enforced by the deploy-time env allowlist, not just
by the system prompt. This mirrors how Hutch keeps its inert Twilio path.

**SmartPBX-only.** IAAC connects via **Dialog SmartPBX ("Client Connect")
ONLY** — there is no Twilio number provisioned for this agent. The Twilio /
ConversationRelay code path in `server.py` is present and importable but
**inert** (unconfigured, no credentials, no phone number). `IAAC_SERVICE_MODE`
defaults to `"smartpbx"` (same inversion as Hutch — SmartPBX is the default,
not an opt-in add-on, because there is no Twilio number to default to).

**Bilingual via a DTMF language menu.** On the Dialog line, callers hear a menu
and press **1 for English** or **2 for Sinhala**. A timeout, invalid, or
replayed-invalid selection resolves to English. English runs Claude + ElevenLabs;
Sinhala runs Gemini (LLM + voice) + Azure STT. The bilingual menu is
deliberately withheld when `GEMINI_API_KEY` is missing/blank, so a caller can
never reach a broken Sinhala path.

## The stack

- **English brain:** Anthropic **Claude** (`CLAUDE_MODEL`, default
  `claude-sonnet-4-5-20250929`), `LLM_PROVIDER=claude`. Claude is the only
  provider with tool support, but IAAC ships no tools, so this is KB-grounded
  conversation.
- **Sinhala brain:** **Gemini** (`SMARTPBX_SINHALA_GEMINI_LLM_MODEL`, default
  `gemini-3.7-flash`, `low` thinking, 1024-token ceiling), call-local, with a
  bounded Gemini→Claude technical failover.
- **English voice (TTS):** **ElevenLabs** (`KAVYA_EN_ELEVENLABS_VOICE_ID` /
  `ELEVENLABS_VOICE_ID`).
- **Sinhala voice (TTS):** **Gemini** TTS (`gemini-3.1-flash-tts-preview`,
  voice `Vindemiatrix`), with a quota-aware model fallback chain and a
  persistent phrase cache.
- **Sinhala STT (ears):** **Azure Speech** forced to `si-LK`
  (`AZURE_SPEECH_KEY` / `AZURE_SPEECH_REGION`).
- **English STT (ears):** **Google Cloud STT**, reusing the shared GCP
  service-account file mounted at `/app/gcp-credentials.json`
  (`GOOGLE_APPLICATION_CREDENTIALS`, set in `docker-compose.yml`).
- **Knowledge base:** ChromaDB RAG over `knowledge_docs/`, `KB_N_RESULTS=8`.

## Project File Map

```
IAAC Agent/
├── server.py                  # Production server -- SmartPBX (default) + inert Twilio/ConversationRelay paths
├── knowledge_base.py          # ChromaDB RAG -- chunk, embed, query knowledge docs
├── knowledge_docs/            # Source documents for RAG (IAAC courses/fees/intakes/FAQs)
├── english_voice_profile.py   # ElevenLabs English voice profile loader
├── smartpbx_protocol.py       # Dialog SmartPBX wire-event parser
├── smartpbx_gateway.py        # SmartPBX auth/admission/session-loop gateway
├── smartpbx_transport.py      # Bounded, realtime-paced outbound audio transport
├── smartpbx_session.py        # Adapter binding one Dialog call into MediaStreamSession (+ language menu)
├── smartpbx_dtmf.py           # DTMF keypad capture helpers
├── smartpbx_diagnostics.py    # Seven-field diagnostic log vocabulary
├── smartpbx_language_menu.ulaw # Pre-rendered g711_ulaw language-menu prompt (MUST be re-recorded for IAAC — see below)
├── tools.py                   # Tool definitions + dispatch (INERT: no tools without Yanolja creds)
├── yanolja_service.py         # INERT clone (no PMS creds -> no booking/availability)
├── yanolja_client.py          # INERT clone
├── booking_api.py             # INERT clone
├── smartpbx_mcp.py            # INERT clone (no live transfer)
├── smartpbx_handover.py       # INERT clone (no live transfer fallback)
├── handover.py                # INERT clone (no n8n handover) — still holds number-normalisation helpers
├── post_call.py               # INERT clone (no post-call webhook)
├── dashboard_client.py        # INERT clone (no dashboard push)
├── rate_catalog.py            # INERT clone (no rate quoting)
├── chroma_db_iaac_smartpbx/   # ChromaDB vector store (runtime, gitignored, bind-mounted)
├── smartpbx_phrase_cache_iaac/ # Persistent Sinhala fixed-phrase TTS cache (runtime, bind-mounted)
├── Dockerfile                 # Production image (python:3.11-slim), runs server:app
├── docker-compose.yml         # iaac-smartpbx (profile smartpbx, 127.0.0.1:8042), explicit env allowlist
├── nginx-smartpbx.conf        # TLS vhost for smartpbx-iaac.taskforceai.tech -> 127.0.0.1:8042
├── nginx-smartpbx-acme.conf   # One-time HTTP-01 ACME bootstrap vhost (port 80)
├── requirements-prod.txt      # Production dependencies
├── .env.example               # Template documenting only the inquiry-only allowlist vars
├── CLAUDE.md                  # This file
└── AGENTS.md                  # Kept in sync with this file (repo convention)
```

## Architecture

### Service modes

`server.py` builds **one of two mutually exclusive FastAPI apps**, selected by
`IAAC_SERVICE_MODE` (env var, `"smartpbx"` default) via
`build_service_app(service_mode, environ)` — the same pattern as Kavya's
`KAVYA_SERVICE_MODE` and Hutch's `HUTCH_SERVICE_MODE`.

**`smartpbx` (default) — the only real ingress.** A narrow FastAPI app
(`docs_url`/`redoc_url`/`openapi_url` all disabled) exposing exactly:
- `GET /health` — `{"status": "ok", "service_mode": "smartpbx"}` (unauthenticated liveness)
- `GET /smartpbx/status` — session counters, `enabled`, `configured`,
  `protocol_version`. Requires the `X-IAAC-SmartPBX-Token` header (constant-time compare).
- `WS /ws/v1/smartpbx/media` — the Dialog media socket, gated by the same
  `X-IAAC-SmartPBX-Token` header, checked before `websocket.accept()`.

Audio is exact `g711_ulaw` at `8000` Hz only. Capacity defaults to 4 concurrent
calls (`SMARTPBX_MAX_CALLS`).

**`twilio` — present, inert.** The Twilio/ConversationRelay/Media-Streams code
is still in `server.py` but has no credentials or number and is never reached.
Do not delete it.

### Dialog call flow

```
Dialog SmartPBX call
  -> WS /ws/v1/smartpbx/media (smartpbx_gateway.SmartPBXGateway.handle)
  -> smartpbx_session: play language menu (press 1 English / 2 Sinhala, DTMF)
  -> resolve a call-local English or Sinhala profile, bind into MediaStreamSession
  -> English: Google/Azure STT -> KB retrieval -> Claude -> ElevenLabs TTS
     Sinhala: Azure si-LK STT -> KB retrieval -> Gemini -> Gemini TTS
  -> g711_ulaw audio back to Dialog
```

The session class is still named `KavyaSmartPBXSession` (clone). No PMS/booking
tool binding, no transfer coordinator, no post-call pipeline is active in the
inquiry-only configuration.

### Language menu audio

`smartpbx_session._load_smartpbx_language_menu_audio()` loads and validates
`smartpbx_language_menu.ulaw` at session start. It is a **strict-format
`g711_ulaw` (8 kHz, mono) asset** — the loader validates it, so a wrong-format
or corrupt file fails the session. **The cloned file currently plays the
Kavya / Hatton Hills prompt and MUST be re-recorded for IAAC before go-live**
(see Pending operator setup).

## Environment Setup

Real secrets live only in `/opt/iaac/.env.smartpbx` on the VPS (never
committed). `.env.example` documents the full inquiry-only allowlist. The
`iaac-smartpbx` service uses an **explicit environment allowlist** in
`docker-compose.yml` with **no `env_file:` stanza** — a var set only in
`.env.smartpbx` is ignored unless it is also in that allowlist. See
`.env.example` for the grouped key list.

## Deployment

### First deploy — MANUAL (once), on the VPS

IAAC builds **on the VPS** (like Hutch), not from a pre-built immutable image
(unlike Kavya). The first deploy is done by hand:

1. Clone/checkout the repo to `/opt/iaac` on `67.207.90.109` (repo root), so the
   agent dir is `/opt/iaac/IAAC Agent`.
2. Create `/opt/iaac/.env.smartpbx` (chmod 600, root-owned) with the real
   secrets — see `.env.example` and Pending operator setup.
3. Bring the container up:

```bash
cd "/opt/iaac/IAAC Agent"
docker compose --env-file .env.smartpbx --profile smartpbx up -d --build iaac-smartpbx
docker compose --env-file .env.smartpbx --profile smartpbx logs -f iaac-smartpbx
```

> **`--env-file .env.smartpbx` is MANDATORY on EVERY compose command for this
> service.** The service has no `env_file:` stanza and an explicit allowlist, so
> without `--env-file` the `${...}` substitutions resolve empty and the
> container comes up mis-configured (no API keys, no token). This is the same
> hard requirement Hutch documents — do not drop the flag, ever.

- Container name: **`iaac-smartpbx`**. Compose profile: **`smartpbx`**.
  Loopback port: **`127.0.0.1:8042`** (kavya-smartpbx=8006, hutch-smartpbx=8041
  are taken). Public TLS terminates at the `smartpbx-iaac.taskforceai.tech`
  Nginx vhost in front of that port.

### After the first deploy — AUTO-DEPLOY ON PUSH

Pushing to `main` with changes under `IAAC Agent/**` auto-deploys to the live
`iaac-smartpbx` container via `.github/workflows/deploy-iaac.yml` — a dedicated
workflow, **not** part of the shared `deploy-on-push.yml`/`deploy.yml` matrix
(same reasoning as Hutch/Kavya). No approval gate — the human gate is the review
before the merge.

- **fast** (code / `knowledge_docs` only): rsync to `/opt/iaac/IAAC Agent` +
  `docker cp` the changed `.py` into the running container + `docker restart
  iaac-smartpbx`. Seconds, no rebuild.
- **build** (`requirements*.txt` / `Dockerfile` / `docker-compose.yml` changed):
  rsync + `docker builder prune -f` + `docker compose --env-file .env.smartpbx
  --profile smartpbx build iaac-smartpbx` + `up -d --force-recreate`.
- A `py_compile` syntax gate on changed `.py` files blocks a broken push.
- `.env.smartpbx`, `chroma_db_iaac_smartpbx/`, and `smartpbx_phrase_cache_iaac/`
  are never touched by the rsync (excluded; no `--delete`).
- The rsync uses `rsync -az -s` (`--protect-args`) so the **space in
  "IAAC Agent"** survives — do NOT hand-escape the space (Hutch lost two deploys
  to exactly that bug).
- Manual redeploy: `workflow_dispatch` on "Auto-Deploy IAAC" from the Actions
  tab, choosing `mode: fast` or `mode: build`.
- **Verifying a deploy landed:** `docker exec iaac-smartpbx grep -n "<a string
  unique to the change>" server.py`. A `docker restart` does not change the
  image `Created` timestamp, so an old time is normal for fast mode.

## Pending operator setup

Before this agent can go live, the operator still needs to:

1. **Provision the Dialog SmartPBX / Client Connect tenant** — obtain the
   `accountId` (→ `SMARTPBX_ACCOUNT_ID`), confirm concurrent-call capacity
   (→ `SMARTPBX_MAX_CALLS`), and configure the per-language DID / menu routing
   into `/ws/v1/smartpbx/media`.
2. **Generate `SMARTPBX_WS_TOKEN`** — `openssl rand -hex 32`, set it in
   `/opt/iaac/.env.smartpbx` and give the same value to Dialog for the
   `X-IAAC-SmartPBX-Token` header.
3. **Create the API keys** into `/opt/iaac/.env.smartpbx` (chmod 600):
   `ANTHROPIC_API_KEY` (English brain), `GEMINI_API_KEY` (Sinhala brain +
   voice — the bilingual menu is withheld without it), `ELEVENLABS_API_KEY`
   + a distinct `KAVYA_EN_ELEVENLABS_VOICE_ID`/`ELEVENLABS_VOICE_ID` for
   Vidya (not shared with another agent's persona), and
   `AZURE_SPEECH_KEY`/`AZURE_SPEECH_REGION` (Sinhala STT). The shared GCP
   credentials file must be present at the mount path for English Google STT.
4. **DNS + TLS** for `smartpbx-iaac.taskforceai.tech` — point DNS at the VPS,
   bootstrap the cert with `nginx-smartpbx-acme.conf` (HTTP-01 on port 80) +
   certbot, then install `nginx-smartpbx.conf` (the TLS vhost → `127.0.0.1:8042`).
5. **Re-record the language-menu audio `smartpbx_language_menu.ulaw` for IAAC.**
   The cloned file currently speaks the Kavya / Hatton Hills prompt. It MUST be
   replaced with an IAAC-branded English + Sinhala "press 1 for English, press 2
   for Sinhala" prompt. It is a **strict-format `g711_ulaw` (8 kHz mono)** asset
   validated by `smartpbx_session._load_smartpbx_language_menu_audio` — an
   MP3/WAV or wrong-rate file will fail the session, so render it to raw 8 kHz
   mono µ-law.

## graphify — GRAPH-FIRST, ALWAYS

This sub-project is part of the shared graphify knowledge graph at
`../graphify-out/` (project root). See the root `CLAUDE.md` for the full
graphify workflow. This agent is new as of this scaffold and has not yet been
picked up by a graph update — run the appropriate `graphify update` variant
(see root `CLAUDE.md`) after this change lands, from whichever machine you are on.
