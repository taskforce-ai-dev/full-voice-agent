# SmartPBX Factory Console hardening bundle

This is an additive, **review-only** operations bundle for a separate private
admin console. It is not a Factory runtime, installer, deployment workflow, or
network-provisioning tool. It adds no command or endpoint for `deploy` or
`provision`.

The bundle supplies a strict non-secret policy, reverse-proxy and authenticated
Cloudflare Tunnel templates, hardened systemd units, and a runbook. All
identifiers, domains, and credential locations are placeholders. Secret values
belong only in an approved host-local secret mechanism; never commit them or
place them in process arguments, environment files, logs, tickets, or reviews.

## Security boundary

```
owner browser -> Cloudflare Access -> authenticated cloudflared tunnel
              -> Nginx 127.0.0.1:8400 -> static standalone console
                                          /v1 -> console 127.0.0.1:8401
```

Cloudflared validates the configured Access application before forwarding a
request. The console independently validates the signed
`Cf-Access-Jwt-Assertion` itself, using the issuer JWKS and exact issuer,
single configured audience, `type=app`, time claims, exact `sub`, and exact
`email` claim when the Access application supplies it. It must fail closed on a missing, invalid, duplicate, or
unverifiable assertion. It must never use client-supplied identity headers or
the cookie as an authentication substitute.

`policy.json` is deliberately non-secret and validates these minimums:

- only an IP-literal loopback application origin;
- the Cloudflare Access assertion header, issuer/JWKS relationship, audience,
  and exact owner subject/email configuration;
- a 16 KiB maximum body, with uploads disabled;
- the complete review lifecycle, including digest-bound `approve-knowledge`
  and `approve-plan`, plus explicit approval for `generate` and `open-pr`;
- an exact deny list for `deploy` and `provision`; and
- journal audit metadata with no body, authorization header, or JWT claim
  capture.

See [RUNBOOK.md](RUNBOOK.md) for the operator process.

## Validation

Run the isolated stdlib tests from the repository root:

```sh
python3 -m unittest factory_console_hardening.tests.test_policy -v
```

The test suite validates the policy and static hardening invariants. It does
not validate a live Cloudflare account, tunnel, identity provider, process, or
production service.

The standalone UI is built as static files (`factory-console/out`) and served
only by the loopback Nginx origin. The service template runs the implemented
`python -m factory_console verify-access-config` preflight, then the pinned
Gunicorn WSGI server on `127.0.0.1:8401`. It loads only root-owned files named
by `/etc/factory-console/runtime.json`; it does not read environment settings.
See [RUNBOOK.md](RUNBOOK.md) for the exact host-local files and the mandatory
Access-claim prerequisite.

## References

- Cloudflare documents that origins receive `Cf-Access-Jwt-Assertion` and
  should validate its signature, issuer, and application AUD:
  <https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/authorization-cookie/validating-json/>.
- Cloudflare documents the application-token identity fields, time claims, and
  `type=app` application token shape:
  <https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/authorization-cookie/application-token/>.
- Cloudflare documents authenticated ingress and a terminating 404 rule for
  locally managed tunnels:
  <https://developers.cloudflare.com/tunnel/features/locally-managed-tunnels/configuration-file/>.
- Nginx documents that `client_max_body_size` rejects oversized request bodies:
  <https://nginx.org/en/docs/http/ngx_http_core_module.html#client_max_body_size>.
