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
CLAUDE_MODEL=claude-sonnet-4-5-20250929
OPENAI_MODEL=gpt-4o
GEMINI_API_KEY=
GEMINI_MODEL=gemini-2.5-flash
OPENAI_TTS_MODEL=gpt-4o-mini-tts
OPENAI_TTS_VOICE=nova
OPENAI_TTS_INSTRUCTIONS=
ELEVENLABS_API_KEY=
KAVYA_EN_ELEVENLABS_VOICE_ID=
ELEVENLABS_VOICE_ID=
ELEVENLABS_VOICE_ID_AR=
STT_PROVIDER=azure
STT_ENDPOINTING_SILENCE_SECONDS=1.0
STT_FINAL_GRACE_SECONDS=0.5
STT_DIGIT_CLASS_BOOST=
DTMF_INTERDIGIT_TIMEOUT_SECONDS=6
DTMF_OVERALL_TIMEOUT_SECONDS=30
DTMF_MAX_DIGITS=15
BARGEIN_MIN_CHARS=12
BARGEIN_DEBOUNCE_SECONDS=0.6
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
SMARTPBX_MAX_OUTBOUND_FRAMES=512
SMARTPBX_START_TIMEOUT_SECONDS=10
SMARTPBX_IDLE_TIMEOUT_SECONDS=90
SMARTPBX_TRANSFER_PENDING_TIMEOUT_SECONDS=300
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

## Later reviewed English digit-class rollout

This runbook does not select a production boost. After review of the relevant
test and privacy-safe telemetry evidence, choose a nonzero
`STT_DIGIT_CLASS_BOOST` only in the protected `.env.smartpbx` file. Do not print
that file, the selected value, or any secret.

Before a separately approved recreate, render configuration with
`docker compose --env-file .env.smartpbx config` (and the SmartPBX profile) and
verify only that the explicit allowlist contains the key:

```sh
set -euo pipefail
cd /opt/kavya
SMARTPBX_IMAGE_TAG="$REVIEWED_CI_SHORT_SHA" docker compose --env-file .env.smartpbx --profile smartpbx config --format json \
  | jq -e '.services["kavya-smartpbx"].environment | has("STT_DIGIT_CLASS_BOOST")' >/dev/null
```

After the separately approved pinned recreate using the established guarded
deployment command in the TLS bootstrap or deployment path below, verify without
printing configuration or secrets that the one safe state event was emitted:

```sh
docker compose --env-file .env.smartpbx --profile smartpbx logs --since 10m kavya-smartpbx \
  | rg -q 'smartpbx_media event=stt_digit_class_state digit_class_enabled=(true|false) digit_class_boost='
```

The event reports only the clamped boost and enabled state; it contains no
environment dump, transcript, caller identifier, provider payload, or secret.

## Canonical English voice provisioning

Retrieve Kavya's established English voice identity only from the approved root-only secret source. Do not copy it from source code, a log, the dashboard, or this runbook. Do not rotate `ELEVENLABS_API_KEY` or alter `ELEVENLABS_VOICE_ID`. Set the same protected value in both files with `sudoedit`; never place it in a command argument, commit, ticket, or screen share.

```sh
set -euo pipefail
cd /opt/kavya
sudo test -f /opt/kavya/.env
sudo touch /opt/kavya/.env.smartpbx
sudo chown root:root /opt/kavya/.env /opt/kavya/.env.smartpbx
sudo chmod 600 /opt/kavya/.env /opt/kavya/.env.smartpbx
sudoedit /opt/kavya/.env
sudoedit /opt/kavya/.env.smartpbx
sudo /opt/kavya/scripts/validate_english_voice_env.sh /opt/kavya/.env /opt/kavya/.env.smartpbx
SMARTPBX_IMAGE_TAG="$REVIEWED_CI_SHORT_SHA" docker compose --env-file .env.smartpbx --profile smartpbx config > /dev/null
```

`canonical_voice_match=ok` proves only that both root-only files contain an equal nonblank protected value; it does not print the value. The configuration check prints no secrets. This preflight does not start, stop, recreate, or reroute any service, and it preserves `SMARTPBX_TRANSFER_DESTINATIONS_JSON={}` with MCP transfer-disabled. If either command fails, do not run a Compose `up`; leave containers unchanged, restore both files to their prior root-only state with `sudoedit`, and rerun the preflight before any later approved deployment.

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
smartpbx_status_token() {
  # Read-only, never echoed. The status endpoint requires the same shared
  # token as the media socket, so readiness checks must present it.
  sed -n 's/^SMARTPBX_WS_TOKEN=//p' /opt/kavya/.env.smartpbx | head -n 1
}
wait_for_smartpbx_ready() {
  deadline=$((SECONDS + 90))
  while ! curl --silent --show-error --fail --connect-timeout 2 --max-time 5 http://127.0.0.1:8006/health >/dev/null \
    || ! printf 'header = "X-Kavya-SmartPBX-Token: %s"\n' "$(smartpbx_status_token)" \
      | curl --silent --show-error --fail --connect-timeout 2 --max-time 5 http://127.0.0.1:8006/smartpbx/status --config - >/dev/null; do
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
printf 'header = "X-Kavya-SmartPBX-Token: %s"\n' "$(smartpbx_status_token)" \
  | curl --fail https://smartpbx-kavya.taskforceai.tech/smartpbx/status --config -
```

`config > /dev/null` validates Compose without printing secrets. `--pull never`
requires the exact reviewed image verified above and prevents an unreviewed pull.
The bounded 90-second loop covers the configured 40-second container warm-up;
it requires both loopback endpoints before the final TLS vhost is installed.

## Cutover gates

Before enabling the Dialog route, emit only the fixed protocol diagnostic with exactly seven fields: `event=smartpbx_protocol_diagnostic`, `correlation_id`, `stage`, `outcome`, `failure_class`, `active_sessions`, and `duration_ms`. The `correlation_id` is opaque, local, randomly generated, and never derived from Dialog. No additional event names, fields, values, or measurements are permitted.

1. Bad or missing WSS auth is rejected.
2. A bidirectional call proves caller audio reaches STT, an LLM turn completes,
   and the caller receives the response.
3. Exercise a KB answer and PMS tool, then verify a post-call record reaches the
   dashboard/webhook.
4. Hold four authenticated calls: **4 accepted + 5th rejected**, then hang up
   and verify `/smartpbx/status` returns zero active sessions. Note that a
   pre-accept rejection closes before the WebSocket handshake completes, so the
   close codes (`1008` policy violation, `1013` capacity) never reach the wire —
   Dialog sees a bare HTTP `403` and cannot distinguish "wrong token" from "at
   capacity". Record what Dialog actually observes for the capacity rejection,
   and use the `failure_class` in our own diagnostic stream (`authentication` vs
   `capacity`) as the authoritative signal.
5. Test endpoint-down fallback before shifting traffic.
6. **Live barge-in.** Also confirm Dialog's reconnect behaviour here: nginx now
   rate-limits the media location (30r/m, burst 10) and returns `429` beyond
   that. Restart the container and watch how Dialog re-establishes its sockets —
   record whether it retries on `429` and how quickly, and raise the burst if a
   normal reconnect storm trips it.
   Ask Kavya something that produces a long answer, then
   interrupt her mid-sentence. Confirm she **stops speaking within about a
   second** and responds to the interruption. Dialog has no `clear` wire event,
   so the only thing that can cancel queued speech is the outbound queue still
   holding it: audio is paced at realtime for exactly this reason. If she talks
   over you to the end of the answer, pacing is not in effect — treat that as a
   gate failure, not a cosmetic issue, because every interruption for the whole
   call will behave the same way.

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
smartpbx_status_token() {
  # Read-only, never echoed. The status endpoint requires the same shared
  # token as the media socket, so readiness checks must present it.
  sed -n 's/^SMARTPBX_WS_TOKEN=//p' /opt/kavya/.env.smartpbx | head -n 1
}
wait_for_smartpbx_ready() {
  deadline=$((SECONDS + 90))
  while ! curl --silent --show-error --fail --connect-timeout 2 --max-time 5 http://127.0.0.1:8006/health >/dev/null \
    || ! printf 'header = "X-Kavya-SmartPBX-Token: %s"\n' "$(smartpbx_status_token)" \
      | curl --silent --show-error --fail --connect-timeout 2 --max-time 5 http://127.0.0.1:8006/smartpbx/status --config - >/dev/null; do
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
smartpbx_status_token() {
  # Read-only, never echoed. The status endpoint requires the same shared
  # token as the media socket, so readiness checks must present it.
  sed -n 's/^SMARTPBX_WS_TOKEN=//p' /opt/kavya/.env.smartpbx | head -n 1
}
wait_for_smartpbx_ready() {
  deadline=$((SECONDS + 90))
  while ! curl --silent --show-error --fail --connect-timeout 2 --max-time 5 http://127.0.0.1:8006/health >/dev/null \
    || ! printf 'header = "X-Kavya-SmartPBX-Token: %s"\n' "$(smartpbx_status_token)" \
      | curl --silent --show-error --fail --connect-timeout 2 --max-time 5 http://127.0.0.1:8006/smartpbx/status --config - >/dev/null; do
    if (( SECONDS >= deadline )); then
      echo "SmartPBX did not become ready within 90 seconds" >&2
      exit 1
    fi
    sleep 2
  done
}
wait_for_smartpbx_ready
printf 'header = "X-Kavya-SmartPBX-Token: %s"\n' "$(smartpbx_status_token)" \
  | curl --fail http://127.0.0.1:8006/smartpbx/status --config - | jq -e '.transfer_enabled == false'
```

## Monitoring `/smartpbx/status`

`/smartpbx/status` requires the same `X-Kavya-SmartPBX-Token` header as the media
socket. It is not publicly readable: `active_sessions` against the cap of four is
a live occupancy oracle, `admitted_total` is a call-volume counter, and
`transfer_enabled` reveals whether live transfer is armed. An unauthenticated or
wrong-token request gets `401` with no body.

Pass the token on standard input, never as a command argument, so it cannot
appear in the process list or shell history:

```sh
set -euo pipefail
smartpbx_status_token() {
  sed -n 's/^SMARTPBX_WS_TOKEN=//p' /opt/kavya/.env.smartpbx | head -n 1
}
printf 'header = "X-Kavya-SmartPBX-Token: %s"\n' "$(smartpbx_status_token)" \
  | curl --fail --silent http://127.0.0.1:8006/smartpbx/status --config - \
  | jq '{active_sessions, max_sessions, frames_dropped_total, transfer_enabled}'
```

Reading it requires root on the host, because `.env.smartpbx` is `root:root 600`.
Point uptime monitoring at `/health`, which stays unauthenticated and reveals
nothing beyond liveness and the service mode. If SmartPBX is not configured there
is no token, so status fails closed rather than exposing counters — use `/health`
to distinguish "down" from "not configured".

`frames_dropped_total` is a saturating count of outbound audio frames refused by
transport backpressure. A non-zero value means some replies were **cut short** —
delivery is a contiguous prefix, so the guest hears the start of the answer and
then silence, not garbled speech.

The dominant trigger is **reply length against queue depth**, not CPU or socket
health. Outbound audio is paced at realtime while TTS produces it faster, so the
queue holds the backlog; a reply longer than the queue can hold overflows it. At
the default 512 frames that is roughly 40 seconds of continuous speech. Read a
rising counter as "Kavya is answering at unusual length" and look at the prompt
and the KB content first. Raise `SMARTPBX_MAX_OUTBOUND_FRAMES` (ceiling 512) only
if it is already below the default. Investigate CPU or the Dialog socket only
once reply length has been ruled out.

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

## Guarded immutable image deployment

Use the guarded helper only after an authenticated integration probe and an
approved immutable-image review record. Its three arguments are the exact
lowercase CI short tag, the full reviewed OCI revision, and the image digest;
the short tag must be the first seven characters of that revision.

Prerequisites: run as root on the target host, keep `/opt/kavya/.env` and
`/opt/kavya/.env.smartpbx` owned by `root:root` with mode `0600`, retain a
healthy `flico-voice-agent` and `kavya-voice-agent`, and ensure the reviewed
GHCR digest is pullable. The helper checks the existing SmartPBX image ID,
repository digest, and OCI revision, then records a local rollback alias before
it recreates only `kavya-smartpbx`.

```sh
# As root: deploy_smartpbx_image.sh NEW_TAG EXPECTED_SHA EXPECTED_DIGEST
/opt/kavya/scripts/deploy_smartpbx_image.sh "$NEW_TAG" "$EXPECTED_SHA" "$EXPECTED_DIGEST"
```

It rolls back once for ordinary errors and `INT`, `TERM`, or `HUP`, and verifies
the restored image identity and the unchanged healthy Flico and legacy Kavya
containers. `SIGKILL`, kernel panic, power loss, and host loss cannot run a
shell trap; an operator must inspect and recover those cases from the recorded
baseline/rollback alias. The helper never manages Nginx, prunes images, or
mutates another service.
