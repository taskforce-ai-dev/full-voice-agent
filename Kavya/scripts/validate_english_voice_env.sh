#!/bin/sh
set -eu

key=KAVYA_EN_ELEVENLABS_VOICE_ID
first_value=
second_value=

cleanup() {
    unset first_value second_value key
}
trap cleanup EXIT HUP INT TERM

read_value() {
    env_file=$1
    [ -r "$env_file" ] || return 1
    value=$(awk -F= -v expected_key="$key" '
        $1 == expected_key {
            if (seen) {
                exit 2
            }
            seen = 1
            value = substr($0, index($0, "=") + 1)
        }
        END {
            if (!seen) {
                exit 1
            }
            print value
        }
    ' "$env_file") || return 1
    case "$value" in
        *[![:space:]]*) printf '%s' "$value" ;;
        *) return 1 ;;
    esac
}

[ "$#" -eq 2 ] || exit 64
first_value=$(read_value "$1") || exit 1
second_value=$(read_value "$2") || exit 1
[ "$first_value" = "$second_value" ] || exit 1
printf '%s\n' 'canonical_voice_match=ok'
