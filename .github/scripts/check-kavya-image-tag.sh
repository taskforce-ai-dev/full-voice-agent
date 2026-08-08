#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 || -z "$1" ]]; then
  echo "image_tag_state=probe_failed"
  exit 1
fi

# Both callers pass an already-lowercase image reference. A mixed-case argument
# is rejected rather than case-folded, so nothing can reach the exact-match
# allowlist below by way of case normalisation.
TAG=$1
if [[ "$TAG" != "${TAG,,}" ]]; then
  echo "image_tag_state=probe_failed"
  exit 1
fi

captured_error=$(mktemp)
trap 'rm -f "$captured_error"' EXIT
docker_status=0
docker buildx imagetools inspect "$TAG" >"$captured_error" 2>&1 || docker_status=$?

# Bash command substitution cannot preserve NUL bytes, so a NUL-bearing capture
# could be silently reduced to an exact absent-message match. Detect it
# byte-safely before returning existing or loading the capture into a variable.
capture_bytes=$(LC_ALL=C od -An -tx1 -v "$captured_error") || {
  echo "image_tag_state=probe_failed"
  exit 1
}
if [[ " ${capture_bytes//$'\n'/ } " == *" 00 "* ]]; then
  echo "image_tag_state=probe_failed"
  exit 1
fi

if [[ "$docker_status" -eq 0 ]]; then
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
# "error: $TAG: not found" is the wording GHCR actually returned to an
# authenticated runner for a missing tag, observed 2026-08-08 in run
# https://github.com/taskforce-ai-dev/full-voice-agent/actions/runs/31269079259
# (buildx prints "ERROR: %v"; this is the case-folded whole capture).
if [[ "$registry_error" == "manifest unknown: $TAG" || "$registry_error" == "no such manifest: $TAG" || "$registry_error" == "failed to resolve source metadata for $TAG: not found" || "$registry_error" == "error: $TAG: not found" ]]; then
  echo "image_tag_state=absent"
  exit 0
fi

# Exit 2 is reserved for "the registry said something this allowlist does not
# cover". It is distinct from the exit 1 rejections above so a failed run states
# which remedy applies: read the real capture from the run and widen the
# allowlist in a reviewed change. Never widen by substring. Every caller still
# treats this as a hard failure.
echo "image_tag_state=probe_unrecognized"
exit 2
