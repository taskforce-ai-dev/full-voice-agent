#!/usr/bin/env bash
set -euo pipefail

IPSET_NAME="${IPSET_NAME:-flico_sip_allowed}"
CHAIN_NAME="${CHAIN_NAME:-FLICO-SIP-RTP}"

if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
  printf 'Run as root so iptables and ipset can be updated.\n' >&2
  exit 1
fi

delete_rule() {
  while iptables -D DOCKER-USER "$@" >/dev/null 2>&1; do
    :
  done
}

delete_rule -p udp -m set --match-set "$IPSET_NAME" src -m udp --dport 5060 -m comment --comment flico-sip-allow -j ACCEPT
delete_rule -p udp -m set --match-set "$IPSET_NAME" src -m udp --dport 10000:10199 -m comment --comment flico-rtp-allow -j ACCEPT
delete_rule -p udp -m udp --dport 5060 -m comment --comment flico-sip-drop -j DROP
delete_rule -p udp -m udp --dport 10000:10199 -m comment --comment flico-rtp-drop -j DROP

delete_rule -p udp -m udp --dport 5060 -m comment --comment flico-sip-filter -j "$CHAIN_NAME"
delete_rule -p udp -m udp --dport 10000:10199 -m comment --comment flico-rtp-filter -j "$CHAIN_NAME"

iptables -F "$CHAIN_NAME" >/dev/null 2>&1 || true
iptables -X "$CHAIN_NAME" >/dev/null 2>&1 || true

if command -v ipset >/dev/null 2>&1; then
  ipset destroy "$IPSET_NAME" >/dev/null 2>&1 || true
fi

printf 'Removed Flico SIP/RTP DOCKER-USER policy artifacts.\n' >&2
