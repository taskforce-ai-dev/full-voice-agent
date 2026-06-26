## VPS Access

- Host: `67.207.90.109` (DigitalOcean)
- SSH user: **`root`** — always `ssh root@67.207.90.109`
- To restart any agent container: `ssh root@67.207.90.109 "cd /opt/<agent-dir> && docker compose up -d --force-recreate <container-name>"`
- Dashboard service: `systemctl restart agent-dashboard` (runs at `http://127.0.0.1:3100`)

## Second VPS — Yanolja/eZee Booking Integration (Kavya's data source)

A **second DigitalOcean droplet**, separate from the voice-agent box. It was
undocumented until Jun 2026 (showed up only as billing line
`ubuntu-s-1vcpu-2gb-nyc1-01`, droplet id `562844704`).

- Host: `198.211.114.60` (DigitalOcean, nyc1) — SSH `root@198.211.114.60`
- Purpose: hosts the **Yanolja/eZee booking integration that feeds Kavya**.
  Kavya (on `67.207.90.109`) calls it over HTTPS — do **not** disable these:
  - `https://yanolja.taskforceai.tech/api` → nginx → `127.0.0.1:5000`
    (`yanolja-eezy-api`, PM2) + `127.0.0.1:5080` (`yanolja-eezy-front`)
  - `https://booking-pms.taskforceai.tech` → nginx → `127.0.0.1:3721`
    (`booking-pms`, PM2)
- Apps run under **PM2 as root** (`booking-pms`, `yanolja-eezy-api`,
  `yanolja-eezy-front`); `pm2-root` boot persistence is enabled, so they
  auto-restart on reboot/resize. Run `pm2 save` before any resize.
- The apps are **pure HTTP clients to eZee's API** — no playwright/puppeteer/
  selenium dependency. No headless browser needed.

**Jun 15 2026 cleanup:** the box was an oversized `s-4vcpu-8gb` idling at
~1.5 GB/8 GB. Disabled two abandoned non-Kavya browser experiments serving only
`addons.firefox.taskforceai.tech`: `browser-mirror.service` (broken noVNC crash
loop) and `ai-vision-browser.service` (`:5055`). Vacuumed the journal and cleared
regenerable + `ms-playwright` caches. Backup at `/root/cleanup-backup-20260615/`;
restore with `systemctl enable --now browser-mirror.service ai-vision-browser.service`.
Pending operator actions (DO console): downsize droplet, delete the
`snapshot_before_resizing_20-05-2026` snapshot, raise the account spending limit.

## KB Auto-Structuring Feature (Jun 2026)

Sentinel admin portal → Knowledge Base page has a 4-step AI structuring flow: pick agent + configure VPS target → paste/upload raw content → Claude structures it → publish live to agent.

**Key new endpoints (admin-auth required):**
- `PUT /api/admin/agents/:agentId/kb-target` — set VPS base URL, secret, filename
- `POST /api/admin/knowledge-base/structure` — Claude structures raw text, returns preview
- `POST /api/admin/knowledge-base/publish` — snapshots current KB, saves new, pushes to agent

**Schema additions:** `AgentKbTarget`, `AgentKbDocument`, `AgentKbSnapshot` models; `businessType`/`businessDescription` on `Agent`.

**Required env var:** `ANTHROPIC_API_KEY` in `/opt/agent-dashboard/.env` — structuring returns 501 without it (`ANTHROPIC_MODEL=claude-sonnet-4-6` optional default).

**ChromaDB fix:** All 5 `knowledge_base.py` files now delete-before-upsert in `initialize_kb()` (`collection.delete(where={"source": filename})`) to prevent stale chunk accumulation on KB updates. All running agent containers restarted to activate.

**Current checkout note (Jun 9, 2026):** the local `agent-dashboard/app/api` tree shows the AI structuring route plus per-agent KB GET/PUT with `kb_reload_url`; the `kb-target`, `publish`, and Prisma-model implementation may exist only in the deployed/other checkout. Verify local routes before editing this feature.

## Flico Asterisk SIP Pilot (Jun 2026)

Flico has an additive Asterisk/SIP pilot path. It does **not** replace Twilio
webhooks or ConversationRelay. The pilot stack lives in `asterisk-flico/` and
deploys separately to `/opt/asterisk-flico` on `67.207.90.109`.

Runtime shape:
- Softphone/SIP trunk → Asterisk PJSIP on UDP `5060`
- Asterisk dialplan → language IVR (`Read()` DTMF `1=en`, `2=ta`, `3=si`) →
  `Stasis(flico-sip-agent,en|ta|si)`
- Flico ARI worker → `http://flico-asterisk-pilot:8088/ari` over `taskforceai-net`
- Asterisk External Media → Flico UDP RTP ports `18000-18100`
- Asterisk's own public RTP range is `10000-10199` to avoid colliding with Flico's
  External Media listener ports.
- Flico Media Streams-style pipeline owns STT → LLM/tools/KB → TTS

Flico env controls:
- `ENABLE_ASTERISK_ARI=false` by default
- `ASTERISK_ARI_URL=http://flico-asterisk-pilot:8088/ari`
- `ASTERISK_ARI_PASSWORD` must match `asterisk-flico/config/ari.conf`
- `ASTERISK_RTP_ADVERTISE_HOST=flico-voice-agent`
- Asterisk and Flico both attach to external Docker network `taskforceai-net`;
  ARI is private on that network while SIP/RTP are published.

Do not point Asterisk at `/voice/incoming` (returns TwiML for Twilio). Do not
touch Kavya for this pilot.

Ops:
- `asterisk-flico/sounds/` mounts to Asterisk `custom/`; generate
  `flico-language-menu.ulaw` on the VPS with `python3 ops/generate-language-menu-prompt.py`.
- `asterisk-flico/ops/flico-sip-firewall.sh` is the Docker-aware SIP/RTP allowlist
  helper (UFW alone does not protect Docker-published UDP `5060`/`10000-10199`; use
  DigitalOcean Cloud Firewall and/or `DOCKER-USER` rules). Operator-editable
  allowlist: `asterisk-flico/ops/allowed-sources.txt`. Reapply on boot via
  `flico-sip-firewall.service`; roll back with `flico-sip-firewall-rollback.sh`.
- Flico exposes `GET /asterisk/status` when deployed with the pilot — use during
  Zoiper QA to confirm active call/session/RTP counts return to zero after hangup.
  Keep `/health` backward-compatible.
- Concurrency ceiling bounded by both RTP ranges (~100 Asterisk public RTP / 101
  Flico External Media ports), plus SIP trunk limits and STT/LLM/TTS latency.

## Taskforce AI Website (in-repo, Jun 2026)

The marketing/demo website lives at `Taskforce_AI_Website/`, brought in for
future editing convenience.

- It is **its own git repo** (nested `.git`) → deploy remote
  `git@github.com:ChrysFernando/Taskforce_AI_Website.git` (branch `main`).
  `full-voice-agent` does not track it (it's in the parent `.gitignore` to avoid
  a broken gitlink). See `Taskforce_AI_Website/CLAUDE.md` for app details.
- Edit + deploy: edit in `Taskforce_AI_Website/`, then
  `cd Taskforce_AI_Website && git commit && git push origin main`. Pushing `main`
  ships the live public site (`npm run build` = sitemap → vite → prerender).
  Confirm before pushing — outward-facing.
- Stack: Vite + React + TypeScript (Tailwind, framer-motion, i18n EN/FR/AR),
  Firebase/Supabase, Cloudinary. Demo page: `components/pages/BookDemo.tsx`
  (Hatton Hills / Tanya browser-call demo via Twilio Voice JS SDK).
- graphify: website is in the graph (AST nodes only, no INFERRED edges yet).
  **Do NOT run stock `graphify update .`** — it re-ingests the minified admin SPA
  bundles (`agent-dashboard/public/admin/asset_*.js`, ~2,900 junk nodes). The
  repo `.graphifyignore` excludes those; use `ops/graphify-update-wsl.py` (the
  WSL-safe AST-update wrapper). Run full `/graphify .` for semantic edges.

## graphify — GRAPH-FIRST, ALWAYS

This project has a graphify knowledge graph at `graphify-out/`. It covers all 6 voice
agents (BSL, Kavya, SLIC, Sofia, Flico, HattonHills), agent-dashboard, SinhalaVITS-TTS,
flico-dashboard, and the **Taskforce AI website** (`Taskforce_AI_Website/`). After the
Jun 18, 2026 full LLM rebuild (2,170 nodes) the website was added via AST update, so it
now has **~2,881 nodes, ~4,473 edges, 279 communities**, plus an interactive `graph.html`.
The website's nodes are AST-only (no INFERRED semantic edges yet). **Use it instead of
scanning the codebase.** This is faster and consumes far fewer tokens.

> **Jun 18, 2026 — full rebuild notes:** a source-of-truth audit found the prior graph's
> INFERRED edges had drifted (it claimed Flico Sinhala used self-hosted VITS; actually
> OpenAI `gpt-4o-mini-tts`). A full LLM rebuild was run from current source, so both the
> EXTRACTED (AST) and INFERRED (semantic) layers are now correct. Node count dropped from
> ~5,900 to 2,170 because compiled/minified build artifacts (the Sentinel admin SPA under
> `agent-dashboard/{public,admin-portal}/admin/` and `asset_*.js` bundles) are now
> EXCLUDED — they had been injecting ~5,600 junk nodes. `graphifyy` was upgraded 0.4.21 →
> 0.7.4. NOTE: 0.7.4 has a WSL/UNC bug — `os.path.normcase` lowercases the case-sensitive
> `\\wsl.localhost\` mount path and breaks `cache.file_hash`; the rebuild worked around it
> with an in-process monkeypatch on `graphify.cache._normalize_path` + `parallel=False`.
> If you re-run `/graphify .` from this Windows UNC path, apply the same patch or run from
> a native Linux path.
>
> **Jun 18, 2026 — website added + update gotchas:** stock `graphify update .` does NOT
> work cleanly here. (1) The same WSL/UNC normcase bug aborts it (`file_hash requires a
> file`). (2) No exclusion filter, so it re-ingests the minified admin SPA bundles
> (`agent-dashboard/public/admin/asset_*.js`) — ~2,900 junk nodes past the 5,000-node
> HTML-viz limit (graph.html then stops generating). Both solved: repo-root
> **`.graphifyignore`** excludes the admin bundles + `__MACOSX`, and
> **`ops/graphify-update-wsl.py`** is a WSL-safe wrapper (normcase patch + sequential
> extraction). Use that wrapper, not bare `graphify update .`.

MANDATORY at the start of EVERY session, before any code exploration:
1. Read `graphify-out/GRAPH_REPORT.md` first — it gives god nodes, communities, and
   architecture in one read. Do NOT grep or read source files to "get oriented".
2. To answer any "how/where/what/why" question about the code, query the graph BEFORE
   touching raw files:
   - `graphify query "<question>"`        — broad context, what connects to what
   - `graphify path "<A>" "<B>"`          — how concept A reaches concept B
   - `graphify explain "<concept>"`       — everything connected to one node
   These traverse EXTRACTED + INFERRED edges — far cheaper than reading whole files.
3. Only open raw source files when the graph points you to a specific file/symbol and
   you need exact line-level detail to edit it. Never read files just to understand
   structure — the graph already has that.

Codex note: when the user types `/graphify`, use the `graphify` skill before doing
anything else.

After modifying any code file in a session, run **`python ops/graphify-update-wsl.py`**
(repo root, with the Windows store Python that has graphify) to keep the graph current —
AST-only, no API cost. Do NOT run bare `graphify update .` on this WSL/UNC checkout: it
aborts on the normcase bug and re-bloats the graph with admin build artifacts. Add
`--force` when the node count legitimately drops.

Rebuild the full graph (`/graphify .`) only when large new areas of the codebase appear
that `graphify update` cannot pick up.

## claude-mem for Claude Code + Codex

Claude Code and Codex both use the same claude-mem data store at
`/mnt/c/Users/mrdar/.claude-mem` (`/home/thiva/.claude-mem` is a symlink). Do not
delete or recreate this directory: it holds the historical SQLite database,
WAL/SHM files, Chroma files, logs, and backups.

Current reliability setting: `CLAUDE_MEM_CHROMA_ENABLED=false` in
`/mnt/c/Users/mrdar/.claude-mem/settings.json` — preserves all old data but keeps
search on the fast SQLite/FTS path (local Chroma MCP startup was timing out).

> **Jun 16 2026:** re-pointed Codex's claude-mem MCP runtime from the stale
> `13.4.2` cache folder to `13.6.1` (current cached version) in
> `/home/thiva/.codex/config.toml`, so Codex and Claude Code run the same runtime
> against the shared store. Old caches (`13.4.0`–`13.6.1`) retained; if pruned,
> re-point the `args` path. Backup of prior Codex config:
> `~/.codex/config.toml.bak.20260616`.

Use `mem-search` for previous-session lookup (search → timeline → get_observations).

## Claude Code / Codex Sync

This repo is maintained with both Claude Code and Codex. Keep `CLAUDE.md` and
`AGENTS.md` in sync for shared project guidance. When either assistant adds or
changes meaningful repo context, update both files where relevant, especially:

- New files, tools, MCP servers, skills, or workflow expectations.
- Deployment, environment, or operational changes.
- Any known limitations or follow-up work that affects future Claude Code or
  Codex sessions.

Codex-side setup added so far:
- `AGENTS.md` was added for Codex with graphify-first project guidance.
- Codex user skills were installed under `/home/thiva/.codex/skills`, including
  `graphify`, `ui-ux-pro-max`, and the portable `claude-mem` skills synced from
  claude-mem plugin cache `13.4.2`.
- Codex MCP currently has `claude-mem` enabled from the cached Claude plugin
  runtime `13.6.1` (re-pointed Jun 16 2026 from `13.4.2`). Vercel MCP was tested
  but removed because its OAuth redirect was rejected in this environment.
