# Private Factory Console runbook

## Scope and invariant

This runbook describes a private, owner-only **review console** for the
existing SmartPBX Factory. It can expose only the review lifecycle actions
listed in `policy.json`: `inspect`, `plan`, `generate`, `verify`, and
`open-pr`. The factory's own existing approval and readiness gates remain
authoritative.

The console must not create infrastructure, DNS records, tunnels, production
services, or client connections. It has no `deploy` or `provision` endpoint;
Nginx returns 404 for those names as a defense in depth measure. A generated
PR remains review material, not a production release.

## Private origin layout

1. Run the console as the dedicated unprivileged `factory-console` account.
   It must bind only to `127.0.0.1:8401` (or a separately approved `::1`
   equivalent). Do not publish this port through a firewall, Docker, or a load
   balancer.
2. Install the Nginx template as a dedicated server that binds only to
   `127.0.0.1:8400`. It is the only local HTTP origin configured for this
   console. Its access log deliberately records only timestamp, status, method,
   path without query string, byte count, and duration.
3. Run the dedicated Cloudflared unit as its own unprivileged account. It makes
   outbound tunnel connections; no public inbound listener is required or
   permitted for the console origin. Its one named hostname maps to
   `http://127.0.0.1:8400`, and the final `http_status:404` ingress rule must
   remain last.
4. Keep the Cloudflare tunnel credential file outside this repository, owned by
   root and readable only by the Cloudflared account (for example mode `0640`,
   group `cloudflared`). The template contains only its placeholder path.
   Store the console policy and environment file root-owned and mode `0640` or
   tighter; they contain no secret values.

Do not use a quick tunnel for this service. Before enabling a host-local tunnel
configuration, an authorized operator must run Cloudflared's documented
ingress validation against the private, substituted file and confirm its named
hostname resolves to the loopback origin. That check is a local configuration
check, not production authorization.

## Cloudflare Access and application authorization

Configure one Access application for exactly one private console hostname.
Its Access policy must allow the single owner identity and deny every other
identity. Do not use an email-domain, group, wildcard, service-token, bypass,
or public policy as a substitute for this owner-only rule.

Set the per-origin Cloudflared Access gate to `required: true` with the exact
application AUD tag. The console then performs an independent, fail-closed
verification for every request:

1. Read only `Cf-Access-Jwt-Assertion`; reject no value, multiple values, or a
   malformed value. Never authenticate from `CF_Authorization`, a forwarded
   email, a `X-Forwarded-*` identity field, or a console session cookie alone.
2. Fetch/cache signing keys only from the configured issuer's JWKS endpoint.
   Verify an `RS256` signature selected by `kid`; reject any unsupported
   algorithm or missing signing key.
3. Require exact equality for `iss`, a single configured Access application
   `aud`, `type=app`, and the configured owner `sub` and verified `email`.
   Validate `exp`, `nbf`, and `iat` with a small documented clock-skew bound.
   Do not case-fold, wildcard-match, or use a prefix for the owner fields.
4. Fail closed if the JWKS cannot be refreshed. A bounded cache may continue
   only until its documented expiry; it must not turn a key-fetch failure into
   anonymous or stale authorization.

The configured owner subject and email are identifiers, not credentials. They
must be copied into a root-owned host-local policy only after an operator
independently verifies them in the Access application. Do not place the real
identifiers in this repository.

## Approval-bound review actions

The UI must require a fresh, explicit owner approval before `generate` and
again before `open-pr`. An approval record must bind all of: action,
generation ID, manifest digest, knowledge-review digest, plan digest, issuing
owner subject, issued timestamp, expiry, and a single-use nonce. A redirect,
page load, checkbox retained from an older plan, or approval for a different
digest is not approval.

Before `open-pr`, the console calls the factory's existing verify/readiness
path and must display a blocked result rather than attempting a workaround.
It must not synthesize CI evidence, override a failed readiness gate, or
promote a generated PR into a release.

## Audit, privacy, and incident handling

Audit events are metadata only: event name, request ID, HTTP method, normalized
route, action, generation ID, approval nonce identifier, result code, duration,
and a fixed `actor=owner` label. Never record request/response bodies,
knowledge content, manifests, prompts, cookies, authorization headers, JWTs,
JWT claims, secret names, credential values, exception dumps, or full query
strings. Normalize errors to stable error codes before logging.

On an authorization failure, log the error code and request ID only, return a
generic 403/401 response, and do not echo token parsing errors. On suspected
credential or token exposure, revoke/rotate it through the provider, preserve
only safe audit metadata, and follow the repository security policy. Do not
copy the exposed value into this runbook, an issue, or an incident report.

## Host checks before any authorized activation

- Verify `factory-console` and `cloudflared` are distinct unprivileged service
  accounts and their service files use the supplied restrictive settings.
- Verify policy and tunnel credential file ownership/modes, and that no secret
  appears in an environment file, process argument, unit file, or log.
- Verify `ss -ltnp` shows only loopback listeners for ports 8400 and 8401.
- Run `nginx -t` against the host configuration and the documented
  `cloudflared tunnel ingress validate` against the substituted private file.
- Run `systemd-analyze security factory-console.service` and
  `systemd-analyze security cloudflared-factory-console.service`; investigate
  any regression introduced by local unit edits.
- Test from an unauthorized identity that Access and the origin both reject the
  request, then test the owner path with a deliberately wrong AUD, subject, and
  email in a non-production test environment. No test should log a token.
- Re-run `python3 -m unittest factory_console_hardening.tests.test_policy -v`.

These checks are prerequisites for a separately approved operational change.
They do not authorize deployment, production provisioning, or a factory action.
