# SmartPBX Factory Console

This owner-only, review-only WSGI façade has a production entrypoint at
`factory_console.entrypoint:application`. It reads only the root-owned
`/etc/factory-console/runtime.json`, its root-owned policy, the existing
root-owned factory configuration, and a root-owned CSRF secret file. It has no
environment-variable or test-mode production fallback.

`requirements-prod.txt` pins the production WSGI server and PyJWT crypto
dependency. The entrypoint validates `Cf-Access-Jwt-Assertion` with PyJWT,
the configured Cloudflare Access JWKS URL, fixed `RS256`, exact issuer and
single audience, expiry/not-before/issued-at claims, `type=app`, and exact
configured owner `sub` and `email`. Errors do not contain token values or
claims.

Every mutating request additionally requires the exact configured HTTPS Origin
and a server-signed, expiring CSRF double-submit token bound to the owner
subject and fixed review-write scope. `GET /v1/csrf` is the only issuance
endpoint: it still requires Cloudflare Access, exact Origin, and the
bootstrap header. It sets a `Secure; SameSite=Strict` cookie; the static UI
obtains it before its first mutation. No caller can request a token for an
arbitrary method or path.

The review response exposes only a review status and digest. The owner must
POST that exact digest to `approve-knowledge` before `approve-plan` becomes
available, and must separately POST the exact plan digest before generation.
There is no deploy or provisioning API.
