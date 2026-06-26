# Flico SIP/RTP Firewall Runbook

Use `flico-sip-firewall.sh` on the VPS before exposing the Asterisk pilot beyond
a controlled test window. It restricts Docker-published SIP/RTP ports in the
`DOCKER-USER` chain:

- UDP `5060` for SIP signaling
- UDP `10000:10199` for Asterisk public RTP

UFW alone is not enough for Docker-published ports. Docker's NAT and forwarding
rules can bypass host `INPUT` policy, so this runbook uses `DOCKER-USER`, which
Docker evaluates before forwarding traffic to published containers.

## Dry Run

From `/opt/asterisk-flico` on `67.207.90.109`:

```bash
sudo ./ops/flico-sip-firewall-apply.sh --dry-run
```

Edit `ops/allowed-sources.txt` to contain the exact carrier, PBX, VPN, office,
or tester source IPs/CIDRs. The helper does not auto-detect a personal IP. The
current pilot allowlist is:

```text
111.223.176.194
104.28.66.29
175.157.131.255
```

## Apply

```bash
sudo ./ops/flico-sip-firewall-apply.sh
```

When `ipset` is installed, each run replaces the members of the
`flico_sip_allowed` ipset with the sources passed on that command line, then
ensures these idempotent rules exist:

```text
ACCEPT udp dpt:5060 from flico_sip_allowed
ACCEPT udp dpts:10000:10199 from flico_sip_allowed
DROP   udp dpt:5060 from all other sources
DROP   udp dpts:10000:10199 from all other sources
```

If `ipset` is unavailable, the helper uses a managed `FLICO-SIP-RTP` chain
instead. The `DOCKER-USER` chain jumps into it for the pilot SIP/RTP ports, the
trusted sources return to Docker's normal forwarding path, and all other
sources are dropped.

## Reapply After Reboot

Install the one-shot systemd service after copying this directory to
`/opt/asterisk-flico`:

```bash
sudo install -m 0644 /opt/asterisk-flico/ops/flico-sip-firewall.service /etc/systemd/system/flico-sip-firewall.service
sudo chmod +x /opt/asterisk-flico/ops/flico-sip-firewall.sh /opt/asterisk-flico/ops/flico-sip-firewall-apply.sh /opt/asterisk-flico/ops/flico-sip-firewall-rollback.sh
sudo systemctl daemon-reload
sudo systemctl enable --now flico-sip-firewall
```

The service runs after `network-online.target` and `docker.service`, then
executes:

```bash
/opt/asterisk-flico/ops/flico-sip-firewall-apply.sh --sources-file /opt/asterisk-flico/ops/allowed-sources.txt
```

## Verify

```bash
systemctl status flico-sip-firewall
sudo ipset list flico_sip_allowed
sudo iptables -S DOCKER-USER
sudo iptables -S FLICO-SIP-RTP
```

Confirm the allow rules or jump rules appear before the drop rules. Keep
provider firewalls restricted too; this host-level helper is the Docker-aware
guardrail for the published pilot ports.

## Rollback

Disable the reapply service and remove the managed `DOCKER-USER` policy
artifacts:

```bash
sudo systemctl disable --now flico-sip-firewall
sudo rm -f /etc/systemd/system/flico-sip-firewall.service
sudo systemctl daemon-reload
sudo /opt/asterisk-flico/ops/flico-sip-firewall-rollback.sh
```
