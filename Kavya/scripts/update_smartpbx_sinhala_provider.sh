#!/usr/bin/env bash
# Closed, root-only transaction for the one SmartPBX Sinhala LLM rollback.
set -Eeuo pipefail
umask 077

readonly PROTECTED_FILE=/opt/kavya/.env.smartpbx
readonly TRANSACTION_DIR=/opt/kavya/.smartpbx-sinhala-rollback
readonly BACKUP_FILE="$TRANSACTION_DIR/backup.env"
readonly METADATA_FILE="$TRANSACTION_DIR/metadata"
readonly PENDING_FILE="$TRANSACTION_DIR/pending"
readonly PROVIDER_KEY=SMARTPBX_SINHALA_LLM_PROVIDER
readonly PROVIDER_VALUE=claude
readonly SENTINEL='SMARTPBX_SINHALA_LLM_PROVIDER=__SMARTPBX_ROLLBACK_SENTINEL__'

status() { printf '%s\n' "$1"; }
fail() { status SMARTPBX_SINHALA_PROVIDER_UPDATE_FAILED >&3; return 1; }
silent() { "$@" >/dev/null 2>&1; }

metadata_is() {
  local path=$1 expected=$2 actual
  actual=$(stat -c '%u:%g:%a' -- "$path" 2>/dev/null) || return 1
  [[ $actual == "$expected" ]]
}

validate_interface() {
  case ${1:-} in
    apply) [[ $# -eq 3 && $2 == "$PROTECTED_FILE" && $3 == "$PROVIDER_VALUE" ]] ;;
    restore|cleanup) [[ $# -eq 2 && $2 == "$PROTECTED_FILE" ]] ;;
    *) return 1 ;;
  esac
}

lock_transaction_directory() {
  metadata_is "$TRANSACTION_DIR" '0:0:700' || return 1
  exec 9<"$TRANSACTION_DIR" || return 1
  silent flock -n 9
}

transaction_empty() {
  [[ -z $(find "$TRANSACTION_DIR" -mindepth 1 -maxdepth 1 -printf x 2>/dev/null) ]]
}

transaction_pending() {
  [[ -f $BACKUP_FILE && -f $METADATA_FILE && -f $PENDING_FILE ]] || return 1
  metadata_is "$BACKUP_FILE" '0:0:600' || return 1
  metadata_is "$METADATA_FILE" '0:0:600' || return 1
  metadata_is "$PENDING_FILE" '0:0:600' || return 1
  [[ $(find "$TRANSACTION_DIR" -mindepth 1 -maxdepth 1 -printf x 2>/dev/null | wc -c) -eq 3 ]]
}

remove_transaction_artifacts() {
  # The directory has already been proven root-only mode 0700. Remove every
  # transaction file (including incomplete normalized copies) but never the dir.
  silent find "$TRANSACTION_DIR" -mindepth 1 -maxdepth 1 -exec rm -f -- {} +
}

assignment_count() {
  awk -v key="$PROVIDER_KEY" '
    /^[[:space:]]*#/ || /^[[:space:]]*$/ { next }
    $0 ~ "^[[:space:]]*" key "=" { count += 1 }
    END { print count + 0 }
  ' "$1" 2>/dev/null
}

candidate_is_claude() {
  [[ $(assignment_count "$1") == 1 ]] || return 1
  awk -v key="$PROVIDER_KEY" -v value="$PROVIDER_VALUE" '
    /^[[:space:]]*#/ || /^[[:space:]]*$/ { next }
    $0 ~ "^[[:space:]]*" key "=" {
      seen += 1
      if ($0 != key "=" value) exit 1
    }
    END { exit seen == 1 ? 0 : 1 }
  ' "$1" >/dev/null 2>&1
}

write_candidate() {
  awk -v key="$PROVIDER_KEY" -v value="$PROVIDER_VALUE" '
    /^[[:space:]]*#/ || /^[[:space:]]*$/ { print; next }
    $0 ~ "^[[:space:]]*" key "=" { print key "=" value; next }
    { print }
  ' "$1" >"$2" 2>/dev/null
}

write_normalized_copy() {
  awk -v key="$PROVIDER_KEY" -v sentinel="$SENTINEL" '
    /^[[:space:]]*#/ || /^[[:space:]]*$/ { next }
    $0 ~ "^[[:space:]]*" key "=" { print sentinel; next }
    { print }
  ' "$1" >"$2" 2>/dev/null
}

record_metadata() {
  local recorded=$1 tmp
  tmp=$(mktemp "$TRANSACTION_DIR/.metadata.XXXXXX" 2>/dev/null) || return 1
  printf '%s\n' "$recorded" >"$tmp" 2>/dev/null || return 1
  silent chown 0:0 "$tmp" || return 1
  silent chmod 600 "$tmp" || return 1
  silent mv -f -- "$tmp" "$METADATA_FILE"
}

read_metadata() {
  local recorded
  recorded=$(<"$METADATA_FILE") || return 1
  [[ $recorded =~ ^[0-9]+:[0-9]+:[0-7]{3,4}$ ]] || return 1
  ORIGINAL_METADATA=$recorded
}

copy_with_original_metadata() {
  local source=$1 tmp
  tmp=$(mktemp "$TRANSACTION_DIR/.protected.XXXXXX" 2>/dev/null) || return 1
  silent cp -- "$source" "$tmp" || return 1
  local uid gid mode
  IFS=: read -r uid gid mode <<<"$ORIGINAL_METADATA"
  silent chown "$uid:$gid" "$tmp" || return 1
  silent chmod "$mode" "$tmp" || return 1
  metadata_is "$tmp" "$ORIGINAL_METADATA" || return 1
  silent mv -f -- "$tmp" "$PROTECTED_FILE" || return 1
  metadata_is "$PROTECTED_FILE" "$ORIGINAL_METADATA"
}

create_backup() {
  local tmp
  tmp=$(mktemp "$TRANSACTION_DIR/.backup.XXXXXX" 2>/dev/null) || return 1
  silent install -m 600 /dev/null "$tmp" || return 1
  silent cp -- "$PROTECTED_FILE" "$tmp" || return 1
  silent chown 0:0 "$tmp" || return 1
  silent chmod 600 "$tmp" || return 1
  silent mv -f -- "$tmp" "$BACKUP_FILE"
}

apply() {
  local candidate normalized_backup normalized_candidate
  transaction_empty || return 1
  metadata_is "$PROTECTED_FILE" '0:0:600' || return 1
  [[ $(assignment_count "$PROTECTED_FILE") == 1 ]] || return 1
  ORIGINAL_METADATA=$(stat -c '%u:%g:%a' -- "$PROTECTED_FILE" 2>/dev/null) || return 1
  [[ $ORIGINAL_METADATA == '0:0:600' ]] || return 1

  create_backup || { remove_transaction_artifacts; return 1; }
  record_metadata "$ORIGINAL_METADATA" || { remove_transaction_artifacts; return 1; }
  candidate=$(mktemp "$TRANSACTION_DIR/.candidate.XXXXXX" 2>/dev/null) || { remove_transaction_artifacts; return 1; }
  normalized_backup=$(mktemp "$TRANSACTION_DIR/.backup-normalized.XXXXXX" 2>/dev/null) || { remove_transaction_artifacts; return 1; }
  normalized_candidate=$(mktemp "$TRANSACTION_DIR/.candidate-normalized.XXXXXX" 2>/dev/null) || { remove_transaction_artifacts; return 1; }
  silent chmod 600 "$candidate" "$normalized_backup" "$normalized_candidate" || { remove_transaction_artifacts; return 1; }
  write_candidate "$BACKUP_FILE" "$candidate" || { remove_transaction_artifacts; return 1; }
  candidate_is_claude "$candidate" || { remove_transaction_artifacts; return 1; }
  write_normalized_copy "$BACKUP_FILE" "$normalized_backup" || { remove_transaction_artifacts; return 1; }
  write_normalized_copy "$candidate" "$normalized_candidate" || { remove_transaction_artifacts; return 1; }
  silent cmp -s -- "$normalized_backup" "$normalized_candidate" || { remove_transaction_artifacts; return 1; }
  silent rm -f -- "$normalized_backup" "$normalized_candidate" || { remove_transaction_artifacts; return 1; }
  copy_with_original_metadata "$candidate" || { remove_transaction_artifacts; return 1; }
  if ! silent rm -f -- "$candidate" \
    || ! printf 'pending\n' >"$PENDING_FILE" 2>/dev/null \
    || ! silent chown 0:0 "$PENDING_FILE" \
    || ! silent chmod 600 "$PENDING_FILE"; then
    copy_with_original_metadata "$BACKUP_FILE" || return 1
    remove_transaction_artifacts || return 1
    return 1
  fi
  transaction_pending && return 0
  copy_with_original_metadata "$BACKUP_FILE" || return 1
  remove_transaction_artifacts || return 1
  return 1
}

restore() {
  transaction_pending || return 1
  read_metadata || return 1
  copy_with_original_metadata "$BACKUP_FILE" || return 1
  # Keep the private backup locked through the guarded-image retry. Cleanup is
  # the single terminal operation and refuses to run before health/status pass.
  printf 'restored\n' >"$PENDING_FILE" 2>/dev/null || return 1
  silent chown 0:0 "$PENDING_FILE" || return 1
  silent chmod 600 "$PENDING_FILE" || return 1
  transaction_pending
}

authenticated_post_deploy_checks() {
  local token health status_json
  token=$(awk -F= '/^[[:space:]]*SMARTPBX_WS_TOKEN=/{print substr($0, index($0, "=") + 1); exit}' "$PROTECTED_FILE" 2>/dev/null) || return 1
  [[ -n ${token//[[:space:]]/} ]] || return 1
  health=$(curl --silent --show-error --fail --connect-timeout 2 --max-time 5 http://127.0.0.1:8006/health 2>/dev/null) || return 1
  jq -e '.status == "ok" and .service_mode == "smartpbx"' >/dev/null 2>&1 <<<"$health" || return 1
  status_json=$(printf 'header = "X-Kavya-SmartPBX-Token: %s"\n' "$token" | curl --silent --show-error --fail --connect-timeout 2 --max-time 5 http://127.0.0.1:8006/smartpbx/status --config - 2>/dev/null) || return 1
  jq -e '.active_sessions == 0 and .transfer_enabled == false' >/dev/null 2>&1 <<<"$status_json"
}

cleanup() {
  transaction_pending || return 1
  authenticated_post_deploy_checks || return 1
  remove_transaction_artifacts || return 1
  transaction_empty
}

main() {
  exec 3>&2
  exec 2>/dev/null
  [[ $EUID -eq 0 ]] || { fail; return 1; }
  validate_interface "$@" || { fail; return 1; }
  lock_transaction_directory || { fail; return 1; }
  case $1 in
    apply) apply || { fail; return 1; }; status SMARTPBX_SINHALA_PROVIDER_APPLIED ;;
    restore) restore || { fail; return 1; }; status SMARTPBX_SINHALA_PROVIDER_RESTORED ;;
    cleanup) cleanup || { fail; return 1; }; status SMARTPBX_SINHALA_PROVIDER_CLEANED ;;
  esac
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
  main "$@"
fi
