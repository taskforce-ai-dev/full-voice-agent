#!/usr/bin/env bash
set -Eeuo pipefail
APP_DIR=/opt/kavya
IMAGE=ghcr.io/taskforce-ai-dev/kavya
LOCK_FILE=/var/lock/kavya-smartpbx-image-deploy.lock
fail(){ return 1; }
validate_inputs(){ NEW_TAG=$1; EXPECTED_SHA=$2; EXPECTED_DIGEST=$3; [[ $NEW_TAG =~ ^[0-9a-f]{7,40}$ && $EXPECTED_SHA =~ ^[0-9a-f]{40}$ && $EXPECTED_DIGEST =~ ^sha256:[0-9a-f]{64}$ ]]; }
check_env_files(){ [[ $(stat -c "%U:%G:%a" .env) == root:root:600 && $(stat -c "%U:%G:%a" .env.smartpbx) == root:root:600 ]]; }
check_loopback_preflight(){ local status_json; curl --silent --show-error --fail --connect-timeout 2 --max-time 5 http://127.0.0.1:8006/health >/dev/null 2>&1; status_json=$(curl --silent --show-error --fail --connect-timeout 2 --max-time 5 http://127.0.0.1:8006/smartpbx/status 2>/dev/null); jq -e ".active_sessions == 0 and .transfer_enabled == false" >/dev/null <<<"$status_json"; }
wait_for_smartpbx_ready(){ local deadline=$((SECONDS + 90)); while ! check_loopback_preflight; do ((SECONDS < deadline)) || return 1; sleep 2; done; }
check_legacy_flico(){ [[ $(docker inspect --format "{{.State.Health.Status}}" flico-voice-agent) == healthy ]]; }
capture_baseline(){ ROLLBACK_IMAGE_ID=$(docker inspect --format "{{.Image}}" kavya-smartpbx); ROLLBACK_TAG=rollback-local; ROLLBACK_DIGEST=$(docker image inspect "$ROLLBACK_IMAGE_ID" --format "{{index .RepoDigests 0}}"); ROLLBACK_REVISION=$(docker image inspect "$ROLLBACK_IMAGE_ID" --format "{{ index .Config.Labels \"org.opencontainers.image.revision\" }}"); docker image tag "$ROLLBACK_IMAGE_ID" "$IMAGE:$ROLLBACK_TAG"; verify_local_rollback; }
verify_local_rollback(){ docker image inspect "ghcr.io/taskforce-ai-dev/kavya:$ROLLBACK_TAG" >/dev/null 2>&1; }
verify_candidate_image(){ docker pull "$IMAGE@$EXPECTED_DIGEST" > /dev/null 2>&1; }
recreate_smartpbx(){ SMARTPBX_IMAGE_TAG="$TAG" docker compose --env-file .env.smartpbx --profile smartpbx up -d --force-recreate --pull never kavya-smartpbx >/dev/null 2>&1; }
verify_running_image(){ docker inspect --format "{{.Image}}" kavya-smartpbx >/dev/null; }
verify_legacy_flico_unchanged(){ check_legacy_flico; }
rollback(){ trap - ERR; TAG=$ROLLBACK_TAG; if ! recreate_smartpbx || ! wait_for_smartpbx_ready; then printf "%s\n" SMARTPBX_ROLLBACK_ESCALATION_REQUIRED >&2; return 1; fi; exit 1; }
main(){ [[ $EUID -eq 0 ]] || fail; validate_inputs "$@"; cd "$APP_DIR"; exec 9>"$LOCK_FILE"; flock -n 9; capture_baseline; check_loopback_preflight; check_env_files; voice_validation=$("$APP_DIR/scripts/validate_english_voice_env.sh" .env .env.smartpbx); [[ $voice_validation == canonical_voice_match=ok ]]; docker compose --env-file .env.smartpbx --profile smartpbx config > /dev/null 2>&1; check_legacy_flico; verify_candidate_image; TAG=$NEW_TAG; trap rollback ERR; recreate_smartpbx; wait_for_smartpbx_ready; verify_running_image; verify_legacy_flico_unchanged; }
if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
 main "$@"
fi
