# Contributing to full-voice-agent

This is a **private monorepo** of multilingual AI voice agents for Taskforce AI.
Read this before making your first change — a few things in this repo are
WSL-specific and will silently break if you ignore them.

> **This file is the source of truth for how we branch, review, and deploy.**
> `CLAUDE.md`, `AGENTS.md`, and `README.md` carry short summaries for their own
> audiences and link back here. If any of them disagrees with this file, **this
> file wins** — and the summary is a bug, so fix it in the same PR.

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

## 3. Branching — PR into `main`, always

> **Read §6 before your first push.** `main` is not a staging area:
> **every push to `main` deploys to production automatically.**

- `main` is the default branch and the **live deploy source**.
- **Never commit directly to `main`.** Branch, push the branch, open a PR, get
  one review, then merge. The merge is what ships.
- Branch naming: `feat/<scope>-<short-desc>`, `fix/<scope>-<short-desc>`,
  `chore/<...>` — scope is the agent id (`kavya`, `flico`, `bsl`, …) or area
  (`ci`, `docs`, `ops`).
- Keep branches short-lived and `main` green. Don't force-push shared history.

### This is convention, not enforcement

We are on the **free GitHub plan**, where protected branches and rulesets are
unavailable on private repos. GitHub will **not** stop you from pushing straight
to `main`, and it will not stop that push from deploying to production. There is
no safety net behind this section — the rule is the safety net.

If you are about to push to `main` and you are not certain what will deploy, stop
and ask in the team channel first.

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

## 6. Deploying — merging to `main` IS deploying

> ⚠️ **There is no approval step and no staging environment.** The moment a
> commit lands on `main`, `.github/workflows/deploy-on-push.yml` deploys every
> agent whose folder changed to the **production** VPS (`67.207.90.109`). Treat a
> merge to `main` as a production release, because it is one.

> **Status (Aug 2026): no agent is answering for a client yet.** The fleet is
> deployed and healthy but takes no customer traffic, so today a bad merge costs
> a rebuild rather than a dropped call. That will change, and it will not change
> with an announcement — so the rules here are written for the live case.
> **If you are unsure whether an agent is live, assume it is.**

Each agent deploys independently as a Docker container. On a push to `main`, the
mode is chosen automatically **per changed agent**:

- **image** — the image is built on the **GitHub runner**, pushed to GHCR tagged
  by commit SHA, and the VPS only pulls and restarts. Nothing is compiled on the
  production host.
- **fast** — only code / `knowledge_docs` changed → rsync + hot-swap the changed
  `.py` files into the running container + `docker restart`. Seconds, no rebuild.
- **build** — `requirements*.txt` / `Dockerfile` / `docker-compose.yml` changed →
  rsync + `docker compose up -d --build`, **built on the VPS**. Legacy.

> **All seven active agents use `image`.** Building on the production host meant
> pip and docker competing with the containers answering calls — on 2026-08-02
> eight concurrent builds starved the box and failed 3 of 8 deploys. Nothing is
> compiled on the VPS any more.
>
> `fast` and `build` remain only for `Sofia Agent`, which is parked. Do not use
> them for a new agent.
>
> The mode is chosen from the agent's `docker-compose.yml`: if it pulls a
> `ghcr.io/...` image instead of declaring `build:`, it gets `image` mode. There
> is no second list to keep in sync. For a registry agent **every** change means
> a new image — there is no `fast` hot-swap path, because the container no longer
> runs code copied from disk.

Only agents that actually changed deploy; pushes touching just tooling or docs
deploy nothing. A `py_compile` syntax gate blocks obviously-broken pushes. The
VPS `.env` files and runtime state are never touched (rsync excludes them).

**Either mode restarts that agent's container** — fast is quick, build is longer.
Once an agent is live this drops any call in progress, so land changes during a
quiet window. While the fleet is pre-launch the restart costs only the rebuild
time, which is why structural work is best done now rather than later.

### Manual deploy / redeploy / rollback

`deploy.yml` is the engine and can also be run on its own — use this to redeploy
without a code change, or to roll back to an earlier ref:

```bash
gh workflow run deploy.yml -f agent=kavya -f ref=main                  # tip of main
gh workflow run deploy.yml -f agent=flico -f ref=flico-v1.0.0          # a tag
gh workflow run deploy.yml -f agent=kavya -f ref=<sha> -f mode=fast    # roll back
```

Or **Actions tab → Deploy Agent → Run workflow**. Full details, the
agent → folder → container table, and rollback steps are in
[`docs/DEPLOYMENT.md`](./docs/DEPLOYMENT.md).

### Dependencies are pinned — edit the source, regenerate the lock

Each agent has two dependency files:

- **`requirements-prod.txt`** — the human-readable statement of intent, with
  comments. This is what you edit and what Dependabot updates.
- **`requirements-prod.lock.txt`** — **generated**, fully resolved including
  transitive packages. **This is what the Dockerfile installs.**

The lock is why rebuilding the same commit gives you the same image. Without it
every rebuild resolved to whatever was newest that day, so an agent could break
with nobody having changed a line.

After changing `requirements-prod.txt`, regenerate the lock:

```bash
cd "<Agent Folder>"
docker run --rm -v "$PWD:/w" -w /w python:3.11-slim sh -c \
  'pip install --no-cache-dir -q -r requirements-prod.txt && pip freeze' \
  > requirements-prod.lock.txt
```

Never hand-edit the lock. If a Dependabot PR bumps `requirements-prod.txt`, the
lock must be regenerated in the same PR or the bump has no effect on the image.

> `Sofia Agent/` is intentionally unpinned — it is parked and does not deploy.

### Who can deploy

Anyone with Write access to this repo can merge to `main`, and therefore can
deploy to production. Repo secrets (`VPS_HOST`, `VPS_USER`, `VPS_SSH_KEY`) are
readable by any workflow in the repo. Access to this repo is access to
production — treat the member list accordingly.

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
