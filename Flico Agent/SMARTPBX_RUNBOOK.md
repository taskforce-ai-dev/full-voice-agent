# Dialog SmartPBX WSS runbook

This runbook prepares an opt-in, isolated WSS process. It does **not** mean DNS,
TLS, a Dialog dashboard entry, or carrier routing is configured. The candidate
endpoint is `wss://smartpbx-flico.taskforceai.tech/ws/v1/smartpbx/media` only
after every operator gate below has been completed.

## Operator gates and preflight

Do not start the profile until the on-call operator has recorded each of these
gates as complete:

1. **DNS and TLS:** create the `smartpbx-flico.taskforceai.tech` DNS record,
   issue and install its certificate, and validate the Nginx configuration. This
   repository supplies an example; it does not configure either system.
2. **Rotated API key:** the Dialog API key previously exposed outside the secret
   store must be revoked and never reused. Generate a new least-privilege key,
   store it only in the deployment `.env`, and set `SMARTPBX_API_KEY` there.
3. **WSS token:** generate a unique high-entropy token, for example
   `openssl rand -hex 32`, store it as `SMARTPBX_WS_TOKEN`, and rotate it by
   replacing the value and recreating the SmartPBX container. Do not put it in
   a shell history, ticket, dashboard note, log, or committed file.
4. **Dialog account and transfer policy:** obtain the production account ID,
   account-header name, allowed transfer destinations JSON, and a transfer URI
   approved by the telephony owner. Leave transfer destinations `{}` until that
   approval exists.
5. **Network source IPs:** obtain the Dialog egress/source IPs and constrain
   the perimeter firewall to TCP 443 from those source IPs. The loopback
   application port `8005` must never be public.
6. **Dashboard update:** create the Dialog dashboard media-stream entry with
   the exact candidate URL and WSS token only after TLS passes. This runbook
   does not claim that dashboard update has occurred.
7. **Carrier fallback:** approve and schedule the endpoint-down carrier
   fallback test before enabling production routing.

Before every launch, run the focused deployment contract test plus the Compose
and Nginx checks shown under validation. Confirm `IMAGE_TAG` is an immutable
release SHA, the host has the certificate paths referenced by
`nginx-smartpbx.conf`, and the service profile is intentionally selected.

## Start, stop, and rollback

From the `Flico Agent` directory on the deployment host:

```bash
IMAGE_TAG=<immutable-sha> docker compose --profile smartpbx pull flico-smartpbx
IMAGE_TAG=<immutable-sha> docker compose --profile smartpbx up -d flico-smartpbx
docker compose --profile smartpbx ps
```

An ordinary `docker compose up -d` does not start this service. To stop it
without affecting the existing voice-agent container:

```bash
docker compose --profile smartpbx stop flico-smartpbx
docker compose --profile smartpbx rm -f flico-smartpbx
```

Rollback is an immediate profile stop. If the issue is image-specific, use the
last known immutable SHA and recreate only `flico-smartpbx`; do not roll back
the legacy `flico` service as part of this procedure.

## Status, logs, and safe observability

On the host, verify the loopback health endpoint and container state:

```bash
curl --fail http://127.0.0.1:8005/health
curl --fail http://127.0.0.1:8005/smartpbx/status
docker compose --profile smartpbx ps flico-smartpbx
docker compose --profile smartpbx logs --since=15m --tail=200 flico-smartpbx
```

The Nginx example disables access logs because WSS headers can carry a token.
Use the bounded container logs for event names and failure classes only; do not
copy request headers, tokens, audio, transcript content, or API keys into an
incident record.

## Synthetic WSS smoke test

Before changing any dashboard routing, use fake non-production IDs and the new
WSS token. Send the smallest valid protocol `start` event, a short `media`
event, and `stop`; confirm the service accepts the connection and emits no
secret-bearing logs. Never use a real caller ID, live account ID, or production
recording in this smoke test. If an external WSS client is unavailable, keep the
dashboard gate open and do not claim the endpoint is verified.

## Capacity, transfer, and failure drills

1. Run a four-call capacity test using fake non-production IDs. Confirm four
   concurrent sessions are accepted and a fifth is rejected without destabilizing
   the first four. Stop all synthetic calls afterward.
2. Run an MCP transfer test with the approved non-production transfer URI and a
   test destination in `SMARTPBX_TRANSFER_DESTINATIONS_JSON`. Verify one
   allowlisted transfer reaches the expected test endpoint, then remove the
   temporary destination if it is not part of production policy.
3. Run the mandatory endpoint-down carrier fallback drill: stop
   `flico-smartpbx` (or block the WSS route under a controlled maintenance
   window), place a synthetic carrier call, confirm the carrier uses its
   documented fallback rather than retrying indefinitely, restore the service,
   and repeat the health/status checks. Record the result before enabling live
   callers.

## Validation before a change window

```bash
/home/dev/full-voice-agent/.venv/bin/python -m pytest tests/test_smartpbx_deployment.py -q
docker compose -f docker-compose.yml config
nginx -t -c "$PWD/nginx-smartpbx.conf"
```

Run the last two commands on a host with Docker Compose and Nginx installed;
they are launch-preflight gates, not checks this repository can perform by
itself.

Never mark either validation as passed when the required host tool or certificate is unavailable.
