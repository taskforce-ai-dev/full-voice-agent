## VPS Access

- Host: `67.207.90.109` (DigitalOcean)
- SSH user: **`root`** — always `ssh root@67.207.90.109`
- To restart any agent container: `ssh root@67.207.90.109 "cd /opt/<agent-dir> && docker compose up -d --force-recreate <container-name>"`
- Dashboard service: `systemctl restart agent-dashboard` (runs at `http://127.0.0.1:3100`)

## KB Auto-Structuring Feature (Jun 2026)

Sentinel admin portal → Knowledge Base page has a 4-step AI structuring flow: pick agent + configure VPS target → paste/upload raw content → Claude structures it → publish live to agent.

**Key new endpoints (admin-auth required):**
- `PUT /api/admin/agents/:agentId/kb-target` — set VPS base URL, secret, filename
- `POST /api/admin/knowledge-base/structure` — Claude structures raw text, returns preview
- `POST /api/admin/knowledge-base/publish` — snapshots current KB, saves new, pushes to agent

**Schema additions:** `AgentKbTarget`, `AgentKbDocument`, `AgentKbSnapshot` models; `businessType`/`businessDescription` on `Agent`.

**Required env var:** `ANTHROPIC_API_KEY` in `/opt/agent-dashboard/.env` — structuring returns 501 without it.

**ChromaDB fix:** All 5 `knowledge_base.py` files now delete-before-upsert in `initialize_kb()` to prevent stale chunk accumulation on KB updates.

## graphify — GRAPH-FIRST, ALWAYS

This project has a graphify knowledge graph at `graphify-out/`. It covers all 5 voice
agents (BSL, Kavya, SLIC, Sofia, Flico) plus SinhalaVITS-TTS and flico-dashboard —
1,988 nodes across 118 communities. **Use it instead of scanning the codebase.** This is
faster and consumes ~83x fewer tokens per question.

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

After modifying any code file in a session, run `graphify update .` to keep the graph
current (AST-only, no API cost).

Rebuild the full graph (`/graphify .`) only when large new areas of the codebase appear
that `graphify update` cannot pick up.

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
  `graphify`, `ui-ux-pro-max`, and the portable `claude-mem` skills.
- Codex MCP currently has `claude-mem` enabled from the cached Claude plugin
  runtime. Vercel MCP was tested but removed because its OAuth redirect was
  rejected in this environment.
