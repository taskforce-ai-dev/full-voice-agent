## VPS Access

- Host: `67.207.90.109` (DigitalOcean)
- SSH user: **`root`** — always `ssh root@67.207.90.109`
- To restart any agent container: `ssh root@67.207.90.109 "cd /opt/<agent-dir> && docker compose up -d --force-recreate <container-name>"`
- Dashboard service: `systemctl restart agent-dashboard` (runs at `http://127.0.0.1:3100`)

## Local API-key stash (gitignored — never commit values)

Reusable secrets live in **`.env.secrets`** at the repo root (renamed Jul 2026 from
`.env.n8n` — that name stopped making sense once it grew past n8n-only keys; matched
by `.env.*` in `.gitignore`, so never committed). Read values from there when a task
needs them:
- `N8N_API_KEY` — n8n public API (header `X-N8N-API-KEY`)
- `SENTRY_API_TOKEN` — Sentry API (`Authorization: Bearer …`); also powers the Sentry→Claude auto-triage
- `ANTHROPIC_API_KEY` — Anthropic key; also set as the GitHub Actions repo secret `ANTHROPIC_API_KEY` (used by `.github/workflows/sentry-autofix.yml`)
- `CLOUDFLARE_API_TOKEN` / `CLOUDFLARE_ACCOUNT_ID` — Cloudflare API (`Authorization: Bearer $CLOUDFLARE_API_TOKEN`); scope unconfirmed, verify via `/accounts/{id}/tokens/verify` before assuming zone-write access

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

## Cloud Dev Rig — 2nd droplet doubles as our dev environment (Jul 1 2026)

The second droplet (`198.211.114.60`) now ALSO hosts the **cloud dev rig** where
Claude Code / Codex do the editing, so local machines stay thin. Fully
**isolated from the booking system** (which still runs there — never disturb it):

- Unprivileged **`dev`** user (`/home/dev`); cannot touch `/var/www`, PM2, nginx.
- Its own **nvm Node 24** (system Node 18 left untouched for booking PM2). **No Docker.**
- Installed for `dev`: Claude Code + Codex CLIs, `ruflo`, `graphify` (0.8.13 via
  pipx — works natively, **no WSL wrapper needed**), repo at
  `/home/dev/full-voice-agent` (origin → GitHub over HTTPS via `gh auth`).
- Migrated: user skills (Claude + Codex), plugins, MCP config (ruflo + account
  connectors), the **claude-mem store** (`/home/dev/.claude-mem`, Chroma off), `.env.secrets`.
- **Steer it:** `ssh dev@198.211.114.60` then `tmux new -A -s dev`; work in
  `~/full-voice-agent`. `MCP_TIMEOUT=60000` set for ruflo's slow ONNX startup.
  (Claude Code Remote / mobile app parked — tmux only.)

## Deployment — AUTO-DEPLOY ON PUSH (Jul 1 2026)

> **Source of truth: [`CONTRIBUTING.md`](./CONTRIBUTING.md) §6.** This section is
> a summary for agent sessions. If it contradicts CONTRIBUTING.md, CONTRIBUTING.md
> is right — fix this section.
>
> **Never commit directly to `main`** (CONTRIBUTING.md §3). Branch → PR → review →
> merge. The merge is the production release.

Pushing to `main` **auto-deploys** each changed agent to prod (`67.207.90.109`)
via `.github/workflows/deploy-on-push.yml` → `deploy.yml`:
- **fast** (code / `knowledge_docs` only): rsync + hot-swap `.py` + `docker restart` (seconds).
- **build** (`requirements*.txt` / `Dockerfile` / `docker-compose.yml`): rsync + `docker compose up -d --build`.
- Mode auto-chosen per agent; only changed agents deploy; `py_compile` gate blocks
  broken pushes; `.env`/runtime state never touched. **No approval gate** — the
  human gate is the pre-push risk analysis. Manual: Actions → "Deploy Agent", or
  `gh workflow run deploy.yml -f agent=<id> -f ref=main -f mode=fast|build`.
- **Sentry → Claude auto-triage** uses **Opus 4.8** (`claude-opus-4-8`), draft-PR only.

## Handover — revert the prod server to clean (Jul 1 2026)

`ops/revert-server-to-clean.sh` (also on the prod VPS at
`/opt/revert-server-to-clean.sh`, baselines in `/opt/.handover-baseline`) restores
every agent's original pre-Sentry code, strips Sentry code/sdk/env, and removes the
Actions deploy key. `SELF_DESTRUCT=1` also removes staging + the script; a single
agent id = safe dry run.

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
  `https://github.com/thiva2k/Taskforce_AI_Website.git` (branch `main`, private).
  Migrated 2026-07-10 off the `taskforceai-sl` org repo (a third party there
  holds org-owner rights; keeping deploy control on an account we fully own
  avoids depending on their access).
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

After modifying any code file in a session, refresh the graph — AST-only, no API cost.
**The command depends on the machine:**

- **Cloud dev rig (`198.211.114.60`, native Linux, graphify 0.8.13):** plain
  **`graphify update .`** from the repo root. The WSL wrapper is not needed — the
  normcase bug is specific to `\\wsl.localhost\` UNC paths, and the repo-root
  `.graphifyignore` handles the admin-bundle exclusions (verified 2026-07-30).
- **Windows/WSL UNC checkout:** **`python ops/graphify-update-wsl.py`**. Bare
  `graphify update .` aborts there on the normcase bug.

Add `--force` when the node count legitimately drops.

> **Three gotchas learned on 2026-07-30:**
>
> 1. **The graph now exceeds the HTML-viz limit.** AST re-extraction is far more
>    fine-grained than the Jun 18 curated LLM build: 3,411 → **7,345 nodes / 10,191
>    edges / 568 communities** (limit 5,000). A plain `graphify update .` therefore
>    **silently deletes `graph.html`**, printing only a skip notice. Restore it with
>    `GRAPHIFY_VIZ_NODE_LIMIT=9000 graphify cluster-only .` — rebuilds report + viz from
>    the existing `graph.json`, no re-extraction, no API cost.
> 2. **NEVER delete `graph.json` to force a clean rebuild.** It carries the INFERRED
>    semantic layer from the Jun 18 LLM build — ~1,790 edges (`rationale_for` 1,678,
>    `semantically_similar_to` 66, `conceptually_related_to` 48) that cost real API
>    spend and that an AST-only rebuild cannot recreate. `update` merges into the
>    existing graph and preserves them; a from-scratch build does not.
> 3. **`.graphifyignore` additions do NOT retroactively remove nodes.** `update` refuses
>    to rewrite for pure removals — it reports "No code-graph topology changes detected"
>    even with `--force` and even after `rm -rf graphify-out/cache`. Newly excluded paths
>    stop being extracted at once, but their nodes linger until another change forces a
>    real rebuild. Hence **`.firecrawl/` scrape caches left 328 stale nodes** in the graph
>    despite now being excluded — harmless noise (4.5%), will drop on the next rebuild.

Rebuild the full graph (`/graphify .`) only for large new areas `graphify update` cannot
pick up — and budget for it, since it re-runs the LLM semantic pass.

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

## n8n Post-Call Processor — WhatsApp JID rules (Jul 2026)

The Treehouse Post-Call Processor (n8n workflow `lGCsV0DYRtPNXfsd` on
`automation.taskforceai.tech`) sends booking confirmations via WasenderAPI,
which requires `to` to be a WhatsApp JID (`94XXXXXXXXX@s.whatsapp.net`).
This has broken twice; the fixed contract is:

- **Phone normalization lives ONLY in the `to` expression** of the
  "wa customer confirmation" node (strips non-digits, drops leading 0,
  forces the `94` country code, appends `@s.whatsapp.net`). The OpenAI
  extraction prompt is instructed to copy the phone digits from the
  transcript VERBATIM — never re-add "include the country code" wording
  there; the model garbles digits when asked to reformat (it once turned
  `0711754668` into `947171754668`).
- **`text` must be** `{{ $('Parse AI JSON').item.json.customer_whatsapp_message }}`.
  A hand-edit once pasted the `to` normalization expression into `text`,
  so guests received a raw JID string as their "confirmation".
- Known-good workflow snapshot: `n8n-workflows/treehouse-post-call-processor.json`
  (re-export after any intentional change; restore via PUT
  `/api/v1/workflows/lGCsV0DYRtPNXfsd` with `name,nodes,connections,settings`).
- **After ANY edit to this workflow, run**
  `python3 n8n-workflows/smoke_test_postcall.py` — it replays the failing
  local-format-number call shape end-to-end and asserts extraction +
  JID + send. It delivers one real WhatsApp message to the test number
  (94711754668) and one manager summary per run.
- The WA nodes have `onError: continueRegularOutput`, so WA send failures
  do NOT mark executions as errored — check the node output JSON for
  `error` when debugging, not just the execution status.
- **Sentry alerting (Jul 2026):** each WA node feeds an
  `IF send failed — <node>` → `Sentry alert — <node>` pair that POSTs an
  error event to the `full-voice-agent` Sentry project (org
  `nutech-solutions`) via the DSN store endpoint whenever the WA node
  output contains `error`. Events carry `logger:n8n.postcall`,
  tags `agent:kavya` / `source:n8n` / `wa_node:<node>`, fingerprint
  `n8n-wa-send-failure`, and `call_sid`/`caller_phone` in extra. Sentry's
  "Notify via GitHub" rule then opens a repo issue, which triggers the
  Claude auto-triage workflow. NOTE for triage: WA send failures are
  fixed in the n8n workflow (this section + the snapshot JSON), NOT in
  this repo's Python code.

## n8n Kavya Handover Notify — unanswered human transfer (Jul 2026)

When Kavya transfers a caller to `HUMAN_AGENT_PHONE` and nobody picks up, the
voice agent collects the guest's name + WhatsApp number and POSTs them to n8n
workflow **`YmeWVEUR54A8o8Tb`** ("Kavya — Handover WhatsApp Notify (No Answer)")
at `/webhook/kavya-handover`, which WhatsApps the property manager via
WasenderAPI. See `Kavya/CLAUDE.md` v0.17 for the agent-side design.

- Payload keys: `call_sid`, `customer_name`, `customer_whatsapp`, `call_summary`,
  `human_agent_whatsapp`, `timestamp`. The workflow reads them off `$json.body.*`.
- **Numbers are sent as bare digits** (`94771234567`) — normalisation lives in
  `Kavya/handover.py::normalize_whatsapp`. Unlike the post-call processor above,
  this workflow does NOT append `@s.whatsapp.net`; WasenderAPI's
  `/api/send-message` resolves the JID itself for this call shape (verified live).
  If sends ever start 422-ing on `to`, append the JID suffix in *Build Message*
  rather than changing the agent payload.
- Known-good snapshot: `n8n-workflows/kavya-handover-whatsapp.json` (the
  WasenderAPI bearer token is scrubbed — copy it from the n8n UI before any
  restore). Restore via PUT `/api/v1/workflows/YmeWVEUR54A8o8Tb` with
  `name,nodes,connections,settings`.
- The agent never blocks on this webhook failing: `send_handover_notification`
  swallows every error, and Kavya still promises the guest a callback.

## Team updates go to ClickUp (Aug 2026)

**Whenever work lands that affects Chanya or Oshadi, post an update in ClickUp —
both of them, every time.** A GitHub review comment is not enough on its own:
they work in Claude Code on the web and do not live in the repo's notification
feed. If a PR of theirs is merged, blocked, conflicted, or superseded, say so in
ClickUp as well as on the PR.

- **Workspace:** one space, `Team Space` (`901811463233`).
- **Channel:** `Critical now` (`6-901818856901-8`). There is **no** `general`
  channel, despite the name being used informally.
- **Member IDs:** Chanya Shehani `113567586`, Oshadi Whyshni Kumaravel
  `113618423`.
  - `chanya@taskfirceai.tech` (`113618477`) is a **dead seat** from a mistyped
    invite — note the `taskfirce`. Never address that one; she gets nothing.

> **Markdown `@mentions` do not notify anyone.** Writing `@Chanya Shehani` in the
> message body renders as plain text — ClickUp needs a real mention entity, which
> the MCP server does not expose. Use the `followers` array on
> `clickup_send_chat_message` (member IDs above) instead. This was discovered the
> hard way: the first update posted looked correct and notified nobody.

> **Markdown tables and `---` rules are silently destroyed.** ClickUp chat
> renders each of them as the literal string `undefined` — the message posts
> "successfully", and the content is simply gone. A status update posted this way
> lost its entire merged-PRs table and nobody could have told from the send
> result. **Use bullet lists instead of tables, and blank lines instead of `---`.**
> Fenced code blocks, `**bold**`, `` `code` `` and emoji all render fine.
>
> **Read the message back after sending.** `clickup_get_chat_channel_messages`
> with `limit: 1` shows what actually landed. Both failure modes in this section
> — mentions that notify nobody, tables that vanish — return `success: true` and
> look correct until you read the channel.

Messages post as `Thivarrakesh Parthipan` (`216208369`), the account holding the
ClickUp OAuth. **That is the repo owner himself** — he drives Claude Code from a
shared `chrys@taskforceai.tech` login but is Thivarrakesh (GitHub `thiva2k`), so
the two names are one person, not a mismatch to work around or apologise for.
Write updates in a voice that suits being sent by him, not by a bot.

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
