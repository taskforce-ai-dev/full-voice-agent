# SmartPBX Factory CLI bootstrap

`create-smartpbx-agent` is review-only. It has no deploy, provision, DNS,
TLS, Client Connect, or production-host command.

Every stateful command requires a strict JSON config with no credential values:

```sh
create-smartpbx-agent bootstrap --config /absolute/path/factory.json
create-smartpbx-agent inspect --config /absolute/path/factory.json --manifest /absolute/path/company.json
create-smartpbx-agent plan --config /absolute/path/factory.json --manifest /absolute/path/company.json
create-smartpbx-agent generate <generation-id> --config /absolute/path/factory.json
create-smartpbx-agent resume <generation-id> --config /absolute/path/factory.json --approve-knowledge <digest> --approve-plan <digest>
create-smartpbx-agent verify <generation-id> --config /absolute/path/factory.json
create-smartpbx-agent open-pr <generation-id> --config /absolute/path/factory.json
```

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

`verify` accepts only authoritative GitHub check-run evidence that names each
exact committed lane SHA and artifact digest. A missing, pending, malformed, or
unprovable external result remains blocked. `open-pr` rechecks persisted
`ReadinessAuthority` evidence, pushes only `smartpbx-agent-factory/<generation>`
branches, opens backend then operations then website, writes deterministic
back-links, and stops at `THREE_PRS_OPENED`.
