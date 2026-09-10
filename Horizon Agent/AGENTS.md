# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with
code in this repository directory.

## What This Is

**Vidya** is a bilingual (English + Sinhala) AI voice agent for **Horizon
Airline & Aviation Academy**, a **de-identified aviation-college INQUIRY demo**.
She answers callers' questions about the academy — courses, course fees, entry
requirements, course durations, class schedules, campus, and how to apply —
grounded in a ChromaDB RAG knowledge base (`knowledge_docs/horizon_info.txt`).

This is a **Twilio web-demo agent**: it is reached through the shared
**hattonhills.taskforceai.tech** Book-a-Demo router (agent id **`horizon`**),
not a dedicated phone number. The website's demo card mints a token for the one
shared Twilio TwiML app whose voiceUrl is HattonHills' `/voice/demo-incoming`;
the site passes `agent=horizon`, and HattonHills `<Redirect>`s the call to this
agent's own `/voice/demo-incoming`. That routing depends on a
`DEMO_AGENT_HOSTS["horizon"]` entry in HattonHills (added separately) and on
DNS/TLS for **horizon.taskforceai.tech**.

**INQUIRY-ONLY.** Vidya takes NO bookings, enrolments, payments, or student
registrations, and cannot look up individual student records. This is enforced
**by configuration, not by deleting code** (same pattern as the Hutch agent): the
cloned booking/PMS modules (`booking_api.py`, `yanolja_client.py`,
`yanolja_service.py`, `kpms_client.py`, `kpms_service.py`, `post_call.py`,
`dashboard_client.py`) are still present for fleet consistency, but are **inert**
— with no PMS/Yanolja credentials `get_tools()` returns `[]`, so the model has no
booking tool to call, and with no dashboard/n8n creds those paths never fire.

## The stack

- **Brain (both languages):** Anthropic **Claude** (`CLAUDE_MODEL`, default
  `claude-sonnet-4-6`), `LLM_PROVIDER=claude`. KB-grounded conversation; no tools
  are wired in the inquiry-only configuration.
- **English (Press 1 / web-demo `en`):** Twilio **ConversationRelay** — Twilio
  owns STT + TTS; TTS voice is **ElevenLabs** (`CR_VOICE_EN`).
- **Sinhala (web-demo `si`):** Twilio **Media Streams** — **OpenAI TTS**
  (`gpt-4o-mini-tts`, voice `sage`) + **Azure Speech STT** (`si-LK`). Mirrors the
  Flico Sinhala pipeline.
- **Knowledge base:** ChromaDB RAG over `knowledge_docs/`.
- `ar` / `ru` code paths from the HattonHills base remain present but are **not
  offered** for this demo (en + si only).

## Runtime shape

- **Container:** `horizon-voice-agent`. Loopback port **`127.0.0.1:8050`**
  (host) → `8000` (container). nginx (`nginx.conf`, domain
  `horizon.taskforceai.tech`) terminates TLS and proxies to `127.0.0.1:8050`,
  keeping the WSS `/ws/` upgrade and `/voice/*` rate-limit blocks.
- **Image-mode deploy** (mirrors Kitchened / WorldOfRefrigerators):
  `docker-compose.yml` has **no `build:`** — it pulls
  `ghcr.io/taskforce-ai-dev/horizon:${IMAGE_TAG:-latest}`. CI builds and pushes
  the image on the GitHub runner; the VPS only pulls. Roll back by hand with
  `IMAGE_TAG=<sha> docker compose pull && IMAGE_TAG=<sha> docker compose up -d`.
- **GCP creds:** `docker-compose.yml` mounts
  `./full-voice-agent-a8a245fb37cb.json` → `/app/gcp-credentials.json:ro` and
  sets `GOOGLE_APPLICATION_CREDENTIALS` (present for the shared STT plumbing;
  Sinhala STT actually uses Azure).

## Environment

Real secrets live only in **`/opt/horizon/.env`** on the VPS (never committed).
`.env.example` documents the full inquiry-only key list:
- LLM: `LLM_PROVIDER=claude`, `ANTHROPIC_API_KEY`, `CLAUDE_MODEL`
- English voice: `ELEVENLABS_API_KEY`, `CR_VOICE_EN`, `ELEVENLABS_VOICE_ID`
- Sinhala voice: `OPENAI_API_KEY`, `OPENAI_TTS_MODEL=gpt-4o-mini-tts`,
  `OPENAI_TTS_VOICE=sage`, `OPENAI_TTS_INSTRUCTIONS`
- Sinhala STT: `STT_PROVIDER=azure`, `AZURE_SPEECH_KEY`,
  `AZURE_SPEECH_REGION=southeastasia`
- Telephony: `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`
- KB reload: `KB_RELOAD_SECRET`

No PMS/booking keys, no Gemini key, no Google STT key are configured — that is
what keeps the demo inquiry-only.

## Caller-facing persona

`_build_system_prompt(lang)` in `server.py` builds Vidya's inquiry persona
(en + si language rules): answer only from the reference KB, never invent fees or
dates, no bookings/payments, phone-call voice rules. Greetings/re-prompts live in
`LANGUAGE_CONFIGS`, `MEDIA_STREAM_WELCOME`, and `REPROMPT_MESSAGES`; the Sinhala
TTS persona is `OPENAI_TTS_INSTRUCTIONS`.

- EN greeting: "Welcome to Horizon Airline and Aviation Academy. I'm Vidya. How
  can I help you today?"
- SI greeting: "ආයුබෝවන්! Horizon Airline and Aviation Academy වෙත සාදරයෙන්
  පිළිගනිමු. මම විද්‍යා. මට ඔබට කෙසේ උදව් කළ හැකිද?"

## Knowledge base

`knowledge_docs/` contains a single file, `horizon_info.txt`. `initialize_kb()`
in `knowledge_base.py` ingests every file in the directory, so keep only the
Horizon doc there. The `/kb-reload` endpoint and `reload_kb_from_content()`
default to `horizon_info.txt`.

## graphify — GRAPH-FIRST, ALWAYS

This sub-project is part of the shared graphify knowledge graph at
`../graphify-out/` (project root). See the root `CLAUDE.md` for the full
graphify workflow. This agent is new as of this scaffold; run the appropriate
`graphify update` variant (see root `CLAUDE.md`) after this change lands.
