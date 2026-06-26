# Flico Asterisk SIP Pilot

This stack is an isolated Asterisk pilot for routing a SIP softphone or SIP
trunk into the Flico voice agent through ARI External Media.

## Softphone Test Details

- SIP server: `67.207.90.109`
- Transport: UDP
- Port: `5060`
- Username/Auth ID: `1001`
- Password: replace `CHANGE_ME_SOFTPHONE_SECRET` in `config/pjsip.conf`
- Codec priority: PCMU/ulaw first
- DTMF: RFC2833/RFC4733
- Dial target: `7001`

## Language Menu

Softphone calls to `7001` and generic DID/trunk ingress through the
`flico-softphone` context now enter the same language IVR before ARI handoff:

- Press `1` for English: `Stasis(flico-sip-agent,en)`
- Press `2` for Tamil: `Stasis(flico-sip-agent,ta)`
- Press `3` for Sinhala: `Stasis(flico-sip-agent,si)`

The dialplan uses `Read(FLICO_LANG_CHOICE,custom/flico-language-menu,1,,3,8)`.
Install a matching prompt at `asterisk-flico/sounds/flico-language-menu.*`;
Docker mounts that directory to Asterisk sound path `custom/`. The prompt
should tell callers to press 1, 2, or 3; DTMF must be RFC2833/RFC4733.

Generate the prompt on the VPS with the existing Flico ElevenLabs voice:

```bash
cd /opt/asterisk-flico
python3 ops/generate-language-menu-prompt.py
docker compose up -d --force-recreate asterisk
```

After changing `extensions.conf` on a running pilot:

```text
asterisk -rx "dialplan reload"
asterisk -rx "dialplan show flico-softphone"
asterisk -rx "dialplan show flico-language-ivr"
```

## ARI

ARI listens inside the Asterisk container and is reachable only to containers on
the shared external Docker network `taskforceai-net`. The Flico ARI worker
connects to:

```text
http://flico-asterisk-pilot:8088/ari
ws://flico-asterisk-pilot:8088/ari/events?app=flico-sip-agent&api_key=flico_ari:<password>
```

Replace `CHANGE_ME_LONG_RANDOM` in `config/ari.conf` and copy the same value
to Flico's `ASTERISK_ARI_PASSWORD`.

SIP is published on UDP `5060`; public RTP for the softphone leg is published
on UDP `10000-10199`. Flico's own External Media listener stays on UDP
`18000-18100` inside Docker and is advertised as `flico-voice-agent`.
Keep SIP and RTP limited to the known pilot carrier, PBX, or tester IPs at the
VPS firewall/provider firewall. Do not expose placeholder SIP credentials or a
wide-open UDP `5060` service beyond the pilot window.

## Bring-Up

```bash
cd /opt/asterisk-flico
docker compose up -d
docker exec -it flico-asterisk-pilot asterisk -rvvv
```

Useful Asterisk CLI checks:

```text
http show status
ari show users
pjsip show endpoints
pjsip show contacts
dialplan show flico-softphone
dialplan show flico-language-ivr
```

Firewall helper:

```bash
sudo ./ops/flico-sip-firewall-apply.sh --dry-run
sudo ./ops/flico-sip-firewall-apply.sh
sudo systemctl status flico-sip-firewall
sudo iptables -S DOCKER-USER
sudo iptables -S FLICO-SIP-RTP
```

Keep trusted source IPs/CIDRs in `ops/allowed-sources.txt`. Use a DigitalOcean
Cloud Firewall as the outer control where possible. This helper protects the
Docker forwarding path on the host and can be reapplied after reboot with the
`ops/flico-sip-firewall.service` unit.

Rollback:

```bash
sudo systemctl disable --now flico-sip-firewall
sudo /opt/asterisk-flico/ops/flico-sip-firewall-rollback.sh
```

Do not run this stack with public SIP credentials left at the placeholder
values.

## Flico Runtime Status

With `ENABLE_ASTERISK_ARI=true`, Flico exposes an Asterisk pilot status endpoint
beside the normal health check:

```bash
curl http://127.0.0.1:8003/asterisk/status
```

Before a call, `active_calls` should be `0`. During a Zoiper call, it should
show one active call, the selected language, and the allocated External Media
RTP port. After hangup, `active_calls`, `sessions`, and used RTP ports should
return to zero.

Focused Zoiper QA:

1. Register extension `1001`.
2. Dial `7001` and confirm the IVR prompt starts quickly.
3. Press `1`, `2`, and `3` in separate calls to validate English, Tamil, and
   Sinhala routing.
4. Confirm agent audio is heard after each selection.
5. Check `/asterisk/status` during and after each call to confirm cleanup.

If a concurrency sanity check is needed, run only a tiny 3-call manual or SIPp
proof to validate cleanup. Do not treat it as a load benchmark.

## Tonight Pilot Runbook

1. Replace both placeholders:
   - `CHANGE_ME_LONG_RANDOM` in `config/ari.conf`
   - `CHANGE_ME_SOFTPHONE_SECRET` in `config/pjsip.conf`
2. Copy this directory to `/opt/asterisk-flico` on `67.207.90.109`.
3. In `/opt/flico/.env`, set:
   - `ENABLE_ASTERISK_ARI=true`
   - `ASTERISK_ARI_PASSWORD=<same value as config/ari.conf>`
   - `ASTERISK_ARI_URL=http://flico-asterisk-pilot:8088/ari`
   - `ASTERISK_RTP_ADVERTISE_HOST=flico-voice-agent`
4. Recreate only the Flico container after copying the updated Flico code:
   `cd /opt/flico && docker compose up -d --force-recreate flico`
5. Start only the Asterisk pilot:
   `cd /opt/asterisk-flico && docker compose up -d`
6. Register the softphone as extension `1001` and dial `7001`.

Rollback is to stop only the pilot stack and set `ENABLE_ASTERISK_ARI=false`.
Existing Twilio Flico/Kavya routes do not need to change.
