# Kavya Dialog SmartPBX cutover runbook

This runbook operates the isolated `kavya-smartpbx` Compose profile. It does
not replace or modify the existing `kavya` Twilio service. The profile is
disabled by default and has no public Docker port: its application listens only
at `127.0.0.1:8006`, behind the dedicated TLS Nginx host.

## Preconditions and server configuration

1. Provision DNS and a valid TLS certificate for
   `smartpbx-kavya.taskforceai.tech`, then install `nginx-smartpbx.conf` as the
   dedicated Nginx virtual host. Validate Nginx before reload.
2. Generate a unique WebSocket token on the server. Never reuse a Twilio,
   dashboard, MCP, or API credential:

   ```sh
   /home/dev/full-voice-agent/.venv/bin/python -c 'import secrets; print(secrets.token_urlsafe(32))'
   ```

3. In the server-only `Kavya/.env`, set fresh values for
   `SMARTPBX_WS_TOKEN` and `SMARTPBX_ACCOUNT_ID`. Keep
   `ENABLE_SMARTPBX_WSS=false` in the shared file: the isolated Compose profile
   sets it to `true` only for its own container. Do not add these values to a
   dashboard, browser, client source, or Nginx configuration.
4. Start only the isolated profile after the preceding checks:

   ```sh
   docker compose --profile smartpbx up -d kavya-smartpbx
   curl --fail https://smartpbx-kavya.taskforceai.tech/health
   ```

## Dialog dashboard fields

Create or update the Dialog AI-provider connection with exactly these values:

| Field | Value |
| --- | --- |
| Media WebSocket URL | `wss://smartpbx-kavya.taskforceai.tech/ws/v1/smartpbx/media` |
| WebSocket header name | `X-Kavya-SmartPBX-Token` |
| WebSocket header value | the fresh `SMARTPBX_WS_TOKEN` only |
| Account ID in Dialog start event | the server `SMARTPBX_ACCOUNT_ID` |
| Audio encoding | `g711_ulaw` |
| Sample rate | `8000` Hz |
| Maximum concurrent calls | `4` |

The dashboard WSS headers contain only `X-Kavya-SmartPBX-Token`. API/account
credentials are server-only and must not be added to dashboard WSS headers:
that includes `SMARTPBX_API_KEY`, any MCP endpoint credential, and transfer
destinations.

## MCP transfer boundary (disabled until deliberately enabled)

Transfer is transfer-disabled with the checked-in example configuration:
`SMARTPBX_TRANSFER_DESTINATIONS_JSON={}`. It remains disabled unless all of
the following are server-only values: `SMARTPBX_MCP_URL`, `SMARTPBX_API_KEY`,
`SMARTPBX_ACCOUNT_ID`, `SMARTPBX_MCP_ACCOUNT_HEADER`, and a non-empty approved
destination map.

The Dialog MCP URL shape is `https://<dialog-mcp-host>/ucp/v2/mcp`. The explicit
account header choice is `SMARTPBX_MCP_ACCOUNT_HEADER=account_id`. At runtime
send only that one account header; do not also send `X-Account-ID`.

Before enabling transfer, obtain and record from Dialog:

- the production MCP URL and API key;
- the account ID and confirmation that the exact account header is `account_id`;
- an approved non-production test destination, formatted `tel:+<digits>` or
  `sip:<user>@<host>`;
- each approved production destination in the same format, with its logical
  destination key and business owner.

Populate `SMARTPBX_TRANSFER_DESTINATIONS_JSON` only with those approved
destinations, for example `{"human_support":"tel:+<approved-test-number>"}`.
Never use an unapproved number in a production map.

## Test call and non-production transfer drill

1. With transfer still disabled, place one Dialog test call. Confirm the TLS
   connection reaches the WSS path, the `g711_ulaw`/`8000` media session starts,
   the agent speaks, and `https://smartpbx-kavya.taskforceai.tech/smartpbx/status`
   returns to zero active sessions after hangup.
2. For a non-production transfer drill, obtain written approval, use a
   non-production Dialog account and the approved test destination, and set all
   required server-only MCP values. Place one supervised test call, invoke the
   logical destination once, and verify the Dialog leg—not a caller-supplied
   number—was transferred.
3. Remove the temporary destination or restore `{}` immediately after the drill
   unless a separately approved production cutover has occurred. Record the
   call ID, operator, outcome, and rollback owner without recording credentials.

## Cutover and rollback

For cutover, enable the Dialog dashboard connection only after the test call
passes. Continue to leave the existing `kavya` Twilio service running; no
production deployment mutation is part of this profile addition.

To roll back, disable the Dialog dashboard connection first, then run:

```sh
docker compose --profile smartpbx stop kavya-smartpbx
```

Remove any temporary MCP destinations from the server configuration and restore
`SMARTPBX_TRANSFER_DESTINATIONS_JSON={}`. Do not stop, edit, restart, or route
Flico through this service; Flico is not part of this deployment or rollback.
