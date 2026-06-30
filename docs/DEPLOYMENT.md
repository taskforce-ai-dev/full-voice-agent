# Deployment Guide — full-voice-agent

How the voice agents reach the production VPS via the **git-sourced CD** in
`.github/workflows/deploy.yml`.

---

## ✅ STATUS: WIRED

The CD is set up and the SSH path is validated. Repo secrets `VPS_HOST`,
`VPS_USER`, `VPS_SSH_KEY` are set, and a dedicated ed25519 deploy key was added
to the VPS `~/.ssh/authorized_keys`. You can deploy from the **Actions** tab or
`gh workflow run` right now.

> ⚠️ A deploy runs `docker compose up -d --build`, which **rebuilds the image and
> restarts that agent's container** — briefly interrupting any in-flight call on
> that agent. Deploy during a quiet window. `.env` and runtime state on the VPS
> are never touched (rsync excludes them).

---

## 0. The box

- **Host:** `67.207.90.109` (DigitalOcean) · **SSH user:** `root`
- Each agent runs in **Docker** under `/opt/<dir>`, started with `docker compose up -d`.
- Each agent's `.env` lives **only on the VPS** and is gitignored — never committed, never overwritten by deploys.

---

## 1. How the CD works (rsync model)

No git checkout is needed on the VPS — the **GitHub Actions runner** is the
source of truth:

1. You trigger `Deploy Agent (manual)` with an **agent** + a **ref/tag**.
2. The runner checks out the repo at that ref.
3. It `rsync`s the agent's source folder (e.g. `BSL Agent/`) to its `/opt/<dir>`,
   **excluding** `.env`, `.env.*`, `active_sessions.json`, `*.log`, `*.bak`,
   `venv/`, `chroma_db/`, `node_modules/`, `__pycache__/`, `.git/` (no `--delete`,
   so VPS-only files/state are preserved).
4. It SSHes in and runs `docker compose up -d --build`, then a health check.

This mirrors the old manual `scp` flow, but the source is a known git commit/tag
— reproducible and rollback-able.

---

## 2. Required GitHub Secrets  (already set ✅)

| Secret        | Value             | Notes |
|---------------|-------------------|-------|
| `VPS_HOST`    | `67.207.90.109`   | droplet IP |
| `VPS_USER`    | `root`            | SSH user |
| `VPS_SSH_KEY` | private ed25519   | the Actions deploy key (public half is in the VPS `authorized_keys`) |

To rotate the deploy key: generate a new ed25519 pair, append the public half to
the VPS `~/.ssh/authorized_keys`, update `VPS_SSH_KEY`, then remove the old
public key from the VPS.

---

## 3. Agent → repo folder → /opt → container  (verified against the VPS)

Confirmed via `ls -d /opt/*/` and `docker ps` on 2026-06-29.

| Agent id | Repo folder              | `/opt/<dir>`                  | Container                     |
|----------|--------------------------|-------------------------------|-------------------------------|
| `bsl`    | `BSL Agent`              | `/opt/bsl-agent`              | `bsl-agent`                   |
| `flico`  | `Flico Agent`            | `/opt/flico`                  | `flico-voice-agent`           |
| `hatton` | `HattonHills`            | `/opt/hatton-hills`           | `hatton-hills-voice-agent`    |
| `slic`   | `SLIC Agent`             | `/opt/slic-agent`             | `slic-voice-agent`            |
| `sofia`  | `Sofia Agent`            | `/opt/sofia`                  | `sofia-voice-agent` *(stopped at last check)* |
| `kavya`  | `Kavya`                  | `/opt/kavya`                  | `kavya-voice-agent`           |
| `kitchened` | `Kitchened`           | `/opt/kitchened`             | `kitchened-voice-agent`       |
| `wor`    | `WorldOfRefrigerators`   | `/opt/worldofrefrigerators`   | `wor-voice-agent`             |

This table and the `case` statement in `deploy.yml` must stay in sync.

---

## 4. Deploying

**UI:** Actions → **Deploy Agent (manual)** → **Run workflow** → pick `agent` + `ref`.

**CLI:**
```bash
gh workflow run deploy.yml -f agent=kavya -f ref=main          # tip of main
gh workflow run deploy.yml -f agent=flico -f ref=flico-v1.0.0  # a specific tag
```

Pushing a `*-v*` tag does **not** auto-deploy — it only prints a reminder.

---

## 5. Rollback

Redeploy a previous tag (the runner checks it out and rsyncs that version):

```bash
gh workflow run deploy.yml -f agent=flico -f ref=flico-v1.0.0
```

Emergency manual equivalent on the box:
```bash
ssh root@67.207.90.109 'cd /opt/flico && docker compose up -d --build && docker compose ps'
```
(After any manual hotfix on the VPS, commit it back to git so the next CD deploy
doesn't silently revert it.)

---

## 6. Safe-by-default

- Manual `workflow_dispatch` only — pushes never auto-deploy.
- Tag pushes trigger a reminder job, not a deploy.
- A `concurrency` group serializes deploys so two runs can't fight over the box.
- Workflow inputs are passed as env vars (not interpolated into shell) — no
  command injection via the free-form `ref`.
- Missing/invalid secrets fail the SSH step immediately — no half-applied deploy.

---

## 7. Legacy manual path (still available)

The old per-file flow still works for true emergencies, but prefer the CD and
commit any change back to git afterwards:
```bash
scp "server.py" root@67.207.90.109:/opt/<dir>/ && ssh root@67.207.90.109 "docker restart <container>"
```
