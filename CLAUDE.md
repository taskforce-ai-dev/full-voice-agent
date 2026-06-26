## VPS Access

- Host: `67.207.90.109` (DigitalOcean)
- SSH user: **`root`** — always `ssh root@67.207.90.109`
- To restart any agent container: `ssh root@67.207.90.109 "cd /opt/<agent-dir> && docker compose up -d --force-recreate <container-name>"`
- Dashboard service: `systemctl restart agent-dashboard` (runs at `http://127.0.0.1:3100`)

## Second VPS — Yanolja/eZee Booking Integration (Kavya's data source)

There is a **second DigitalOcean droplet** separate from the voice-agent box.
It was undocumented until Jun 2026 (showed up only as a billing line named
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
  selenium dependency. They do not need a headless browser.

**Jun 15 2026 cleanup (cost/cruft reduction):** the box was an oversized
`s-4vcpu-8gb` ($48/mo) idling at ~1.5 GB/8 GB. Disabled two abandoned,
non-Kavya browser experiments that only served `addons.firefox.taskforceai.tech`
(8 scanner hits ever): `browser-mirror.service` (broken noVNC crash loop) and
`ai-vision-browser.service` (Claude screenshot-browse UI on `:5055`). Vacuumed
the systemd journal (−1.8 GB) and cleared regenerable + `ms-playwright` caches.
Config/unit/pm2 backup saved at `/root/cleanup-backup-20260615/`; restore the
services with `systemctl enable --now browser-mirror.service ai-vision-browser.service`.
Pending operator actions (DO console): downsize the droplet, delete the
`snapshot_before_resizing_20-05-2026` snapshot (~$2/mo), and raise the account
spending limit (a low limit triggered a mid-month "account paused" email).

## KB Auto-Structuring Feature (Jun 2026)

The Sentinel admin portal (`/admin/` → Knowledge Base page) now has an AI-powered KB structuring flow:

**How it works:**
1. Admin selects an agent, configures its VPS target (base URL + KB_RELOAD_SECRET + filename).
2. Paste raw text or upload a PDF — Claude infers the business vertical and rewrites it as a retrieval-optimized KB document (300–500-char paragraphs, explicit entity names, blank-line separation).
3. Preview + edit the structured doc, compare vs current KB, then Publish → POSTs live to the agent's `/kb-reload` endpoint.

**New API endpoints (all under `/api/admin/*`, require admin session):**
- `GET  /api/admin/agents/:agentId/kb-target` — get VPS target config
- `PUT  /api/admin/agents/:agentId/kb-target` — create/update VPS target (baseUrl, secret, filename)
- `GET  /api/admin/agents/:agentId/kb-document` — get current structured KB + last 5 snapshots
- `POST /api/admin/knowledge-base/structure` — preview-only: Claude structures raw text → `{structuredContent, businessType, sections, warnings}`
- `POST /api/admin/knowledge-base/publish` — snapshots current doc, persists new content, POSTs to agent's `/kb-reload`

**New Prisma models:** `AgentKbTarget`, `AgentKbDocument`, `AgentKbSnapshot`. New fields on `Agent`: `businessType`, `businessDescription`.

**New env vars needed in `/opt/agent-dashboard/.env`:**
- `ANTHROPIC_API_KEY=sk-ant-...` ← **must be added manually; structuring returns 501 without it**
- `ANTHROPIC_MODEL=claude-sonnet-4-6` (optional, this is the default)

**ChromaDB stale-chunk fix (deployed Jun 2026):**
All 5 `knowledge_base.py` files now run `collection.delete(where={"source": filename})` before each upsert inside `initialize_kb()`. This prevents old chunks from accumulating in ChromaDB when KB content is updated. All 4 running agent containers were restarted to activate the fix.

**Current checkout note (Jun 9, 2026):** the local `agent-dashboard/app/api`
tree currently shows the AI structuring route plus per-agent KB GET/PUT with
`kb_reload_url`; the `kb-target`, `publish`, and Prisma-model implementation
described above may exist only in the deployed/other checkout. Verify local
routes before editing this feature.

## Flico Asterisk SIP Pilot (Jun 2026)

Flico now has an additive Asterisk/SIP pilot path. It does **not** replace
Twilio webhooks or ConversationRelay. The pilot stack lives in `asterisk-flico/`
and is intended to deploy separately to `/opt/asterisk-flico` on `67.207.90.109`.

Runtime shape:
- Softphone/SIP trunk → Asterisk PJSIP on UDP `5060`
- Asterisk dialplan → language IVR → `Stasis(flico-sip-agent,en|ta|si)`
- Flico ARI worker → `http://flico-asterisk-pilot:8088/ari` over `taskforceai-net`
- Asterisk External Media → Flico UDP RTP ports `18000-18100`
- Asterisk's own public RTP media range is `10000-10199` to avoid colliding with
  Flico's External Media listener ports.
- Flico Media Streams-style pipeline owns STT → LLM/tools/KB → TTS

Flico env controls:
- `ENABLE_ASTERISK_ARI=false` by default
- `ASTERISK_ARI_URL=http://flico-asterisk-pilot:8088/ari`
- `ASTERISK_ARI_PASSWORD` must match `asterisk-flico/config/ari.conf`
- `ASTERISK_RTP_ADVERTISE_HOST=flico-voice-agent`
- Asterisk and Flico must both attach to the external Docker network
  `taskforceai-net`; ARI is private on that network while SIP/RTP are published.

Do not point Asterisk at `/voice/incoming`; that route returns TwiML for Twilio.
Do not touch Kavya for this pilot.

Day 2 additions:
- `asterisk-flico/config/extensions.conf` now sends both `7001` and generic DID
  ingress through `flico-language-ivr`, using `Read()` for DTMF `1=en`,
  `2=ta`, `3=si` before ARI handoff.
- `asterisk-flico/sounds/` is mounted to Asterisk `custom/`; generate
  `flico-language-menu.ulaw` on the VPS with
  `python3 ops/generate-language-menu-prompt.py`.
- `asterisk-flico/ops/flico-sip-firewall.sh` is the Docker-aware SIP/RTP
  allowlist helper. UFW alone does not protect Docker-published UDP `5060` and
  `10000-10199`; use DigitalOcean Cloud Firewall and/or `DOCKER-USER` rules.
- `asterisk-flico/ops/allowed-sources.txt` is the operator-editable allowlist;
  current pilot sources are `111.223.176.194`, `104.28.66.29`, and
  `175.157.131.255`.
- `asterisk-flico/ops/flico-sip-firewall.service` reapplies the SIP/RTP
  allowlist after `network-online.target` and `docker.service`. Install it to
  `/etc/systemd/system/flico-sip-firewall.service`, then run
  `systemctl enable --now flico-sip-firewall`.
- Roll firewall rules back with
  `/opt/asterisk-flico/ops/flico-sip-firewall-rollback.sh`; it removes managed
  `DOCKER-USER` rules, `FLICO-SIP-RTP`, and the `flico_sip_allowed` ipset.
- Flico exposes `GET /asterisk/status` when deployed with the Asterisk pilot;
  use it during Zoiper QA to confirm active call/session/RTP counts return to
  zero after hangup. Keep `/health` backward-compatible.
- Current pilot concurrency ceiling is bounded by both RTP ranges: Asterisk
  public RTP `10000-10199` gives ~100 calls, while Flico External Media
  `18000-18100` gives 101 allocated media ports. The real production ceiling
  also depends on SIP trunk channel limits and STT/LLM/TTS latency.

## Taskforce AI Website (in-repo, Jun 2026)

The marketing/demo website now lives in this repo at `Taskforce_AI_Website/`,
brought in for future editing convenience.

- **It is its own git repo** (nested `.git`), wired to its deploy remote
  `git@github.com:ChrysFernando/Taskforce_AI_Website.git` (branch `main`).
  `full-voice-agent` does **not** track it — `Taskforce_AI_Website/` is in the
  parent `.gitignore` to avoid recording a broken gitlink. See that folder's own
  `CLAUDE.md` for app-level details.
- **Edit + deploy:** edit files in `Taskforce_AI_Website/`, then
  `cd Taskforce_AI_Website && git commit && git push origin main`. Pushing `main`
  ships to the live public site (build = `npm run build`: sitemap → vite →
  prerender). Confirm before pushing — it's outward-facing.
- **Stack:** Vite + React + TypeScript (Tailwind, framer-motion, i18n EN/FR/AR),
  Firebase/Supabase, Cloudinary. The demo page is
  `components/pages/BookDemo.tsx` (Hatton Hills / Tanya browser-call demo via
  Twilio Voice JS SDK).
- **graphify note:** the website is in the graph (AST nodes only — no INFERRED
  semantic edges for it yet; run a full `/graphify .` if those are needed).
  **Do NOT run stock `graphify update .`** — it has no exclusion filter and
  re-ingests the minified admin SPA bundles (`agent-dashboard/public/admin/
  asset_*.js`), bloating the graph by ~2,900 junk nodes and blowing past the
  5,000-node HTML-viz limit. The repo's `.graphifyignore` now excludes those,
  and `ops/graphify-update-wsl.py` is the WSL-safe AST-update wrapper (handles
  the UNC normcase + multiprocessing bugs). See the graphify section below.

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
> file`). (2) It has no exclusion filter, so it re-ingests the minified admin SPA bundles
> (`agent-dashboard/public/admin/asset_*.js`) — ~2,900 junk nodes that blow past the
> 5,000-node HTML-viz limit (graph.html then stops generating). Both are now solved:
> a repo-root **`.graphifyignore`** excludes the admin bundles + `__MACOSX`, and
> **`ops/graphify-update-wsl.py`** is a WSL-safe wrapper (normcase patch + sequential
> extraction) that runs the AST update correctly. Use that wrapper, not bare
> `graphify update .`.

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

After modifying any code file in a session, run **`python ops/graphify-update-wsl.py`**
(from the repo root, with the Windows store Python that has graphify) to keep the graph
current — AST-only, no API cost. Do NOT run bare `graphify update .` on this WSL/UNC
checkout: it aborts on the normcase bug and re-bloats the graph with admin build
artifacts. Add `--force` when the node count legitimately drops.

Rebuild the full graph (`/graphify .`) only when large new areas of the codebase appear
that `graphify update` cannot pick up.

## claude-mem for Claude Code + Codex

Claude Code and Codex both use the same claude-mem data store at
`/mnt/c/Users/mrdar/.claude-mem` (`/home/thiva/.claude-mem` is a symlink). Do not
delete or recreate this directory: it contains the historical SQLite database,
WAL/SHM files, Chroma files, logs, and backups.

Current reliability setting: `CLAUDE_MEM_CHROMA_ENABLED=false` in
`/mnt/c/Users/mrdar/.claude-mem/settings.json`. This preserves all old data but
keeps search on the fast SQLite/FTS path because local Chroma MCP startup was
timing out.

Codex MCP is registered as `claude-mem` using the cached Claude plugin runtime:
`/mnt/c/Users/mrdar/.claude/plugins/cache/thedotmack/claude-mem/13.6.1/scripts/mcp-server.cjs`
with `CLAUDE_CONFIG_DIR=/mnt/c/Users/mrdar/.claude`,
`CLAUDE_MEM_CHROMA_ENABLED=false`, and `CLAUDE_MEM_SEMANTIC_INJECT=false`.

> **Jun 16 2026:** re-pointed Codex's claude-mem MCP runtime from the stale
> `13.4.2` cache folder to `13.6.1` (the current cached version) in
> `/home/thiva/.codex/config.toml`, so Codex and Claude Code now run the same
> claude-mem runtime against the shared `/mnt/c/Users/mrdar/.claude-mem` store.
> Old cache versions (`13.4.0`–`13.6.1`) are retained; if a future cleanup prunes
> them, re-point this `args` path to whatever version remains. Backup of the prior
> Codex config: `~/.codex/config.toml.bak.20260616`. (The Codex *skills* below were
> NOT re-synced and still originate from the `13.4.2` cache.)

Codex skills under `/home/thiva/.codex/skills` include the current claude-mem
skills from plugin cache `13.4.2` plus Codex-specific skills such as `graphify`
and `ui-ux-pro-max`. Use `mem-search` for previous-session lookup and follow its
search → timeline → get_observations workflow.

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
  runtime `13.6.1` (re-pointed Jun 16 2026 from the stale `13.4.2`). Vercel MCP
  was tested but removed because its OAuth redirect was rejected in this
  environment.
