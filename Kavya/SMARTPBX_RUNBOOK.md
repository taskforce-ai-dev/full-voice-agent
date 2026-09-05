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
# The reviewed image workflow also emits this digest; every guarded-script
# invocation below (deploy_smartpbx_image.sh takes exactly these three
# positional values) and the Sinhala-rollback transaction need it.
REVIEWED_IMAGE_DIGEST=$(docker image inspect "$SMARTPBX_IMAGE" --format '{{index .RepoDigests 0}}' | sed 's/^.*@//')
[[ "$REVIEWED_IMAGE_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]]
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
CLAUDE_MODEL=claude-sonnet-5
OPENAI_MODEL=gpt-4o
GEMINI_API_KEY=
RIME_API_KEY=
GEMINI_MODEL=gemini-2.5-flash
SMARTPBX_LANGUAGE_SELECTION_TIMEOUT_SECONDS=8.0
SMARTPBX_SINHALA_LLM_PROVIDER=gemini
SMARTPBX_SINHALA_GEMINI_LLM_MODEL=gemini-3.7-flash
SMARTPBX_SINHALA_GEMINI_THINKING_LEVEL=low
SMARTPBX_SINHALA_GEMINI_MAX_TOKENS=1024
SMARTPBX_SINHALA_GEMINI_TTS_MODEL=gemini-3.1-flash-tts-preview
SMARTPBX_SINHALA_GEMINI_TTS_VOICE=Vindemiatrix
SMARTPBX_SINHALA_GEMINI_TTS_TIMEOUT_SECONDS=15.0
SMARTPBX_SINHALA_TTS_PROVIDER=gemini
SMARTPBX_SINHALA_TTS_QUOTA_STICKY_AFTER=3
SMARTPBX_SINHALA_GEMINI_TTS_FALLBACK_MODELS=
SMARTPBX_SINHALA_TTS_MODEL_RESET_UTC_HOUR=7
OPENAI_TTS_MODEL=gpt-4o-mini-tts
OPENAI_TTS_VOICE=nova
OPENAI_TTS_INSTRUCTIONS=
ELEVENLABS_API_KEY=
KAVYA_EN_ELEVENLABS_VOICE_ID=
ELEVENLABS_VOICE_ID=
ELEVENLABS_VOICE_ID_AR=
STT_PROVIDER=azure
SMARTPBX_SINHALA_AZURE_SEGMENTATION_SILENCE_MS=0
STT_ENDPOINTING_SILENCE_SECONDS=1.0
STT_FINAL_GRACE_SECONDS=0.5
CAPTURE_ENDPOINTING_SILENCE_SECONDS=1.5
CAPTURE_FINAL_GRACE_SECONDS=1.2
CAPTURE_VALID_LK_NUMBER_GRACE_SECONDS=0.35
STT_DIGIT_CLASS_BOOST=
SMARTPBX_PILOT_TRANSCRIPT_LOGGING=0
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
SMARTPBX_STARTUP_PREROLL_MS=0
SMARTPBX_MAX_TOKENS=
SMARTPBX_CLAUDE_MAX_TOKENS=
SMARTPBX_SINHALA_CLAUDE_EFFORT=
SMARTPBX_INITIAL_FILLER_DELAY_SECONDS=
SMARTPBX_SINHALA_INITIAL_FILLER_DELAY_SECONDS=
SMARTPBX_LLM_INITIAL_RESPONSE_TIMEOUT_SECONDS=
SMARTPBX_LLM_STALL_TIMEOUT_SECONDS=
SMARTPBX_CLAUDE_THINKING_STALL_TIMEOUT_SECONDS=
SMARTPBX_START_TIMEOUT_SECONDS=10
SMARTPBX_IDLE_TIMEOUT_SECONDS=90
SMARTPBX_TRANSFER_PENDING_TIMEOUT_SECONDS=300
SMARTPBX_MAX_CALL_SECONDS=3600
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

## SmartPBX Sinhala menu and Gemini TTS

The SmartPBX call menu is **1 English, 2 Sinhala**. The timeout defaults to
English; an invalid selection replays once then defaults to English. This is a
direct SmartPBX-only choice: Twilio behavior is unchanged.

Press 1 keeps the English Azure STT -> Claude -> ElevenLabs pipeline. Press
2 uses Azure `si-LK` -> Gemini `gemini-3.7-flash` at thinking level `low`
with a separate `600`-token ceiling -> Gemini
`gemini-3.1-flash-tts-preview` / `Vindemiatrix`. The bilingual prompt before
selection is the reviewed static μ-law asset documented below; neither menu
segment makes a live TTS request.

Direct Sinhala TTS uses `SMARTPBX_SINHALA_TTS_PROVIDER`, defaulting to
`gemini` as the safe repository and rollback value. The protected template
therefore uses `SMARTPBX_SINHALA_TTS_PROVIDER=gemini`. The production canary
selects `rime` by changing that setting only in the protected
`.env.smartpbx`; `RIME_API_KEY` is server-side only and never appears in
status responses or logs (including `/smartpbx/status`). Rime failures fall
back once to the existing Gemini Sinhala TTS path. Keep the key out of the
general `.env`, tickets, commands, and diagnostics.

The Gemini credential check is no-output and stripped: exactly one active,
nonblank `GEMINI_API_KEY` is required before exposing the bilingual menu,
including to a caller who will press `1`. Blank, whitespace-only, duplicate,
or later-active assignments fail closed before either menu segment or STT can
start, with only the existing generic unavailable outcome. Never print or
inspect the protected file outside the reviewed root-only procedures.

Gemini-to-Claude fallback is call-local and conditional on both
`GEMINI_FAILOVER_TO_CLAUDE` and usable Anthropic readiness. If either is
unavailable, the bounded Gemini failure outcome remains. TTS remains selected
by language and has no OpenAI, ElevenLabs, or Azure-TTS fallback. Diagnostics
and canaries use bounded metadata only: never a key, transcript, response, or
exception detail.

Azure remains the live Sinhala STT provider. Gemini Transcribe, Chirp 2, and
StreamingRecognize are offline-only evaluations; do not change the live STT
route while operating this canary.

### SmartPBX-only one-setting Sinhala LLM rollback

This transaction changes only `SMARTPBX_SINHALA_LLM_PROVIDER=claude`; it does
not change global `LLM_PROVIDER`, English, Twilio, STT, TTS, or any secret.
First drain to authenticated `active_sessions=0`. The root-owned updater owns
the locked `/opt/kavya/.smartpbx-sinhala-rollback` directory (already
`root:root`, mode `0700`), its private mode-`0600` backup, all temporary files,
and recorded original metadata. Operators do not create, name, inspect, or
pass a backup path.

The guarded image deployer does not install host-side scripts. Before this
procedure, the reviewed `/opt/kavya` checkout must contain this utility and it
must be root-owned mode `0755`; otherwise stop as a deployment blocker rather
than assuming an image build installed it:

```sh
sudo test -x /opt/kavya/scripts/update_smartpbx_sinhala_provider.sh
sudo chown root:root /opt/kavya/scripts/update_smartpbx_sinhala_provider.sh
sudo chmod 0755 /opt/kavya/scripts/update_smartpbx_sinhala_provider.sh
sudo install -d -o root -g root -m 0700 /opt/kavya/.smartpbx-sinhala-rollback
```

```sh
# SmartPBX Sinhala LLM rollback transaction (covered by deployment-contract tests)
rollback_smartpbx_sinhala_llm() {
set -euo pipefail
cd /opt/kavya
if ! sudo /opt/kavya/scripts/update_smartpbx_sinhala_provider.sh apply /opt/kavya/.env.smartpbx claude; then
  return 1
fi
[[ "$REVIEWED_CI_SHORT_SHA" =~ ^[0-9a-f]{7}$ ]]
[[ "$REVIEWED_FULL_COMMIT_SHA" =~ ^[0-9a-f]{40}$ ]]
[[ "$REVIEWED_CI_SHORT_SHA" == "${REVIEWED_FULL_COMMIT_SHA:0:7}" ]]
[[ "$REVIEWED_IMAGE_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]]
if ! SMARTPBX_IMAGE_TAG="$REVIEWED_CI_SHORT_SHA" docker compose --env-file .env.smartpbx --profile smartpbx config > /dev/null; then
  sudo /opt/kavya/scripts/update_smartpbx_sinhala_provider.sh restore /opt/kavya/.env.smartpbx
  sudo /opt/kavya/scripts/update_smartpbx_sinhala_provider.sh cleanup /opt/kavya/.env.smartpbx
  return 1
fi
if ! sudo /opt/kavya/scripts/deploy_smartpbx_image.sh "$REVIEWED_CI_SHORT_SHA" "$REVIEWED_FULL_COMMIT_SHA" "$REVIEWED_IMAGE_DIGEST"; then
  sudo /opt/kavya/scripts/update_smartpbx_sinhala_provider.sh restore /opt/kavya/.env.smartpbx
  sudo /opt/kavya/scripts/deploy_smartpbx_image.sh "$REVIEWED_CI_SHORT_SHA" "$REVIEWED_FULL_COMMIT_SHA" "$REVIEWED_IMAGE_DIGEST"
fi
sudo /opt/kavya/scripts/update_smartpbx_sinhala_provider.sh cleanup /opt/kavya/.env.smartpbx
}
rollback_smartpbx_sinhala_llm
```

The updater accepts only the fixed protected path and `claude`; it verifies one
active provider assignment, normalized byte equality everywhere else, and the
recorded owner/group/mode before atomic replacement. It prints fixed redacted
status tokens only. A restore retains the private backup through the retry;
`cleanup` is the terminal action and first performs authenticated health/status
checks. Thus config validation failure restores then cleans up without any
guarded deploy; post-recreate failure restores then retries with the same
reviewed identity; successful authenticated health/status checks invoke cleanup
exactly once.

Never run `docker compose up`, `recreate`, or `restart` directly for this
rollback.

After the guarded deployment, run this **two-language canary call checklist**:

1. Call the SmartPBX endpoint, press `1`, and confirm the English pipeline.
2. Call again, press `2`, and confirm Azure `si-LK`, Gemini LLM, and Gemini
   `Vindemiatrix` TTS respond.
3. Confirm `/health` and authenticated `/smartpbx/status`; inspect only
   bounded provider/event/outcome diagnostics if the second call fails.

### SmartPBX-only Rime Sinhala TTS rollback

If the production canary has a regression, set
`SMARTPBX_SINHALA_TTS_PROVIDER` back to `gemini` in the protected
`/opt/kavya/.env.smartpbx` file. Keep `RIME_API_KEY` server-side only; do not
print it, include it in a status request, or search for it in logs. Then run
the existing guarded recreate procedure (render `docker compose ... config`,
wait for authenticated `active_sessions=0`, and recreate only
`kavya-smartpbx` with the same pinned image). Verify health, status, and the
two-language checklist before restoring traffic. Do not run a direct
unreviewed recreate or alter the Twilio `kavya` service.

## Static SmartPBX language menu

The pre-selection prompt is the committed `smartpbx_language_menu.ulaw` asset,
not a live ElevenLabs or Gemini request. It is 8 kHz G.711 μ-law aligned to
160-byte/20 ms frames. Its first 2,400 bytes are exactly fifteen frames (300
ms) of digital μ-law silence, followed by the approved canonical English
voice saying “For English, press 1.” and the approved Gemini `Vindemiatrix`
voice saying “සිංහල සඳහා, 2 ඔබන්න.”

The runtime validates and caches the entire asset before starting menu
playback. Missing, empty, oversized, misaligned, incorrectly prefixed, or
all-silent assets fail admission before partial audio reaches the caller.

`SMARTPBX_LANGUAGE_SELECTION_TIMEOUT_SECONDS` is armed from the END of menu
playback -- after the completion mark returns, i.e. once the last frame has
reached the wire -- not when playback is scheduled. The asset is roughly 4.4 s
long, so arming at the start left a caller only the remainder of the window and
cut the Sinhala half off mid-sentence. Budget the value as time the caller has
AFTER hearing the whole menu. A replayed menu (one invalid key) opens a fresh
window from its own end, and a menu whose playback fails still opens one, so a
transport fault cannot park the call in the pre-selection state.
Changing the wording or either voice requires regenerating the asset once with
protected provider credentials, verifying the same wire contract, reviewing
the resulting audio, and shipping it in a new image. Never generate this menu
per call and never store provider credentials in the repository.

When this static menu is deployed, keep the separate transport experiment at
`SMARTPBX_STARTUP_PREROLL_MS=0`; the asset's own 300 ms prefix is the complete
intentional startup lead. Roll back the image and restore the prior protected
environment value together if the opening prompt regresses.

## Controlled startup pre-roll canary

`SMARTPBX_STARTUP_PREROLL_MS` is a one-time, transport-only run of digital
G.711 μ-law silence immediately after Dialog's authenticated `start` event and
before Kavya's welcome greeting. It is default-off (`0`). The approved
controlled canary is `SMARTPBX_STARTUP_PREROLL_MS=2000`: one hundred exact
20 ms/160-byte silent frames, fully sent before the greeting begins. Its
purpose is to give the carrier decoder/jitter buffer time to settle before
Kavya speaks and test whether that avoids startup crackle.

This is not an Opus migration. Keep the Dialog dashboard and Kavya media
contract at `g711_ulaw` and `8000` Hz throughout this experiment.

To canary, change only the protected `.env.smartpbx` value to
`SMARTPBX_STARTUP_PREROLL_MS=2000`, render the compose configuration, and use
the normal guarded recreate of the same pinned image. Production's validated
stable setting is the default-off `SMARTPBX_STARTUP_PREROLL_MS=0` (matching
`docker-compose.yml`'s `${SMARTPBX_STARTUP_PREROLL_MS:-0}` and `.env.example`)
-- if the canary result is worse or inconclusive, restore
`SMARTPBX_STARTUP_PREROLL_MS=0` and recreate the same pinned image
immediately. Do not use this knob before replies, fillers, transfers, or any
later sentence in a call.

## Controlled Sinhala Azure segmentation canary

`SMARTPBX_SINHALA_AZURE_SEGMENTATION_SILENCE_MS` is a direct SmartPBX Sinhala
Azure-only recognizer setting. It is default-off (`0`), which preserves Azure's
service default and leaves English, Google STT, both Twilio paths, shared
endpointing/final/capture grace, barge-in, locale, and audio format unchanged.
The controlled production canary value is
`SMARTPBX_SINHALA_AZURE_SEGMENTATION_SILENCE_MS=800`.

For the canary, set that exact variable to `800` in the protected
`.env.smartpbx`, render the SmartPBX Compose configuration, and use the normal
guarded recreate of the same pinned image. Verify only the privacy-safe
`stt_provider_start` diagnostic: it reports `segmentation=enabled` and
`segmentation_silence_ms=800` and never contains transcript or caller data.

There are two independent rollback choices for this exact variable:

1. Set `SMARTPBX_SINHALA_AZURE_SEGMENTATION_SILENCE_MS=0` and recreate the same
   pinned image; or
2. Omit/remove `SMARTPBX_SINHALA_AZURE_SEGMENTATION_SILENCE_MS` from the
   protected `.env.smartpbx` so Compose supplies zero via
   `${SMARTPBX_SINHALA_AZURE_SEGMENTATION_SILENCE_MS:-0}`, then recreate the
   same pinned image.

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

## Direct SmartPBX English reliability timing (Phase B)

The shared timeout knobs govern direct SmartPBX English provider rounds and
direct SmartPBX Sinhala Claude/Gemini rounds. The initial filler now runs on
both profiles, each on its own delay knob (below) and its own rotating
phrase bank -- English through live ElevenLabs TTS, Sinhala through
pre-rendered/cached Gemini TTS clips only.
Twilio Media Streams (Arabic/Sinhala/Tamil) and the Twilio ConversationRelay
path are unaffected — none of this timing applies outside a direct SmartPBX
call.

- `SMARTPBX_INITIAL_FILLER_DELAY_SECONDS` (default `1.5`, clamp `[0.5, 5.0]`) —
  the one cancellable neutral filler for the first provider round of a call.
  Cancels the instant real content, a tool selection, a barge-in, a
  generation change, a transfer, or session finish pre-empts it.
- `SMARTPBX_SINHALA_INITIAL_FILLER_DELAY_SECONDS` (default `2.2`, clamp
  `[0.5, 5.0]`) — the direct-Sinhala-only counterpart. Higher default than
  the English knob because Gemini's first token is typically 1.2-1.5s
  (3.9s when throttled), and the shared 1.5s delay fired the Sinhala filler
  on most turns even when the answer was only moments away (2026-09-04
  tester feedback). The Sinhala filler also never plays on two consecutive
  turns within 15s unless this delay itself exceeds 3.5s — a fixed,
  not-env-tunable per-session repeat guard.
- `SMARTPBX_LLM_INITIAL_RESPONSE_TIMEOUT_SECONDS` (default `8.0`, clamp
  `[1.0, 30.0]`) — how long a provider round may run with zero content/tool
  deltas before Kavya gives up on it.
- `SMARTPBX_LLM_STALL_TIMEOUT_SECONDS` (default `8.0`, clamp `[1.0, 30.0]`) —
  the maximum gap between successive deltas once a round has started
  streaming. There is no total stream deadline while content keeps arriving.
- `SMARTPBX_CLAUDE_THINKING_STALL_TIMEOUT_SECONDS` (default `12.0`, clamp
  `[1.0, 30.0]`, effective minimum the shared stall timeout) — Claude-only
  first-attempt grace after a verified thinking block. Metadata-only, visible
  text, tool generation, every retry, OpenAI, and Gemini keep the shared
  timeout. Setting it to `8.0` restores the previous timing; thinking remains
  enabled and this timeout change does not alter the separately configured Claude output ceiling.

On either timeout, or on an empty response (no text, no tool call) that has
already used its one same-provider retry, Kavya cancels only that turn's own
generation, records one bounded fixed-enum telemetry stage (`llm_timeout`, or
the existing empty-response log line), blocks any stale history/transcript/
tool-result/audio write from that abandoned round, speaks one of two fixed
recovery lines, and keeps the call open for the caller's next utterance:
before any tool/side effect has started this turn, "I'm sorry, I'm having
trouble responding right now. Could you please say that again?"; once a tool
may have started this turn, that turn is never replayed — the caller instead
hears "I'm sorry, I wasn't able to give you a clear update. Would you like me
to continue?". Capture-name, capture-number and keypad flows keep their own
specialised logic and are excluded from the initial filler and from this
retry/recovery policy.

## Claude direct SmartPBX output budget (600-token canary)

`SMARTPBX_MAX_TOKENS` (default `120`, clamp `[40, 200]`) is the shared direct
SmartPBX English output budget and continues to govern OpenAI and English
Gemini. Claude and profile-configured Sinhala Gemini are exceptions:

- `SMARTPBX_CLAUDE_MAX_TOKENS` (default `600`, clamp `[200, 1024]`) — the
  Claude-only direct SmartPBX English/Sinhala output budget. Leave it blank to
  take the default.
- `SMARTPBX_SINHALA_GEMINI_MAX_TOKENS` (default `1024`, clamp `[200, 1024]`) —
  the direct Sinhala Gemini ceiling. Gemini 3.x may consume thinking tokens;
  this does not alter English Gemini's preserved shared-budget contract.

For direct Sinhala SmartPBX only, `SMARTPBX_SINHALA_CLAUDE_EFFORT` accepts
`medium` (default) or `high`. Thinking remains enabled in both modes; set
`high` for an immediate rollback to the deeper prior effort level. Missing,
blank, or whitespace values take the `medium` default; a nonblank invalid or
unknown value resolves safely to `high`.

**Canary model: `claude-sonnet-5`.** This is what the SmartPBX canary and prod
run, and it is what `CLAUDE_MODEL` in the `.env.smartpbx` template above is set
to. Do not read `server.py`'s hard-coded `CLAUDE_MODEL` env-default
(`claude-sonnet-4-5-20250929`) as the SmartPBX model — that default is the
Twilio path's pinned non-retired snapshot and a last-resort fallback for a
container started with no model configured at all. `.env.smartpbx` always sets
the model explicitly, so the two never disagree in practice; the 4.5 default is
deliberately left alone (see `tests/test_llm_default_model.py`).

Sonnet 5 runs **adaptive thinking on by default, and that is kept
deliberately** — it is what makes the model reliable at picking the right
booking tool and arguments on a noisy phone transcript. Nothing anywhere passes
a `thinking` parameter, and nothing disables it.

**Why 600: the thinking block AND a full tool block must both fit in one
budget.** Thinking is spent out of the same output allowance before any visible
block opens (roughly the first 50–140 tokens), and a `check_availability` call
needs about 160 output tokens on its own. At the shared 120-token budget a
tool-calling turn ran out part-way through the `tool_use` block: the block
never reached `content_block_stop`, so it was never accumulated, and the round
was misread as an empty response — the caller heard the recovery line and the
tool never ran. 600 leaves room for both. If `max_tokens_truncated` appears at
a non-zero rate while the canary is live, that is this ceiling being hit again,
not a model fault.

Nothing else moves: the global ConversationRelay/Twilio `MAX_TOKENS` (300),
every Twilio-path budget, and the OpenAI/Gemini SmartPBX budgets are untouched.

Each terminal-classified Claude or direct Gemini round logs exactly one
privacy-safe outcome line —
`smartpbx_media event=llm_round_outcome provider=<claude|gemini> outcome=<enum>
stop_reason=<enum> output_tokens=<bounded n|unknown> attempt=<1-9>` — carrying
no text, no tool arguments and no caller identifiers. See the cutover-gate
allowlist below for the exact, closed field set. `outcome` is one of
`completed`, `max_tokens_truncated`, `true_empty`, `incomplete_tool_block`,
`malformed_tool_json`, or `stream_aborted`; anything other than `completed`
logs at WARNING.

`true_empty` and `stream_aborted` are deliberately separate. `true_empty` means
the model reported terminal metadata having produced nothing — a
model-behaviour signal. `stream_aborted`
means the stream simply stopped arriving with no terminal metadata at all — a
transport/connection signal. Treat a rising `stream_aborted` rate as a network
or upstream-proxy investigation, never as a prompt problem.

**Only a `completed` round proceeds.** Every other outcome takes the shared
retry-once-then-recovery path (one retry, then the recovery line), and a
truncated, aborted or unparseable round is discarded WHOLE: no tool from that
round is dispatched — not even a tool block that completed cleanly alongside a
truncated sibling — nothing is written to history, and any per-sentence TTS
already in flight for it is cancelled and the transport generation cleared
before the retry or the recovery line speaks. Discarding the complete siblings
too is what makes the retry safe: nothing executed, so re-asking the model is a
fresh request rather than a replay of a committed booking side effect.

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
wait_for_smartpbx_idle() {
  # Pre-flight-only idle gate (P1-2/P1-3): active_sessions==0, bounded at
  # 10 minutes. A pending transfer holds its session slot, so this already
  # covers a live transfer; transfer_enabled is a configuration flag and
  # never gates a recreate. If this never returns, a call is live -- do not
  # force the recreate.
  local token deadline status_json
  token=$(sed -n 's/^SMARTPBX_WS_TOKEN=//p' /opt/kavya/.env.smartpbx | head -n 1)
  deadline=$((SECONDS + 600))
  while :; do
    status_json=$(printf 'header = "X-Kavya-SmartPBX-Token: %s"\n' "$token" \
      | curl --silent --show-error --fail --connect-timeout 2 --max-time 5 http://127.0.0.1:8006/smartpbx/status --config -) || status_json=""
    if [[ -n $status_json ]] && printf '%s' "$status_json" | jq -e '.active_sessions == 0' >/dev/null; then
      return 0
    fi
    if (( SECONDS >= deadline )); then
      echo "SmartPBX did not go idle within 10 minutes -- abort; do not force a recreate while a call may be live" >&2
      exit 1
    fi
    sleep 5
  done
}
wait_for_smartpbx_idle
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

Before enabling the Dialog route, the cutover evidence export is a **finite approved
event allowlist**. It may contain only the following runtime event names:
`smartpbx_protocol_diagnostic`, `stt_digit_class_state`, `stt_interim_shape`,
`stt_provider_start`,
`turn_stage`, `turn_summary`, `session_summary`, `echo_rejected`,
`agent_response`, `assistant_turn_delivery`, `audio_dump_written`,
`bad_tool_json`, `llm_round`, `llm_round_complete`, `llm_round_outcome`,
`llm_empty_response`,
`llm_error`, `llm_provider_degraded`, `llm_provider_failover`, `tool_execute`,
`tool_result`, `tool_error`, `tool_batch`, `tool_round_limit`, `tts_failure`, `rime_tts`,
`sinhala_tts_quota_degraded`, `sinhala_tts_model_fallback`,
`tts_interrupted`, `barge_in`, `guest_utterance`, `kb_error`,
`llm_stream_timeout`,
`silence_reprompt`, `stt_final`, `stt_post_dispatch_result`,
`stt_provider_final`, `stt_provider_interim`,
`capture_buffer_bounded`, `capture_final_buffered`, `capture_deferred_rearm`,
`capture_endpointing_decision`,
`capture_forced_dispatch`,
`stt_callback_drain`, `capture_mode_enter`, `capture_mode_exit`, `dtmf_collect_start`, and
`dtmf_collect_done`; unlisted event names are not permitted.
The protocol diagnostic record is emitted as
`event=smartpbx_protocol_diagnostic`.

The fixed, aggregate-only fields are `correlation_id`, `stage`, `outcome`,
`failure_class`, `active_sessions`, `duration_ms`, `turn_id`,
`session_trace_id`, and `provider` where the named event emits that field.
`correlation_id`, `turn_id`, and `session_trace_id` are opaque, local, randomly
generated identifiers and are never derived from dialog. The `provider` field is
a bounded provider enum: `openai`, `gemini`, `claude`, `elevenlabs`, `azure`, or `rime`;
the `llm_stream_timeout` event additionally permits its normalized `unknown`
sentinel as documented below.

`rime_tts` emits exactly `provider=rime`, `outcome`, and only the documented
bounded metadata: `status` is present for an HTTP status outcome; a successful
native PCMU stream also emits `first_chunk_ms`, `total_ms`, `chunk_count`, and
`audio_bytes`. `outcome` is a bounded enum: `success`,
`missing_api_key`, `timeout`, `http_status`, `transport_error`, `empty_audio`,
or `response_too_large`. `status` is present only for an HTTP status outcome
and is a bounded integer `100`–`599`, clamped. `first_chunk_ms` and `total_ms`
are bounded integers `0`–`600000`; `chunk_count` is bounded `0`–`100000`; and
`audio_bytes` is bounded `0`–`10485760`. The event contains no text, audio
bytes, response body, endpoint credential, API key, Authorization value,
caller identifier, or exception body.

`stt_provider_start` emits exactly `segmentation` (`enabled` or `disabled`) and
`segmentation_silence_ms` (bounded `0`-`5000`), alongside its fixed event name.
It appears at most once for each privacy-safe Azure startup and contains no
language, transcript, caller data, digits, credentials, SDK body, or exception
message.

`llm_round_outcome` emits exactly four fields beyond `provider`, and no others:

| Field | Type | Permitted values |
| --- | --- | --- |
| `provider` | bounded enum | `claude`, `gemini` |
| `outcome` | bounded enum | `completed`, `max_tokens_truncated`, `true_empty`, `incomplete_tool_block`, `malformed_tool_json`, `stream_aborted` |
| `stop_reason` | bounded enum | `end_turn`, `max_tokens`, `tool_use`, `stop_sequence`, `refusal`, `none` (absent), `unknown` (anything else) |
| `output_tokens` | bounded integer | `0`–`1000000`, clamped; or `unknown` when the stream reported no usage |
| `attempt` | bounded integer | `1`–`9`, clamped |

`stop_reason` is normalized to that fixed vocabulary **before** logging: the raw
provider string is never emitted, so a future API value or a proxy's arbitrary
text cannot turn this field into an open channel (a `stop_sequence`'s contents
are caller-derived). `output_tokens` and `attempt` are likewise clamped to the
ranges above, so a corrupt usage payload cannot write an unbounded numeral.
This event carries no free text, no tool names, no tool arguments and no caller
identifiers of any kind.

`capture_deferred_rearm` emits exactly three fields, and no others:

| Field | Type | Permitted values |
| --- | --- | --- |
| `event` | fixed literal | `capture_deferred_rearm` |
| `provenance` | bounded enum | `final`, `interim` |
| `delay_ms` | bounded integer | `0`–`5000`, clamped |

It appears only for direct SmartPBX capture speech whose endpointing deadline
expired while a prior turn was still active. It proves that, after the capture
ask was delivered, the buffered caller fragment received the existing capture
window instead of immediate dispatch. The record contains no transcript text,
length, phone digits, caller identifier, prompt, or tool data.

`capture_endpointing_decision` emits exactly four fields, and no others:

| Field | Type | Permitted values |
| --- | --- | --- |
| `event` | fixed literal | `capture_endpointing_decision` |
| `kind` | fixed literal | `phone` |
| `outcome` | fixed literal | `accelerated` |
| `delay_ms` | bounded integer | `100`–`1000`, clamped |

It appears only when a Direct SmartPBX phone-capture provider final combines to
an unambiguous Sri Lankan mobile number and receives the bounded short grace.
It contains no transcript text, phone digits, caller identifier, tool data, or
provider response.

`llm_stream_timeout` emits exactly eight fields, and no others:

| Field | Type | Permitted values |
| --- | --- | --- |
| `provider` | bounded enum | `openai`, `gemini`, `claude`, `unknown` |
| `phase` | bounded enum | `initial`, `stall` |
| `tool_executed` | boolean enum | `true`, `false` |
| `progress` | bounded enum | `none`, `metadata`, `thinking`, `text`, `tool` |
| `retrying` | boolean enum | `true`, `false` |
| `attempt` | bounded integer | `1`–`9`, clamped |
| `timeout_ms` | bounded integer | `1000`–`30000`, clamped |

The complete log line is `event=llm_stream_timeout provider=<enum>
phase=<initial|stall> tool_executed=<true|false> progress=<none|metadata|thinking|text|tool>
retrying=<true|false> attempt=<1-9> timeout_ms=<1000-30000>`. The provider is
normalized to `openai`, `gemini`, `claude`, or `unknown`; `timeout_ms` is the
bounded timeout that fired. Arbitrary or malformed provider
values are always logged as `unknown`. Claude records `metadata` for
`message_start` and `thinking` for thinking-block progress without exposing
thinking text. Only direct SmartPBX Claude `stall` progress before visible text,
a tool-use start, or tool execution may retry; an initial timeout remains one
request/one recovery. The raw provider value is never logged, and this event
contains no thinking text, transcript text, tool names, tool arguments, caller
identifiers, or exception bodies.

`stt_post_dispatch_result` emits exactly three fields, and no others:

| Field | Type | Permitted values |
| --- | --- | --- |
| `result_type` | bounded enum | `final`, `interim` (any other value emits nothing) |
| `action` | fixed string | `ignored_active_turn` |
| `elapsed_ms` | bounded integer | `0`–`60000`, clamped |

It records that an EMPTY provider result arrived while a turn was already
dispatched and was therefore refused before any counter, buffer or endpointing
timer changed — the age of the owning turn, nothing about what was said. No
transcript text, no character or token counts, no provider payload, no phone or
call identifier. A genuine barge-in is handled on the speaking-time path and
never reaches this event, so a rise in this counter still does not indicate
missed interruptions.

"Empty" is the whole of the refusal test, and it is evaluated in memory and never
logged: the result carries no material characters — nothing alphanumeric, so
empty, whitespace or punctuation only. Every other result is ADMITTED: it is
buffered, it cancels and resets the silence re-prompt, and it is dispatched as
the next turn, so it never reaches this event.

**This signal cannot prove whether a result was a provider tail or the caller
repeating themselves immediately, and it no longer tries.** An earlier revision
refused results whose text matched the dispatched utterance (equal, a
token-boundary prefix, or a punctuation-only superset) within two seconds of
dispatch, and called that proof of provider ownership. It is not. Establishing
that a result belongs to the utterance already answered needs provider result
identity, and none reaches this pipeline: `GoogleSTTStream` and `AzureSTTStream`
both deliver a bare string to their callbacks — no result id, no segment id, no
audio-time span — and `GoogleSTTStream._stream_epoch` is an internal gRPC-swap
fence that is identical for a tail and for a repetition. Matching text and a
short elapsed time are exactly what an immediate caller repetition looks like, so
that predicate was withdrawn along with the two-second window. `elapsed_ms`
survives as a description of the refusal, never as a gate.

Consequently, what the event **cannot** tell you is anything about the provider
or the network: draw **no packet-loss diagnosis and no provider-duplication
diagnosis** from it — a rise means empty results were seen and refused, and the
reason they were produced is undetermined by this event alone. Establishing that
needs the turn timings and the endpointing settings for the same calls, not this
counter. A rise is also not evidence of lost caller speech: a refused result had
no speech in it, and everything with speech in it is admitted.

Where that admitted speech goes is not silent either. Once it is buffered, every
boundary that can end or divert the call gives it an explicit owner — it becomes
a turn, or it is written into the call transcript (a barge-in supersedes it for
dispatch but still records it; transfer-pending and both teardown paths record it
before the post-call snapshot). Retention is reported by the existing
`capture_forced_dispatch` event. Its complete fixed, content-free contract is
`reason` (`barge_in`, `transfer`, `transfer_flush`, `session_end`, `hangup`),
`provenance` (`final` or `interim`), `answered=unanswered`, and `chars` capped
at 1000. No boundary drops the buffer silently. Teardown first closes endpoint
and turn dispatch synchronously, then keeps callback admission open only through
provider stop/audio dump; it closes that admission and bounded-drains it before
retention. `stt_callback_drain` reports only fixed outcome (`drained`, `timeout`,
or `error`), pending count, and bounded elapsed time; it never logs speech or
exception text. `privacy_safe=True` redacts operational logs only. Raw bounded,
labelled retained text remains only in the approved n8n transcript representation;
post-call extraction receives a provenance-only marker and never the raw text.

Volume is bounded per turn: the line is emitted **at most once per `result_type`
per owning turn**, so one turn contributes at most two lines however many results
it refuses. Per-turn totals travel on `turn_summary` instead, as three bounded
integers: `ignored_post_dispatch_finals` and `ignored_post_dispatch_interims`
(each `0`–`100000`, clamped) and `ignored_post_dispatch_max_elapsed_ms`
(`0`–`60000`, clamped, the same range as `elapsed_ms` above). All three are
absent from the summary of a turn that refused nothing — absent, not zero, the
same convention as `kb_ms` and `tool_ms`.

Wire-delivery proxies describe paced transport behavior only; they are not
playback acknowledgements. Every approved event must not contain transcript text,
audio, call ids, exception bodies, or secrets.

**Break-glass pilot transcript logging**

`SMARTPBX_PILOT_TRANSCRIPT_LOGGING=0` is the required normal state. For a
controlled pilot call with no customer traffic, a separately approved diagnostic
may set it to `1` only in the protected `.env.smartpbx`. This does not relax the
provider logger: raw STT interims, identifiers, prompts, KB context, tools,
credentials, and provider payloads remain redacted. It adds only these two local
records, with control characters escaped into a single line:

```text
smartpbx_pilot_transcript role=guest text='final dispatched guest phrase'
smartpbx_pilot_transcript role=kavya text='exact phrase submitted to TTS'
```

Render `docker compose --env-file .env.smartpbx --profile smartpbx config`, then
recreate only `kavya-smartpbx` with the same pinned image. Verify image identity,
health, and Flico/legacy Kavya isolation before placing the controlled call. Tail
only the dedicated class locally:

```sh
docker logs --since 2m -f kavya-smartpbx 2>&1 \
  | grep --line-buffered 'smartpbx_pilot_transcript'
```

Do not export raw logs. Immediately after diagnosis, restore
`SMARTPBX_PILOT_TRANSCRIPT_LOGGING=0`, recreate the same pinned image again, and
repeat the identity, health, and isolation checks. Docker's bounded log rotation
remains the retention limit for records already written.

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
wait_for_smartpbx_idle() {
  # Pre-flight-only idle gate (P1-2/P1-3): active_sessions==0, bounded at
  # 10 minutes. A pending transfer holds its session slot, so this already
  # covers a live transfer; transfer_enabled is a configuration flag and
  # never gates a recreate. If this never returns, a call is live -- do not
  # force the recreate.
  local token deadline status_json
  token=$(sed -n 's/^SMARTPBX_WS_TOKEN=//p' /opt/kavya/.env.smartpbx | head -n 1)
  deadline=$((SECONDS + 600))
  while :; do
    status_json=$(printf 'header = "X-Kavya-SmartPBX-Token: %s"\n' "$token" \
      | curl --silent --show-error --fail --connect-timeout 2 --max-time 5 http://127.0.0.1:8006/smartpbx/status --config -) || status_json=""
    if [[ -n $status_json ]] && printf '%s' "$status_json" | jq -e '.active_sessions == 0' >/dev/null; then
      return 0
    fi
    if (( SECONDS >= deadline )); then
      echo "SmartPBX did not go idle within 10 minutes -- abort; do not force a recreate while a call may be live" >&2
      exit 1
    fi
    sleep 5
  done
}
wait_for_smartpbx_idle
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
wait_for_smartpbx_idle() {
  # Pre-flight-only idle gate (P1-2/P1-3): active_sessions==0, bounded at
  # 10 minutes. A pending transfer holds its session slot, so this already
  # covers a live transfer; transfer_enabled is a configuration flag and
  # never gates a recreate. If this never returns, a call is live -- do not
  # force the recreate.
  local token deadline status_json
  token=$(sed -n 's/^SMARTPBX_WS_TOKEN=//p' /opt/kavya/.env.smartpbx | head -n 1)
  deadline=$((SECONDS + 600))
  while :; do
    status_json=$(printf 'header = "X-Kavya-SmartPBX-Token: %s"\n' "$token" \
      | curl --silent --show-error --fail --connect-timeout 2 --max-time 5 http://127.0.0.1:8006/smartpbx/status --config -) || status_json=""
    if [[ -n $status_json ]] && printf '%s' "$status_json" | jq -e '.active_sessions == 0' >/dev/null; then
      return 0
    fi
    if (( SECONDS >= deadline )); then
      echo "SmartPBX did not go idle within 10 minutes -- abort; do not force a recreate while a call may be live" >&2
      exit 1
    fi
    sleep 5
  done
}
wait_for_smartpbx_idle
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

`sinhala_tts_degraded` is `true` once `SMARTPBX_SINHALA_TTS_QUOTA_STICKY_AFTER`
(default 3, clamp `[1, 10]`) consecutive live Gemini Sinhala TTS calls in this
process have failed with a `quota_exceeded` provider error, and resets to
`false` the next time a live Gemini Sinhala TTS synthesis succeeds. It is
process-wide, not per-call: point uptime monitoring or an operator alert at it
to catch Gemini TTS quota exhaustion before a run of Sinhala guests hears only
the cached apology line. A single `smartpbx_media event=sinhala_tts_quota_degraded`
WARNING is logged once per degraded episode (never per turn) alongside it.

`sinhala_tts_model` reports whichever Gemini model most recently completed a
live Sinhala synthesis -- the default `SMARTPBX_SINHALA_GEMINI_TTS_MODEL`
(`gemini-3.1-flash-tts-preview`) until a quota/rate-limit hit moves it to a
fallback. The fallback chain is `SMARTPBX_SINHALA_GEMINI_TTS_MODEL` then
`SMARTPBX_SINHALA_GEMINI_TTS_FALLBACK_MODELS` (comma list, default
`gemini-2.5-flash-preview-tts,gemini-2.5-pro-preview-tts`; names must match
`^[a-z0-9.-]+$` or the whole list falls back to that default) -- same client,
same voice, same text, retried immediately within the turn. A model that hits
`quota_exceeded`/`rate_limited` is skipped for the rest of that quota day
(sticky per process, not per call) and restored at `SMARTPBX_SINHALA_TTS_MODEL_RESET_UTC_HOUR`
(default `7`, i.e. `07:00` UTC). Each fallback logs
`smartpbx_media event=sinhala_tts_model_fallback from=<model> to=<model> reason=quota_exceeded|rate_limited`
(model names only); `session_summary` counts them per call as
`tts_model_fallbacks`. `invalid_request`/`permission_denied`/`server_error`/
`unknown_provider_error` and any malformed-audio/timeout/HTTP failure never
fall back -- only a classified quota or rate-limit hit does.

### Sinhala fixed-phrase prewarm: persistent cache, pacing, and re-prewarm

The ~19 fixed Sinhala phrases (initial/tool fillers, keypad prompts, the
TTS-unavailable apology) are rendered once per process and replayed from
bytes -- a live Gemini TTS round trip is 2-5 s, too slow to sit in front of
the answer it exists to cover. Two problems this section addresses: a cold
container start used to burst all ~19 requests back-to-back, tripping
Gemini TTS's ~10 requests/minute cap and spending ~19% of the 100
requests/day cap on every restart; and there was no visibility into which
phrase failed or why.

**Persistent cache.** Rendered mu-law audio is written to
`SMARTPBX_SINHALA_PHRASE_CACHE_DIR` (default `/app/smartpbx_phrase_cache`,
bind-mounted from `./smartpbx_phrase_cache` -- same ownership pattern as
`chroma_db_smartpbx`: created and owned by the container's runtime user,
survives `docker compose up -d --force-recreate`, never shared with the
Twilio `kavya` service). Each file is named by a sha256 hash of
`(model, voice, text)` and contains nothing but raw mu-law bytes -- never
the phrase text, never JSON, never a caller transcript. On startup every
allowlisted phrase is loaded from disk first; only misses are synthesised.
**Deleting the directory is always safe** -- it costs exactly one Gemini TTS
re-render per phrase on the next prewarm, nothing more. Set
`SMARTPBX_SINHALA_PHRASE_CACHE_DIR=` (blank) to disable disk persistence
entirely and fall back to the pre-2026-09 in-memory-only behaviour.

**Paced, classified synthesis.** Misses are rendered sequentially with a
minimum spacing of `SMARTPBX_SINHALA_PREWARM_INTERVAL_SECONDS` (default
`7.0`, clamp `[0, 60]`), keeping a cold start under ~9 requests/minute. A
classified `rate_limited` error backs off (doubling the spacing, capped at
60 s, up to 3 retries against the same model before moving to the next one
in the fallback chain). A classified `quota_exceeded` error stops the whole
prewarm run immediately -- it does not keep spending the daily budget
discovering the same limit phrase by phrase -- marks that model exhausted via
the existing `SMARTPBX_SINHALA_TTS_MODEL_RESET_UTC_HOUR` chain state, and
leaves the remaining phrases to the next scheduled prewarm. Prewarm uses the
same model fallback chain as live calls: a phrase rendered on a fallback
model is cached under that model's key and is just as servable at playback
time as one rendered on the primary model (voice is identical); the summary
line and a dedicated `sinhala_phrase_prewarm_fallback_model` log line record
which model rendered it.

**Re-prewarm triggers.** A Sinhala call activation still retries prewarm
while any phrase is missing, but is now debounced to at most once per 10
minutes -- otherwise, once "not ready" can legitimately mean "quota
exhausted for the day", every incoming call would restart a full paced run.
Independently, one re-prewarm attempt is forced at the daily Gemini quota
reset boundary (`SMARTPBX_SINHALA_TTS_MODEL_RESET_UTC_HOUR`, default `7`,
i.e. `07:00` UTC), bypassing that debounce since it is a scheduled event, not
a caller-triggered retry.

**Observability.** The summary log line now carries the full picture:

```
smartpbx_media event=sinhala_phrase_prewarm rendered=<N> total=<N> ready=<bool>
  loaded_from_disk=<N> synthesised=<N> failed=<N>
  failure_codes=quota_exceeded:1,rate_limited:2 elapsed_ms=<N>
```

`failure_codes` is a comma-separated `code:count` tally over the same closed
vocabulary as `_GeminiTTSProviderError` plus `local_synthesis_failure`
(malformed audio, not a provider error) and `unknown_error` — never free text.
Each failed phrase also logs one line naming its position, not its text:

```
smartpbx_media event=sinhala_phrase_prewarm_phrase_failed index=<N> code=<code>
```

`/smartpbx/status` gains `sinhala_phrases_ready` (count of allowlisted
phrases currently servable without synthesis) and `sinhala_phrases_total`
(the fixed allowlist size, ~19) alongside the existing `sinhala_tts_model`
and `sinhala_tts_degraded` fields. `ready=true` in the summary line (and full
parity between the two status counts) still means every phrase rendered;
short of that, the initial-filler and tool-filler banks each become usable
as soon as at least one of their own variants is cached -- a partially warm
cache is not a broken cache.

## SmartPBX telemetry privacy and retention

Keep the cutover evidence aggregate-only and limited to the finite approved
event allowlist above. Do not export raw logs, transcript text, audio, payloads,
caller identifiers, or environment values. The cadence values are wire-delivery
proxies, not playback proof.

The SmartPBX Compose service uses Docker's `json-file` logging with
`max-size: "10m"` and `max-file: "3"`: local retention is therefore a 30 MB
maximum. Preserve that cap during operations; do not add a remote raw-log sink
or widen the approved event set without a separately reviewed privacy change.

### Durable telemetry archive

Docker's `json-file` logs belong to the *container*, not the image or the
service: `--force-recreate` deletes the outgoing container and its log files
with it, so any incident older than the current container is otherwise
unrecoverable. `scripts/deploy_smartpbx_image.sh` archives the outgoing
container's telemetry immediately before every recreate it performs (forward
deploy and rollback alike), filtered to exactly the approved event allowlist
above (pilot transcript lines included -- the operator has chosen to keep
`SMARTPBX_PILOT_TRANSCRIPT_LOGGING` on for this pilot), to a root-only file:
`/var/log/kavya-smartpbx/<utc-timestamp>-<old-image-id>.log` (mode `0600`,
directory mode `0700`). This is still aggregate-only, stays on the host, and
does not widen the approved event set.

Install the accompanying logrotate policy once, from the reviewed checkout:

```sh
sudo install -m 644 -o root -g root \
  /opt/kavya/ops/kavya-smartpbx-logrotate /etc/logrotate.d/kavya-smartpbx
```

This keeps 8 weekly, compressed, root-only archives. The three manual
`docker compose ... up -d --force-recreate` blocks elsewhere in this runbook
(TLS bootstrap, transfer drill activation, transfer drill revoke) do not run
the guarded script and therefore do not archive telemetry automatically --
prefer the guarded script for any recreate on a host carrying live traffic.

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
GHCR digest is pullable. Transfer being enabled
(`SMARTPBX_TRANSFER_DESTINATIONS_JSON` set to a real destination, as in
production) does **not** block a deploy: a pending transfer holds its session
slot, so the idle gate below already covers it.

Before touching anything, the helper polls `/health` and the authenticated
`/smartpbx/status` for **idleness** (`active_sessions == 0`), for a bounded
wait of up to 10 minutes,
aborting with a clear message if the line never goes idle in that window --
it never force-recreates a container that might be carrying a live call.
Once idle, it checks the existing SmartPBX image ID, repository digest, and
OCI revision, then records a local rollback alias, archives the outgoing
container's allowlisted telemetry (see "SmartPBX telemetry privacy and
retention" below), and recreates only `kavya-smartpbx`. The **post-recreate
and rollback** ready gate is readiness only (`/health` ok and
`/smartpbx/status` returns 200) -- it deliberately does not re-check
occupancy, so a call that connects in the few seconds after the new
container comes up never fails the deploy or triggers a rollback that would
itself cut that call.

```sh
# As root: deploy_smartpbx_image.sh NEW_TAG EXPECTED_SHA EXPECTED_DIGEST
/opt/kavya/scripts/deploy_smartpbx_image.sh "$NEW_TAG" "$EXPECTED_SHA" "$EXPECTED_DIGEST"
```

It rolls back once for ordinary errors and `INT`, `TERM`, or `HUP`, and verifies
the restored image identity and the unchanged healthy Flico and legacy Kavya
containers. `SIGKILL`, kernel panic, power loss, and host loss cannot run a
shell trap; an operator must inspect and recover those cases from the recorded
baseline/rollback alias. The helper never manages Nginx or mutates another
service. After a successful deploy it disarms rollback and then, best-effort
and never failing the deploy, removes stale `ghcr.io/taskforce-ai-dev/kavya`
tags -- keeping exactly the tag now running as `kavya-smartpbx`, the tag
running as the Twilio `kavya-voice-agent`, `rollback-local`, `latest`, and any
`stable-*` tag -- then runs `docker image prune -f` and
`docker builder prune -af`. It never runs an all-images prune, which would
remove a tag either running container still needs.

### Optional host-file integrity check (`SMARTPBX_HOST_FILES_SHA256`)

The generic `deploy.yml` rejects `agent=kavya` for every mode (image, fast,
build), so `/opt/kavya`'s host-side control plane — `docker-compose.yml`, the
two nginx vhosts, and every `scripts/*.sh` helper — is normally only ever
changed by an operator applying a reviewed diff by hand. As defence in depth
against that guard ever being loosened by mistake, the guarded deploy helper
supports an opt-in pre-recreate integrity check: if the env var
`SMARTPBX_HOST_FILES_SHA256` names a checksum manifest file, the helper
verifies `docker-compose.yml`, `nginx-smartpbx.conf`, `nginx-smartpbx-acme.conf`,
and every `scripts/*.sh` file under `/opt/kavya` against it with `sha256sum -c`
before recreating the container. A manifest that is missing, that omits any of
those files, or that fails a checksum, aborts the deployment before anything is
mutated. Leaving the variable unset (the default) skips the check entirely —
it is opt-in, not required.

Produce the manifest **from the reviewed checkout**, not from `/opt/kavya`
itself (checksumming files already on the host proves nothing about tampering
of those same files):

```sh
# Run on a trusted machine against the exact reviewed commit ($REVIEWED_FULL_COMMIT_SHA).
checkout=/path/to/a/clean/full-voice-agent/checkout
sha=$REVIEWED_FULL_COMMIT_SHA
manifest=/opt/kavya/host-files.sha256   # root-owned, mode 0600, outside the repo tree

: > "$manifest"
for f in docker-compose.yml nginx-smartpbx.conf nginx-smartpbx-acme.conf \
         scripts/deploy_smartpbx_image.sh scripts/update_smartpbx_sinhala_provider.sh \
         scripts/validate_english_voice_env.sh; do
  hash=$(git -C "$checkout" show "$sha:Kavya/$f" | sha256sum | cut -d' ' -f1)
  printf '%s  %s\n' "$hash" "$f" >> "$manifest"
done

sudo install -o root -g root -m 0600 "$manifest" /opt/kavya/host-files.sha256
```

List every `scripts/*.sh` file that exists in the reviewed checkout under
`Kavya/scripts/` — the check fails closed if any current `scripts/*.sh` file on
the host has no manifest entry, so an incomplete list blocks the next deploy
rather than silently skipping coverage. Then set
`SMARTPBX_HOST_FILES_SHA256=/opt/kavya/host-files.sha256` in the deploy
operator's shell (or export it inline before invoking the helper) before
running `deploy_smartpbx_image.sh`.
