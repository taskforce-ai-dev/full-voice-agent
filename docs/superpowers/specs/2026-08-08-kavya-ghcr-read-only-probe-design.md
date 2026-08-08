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

## Trust boundary and declared action parity

The first shell step in trusted workflow code validates both inputs before any
checkout, setup, login, or registry access. After validation, the first
checkout is the only tooling checkout. It uses
`actions/checkout@v7`, checks out `github.workflow_sha` into an isolated
directory, and sets `persist-credentials: false`. This makes the script and
workflow-supporting tooling come from the protected workflow revision, never
from an input ref.

The job reuses the reviewed `.github/scripts/check-kavya-image-tag.sh` from
that isolated checkout. It uses the same reviewed declared references as the
publisher: `actions/checkout@v7`, `docker/setup-buildx-action@v4`, and
`docker/login-action@v4`. The login uses `GITHUB_TOKEN` for registry read
access only. No source-controlled input or checkout is executed, and the job
never checks out an application source ref.

Those action tags and `ubuntu-latest` are version/channel references, not
immutable SHA-pinned actions or runner images. This is an explicit residual
supply-chain limitation. The acceptance record captures the workflow commit
SHA, declared action references, GitHub runner image/version metadata, and
the actual Buildx version. Any changed action-ref resolution or unexpected
runner or Buildx version fails the human review and acceptance comparison; the
workflow cannot cryptographically prove action-tag immutability. This design
does not expand scope to repin publisher actions.

## Inputs and validation

`workflow_dispatch` has exactly two maintainer-supplied inputs:

| Input | Required format | Operational value |
| --- | --- | --- |
| `existing_tag` | exactly seven lowercase hexadecimal characters | `37bfaf0` |
| `expected_revision` | exactly forty lowercase hexadecimal characters | `37bfaf02f04ce7614b9674b1c867b78ab3c7d414` |

The first trusted shell step performs validation before checkout, setup, login,
probing, or image access. Empty values, uppercase values, prefixes,
whitespace, non-hex values, or any other shape fail closed. The tag is
combined with the workflow's fixed Kavya image repository internally; callers
never supply a repository, registry, digest, or arbitrary image reference.

The absent canary is not an input. The job derives it internally from immutable
GitHub repository and run identifiers, including repository ID, run ID, and
run attempt, with a fixed `probe-` prefix. That creates a per-run name without
accepting attacker-controlled tag material.

## Read-only data flow and failure handling

1. Validate both inputs exactly.
2. Check out trusted tooling at `github.workflow_sha` and initialize the
   publisher-parity Buildx and GHCR login actions.
3. Run the trusted tag-probe script for the fixed-repository existing tag.
   Capture stdout and stderr rather than streaming either. Require exactly exit
   `10` and the single complete stdout line `image_tag_state=existing`; reject
   extra lines, whitespace, or substring-only matches.
4. Pull that existing image on the ephemeral GitHub-hosted runner. Capture and
   suppress stdout and stderr from both `docker pull` and image inspection.
   Inspect only the `org.opencontainers.image.revision` label, without
   printing layer progress or image configuration, and require it to exactly
   equal `expected_revision`.
5. Derive the internal canary and run the same trusted script. Capture stdout
   and stderr and require exactly exit `0` and the single complete stdout line
   `image_tag_state=absent`; reject extra lines, whitespace, or
   substring-only matches.
6. Emit only the fixed markers `probe_version=1`,
   `existing_tag_state=pass`, `existing_revision=pass`,
   `canary_state=pass`, and `probe_result=pass`. Do not emit raw command
   output.

If the canary resolves as existing, the job fails; it never overwrites,
deletes, or cleans up a registry object. A malformed marker, any exit other
than the required exit for that probe, or any authentication, authorization,
network, DNS, TLS, rate-limit, server, or ambiguous registry failure fails
closed. Any captured pull or inspect failure emits only a generic fixed failure
marker. The workflow must suppress raw registry errors, token material, layer
output, and image configuration from logs and summaries. Its output contract
contains only fixed state, action-version, and pass/fail markers.

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
- declared checkout, setup-Buildx, and login action references match the
  publisher, while tests do not misrepresent mutable tags or
  `ubuntu-latest` as immutable pins;
- first-shell-step input validation before checkout, setup, login, and
  registry access;
- exact input validation and internal-only canary derivation;
- exact `10`/single-line `existing` and `0`/single-line `absent` probe
  handling, with no substring acceptance;
- pull-by-existing-tag provenance verification against the exact OCI revision
  label with suppressed pull and inspect stdout/stderr;
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
   revision. Require existing-tag exit `10`, a matching revision label, and
   canary exit `0` using the same reviewed declared action references as the
   publisher.
5. Preserve the run URL, fixed markers, workflow commit SHA, declared action
   references, GitHub runner image/version metadata, actual Buildx version,
   input revision, and exact commit as acceptance evidence. Compare these
   values in human review; changed action-ref resolution or unexpected runner
   or Buildx version fails acceptance. Do not record credentials, digests,
   account data, raw registry output, layer output, or image configuration.
6. Only after that evidence passes may the immutable publisher be run for
   reviewed SHA `69ec0b3`.

Because the workflow has no external writes, rollback is simply to stop using
the workflow, correct the reviewed workflow or tests, merge the correction,
and rerun the read-only probe. There is no registry or production state to
undo.

## Self-review

This design contains no placeholders. The trust boundary, input shapes, exact
single-line probe contracts, read-only permissions, residual action-tag and
runner-channel limitation, and rollout gate agree: the probe uses only
protected-default-branch tooling, cannot accept a source ref, and cannot
create or change registry or production state.
