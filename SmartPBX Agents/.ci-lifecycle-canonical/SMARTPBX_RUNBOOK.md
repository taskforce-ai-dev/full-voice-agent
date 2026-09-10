# SmartPBX runtime candidate runbook

REVIEW-ONLY

activation: blocked

This candidate is not approved for rendering, image publication, runtime execution,
or traffic activation. Verify the immutable image reference, OCI revision, explicit
runtime-file allowlist, environment allowlist, loopback binding, resource limits,
and authenticated status endpoint in a separate approval.

## SmartPBX AI Provider V07 compatibility boundary

The authenticated `/smartpbx/status` field `protocol_version` is the generated
agent's compatibility marker and is `smartpbx-ai-provider-v07`. It is not a
vendor-negotiated wire field, a Client Connect dashboard value, or an extra
WebSocket event.

The media contract remains exact `g711_ulaw` at `8000` Hz on
`/ws/v1/smartpbx/media`. The accepted compatibility events remain `connected`,
`start`, `media`, `dtmf`, `hangup`, and `stop`; this marker does not alter media
framing, WebSocket authentication, or URL routing.

This inquiry-only candidate exposes no transfer tool. If a separately approved
transfer integration is added, its Dialog MCP argument key must remain the
literal `destination number` and its non-empty disposition must remain
`tier=BYPASS`; do not substitute the PDF's `destination_number` spelling.

## Website-demo relay privacy

The website-demo ConversationRelay URL carries a short-lived, one-time relay
ticket in its query string. Its exact Nginx location uses `access_log off` so
the admission credential cannot be retained by the default request log. Do not
replace that directive with a log format containing `$request`, `$request_uri`,
or query arguments. The isolated website-demo Uvicorn command uses
`--no-access-log` so its container logger cannot retain the WebSocket query
either. The signed `/voice/demo-incoming` callback also consumes
the exact `From=client:<identity>` previously issued by `/api/voice-token`;
unknown, expired, or reused identities must return 403 before any relay ticket
is created.
