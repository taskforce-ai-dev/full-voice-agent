#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 || -z "$1" ]]; then
  echo "image_tag_state=probe_failed"
  exit 1
fi

TAG=${1,,}
captured_error=$(mktemp)
trap 'rm -f "$captured_error"' EXIT
if docker buildx imagetools inspect "$TAG" >"$captured_error" 2>&1; then
  echo "image_tag_state=existing"
  exit 10
fi

registry_error=$(<"$captured_error")
registry_error=${registry_error,,}
# Absent is claimed only on an exact allowlist match. Every other capture --
# authorization, network, throttling, server, and unrecognised errors alike --
# falls through to the fail-closed default below. Deny-list globs are
# deliberately absent: an unanchored match against the whole capture also
# matches the image reference, which carries caller-independent digit runs.
if [[ "$registry_error" == "manifest unknown: $TAG" || "$registry_error" == "no such manifest: $TAG" || "$registry_error" == "failed to resolve source metadata for $TAG: not found" ]]; then
  echo "image_tag_state=absent"
  exit 0
fi

echo "image_tag_state=probe_failed"
exit 1
