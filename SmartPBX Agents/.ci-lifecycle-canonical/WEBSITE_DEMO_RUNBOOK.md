# Website-demo runtime candidate runbook

REVIEW-ONLY

activation: blocked

The `website-demo` Compose profile is isolated from the SmartPBX profile and
binds only `127.0.0.1:18999`. Its browser-token, signed-webhook, and
ConversationRelay adapter are source-extracted, but remain review-only. Do not
point a TwiML App, public proxy, or the website at this candidate until the CI
lifecycle lane proves browser-token issuance, signed-webhook admission,
relay-session disconnect, and release lifecycle evidence.

Website-demo Twilio credentials are supplied only to `smartpbx-canonical-ci-fixture-website`.
Never copy them to `smartpbx-canonical-ci-fixture`, and never add SmartPBX WSS/account
credentials to the website-demo service.
