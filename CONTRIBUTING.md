# Contributing to full-voice-agent

This is a **private monorepo** of multilingual AI voice agents for Taskforce AI.
Read this before making your first change — a few things in this repo are
WSL-specific and will silently break if you ignore them.

For a tour of the repo and the agents, start with [`README.md`](./README.md).
For deep work on any single agent, read that agent's `CLAUDE.md`.

---

## 1. Environment — commit from WSL Ubuntu

The repo lives on a **WSL Ubuntu** filesystem (the working tree is the
`\\wsl.localhost\Ubuntu\home\thiva\Full Voice agent` UNC path from Windows).

**Always run `git commit` from inside WSL Ubuntu, not from Windows Git Bash /
PowerShell.** The pre-commit hook hardcodes `/usr/bin/python3`, which only
exists in WSL. Committing from a Windows shell either skips the hook or fails to
find the interpreter, so secret scanning and the docs-sync guard never run.

Activate the hooks **once per clone** (in WSL):

```bash
pipx install pre-commit      # or: pip install pre-commit
pre-commit install
```

After that the hooks run automatically on every `git commit`. See
[`docs/PRECOMMIT.md`](./docs/PRECOMMIT.md) for the full hook list.

### What the pre-commit hooks enforce

- **pre-commit-hooks** — trailing whitespace, end-of-file newline, large-file
  block (~5 MB / `--maxkb=5000`), merge-conflict markers, private keys, valid
  YAML/JSON.
- **gitleaks** — scans staged changes for secrets (config in `.gitleaks.toml`).
- _(ruff lint is **not** a pre-commit hook on this pre-ruff codebase — it runs
  in CI, advisory; see `.github/workflows/ci.yml`.)_
- **agents-md-sync** — runs `ops/sync-agent-docs.sh` and fails if any
  `*/AGENTS.md` is out of date with its `CLAUDE.md`.

`git commit --no-verify` bypasses all of the above. Don't — it skips secret
scanning. Use it only for a genuine hotfix when a hook is misbehaving, then run
`pre-commit run --all-files` and fix any fallout immediately.

---

## 2. Commit conventions

Use **conventional-commit** style with an agent (or area) scope:

```
fix(flico): correct Sinhala TTS voice id
feat(kavya): add eZee availability tool retry
ci: harden deploy.yml secret handling
docs: per-agent READMEs + AGENTS.md mirrors
chore: consolidate voice agent fleet into monorepo
```

Common scopes are the agent ids (`bsl`, `flico`, `hatton`, `slic`, `sofia`,
`kavya`, `kitchened`, `wor`) and area types (`ci`, `docs`, `chore`, `ops`).
Keep the subject imperative and concise.

---

## 3. Branching

- `main` is the default branch and the deploy source.
- Branch protection is **not enforced server-side** (free GitHub plan), so the
  discipline is on us: prefer a short-lived feature branch + PR for anything
  non-trivial, keep `main` green, and don't force-push shared history.

---

## 4. Per-agent docs: CLAUDE.md is the source of truth

Each **agent** has a `CLAUDE.md` — the **source of truth** — and a matching
`AGENTS.md` that is a **generated mirror** for Codex (and other AGENTS.md-aware
tools).

After editing any `CLAUDE.md`, regenerate the mirrors:

```bash
bash ops/sync-agent-docs.sh
```

Never hand-edit a **per-agent** `AGENTS.md` — your change will be overwritten.
The `agents-md-sync` pre-commit hook guards against drift and will fail the
commit if a mirror is stale.

> **The repo root is the exception.** The root `CLAUDE.md` and `AGENTS.md` are a
> **manually-maintained pair** — the root `AGENTS.md` is a distinct,
> Codex-oriented document, **not** a generated mirror. `ops/sync-agent-docs.sh`
> only mirrors per-agent subdirectories (`*/AGENTS.md`) and does **not** touch
> the root pair, and the `agents-md-sync` hook does not check it. When you change
> shared project guidance, edit **both** root files by hand and keep them in sync.

---

## 5. graphify-first exploration

The repo ships a graphify knowledge graph in `graphify-out/`. Before scanning
source files to "get oriented":

1. Read [`graphify-out/GRAPH_REPORT.md`](./graphify-out/GRAPH_REPORT.md) for god
   nodes, communities, and architecture in one read.
2. Query the graph for any how/where/what/why question:
   ```bash
   graphify query "<question>"
   graphify path "<A>" "<B>"
   graphify explain "<concept>"
   ```
3. Only open raw files once the graph points you at a specific symbol you need
   to edit.

After modifying code, refresh the graph (AST-only, no API cost):

```bash
python ops/graphify-update-wsl.py
```

Do **not** run bare `graphify update .` on this WSL/UNC checkout — it aborts on
the `os.path.normcase` bug and re-ingests minified admin bundles. Use the
`ops/graphify-update-wsl.py` wrapper. A `.graphifyignore` at the repo root keeps
the admin SPA bundles out of the graph. See the root `CLAUDE.md` for the full
rationale.

---

## 6. Deploying

Each agent deploys independently to the DigitalOcean VPS (`67.207.90.109`) as a
Docker container, via the **manual CD workflow** in
`.github/workflows/deploy.yml`. The GitHub Actions runner checks out the chosen
ref, rsyncs the agent's folder to `/opt/<dir>` (excluding `.env` and runtime
state), then runs `docker compose up -d --build` plus a health check.

```bash
gh workflow run deploy.yml -f agent=kavya -f ref=main           # tip of main
gh workflow run deploy.yml -f agent=flico -f ref=flico-v1.0.0   # a specific tag
```

Or use the **Actions tab → Deploy Agent (manual) → Run workflow**. A deploy
rebuilds the image and restarts that agent's container, briefly interrupting
in-flight calls — deploy during a quiet window. Full details, the
agent → folder → container table, and rollback steps are in
[`docs/DEPLOYMENT.md`](./docs/DEPLOYMENT.md).

---

## 7. Secrets

**Never commit secrets.** Every agent keeps real credentials in a git-ignored
`.env`; only `.env.example` (a key-less template) is committed. `.gitignore`
also blocks GCP service-account JSON, ChromaDB stores, model caches, and audio
temp files. The `gitleaks` pre-commit hook and the `secret-scan` CI workflow are
the guardrails.

If you ever find a committed secret, treat it as compromised: scrub it from the
working tree (and history if needed) and **rotate the credential immediately**.
Production secrets belong only in the per-agent `.env` files on the VPS. See the
**Security** section of [`README.md`](./README.md) (and `SECURITY.md` if present)
for the full policy.
