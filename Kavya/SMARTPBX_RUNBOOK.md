# Kavya Dialog SmartPBX cutover runbook

This runbook operates only the opt-in `kavya-smartpbx` profile at `/opt/kavya`.
It leaves the existing `kavya` Twilio service unchanged and keeps Flico untouched:
do not stop, edit, restart, or route Flico through this service.

## Preconditions

- Dialog has confirmed the WSS account ID. Kavya accepts either `account_id` or
  `X-Account-ID` for MCP, but Dialog vendor docs conflict: a tenant must confirm
  and credentialedly test exactly one spelling before optional transfer activation.
  If Dialog supplies egress IPs, its source-IP allowlist is an operator prerequisite.
- DNS and a certificate exist for `smartpbx-kavya.taskforceai.tech`.
- Replace `<REVIEWED_COMMIT_SHA>` with the immutable CI-built SHA containing this
  change. Never use `latest`.
- The 7.8 GiB VPS must retain observed headroom for legacy Twilio, Nginx, Docker,
  and the host after SmartPBX's `1536m`, `2.0` CPU, and `256` PID caps.

## Create the isolated server-side environment

```sh
cd /opt/kavya
umask 077
touch /opt/kavya/.env.smartpbx
chmod 600 /opt/kavya/.env.smartpbx
openssl rand -hex 32
```

Paste the generated value only as `SMARTPBX_WS_TOKEN` in the protected file.
Populate every line below from approved server-side secrets; do not copy `.env`,
and do not add Twilio credentials or `HUMAN_AGENT_PHONE`.

```dotenv
# LLM and TTS
ANTHROPIC_API_KEY=
OPENAI_API_KEY=
LLM_PROVIDER=claude
CLAUDE_MODEL=claude-sonnet-4-20250514
OPENAI_MODEL=gpt-4o
GEMINI_API_KEY=
GEMINI_MODEL=gemini-2.5-flash
OPENAI_TTS_MODEL=gpt-4o-mini-tts
OPENAI_TTS_VOICE=nova
OPENAI_TTS_INSTRUCTIONS=
ELEVENLABS_API_KEY=
ELEVENLABS_VOICE_ID=
ELEVENLABS_VOICE_ID_AR=
# STT and PMS
STT_PROVIDER=azure
AZURE_SPEECH_KEY=
AZURE_SPEECH_REGION=southeastasia
YANOLJA_BASE_URL=
YANOLJA_USERNAME=
YANOLJA_PASSWORD=
YANOLJA_TIMEOUT=30
DEMO_RATES_ENABLED=true
# Post-call, dashboard, and observability
N8N_BASE_URL=
N8N_POSTCALL_WEBHOOK=/webhook/post-call-data
DASHBOARD_API_URL=
DASHBOARD_API_KEY=
DASHBOARD_AGENT_ID=kavya
SENTRY_DSN=
SENTRY_TRACES_SAMPLE_RATE=0.0
SENTRY_ENV=production
# SmartPBX ingress and manager notification
SMARTPBX_WS_TOKEN=
SMARTPBX_ACCOUNT_ID=
SMARTPBX_MAX_MESSAGE_CHARS=65536
SMARTPBX_MAX_AUDIO_BYTES=32768
SMARTPBX_MAX_OUTBOUND_FRAMES=128
SMARTPBX_START_TIMEOUT_SECONDS=10
SMARTPBX_IDLE_TIMEOUT_SECONDS=90
SMARTPBX_HUMAN_AGENT_WHATSAPP=
# Dialog MCP (leave destinations {} to keep transfer disabled)
SMARTPBX_MCP_URL=https://dialog.cybergate.lk:9443/ucp/v2/mcp
SMARTPBX_API_KEY=
SMARTPBX_MCP_ACCOUNT_HEADER=
SMARTPBX_TRANSFER_DESTINATIONS_JSON={}
SMARTPBX_MCP_CONNECT_TIMEOUT_SECONDS=5
SMARTPBX_MCP_READ_TIMEOUT_SECONDS=15
SMARTPBX_MCP_MAX_RESPONSE_BYTES=1048576
SMARTPBX_MCP_RETRIES=1
```

## MCP header and dashboard boundary

Kavya accepts `SMARTPBX_MCP_ACCOUNT_HEADER=account_id` and
`SMARTPBX_MCP_ACCOUNT_HEADER=X-Account-ID`. Dialog vendor docs conflict, so a
tenant must credentialedly confirm and test exactly one spelling in
`.env.smartpbx`; never send both headers. MCP API/account headers and all MCP
credentials remain server-only. The dashboard WSS headers carry only the
dedicated WSS token; they never carry MCP credentials.

Only the dedicated WSS token is pasted into the Dialog dashboard. Configure:

| Field | Value |
| --- | --- |
| Media WebSocket URL | `wss://smartpbx-kavya.taskforceai.tech/ws/v1/smartpbx/media` |
| WebSocket header name | `X-Kavya-SmartPBX-Token` |
| WebSocket header value | the `SMARTPBX_WS_TOKEN` value only |
| Account ID in start event | `SMARTPBX_ACCOUNT_ID` |
| Audio encoding / sample rate | `g711_ulaw` / `8000` Hz |
| Maximum concurrent calls | `4` |

Do not paste the MCP URL, API key, account ID/header, destinations, or other
server value into dashboard WSS headers.

## Nginx and immutable-profile preflight

```sh
cd /opt/kavya
sudo install -m 0644 nginx-smartpbx.conf /etc/nginx/sites-available/kavya-smartpbx
sudo ln -sfn /etc/nginx/sites-available/kavya-smartpbx /etc/nginx/sites-enabled/kavya-smartpbx
sudo nginx -t
sudo systemctl reload nginx
SMARTPBX_IMAGE_TAG=<REVIEWED_COMMIT_SHA> docker compose --env-file .env.smartpbx --profile smartpbx config > /dev/null
SMARTPBX_IMAGE_TAG=<REVIEWED_COMMIT_SHA> docker compose --env-file .env.smartpbx --profile smartpbx pull kavya-smartpbx
SMARTPBX_IMAGE_TAG=<REVIEWED_COMMIT_SHA> docker compose --env-file .env.smartpbx --profile smartpbx up -d kavya-smartpbx
SMARTPBX_IMAGE_TAG=<REVIEWED_COMMIT_SHA> docker compose --env-file .env.smartpbx --profile smartpbx ps
SMARTPBX_IMAGE_TAG=<REVIEWED_COMMIT_SHA> docker compose --env-file .env.smartpbx --profile smartpbx logs --tail=100 kavya-smartpbx
curl --fail https://smartpbx-kavya.taskforceai.tech/health
curl --fail https://smartpbx-kavya.taskforceai.tech/smartpbx/status
```

Use the identical pin for both `pull` and `up`. The config check intentionally
does not print rendered secrets. Do not continue on a config failure, public
Docker port, or unreviewed tag.

## Cutover gates

Before enabling the Dialog route, record privacy-safe call fingerprints and outcomes, never raw call IDs or credentials:

1. Bad/missing WSS auth is rejected.
2. A real or synthetic bidirectional call proves caller audio reaches STT, an
   LLM turn completes, and the caller receives the response.
3. Exercise a KB answer and representative PMS tool, then verify a post-call
   record reaches dashboard/webhook.
4. Hold four authenticated calls: **4 accepted + 5th rejected**, then hang up
   and verify `/smartpbx/status` returns zero active sessions.
5. Test endpoint-down behavior and verify the carrier/dashboard fallback reaches
   the approved operator without a caller-supplied destination.

## Optional transfer activation

Base WSS voice cutover does not require MCP credentials or a destination map:
`SMARTPBX_TRANSFER_DESTINATIONS_JSON={}` is valid and keeps transfer-disabled
behavior. Activate transfer only after Dialog confirms the MCP endpoint/API key
and the exact one account-header spelling. For a supervised non-production transfer drill,
approve one non-production destination, make one observed transfer, then restore
`{}` unless separately approved for production.

Enable the dashboard route only after every gate passes; keep legacy Twilio running.

## Withdraw and rollback without dropping calls

1. Withdraw the Dialog dashboard/carrier route and verify its approved fallback.
2. Before stop, drain active calls: poll `/smartpbx/status` until `active_sessions`
   is zero; retain service until the agreed active-call deadline, then escalate.
3. Only after drain completes:

   ```sh
   cd /opt/kavya
   SMARTPBX_IMAGE_TAG=<REVIEWED_COMMIT_SHA> docker compose --env-file .env.smartpbx --profile smartpbx stop kavya-smartpbx
   ```

Restore `SMARTPBX_TRANSFER_DESTINATIONS_JSON={}` after every temporary drill.
