# full-voice-agent

A **private monorepo** of multilingual AI voice agents for [Taskforce AI](https://taskforceai.tech).
Each agent is an inbound phone assistant built on the same stack — **Twilio** telephony,
**FastAPI / Python 3.11**, a **configurable LLM** (Anthropic Claude by default, with OpenAI and
Gemini switchable via `LLM_PROVIDER`), and a **ChromaDB** RAG knowledge base — but wears a different
persona, business, language set, and (where relevant) booking/transaction tools. Agents share a
common shape: a `<ConversationRelay>` path for English (and other Twilio-supported locales) plus a
**Media Streams** path (Google/Azure STT + ElevenLabs/OpenAI TTS) for languages ConversationRelay
can't handle (Sinhala, Tamil, Arabic). Each ships its own `Dockerfile`, `docker-compose.yml`,
`nginx.conf`, and `deploy.sh`, and is deployed independently to a DigitalOcean VPS.

> Repo: `github.com/thiva2k/full-voice-agent`

## Agents

| Agent | Persona | Business | Languages | Type | README |
|---|---|---|---|---|---|
| **Kavya** | Kavya | Treehouse Chalets (hotel, Belihuloya) | EN / Arabic / Sinhala (Tamil coded, unlisted) | Transactional (eZee PMS bookings via n8n) | [Kavya/README.md](./Kavya/README.md) |
| **HattonHills** | Kavya/Tanya | Hatton Hills (hotel) | EN / Sinhala / Tamil (+ Arabic, Russian web-demo) | Transactional (PMS bookings) | [HattonHills/README.md](./HattonHills/README.md) |
| **Flico** | Fiona | Flico (flico.lk electronics retailer) | EN / Tamil / Sinhala | Informational (KB only) | [Flico Agent/README.md](./Flico%20Agent/README.md) |
| **Sofia** | Sofia | BuyAbans (Abans retail) | EN / Tamil | Informational (KB only) | [Sofia Agent/README.md](./Sofia%20Agent/README.md) |
| **BSL** | Generic virtual assistant | Bank of Sri Lanka (banking) | English only | Transactional (mocked banking tools) | [BSL Agent/README.md](./BSL%20Agent/README.md) |
| **SLIC** | Nimali | Sri Lanka Insurance (accident hotline) | English only | Transactional (mocked claim dispatch + real SMS) | [SLIC Agent/README.md](./SLIC%20Agent/README.md) |
| **Kitchened** | — | Kitchen & Co. (commercial kitchen/bakery equipment) | see `Kitchened/server.py` | Informational (KB only) | _no folder README yet_ |
| **WorldOfRefrigerators** | — | World Of Refrigerators (refrigeration sales) | see folder `server.py` | Informational (KB only) | _no folder README yet_ |

Each agent folder also contains a `CLAUDE.md` with the full architecture, design decisions, change
history, and operational gotchas — start there for any deep work on an agent.

## Repo structure

```
full-voice-agent/
├── Kavya/                  # Treehouse Chalets hotel agent (transactional)
├── HattonHills/            # Hatton Hills hotel agent (same codebase as Kavya)
├── Flico Agent/            # Fiona — Flico electronics (informational)
├── Sofia Agent/            # Sofia — BuyAbans retail (informational)
├── BSL Agent/              # Bank of Sri Lanka banking demo (English, mocked)
├── SLIC Agent/             # Nimali — SLIC accident hotline (English, mocked + SMS)
├── Kitchened/              # Kitchen & Co. equipment agent (informational)
├── WorldOfRefrigerators/   # World Of Refrigerators agent (informational)
│
├── asterisk-flico/         # Additive Asterisk/SIP pilot for Flico (off by default)
├── flico-dashboard/        # Flico dashboard
├── telephony/              # Shared telephony helpers / configs
├── ops/                    # Repo tooling — sync-agent-docs.sh, graphify-update-wsl.py
├── docs/                   # Project documentation
├── graphify-out/           # graphify knowledge graph (GRAPH_REPORT.md + graph.html)
├── knowledge_docs/         # Shared/source knowledge documents
│
├── CLAUDE.md / AGENTS.md   # Project-wide AI context (kept in sync)
├── .graphifyignore         # Excludes minified admin bundles from the graph
└── .gitignore
```

Not committed here — these live in their own repos or are too large, and are git-ignored:
- **agent-dashboard/** → `ChrysFernando/Client-Portal` (Sentinel admin portal)
- **Taskforce_AI_Website/** → `ChrysFernando/Taskforce_AI_Website` (marketing/demo site)
- **SinhalaVITS-TTS-M2/** → ~8 GB HuggingFace model download (never commit)

## Working on an agent

Each agent is a self-contained Python project. From the repo root:

```bash
cd "Kavya"                  # or "Flico Agent", "BSL Agent", etc.
cp .env.example .env        # then fill in API keys (Anthropic/OpenAI, ElevenLabs, Twilio, …)
pip install -r requirements.txt
python server.py            # uvicorn loads server:app
```

Minimum to test most agents locally in text-only mode: `ANTHROPIC_API_KEY` + `LLM_PROVIDER=claude`.
Add `ELEVENLABS_API_KEY` + `ELEVENLABS_VOICE_ID` for the local voice smoke tests
(`python test_voice_elevenlabs.py`, where present). See each agent's `README.md` / `CLAUDE.md` for
its host port and the full env var list.

## Deploy

Each agent deploys independently to the DigitalOcean VPS at **`67.207.90.109`** (SSH `root@`),
running as a Docker container behind nginx (SSL termination, WebSocket upgrade, rate limiting), each
on its own host port (e.g. Kavya `8000`, Sofia/SLIC `8001`, BSL `8002`, Flico `8003`).

```bash
cd "<Agent Folder>"
./deploy.sh setup     # first-time VPS provisioning
./deploy.sh deploy    # build + push code (rebuilds the image — slow)
./deploy.sh logs      # tail remote logs
./deploy.sh status    # health check (GET /health)
```

For Python-only changes some agents prefer `scp` + `docker restart` over a full image rebuild — see
the agent's `CLAUDE.md` (BSL documents this explicitly). Note `docker compose restart` does **not**
re-read `.env`; use `docker compose up -d --force-recreate <container>` after env changes.

Per-agent **baseline tags** mark the first consolidated release of each:
`bsl-v1.0.0`, `flico-v1.0.0`, `hatton-v1.0.0`, `slic-v1.0.0`, `sofia-v1.0.0`, `kavya-v1.0.0`.

A separate DigitalOcean droplet (`198.211.114.60`) hosts the **Yanolja/eZee booking integration**
that feeds the hotel agents over HTTPS. See the root `CLAUDE.md` for details — do not disable it.

## Conventions

**Secrets — never commit them.** Every agent keeps its real credentials in a git-ignored `.env`;
only `.env.example` (a key-less template) is committed. `.gitignore` also blocks GCP service-account
JSON, credential drops, ChromaDB stores, model caches, and audio temp files. There are
`pre-commit` hooks and a `gitleaks` config (`.gitleaks.toml`) to catch accidental secret commits.

**AI context files.** Each agent's source of truth for AI assistants is its `CLAUDE.md`; this is
mirrored to an `AGENTS.md` (for Codex) via `ops/sync-agent-docs.sh`. Keep the root `CLAUDE.md` and
`AGENTS.md` in sync when you change shared project guidance.

**graphify-first.** The repo has a graphify knowledge graph in `graphify-out/`. Before exploring
code, read `graphify-out/GRAPH_REPORT.md` and query the graph (`graphify query/path/explain`)
rather than scanning files — it's faster and far cheaper in tokens. After modifying code, refresh
the graph with `python ops/graphify-update-wsl.py` (the WSL/UNC-safe wrapper). Do **not** run bare
`graphify update .` on this checkout — see the root `CLAUDE.md` for why.

## Security

This is a **private repository**. Do not make it public or share its contents. If you ever find a
secret committed (API key, token, service-account JSON, password), treat it as compromised:
**scrub it from the working tree (and history if needed) and rotate the credential immediately.**
Production secrets belong only in the per-agent `.env` files on the VPS, never in git.
