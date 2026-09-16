# Riya — Riviera Resort Voice Agent

Riya is an inbound phone agent for **Riviera Resort**, a lagoon-front resort on the Kallady
lagoon in Batticaloa, on Sri Lanka's east coast. She handles reservations and guest queries in
**English, Sinhala and Tamil**, answers from a ChromaDB knowledge base, and checks availability /
takes bookings against a **dedicated Yanolja-style PMS instance**.

> **Riviera is a full clone of [`Kavya/`](../Kavya/)** (taken at monorepo commit `6258d10`,
> Sep 2026) with every protection Kavya carries — the guarded immutable-image SmartPBX deploy,
> the fail-closed Dialog media gateway, the handover failsafe, the deterministic rate catalogue,
> the ~600-test suite — rebranded for a real client and **fully isolated**: its own folder,
> image, containers, loopback ports, Chroma collection, PMS instance, hostnames and secrets.
> Nothing in `Riviera/` imports from or depends on `Kavya/` (or any other agent) at runtime or
> deploy time.

- **Property:** single (Riviera Resort, New Dutch Bar Road, Kallady, Batticaloa)
- **Persona:** Riya — female, British accent (client requirement); the ElevenLabs voice id is
  supplied via `RIVIERA_EN_ELEVENLABS_VOICE_ID` and never defaulted
- **Languages:** English (Twilio ConversationRelay / Dialog direct), Sinhala and Tamil (Twilio
  Media Streams / Dialog direct). Twilio IVR: `1` EN, `2` SI, `3` TA (`IVR_MENU_ENABLED=true`).
  Dialog SmartPBX menu: `1` EN, `2` SI, `3` TA.
- **LLM:** configurable — Claude (default) / OpenAI / Gemini, with tool use; Sinhala on the Dialog
  line uses the Gemini brain + Gemini/Rime TTS exactly as Kavya does
- **STT/TTS:** EN → Azure/Google STT + ElevenLabs flash; SI → Azure `si-LK` STT + Gemini
  `Vindemiatrix` (Dialog) or OpenAI `gpt-4o-mini-tts` (Twilio); TA → Google `ta-IN` STT +
  ElevenLabs `eleven_multilingual_v2`
- **Server:** FastAPI / uvicorn — host ports `127.0.0.1:8060` (Twilio) and `127.0.0.1:8061`
  (SmartPBX profile), containers `riviera-voice-agent` / `riviera-smartpbx`
- **Hostnames (to provision):** `riviera.taskforceai.tech` (Twilio) and
  `smartpbx-riviera.taskforceai.tech` (Dialog)

## Room types and rates

Seven room types. Sri Lankan **resident** rates in LKR, per room per night, **room only**,
inclusive of all taxes. Two date-range seasons: **mid** 1 Sep 2026 – 30 Jun 2027, **high**
1 Jul – 31 Aug 2027. No foreign-guest rate sheet was supplied — foreign guests are referred to
reservations (`065 222 2164`, WhatsApp `077 842 2223`).

| Room type | Max guests | Mid LKR | High LKR | PMS code | Rooms |
|---|---|---|---|---|---|
| Family Chalet | 4 | 32,100 | 46,900 | `RV-FCH` | 36, 37 |
| Basic Room Single *(fan only, no A/C)* | 2 | 8,800 | 12,800 | `RV-BRS` | 7, 10, 11 |
| Wooden Cabana *(fan only, no A/C)* | 2 | 11,000 | 16,000 | `RV-WCB` | 12 |
| Lagoon View Steel Cabana | 2 | 14,600 | 21,300 | `RV-SCB` | 31–34 |
| Double Lagoon or Garden View | 3 | 17,500 | 25,600 | `RV-DBL` | 4, 5, 6, 15, 16, 18, 19 |
| Triple Garden View | 3 | 20,400 | 29,800 | `RV-TRP` | 20, 9 |
| Family Cottage | 5 | 24,800 | 36,200 | `RV-FCT` | *(confirm — sheet says 9 twice)* |

Meal plans are per-person per-day supplements: BB LKR 2,000 (child 3–12: 1,500), HB LKR 4,500
(child: 3,000). Extra-adult / extra-child supplements and activity prices were not supplied and
are never invented — Riya refers those to reservations.

Rates are gated behind `RATES_ENABLED` (default `true`; `DEMO_RATES_ENABLED` still honoured) and
`base_price` in the PMS must equal the **mid** figure. **The room-type names are load-bearing**: the
PMS schema has no property column, so the name is the only source of property identity. They must
match `yanolja_service.ROOM_TYPES_BY_PROPERTY`, `tools.ROOM_TYPES_BY_PROPERTY`,
`post_call.ROOM_TYPES_BY_PROPERTY` and `room_types.name` byte-for-byte, or `_property_of()`
returns `""` and the room silently vanishes from availability.

## Knowledge base

`knowledge_docs/riviera_info.txt` — built from the client's *HOSPITALITY BOOKING AGENT
REQUIREMENTS* and *RATES* sheets plus a full scrape of riviera-online.com (Sep 2026), in the
retrieval-optimised style the fleet uses (300–500-character paragraphs, explicit entity names,
spelled-out numbers). It covers rooms and rates, meal plans, check-in/out and late-departure
rules, deposit and cancellation terms, dining, facilities, activities, sustainability and the
Batticaloa sights. Facts still not supplied (extra-bed supplements, activity and menu prices,
foreign rates) are written as "reservations will confirm", never guessed. The file is re-embedded on
container start (and via `POST /kb-reload`).

## Key files
| File | Purpose |
|---|---|
| `server.py` | Unified production server (IVR + ConversationRelay + Media Streams + SmartPBX app) + system prompt |
| `smartpbx_*.py` | Dialog SmartPBX gateway, protocol, transport, session (language menu, en/si/ta profiles), MCP transfer, handover |
| `tools.py` | Tool defs (Anthropic/OpenAI/Gemini) + dispatch |
| `booking_api.py`, `yanolja_service.py`, `yanolja_client.py` | PMS / booking integration; `yanolja_service` holds the room catalogue and LKR rate card |
| `rate_catalog.py` | Deterministic rate resolution (room × residency × date-range season) |
| `handover.py`, `smartpbx_handover.py` | Human transfer + WhatsApp failsafe |
| `knowledge_base.py` + `knowledge_docs/` | ChromaDB RAG (collection `riviera_kb`) |
| `post_call.py` | Post-call summary → n8n → Google Sheets |
| `ops/riviera-pms/` | Dedicated PMS instance seed SQL, runbook, live verifier |
| `scripts/deploy_smartpbx_image.sh` | Guarded immutable-image deploy (runbook) |
| `scripts/generate_smartpbx_language_menu.py` | Renders the trilingual static menu asset |
| `SMARTPBX_RUNBOOK.md` | Full Dialog cutover / rollback procedure |
| `kpms_service.py`, `media_stream_server.py` | Inherited dead/reference code — nothing imports them |

## Bring-up checklist (not done by the repo — operator actions)

1. **DNS + TLS** on `67.207.90.109`: `riviera.taskforceai.tech` and
   `smartpbx-riviera.taskforceai.tech` (nginx sites from `nginx.conf` / `nginx-smartpbx*.conf`).
2. **`/opt/riviera/.env`** from `.env.example` (Twilio creds, `RIVIERA_EN_ELEVENLABS_VOICE_ID`
   = the chosen female British voice, `ELEVENLABS_VOICE_ID` for Tamil, Azure/Google STT creds,
   `GEMINI_API_KEY`, `OPENAI_API_KEY`) and root-only **`/opt/riviera/.env.smartpbx`** per
   `SMARTPBX_RUNBOOK.md`; mount the GCP service-account JSON.
3. **PMS instance** — provision and seed per `ops/riviera-pms/RUNBOOK.md`; set
   `YANOLJA_BASE_URL/USERNAME/PASSWORD`; run `ops/riviera-pms/verify_live.py`.
   **`YANOLJA_BASE_URL` has no default** — while it is blank the booking tools are withheld and
   no PMS request is made, even with credentials set (never point it at another property's PMS).
4. **n8n destination** — set `N8N_BASE_URL` to Riviera's own n8n host, `N8N_POSTCALL_WEBHOOK`
   to the path of its own post-call workflow (e.g. `/webhook/post-call-data`) and
   `N8N_HANDOVER_WEBHOOK` to the path of its own handover workflow (e.g.
   `/webhook/riviera-handover`). **No default for host or either path**: blank means no
   transcript, call record or handover payload ever leaves the container.
5. **Regenerate `smartpbx_language_menu.ulaw`** with `scripts/generate_smartpbx_language_menu.py`
   — the committed asset was copied from Kavya and only announces English and Sinhala.
6. **Twilio number** → `https://riviera.taskforceai.tech/voice/incoming`; **Dialog Client
   Connect** → `wss://smartpbx-riviera.taskforceai.tech/ws/v1/smartpbx/media` with header
   `X-Riviera-SmartPBX-Token`.
7. **GHCR package** `ghcr.io/taskforce-ai-dev/riviera`: first publish creates it private —
   link it to this repo and grant write, exactly as was done for Kavya.
8. Add the two health URLs to `.github/workflows/healthcheck.yml` once they answer.

## Run locally
```bash
cp .env.example .env      # fill in API keys
pip install -r requirements.txt
python server.py
pytest tests --timeout=300
```

## Deploy
Riviera is **excluded from `deploy.yml` / `deploy-on-push.yml`** in every mode, like Kavya. It
ships only through the reviewed route: `probe-riviera-image.yml` (read-only probe) →
`build-riviera-image.yml` (publisher) → `scripts/deploy_smartpbx_image.sh` on the VPS, all
documented in `SMARTPBX_RUNBOOK.md`.

## Full context for AI sessions
See **[CLAUDE.md](./CLAUDE.md)** (also exposed as `AGENTS.md`) for architecture, the delta from
Kavya, and gotchas. Kavya's own `CLAUDE.md` change history (v0.1–v0.23) applies to the inherited
code. Part of the [`full-voice-agent`](../) monorepo.
