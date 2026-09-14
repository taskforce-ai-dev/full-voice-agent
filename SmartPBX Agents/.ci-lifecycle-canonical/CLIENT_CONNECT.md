# Client Connect candidate sheet

REVIEW-ONLY

activation: blocked

The proposed media endpoint is `wss://smartpbx-canonical-ci-fixture.invalid/ws/v1/smartpbx/media`.
Use the `X-SmartPBX-Canonical-CI-Token` request header. The authenticated status marker is
`smartpbx-ai-provider-v07`; it is not a dashboard media setting. Keep the dashboard
media format at `g711_ulaw` and `8000` Hz. No endpoint is active from this candidate;
the status path requires the same header and remains inaccessible without it.
