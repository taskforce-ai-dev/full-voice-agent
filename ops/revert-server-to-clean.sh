#!/usr/bin/env bash
# =============================================================================
# revert-server-to-clean.sh  —  HANDOVER "revert button"
# -----------------------------------------------------------------------------
# Restores the voice agents on THIS VPS to their clean, pre-tooling state — the
# code as it was before anything we added this session (Sentry, automations).
# After it runs, each /opt/<agent> holds only the original agent code.
#
# It does NOT depend on the git repo or the internet: the exact original files
# are staged in /opt/.handover-baseline (taken from the pre-Sentry commit and
# verified byte-identical to the originals).
#
# Per agent it:
#   1. backs up the current server.py/.env/requirements to /opt/.revert-backup
#   2. restores the original server.py + requirements from the baseline
#   3. removes the SENTRY_* lines from .env
#   4. rebuilds a clean image (no sentry-sdk) and restarts  [SKIP_BUILD=1 = fast]
#   5. verifies the container is healthy with zero Sentry references
# Then it removes the GitHub Actions deploy key from /root/.ssh/authorized_keys.
#
# Usage (on the VPS):
#   bash /opt/revert-server-to-clean.sh                 # full clean (rebuild)
#   SKIP_BUILD=1 bash /opt/revert-server-to-clean.sh    # fast: no image rebuild
#   SELF_DESTRUCT=1 bash /opt/revert-server-to-clean.sh # also delete the
#                                                        # baseline + this script
# NOTE: the CI/CD, dependabot, healthcheck and Sentry auto-triage all live on
# GitHub, NOT on this server — simply don't hand over the repo. The only
# server-side traces this script removes are the Sentry code/deps/env and the
# Actions deploy key.
# =============================================================================
set -uo pipefail

BASE=/opt/.handover-baseline
BACKUP=/opt/.revert-backup
AGENTS="bsl-agent flico hatton-hills slic-agent kavya kitchened worldofrefrigerators sofia"

container_for() {
  case "$1" in
    bsl-agent)            echo bsl-agent ;;
    flico)                echo flico-voice-agent ;;
    hatton-hills)         echo hatton-hills-voice-agent ;;
    slic-agent)           echo slic-voice-agent ;;
    kavya)                echo kavya-voice-agent ;;
    kitchened)            echo kitchened-voice-agent ;;
    worldofrefrigerators) echo wor-voice-agent ;;
    sofia)                echo sofia-voice-agent ;;
  esac
}

if [ ! -d "$BASE" ]; then
  echo "ERROR: baseline $BASE not found — cannot revert safely. Aborting." >&2
  exit 1
fi

[ "${SKIP_BUILD:-0}" = "1" ] || docker builder prune -af >/dev/null 2>&1 || true

for d in $AGENTS; do
  od="/opt/$d"
  [ -d "$od" ] || { echo "skip $d (no $od)"; continue; }
  [ -f "$BASE/$d/server.py" ] || { echo "skip $d (no baseline staged)"; continue; }
  echo "=== reverting $d ==="
  mkdir -p "$BACKUP/$d"
  for f in server.py requirements-prod.txt requirements.txt .env docker-compose.yml; do
    [ -f "$od/$f" ] && cp "$od/$f" "$BACKUP/$d/$f" 2>/dev/null
  done

  cp "$BASE/$d/server.py" "$od/server.py"
  [ -f "$BASE/$d/requirements-prod.txt" ] && cp "$BASE/$d/requirements-prod.txt" "$od/requirements-prod.txt"
  [ -f "$BASE/$d/requirements.txt" ]      && cp "$BASE/$d/requirements.txt"      "$od/requirements.txt"
  if [ -f "$od/.env" ]; then
    grep -vE '^SENTRY_' "$od/.env" > "$od/.env.revert.tmp" && mv "$od/.env.revert.tmp" "$od/.env"
  fi

  c="$(container_for "$d")"
  if docker ps -a --format '{{.Names}}' | grep -qx "$c"; then
    if [ "${SKIP_BUILD:-0}" = "1" ]; then
      docker cp "$od/server.py" "$c:/app/server.py" >/dev/null 2>&1 || true
      ( cd "$od" && docker compose up -d --force-recreate ) >/dev/null 2>&1
    else
      ( cd "$od" && docker compose up -d --build ) >/dev/null 2>&1
      docker image prune -f >/dev/null 2>&1
    fi
    sleep 4
    refs="$(docker exec "$c" grep -c SENTRY_DSN /app/server.py 2>/dev/null || echo '?')"
    echo "  $c: $(docker ps --filter "name=$c" --format '{{.Status}}')  | server.py SENTRY refs=$refs (want 0)"
  else
    echo "  $d: files reverted on disk (container '$c' not running)"
  fi
done

# Remove the GitHub Actions deploy key we added.
AK=/root/.ssh/authorized_keys
if [ -f "$AK" ] && grep -q 'gha-deploy-fva' "$AK"; then
  grep -v 'gha-deploy-fva' "$AK" > "$AK.revert.tmp" && mv "$AK.revert.tmp" "$AK" && chmod 600 "$AK"
  echo "=== removed GitHub Actions deploy key from authorized_keys ==="
fi

echo
echo "Backups of the pre-revert files are in $BACKUP (delete once you've confirmed)."
if [ "${SELF_DESTRUCT:-0}" = "1" ]; then
  rm -rf "$BASE" "$BACKUP"
  echo "removed $BASE and $BACKUP"
  rm -f /opt/revert-server-to-clean.sh && echo "removed this script — server is now pure agents."
fi
echo "=== DONE: agents reverted to clean pre-tooling state. ==="
