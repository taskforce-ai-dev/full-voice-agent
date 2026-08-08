#!/usr/bin/env bash
set -Eeuo pipefail

APP_DIR=/opt/kavya
IMAGE=ghcr.io/taskforce-ai-dev/kavya
LOCK_FILE=/var/lock/kavya-smartpbx-image-deploy.lock
ROLLBACK_TAG=rollback-local
ROLLBACK_ARMED=0
ROLLBACK_RUNNING=0

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

check_loopback_preflight() {
  local health_json status_json
  health_json=$(curl --silent --show-error --fail --connect-timeout 2 --max-time 5 http://127.0.0.1:8006/health) || return 1
  jq -e '.status == "ok" and .service_mode == "smartpbx"' >/dev/null <<<"$health_json" || return 1
  status_json=$(curl --silent --show-error --fail --connect-timeout 2 --max-time 5 http://127.0.0.1:8006/smartpbx/status) || return 1
  jq -e '.active_sessions == 0 and .transfer_enabled == false' >/dev/null <<<"$status_json" || return 1
}

wait_for_smartpbx_ready() {
  local deadline=$((SECONDS + 90))
  while ! check_loopback_preflight; do
    ((SECONDS < deadline)) || return 1
    sleep 2
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

main() {
  [[ $EUID -eq 0 ]] || { fail; return 1; }
  validate_inputs "$@" || { fail; return 1; }
  cd "$APP_DIR"
  exec 9>"$LOCK_FILE"
  flock -n 9 || { fail; return 1; }
  capture_baseline || { fail; return 1; }
  check_loopback_preflight || { fail; return 1; }
  check_env_files || { fail; return 1; }
  voice_validation=$("$APP_DIR/scripts/validate_english_voice_env.sh" .env .env.smartpbx) || { fail; return 1; }
  [[ $voice_validation == canonical_voice_match=ok ]] || { fail; return 1; }
  docker compose --env-file .env.smartpbx --profile smartpbx config >/dev/null || { fail; return 1; }
  verify_candidate_image || { fail; return 1; }
  [[ $(image_id "$IMAGE:$ROLLBACK_TAG") == "$ROLLBACK_IMAGE_ID" ]] || { fail; return 1; }
  arm_rollback
  TAG=$NEW_TAG
  recreate_smartpbx
  wait_for_smartpbx_ready
  verify_running_image "$CANDIDATE_ID" "$CANDIDATE_DIGEST" "$CANDIDATE_REVISION"
  verify_isolation_baseline
  disarm_rollback
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
