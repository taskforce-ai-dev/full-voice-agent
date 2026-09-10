# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with
code in this repository directory.

## What This Is

**Vidya** is a multilingual (English, Sinhala, Arabic, Russian) AI voice agent for **Horizon
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

**INQUIRY-ONLY — a hard invariant, not a config state.** Vidya takes NO
bookings, enrolments, payments, or student registrations, and cannot look up
individual student records. The cloned booking/PMS modules (`booking_api.py`,
`yanolja_client.py`, `yanolja_service.py`, `kpms_client.py`, `kpms_service.py`,
`post_call.py`, `dashboard_client.py`) are retained present-but-inert for fleet
consistency, but the enforcement does NOT rely on a credential being absent:
- **Tools:** `tools.py` `get_tools*()` are hard-wired to return `[]`. There is no
  env var or PMS credential that can expose the (hotel-shaped) booking tools — an
  inherited Yanolja key changes nothing. Re-enabling tools needs a code change.
- **Egress fail-closed:** all post-call egress (LLM extraction, n8n, dashboard —
  including the caller-metadata `call.started` event) ships nothing unless
  `HORIZON_POST_CALL_EGRESS` is set to EXACTLY `enabled`. Any other value, a typo,
  or an inherited dashboard/n8n config stays silent.
- **WS ingress auth:** `/ws/conversation` and `/ws/media-stream/{lang}` require a
  short-lived HMAC ticket minted into the TwiML wss URL (secret: `WS_TICKET_SECRET`
  or the Twilio auth token), so a public `/ws/` cannot be driven by arbitrary
  clients to burn LLM/STT/TTS.
- **KB reload** validates the caller-supplied `filename` (bare basename, allowed
  extension, contained in `knowledge_docs/`, no symlink/traversal).

## The stack

Vidya offers **four demo languages — English, Sinhala, Arabic, Russian** — and
mirrors **Kavya's Dialog (SmartPBX) line's language stack** where one exists
(English + Sinhala): Claude+ElevenLabs for English, Gemini brain + Gemini TTS for
Sinhala. The only difference from the Dialog line is transport — a website demo
is a Twilio **browser** call, so audio rides Twilio (ConversationRelay for
en/ru, Media Streams for ar/si) rather than Dialog SIP.

- **English (`en`):** ConversationRelay — Claude brain (`LLM_PROVIDER=claude`) +
  ElevenLabs voice (`CR_VOICE_EN`); Twilio owns STT.
- **Russian (`ru`):** ConversationRelay — Claude brain + ElevenLabs voice
  (`CR_VOICE_RU`, `ru-RU`); Twilio owns STT. (No Dialog equivalent; uses the
  HattonHills base path.)
- **Arabic (`ar`):** Media Streams — Claude brain + Azure STT (`ar-SA`) +
  ElevenLabs Arabic voice (`ELEVENLABS_VOICE_ID_AR`). (No Dialog equivalent.)
- **Sinhala (`si`):** Media Streams — **Gemini brain** (`SI_GEMINI_MODEL`
  default `gemini-3.7-flash`, forced via `SI_LLM_PROVIDER=gemini` regardless of
  the global `LLM_PROVIDER`; `_run_llm_gemini(model=SI_GEMINI_MODEL)`) + **Gemini
  TTS** (`GEMINI_TTS_MODEL` `gemini-3.1-flash-tts-preview`, voice `Vindemiatrix`,
  via `MediaStreamSession._tts_gemini` — Interactions API returns 24 kHz PCM,
  downsampled to 8 kHz mulaw like `_tts_openai`) + Azure STT (`si-LK`). This is
  the Kavya Dialog stack, ported over. **⚠ Preview-model quota:** ~100
  Gemini-TTS requests/day; on any failure (quota/error/no audio, or a missing
  `GEMINI_API_KEY`) Sinhala degrades to Claude brain + OpenAI TTS (`sage`) so the
  call still works.
- **Knowledge base:** ChromaDB RAG over `knowledge_docs/`.
- `ta` code path from the HattonHills base remains present but is **not offered**
  for this demo (en + si + ar + ru only).

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
- **No GCP creds:** unlike the HattonHills base, `docker-compose.yml` has **no**
  `GOOGLE_APPLICATION_CREDENTIALS` env and **no** gcp-credentials bind mount —
  Horizon's Sinhala/Arabic STT is Azure (`STT_PROVIDER=azure`), so it never needs
  a Google service-account JSON, and nothing depends on that file existing on the
  VPS.

## Environment

Real secrets live only in **`/opt/horizon/.env`** on the VPS (never committed).
`.env.example` documents the full inquiry-only key list:
- English/Russian brain: `LLM_PROVIDER=claude`, `ANTHROPIC_API_KEY`, `CLAUDE_MODEL`
- English/Russian voice: `ELEVENLABS_API_KEY`, `CR_VOICE_EN`, `CR_VOICE_RU`,
  `ELEVENLABS_VOICE_ID`
- Arabic voice: `ELEVENLABS_VOICE_ID_AR`; Arabic STT via `STT_PROVIDER=azure`
- Sinhala brain + voice (Gemini): `GEMINI_API_KEY`, `SI_LLM_PROVIDER=gemini`,
  `SI_GEMINI_MODEL=gemini-3.7-flash`,
  `GEMINI_TTS_MODEL=gemini-3.1-flash-tts-preview`,
  `GEMINI_TTS_VOICE=Vindemiatrix`, `GEMINI_TTS_TIMEOUT_SECONDS`
- Sinhala voice fallback (used only if Gemini TTS is unavailable):
  `OPENAI_API_KEY`, `OPENAI_TTS_MODEL=gpt-4o-mini-tts`, `OPENAI_TTS_VOICE=sage`,
  `OPENAI_TTS_INSTRUCTIONS`
- STT: `STT_PROVIDER=azure`, `AZURE_SPEECH_KEY`, `AZURE_SPEECH_REGION=southeastasia`
- Telephony: `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`
- KB reload: `KB_RELOAD_SECRET`

`GEMINI_API_KEY` is **required** for the Sinhala Dialog stack. No PMS/booking or
Google STT keys are configured, but note inquiry-only does NOT depend on that —
it is enforced in code (see the invariant list above). To enable real post-call
egress in a production instance, set `HORIZON_POST_CALL_EGRESS=enabled`
(fail-closed: any other value ships nothing).

## Caller-facing persona

`_build_system_prompt(lang)` in `server.py` builds Vidya's inquiry persona
(per-language rules): answer only from the reference KB, never invent fees or
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
