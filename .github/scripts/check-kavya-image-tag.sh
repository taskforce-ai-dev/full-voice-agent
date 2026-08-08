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
if [[ "$registry_error" == *"denied"* || "$registry_error" == *"unauthorized"* || "$registry_error" == *"forbidden"* || "$registry_error" == *"auth"* || "$registry_error" == *"token"* || "$registry_error" == *"proxy"* || "$registry_error" == *"timeout"* || "$registry_error" == *"timed out"* || "$registry_error" == *"network"* || "$registry_error" == *"dns"* || "$registry_error" == *"dial tcp"* || "$registry_error" == *"no such host"* || "$registry_error" == *"429"* || "$registry_error" == *5[0-9][0-9]* ]]; then
  echo "image_tag_state=probe_failed"
  exit 1
fi

if [[ "$registry_error" == "manifest unknown: $TAG" || "$registry_error" == "no such manifest: $TAG" || "$registry_error" == "failed to resolve source metadata for $TAG: not found" ]]; then
  echo "image_tag_state=absent"
  exit 0
fi

echo "image_tag_state=probe_failed"
exit 1
