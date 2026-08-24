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
CLAUDE_MODEL=claude-sonnet-5
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
SMARTPBX_MAX_TOKENS=
SMARTPBX_CLAUDE_MAX_TOKENS=
SMARTPBX_INITIAL_FILLER_DELAY_SECONDS=
SMARTPBX_LLM_INITIAL_RESPONSE_TIMEOUT_SECONDS=
SMARTPBX_LLM_STALL_TIMEOUT_SECONDS=
SMARTPBX_CLAUDE_THINKING_STALL_TIMEOUT_SECONDS=
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

## Direct SmartPBX English reliability timing (Phase B)

Four env-tunable knobs govern the direct SmartPBX English provider round only
(OpenAI/Gemini/Claude, whichever `LLM_PROVIDER` selects). Twilio Media Streams
(Arabic/Sinhala/Tamil) and the Twilio ConversationRelay path are unaffected —
none of this timing applies outside a direct SmartPBX call.

- `SMARTPBX_INITIAL_FILLER_DELAY_SECONDS` (default `1.5`, clamp `[0.5, 5.0]`) —
  the one cancellable neutral filler for the first provider round of a call.
  Cancels the instant real content, a tool selection, a barge-in, a
  generation change, a transfer, or session finish pre-empts it.
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
SmartPBX English output budget and still governs the OpenAI and Gemini rounds
unchanged. Claude is the one exception:

- `SMARTPBX_CLAUDE_MAX_TOKENS` (default `600`, clamp `[200, 1024]`) — the
  Claude-only direct SmartPBX English output budget. Leave it blank to take
  the default.

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

Each Claude round logs exactly one privacy-safe outcome line —
`smartpbx_media event=llm_round_outcome provider=claude outcome=<enum>
stop_reason=<enum> output_tokens=<bounded n|unknown> attempt=<1-9>` — carrying
no text, no tool arguments and no caller identifiers. See the cutover-gate
allowlist below for the exact, closed field set. `outcome` is one of
`completed`, `max_tokens_truncated`, `true_empty`, `incomplete_tool_block`,
`malformed_tool_json`, or `stream_aborted`; anything other than `completed`
logs at WARNING.

`true_empty` and `stream_aborted` are deliberately separate. `true_empty` means
the model reported ending its turn (a `message_delta` or `message_stop`
arrived) having produced nothing — a model-behaviour signal. `stream_aborted`
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
`turn_stage`, `turn_summary`, `session_summary`, `echo_rejected`,
`agent_response`, `assistant_turn_delivery`, `audio_dump_written`,
`bad_tool_json`, `llm_round`, `llm_round_complete`, `llm_round_outcome`,
`llm_empty_response`,
`llm_error`, `llm_provider_degraded`, `llm_provider_failover`, `tool_execute`,
`tool_result`, `tool_error`, `tool_batch`, `tool_round_limit`, `tts_failure`,
`tts_interrupted`, `barge_in`, `guest_utterance`, `kb_error`,
`llm_stream_timeout`,
`silence_reprompt`, `stt_final`, `stt_post_dispatch_result`,
`stt_provider_final`, `stt_provider_interim`,
`capture_buffer_bounded`, `capture_final_buffered`, `capture_forced_dispatch`,
`stt_callback_drain`, `capture_mode_enter`, `capture_mode_exit`, `dtmf_collect_start`, and
`dtmf_collect_done`; unlisted event names are not permitted.
The protocol diagnostic record is emitted as
`event=smartpbx_protocol_diagnostic`.

The fixed, aggregate-only fields are `correlation_id`, `stage`, `outcome`,
`failure_class`, `active_sessions`, `duration_ms`, `turn_id`,
`session_trace_id`, and `provider` where the named event emits that field.
`correlation_id`, `turn_id`, and `session_trace_id` are opaque, local, randomly
generated identifiers and are never derived from dialog. The `provider` field is
a bounded provider enum: `openai`, `gemini`, `claude`, `elevenlabs`, or `azure`;
the `llm_stream_timeout` event additionally permits its normalized `unknown`
sentinel as documented below.

`llm_round_outcome` emits exactly four fields beyond `provider`, and no others:

| Field | Type | Permitted values |
| --- | --- | --- |
| `provider` | bounded enum | `claude` only (this event is Claude-specific) |
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

## SmartPBX telemetry privacy and retention

Keep the cutover evidence aggregate-only and limited to the finite approved
event allowlist above. Do not export raw logs, transcript text, audio, payloads,
caller identifiers, or environment values. The cadence values are wire-delivery
proxies, not playback proof.

The SmartPBX Compose service uses Docker's `json-file` logging with
`max-size: "10m"` and `max-file: "3"`: local retention is therefore a 30 MB
maximum. Preserve that cap during operations; do not add a remote raw-log sink
or widen the approved event set without a separately reviewed privacy change.

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
