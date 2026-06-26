#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCES_FILE="${SCRIPT_DIR}/allowed-sources.txt"
DRY_RUN=0

usage() {
  cat <<'USAGE'
Usage:
  sudo ./ops/flico-sip-firewall-apply.sh [--dry-run] [--sources-file PATH]

Read trusted SIP/RTP source IPs/CIDRs from a newline-delimited file and apply
the Flico Docker-aware firewall rules.

The source file may contain blank lines and # comments.
USAGE
}

trim() {
  local value="$1"
  value="${value#"${value%%[![:space:]]*}"}"
  value="${value%"${value##*[![:space:]]}"}"
  printf '%s' "$value"
}

while [[ "$#" -gt 0 ]]; do
  case "$1" in
    -h|--help)
      usage
      exit 0
      ;;
    -n|--dry-run)
      DRY_RUN=1
      shift
      ;;
    --sources-file)
      if [[ "$#" -lt 2 ]]; then
        printf '--sources-file requires a path\n' >&2
        exit 1
      fi
      SOURCES_FILE="$2"
      shift 2
      ;;
    *)
      printf 'Unknown option: %s\n' "$1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

if [[ ! -f "$SOURCES_FILE" ]]; then
  printf 'Allowed sources file not found: %s\n' "$SOURCES_FILE" >&2
  exit 1
fi

sources=()
while IFS= read -r line || [[ -n "$line" ]]; do
  line="${line%%#*}"
  line="$(trim "$line")"
  if [[ -n "$line" ]]; then
    sources+=("$line")
  fi
done < "$SOURCES_FILE"

if [[ "${#sources[@]}" -eq 0 ]]; then
  printf 'No allowed sources found in %s\n' "$SOURCES_FILE" >&2
  exit 1
fi

args=()
if [[ "$DRY_RUN" -eq 1 ]]; then
  args+=(--dry-run)
fi

exec "${SCRIPT_DIR}/flico-sip-firewall.sh" "${args[@]}" "${sources[@]}"
