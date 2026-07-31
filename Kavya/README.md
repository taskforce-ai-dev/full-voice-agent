# Kavya — Hatton Hills Voice Agent

Kavya is an inbound phone agent for **Hatton Hills** — a luxury boutique eco retreat set in an
eight-acre private forest in Sri Lanka's central hill country. She handles reservations and guest
queries over Twilio, answers from a ChromaDB knowledge base, and checks availability / takes
bookings against the **Yanolja PMS**.

> **Hatton Hills is an invented property, used for client demonstrations.** The rate card, the
> room descriptions and the reservations number are all fictional. See
> `ops/hattonhills-pms/RUNBOOK.md`.

- **Property:** single (rebranded from the two-property Mosvold setup on 2026-07-30 — see
  "Single-property mode" below)
- **Languages:** **English only** (since 2026-07-28). Sinhala and Arabic were removed from the IVR;
  their code paths (plus Tamil) are still in `server.py` but no DTMF digit routes to them and
  `/ws/media-stream/{si,ar}` refuses the connection.
- **LLM:** configurable — Claude (default) / OpenAI / Gemini, with tool use
- **Telephony/TTS:** Twilio; English → ConversationRelay + ElevenLabs. (Dormant: Arabic/Tamil →
  Media Streams + ElevenLabs, Sinhala → OpenAI `gpt-4o-mini-tts`)
- **STT:** Google Cloud Speech (Media Streams paths)
- **Server:** FastAPI / uvicorn — host port `127.0.0.1:8000`

## Room types and rates

Five room types. USD per room per night, half board (breakfast and dinner), taxes included.

| Room type | Guests | USD/night | PMS code |
|---|---|---|---|
| Forest Escape Suite | 2 | 700 | `HH-FES` |
| Eco Harmony Suite | 2 | 800 | `HH-ECO` |
| Sunrise Vista Premium Suite | 2 | 950 | `HH-SVP` |
| Mount Luxe Chalet | 5 | 1,150 | `HH-MLX` |
| Mount Monarch Chalet | 5 | 1,400 | `HH-MON` |

Rates are gated behind `DEMO_RATES_ENABLED` (default `true`) and must stay in sync with
`room_types.base_price` in the PMS. **The room-type names are load-bearing**: the PMS schema has no
property column, so the name is the only source of property identity. They must match
`yanolja_service.ROOM_TYPES_BY_PROPERTY`, `tools.ROOM_TYPES_BY_PROPERTY` and `room_types.name`
byte-for-byte, or `_property_of()` returns `""` and the room silently vanishes from availability.

## Single-property mode

Kavya previously served two Mosvold properties whose room names collided, so the code was
deliberately **fail-closed**: the property had to be established before a room could be matched, and
`resolve_property()` returned `None` to force an "ask which property" turn.

Hatton Hills is one property, so that protection is now inert by construction —
`resolve_property()` / `normalise_property()` always resolve, and the `property` tool argument is
optional. **The plumbing is retained end to end** (`tools` → `booking_api` → `yanolja_service`) so a
second property can be reintroduced by restoring the alias maps and letting the resolvers return
`None` again. Don't delete it.

## Key files
| File | Purpose |
|---|---|
| `server.py` | Unified production server (IVR + ConversationRelay + Media Streams) + system prompt |
| `media_stream_server.py` | Standalone Media Streams variant (reference) |
| `tools.py` | Tool defs (Anthropic/OpenAI/Gemini) + dispatch |
| `booking_api.py`, `yanolja_service.py`, `yanolja_client.py` | PMS / booking integration |
| `kpms_service.py` | **Dead code** — nothing imports it (still Mosvold-era) |
| `knowledge_base.py` + `knowledge_docs/` | ChromaDB RAG over hotel info |
| `post_call.py` | Post-call summary → n8n → Google Sheets |
| `ops/hattonhills-pms/` | PMS rebrand SQL, runbook, live verifier |
| `ops/mosvold-pms/` | Superseded — kept for rollback |

## Verify against the live PMS
```bash
/home/dev/full-voice-agent/.venv/bin/python ops/hattonhills-pms/verify_live.py
```
Checks that every Kavya room type exists in the PMS, that no PMS room type is invisible to Kavya,
that no Mosvold names survive, that `base_price` matches the quoted rate, and that availability
returns only Hatton Hills rooms.

## Run locally
```bash
cp .env.example .env      # fill in API keys
pip install -r requirements.txt
python server.py
```

## Deploy
`gh workflow run deploy.yml -f agent=kavya -f ref=<branch> -f mode=fast|build`.
Use `build` when `requirements*.txt` / `Dockerfile` / `docker-compose.yml` changed, or when an
`.env` change needs a container recreate. Auto-deploy triggers only on `main`.

## Full context for AI sessions
See **[CLAUDE.md](./CLAUDE.md)** (also exposed as `AGENTS.md`) for architecture, rules, and gotchas.
Part of the [`full-voice-agent`](../) monorepo.
