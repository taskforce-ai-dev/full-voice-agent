# SmartPBX Factory CLI bootstrap

`create-smartpbx-agent` is review-only. It has no deploy, provision, DNS,
TLS, Client Connect, or production-host command.

Every stateful command requires a strict JSON config with no credential values:

```sh
create-smartpbx-agent new --output /absolute/path/company.json
create-smartpbx-agent bootstrap --config /absolute/path/factory.json
create-smartpbx-agent inspect --config /absolute/path/factory.json --manifest /absolute/path/company.json
create-smartpbx-agent plan --config /absolute/path/factory.json --manifest /absolute/path/company.json
create-smartpbx-agent generate <generation-id> --config /absolute/path/factory.json
create-smartpbx-agent resume <generation-id> --config /absolute/path/factory.json --approve-knowledge <digest> --approve-plan <digest>
create-smartpbx-agent verify <generation-id> --config /absolute/path/factory.json
create-smartpbx-agent open-pr <generation-id> --config /absolute/path/factory.json
```

`new` is the interactive, non-secret manifest wizard. It asks for company,
slug, languages, approved provider set, and approved knowledge path; it never
asks for credential material and writes a new mode-0600 review manifest only.

The config must specify all three lane primary paths, safe remote names and
exact canonical remote URLs, full 40-hex base SHAs, distinct target roots,
state root, catalogue, age recipient/policy references, SOPS/age binaries,
GitHub CI identity, and the three PR repository identities. `HEAD`, branches,
short SHAs, unknown fields, and credential-like values are rejected.

`credential_source_policy` currently permits the explicit `environment`
adapter only. Its `path` is an environment-variable *name*, not a value. The
CLI reads the value only in memory while SOPS encrypts it; it is never put in
argv, JSON config, generation state, output, or readiness reports.

`inspect` performs only read-only Git checks: it verifies primary checkout
identity, configured remotes, immutable revisions, clean primaries, and target
root collisions. It does not fetch, reset, clean, or check out anything.

`verify` first publishes the exact clean `smartpbx-agent-factory/<generation>`
branches after local generation has committed all three lanes. This is the one
external mutation before PR creation, solely to trigger review-branch CI; it
opens no PR and performs no deployment. Its first result is normally blocked
as CI pending. Re-run `verify` to query the exact committed SHAs.

`verify` accepts only authoritative GitHub check-run evidence that names each
exact committed lane SHA and artifact digest. A missing, pending, malformed, or
unprovable external result remains blocked. `open-pr` rechecks persisted
`ReadinessAuthority` evidence, pushes only `smartpbx-agent-factory/<generation>`
branches, opens backend then operations then website, writes deterministic
back-links, and stops at `THREE_PRS_OPENED`.
