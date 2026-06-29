# Deployment Guide — full-voice-agent

How the voice agents get onto the production VPS, and how to move from the
current **manual scp** method to a reproducible **git-pull CD**.

---

## > STATUS BANNER

> **This CD pipeline is a TEMPLATE. It will FAIL until two things are done:**
>
> 1. The GitHub Secrets `VPS_HOST`, `VPS_USER`, `VPS_SSH_KEY` are set
>    (repo → Settings → Secrets and variables → Actions).
> 2. Each `/opt/<agent-dir>` on the VPS is turned into a **git checkout** of
>    this monorepo (it is not today — see "Wiring the VPS").
>
> Until then `.github/workflows/deploy.yml` is safe to keep in the repo; running
> it just errors clearly at the SSH / git step. Nothing auto-deploys on push.

---

## 0. The box

- **Host:** `67.207.90.109` (DigitalOcean droplet)
- **SSH user:** `root` → `ssh root@67.207.90.109`
- Every agent runs in **Docker** under `/opt/<agent-dir>`, started with
  `docker compose up -d`.
- Each agent's `.env` (API keys, secrets) lives **only on the VPS** and is
  **gitignored** — see [§5](#5-env-files-stay-on-the-vps).

---

## 1. Two deployment methods

### (a) Legacy manual scp — CURRENT STATE

This is what is used today. Fast, but **not git-based**, so the VPS silently
drifts from `main`.

```bash
# fast path: copy one file and bounce the container
scp server.py root@67.207.90.109:/opt/<agent>/
ssh root@67.207.90.109 "docker restart <container>"

# full rebuild path: per-agent deploy.sh
ssh root@67.207.90.109 "cd /opt/<agent> && ./deploy.sh"
```

Problems: no record of *what commit* is live, no clean rollback, easy to forget
to commit a hotfix back to git.

### (b) Git-pull CD — TARGET STATE

Deploying becomes: **check out a commit/tag on the VPS + `docker compose up -d`.**
Reproducible and rollback-able. Driven by `.github/workflows/deploy.yml`:

- Trigger it manually from the **Actions** tab (or `gh workflow run`), picking
  the **agent** and the **ref/tag**.
- The workflow SSHes in, `git fetch --all --tags`, `git checkout <ref>`,
  `git pull --ff-only` (branches only), then `docker compose up -d --build
  --force-recreate`, then prints `docker compose ps`.

Once wired, **method (b) is the documented way to deploy.** Keep (a) only for
true emergencies, and commit the change back to git immediately afterwards.

---

## 2. Required GitHub Secrets

Set these in **repo → Settings → Secrets and variables → Actions → New
repository secret**:

| Secret        | Value                                              | Notes |
|---------------|----------------------------------------------------|-------|
| `VPS_HOST`    | `67.207.90.109`                                    | droplet IP |
| `VPS_USER`    | `root`                                             | SSH user |
| `VPS_SSH_KEY` | the **private** key (full PEM, incl. BEGIN/END)    | the deploy key the Action uses to SSH in |

### Generate the SSH keypair for the Action

On your workstation (or anywhere — the private half only ever lives in GitHub
Secrets):

```bash
ssh-keygen -t ed25519 -C "github-actions-deploy@full-voice-agent" -f ./fva_deploy -N ""
# produces:
#   fva_deploy       (PRIVATE  -> paste into VPS_SSH_KEY secret)
#   fva_deploy.pub   (PUBLIC   -> add to the VPS authorized_keys)
```

Add the **public** key to the VPS so the Action can log in:

```bash
ssh-copy-id -i ./fva_deploy.pub root@67.207.90.109
# or manually:
cat ./fva_deploy.pub | ssh root@67.207.90.109 'mkdir -p ~/.ssh && cat >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys'
```

Paste the **private** key (`cat ./fva_deploy`, the whole file — the full
`BEGIN … PRIVATE KEY` … `END … PRIVATE KEY` block) into the
`VPS_SSH_KEY` secret. Then delete the local copies.

---

## 3. Wiring the VPS (one-time, per agent dir)

The Action requires each `/opt/<agent-dir>` to be a **git checkout of this
monorepo**, not a loose folder of scp'd files.

### 3a. Give the VPS read access to the private repo

Pick ONE:

- **Read-only deploy key (recommended, repo-scoped):**
  ```bash
  ssh root@67.207.90.109
  ssh-keygen -t ed25519 -C "vps-readonly@full-voice-agent" -f ~/.ssh/fva_repo -N ""
  cat ~/.ssh/fva_repo.pub   # add this in GitHub: repo -> Settings -> Deploy keys
                            # (NAME it, leave "Allow write access" UNCHECKED)
  # tell git to use this key for github:
  cat >> ~/.ssh/config <<'EOF'
  Host github-fva
    HostName github.com
    User git
    IdentityFile ~/.ssh/fva_repo
    IdentitiesOnly yes
  EOF
  # clone URL then becomes: git@github-fva:thiva2k/full-voice-agent.git
  ```

- **Fine-grained PAT (alternative):** create a fine-grained Personal Access
  Token with **Contents: Read-only** on `thiva2k/full-voice-agent`, and clone
  via `https://<PAT>@github.com/thiva2k/full-voice-agent.git`. Store it in a
  root-only file; do not echo it into logs.

### 3b. Turn each /opt dir into a checkout

Two strategies — pick per your taste:

- **Full clone of the monorepo (simplest):** clone the whole repo into each
  `/opt/<dir>`. Wasteful on disk but trivial. Each dir tracks its own ref.

  ```bash
  cd /opt
  git clone git@github-fva:thiva2k/full-voice-agent.git kavya-checkout
  # then point /opt/kavya at the agent subfolder, OR run compose from the
  # subfolder: cd /opt/kavya-checkout/Kavya && docker compose up -d
  ```

- **Sparse-checkout per agent (lean):** one checkout, only that agent's folder
  materialized.

  ```bash
  cd /opt
  git clone --no-checkout git@github-fva:thiva2k/full-voice-agent.git kavya
  cd kavya
  git sparse-checkout init --cone
  git sparse-checkout set "Kavya"      # NB: quote folders that contain spaces
  git checkout main
  ```

> **IMPORTANT — `docker compose` working directory.** The compose file lives
> inside the agent's subfolder (which may contain a space, e.g.
> `"BSL Agent"`). The CD `case` statement maps each agent id to the directory
> that contains its `docker-compose.yml`. After wiring, confirm
> `docker compose config` works from that dir. Adjust the mapping in both
> `deploy.yml` and the table below if your compose file sits at the repo root
> vs. inside the subfolder.

---

## 4. Agent → /opt path → container mapping  (OPERATOR-CONFIRM)

> **OPERATOR: confirm every row against the VPS before first use.** The `/opt`
> dir names and container names below are best-guess. Verify with:
> ```bash
> ssh root@67.207.90.109 'ls -d /opt/*/ && docker ps --format "{{.Names}}\t{{.Image}}"'
> ```
> Then update **both** this table and the `case` statement in
> `.github/workflows/deploy.yml` so they stay in sync.

| Agent id (workflow input) | Repo folder              | `/opt/<dir>` (CONFIRM)         | Container name (CONFIRM) | Baseline tag        |
|---------------------------|--------------------------|--------------------------------|--------------------------|---------------------|
| `bsl`                     | `BSL Agent`              | `/opt/bsl-agent`               | `bsl-agent` ?            | `bsl-v1.0.0`        |
| `flico`                   | `Flico Agent`            | `/opt/flico-agent`             | `flico-voice-agent` ?    | `flico-v1.0.0`      |
| `hatton`                  | `HattonHills`            | `/opt/hattonhills`             | `hattonhills` ?          | `hatton-v1.0.0` ?   |
| `slic`                    | `SLIC Agent`             | `/opt/slic-agent`              | `slic-agent` ?           | `slic-v1.0.0`       |
| `sofia`                   | `Sofia Agent`            | `/opt/sofia-agent`             | `sofia-agent` ?          | `sofia-v1.0.0`      |
| `kavya`                   | `Kavya`                  | `/opt/kavya`                   | `kavya` ?                | `kavya-v1.0.0`      |
| `kitchened`               | `Kitchened`              | `/opt/kitchened`               | `kitchened` ?            | `kitchened-v1.0.0` ?|
| `wor`                     | `WorldOfRefrigerators`   | `/opt/worldofrefrigerators`    | `worldofrefrigerators` ? | `wor-v1.0.0` ?      |

Rows with `?` are unconfirmed guesses — replace once verified.

---

## 5. .env files stay on the VPS

Each agent reads its secrets from a `.env` next to its `docker-compose.yml`.
These files are **gitignored**, so:

- `git pull` / `git checkout` on the VPS will **not** overwrite or delete them.
- They are never committed, so they cannot leak through the repo.
- A fresh clone will **not** contain `.env` — when wiring a brand-new `/opt`
  dir, copy the existing `.env` into place (e.g. from a backup or the old scp'd
  folder) before the first `docker compose up`.

If a `git checkout` ever complains that `.env` "would be overwritten", it means
`.env` got tracked by mistake — fix `.gitignore` and `git rm --cached .env`.

---

## 6. Deploying

### Via GitHub UI
Actions → **Deploy Agent (manual)** → **Run workflow** → choose `agent` and
`ref` → Run.

### Via CLI
```bash
# deploy Kavya at the tip of main
gh workflow run deploy.yml -f agent=kavya -f ref=main

# deploy Flico at a specific tag
gh workflow run deploy.yml -f agent=flico -f ref=flico-v1.0.0
```

---

## 7. Rollback

Roll back by deploying a **previous tag** — same workflow, older ref:

```bash
# CD rollback (preferred): redeploy the last-known-good baseline tag
gh workflow run deploy.yml -f agent=flico -f ref=flico-v1.0.0
```

Manual VPS equivalent (emergency only):

```bash
ssh root@67.207.90.109
cd /opt/flico-agent        # CONFIRM dir from the table above
git fetch --all --tags
git checkout flico-v1.0.0
docker compose up -d --build --force-recreate
docker compose ps
```

After any manual action, make sure the live commit matches a real git ref so
the next CD deploy is clean:

```bash
git --no-pager log -1 --oneline
```

---

## 8. Why this is safe-by-default

- The workflow is **`workflow_dispatch`** (manual). Pushes never auto-deploy.
- A `*-v*` tag push only triggers a **reminder job** that prints the exact
  `gh workflow run` command — it does **not** deploy.
- A `concurrency` group serializes deploys so two runs can't fight over the box.
- Missing secrets → the SSH step fails immediately and loudly. No partial,
  half-applied deploy.
