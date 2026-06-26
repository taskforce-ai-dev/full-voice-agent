#!/usr/bin/env bash
set -euo pipefail

IPSET_NAME="flico_sip_allowed"
CHAIN_NAME="FLICO-SIP-RTP"
DRY_RUN=0

usage() {
  cat <<'USAGE'
Usage:
  sudo ./ops/flico-sip-firewall.sh [--dry-run] [--ipset NAME] [--chain NAME] <allowed-cidr-or-ip> [...]

Restrict Docker-published Flico Asterisk SIP/RTP pilot ports to explicit sources.

Required arguments:
  <allowed-cidr-or-ip>  One or more trusted source IPs/CIDRs, for example:
                        203.0.113.10 198.51.100.0/24

Options:
  -n, --dry-run         Print the ipset/iptables commands without applying them.
  --ipset NAME          Override the ipset name. Default: flico_sip_allowed.
  --chain NAME          Override fallback chain name. Default: FLICO-SIP-RTP.
  -h, --help            Show this help.

Ports protected in the DOCKER-USER chain:
  UDP 5060              SIP signaling
  UDP 10000:10199       Asterisk public RTP media range

Warning:
  UFW alone is not enough for Docker-published ports. Docker installs NAT and
  forwarding rules that can bypass host INPUT-chain policy. This helper uses
  DOCKER-USER, the chain Docker provides for operator firewall policy before
  container forwarding. If ipset is installed, the helper uses it. Otherwise it
  falls back to a dedicated iptables chain with one allow rule per source.

This script does not auto-detect, assume, or hardcode your personal IP. Pass
every allowed carrier, PBX, office, VPN, or tester source explicitly.
USAGE
}

quote_cmd() {
  printf '%q ' "$@"
  printf '\n'
}

run() {
  if [[ "$DRY_RUN" -eq 1 ]]; then
    quote_cmd "$@"
  else
    "$@"
  fi
}

need_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    printf 'Missing required command: %s\n' "$1" >&2
    exit 1
  fi
}

validate_ipset_name() {
  if [[ ! "$IPSET_NAME" =~ ^[A-Za-z0-9_.:-]+$ ]]; then
    printf 'Invalid ipset name: %s\n' "$IPSET_NAME" >&2
    exit 1
  fi
}

validate_chain_name() {
  if [[ ! "$CHAIN_NAME" =~ ^[A-Za-z0-9_.:-]+$ ]]; then
    printf 'Invalid chain name: %s\n' "$CHAIN_NAME" >&2
    exit 1
  fi
}

validate_source() {
  local source="$1"

  if [[ "$source" == *:* ]]; then
    printf 'IPv6 source is not supported by this pilot helper: %s\n' "$source" >&2
    exit 1
  fi

  if [[ ! "$source" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}(/[0-9]{1,2})?$ ]]; then
    printf 'Source must be an IPv4 address or CIDR: %s\n' "$source" >&2
    exit 1
  fi

  local address="${source%%/*}"
  local prefix=""
  if [[ "$source" == */* ]]; then
    prefix="${source##*/}"
    if (( 10#$prefix < 0 || 10#$prefix > 32 )); then
      printf 'CIDR prefix must be between 0 and 32: %s\n' "$source" >&2
      exit 1
    fi
  fi

  IFS='.' read -r o1 o2 o3 o4 <<<"$address"
  for octet in "$o1" "$o2" "$o3" "$o4"; do
    if (( 10#$octet < 0 || 10#$octet > 255 )); then
      printf 'IPv4 octet out of range in source: %s\n' "$source" >&2
      exit 1
    fi
  done
}

ensure_rule() {
  local position="$1"
  shift

  if [[ "$DRY_RUN" -eq 1 ]]; then
    quote_cmd iptables -C DOCKER-USER "$@"
    quote_cmd iptables -I DOCKER-USER "$position" "$@"
    return
  fi

  if iptables -C DOCKER-USER "$@" >/dev/null 2>&1; then
    return
  fi

  run iptables -I DOCKER-USER "$position" "$@"
}

ensure_docker_user_jump() {
  local position="$1"
  shift

  if [[ "$DRY_RUN" -eq 1 ]]; then
    quote_cmd iptables -C DOCKER-USER "$@"
    quote_cmd iptables -I DOCKER-USER "$position" "$@"
    return
  fi

  if iptables -C DOCKER-USER "$@" >/dev/null 2>&1; then
    return
  fi

  run iptables -I DOCKER-USER "$position" "$@"
}

apply_with_ipset() {
  tmp_set="${IPSET_NAME}_next_$$"

  run ipset create "$tmp_set" hash:net family inet -exist
  run ipset flush "$tmp_set"
  for source in "${sources[@]}"; do
    run ipset add "$tmp_set" "$source" -exist
  done

  run ipset create "$IPSET_NAME" hash:net family inet -exist
  run ipset swap "$tmp_set" "$IPSET_NAME"
  run ipset destroy "$tmp_set"

  ensure_rule 1 -p udp -m set --match-set "$IPSET_NAME" src -m udp --dport 5060 -m comment --comment flico-sip-allow -j ACCEPT
  ensure_rule 2 -p udp -m set --match-set "$IPSET_NAME" src -m udp --dport 10000:10199 -m comment --comment flico-rtp-allow -j ACCEPT
  ensure_rule 3 -p udp -m udp --dport 5060 -m comment --comment flico-sip-drop -j DROP
  ensure_rule 4 -p udp -m udp --dport 10000:10199 -m comment --comment flico-rtp-drop -j DROP
}

apply_with_chain() {
  run iptables -N "$CHAIN_NAME" 2>/dev/null || true
  run iptables -F "$CHAIN_NAME"

  for source in "${sources[@]}"; do
    run iptables -A "$CHAIN_NAME" -s "$source" -m comment --comment flico-allow-source -j RETURN
  done
  run iptables -A "$CHAIN_NAME" -m comment --comment flico-drop-untrusted -j DROP

  ensure_docker_user_jump 1 -p udp -m udp --dport 5060 -m comment --comment flico-sip-filter -j "$CHAIN_NAME"
  ensure_docker_user_jump 2 -p udp -m udp --dport 10000:10199 -m comment --comment flico-rtp-filter -j "$CHAIN_NAME"
}

sources=()
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
    --ipset)
      if [[ "$#" -lt 2 ]]; then
        printf '--ipset requires a name\n' >&2
        exit 1
      fi
      IPSET_NAME="$2"
      shift 2
      ;;
    --chain)
      if [[ "$#" -lt 2 ]]; then
        printf '--chain requires a name\n' >&2
        exit 1
      fi
      CHAIN_NAME="$2"
      shift 2
      ;;
    --)
      shift
      while [[ "$#" -gt 0 ]]; do
        sources+=("$1")
        shift
      done
      ;;
    -*)
      printf 'Unknown option: %s\n' "$1" >&2
      usage >&2
      exit 1
      ;;
    *)
      sources+=("$1")
      shift
      ;;
  esac
done

if [[ "${#sources[@]}" -eq 0 ]]; then
  printf 'At least one allowed source IP/CIDR is required.\n\n' >&2
  usage >&2
  exit 1
fi

validate_ipset_name
validate_chain_name
for source in "${sources[@]}"; do
  validate_source "$source"
done

if [[ "$DRY_RUN" -ne 1 && "${EUID:-$(id -u)}" -ne 0 ]]; then
  printf 'Run as root so ipset and iptables can be updated.\n' >&2
  exit 1
fi

if [[ "$DRY_RUN" -ne 1 ]]; then
  need_cmd iptables
fi

printf 'WARNING: UFW alone is not enough for Docker-published ports; applying DOCKER-USER policy.\n' >&2
printf 'Allowed SIP/RTP sources:\n' >&2
for source in "${sources[@]}"; do
  printf '  - %s\n' "$source" >&2
done

if command -v ipset >/dev/null 2>&1; then
  printf 'Using ipset policy set: %s\n' "$IPSET_NAME" >&2
  apply_with_ipset
else
  printf 'ipset is unavailable; using fallback chain: %s\n' "$CHAIN_NAME" >&2
  apply_with_chain
fi

printf 'Flico Asterisk SIP/RTP DOCKER-USER rules are installed.\n' >&2
