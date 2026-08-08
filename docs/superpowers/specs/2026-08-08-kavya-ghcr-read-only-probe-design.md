# Kavya GHCR Read-Only Probe Gate

## Purpose

This design adds a narrow, trusted-operator confidence gate before the
immutable Kavya image publisher may run for a reviewed revision. It proves,
without changing the registry, that:

1. An immutable, caller-selected existing tag has the expected OCI revision
   label.
2. A workflow-derived canary tag is absent.

The first operational acceptance uses existing tag `37bfaf0` and expected
revision `37bfaf02f04ce7614b9674b1c867b78ab3c7d414`.

## Scope and non-goals

Implementation changes exactly these three in-scope surfaces:

- `.github/workflows/probe-kavya-image.yml`;
- `Kavya/tests/test_smartpbx_deployment.py`; and
- `.github/scripts/check-kavya-image-tag.sh`.

The workflow is usable only after it is merged to the protected default branch.
It has no source checkout or build context. It does not build, publish, push,
create, overwrite, delete, or otherwise mutate an image or tag. It does not
deploy, use environments, SSH, access a host or dashboard, read deploy secrets,
change Nginx, or activate transfer or MCP functionality.

The immutable publisher remains a separate, fail-closed single writer. The
shared probe helper becomes stricter by rejecting NUL-bearing registry captures;
that may turn a previously ambiguous publisher probe into `probe_failed`, but
does not relax any publisher decision or permit a write. Publisher workflow
changes, registry writes, transfer, and deploy are out of scope.

## Architecture and dispatch trust boundary

```text
trusted operator with repository dispatch authority
  | POST /repos/{owner}/{repo}/dispatches
  | type=kavya_image_read_only_probe
  | payload={existing_tag, expected_revision}
  v
repository_dispatch resolved by GitHub from protected default branch
  | default-branch workflow file, ref, and SHA
  v
one GitHub-hosted read-only job
  |-- validate event/ref/SHA and payload before tooling or registry access
  |-- checkout tooling at github.workflow_sha only
  |-- byte-exact existing marker and OCI revision checks
  `-- byte-exact absent internally-derived-canary marker check
  v
fixed safe markers only; no registry or production writes
```

The trigger is exactly:

```yaml
on:
  repository_dispatch:
    types: [kavya_image_read_only_probe]
```

For `repository_dispatch`, GitHub resolves `GITHUB_REF` to the default branch
and `GITHUB_SHA` to its last commit, and runs only when the workflow file is on
that branch. Thus neither the caller nor its payload selects a workflow file,
ref, or SHA. The job additionally fails closed before checkout, setup, login,
or registry access unless all of the following hold:

- `github.event_name` is `repository_dispatch` and `github.event.action` is
  `kavya_image_read_only_probe`;
- the event default-branch field is a safe branch name and `github.ref` is
  exactly `refs/heads/<event default branch>`;
- `github.sha` and `github.workflow_sha` are lowercase 40-hex SHAs and are
  byte-for-byte equal; and
- `github.workflow_ref` identifies this repository's
  `.github/workflows/probe-kavya-image.yml` at that same default-branch ref.

These checks are defense in depth against an unexpected Actions context; GitHub
default-branch dispatch semantics and branch protection establish the actual
workflow-selection boundary.

The job has one `ubuntu-latest` GitHub-hosted runner and exactly:

```yaml
permissions:
  contents: read
  packages: read
```

Concurrency is static: group `kavya-image-read-only-probe` with
`cancel-in-progress: false`. No caller value participates in the group.

## Authorization is distinct from job permissions

Dispatch authorization belongs to the human/operator credential, not this
workflow's `GITHUB_TOKEN`. A designated maintainer uses a fine-grained PAT with
the repository's required **Contents: write** dispatch permission (or an
equivalent repository-authorized credential) from a trusted operator machine.
The credential must not be placed in workflow inputs, repository secrets, logs,
or summaries. Repository access policy and protected-default-branch review are
the authorization controls for who may issue the event.

The job token is independently limited to `contents: read` and `packages: read`.
It is used only by `docker/login-action` to read GHCR; it cannot authorize the
operator dispatch and must never gain `packages: write`.

The documented manual dispatcher is intentionally explicit and sends no ref or
SHA selector:

```bash
repo='taskforce-ai-dev/full-voice-agent'
existing_tag='37bfaf0'
expected_revision='37bfaf02f04ce7614b9674b1c867b78ab3c7d414'
[[ "$existing_tag" =~ ^[0-9a-f]{7}$ ]]
[[ "$expected_revision" =~ ^[0-9a-f]{40}$ ]]
[[ "$existing_tag" == "${expected_revision:0:7}" ]]
gh api --method POST "repos/$repo/dispatches" \
  -f event_type=kavya_image_read_only_probe \
  -f "client_payload[existing_tag]=$existing_tag" \
  -f "client_payload[expected_revision]=$expected_revision"
```

The procedure is for a trusted, repository-authorized maintainer only; the
workflow does not attempt to infer caller identity from payload fields.

`workflow_dispatch` plus a branch/ref guard was rejected as the primary design:
its guards are useful defense in depth but its manual UI/API model permits a
selected ref, which weakens the simple default-branch execution story. An
external GitHub App was also rejected for this first trusted-operator dispatch:
it adds credential lifecycle, integration, and operational complexity without
improving the protected-default-branch resolution needed here.

## Payload, validation, and trusted tooling

`client_payload` has exactly these caller-controlled fields:

| Field | Required format | Operational value |
| --- | --- | --- |
| `existing_tag` | exactly seven lowercase hexadecimal characters | `37bfaf0` |
| `expected_revision` | exactly forty lowercase hexadecimal characters | `37bfaf02f04ce7614b9674b1c867b78ab3c7d414` |

The first trusted shell step receives a serialized `client_payload` and its two
values only through `env`. Before any checkout, action setup, login, probe,
pull, or inspection, it uses `jq -e` to require an object with exactly the two
string keys above, then rejects empty values, uppercase, prefixes, whitespace,
non-hex input, and any case where
`existing_tag != expected_revision[0:7]`. The image repository is fixed in
trusted workflow code. Callers cannot supply a repository, registry, digest,
source ref, workflow ref, workflow SHA, or canary name.

After validation, the only checkout is `actions/checkout@v7` at
`github.workflow_sha`, into `.probe-tools`, with `persist-credentials: false`.
The job executes only `.probe-tools/.github/scripts/check-kavya-image-tag.sh`.
It reuses the publisher's declared `actions/checkout@v7`,
`docker/setup-buildx-action@v4`, and `docker/login-action@v4` references. No
application source ref is checked out or executed.

The absent canary is derived only from validated GitHub repository ID, run ID,
and run attempt, as `probe-<repository-id>-<run-id>-<attempt>`. It is not a
payload field and its shape and maximum tag length are validated before use.

## Byte-exact read-only data flow

Every capture resides in a newly created temporary directory, with traps that
remove only that directory. Captured registry output is never printed. Contract
bytes are never recovered through command substitution (`$(cat ...)`, `$(...)`)
or line counting: expected bytes are written to temporary expected files and
compared with `cmp -s`.

1. Validate the event, default-branch ref/SHA/workflow binding, and both payload
   values as above.
2. Check out trusted tooling and initialize the publisher-parity Buildx and
   read-only GHCR login actions.
3. Run the trusted helper against the fixed-repository existing tag, capturing
   stdout and stderr. Require exit `10`. Write exactly
   `image_tag_state=existing\n` with `printf` to a temporary expected file and
   require `cmp -s` against captured stdout. Any byte difference, including a
   missing/final extra newline, whitespace, additional output, or NUL, fails.
4. Pull that existing tag with stdout and stderr captured and suppressed. Inspect
   only `org.opencontainers.image.revision`, also with both streams captured.
   Write `expected_revision` plus exactly one newline to a temporary expected
   file and require `cmp -s` against inspect stdout. Do not assign captured
   inspection bytes to a shell variable.
5. Derive and validate the canary, then run the same trusted helper with streams
   captured. Require exit `0`; compare stdout with a temporary file containing
   exactly `image_tag_state=absent\n` via `cmp -s`.
6. Emit only fixed markers: `probe_version=1`, `existing_tag_state=pass`,
   `existing_revision=pass`, `canary_state=pass`, and `probe_result=pass`.

Any unexpected exit, mismatched byte sequence, helper failure, authentication or
authorization issue, network/DNS/TLS/rate-limit/server error, malformed
metadata, unexpected event context, or ambiguous registry result fails closed.
An existing canary fails without overwrite, deletion, or cleanup. Failure output
is generic and fixed; it contains no raw registry errors, token material, layer
progress, image configuration, payload data, or captured bytes.

## Shared helper hardening

`check-kavya-image-tag.sh` remains the one shared classifier for both the probe
and immutable publisher. Immediately after `docker buildx imagetools inspect`
finishes and before either returning `existing` or loading/classifying the
captured result into a shell variable, it must detect literal NUL bytes in the
capture using a byte-safe operation (for example, `LC_ALL=C od -An -tx1 -v`
followed by an exact `00`-byte-token check). A NUL causes only
`image_tag_state=probe_failed` and exit `1`.

The helper must not load a NUL-bearing file into `registry_error`: Bash command
substitution cannot preserve NUL bytes and would make classification ambiguous.
After the NUL check, its existing exact absent-message allowlist and
authorization/network/ambiguous-error rejection remain fail-closed. It still
never echoes registry output. This applies even when Docker exits successfully,
so an anomalous capture cannot be reported as `existing`.

## Tests and implementation sequence

Implementation starts RED and turns GREEN. `Kavya/tests/test_smartpbx_deployment.py`
will statically parse the workflow with `yaml.BaseLoader` and dynamically execute
named workflow shell steps with local fake tools; no test contacts a registry.
It must cover:

- exactly the `repository_dispatch` trigger and
  `kavya_image_read_only_probe` type, no `workflow_dispatch`, and the
  default-branch event/ref/SHA/workflow-ref defense-in-depth checks;
- payload field schema, exact validation and tag/revision binding before every
  checkout, setup, login, or registry operation;
- one job, static concurrency, exactly the two read permissions, trusted
  `github.workflow_sha` tooling checkout, and no source/input checkout;
- byte-exact `cmp -s` expected-file checks for existing stdout, absent stdout,
  and OCI revision stdout, with no command substitution of captured contract
  bytes;
- fixed-repository existing probe, internal canary derivation, and exact `10`
  existing / `0` absent exit contracts;
- literal-NUL regression cases for helper captures on both Docker-success
  (`existing`) and Docker-failure (`absent`) paths; workflow existing and canary
  marker captures; and the inspect-revision capture. The harness must write real
  NUL bytes and use binary subprocess/file assertions rather than text-mode
  environment variables or strings that cannot represent NUL;
- suppression of captured errors, markers, revision bytes, pull output, inspect
  output, secrets, and image configuration; and
- no build, push, tag mutation, package write, deploy, SSH, host, dashboard,
  environment, transfer, or MCP action.

Existing publisher tests must continue to prove the publisher treats helper
failure as fail-closed and does not write after a NUL-induced `probe_failed`.
No dynamic registry test is introduced.

## Residual supply-chain risk and acceptance

`actions/checkout@v7`, `docker/setup-buildx-action@v4`,
`docker/login-action@v4`, and `ubuntu-latest` are mutable action/runner channel
references, not immutable pins. This is an explicit residual risk. The workflow
records only safe evidence: workflow commit SHA, declared action references,
runner image/version metadata, actual Buildx version, and fixed pass markers.
Human review compares those values and the setup-log resolved action SHAs;
changed resolution or unexpected runner/Buildx metadata fails acceptance. This
task does not repin publisher actions.

Acceptance is main-only: after exact-head CI, secret scanning, and independent
review approve the merged protected-default-branch revision, the trusted
operator dispatches the documented event without a ref selector. Acceptance
requires all fixed pass markers and the stated existing tag/revision. Only then
may a separate reviewed publisher run be considered for SHA `69ec0b3`.

Rollback is to stop using the probe, correct it in a reviewed protected-branch
change, and rerun the read-only gate. No registry or production state exists to
undo.

## Sources and decision notes

- GitHub documents `repository_dispatch` type filtering, payload availability,
  default-branch workflow-file requirement, and default-branch `GITHUB_REF` /
  `GITHUB_SHA` resolution: https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#repository_dispatch
- GitHub documents the dispatch REST endpoint, `event_type`, `client_payload`,
  and fine-grained token requirement of Contents write:
  https://docs.github.com/en/rest/repos/repos#create-a-repository-dispatch-event
- GitHub documents that each Actions job receives a repository-limited,
  short-lived `GITHUB_TOKEN`; job-token permissions are not caller
  authorization: https://docs.github.com/en/actions/concepts/security/github_token
- GitHub documents workflow-context values including workflow SHA/ref used by
  the defense-in-depth binding:
  https://docs.github.com/en/actions/reference/workflows-and-actions/contexts#github-context

## Self-review

There are no placeholders. The trigger, event type, protected-default-branch
resolution, pre-tooling validation, caller/job authorization distinction,
payload shapes, exact binding, byte-exact comparisons, NUL handling, test
coverage, residual action/runner risk, safe summary, main-only acceptance, and
read-only scope agree. `workflow_dispatch` is not an alternate trigger, and no
step creates, writes, publishes, deploys, transfers, or contacts a host.
