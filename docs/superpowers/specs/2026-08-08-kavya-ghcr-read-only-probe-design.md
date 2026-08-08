# Kavya GHCR Read-Only Probe Gate

## Purpose

This design adds a narrow, manually dispatched confidence gate before the
immutable Kavya image publisher is allowed to run for a reviewed revision. The
gate proves two things against the registry without changing it:

1. A maintainer-selected, already immutable tag resolves to the expected OCI
   revision label.
2. A canary tag generated inside the workflow is absent.

The first operational run uses existing tag `37bfaf0` and expected revision
`37bfaf02f04ce7614b9674b1c867b78ab3c7d414`.

## Scope and non-goals

The implementation will add `.github/workflows/probe-kavya-image.yml`. It is
available only after merge to the protected default branch; it is not a way to
run unmerged workflow code.

This workflow is strictly read-only. It does not publish an image, build an
image, push or pull a tag into existence, delete a tag, create a registry
artifact, deploy, use environments, use SSH, read deploy secrets, mutate a
production host or dashboard, change environment permissions, modify Nginx,
or activate transfer or MCP functionality. It has no source checkout and no
build context.

Image publication remains the responsibility of the existing immutable
publisher and is deliberately out of scope for this gate.

## Architecture

```text
maintainer inputs (existing tag, expected revision)
                    |
                    v
protected-default-branch workflow_dispatch
                    |
                    v
GitHub-hosted read-only job
  |-- trusted tooling checkout at github.workflow_sha
  |-- authenticated existing-tag probe -> exit 10 + fixed "existing" state
  |-- pull existing image by tag -> exact OCI revision label comparison
  `-- internally derived canary probe -> exit 0 + fixed "absent" state
                    |
                    v
fixed pass/fail markers only; no registry writes
```

The workflow has one `ubuntu-latest` GitHub-hosted job. Its permissions are
exactly:

```yaml
contents: read
packages: read
```

It must not request `packages: write` or any other permission. Concurrency is
static with group `kavya-image-read-only-probe` and
`cancel-in-progress: false`; no user input participates in the group name.

## Trust boundary and action parity

The first checkout is the only tooling checkout. It uses
`actions/checkout@v7`, checks out `github.workflow_sha` into an isolated
directory, and sets `persist-credentials: false`. This makes the script and
workflow-supporting tooling come from the protected workflow revision, never
from an input ref.

The job reuses the reviewed `.github/scripts/check-kavya-image-tag.sh` from
that isolated checkout. It also uses the publisher's pinned
`docker/setup-buildx-action@v4` and `docker/login-action@v4` versions, plus
the same pinned checkout version. The login uses `GITHUB_TOKEN` for registry
read access only. No source-controlled input or checkout is executed, and the
job never checks out an application source ref.

## Inputs and validation

`workflow_dispatch` has exactly two maintainer-supplied inputs:

| Input | Required format | Operational value |
| --- | --- | --- |
| `existing_tag` | exactly seven lowercase hexadecimal characters | `37bfaf0` |
| `expected_revision` | exactly forty lowercase hexadecimal characters | `37bfaf02f04ce7614b9674b1c867b78ab3c7d414` |

Validation runs before login, setup, probing, or image access. Empty values,
uppercase values, prefixes, whitespace, non-hex values, or any other shape
fail closed. The tag is combined with the workflow's fixed Kavya image
repository internally; callers never supply a repository, registry, digest,
or arbitrary image reference.

The absent canary is not an input. The job derives it internally from immutable
GitHub repository and run identifiers, including repository ID, run ID, and
run attempt, with a fixed `probe-` prefix. That creates a per-run name without
accepting attacker-controlled tag material.

## Read-only data flow and failure handling

1. Validate both inputs exactly.
2. Check out trusted tooling at `github.workflow_sha` and initialize the
   publisher-parity Buildx and GHCR login actions.
3. Run the trusted tag-probe script for the fixed-repository existing tag.
   Capture its output rather than streaming it. Require exactly exit `10` and
   the fixed `image_tag_state=existing` marker.
4. Pull that existing image on the ephemeral GitHub-hosted runner. Inspect
   only the `org.opencontainers.image.revision` label, without printing image
   configuration, and require it to exactly equal `expected_revision`.
5. Derive the internal canary and run the same trusted script. Require exactly
   exit `0` and `image_tag_state=absent`.
6. Emit only the fixed markers `probe_version=1`,
   `existing_tag_state=pass`, `existing_revision=pass`,
   `canary_state=pass`, and `probe_result=pass`. Do not emit raw command
   output.

If the canary resolves as existing, the job fails; it never overwrites,
deletes, or cleans up a registry object. A malformed marker, any exit other
than the required exit for that probe, or any authentication, authorization,
network, DNS, TLS, rate-limit, server, or ambiguous registry failure fails
closed. The workflow must suppress raw registry errors, token material, and
image configuration from logs and summaries. Its output contract contains
only fixed state, action-version, and pass/fail markers.

## Security invariants

- The protected default-branch workflow revision is the sole tooling trust
  root; input/source refs are never checked out or executed.
- The registry token is read-only by permission and is never echoed.
- The only image material downloaded is an existing image for provenance
  verification on an ephemeral GitHub-hosted runner.
- There are no write-capable registry commands (`build`, `push`, tag create,
  tag delete) and no deploy, SSH, host, dashboard, or environment commands.
- A tag is acceptable only when both its probe state and OCI revision label
  match exactly; a successful network response alone is insufficient.
- All uncertain states terminate the job before a success marker.

## Tests and implementation sequence

Implementation starts RED, then turns GREEN. The existing Kavya deployment
test module will gain static tests that parse the workflow and assert:

- schema, one-job GitHub-hosted runner, static concurrency, and exactly the
  two read permissions;
- trusted `github.workflow_sha` checkout, isolated path,
  `persist-credentials: false`, and no input/source checkout;
- pinned checkout, setup-Buildx, and login action versions match the
  publisher;
- exact input validation and internal-only canary derivation;
- exact `10`/`existing` and `0`/`absent` probe handling;
- pull-by-existing-tag provenance verification against the exact OCI revision
  label;
- absence of build, push, tag mutation, deploy, SSH, host, environment, and
  secret-output commands; and
- fixed, non-sensitive output markers with no raw registry error, token, or
  image-configuration output.

The existing dynamic tests for `check-kavya-image-tag.sh` remain the behavior
coverage for the probe script itself. No dynamic registry test is introduced
in the deployment suite.

## Rollout, rollback, and acceptance evidence

1. Implement the workflow and static tests RED then GREEN in one reviewed PR.
2. Require exact-head CI and gitleaks, followed by independent Sol review.
3. Merge only after those checks approve the exact reviewed head.
4. Dispatch the probe from `main` with the stated existing tag and expected
   revision. On the exact pinned runner/action versions, require existing-tag
   exit `10`, a matching revision label, and canary exit `0`.
5. Preserve the run URL, fixed markers, action versions, input revision, and
   exact commit as acceptance evidence. Do not record credentials, digests,
   account data, raw registry output, or image configuration.
6. Only after that evidence passes may the immutable publisher be run for
   reviewed SHA `69ec0b3`.

Because the workflow has no external writes, rollback is simply to stop using
the workflow, correct the reviewed workflow or tests, merge the correction,
and rerun the read-only probe. There is no registry or production state to
undo.

## Self-review

This design contains no placeholders. The trust boundary, input shapes, probe
exit contracts, read-only permissions, and rollout gate agree: the probe uses
only protected-default-branch tooling, cannot accept a source ref, and cannot
create or change registry or production state.
