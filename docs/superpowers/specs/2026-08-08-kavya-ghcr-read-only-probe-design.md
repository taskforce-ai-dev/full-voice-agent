# Kavya GHCR Read-Only Probe Gate

## Purpose

This design adds a narrow, trusted-operator confidence gate before the separate
Kavya image publisher may run for a reviewed revision. It proves, without
changing the registry, that an already-existing, caller-selected tag has the
expected OCI revision label at the time of the probe, and that a
workflow-derived canary tag is absent at that time.

### What a green probe does and does not establish

A green probe is deliberately narrow evidence. It establishes only:

- the probe workflow ran from the protected default branch at a commit equal to
  `github.workflow_sha`, having passed every terminal trust check below;
- the caller-selected tag resolved to a manifest in the fixed GHCR repository at
  probe time, and the image at that resolved digest carried an
  `org.opencontainers.image.revision` label byte-equal to the supplied
  `expected_revision`; and
- the internally derived canary tag was reported absent by the shared
  classifier, which is a live negative control on that classifier and on the
  read path.

It does not establish any of the following, and no green result should be read
as if it did:

- that `expected_revision` is a reviewed commit, is reachable from `main`, or
  corresponds to the source that produced the image. The two payload fields are
  mutually constrained (`existing_tag == expected_revision[0:7]`) so the caller
  supplies one value, and the publisher derives both the tag and the label from
  the same commit SHA — the revision check therefore holds by construction for
  every image the publisher produced. It detects a mismatched or third-party
  image, not an unreviewed one;
- anything about image contents, provenance attestations, or build integrity;
- that the canary was absent for any reason other than never having been
  written. It embeds the run ID, so its absence is guaranteed by construction;
- continuing tag immutability. This is point-in-time evidence. The publisher is
  the only intended in-repository writer, but an out-of-band package writer can
  move a tag after a successful probe. Consumers must use the separately
  verified digest where that property matters; and
- that the hosted runner or the pinned actions are uncompromised.

The gate is **mechanically enforced** on the publisher, not merely a convention.
See "Mechanical publisher gate" below for what that enforcement does and does
not cover.

## Scope and non-goals

Implementation changes exactly these four surfaces:

- `.github/workflows/probe-kavya-image.yml`;
- `.github/scripts/check-kavya-image-tag.sh`;
- `.github/workflows/build-kavya-image.yml`; and
- `Kavya/tests/test_smartpbx_deployment.py`.

The probe has no source checkout or build context. It does not build, publish,
push, create, overwrite, delete, or otherwise mutate an image or tag. It does
not deploy, use environments, SSH, access a host or dashboard, read deploy
secrets, change Nginx, or activate transfer or MCP functionality.

The publisher changes in scope are the action pins, explicit runner label,
timeout, and the terminal trust and probe-gate steps described under "Mechanical
publisher gate". The publisher's trigger is unchanged: it remains
`workflow_dispatch` with the same two inputs. No other workflow is broadened
into scope. The shared helper is stricter by rejecting
NUL-bearing registry captures; that can make the publisher fail closed with
`probe_failed`, but never permits a write.

## Architecture and dispatch trust boundary

```text
trusted operator
  | POST repository_dispatch {existing_tag, expected_revision}
  v
default-branch workflow selected by GitHub
  |-- terminal trust and payload validation before any `uses:` action or registry/network command
  v
GitHub-hosted read-only probe
  |-- trusted helper at github.workflow_sha
  |-- byte-exact existing-tag and revision checks
  `-- byte-exact absent internally-derived canary check
  v
closed workflow-authored safe summary only
```

The trigger is exactly:

```yaml
on:
  repository_dispatch:
    types: [kavya_image_read_only_probe]
```

GitHub documents that `repository_dispatch` runs only when the workflow file is
on the default branch and sets its ref and SHA to that branch and its latest
commit. The first shell step uses Bash and `jq` for validation; it nevertheless
fails before checkout, setup, login, or any `uses:` action or registry/network
command unless every one of these terminal checks passes:

- `github.event_name == repository_dispatch` and
  `github.event.action == kavya_image_read_only_probe`;
- `github.repository == github.event.repository.full_name ==
  taskforce-ai-dev/full-voice-agent`;
- `github.event.repository.default_branch == main`;
- `github.ref == refs/heads/main`, `github.ref_name == main`,
  `github.ref_type == branch`, and `github.ref_protected == true`;
- `github.sha` and `github.workflow_sha` are each lowercase 40-hex strings and
  are byte-for-byte equal; and
- `github.workflow_ref ==
  taskforce-ai-dev/full-voice-agent/.github/workflows/probe-kavya-image.yml@refs/heads/main`.

The workflow must pass those values to the first step through named environment
variables, including a serialized `github.event` for structural payload
validation. The `repository_dispatch` webhook payload exposes `branch`, and it
is therefore accessible as `github.event.branch`; however, GitHub does not
document security, protection, or workflow-selection semantics for that field.
The workflow deliberately ignores it and binds execution using the documented
`github.ref`, `github.ref_name`, `github.ref_type`, `github.ref_protected`,
`github.sha`, `github.workflow_ref`, and `github.workflow_sha` values instead.
Every negative case exits through one generic fixed failure marker before any
`uses:` action or registry/network command.

The probe job is one GitHub-hosted job with this static shape:

```yaml
runs-on: ubuntu-24.04
timeout-minutes: 30
permissions:
  contents: read
  packages: read
```

Concurrency remains static: group `kavya-image-read-only-probe` with
`cancel-in-progress: false`; no caller value participates in the group.

## Dispatcher and authorization

Dispatch authorization belongs to the trusted operator credential, not the
job's `GITHUB_TOKEN`. A repository-authorized maintainer uses the documented
repository-dispatch permission from a trusted machine. A fine-grained PAT or
GitHub App user/installation token must be scoped to this repository with
repository `Contents: write`; a classic PAT must have the `repo` scope. The
credential never appears in workflow inputs, repository secrets, logs, or
summaries.

The dispatcher is terminal and fail-fast before `gh api`; it sends no ref or
SHA selector:

```bash
set -Eeuo pipefail
fail() { printf '%s\n' 'dispatcher_validation=fail' >&2; exit 1; }

repo='taskforce-ai-dev/full-voice-agent'
existing_tag='37bfaf0'
expected_revision='37bfaf02f04ce7614b9674b1c867b78ab3c7d414'

[[ "$repo" == 'taskforce-ai-dev/full-voice-agent' ]] || fail
[[ "$existing_tag" =~ ^[0-9a-f]{7}$ ]] || fail
[[ "$expected_revision" =~ ^[0-9a-f]{40}$ ]] || fail
[[ "$existing_tag" == "${expected_revision:0:7}" ]] || fail

gh api --method POST "repos/$repo/dispatches" \
  -f event_type=kavya_image_read_only_probe \
  -f "client_payload[existing_tag]=$existing_tag" \
  -f "client_payload[expected_revision]=$expected_revision"
```

`workflow_dispatch` is not an alternate trigger because it permits selecting a
ref. The workflow does not infer operator identity from payload fields.

`contents: read` is needed by checkout; `packages: read` is needed by the GHCR
login/read path. Every action can access `github.token`; action pinning and
least job permissions therefore apply to checkout, Buildx, login, and build/
push equally. `persist-credentials: false` prevents checkout from persisting
its credential in local Git configuration; it does not revoke `github.token` or
prevent other actions from accessing the context token. The probe never grants
`packages: write`; the publisher retains that distinct write permission.

## Mechanical publisher gate

`build-kavya-image.yml` enforces the probe rather than assuming it. Two terminal
steps run first, before any `uses:` action -- including both checkouts -- so no
credentialed step executes when the gate fails.

**Step 1, publisher trust.** The publisher applies the same ref binding as the
probe: `github.event_name == workflow_dispatch`, `github.repository`,
`github.ref == refs/heads/main`, `github.ref_name == main`,
`github.ref_type == branch`, `github.ref_protected == true`, lowercase-40-hex and
byte-equal `github.sha` and `github.workflow_sha`, and
`github.workflow_ref == taskforce-ai-dev/full-voice-agent/.github/workflows/build-kavya-image.yml@refs/heads/main`.
This is load-bearing, not decoration. GitHub executes the *selected ref's* copy
of a workflow, so a gate inside an unpinned publisher could simply be deleted on
a branch and dispatched from there. Binding the publisher to protected main is
what makes the commit comparison below mean anything. It constrains only where
the workflow definition comes from; `inputs.ref` still selects the source built.

**Step 2, probe gate.** Using the job's `GITHUB_TOKEN`, the publisher lists the
probe workflow's runs and requires at least one run that is `completed` with
conclusion `success`, on `head_branch` `main`, with `path`
`.github/workflows/probe-kavya-image.yml`, whose `head_sha` is byte-equal to the
publisher's own verified `github.sha`, and whose `updated_at` is within the
freshness window. Both workflows independently prove they ran at that commit, so
equality of the two SHAs means the probe validated the same tooling revision this
publisher run is executing.

The request filters live in the URL query string:

```
repos/<owner>/<repo>/actions/workflows/probe-kavya-image.yml/runs?per_page=100&branch=main&status=success&head_sha=<sha>
```

This form is required, not stylistic. `gh api -f key=value` on a GET request is
either rejected outright (HTTP 404) or accepted while the parameter is silently
dropped as a filter, which would hand the gate an unfiltered result set that
happens to contain successful runs of other commits. Every server-side filter is
therefore re-verified locally with `jq` against each returned run, and the
`head_sha` is validated as lowercase 40-hex before it is interpolated into the
URL.

**Freshness window: 24 hours**, measured from the run's `updated_at` (the
completion time of a finished run) to the publisher's clock. The probe's evidence
is explicitly point-in-time: it shows the tag resolved and the canary was absent
*at probe time*, and a tag can be moved out of band afterwards. A window long
enough to cover an ordinary review-then-publish cycle within one working day, but
short enough that a months-old probe cannot be reused as if it were current, is
the trade-off; 24 hours is that compromise. Re-running the probe is cheap and
read-only, so a stale gate costs one dispatch.

**Failure modes, all fail-closed.** The gate exits non-zero, with no captured
bytes echoed, when: the trust step rejects the context; `GATE_SHA` is not
lowercase 40-hex; the `gh api` call fails; the response is not a JSON object,
is truncated, or is empty; `total_count` is absent, non-numeric, or zero; the
response is a full page of 100 (which cannot be disambiguated without
paginating); no returned run satisfies every re-verified field; every matching
run is older than the window; or a candidate `updated_at` does not parse. The
response body is captured to a temporary directory removed by a trap and is
never printed; the summary records only `probe_gate` and the matched
`probe_run_id`.

**Permissions.** The publisher gains `actions: read` and nothing else. That is
the least grant that can read workflow run history; it confers no write of any
kind. The publisher retains `contents: read` and `packages: write`. No
environment and no required reviewers are introduced.

**What the gate does not do.** It does not verify that `inputs.ref` or
`expected_sha` were reviewed -- it binds the *tooling* commit, not the source
being built. It cannot detect a tag moved between the probe and the publisher
run, which is why the publisher still re-probes the tag and verifies the pushed
digest. And it inherits the probe's own evidentiary limits set out in Purpose.

## Payload, trusted tooling, and action pins

`client_payload` contains exactly these two caller-controlled string fields:

| Field | Required format | Operational value |
| --- | --- | --- |
| `existing_tag` | exactly seven lowercase hexadecimal characters | `37bfaf0` |
| `expected_revision` | exactly forty lowercase hexadecimal characters | `37bfaf02f04ce7614b9674b1c867b78ab3c7d414` |

The first step uses `jq -e` against the environment-supplied serialized event
to require exactly those two keys, both strings, then validates the shapes and
requires `existing_tag == expected_revision[0:7]`. It validates all trust
values above in the same terminal step. The image repository is fixed in
trusted workflow code. Callers cannot provide a registry, digest, source ref,
workflow ref, workflow SHA, or canary name.

Pin every action used by either the probe or publisher to these full commits,
preserving the version comments in both workflows:

```yaml
- uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7
- uses: docker/setup-buildx-action@bb05f3f5519dd87d3ba754cc423b652a5edd6d2c # v4
- uses: docker/login-action@dbcb813823bdd20940b903addbd779551569679f # v4
- uses: docker/build-push-action@53b7df96c91f9c12dcc8a07bcb9ccacbed38856a # v7
```

On 2026-08-08, the official GitHub REST `git/ref/tags/<tag>` endpoints reported
each requested tag object as a `commit`, so no annotated-tag dereference was
needed. The verification commands are:

```bash
curl --fail --silent --show-error \
  https://api.github.com/repos/actions/checkout/git/ref/tags/v7
curl --fail --silent --show-error \
  https://api.github.com/repos/docker/setup-buildx-action/git/ref/tags/v4
curl --fail --silent --show-error \
  https://api.github.com/repos/docker/login-action/git/ref/tags/v4
curl --fail --silent --show-error \
  https://api.github.com/repos/docker/build-push-action/git/ref/tags/v7
```

For an annotated tag, resolve its returned tag object through the official
`git/tags/<sha>` endpoint until the target is a commit, and pin that full commit
instead. Do not substitute a tag object SHA.

The probe's only checkout is the pinned checkout action at
`github.workflow_sha`, into `.probe-tools`, with `persist-credentials: false`.
It executes only `.probe-tools/.github/scripts/check-kavya-image-tag.sh`. The
publisher pins both of its checkout invocations and all of its other actions to
the same mappings. No application source ref is checked out or executed by the
probe.

## Byte-exact read-only data flow

Every capture is in a newly-created temporary directory that its trap removes.
Captured registry output is never printed. Contract bytes are never recovered
through command substitution or line counting; expected bytes are written with
`printf` to temporary files and compared with `cmp -s`.

1. Perform the terminal context and payload validation above.
2. Check out trusted tooling, run pinned Buildx setup, and perform pinned
   read-only GHCR login.
3. Run the trusted helper on the fixed-repository existing tag with both streams
   captured. Require exit `10`, and compare stdout byte-for-byte to a temporary
   file containing `image_tag_state=existing\n`. A missing or extra final
   newline, whitespace, output, or NUL fails.
4. Pull that existing tag with both streams captured and suppressed. Inspect
   only `org.opencontainers.image.revision`, capture both streams, and compare
   stdout byte-for-byte to `expected_revision` followed by one newline.
5. Derive the canary only from validated repository ID, run ID, and attempt as
   `probe-<repository-id>-<run-id>-<attempt>`. Validate its digits-only shape
   and maximum tag length, run the helper, require exit `0`, and compare stdout
   byte-for-byte to `image_tag_state=absent\n`; the same byte-exact failure
   rules apply.
6. Only after every check succeeds, emit the exact workflow-authored safe
   summary contract below. Any failure emits only `probe_result=fail` from
   workflow-authored summary logic.

The workflow-authored safe summary is a closed, line-oriented allowlist. It
contains exactly these keys and no others:

| Key | Exact permitted value/form |
| --- | --- |
| `workflow_commit` | the verified `github.workflow_sha`, matching `^[0-9a-f]{40}$` |
| `checkout_action` | `actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1` |
| `setup_buildx_action` | `docker/setup-buildx-action@bb05f3f5519dd87d3ba754cc423b652a5edd6d2c` |
| `login_action` | `docker/login-action@dbcb813823bdd20940b903addbd779551569679f` |
| `runner_label` | `ubuntu-24.04` |
| `runner_os` | `Linux` |
| `runner_arch` | `X64` |
| `buildx_version` | a value matching `^v[0-9][A-Za-z0-9._+-]{0,62}$`; the value is ASCII and at most 64 bytes |
| `existing_tag` | a value matching `^[0-9a-f]{7}$` |
| `expected_revision` | a value matching `^[0-9a-f]{40}$` |
| `probe_version` | `1` |
| `existing_tag_state` | `pass` |
| `existing_revision` | `pass` |
| `canary_state` | `pass` |
| `probe_result` | `pass` on success or `fail` as the sole generic workflow-authored failure marker |

`buildx_version` is the validated version token, not the raw `docker buildx
version` output: the documented/observed output begins with the Buildx name and
then a `v`-prefixed version token (for example, `v0.27.0`), which satisfies the
stated ASCII expression. The workflow rejects an output from which it cannot
extract exactly one such token. It must not emit raw JSON, any other payload
field, registry output, token material, layer progress, image configuration, or
captured bytes. An existing canary fails without overwrite, deletion, or
cleanup.

Workflow-authored summaries and the captured helper, pull, inspect, and
Buildx-version streams emit only the specified safe lines or a generic failure
marker. GitHub runner infrastructure and pinned actions may emit their own
operational or failure logs; those logs are outside this allowlist and remain
part of the residual GitHub/action trust boundary.

## Shared helper hardening

`check-kavya-image-tag.sh` remains the shared classifier for probe and
publisher. Immediately after `docker buildx imagetools inspect` finishes and
before returning `existing` or loading/classifying the capture into a shell
variable, it must detect literal NUL bytes with a byte-safe operation (for
example `LC_ALL=C od -An -tx1 -v` and an exact `00` token check). A NUL returns
only `image_tag_state=probe_failed` and exit `1`, including when Docker exits
successfully.

The helper must never load a NUL-bearing file into `registry_error`: Bash
command substitution cannot preserve NUL bytes. After that check, its exact
absent-message allowlist and authorization/network/ambiguous-error rejection
remain fail-closed, and it never echoes registry output.

### The absent-message allowlist is unverified against live GHCR

The three absent messages the allowlist matches -- `manifest unknown: <ref>`,
`no such manifest: <ref>`, and
`failed to resolve source metadata for <ref>: not found` -- were written from
expectation, **not observed against live GHCR**. They are almost certainly
incomplete. `docker buildx` prints command failures through
`fmt.Fprintf(cmd.Err(), "ERROR: %v\n", err)` (`docker/buildx`,
`cmd/buildx/main.go`), and the helper captures stderr via `2>&1`, so a real miss
most likely arrives as an `ERROR: `-prefixed line that matches no allowlist
entry. A second code path in the same file prints `"ERROR: %+v"` with a stack
formatter and no trailing newline when debug output is enabled.

The `ERROR: ` prefix is therefore confirmed from source, but the message tail is
resolver-dependent and is **not** confirmed. Because the allowlist compares the
entire capture byte-for-byte, a partially-known string cannot be added safely, so
no `ERROR: ` variants are pre-registered. Guessing them would either miss anyway
or, worse, match something that is not an absent tag.

**Expected first-dispatch failure.** The most likely outcome of the first live
probe at plan Step 8 is `canary_state` failing with the helper exiting `2`
(`image_tag_state=probe_unrecognized`). That is the design working: it fails
closed rather than guessing, and it blocks publishing until resolved.

**The remedy, and the only permitted one.** Read the actual capture from the
failed run's log, then add that exact, complete message to the allowlist in a
reviewed pull request, with the run URL cited as its provenance. Do **not**
reintroduce substring or glob matching to make it pass -- that is precisely the
defect removed in this revision, where an unanchored `*5[0-9][0-9]*` matched the
digits inside the image reference itself and misclassified ~82% of canaries.
Widen only on an exact observed string, one message at a time.

The distinct exit codes exist to make this decidable from the run log without
printing captured bytes: exit `2` means the registry said something unrecognised
and widening may be appropriate; exit `1` means the helper rejected the argument
or the capture structurally (bad argument, mixed case, NUL bytes) and widening is
never the answer.

## Runner and Buildx evidence

Both workflows use `ubuntu-24.04` and `timeout-minutes: 30`. The probe records
the fixed requested label and the documented `${{ runner.os }}` and
`${{ runner.arch }}` values after constraining them to the expected
`Linux`/`X64` forms. It does not read, require, or expose undocumented
`ImageOS` or `ImageVersion` environment variables.

The Buildx provenance is the pinned `docker/setup-buildx-action` commit above.
The workflow captures `docker buildx version`, validates and records only the
`buildx_version` token defined by the summary contract, as diagnostic evidence
of what that pinned setup action installed; no post-run metadata proves an
uncompromised action or runner.
Acceptance also retains GitHub's generated `Set up job` log block, including
its image/version information, as run evidence only. The hosted runner image
remains mutable residual trust and the design makes no immutability claim for
it.

Both workflows also depend on tooling preinstalled in that image rather than
pinned by this design: `bash`, `jq`, `cmp`, `od`, `date`, the Docker CLI, and --
for the publisher's probe gate -- the `gh` CLI. Their presence, versions, and
behaviour are part of the same mutable runner trust. `jq` and `gh` are the two
that carry security weight here: `jq` parses the dispatch payload and the probe
run list, and `gh` performs the gate's authenticated query. A change in either
could alter validation or gating outcomes, which is a further reason both are
used only in fail-closed positions.

The trust guarantees in this design are conditional on GitHub's execution
environment and the pinned action commits being uncompromised. Pinning removes
mutable tag resolution from both the read-only probe and write-capable
publisher; it cannot make compromised code or a compromised hosted runner safe.

## Tests and implementation sequence

Implementation starts RED and turns GREEN. `Kavya/tests/test_smartpbx_deployment.py`
statically parses both workflows with `yaml.BaseLoader` and dynamically runs
named shell steps with local fake tools; no test contacts a registry. It must
cover:

- the exact dispatch-only trigger/type and each terminal trust check, including
  negative tests for either repository identity mismatch, non-main default
  branch/ref/ref name/ref type, false protection, non-lowercase/non-40-hex or
  unequal SHA values, and incorrect workflow ref; it proves that varying the
  webhook-payload `github.event.branch` neither authorizes nor changes workflow
  selection or validation, and proves every failure precedes checkout, setup,
  login, or any `uses:` action or registry/network command (the validation
  itself may use Bash and `jq`);
- the dispatcher has `set -Eeuo pipefail`, terminal validation before `gh api`,
  no ref/SHA selector, and exactly the validated payload keys;
- exact payload object/schema/binding checks, and the complete safe-summary
  allowlist: every listed key/value form, including the lower-40-hex
  `workflow_commit`, three fixed action identifiers, fixed runner values,
  `buildx_version` regex and 64-byte maximum, validated `existing_tag` and
  `expected_revision`, and each fixed pass/fail marker; it rejects every
  unlisted key, overlong value, unvalidated payload value, and additional
  workflow-authored output;
- one probe job, static concurrency, `ubuntu-24.04`, `timeout-minutes: 30`,
  exactly the two read permissions, `github.workflow_sha` tooling checkout,
  no source/input checkout, and no `ImageOS`/`ImageVersion` contract;
- every probe and publisher action use is exactly the four documented full SHA
  pins with retained version comments; both workflows have the fixed runner
  label and timeout, and the publisher has no dispatch change;
- byte-exact `cmp -s` expected-file checks for existing, absent, and OCI
  revision stdout, without command substitution of captured contract bytes;
- fixed-repository existing probe, internal canary derivation, exact exit `10`
  existing / `0` absent contracts, and the scope ban on build/push/tag mutation
  in the probe;
- literal-NUL helper captures on Docker-success and Docker-failure paths plus
  workflow existing/canary marker and inspect-revision captures. The harness
  writes real NUL bytes and uses binary subprocess/file assertions; and
- suppression of captured errors, markers, revision bytes, pull output,
  secrets, and image configuration; no probe deploy, SSH, host, dashboard,
  environment, transfer, or MCP action.

Existing publisher tests continue to prove helper failures, including a
NUL-induced `probe_failed`, block writing. No dynamic registry test is added.

## Acceptance and rollback

After exact-head CI, secret scanning, and independent review approve the
merged protected-main revision, the trusted operator sends the documented
dispatch without a ref selector. Acceptance requires the validated
`existing_tag=37bfaf0`,
`expected_revision=37bfaf02f04ce7614b9674b1c867b78ab3c7d414`, every fixed pass
marker, the pinned-action resolution, the requested runner label and documented
runner context values, Buildx diagnostic, and the GitHub-generated `Set up job`
image/version block. This evidence is point-in-time only and does not establish
future tag immutability or action/runner compromise resistance.

Only then may the separately reviewed publisher be considered for SHA
`69ec0b3`. That sequencing is enforced by the publisher's own probe
gate described above: a publisher run started without a fresh, successful,
same-commit probe fails before any credentialed step. Because the gate binds on
`github.sha`, a commit landing on `main` between the probe and the publisher
dispatch invalidates the evidence and requires a fresh probe -- which is the
intended behaviour, since the tooling would no longer be the revision the probe
validated. Rollback is to stop using the probe, correct it in a reviewed
protected-main change, and rerun the read-only gate. There is no probe-created
registry or production state to undo.

## Sources and decision notes

- GitHub event semantics for `repository_dispatch`: https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#repository_dispatch
- GitHub contexts, including ref, workflow identity, runner, and token
  contexts: https://docs.github.com/en/actions/reference/workflows-and-actions/contexts
- GitHub secure-use guidance for full-length action commit SHAs: https://docs.github.com/en/actions/how-tos/security-for-github-actions/security-guides/security-hardening-your-deployments
- GitHub token permissions and access model: https://docs.github.com/en/actions/concepts/security/github_token
- GitHub REST Create a repository dispatch event endpoint and its token
  permissions: https://docs.github.com/en/rest/repos/repos#create-a-repository-dispatch-event
- GitHub CLI `gh api` manual: https://cli.github.com/manual/gh_api
- GitHub-hosted runner labels and lifecycle: https://docs.github.com/en/actions/reference/runners/github-hosted-runners
- Official tag sources, resolved on 2026-08-08: https://github.com/actions/checkout/tree/v7, https://github.com/docker/setup-buildx-action/tree/v4, https://github.com/docker/login-action/tree/v4, and https://github.com/docker/build-push-action/tree/v7

## Self-review

There are no placeholders. Scope, documented-context trust validation and
deliberate non-reliance on webhook `branch`, dispatcher fail-fast behavior and
credential scopes, full action pins, explicit runner/timeout, byte-exact and
NUL protections, closed workflow-authored safe-summary contract, residual
GitHub/action log trust, point-in-time tag evidence, publisher constraints,
tests, acceptance, rollback, and official sources agree. The probe has no
registry write, deploy, host, transfer, or MCP scope.
