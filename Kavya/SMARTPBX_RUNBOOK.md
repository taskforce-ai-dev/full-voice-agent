# Kavya Dialog SmartPBX cutover runbook

This runbook operates only the opt-in `kavya-smartpbx` profile at `/opt/kavya`.
It leaves the existing `kavya` Twilio service unchanged and keeps Flico untouched:
do not stop, edit, restart, or route Flico through this service.

## Preconditions and immutable image identity

- Dialog has confirmed its tenant account value and the one MCP account-header
  spelling. `SMARTPBX_ACCOUNT_ID` is server-side and must equal the
  carrier-emitted `start.accountId`; it is not a dashboard field.
- The four-call limit is enforced locally and is also the purchased Dialog
  capacity; it is not a dashboard field.
- If Dialog supplies egress ranges, configure its source-IP allowlist before
  cutover.
- The successful image workflow emits exactly two values for the reviewed
  release: its CI short SHA image tag and its full commit revision label. Copy
  both from that successful workflow/review record, without deriving one from
  the other.

```sh
set -euo pipefail
cd /opt/kavya
# Replace these two example shapes with the reviewed values from the successful
# image workflow. The first is exactly the CI `git rev-parse --short HEAD` tag.
REVIEWED_CI_SHORT_SHA=abcdef0
REVIEWED_FULL_COMMIT_SHA=0123456789abcdef0123456789abcdef01234567
SMARTPBX_IMAGE="ghcr.io/taskforce-ai-dev/kavya:$REVIEWED_CI_SHORT_SHA"
```

Prefer the image already pulled by the successful image deploy workflow:

```sh
set -euo pipefail
docker image inspect "$SMARTPBX_IMAGE" >/dev/null
docker image inspect "$SMARTPBX_IMAGE" --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}' | grep -Fx "$REVIEWED_FULL_COMMIT_SHA"
```

If that exact image is not local and the GHCR package is private, use a
least-privilege package-read token. The token is read silently, passed only on
standard input, and is neither an argument nor output:

```sh
set -euo pipefail
read -r GHCR_USERNAME
read -r -s GHCR_READ_TOKEN
printf '\n'
printf '%s' "$GHCR_READ_TOKEN" | docker login ghcr.io -u "$GHCR_USERNAME" --password-stdin
unset GHCR_READ_TOKEN
docker pull "$SMARTPBX_IMAGE"
docker logout ghcr.io >/dev/null 2>&1
docker image inspect "$SMARTPBX_IMAGE" --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}' | grep -Fx "$REVIEWED_FULL_COMMIT_SHA"
```

## Create the isolated server-side environment

```sh
set -euo pipefail
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
STT_PROVIDER=azure
AZURE_SPEECH_KEY=
AZURE_SPEECH_REGION=southeastasia
YANOLJA_BASE_URL=
YANOLJA_USERNAME=
YANOLJA_PASSWORD=
YANOLJA_TIMEOUT=30
DEMO_RATES_ENABLED=true
N8N_BASE_URL=
N8N_POSTCALL_WEBHOOK=/webhook/post-call-data
DASHBOARD_API_URL=
DASHBOARD_API_KEY=
DASHBOARD_AGENT_ID=kavya
SENTRY_DSN=
SENTRY_TRACES_SAMPLE_RATE=0.0
SENTRY_ENV=production
SMARTPBX_WS_TOKEN=
SMARTPBX_ACCOUNT_ID=
SMARTPBX_MAX_MESSAGE_CHARS=65536
SMARTPBX_MAX_AUDIO_BYTES=32768
SMARTPBX_MAX_OUTBOUND_FRAMES=128
SMARTPBX_START_TIMEOUT_SECONDS=10
SMARTPBX_IDLE_TIMEOUT_SECONDS=90
SMARTPBX_HUMAN_AGENT_WHATSAPP=
SMARTPBX_MCP_URL=https://dialog.cybergate.lk:9443/ucp/v2/mcp
SMARTPBX_API_KEY=
SMARTPBX_MCP_ACCOUNT_HEADER=
SMARTPBX_TRANSFER_DESTINATIONS_JSON={}
SMARTPBX_MCP_CONNECT_TIMEOUT_SECONDS=5
SMARTPBX_MCP_READ_TIMEOUT_SECONDS=15
SMARTPBX_MCP_MAX_RESPONSE_BYTES=1048576
SMARTPBX_MCP_RETRIES=1
```

Kavya accepts `SMARTPBX_MCP_ACCOUNT_HEADER=account_id` and
`SMARTPBX_MCP_ACCOUNT_HEADER=X-Account-ID`; Dialog must approve exactly one.
MCP API/account headers and all MCP credentials are server-only. The dashboard WSS
headers carry only the dedicated WSS token; they never carry MCP credentials.

## Dialog dashboard fields

Only the dedicated WSS token is pasted into the Dialog dashboard.

| Field | Value |
| --- | --- |
| Name | `Kavya SmartPBX` |
| Media format | `g711_ulaw` |
| Sample rate | `8000` Hz |
| Media WebSocket URL | `wss://smartpbx-kavya.taskforceai.tech/ws/v1/smartpbx/media` |
| WebSocket headers | `X-Kavya-SmartPBX-Token: <SMARTPBX_WS_TOKEN>` |

## TLS bootstrap, local service validation, then public proxy

The generic sequence below is safe from a blank host. Operator state on
2026-08-06 is already issued: DNS-only A record is live, the HTTP bootstrap site
is installed, and the certificate expires 2026-11-04. Still run the checks and
skip only a completed issuance; never install the certificate-referencing site
before its files exist.

```sh
set -euo pipefail
cd /opt/kavya
getent ahostsv4 smartpbx-kavya.taskforceai.tech | grep -F '67.207.90.109'
sudo install -m 0644 nginx-smartpbx-acme.conf /etc/nginx/sites-available/kavya-smartpbx
sudo ln -sfn /etc/nginx/sites-available/kavya-smartpbx /etc/nginx/sites-enabled/kavya-smartpbx
sudo nginx -t
sudo systemctl reload nginx
sudo certbot certonly --webroot -w /var/www/html -d smartpbx-kavya.taskforceai.tech
test -s /etc/letsencrypt/live/smartpbx-kavya.taskforceai.tech/fullchain.pem
test -s /etc/letsencrypt/live/smartpbx-kavya.taskforceai.tech/privkey.pem
SMARTPBX_IMAGE_TAG="$REVIEWED_CI_SHORT_SHA" docker compose --env-file .env.smartpbx --profile smartpbx config > /dev/null
SMARTPBX_IMAGE_TAG="$REVIEWED_CI_SHORT_SHA" docker compose --env-file .env.smartpbx --profile smartpbx up -d --force-recreate --pull never kavya-smartpbx
wait_for_smartpbx_ready() {
  deadline=$((SECONDS + 90))
  while ! curl --silent --show-error --fail http://127.0.0.1:8006/health >/dev/null \
    || ! curl --silent --show-error --fail http://127.0.0.1:8006/smartpbx/status >/dev/null; do
    if (( SECONDS >= deadline )); then
      echo "SmartPBX did not become ready within 90 seconds" >&2
      exit 1
    fi
    sleep 2
  done
}
wait_for_smartpbx_ready
sudo install -m 0644 nginx-smartpbx.conf /etc/nginx/sites-available/kavya-smartpbx
sudo nginx -t
sudo systemctl reload nginx
curl --fail https://smartpbx-kavya.taskforceai.tech/health
curl --fail https://smartpbx-kavya.taskforceai.tech/smartpbx/status
```

`config > /dev/null` validates Compose without printing secrets. `--pull never`
requires the exact reviewed image verified above and prevents an unreviewed pull.
The bounded 90-second loop covers the configured 40-second container warm-up;
it requires both loopback endpoints before the final TLS vhost is installed.

## Cutover gates

Before enabling the Dialog route, record privacy-safe call fingerprints and
outcomes, never raw call IDs or credentials:

1. Bad or missing WSS auth is rejected.
2. A bidirectional call proves caller audio reaches STT, an LLM turn completes,
   and the caller receives the response.
3. Exercise a KB answer and PMS tool, then verify a post-call record reaches the
   dashboard/webhook.
4. Hold four authenticated calls: **4 accepted + 5th rejected**, then hang up
   and verify `/smartpbx/status` returns zero active sessions.
5. Test endpoint-down fallback before shifting traffic.

## Optional transfer activation and compulsory revoke

Base WSS cutover is transfer-disabled with
`SMARTPBX_TRANSFER_DESTINATIONS_JSON={}`. Every edit to `.env.smartpbx` requires
the following recreation; an environment-file edit alone does not update the
running container.

Enable a supervised non-production transfer drill only after Dialog approves the
endpoint, API key, account-header spelling, and an allowlisted test destination.
Edit `.env.smartpbx` to add only that test destination, then apply it:

```sh
set -euo pipefail
cd /opt/kavya
SMARTPBX_IMAGE_TAG="$REVIEWED_CI_SHORT_SHA" docker compose --env-file .env.smartpbx --profile smartpbx up -d --force-recreate --pull never kavya-smartpbx
wait_for_smartpbx_ready() {
  deadline=$((SECONDS + 90))
  while ! curl --silent --show-error --fail http://127.0.0.1:8006/health >/dev/null \
    || ! curl --silent --show-error --fail http://127.0.0.1:8006/smartpbx/status >/dev/null; do
    if (( SECONDS >= deadline )); then
      echo "SmartPBX did not become ready within 90 seconds" >&2
      exit 1
    fi
    sleep 2
  done
}
wait_for_smartpbx_ready
```

Perform one observed drill. Restore `SMARTPBX_TRANSFER_DESTINATIONS_JSON={}` in
`.env.smartpbx`, then recreate the same and only service. Prove the restored
configuration reached the running process before considering the drill revoked:

```sh
set -euo pipefail
cd /opt/kavya
SMARTPBX_IMAGE_TAG="$REVIEWED_CI_SHORT_SHA" docker compose --env-file .env.smartpbx --profile smartpbx up -d --force-recreate --pull never kavya-smartpbx
wait_for_smartpbx_ready() {
  deadline=$((SECONDS + 90))
  while ! curl --silent --show-error --fail http://127.0.0.1:8006/health >/dev/null \
    || ! curl --silent --show-error --fail http://127.0.0.1:8006/smartpbx/status >/dev/null; do
    if (( SECONDS >= deadline )); then
      echo "SmartPBX did not become ready within 90 seconds" >&2
      exit 1
    fi
    sleep 2
  done
}
wait_for_smartpbx_ready
curl --fail http://127.0.0.1:8006/smartpbx/status | jq -e '.transfer_enabled == false'
```

## Withdraw and rollback without dropping calls

1. Withdraw the Dialog dashboard/carrier route and verify its approved fallback.
2. Before stopping anything, drain active calls: poll `/smartpbx/status` until
   `active_sessions` is zero; retain service until the agreed deadline, then
   escalate.
3. Only after drain completes:

   ```sh
   set -euo pipefail
   cd /opt/kavya
   SMARTPBX_IMAGE_TAG="$REVIEWED_CI_SHORT_SHA" docker compose --env-file .env.smartpbx --profile smartpbx stop kavya-smartpbx
   ```
