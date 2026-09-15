# SmartPBX Factory Console

Standalone owner-review UI for the factory facade. It is intentionally not part of `flico-dashboard` or the public TaskForce website. The deployment environment must place this package on the private origin and enforce owner access there; the client does not claim to provide that enforcement.

The client speaks the server contract directly:

- `POST /v1/jobs` with the exact non-secret intake fields.
- `GET /v1/jobs/{job_id}` for authoritative state and review digests.
- `POST /v1/jobs/{job_id}/inspect`, `/plan`, `/approve-knowledge`, `/approve-plan`, `/generate`, `/verify`, and `/open-pr`.
- Digest approval bodies are exactly `{ "digest": "<server-returned digest>" }`.

Mutations use same-origin credentials and the `X-Factory-Console-CSRF` header. Failed requests do not mutate the client job or timeline. Provisioning, DNS, provider setup, and deployment are not API operations and remain disabled in the UI.

Run `npm run typecheck`, `npm run contract-check`, and `npm run build` for static validation. The build script uses Next's Webpack path, which is the supported reproducible build path for this standalone UI.
