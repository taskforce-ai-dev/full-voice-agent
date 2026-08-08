# Kavya GHCR Read-Only Probe Gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a protected-default-branch, repository-dispatched, read-only GHCR provenance and absent-canary gate that must pass before the immutable Kavya publisher runs for reviewed SHA `69ec0b3`.

> **Scope of the evidence.** A green probe establishes that the workflow ran from
> protected `main` at its own workflow commit, that the caller-selected tag
> resolved to a manifest whose `org.opencontainers.image.revision` label matched
> the supplied revision at probe time, and that the derived canary was absent. It
> does **not** establish that the revision is reviewed or reachable from `main`,
> anything about image contents, or continuing tag immutability. The "probe before
> publisher" rule IS mechanically enforced by the publisher's own gate (see the
> design document's "Mechanical publisher gate"), but that gate binds the *tooling*
> commit, not the source being built. See the design document's Purpose section for
> the full statement.

**Architecture:** A single GitHub-hosted probe job validates two tightly typed inputs before any registry-affecting setup, then checks out only workflow-revision tooling into `.probe-tools`. It uses that trusted tag-probe script to prove a known immutable tag exists and an internally derived canary is absent, while a separate read-only pull verifies the known image's OCI revision label. The job has no source checkout, build context, registry write operation, or deployment capability.

**Tech Stack:** GitHub Actions YAML, Bash with `set -Eeuo pipefail`, Docker Buildx setup and GHCR login actions, Docker CLI read operations, Python `pytest`, and PyYAML `BaseLoader`.

## Global Constraints

- Create `.github/workflows/probe-kavya-image.yml`; it is usable only after merge to the protected default branch.
- Use one `ubuntu-24.04` GitHub-hosted job with `timeout-minutes: 30` and exactly `contents: read` and `packages: read`; do not add environments or other permissions.
- Pin every action in this workflow and in `.github/workflows/build-kavya-image.yml` to the full commit SHA recorded in the design document, retaining the `# v7` / `# v4` version comment on each `uses:` line. Record workflow commit SHA, those pinned action identifiers, the fixed runner label with the documented `runner.os` / `runner.arch` values, and the actual Buildx version for human acceptance comparison. The runner image itself remains mutable residual trust.
- The first trusted shell step is terminal: it validates the dispatch context (`github.event_name`, `github.event.action`, both repository identities, default branch, `github.ref`, `github.ref_name`, `github.ref_type`, `github.ref_protected`, lowercase-40-hex and equal `github.sha` / `github.workflow_sha`, and the exact `github.workflow_ref`) and then the `client_payload` schema — exactly two string keys, `existing_tag` as seven lowercase hex characters, `expected_revision` as forty lowercase hex characters, and `existing_tag == expected_revision[0:7]` — before checkout, setup, login, probing, or image access. Tests cover every negative case.
- Pass context and payload values only through step `env` and step outputs; never interpolate `${{ … }}` inside a Bash block. Parse workflow YAML in tests with `yaml.BaseLoader` so YAML 1.1 does not coerce `on` to a boolean.
- Check out `github.workflow_sha` into `.probe-tools` with `persist-credentials: false`; never check out or execute an input/source ref.
- The fixed Kavya image repository is internal workflow code. Inputs never select registry, repository, digest, source ref, or canary name.
- Existing-tag probe must accept only exit `10` and exactly one line `image_tag_state=existing`; canary probe must accept only exit `0` and exactly one line `image_tag_state=absent`.
- Capture and suppress all probe, pull, and inspect stdout/stderr. Emit fixed markers only; never print registry errors, tokens, layer progress, image configuration, secrets, or environment dumps.
- Prohibit package writes, build/build-push/push/tag mutation, SSH, rsync, Nginx, systemctl, compose, environment mutation, deploy commands, and host/dashboard actions.
- No publisher dispatch is permitted until the main-branch probe succeeds for the revision being published. The **first** publish uses bootstrap mode, because the registry is empty and a strict probe cannot pass against a tag nobody has pushed (see the design document's "Bootstrap mode and the first-image deadlock"); the stale `37bfaf0` / `37bfaf02f04ce7614b9674b1c867b78ab3c7d414` pair never had a published image and is not the acceptance input. Enforcement is mechanical: the publisher hard-fails before any credentialed step unless a completed, successful run of `probe-kavya-image.yml` exists on `main` at the publisher's own `github.sha`, finished within 24 hours. Its `permissions` therefore include `actions: read` alongside `contents: read` and `packages: write`.

---

## File Map

- Create: `.github/workflows/probe-kavya-image.yml` — the single repository-dispatched read-only probe job.
- Modify: `Kavya/tests/test_smartpbx_deployment.py` — static workflow contract tests and BaseLoader parser helpers.
- Modify: `.github/workflows/build-kavya-image.yml` — action SHA pins, runner label, job timeout, and the terminal trust + probe-gate steps; no dispatch change.
- Read only: `.github/scripts/check-kavya-image-tag.sh` — executable probe contract: existing is exit `10`, absent is exit `0`, all uncertain cases exit `1`.

## Interfaces

| Interface | Definition |
| --- | --- |
| Dispatch trigger | `repository_dispatch` with type `kavya_image_read_only_probe` only; no `workflow_dispatch`, so no caller may select a ref. |
| Dispatch payload | `client_payload` with exactly `existing_tag: string` matching `^[0-9a-f]{7}$` and `expected_revision: string` matching `^[0-9a-f]{40}$`, and `existing_tag == expected_revision[0:7]`. |
| Trusted checkout | `actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1` with `ref: ${{ github.workflow_sha }}`, `path: .probe-tools`, and `persist-credentials: false`. |
| Existing probe | `.probe-tools/.github/scripts/check-kavya-image-tag.sh "$image:$existing_tag"` returns `10` and one stdout line `image_tag_state=existing`. |
| Canary probe | The same script receives `"$image:probe-$GITHUB_REPOSITORY_ID-$GITHUB_RUN_ID-$GITHUB_RUN_ATTEMPT"`, returns `0`, and one stdout line `image_tag_state=absent`. |
| Provenance result | The tag is resolved to a digest once; `docker pull` and `docker image inspect` both read that digest, and the label bytes are compared with `cmp -s` against a `printf`-written expected file. Nothing is logged from the digest/pull/inspect streams. |
| Safe evidence | Exactly `workflow_commit`, `checkout_action`, `setup_buildx_action`, `login_action`, `runner_label`, `runner_os`, `runner_arch`, `buildx_version`, `existing_tag_state=pass`, `existing_revision=pass`, `canary_state=pass`, `existing_tag`, `expected_revision`, `probe_version=1`, and `probe_result=pass`. |

> **Note on Tasks 1–4.** The YAML and Python snippets in these tasks record the
> original build sequence. An independent security review then required the
> finalized design's trust boundary — `repository_dispatch`, terminal ref and
> workflow-identity checks, full action SHA pins, `ubuntu-24.04` with a 30-minute
> timeout, `cmp -s` byte-exact contract checks, NUL-safe captures, and the
> `runner.os` / `runner.arch` evidence — so those snippets no longer match what
> shipped. The Global Constraints and Interfaces above and the workflow, helper,
> and test files themselves are authoritative.

### Task 1: Add the RED structural and trust-contract tests

**Files:**

- Modify: `Kavya/tests/test_smartpbx_deployment.py` immediately after `build_kavya_image_job()` and `workflow_step()`.
- Test: `Kavya/tests/test_smartpbx_deployment.py`.

**Consumes:** Existing `PROJECT_ROOT`, `yaml`, `read_build_kavya_image_workflow()`, `build_kavya_image_job()`, `workflow_step()`, and `workflow_run_strings()` helpers.

**Produces:** `read_kavya_image_probe_workflow()`, `kavya_image_probe_job()`, `probe_workflow_step()`, `test_kavya_image_probe_workflow_has_read_only_dispatch_trust_contract()`, and `test_kavya_image_probe_validation_precedes_all_tooling_and_has_no_source_checkout()`.

- [ ] **Step 1: Add the parser helpers and failing structural tests.**

```python
KAVYA_IMAGE_PROBE_WORKFLOW = PROJECT_ROOT.parent / ".github/workflows/probe-kavya-image.yml"


def read_kavya_image_probe_workflow():
    assert KAVYA_IMAGE_PROBE_WORKFLOW.is_file(), "missing read-only Kavya image probe workflow"
    text = KAVYA_IMAGE_PROBE_WORKFLOW.read_text(encoding="utf-8")
    document = yaml.load(text, Loader=yaml.BaseLoader)
    assert isinstance(document, dict)
    return document, text


def kavya_image_probe_job():
    document, text = read_kavya_image_probe_workflow()
    jobs = document.get("jobs", {})
    assert set(jobs) == {"probe"}
    job = jobs["probe"]
    return document, job, job.get("steps", []), text


def probe_workflow_step(steps, name):
    step = next((step for step in steps if step.get("name") == name), None)
    assert step is not None, f"missing probe workflow step: {name}"
    return step


def test_kavya_image_probe_workflow_has_read_only_dispatch_trust_contract():
    document, job, steps, _text = kavya_image_probe_job()
    _publisher_document, _publisher_job, publisher_steps, _publisher_text = build_kavya_image_job()

    assert document["name"] == "Probe Kavya image (read-only)"
    assert set(document["on"]) == {"workflow_dispatch"}
    assert document["on"]["workflow_dispatch"]["inputs"] == {
        "existing_tag": {"description": "Existing immutable Kavya image tag", "required": "true", "type": "string"},
        "expected_revision": {"description": "Expected lowercase OCI revision", "required": "true", "type": "string"},
    }
    assert document["concurrency"] == {"group": "kavya-image-read-only-probe", "cancel-in-progress": "false"}
    assert job["runs-on"] == "ubuntu-latest"
    assert job["permissions"] == {"contents": "read", "packages": "read"}
    assert "environment" not in job

    checkout = probe_workflow_step(steps, "Checkout trusted probe tooling")
    assert checkout["uses"] == workflow_step(publisher_steps, "Checkout trusted publisher tooling")["uses"]
    assert checkout["with"] == {
        "ref": "${{ github.workflow_sha }}",
        "path": ".probe-tools",
        "persist-credentials": "false",
    }
    assert probe_workflow_step(steps, "Set up Buildx")["uses"] == workflow_step(publisher_steps, "Set up Buildx")["uses"]
    assert probe_workflow_step(steps, "Log in to GHCR")["uses"] == workflow_step(publisher_steps, "Log in to GHCR")["uses"]


def test_kavya_image_probe_validation_precedes_all_tooling_and_has_no_source_checkout():
    document, _job, steps, text = kavya_image_probe_job()
    validation = probe_workflow_step(steps, "Validate probe inputs")
    checkout = probe_workflow_step(steps, "Checkout trusted probe tooling")
    buildx = probe_workflow_step(steps, "Set up Buildx")
    login = probe_workflow_step(steps, "Log in to GHCR")

    assert steps.index(validation) == 0
    assert steps.index(validation) < steps.index(checkout) < steps.index(buildx) < steps.index(login)
    assert validation["env"] == {
        "EXISTING_TAG": "${{ inputs.existing_tag }}",
        "EXPECTED_REVISION": "${{ inputs.expected_revision }}",
    }
    assert '[[ "$EXISTING_TAG" =~ ^[0-9a-f]{7}$ ]]' in validation["run"]
    assert '[[ "$EXPECTED_REVISION" =~ ^[0-9a-f]{40}$ ]]' in validation["run"]
    assert '[[ "$EXISTING_TAG" == "${EXPECTED_REVISION:0:7}" ]]' in validation["run"]
    assert "${{ inputs." not in "\n".join(workflow_run_strings(document))
    assert all(step.get("with", {}).get("path") != "source" for step in steps if isinstance(step, dict))
    assert "Checkout reviewed source" not in text
```

- [ ] **Step 2: Run the focused RED tests.**

Run:

```bash
/home/dev/full-voice-agent/.venv/bin/python -m pytest -q Kavya/tests/test_smartpbx_deployment.py -k 'kavya_image_probe_workflow_has_read_only_dispatch_trust_contract or kavya_image_probe_validation_precedes_all_tooling'
```

Expected: both selected tests fail with `missing read-only Kavya image probe workflow`. Do not assert a suite-wide collected-test total.

- [ ] **Step 3: Commit the RED contract.**

```bash
git add Kavya/tests/test_smartpbx_deployment.py
git commit -m "test(kavya): specify read-only GHCR probe structure"
```

### Task 2: Make the structural contract GREEN with the read-only skeleton

**Files:**

- Create: `.github/workflows/probe-kavya-image.yml`.
- Test: `Kavya/tests/test_smartpbx_deployment.py`.

**Consumes:** The exact names and parser contract from Task 1, plus declared action references from `.github/workflows/build-kavya-image.yml`.

**Produces:** A dispatchable-after-merge skeleton with validation first, trusted tooling checkout, read-only permissions, static concurrency, and no source checkout.

- [ ] **Step 1: Create the complete structural skeleton.**

```yaml
name: Probe Kavya image (read-only)

on:
  workflow_dispatch:
    inputs:
      existing_tag:
        description: Existing immutable Kavya image tag
        required: true
        type: string
      expected_revision:
        description: Expected lowercase OCI revision
        required: true
        type: string

concurrency:
  group: kavya-image-read-only-probe
  cancel-in-progress: false

jobs:
  probe:
    runs-on: ubuntu-latest
    permissions:
      contents: read
      packages: read
    steps:
      - name: Validate probe inputs
        env:
          EXISTING_TAG: ${{ inputs.existing_tag }}
          EXPECTED_REVISION: ${{ inputs.expected_revision }}
        run: |
          set -Eeuo pipefail
          fail() { printf 'probe_result=fail\n' >> "$GITHUB_STEP_SUMMARY"; exit 1; }
          [[ "$EXISTING_TAG" =~ ^[0-9a-f]{7}$ ]] || fail
          [[ "$EXPECTED_REVISION" =~ ^[0-9a-f]{40}$ ]] || fail
          [[ "$EXISTING_TAG" == "${EXPECTED_REVISION:0:7}" ]] || fail
      - name: Checkout trusted probe tooling
        uses: actions/checkout@v7
        with:
          ref: ${{ github.workflow_sha }}
          path: .probe-tools
          persist-credentials: false
      - name: Set up Buildx
        uses: docker/setup-buildx-action@v4
      - name: Log in to GHCR
        uses: docker/login-action@v4
        with:
          registry: ghcr.io
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}
```

- [ ] **Step 2: Run the focused GREEN tests.**

Run:

```bash
/home/dev/full-voice-agent/.venv/bin/python -m pytest -q Kavya/tests/test_smartpbx_deployment.py -k 'kavya_image_probe_workflow_has_read_only_dispatch_trust_contract or kavya_image_probe_validation_precedes_all_tooling'
```

Expected: every selected structural test passes; do not assert a fixed total.

- [ ] **Step 3: Commit the GREEN skeleton.**

```bash
git add .github/workflows/probe-kavya-image.yml Kavya/tests/test_smartpbx_deployment.py
git commit -m "feat(kavya): add read-only GHCR probe skeleton"
```

### Task 3: Add the RED semantic and security-contract tests

**Files:**

- Modify: `Kavya/tests/test_smartpbx_deployment.py` after the Task 1 tests.
- Test: `Kavya/tests/test_smartpbx_deployment.py`.

**Consumes:** `kavya_image_probe_job()`, `probe_workflow_step()`, `workflow_run_strings()`, and `KAVYA_IMAGE_TAG_PROBE`.

**Produces:** `test_kavya_image_probe_requires_exact_states_provenance_and_internal_canary()` and `test_kavya_image_probe_has_no_write_deploy_or_sensitive_output_surface()`.

- [ ] **Step 1: Add the first failing semantic-contract test.**

```python
def test_kavya_image_probe_requires_exact_states_provenance_and_internal_canary():
    _document, _job, steps, text = kavya_image_probe_job()
    existing = probe_workflow_step(steps, "Probe known existing tag")
    verify = probe_workflow_step(steps, "Verify existing OCI revision")
    canary = probe_workflow_step(steps, "Probe generated absent canary")
    metadata = probe_workflow_step(steps, "Record safe runtime metadata")
    summary = probe_workflow_step(steps, "Write safe probe summary")

    assert KAVYA_IMAGE_TAG_PROBE.is_file()
    assert ".probe-tools/.github/scripts/check-kavya-image-tag.sh" in existing["run"]
    assert "probe_code" in existing["run"]
    assert '[[ "$probe_code" -eq 10 ]]' in existing["run"]
    assert 'expected_marker="image_tag_state=existing"' in existing["run"]
    assert 'probe_lines="$(wc -l < "$probe_stdout")" || fail' in existing["run"]
    assert 'probe_marker="$(cat "$probe_stdout")" || fail' in existing["run"]
    assert '[[ "$probe_lines" -eq 1 ]] || fail' in existing["run"]
    assert '[[ "$probe_marker" == "$expected_marker" ]] || fail' in existing["run"]
    assert '[[ "$probe_code" -eq 0 ]]' in canary["run"]
    assert 'expected_marker="image_tag_state=absent"' in canary["run"]
    assert 'canary="probe-${GITHUB_REPOSITORY_ID}-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}"' in canary["run"]
    assert '[[ "$canary" =~ ^probe-[0-9]+-[0-9]+-[0-9]+$ ]]' in canary["run"]
    assert 'image="ghcr.io/taskforce-ai-dev/kavya"' in existing["run"]
    assert "docker pull \"$image\" >\"$pull_stdout\" 2>\"$pull_stderr\"" in verify["run"]
    assert "docker image inspect \"$image\" --format" in verify["run"]
    assert '[[ "$revision" == "$EXPECTED_REVISION" ]]' in verify["run"]
    assert "workflow_commit=$GITHUB_WORKFLOW_SHA" in metadata["run"]
    assert "buildx_version=" in metadata["run"]
    for marker in ("probe_version=1", "existing_tag_state=pass", "existing_revision=pass", "canary_state=pass", "probe_result=pass"):
        assert marker in summary["run"]
    assert "${{ inputs." not in "\n".join(workflow_run_strings(document))
    assert "source/.github/scripts" not in text


```

- [ ] **Step 2: Add the second failing negative-surface test.**

```python
def test_kavya_image_probe_has_no_write_deploy_or_sensitive_output_surface():
    _document, job, _steps, text = kavya_image_probe_job()
    runs = "\n".join(workflow_run_strings(kavya_image_probe_job()[0]))

    assert job["permissions"] == {"contents": "read", "packages": "read"}
    assert re.findall(r"secrets\.([A-Za-z0-9_]+)", text) == ["GITHUB_TOKEN"]
    for forbidden in (
        "packages: write",
        "docker/build-push-action@",
        "docker push",
        "docker build ",
        "docker tag ",
        "docker manifest",
        "docker compose",
        "ssh ",
        "rsync ",
        "nginx",
        "systemctl",
        "env |",
        "printenv",
        "set -x",
        "source/",
    ):
        assert forbidden not in text.lower()
    assert "probe_result=fail" in runs
    for forbidden in (
        "echo \"$GITHUB_TOKEN\"",
        "cat \"$probe_stderr\"",
        "cat \"$pull_stderr\"",
        "cat \"$inspect_stderr\"",
        "--format '{{json .}}'",
    ):
        assert forbidden not in runs
```

- [ ] **Step 3: Add executable named-step harness tests; these test workflow Bash semantics only.**

```python
def run_probe_workflow_step(tmp_path, name, **values):
    document, _job, steps, _text = kavya_image_probe_job()
    script = probe_workflow_step(steps, name)["run"]
    scripts = tmp_path / ".probe-tools" / ".github" / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    log = tmp_path / "tools.log"
    summary = tmp_path / "summary"
    (scripts / "check-kavya-image-tag.sh").write_text(textwrap.dedent("""\
        #!/usr/bin/env bash
        set -Eeuo pipefail
        printf 'probe %s\\n' "$1" >> "$FAKE_LOG"
        if [[ "$1" == *":probe-"* ]]; then
          printf '%s' "$CANARY_OUT"; printf '%s' "$CANARY_ERR" >&2; exit "$CANARY_CODE"
        fi
        printf '%s' "$EXISTING_OUT"; printf '%s' "$EXISTING_ERR" >&2; exit "$EXISTING_CODE"
    """), encoding="utf-8")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(exist_ok=True)
    docker = fake_bin / "docker"
    docker.write_text(textwrap.dedent("""\
        #!/usr/bin/env bash
        set -Eeuo pipefail
        printf 'docker %s\\n' "$*" >> "$FAKE_LOG"
        if [[ "$1 $2" == "buildx version" ]]; then printf 'github.com/docker/buildx %s\\n' "$BUILDX_VERSION"; exit "$BUILDX_CODE"; fi
        if [[ "$1" == pull ]]; then printf '%s' "$PULL_OUT"; printf '%s' "$PULL_ERR" >&2; exit "$PULL_CODE"; fi
        if [[ "$1 $2" == "image inspect" ]]; then printf '%s' "$INSPECT_OUT"; printf '%s' "$INSPECT_ERR" >&2; exit "$INSPECT_CODE"; fi
        exit 97
    """), encoding="utf-8")
    docker.chmod(0o755)
    env = os.environ | {
        "PATH": f"{fake_bin}:{os.environ['PATH']}", "FAKE_LOG": str(log), "GITHUB_STEP_SUMMARY": str(summary),
        "EXISTING_TAG": "37bfaf0", "EXPECTED_REVISION": "37bfaf02f04ce7614b9674b1c867b78ab3c7d414",
        "GITHUB_WORKFLOW_SHA": "a" * 40, "GITHUB_REPOSITORY_ID": "123", "GITHUB_RUN_ID": "456", "GITHUB_RUN_ATTEMPT": "1",
        "ImageOS": "ubuntu24", "ImageVersion": "20240825.1", "BUILDX_VERSION": "v0.16.2", "BUILDX_CODE": "0",
        "EXISTING_OUT": "image_tag_state=existing\n", "EXISTING_ERR": "", "EXISTING_CODE": "10",
        "CANARY_OUT": "image_tag_state=absent\n", "CANARY_ERR": "", "CANARY_CODE": "0",
        "PULL_OUT": "", "PULL_ERR": "", "PULL_CODE": "0",
        "INSPECT_OUT": "37bfaf02f04ce7614b9674b1c867b78ab3c7d414\n", "INSPECT_ERR": "", "INSPECT_CODE": "0",
    } | {key: str(value) for key, value in values.items()}
    result = subprocess.run(["bash", "-c", script], cwd=tmp_path, env=env, text=True, capture_output=True, check=False)
    return result, summary.read_text(encoding="utf-8") if summary.exists() else "", log.read_text(encoding="utf-8") if log.exists() else ""


@pytest.mark.parametrize(("tag", "revision", "code"), [
    ("37bfaf0", "37bfaf02f04ce7614b9674b1c867b78ab3c7d414", 0), ("", "37bfaf02f04ce7614b9674b1c867b78ab3c7d414", 1),
    ("37BFAF0", "37bfaf02f04ce7614b9674b1c867b78ab3c7d414", 1), ("37bfaf00", "37bfaf02f04ce7614b9674b1c867b78ab3c7d414", 1),
    ("37bfaf0", "37BFAF02f04ce7614b9674b1c867b78ab3c7d414", 1), ("37bfaf0", "37bfaf02f04ce7614b9674b1c867b78ab3c7d4140", 1),
    ("37bfaf0", "47bfaf02f04ce7614b9674b1c867b78ab3c7d414", 1), ("37bfaf0", "", 1),
])
def test_kavya_image_probe_validation_binds_identity_before_tools(tmp_path, tag, revision, code):
    result, _summary, log = run_probe_workflow_step(tmp_path, "Validate probe inputs", EXISTING_TAG=tag, EXPECTED_REVISION=revision)
    assert result.returncode == code
    assert log == ""


@pytest.mark.parametrize(("step", "overrides", "code"), [
    ("Probe known existing tag", {"EXISTING_CODE": 10, "EXISTING_OUT": "image_tag_state=existing\n"}, 0),
    ("Probe known existing tag", {"EXISTING_CODE": 0, "EXISTING_OUT": "image_tag_state=existing\n"}, 1),
    ("Probe known existing tag", {"EXISTING_CODE": 1, "EXISTING_OUT": "image_tag_state=existing\n"}, 1),
    ("Probe known existing tag", {"EXISTING_CODE": 99, "EXISTING_OUT": "image_tag_state=existing\n"}, 1),
    ("Probe known existing tag", {"EXISTING_CODE": 10, "EXISTING_OUT": "image_tag_state=existing\nextra\n"}, 1),
    ("Probe known existing tag", {"EXISTING_CODE": 10, "EXISTING_OUT": "wrong\n"}, 1), ("Probe known existing tag", {"EXISTING_CODE": 10, "EXISTING_OUT": ""}, 1),
    ("Probe generated absent canary", {"CANARY_CODE": 0, "CANARY_OUT": "image_tag_state=absent\n"}, 0),
    ("Probe generated absent canary", {"CANARY_CODE": 1, "CANARY_OUT": "image_tag_state=absent\n"}, 1), ("Probe generated absent canary", {"CANARY_CODE": 99, "CANARY_OUT": "image_tag_state=absent\n"}, 1),
    ("Probe generated absent canary", {"CANARY_CODE": 10, "CANARY_OUT": "image_tag_state=absent\n"}, 1),
    ("Probe generated absent canary", {"CANARY_CODE": 0, "CANARY_OUT": "image_tag_state=existing\n"}, 1), ("Probe generated absent canary", {"CANARY_CODE": 0, "CANARY_OUT": "wrong\n"}, 1), ("Probe generated absent canary", {"CANARY_CODE": 0, "CANARY_OUT": ""}, 1),
])
def test_kavya_image_probe_accepts_only_exact_probe_states(tmp_path, step, overrides, code):
    result, summary, log = run_probe_workflow_step(tmp_path, step, EXISTING_ERR="SENTINEL_EXISTING", CANARY_ERR="SENTINEL_CANARY", **overrides)
    assert result.returncode == code
    assert "SENTINEL" not in result.stdout + result.stderr + summary
    if step == "Probe generated absent canary":
        assert log == "probe ghcr.io/taskforce-ai-dev/kavya:probe-123-456-1\n"


@pytest.mark.parametrize(("pull_code", "inspect_code", "revision", "code"), [
    (0, 0, "37bfaf02f04ce7614b9674b1c867b78ab3c7d414\n", 0), (1, 0, "37bfaf02f04ce7614b9674b1c867b78ab3c7d414\n", 1),
    (0, 1, "37bfaf02f04ce7614b9674b1c867b78ab3c7d414\n", 1), (0, 0, "47bfaf02f04ce7614b9674b1c867b78ab3c7d414\n", 1),
    (0, 0, "37bfaf02f04ce7614b9674b1c867b78ab3c7d414\nextra\n", 1),
])
def test_kavya_image_probe_provenance_and_metadata_suppress_sentinels(tmp_path, pull_code, inspect_code, revision, code):
    result, summary, _log = run_probe_workflow_step(tmp_path, "Verify existing OCI revision", PULL_CODE=pull_code, INSPECT_CODE=inspect_code, INSPECT_OUT=revision, PULL_ERR="SENTINEL_PULL", INSPECT_ERR="SENTINEL_INSPECT")
    assert result.returncode == code
    assert "SENTINEL" not in result.stdout + result.stderr + summary
    metadata, metadata_summary, _log = run_probe_workflow_step(tmp_path / "metadata", "Record safe runtime metadata", ImageOS="ubuntu 24", BUILDX_VERSION="bad value")
    assert metadata.returncode == 1
    assert "PATH=" not in metadata.stdout + metadata.stderr + metadata_summary


@pytest.mark.parametrize(("repository_id", "run_id", "attempt"), [("", "456", "1"), ("abc", "456", "1"), ("123" * 50, "456", "1"), ("123", "", "1"), ("123", "456", "x")])
def test_kavya_image_probe_canary_identifier_table_fails_before_fake_tools(tmp_path, repository_id, run_id, attempt):
    result, summary, log = run_probe_workflow_step(tmp_path, "Probe generated absent canary", GITHUB_REPOSITORY_ID=repository_id, GITHUB_RUN_ID=run_id, GITHUB_RUN_ATTEMPT=attempt)
    assert result.returncode != 0
    assert log == ""
    assert "probe_result=fail" in summary
    assert repository_id not in result.stdout + result.stderr + summary


@pytest.mark.parametrize(("workflow_sha", "image_os", "image_version", "buildx_code", "buildx_version", "code"), [
    ("a" * 40, "ubuntu24", "20240825.1", 0, "v0.16.2", 0), ("", "ubuntu24", "20240825.1", 0, "v0.16.2", 1),
    ("A" * 40, "ubuntu24", "20240825.1", 0, "v0.16.2", 1), ("a" * 40, "", "20240825.1", 0, "v0.16.2", 1),
    ("a" * 40, "ubuntu 24", "20240825.1", 0, "v0.16.2", 1), ("a" * 40, "ubuntu24", "", 0, "v0.16.2", 1),
    ("a" * 40, "ubuntu24", "bad value", 0, "v0.16.2", 1), ("a" * 40, "ubuntu24", "20240825.1", 1, "v0.16.2", 1),
    ("a" * 40, "ubuntu24", "20240825.1", 0, "bad value", 1),
])
def test_kavya_image_probe_metadata_table_is_safe(tmp_path, workflow_sha, image_os, image_version, buildx_code, buildx_version, code):
    result, summary, _log = run_probe_workflow_step(tmp_path, "Record safe runtime metadata", GITHUB_WORKFLOW_SHA=workflow_sha, ImageOS=image_os, ImageVersion=image_version, BUILDX_CODE=buildx_code, BUILDX_VERSION=buildx_version)
    assert result.returncode == code
    assert "PATH=" not in result.stdout + result.stderr + summary
    assert "SENTINEL" not in result.stdout + result.stderr + summary


def test_kavya_image_probe_run_scripts_parse_and_summary_is_allowlisted(tmp_path):
    document, _job, _steps, _text = kavya_image_probe_job()
    for script in workflow_run_strings(document):
        parsed = subprocess.run(["bash", "-n"], input=script, text=True, capture_output=True, check=False)
        assert parsed.returncode == 0, parsed.stderr
    allowed = ("workflow_commit=", "checkout_action=", "buildx_action=", "login_action=", "runner_image=", "runner_image_version=", "buildx_version=", "existing_tag_state=pass", "existing_revision=pass", "canary_state=pass", "probe_version=1", "probe_result=pass")
    for name in ("Record safe runtime metadata", "Probe known existing tag", "Verify existing OCI revision", "Probe generated absent canary", "Write safe probe summary"):
        result, summary, _log = run_probe_workflow_step(tmp_path, name)
        assert result.returncode == 0
    assert all(line.startswith(allowed) for line in summary.splitlines())
    assert "SENTINEL" not in summary
```

The fake `.probe-tools` script and fake Docker binary live under `tmp_path`.
Their `summary` and `tools.log` files intentionally accumulate across repeated
calls with the same `tmp_path`; the allowlist test verifies the cumulative
record and the harness never reads host temporary artifacts.
They prove workflow-shell behavior and suppression only; existing dynamic tests
continue to cover the real tag-probe script, and this harness never contacts a registry.

- [ ] **Step 3: Run the focused RED tests.**

Run:

```bash
/home/dev/full-voice-agent/.venv/bin/python -m pytest -q Kavya/tests/test_smartpbx_deployment.py -k 'kavya_image_probe'
```

Expected: tests fail only because the skeleton lacks named workflow steps; there are no collection, parser, or harness-scaffolding failures. Do not assert a fixed total.

- [ ] **Step 4: Commit the RED semantic contract.**

```bash
git add Kavya/tests/test_smartpbx_deployment.py
git commit -m "test(kavya): specify read-only probe semantics"
```

### Task 4: Make the semantic and security contract GREEN

**Files:**

- Modify: `.github/workflows/probe-kavya-image.yml`.
- Test: `Kavya/tests/test_smartpbx_deployment.py`.

**Consumes:** The Task 3 step names and exact assertions, plus `.probe-tools/.github/scripts/check-kavya-image-tag.sh`.

**Produces:** A complete non-writing probe workflow that validates inputs first, records safe version evidence, enforces exact probe lines, verifies OCI provenance, and fails generically on every uncertain state.

- [ ] **Step 1: Replace the skeleton with this complete workflow.**

```yaml
name: Probe Kavya image (read-only)

on:
  workflow_dispatch:
    inputs:
      existing_tag:
        description: Existing immutable Kavya image tag
        required: true
        type: string
      expected_revision:
        description: Expected lowercase OCI revision
        required: true
        type: string

concurrency:
  group: kavya-image-read-only-probe
  cancel-in-progress: false

jobs:
  probe:
    runs-on: ubuntu-latest
    permissions:
      contents: read
      packages: read
    steps:
      - name: Validate probe inputs
        env:
          EXISTING_TAG: ${{ inputs.existing_tag }}
          EXPECTED_REVISION: ${{ inputs.expected_revision }}
        run: |
          set -Eeuo pipefail
          fail() { printf 'probe_result=fail\n' >> "$GITHUB_STEP_SUMMARY"; exit 1; }
          [[ "$EXISTING_TAG" =~ ^[0-9a-f]{7}$ ]] || fail
          [[ "$EXPECTED_REVISION" =~ ^[0-9a-f]{40}$ ]] || fail
          [[ "$EXISTING_TAG" == "${EXPECTED_REVISION:0:7}" ]] || fail
      - name: Checkout trusted probe tooling
        uses: actions/checkout@v7
        with:
          ref: ${{ github.workflow_sha }}
          path: .probe-tools
          persist-credentials: false
      - name: Set up Buildx
        uses: docker/setup-buildx-action@v4
      - name: Log in to GHCR
        uses: docker/login-action@v4
        with:
          registry: ghcr.io
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}
      - name: Record safe runtime metadata
        run: |
          set -Eeuo pipefail
          fail() { printf 'probe_result=fail\n' >> "$GITHUB_STEP_SUMMARY"; exit 1; }
          workflow_sha="${GITHUB_WORKFLOW_SHA:-}"
          runner_image="${ImageOS:-}"
          runner_version="${ImageVersion:-}"
          [[ "$workflow_sha" =~ ^[0-9a-f]{40}$ ]] || fail
          [[ "$runner_image" =~ ^[A-Za-z0-9._-]+$ ]] || fail
          [[ "$runner_version" =~ ^[A-Za-z0-9._-]+$ ]] || fail
          buildx_dir="$(mktemp -d)" || fail
          cleanup() { rm -rf -- "$buildx_dir" || true; }
          trap cleanup EXIT
          docker buildx version >"$buildx_dir/stdout" 2>"$buildx_dir/stderr" || fail
          buildx_version="$(awk '{print $2}' "$buildx_dir/stdout")" || fail
          [[ "$runner_image" =~ ^[A-Za-z0-9._-]+$ ]] || fail
          [[ "$runner_version" =~ ^[A-Za-z0-9._-]+$ ]] || fail
          [[ "$buildx_version" =~ ^v[0-9][A-Za-z0-9._-]*$ ]] || fail
          {
            printf 'workflow_commit=%s\n' "$workflow_sha"
            printf '%s\n' 'checkout_action=actions/checkout@v7'
            printf '%s\n' 'buildx_action=docker/setup-buildx-action@v4'
            printf '%s\n' 'login_action=docker/login-action@v4'
            printf 'runner_image=%s\n' "$runner_image"
            printf 'runner_image_version=%s\n' "$runner_version"
            printf 'buildx_version=%s\n' "$buildx_version"
          } >> "$GITHUB_STEP_SUMMARY" || fail
      - name: Probe known existing tag
        env:
          EXISTING_TAG: ${{ inputs.existing_tag }}
        run: |
          set -Eeuo pipefail
          fail() { printf 'probe_result=fail\n' >> "$GITHUB_STEP_SUMMARY"; exit 1; }
          image="ghcr.io/taskforce-ai-dev/kavya:${EXISTING_TAG}"
          probe_dir="$(mktemp -d)" || fail
          cleanup() { rm -rf -- "$probe_dir" || true; }
          trap cleanup EXIT
          probe_stdout="$probe_dir/stdout"
          probe_stderr="$probe_dir/stderr"
          expected_marker="image_tag_state=existing"
          if bash .probe-tools/.github/scripts/check-kavya-image-tag.sh "$image" >"$probe_stdout" 2>"$probe_stderr"; then probe_code=0; else probe_code=$?; fi
          [[ "$probe_code" -eq 10 ]] || fail
          probe_lines="$(wc -l < "$probe_stdout")" || fail
          probe_marker="$(cat "$probe_stdout")" || fail
          [[ "$probe_lines" -eq 1 ]] || fail
          [[ "$probe_marker" == "$expected_marker" ]] || fail
          printf 'existing_tag_state=pass\n' >> "$GITHUB_STEP_SUMMARY" || fail
      - name: Verify existing OCI revision
        env:
          EXISTING_TAG: ${{ inputs.existing_tag }}
          EXPECTED_REVISION: ${{ inputs.expected_revision }}
        run: |
          set -Eeuo pipefail
          fail() { printf 'probe_result=fail\n' >> "$GITHUB_STEP_SUMMARY"; exit 1; }
          image="ghcr.io/taskforce-ai-dev/kavya:${EXISTING_TAG}"
          probe_dir="$(mktemp -d)" || fail
          cleanup() { rm -rf -- "$probe_dir" || true; }
          trap cleanup EXIT
          pull_stdout="$probe_dir/pull.stdout"
          pull_stderr="$probe_dir/pull.stderr"
          inspect_stdout="$probe_dir/inspect.stdout"
          inspect_stderr="$probe_dir/inspect.stderr"
          docker pull "$image" >"$pull_stdout" 2>"$pull_stderr" || fail
          docker image inspect "$image" --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}' >"$inspect_stdout" 2>"$inspect_stderr" || fail
          inspect_lines="$(wc -l < "$inspect_stdout")" || fail
          revision="$(cat "$inspect_stdout")" || fail
          [[ "$inspect_lines" -eq 1 ]] || fail
          [[ "$revision" == "$EXPECTED_REVISION" ]] || fail
          printf 'existing_revision=pass\n' >> "$GITHUB_STEP_SUMMARY" || fail
      - name: Probe generated absent canary
        run: |
          set -Eeuo pipefail
          fail() { printf 'probe_result=fail\n' >> "$GITHUB_STEP_SUMMARY"; exit 1; }
          image="ghcr.io/taskforce-ai-dev/kavya"
          repository_id="${GITHUB_REPOSITORY_ID:-}"
          run_id="${GITHUB_RUN_ID:-}"
          attempt="${GITHUB_RUN_ATTEMPT:-}"
          [[ "$repository_id" =~ ^[0-9]+$ ]] || fail
          [[ "$run_id" =~ ^[0-9]+$ ]] || fail
          [[ "$attempt" =~ ^[0-9]+$ ]] || fail
          canary="probe-${repository_id}-${run_id}-${attempt}"
          [[ "$canary" =~ ^probe-[0-9]+-[0-9]+-[0-9]+$ ]] || fail
          [[ ${#canary} -le 128 ]] || fail
          probe_dir="$(mktemp -d)" || fail
          cleanup() { rm -rf -- "$probe_dir" || true; }
          trap cleanup EXIT
          probe_stdout="$probe_dir/stdout"
          probe_stderr="$probe_dir/stderr"
          expected_marker="image_tag_state=absent"
          if bash .probe-tools/.github/scripts/check-kavya-image-tag.sh "$image:$canary" >"$probe_stdout" 2>"$probe_stderr"; then probe_code=0; else probe_code=$?; fi
          [[ "$probe_code" -eq 0 ]] || fail
          probe_lines="$(wc -l < "$probe_stdout")" || fail
          probe_marker="$(cat "$probe_stdout")" || fail
          [[ "$probe_lines" -eq 1 ]] || fail
          [[ "$probe_marker" == "$expected_marker" ]] || fail
          printf 'canary_state=pass\n' >> "$GITHUB_STEP_SUMMARY" || fail
      - name: Write safe probe summary
        run: |
          set -Eeuo pipefail
          fail() { printf 'probe_result=fail\n' >> "$GITHUB_STEP_SUMMARY"; exit 1; }
          {
            printf '%s\n' 'probe_version=1'
            printf '%s\n' 'probe_result=pass'
          } >> "$GITHUB_STEP_SUMMARY" || fail
```

- [ ] **Step 2: Run focused GREEN semantic and structural tests.**

Run:

```bash
/home/dev/full-voice-agent/.venv/bin/python -m pytest -q Kavya/tests/test_smartpbx_deployment.py -k 'kavya_image_probe'
```

Expected: every selected probe-contract test passes; do not assert a fixed total.

- [ ] **Step 3: Validate YAML with the same loader policy as the tests.**

Run:

```bash
/home/dev/full-voice-agent/.venv/bin/python -c 'from pathlib import Path; import yaml; document=yaml.load(Path(".github/workflows/probe-kavya-image.yml").read_text(encoding="utf-8"), Loader=yaml.BaseLoader); assert set(document["on"]) == {"workflow_dispatch"}; assert set(document["jobs"]) == {"probe"}'
```

Expected: exit status `0` with no output.

- [ ] **Step 4: Commit the complete GREEN workflow.**

```bash
git add .github/workflows/probe-kavya-image.yml Kavya/tests/test_smartpbx_deployment.py
git commit -m "feat(kavya): add read-only GHCR probe gate"
```

### Task 5: Verify, review, release, and collect acceptance evidence

**Files:**

- Verify: `.github/workflows/probe-kavya-image.yml`.
- Verify: `Kavya/tests/test_smartpbx_deployment.py`.
- Read only: `.github/scripts/check-kavya-image-tag.sh`.

**Consumes:** The complete workflow from Task 4 and the committed exact head.

**Produces:** Reviewable local evidence, a reviewed PR, and a main-branch dispatch gate before any publisher run.

- [ ] **Step 1: Run focused static workflow tests.**

Run:

```bash
/home/dev/full-voice-agent/.venv/bin/python -m pytest -q Kavya/tests/test_smartpbx_deployment.py -k 'kavya_image_probe'
```

Expected: every selected probe test passes; do not record or require a fixed count.

- [ ] **Step 2: Run the deployment-plus-MCP regression set.**

Run:

```bash
/home/dev/full-voice-agent/.venv/bin/python -m pytest -q Kavya/tests/test_smartpbx_deployment.py Kavya/tests/test_smartpbx_mcp.py
```

Expected: exit status `0`; report the observed count without treating a historical count as a requirement.

- [ ] **Step 3: Run the full Kavya suite and static syntax checks.**

Run:

```bash
/home/dev/full-voice-agent/.venv/bin/python -m pytest -q Kavya
bash -n .github/scripts/check-kavya-image-tag.sh
/home/dev/full-voice-agent/.venv/bin/python -c 'from pathlib import Path; import yaml; yaml.load(Path(".github/workflows/probe-kavya-image.yml").read_text(encoding="utf-8"), Loader=yaml.BaseLoader)'
git diff --check
git status --short
```

Expected: each command exits `0`; the final status command prints nothing.

- [ ] **Step 4: Commit only if the worktree is clean and all local checks passed.**

```bash
git status --short
git log -1 --oneline
```

Expected: clean status and the Task 4 commit at `HEAD`.

- [ ] **Step 5: Request independent Sol review before publication.**

Provide Sol the exact `HEAD` SHA, the workflow path, the focused-test output, the regression outputs, and the negative-surface assertions. Require confirmation that no source checkout, write permission, registry mutation, deploy, SSH, host operation, secret output, or action-version immutability claim was introduced.

- [ ] **Step 6: Publish and open review only after the independent approval.**

```bash
git push origin Rakesh
gh pr create --base main --head Rakesh --title 'feat(kavya): add read-only GHCR probe gate' --body 'Adds only the read-only GHCR provenance and absent-canary gate. The immutable publisher remains undispatched until the probe passes on main.'
```

Expected: a normal fast-forward push and a PR URL. Do not force-push, merge locally, dispatch any workflow, build or push an image, or contact a host in this step.

- [ ] **Step 7: Require exact-head CI and gitleaks, then merge through the approved PR process.**

Check that CI and gitleaks report success for the PR's current head SHA, obtain independent Sol approval, and merge only through the approved protected-branch PR control. If the PR head changes, repeat exact-head checks and review before merge.

- [ ] **Step 8: Dispatch the probe on `main` and evaluate only safe evidence.**

Run:

**Precondition — branch protection must be enabled on `main`.** Both workflows check `github.ref_protected == true` as a terminal condition. If `main` is not a protected branch, that check fails and **both the probe and the publisher fail closed permanently**, with no message distinguishing "unprotected branch" from an attack. Confirm protection is on before dispatching:

```bash
gh api repos/taskforce-ai-dev/full-voice-agent/branches/main --jq '.protected'
```

Expected: `true`. If it prints `false`, enable branch protection first — do not work around the check.

**The allowlist gap has been hit and partly closed.** The first dispatch failed exit `2` at the **existing-tag** step (run 31267746235). The wording was captured deliberately and `"error: $TAG: not found"` is now allowlisted — see the design document's provenance note. If a future run reports `probe_unrecognized` again, the remedy is unchanged: capture the real bytes, add that exact whole-capture string in a reviewed PR citing the run URL, never loosen the matching.

**The first acceptance runs in bootstrap mode.** Nothing has ever been published, so a strict probe cannot pass. Use the current `main` revision, not the stale `37bfaf0` pair. Send the documented repository dispatch — it carries no ref or SHA selector, and GitHub selects the default-branch copy of the workflow:

```bash
set -Eeuo pipefail
fail() { printf '%s\n' 'dispatcher_validation=fail' >&2; exit 1; }

repo='taskforce-ai-dev/full-voice-agent'
expected_revision=$(git rev-parse origin/main)
existing_tag=${expected_revision:0:7}

[[ "$expected_revision" =~ ^[0-9a-f]{40}$ ]] || fail
[[ "$existing_tag" =~ ^[0-9a-f]{7}$ ]] || fail

# bootstrap=true is accepted only as this exact string, and only while no image
# exists. Drop the flag entirely for every probe after the first publish.
gh api --method POST "repos/$repo/dispatches" \
  -f event_type=kavya_image_read_only_probe \
  -f "client_payload[existing_tag]=$existing_tag" \
  -f "client_payload[expected_revision]=$expected_revision" \
  -f 'client_payload[bootstrap]=true'
```

Run the design document's full fail-fast dispatcher (it validates both values before `gh api`) rather than the bare call when dispatching by hand.

Expected for a **bootstrap** run: `probe_mode=bootstrap`, `existing_tag_state=absent_bootstrap`, `existing_revision=skipped_no_image`, and still `canary_state=pass`, `probe_version=1`, `probe_result=pass`. Expected for every **strict** run afterwards: `probe_mode=strict`, `existing_tag_state=pass`, `existing_revision=pass`, `canary_state=pass`, `probe_version=1`, `probe_result=pass`. Either way the run records together with the workflow commit SHA, the three pinned action identifiers, `runner_label=ubuntu-24.04` with the documented `runner_os` / `runner_arch`, and the actual Buildx version. Inspect GitHub's setup-action logs and confirm the resolved action commit SHAs match the pins; the summary records the pinned identifiers but does not itself cryptographically prove resolution. A changed action resolution or an unexpected runner or Buildx version fails acceptance.

- [ ] **Step 9: Gate publisher authority on the accepted probe.**

Do not dispatch the immutable publisher unless Step 8 passed and its safe evidence was accepted. Publish the **current `main`** revision, not the stale `69ec0b3`. A bootstrap-green probe satisfies the gate honestly, but read the summary: `probe_mode=bootstrap` with `existing_revision=skipped_no_image` means no image was verified because none existed, which is exactly the expected state for a first publish and must never be accepted as evidence for a later one. The publisher now enforces this itself and will fail closed without a fresh same-commit probe, but the human acceptance of the probe's evidence in Step 8 is still required — the gate checks that a probe *succeeded*, not that anyone read it. Note the 24-hour window and the `github.sha` binding: if a commit lands on `main` after the probe, or more than a day passes, re-run the probe before dispatching. A probe failure, malformed marker, existing canary, missing provenance label, metadata mismatch, or uncertain registry result stops the release; correct the reviewed workflow or tests in a new PR and repeat Tasks 1 through 8.

- [ ] **Step 10: Re-probe strict against the newly published tag.**

Once the publisher has pushed, dispatch the probe again **without** `bootstrap`, using the published revision. This is the run that actually proves the image's `org.opencontainers.image.revision` label, and it is the evidence the gate should be carrying from then on. Expect `probe_mode=strict`, `existing_tag_state=pass`, `existing_revision=pass`. A failure here means the published image does not match what was reviewed — stop and investigate rather than re-running bootstrap.

## Plan Self-Check

- Spec coverage: Tasks 1–4 implement the one-job read-only workflow, exact permissions, trusted tooling checkout, declared action-ref parity, strict bound inputs, canary derivation, exact state handling, provenance pull, safe metadata, Bash syntax checks, and negative surface. Task 5 covers CI, gitleaks, Sol review, protected merge, main dispatch, setup-log action-SHA inspection, and the publisher gate.
- Naming and types: Every test function, helper, workflow step, input, marker, and environment variable is defined above and retains the same spelling in later tasks.
- Scope: The only implementation files are the new workflow and its existing-module static tests. The plan contains no image publication, deploy, host, dashboard, environment, Nginx, transfer, or MCP activation work.
