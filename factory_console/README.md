# SmartPBX Factory Console

This dependency-free WSGI façade is owner-only and review-only. It accepts a
`Cf-Access-Jwt-Assertion` only after an injected verifier has validated the
JWT signature, issuer, and audience. Production construction requires the
expected issuer/audience and both configured owner subject and email; no
default verifier or identity bootstrap is provided.

Every mutating request additionally requires the exact configured HTTPS Origin
and a server-side CSRF verifier that binds its token to the owner subject,
method, and path. Configure that verifier with external secret management; do
not put a CSRF signing secret in this package or browser payloads.

The review response exposes only a review status and digest. The owner must
POST that exact digest to `approve-knowledge` before `approve-plan` becomes
available, and must separately POST the exact plan digest before generation.
There is no deploy or provisioning API.
