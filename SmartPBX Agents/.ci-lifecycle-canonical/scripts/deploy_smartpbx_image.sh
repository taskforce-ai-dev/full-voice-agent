#!/bin/sh
# REVIEW-ONLY provenance gate. This helper intentionally cannot change runtime state.
set -eu

image_ref=${1:-}
source_revision=${2:-}
expected_digest=${3:-}

case "$image_ref" in *@sha256:[0-9a-f][0-9a-f]*) ;; *) printf '%s\n' 'immutable image reference required' >&2; exit 2 ;; esac
case "$source_revision" in ????????????????????????????????????????) ;; *) printf '%s\n' 'full source revision required' >&2; exit 2 ;; esac
case "$expected_digest" in sha256:[0-9a-f][0-9a-f]*) ;; *) printf '%s\n' 'immutable digest required' >&2; exit 2 ;; esac

printf '%s\n' 'REVIEW-ONLY: provenance shape accepted; runtime changes remain disabled.' >&2
exit 1
