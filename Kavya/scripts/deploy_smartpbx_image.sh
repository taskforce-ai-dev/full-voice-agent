#!/usr/bin/env bash
set -Eeuo pipefail

APP_DIR=/opt/kavya
IMAGE=ghcr.io/taskforce-ai-dev/kavya
LOCK_FILE=/var/lock/kavya-smartpbx-image-deploy.lock
ROLLBACK_TAG=rollback-local
ROLLBACK_ARMED=0
ROLLBACK_RUNNING=0

# Bounded wait for the *pre-flight* idle gate only (active_sessions==0). Kept
# env-overridable so tests never have to sleep out a real 10-minute window;
# production leaves these at the defaults.
SMARTPBX_PREFLIGHT_IDLE_TIMEOUT_SECONDS="${SMARTPBX_PREFLIGHT_IDLE_TIMEOUT_SECONDS:-600}"
SMARTPBX_PREFLIGHT_IDLE_POLL_SECONDS="${SMARTPBX_PREFLIGHT_IDLE_POLL_SECONDS:-5}"

# Root-only durable archive of the outgoing container's allowlisted telemetry,
# written immediately before every recreate (see SMARTPBX_RUNBOOK.md
# "Durable telemetry archive"). Overridable only for tests.
SMARTPBX_LOG_ARCHIVE_DIR="${SMARTPBX_LOG_ARCHIVE_DIR:-/var/log/kavya-smartpbx}"

# Exactly the runbook's cutover event allowlist (SMARTPBX_RUNBOOK.md "Durable
# telemetry archive"). Pilot transcript lines are included deliberately: the
# operator has kept SMARTPBX_PILOT_TRANSCRIPT_LOGGING on for this pilot.
SMARTPBX_LOG_ALLOWLIST_PATTERN='event=(smartpbx_protocol_diagnostic|turn_stage|turn_summary|session_summary|stt_post_dispatch_result|llm_round_outcome|llm_stream_timeout|llm_round|capture_[A-Za-z0-9_]*|dtmf_[A-Za-z0-9_]*|barge_in|assistant_turn_delivery|smartpbx_post_call|smartpbx_pilot_transcript)'

fail() { printf '%s\n' 'SmartPBX image deployment failed' >&2; return 1; }

valid_image_id() { [[ $1 =~ ^sha256:[0-9a-f]{64}$ ]]; }
valid_digest() { [[ $1 =~ ^sha256:[0-9a-f]{64}$ ]]; }
valid_revision() { [[ $1 =~ ^[0-9a-f]{40}$ ]]; }
valid_repo_digest() { [[ $1 =~ ^ghcr\.io/taskforce-ai-dev/kavya@sha256:[0-9a-f]{64}$ ]]; }

validate_inputs() {
  [[ $# -eq 3 ]] || return 1
  NEW_TAG=$1
  EXPECTED_SHA=$2
  EXPECTED_DIGEST=$3
  [[ $NEW_TAG =~ ^[0-9a-f]{7}$ ]] || return 1
  valid_revision "$EXPECTED_SHA" || return 1
  valid_digest "$EXPECTED_DIGEST" || return 1
  [[ $NEW_TAG == "${EXPECTED_SHA:0:7}" ]]
}

image_digests() { docker image inspect "$1" --format '{{range .RepoDigests}}{{println .}}{{end}}'; }
image_revision() { docker image inspect "$1" --format '{{index .Config.Labels "org.opencontainers.image.revision"}}'; }
image_id() { docker image inspect "$1" --format '{{.Id}}'; }

select_repo_digest() {
  local image=$1 digests digest
  digests=$(image_digests "$image") || return 1
  while IFS= read -r digest; do
    valid_repo_digest "$digest" || continue
    printf '%s\n' "$digest"
    return 0
  done <<<"$digests"
  return 1
}

has_repo_digest() {
  local image=$1 expected=$2 digests
  digests=$(image_digests "$image") || return 1
  grep -Fx -- "$expected" <<<"$digests" >/dev/null || return 1
}

check_env_files() {
  [[ $(stat -c '%U:%G:%a' .env) == root:root:600 ]] || return 1
  [[ $(stat -c '%U:%G:%a' .env.smartpbx) == root:root:600 ]] || return 1
}

# Optional defence in depth: if SMARTPBX_HOST_FILES_SHA256 names a manifest,
# every host-side control-plane file (the compose file, both nginx vhosts, and
# every scripts/*.sh helper) must have a matching sha256sum entry in it, and
# every entry must verify. Unset/blank -> no-op (opt-in only). A manifest that
# omits one of these files, or does not verify, fails closed.
required_host_files() {
  local file
  printf '%s\n' docker-compose.yml nginx-smartpbx.conf nginx-smartpbx-acme.conf
  for file in scripts/*.sh; do
    [[ -f $file ]] && printf '%s\n' "$file"
  done
}

check_host_files_integrity() {
  local manifest=${SMARTPBX_HOST_FILES_SHA256:-}
  [[ -n $manifest ]] || return 0
  [[ -f $manifest ]] || return 1
  local file
  while IFS= read -r file; do
    [[ -f $file ]] || return 1
    grep -qF " $file" "$manifest" || return 1
  done < <(required_host_files)
  sha256sum -c "$manifest" --strict --quiet
}

smartpbx_status_token() {
  # Read-only and never echoed. Passed to curl on standard input rather than as
  # an argument so it cannot appear in the process list.
  local token
  token=$(sed -n 's/^SMARTPBX_WS_TOKEN=//p' .env.smartpbx | head -n 1) || return 1
  [[ -n $token ]] || return 1
  printf '%s' "$token"
}

smartpbx_status_json() {
  local token
  token=$(smartpbx_status_token) || return 1
  printf 'header = "X-Kavya-SmartPBX-Token: %s"\n' "$token" \
    | curl --silent --show-error --fail --connect-timeout 2 --max-time 5 http://127.0.0.1:8006/smartpbx/status --config -
}

# Readiness = the app answered health and the authenticated status endpoint
# returned *some* JSON. This is deliberately silent on call volume -- a live
# call in progress must never fail the post-recreate/rollback ready gate.
check_smartpbx_ready() {
  local health_json
  health_json=$(curl --silent --show-error --fail --connect-timeout 2 --max-time 5 http://127.0.0.1:8006/health) || return 1
  jq -e '.status == "ok" and .service_mode == "smartpbx"' >/dev/null <<<"$health_json" || return 1
  smartpbx_status_json >/dev/null || return 1
}

# Idleness = readiness AND no active call AND no live transfer. This gate
# belongs ONLY in the pre-flight, before anything is mutated -- it must never
# gate a post-recreate or rollback ready check (a live call arriving in that
# window would otherwise fail the deploy and trigger a rollback that drops
# that same call).
check_loopback_preflight() {
  local health_json status_json
  health_json=$(curl --silent --show-error --fail --connect-timeout 2 --max-time 5 http://127.0.0.1:8006/health) || return 1
  jq -e '.status == "ok" and .service_mode == "smartpbx"' >/dev/null <<<"$health_json" || return 1
  status_json=$(smartpbx_status_json) || return 1
  jq -e '.active_sessions == 0 and .transfer_enabled == false' >/dev/null <<<"$status_json" || return 1
}

# Post-recreate / rollback readiness gate. Bounded at 90s and never checks
# occupancy -- see check_smartpbx_ready above.
wait_for_smartpbx_ready() {
  local deadline=$((SECONDS + 90))
  while ! check_smartpbx_ready; do
    ((SECONDS < deadline)) || return 1
    sleep 2
  done
}

# Pre-flight-only idle gate: poll (never fail-fast) for active_sessions==0 and
# no transfer in progress, up to a bounded wait, before touching the running
# container. A transfer drill (transfer_enabled=true) must be revoked before
# an image deploy -- see SMARTPBX_RUNBOOK.md.
wait_for_smartpbx_idle_preflight() {
  local deadline=$((SECONDS + SMARTPBX_PREFLIGHT_IDLE_TIMEOUT_SECONDS))
  while ! check_loopback_preflight; do
    ((SECONDS < deadline)) || {
      printf '%s\n' 'SMARTPBX_PREFLIGHT_IDLE_TIMEOUT: active_sessions never reached 0 (or a transfer drill is still enabled) within the bounded pre-flight wait -- abort and retry later; do not force a deploy while a call may be live' >&2
      return 1
    }
    sleep "$SMARTPBX_PREFLIGHT_IDLE_POLL_SECONDS"
  done
}

capture_service_snapshot() {
  local service=$1 prefix=$2 id health
  id=$(docker inspect --format '{{.Id}}' "$service") || return 1
  health=$(docker inspect --format '{{.State.Health.Status}}' "$service") || return 1
  [[ $id =~ ^[0-9a-f]{12,64}$ && $health == healthy ]] || return 1
  printf -v "${prefix}_ID" '%s' "$id" || return 1
}

capture_isolation_baseline() {
  capture_service_snapshot flico-voice-agent FLICO || return 1
  capture_service_snapshot kavya-voice-agent LEGACY || return 1
}

verify_isolation_baseline() {
  [[ $(docker inspect --format '{{.Id}}' flico-voice-agent) == "$FLICO_ID" ]] || return 1
  [[ $(docker inspect --format '{{.State.Health.Status}}' flico-voice-agent) == healthy ]] || return 1
  [[ $(docker inspect --format '{{.Id}}' kavya-voice-agent) == "$LEGACY_ID" ]] || return 1
  [[ $(docker inspect --format '{{.State.Health.Status}}' kavya-voice-agent) == healthy ]] || return 1
}

capture_baseline() {
  ROLLBACK_IMAGE_ID=$(docker inspect --format '{{.Image}}' kavya-smartpbx) || return 1
  valid_image_id "$ROLLBACK_IMAGE_ID" || return 1
  ROLLBACK_DIGEST=$(select_repo_digest "$ROLLBACK_IMAGE_ID") || return 1
  ROLLBACK_REVISION=$(image_revision "$ROLLBACK_IMAGE_ID") || return 1
  valid_repo_digest "$ROLLBACK_DIGEST" || return 1
  valid_revision "$ROLLBACK_REVISION" || return 1
  docker image tag "$ROLLBACK_IMAGE_ID" "$IMAGE:$ROLLBACK_TAG" >/dev/null || return 1
  [[ $(image_id "$IMAGE:$ROLLBACK_TAG") == "$ROLLBACK_IMAGE_ID" ]] || return 1
  capture_isolation_baseline || return 1
}

verify_candidate_image() {
  docker pull "$IMAGE@$EXPECTED_DIGEST" >/dev/null || return 1
  CANDIDATE_ID=$(image_id "$IMAGE@$EXPECTED_DIGEST") || return 1
  CANDIDATE_REVISION=$(image_revision "$CANDIDATE_ID") || return 1
  valid_image_id "$CANDIDATE_ID" || return 1
  CANDIDATE_DIGEST="$IMAGE@$EXPECTED_DIGEST"
  has_repo_digest "$CANDIDATE_ID" "$CANDIDATE_DIGEST" || return 1
  [[ $CANDIDATE_REVISION == "$EXPECTED_SHA" ]] || return 1
  docker image tag "$CANDIDATE_ID" "$IMAGE:$NEW_TAG" >/dev/null || return 1
  [[ $(image_id "$IMAGE:$NEW_TAG") == "$CANDIDATE_ID" ]] || return 1
}

# Best-effort, never fails the deploy: archive the OUTGOING container's
# allowlisted telemetry to a root-only host file before it is torn down by
# recreate_smartpbx. See SMARTPBX_RUNBOOK.md "Durable telemetry archive" and
# Kavya/ops/kavya-smartpbx-logrotate for retention. Takes the sha256 image id
# of the container about to be replaced (the forward path passes the baseline
# it is leaving; rollback passes the bad candidate it is leaving).
archive_outgoing_logs() {
  local outgoing_id=$1 stamp out
  stamp=$(date -u +%Y%m%dT%H%M%SZ) || return 0
  mkdir -p "$SMARTPBX_LOG_ARCHIVE_DIR" 2>/dev/null || true
  chmod 700 "$SMARTPBX_LOG_ARCHIVE_DIR" 2>/dev/null || true
  out="$SMARTPBX_LOG_ARCHIVE_DIR/${stamp}-${outgoing_id:7:12}.log"
  { docker logs kavya-smartpbx 2>&1 | grep -E "$SMARTPBX_LOG_ALLOWLIST_PATTERN" >"$out"; } || true
  chmod 600 "$out" 2>/dev/null || true
  return 0
}

recreate_smartpbx() {
  SMARTPBX_IMAGE_TAG="$TAG" docker compose --env-file .env.smartpbx --profile smartpbx up -d --force-recreate --pull never kavya-smartpbx >/dev/null
}

verify_running_image() {
  local expected_id=$1 expected_digest=$2 expected_revision=$3 current_id current_digest current_revision
  current_id=$(docker inspect --format '{{.Image}}' kavya-smartpbx) || return 1
  [[ $current_id == "$expected_id" ]] || return 1
  current_revision=$(image_revision "$current_id") || return 1
  has_repo_digest "$current_id" "$expected_digest" || return 1
  [[ $current_revision == "$expected_revision" ]] || return 1
}

rollback_once() {
  [[ $ROLLBACK_ARMED -eq 1 && $ROLLBACK_RUNNING -eq 0 ]] || return 0
  ROLLBACK_RUNNING=1
  trap - ERR EXIT
  trap '' INT TERM HUP
  TAG=$ROLLBACK_TAG
  archive_outgoing_logs "$CANDIDATE_ID"
  if ! recreate_smartpbx || ! wait_for_smartpbx_ready || ! verify_running_image "$ROLLBACK_IMAGE_ID" "$ROLLBACK_DIGEST" "$ROLLBACK_REVISION" || ! verify_isolation_baseline; then
    printf '%s\n' SMARTPBX_ROLLBACK_ESCALATION_REQUIRED >&2
    return 1
  fi
  ROLLBACK_ARMED=0
  return 0
}

on_error() { exit "$1"; }
on_exit() {
  local status=$1
  trap - EXIT
  if [[ $ROLLBACK_ARMED -eq 1 ]]; then
    rollback_once || exit 1
  fi
  exit "$status"
}
on_signal() {
  rollback_once || exit 1
  exit 1
}

arm_rollback() {
  trap 'on_error $?' ERR
  trap 'on_exit $?' EXIT
  trap 'on_signal' INT TERM HUP
  ROLLBACK_ARMED=1
}

disarm_rollback() {
  ROLLBACK_ARMED=0
  trap - ERR EXIT INT TERM HUP
}

# Post-disarm disk hygiene (never runs while rollback is armed, never blocks
# or fails the deploy). Removes stale ghcr.io/taskforce-ai-dev/kavya tags,
# keeping exactly: the tag this container is now running, the Twilio
# kavya-voice-agent container's tag, rollback-local, latest, and any
# stable-* tag. Then prunes dangling images and the build cache. Never runs
# the aggressive all-images prune flag -- that would remove tags still in
# use by either running container.
prune_stale_kavya_images() {
  local smartpbx_ref twilio_ref image_line tag
  smartpbx_ref=$(docker inspect --format '{{.Config.Image}}' kavya-smartpbx 2>/dev/null) || smartpbx_ref=""
  twilio_ref=$(docker inspect --format '{{.Config.Image}}' kavya-voice-agent 2>/dev/null) || twilio_ref=""
  while IFS= read -r image_line; do
    [[ -n $image_line && $image_line == "$IMAGE:"* ]] || continue
    tag=${image_line#"$IMAGE:"}
    case $tag in
      "$ROLLBACK_TAG" | latest | stable-*) continue ;;
    esac
    [[ $image_line == "$smartpbx_ref" ]] && continue
    [[ $image_line == "$twilio_ref" ]] && continue
    docker rmi "$image_line" >/dev/null 2>&1 || true
  done < <(docker images --format '{{.Repository}}:{{.Tag}}' "$IMAGE" 2>/dev/null || true)
  docker image prune -f >/dev/null 2>&1 || true
  docker builder prune -af >/dev/null 2>&1 || true
  return 0
}

main() {
  [[ $EUID -eq 0 ]] || { fail; return 1; }
  validate_inputs "$@" || { fail; return 1; }
  cd "$APP_DIR"
  exec 9>"$LOCK_FILE"
  flock -n 9 || { fail; return 1; }
  capture_baseline || { fail; return 1; }
  wait_for_smartpbx_idle_preflight || { fail; return 1; }
  check_env_files || { fail; return 1; }
  check_host_files_integrity || { fail; return 1; }
  voice_validation=$("$APP_DIR/scripts/validate_english_voice_env.sh" .env .env.smartpbx) || { fail; return 1; }
  [[ $voice_validation == canonical_voice_match=ok ]] || { fail; return 1; }
  docker compose --env-file .env.smartpbx --profile smartpbx config >/dev/null || { fail; return 1; }
  # Free space BEFORE pulling the candidate (the image is ~2.5 GB and grows with
  # the baked embedding model); the same keep-set as the post-deploy prune.
  prune_stale_kavya_images
  verify_candidate_image || { fail; return 1; }
  [[ $(image_id "$IMAGE:$ROLLBACK_TAG") == "$ROLLBACK_IMAGE_ID" ]] || { fail; return 1; }
  arm_rollback
  TAG=$NEW_TAG
  archive_outgoing_logs "$ROLLBACK_IMAGE_ID"
  recreate_smartpbx
  wait_for_smartpbx_ready
  verify_running_image "$CANDIDATE_ID" "$CANDIDATE_DIGEST" "$CANDIDATE_REVISION"
  verify_isolation_baseline
  disarm_rollback
  prune_stale_kavya_images
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
