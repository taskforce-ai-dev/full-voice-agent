#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 || -z "$1" ]]; then
  echo "image_tag_state=probe_failed"
  exit 1
fi

captured_error=$(mktemp)
trap "rm -f \"$captured_error\"" EXIT
if docker manifest inspect "$1" >"$captured_error" 2>&1; then
  echo "image_tag_state=existing"
  exit 10
fi

registry_error=$(<"$captured_error")
registry_error=${registry_error,,}
if [[ "$registry_error" == *"manifest unknown"* || "$registry_error" == *"no such manifest"* || "$registry_error" == *"registry"*"404"* || "$registry_error" == *"404"*"registry"* ]]; then
  echo "image_tag_state=absent"
  exit 0
fi

echo "image_tag_state=probe_failed"
exit 1
