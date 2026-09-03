# Running the Kavya container as a non-root user — do this deliberately, not casually

**Status:** not done. This note exists so the next person who reaches for it
does not do it as a one-line `USER` addition during an unrelated change — the
security audit that raised this (2026-09-02, Medium: "container runs as root
on a floating base tag with no hardening options") deliberately left it out of
scope for the same reason: the failure mode is a silent crash loop discovered
only after deploy, on the host volumes both `kavya-voice-agent` and
`kavya-smartpbx` write to.

## Why this isn't a one-line change

Both services in `docker-compose.yml` bind-mount host directories that the
container process writes to:

- `./chroma_db:/app/chroma_db` (legacy) and `./chroma_db_smartpbx:/app/chroma_db`
  (SmartPBX) — ChromaDB's persisted vector store, written on every embedding.
- `./knowledge_docs:/app/knowledge_docs` — read-only for SmartPBX, read-write
  for legacy (`:ro` is present on the SmartPBX mount only).
- `./full-voice-agent-a8a245fb37cb.json:/app/gcp-credentials.json:ro` — read
  by the container for Google Cloud STT.

Today the container runs as root (no `USER` in `Dockerfile`, no `user:` in
compose), so every one of those host paths is already owned by whatever UID
created them on the VPS — in practice `root`, because the container itself
created `chroma_db/` and `chroma_db_smartpbx/` on first write. If a future
change adds `RUN useradd ... && USER kavya` to the `Dockerfile` and
`user: "10001:10001"` to compose **without first fixing host-side ownership**,
the very next deploy:

1. Pulls/builds the new image and recreates the container as uid 10001.
2. ChromaDB tries to write to `/app/chroma_db`, which is bind-mounted to a
   host directory still owned by `root:root` — `PermissionError` on the very
   first embedding write.
3. The legacy service crash-loops (`kavya-voice-agent` restarts, answers no
   calls) or, on SmartPBX, `check_loopback_preflight` in
   `scripts/deploy_smartpbx_image.sh` never goes healthy and the guarded
   deploy rolls back — but only if this was deployed through that script; a
   generic `docker compose up -d --build` has no such rollback.

This is exactly the kind of failure that is invisible in CI (no bind mounts
there) and only shows up against the real host filesystem — the audit's
concern was specifically about avoiding an "it worked in review, broke in
prod" change.

## How to do it safely, when it's time

1. **Pick a UID/GID up front and keep it stable** (e.g. `10001:10001`) — it
   has to match exactly between the `Dockerfile`'s `useradd -u 10001` and
   compose's `user: "10001:10001"` on every service that shares a volume.

2. **Chown the host-side bind mount directories to that UID before the new
   image is ever started**, for both services, on the VPS:

   ```sh
   cd /opt/kavya
   sudo chown -R 10001:10001 chroma_db chroma_db_smartpbx knowledge_docs
   # The GCP credentials file only needs to be *readable* by the new UID; it
   # does not need to be owned by it.
   sudo chmod 0644 full-voice-agent-a8a245fb37cb.json
   ```

   Do this as a **separate, reviewed step immediately before** the deploy
   that ships the `USER`/`user:` change — not weeks earlier (something else
   could recreate the directories as root in between) and not after (the
   crash-loop already happened by then).

3. **Land the `Dockerfile` and `docker-compose.yml` changes in the same PR**,
   using `mode=build` for the deploy (a `USER` change is not something `fast`
   hot-swap can apply — it needs an image rebuild). Reference:
   `.github/workflows/deploy.yml`'s mode table.

4. **Roll out to one service at a time.** SmartPBX (`kavya-smartpbx`) is the
   lower-traffic, better-instrumented path — it already has the guarded
   `scripts/deploy_smartpbx_image.sh` with an automatic rollback on a failed
   `check_loopback_preflight`. Prove the non-root user there first, watch
   `/smartpbx/status` and the container's health for a full day, then apply
   the same change to the legacy `kavya-voice-agent` service, which has no
   equivalent guarded rollback — plan a manual verification window for it
   (`docker compose ps`, `docker logs`, an inbound test call) after that
   deploy.

5. **Add `cap_drop: [ALL]` and `security_opt: [no-new-privileges:true]` in
   the same change**, and consider `read_only: true` with a `tmpfs: [/tmp]`
   mount — these don't have the ownership hazard above and are safe to bundle
   in, but verify the app doesn't write anywhere under `/app` other than the
   two bind-mounted paths first (a `read_only` root filesystem will surface
   any other write immediately as a crash, which is the point, but confirm
   it's not a surprise in the same deploy as the UID change).

6. **Pin the base image too, while touching the `Dockerfile` anyway**:
   `FROM python:3.11-slim@sha256:<digest>` instead of the floating
   `python:3.11-slim` tag, so a rebuild of the same reviewed commit cannot
   silently pick up different OS packages. Bump the digest via a normal
   reviewed PR when Python 3.11 gets a new patch release.

## What NOT to do

- Do not add `USER` to the `Dockerfile` alone without the matching
  `chown` step above — that is exactly the crash-loop path described.
- Do not do this as a drive-by part of an unrelated change; it touches both
  services' runtime identity and needs its own rollout window.
- Do not skip the SmartPBX-first staged rollout — the legacy service has
  no automatic rollback if the ownership fix was missed or incomplete.
