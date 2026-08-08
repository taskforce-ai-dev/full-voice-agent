# Kavya GHCR Read-Only Probe Gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a protected-default-branch, manually dispatched, read-only GHCR provenance and absent-canary gate that must pass before the immutable Kavya publisher runs for reviewed SHA `69ec0b3`.

**Architecture:** A single GitHub-hosted probe job validates two tightly typed inputs before any registry-affecting setup, then checks out only workflow-revision tooling into `.probe-tools`. It uses that trusted tag-probe script to prove a known immutable tag exists and an internally derived canary is absent, while a separate read-only pull verifies the known image's OCI revision label. The job has no source checkout, build context, registry write operation, or deployment capability.

**Tech Stack:** GitHub Actions YAML, Bash with `set -Eeuo pipefail`, Docker Buildx setup and GHCR login actions, Docker CLI read operations, Python `pytest`, and PyYAML `BaseLoader`.

## Global Constraints

- Create `.github/workflows/probe-kavya-image.yml`; it is usable only after merge to the protected default branch.
- Use one `ubuntu-latest` GitHub-hosted job with exactly `contents: read` and `packages: read`; do not add environments or other permissions.
- Use declared references `actions/checkout@v7`, `docker/setup-buildx-action@v4`, and `docker/login-action@v4`, matching `.github/workflows/build-kavya-image.yml` exactly.
- Treat those declared action tags and `ubuntu-latest` as mutable channel references, not immutable SHA pins. Record workflow commit SHA, declared action references, runner image/version metadata, and actual Buildx version for human acceptance comparison.
- The first trusted shell step validates `existing_tag` as seven lowercase hex characters and `expected_revision` as forty lowercase hex characters before checkout, setup, login, probing, or image access.
- Use inputs only through step `env`; never interpolate `${{ inputs.* }}` in Bash. Parse workflow YAML in tests with `yaml.BaseLoader` so YAML 1.1 does not coerce `on` to a boolean.
- Check out `github.workflow_sha` into `.probe-tools` with `persist-credentials: false`; never check out or execute an input/source ref.
- The fixed Kavya image repository is internal workflow code. Inputs never select registry, repository, digest, source ref, or canary name.
- Existing-tag probe must accept only exit `10` and exactly one line `image_tag_state=existing`; canary probe must accept only exit `0` and exactly one line `image_tag_state=absent`.
- Capture and suppress all probe, pull, and inspect stdout/stderr. Emit fixed markers only; never print registry errors, tokens, layer progress, image configuration, secrets, or environment dumps.
- Prohibit package writes, build/build-push/push/tag mutation, SSH, rsync, Nginx, systemctl, compose, environment mutation, deploy commands, and host/dashboard actions.
- No publisher dispatch is permitted until the main-branch probe succeeds with tag `37bfaf0` and revision `37bfaf02f04ce7614b9674b1c867b78ab3c7d414`.

---

## File Map

- Create: `.github/workflows/probe-kavya-image.yml` — the single manually dispatched read-only probe job.
- Modify: `Kavya/tests/test_smartpbx_deployment.py` — static workflow contract tests and BaseLoader parser helpers.
- Read only: `.github/workflows/build-kavya-image.yml` — authoritative declared action references and trusted checkout pattern.
- Read only: `.github/scripts/check-kavya-image-tag.sh` — executable probe contract: existing is exit `10`, absent is exit `0`, all uncertain cases exit `1`.

## Interfaces

| Interface | Definition |
| --- | --- |
| Dispatch inputs | `existing_tag: string` matching `^[0-9a-f]{7}$`; `expected_revision: string` matching `^[0-9a-f]{40}$`. |
| Trusted checkout | `actions/checkout@v7` with `ref: ${{ github.workflow_sha }}`, `path: .probe-tools`, and `persist-credentials: false`. |
| Existing probe | `.probe-tools/.github/scripts/check-kavya-image-tag.sh "$image:$existing_tag"` returns `10` and one stdout line `image_tag_state=existing`. |
| Canary probe | The same script receives `"$image:probe-$GITHUB_REPOSITORY_ID-$GITHUB_RUN_ID-$GITHUB_RUN_ATTEMPT"`, returns `0`, and one stdout line `image_tag_state=absent`. |
| Provenance result | `docker image inspect` yields exactly the expected `org.opencontainers.image.revision` value and nothing is logged from pull/inspect streams. |
| Safe evidence | `probe_version=1`, declared action references, workflow commit SHA, runner image/version, actual Buildx version, `existing_tag_state=pass`, `existing_revision=pass`, `canary_state=pass`, and `probe_result=pass`. |

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
    _document, _job, steps, text = kavya_image_probe_job()
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
    assert "${{ inputs." not in "\n".join(workflow_run_strings(steps))
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
    assert '[[ "$(wc -l < "$probe_stdout")" -eq 1 ]]' in existing["run"]
    assert '[[ "$(cat "$probe_stdout")" == "$expected_marker" ]]' in existing["run"]
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
    assert "${{ inputs." not in "\n".join(workflow_run_strings(steps))
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

- [ ] **Step 3: Run the focused RED tests.**

Run:

```bash
/home/dev/full-voice-agent/.venv/bin/python -m pytest -q Kavya/tests/test_smartpbx_deployment.py -k 'kavya_image_probe_requires_exact_states_provenance_and_internal_canary or kavya_image_probe_has_no_write_deploy_or_sensitive_output_surface'
```

Expected: selected semantic tests fail because the skeleton lacks the named probe, provenance, metadata, and summary steps. Do not assert a fixed total.

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
          [[ "$GITHUB_WORKFLOW_SHA" =~ ^[0-9a-f]{40}$ ]] || fail
          runner_image="${ImageOS:-unknown}"
          runner_version="${ImageVersion:-unknown}"
          buildx_version="$(docker buildx version 2>/dev/null | awk '{print $2}')" || fail
          [[ "$runner_image" =~ ^[A-Za-z0-9._-]+$ ]] || fail
          [[ "$runner_version" =~ ^[A-Za-z0-9._-]+$ ]] || fail
          [[ "$buildx_version" =~ ^v[0-9][A-Za-z0-9._-]*$ ]] || fail
          {
            printf 'workflow_commit=%s\n' "$GITHUB_WORKFLOW_SHA"
            printf '%s\n' 'checkout_action=actions/checkout@v7'
            printf '%s\n' 'buildx_action=docker/setup-buildx-action@v4'
            printf '%s\n' 'login_action=docker/login-action@v4'
            printf 'runner_image=%s\n' "$runner_image"
            printf 'runner_image_version=%s\n' "$runner_version"
            printf 'buildx_version=%s\n' "$buildx_version"
          } >> "$GITHUB_STEP_SUMMARY"
      - name: Probe known existing tag
        env:
          EXISTING_TAG: ${{ inputs.existing_tag }}
        run: |
          set -Eeuo pipefail
          fail() { printf 'probe_result=fail\n' >> "$GITHUB_STEP_SUMMARY"; exit 1; }
          image="ghcr.io/taskforce-ai-dev/kavya:${EXISTING_TAG}"
          probe_stdout="$(mktemp)"
          probe_stderr="$(mktemp)"
          trap 'rm -f "$probe_stdout" "$probe_stderr"' EXIT
          expected_marker="image_tag_state=existing"
          set +e
          bash .probe-tools/.github/scripts/check-kavya-image-tag.sh "$image" >"$probe_stdout" 2>"$probe_stderr"
          probe_code=$?
          set -e
          [[ "$probe_code" -eq 10 ]] || fail
          [[ "$(wc -l < "$probe_stdout")" -eq 1 ]] || fail
          [[ "$(cat "$probe_stdout")" == "$expected_marker" ]] || fail
          printf 'existing_tag_state=pass\n' >> "$GITHUB_STEP_SUMMARY"
      - name: Verify existing OCI revision
        env:
          EXISTING_TAG: ${{ inputs.existing_tag }}
          EXPECTED_REVISION: ${{ inputs.expected_revision }}
        run: |
          set -Eeuo pipefail
          fail() { printf 'probe_result=fail\n' >> "$GITHUB_STEP_SUMMARY"; exit 1; }
          image="ghcr.io/taskforce-ai-dev/kavya:${EXISTING_TAG}"
          pull_stdout="$(mktemp)"
          pull_stderr="$(mktemp)"
          inspect_stdout="$(mktemp)"
          inspect_stderr="$(mktemp)"
          trap 'rm -f "$pull_stdout" "$pull_stderr" "$inspect_stdout" "$inspect_stderr"' EXIT
          docker pull "$image" >"$pull_stdout" 2>"$pull_stderr" || fail
          docker image inspect "$image" --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}' >"$inspect_stdout" 2>"$inspect_stderr" || fail
          [[ "$(wc -l < "$inspect_stdout")" -eq 1 ]] || fail
          revision="$(cat "$inspect_stdout")"
          [[ "$revision" == "$EXPECTED_REVISION" ]] || fail
          printf 'existing_revision=pass\n' >> "$GITHUB_STEP_SUMMARY"
      - name: Probe generated absent canary
        run: |
          set -Eeuo pipefail
          fail() { printf 'probe_result=fail\n' >> "$GITHUB_STEP_SUMMARY"; exit 1; }
          image="ghcr.io/taskforce-ai-dev/kavya"
          canary="probe-${GITHUB_REPOSITORY_ID}-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}"
          [[ "$canary" =~ ^probe-[0-9]+-[0-9]+-[0-9]+$ ]] || fail
          [[ ${#canary} -le 128 ]] || fail
          probe_stdout="$(mktemp)"
          probe_stderr="$(mktemp)"
          trap 'rm -f "$probe_stdout" "$probe_stderr"' EXIT
          expected_marker="image_tag_state=absent"
          set +e
          bash .probe-tools/.github/scripts/check-kavya-image-tag.sh "$image:$canary" >"$probe_stdout" 2>"$probe_stderr"
          probe_code=$?
          set -e
          [[ "$probe_code" -eq 0 ]] || fail
          [[ "$(wc -l < "$probe_stdout")" -eq 1 ]] || fail
          [[ "$(cat "$probe_stdout")" == "$expected_marker" ]] || fail
          printf 'canary_state=pass\n' >> "$GITHUB_STEP_SUMMARY"
      - name: Write safe probe summary
        run: |
          set -Eeuo pipefail
          {
            printf '%s\n' 'probe_version=1'
            printf '%s\n' 'probe_result=pass'
          } >> "$GITHUB_STEP_SUMMARY"
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

```bash
gh workflow run probe-kavya-image.yml --ref main -f existing_tag=37bfaf0 -f expected_revision=37bfaf02f04ce7614b9674b1c867b78ab3c7d414
```

Expected: the completed run records `existing_tag_state=pass`, `existing_revision=pass`, `canary_state=pass`, `probe_version=1`, and `probe_result=pass`, together with workflow commit SHA, declared action references, runner image/version metadata, and actual Buildx version. Compare those values in human review; a changed action-ref resolution or unexpected runner or Buildx version fails acceptance.

- [ ] **Step 9: Gate publisher authority on the accepted probe.**

Do not dispatch the immutable publisher for `69ec0b3` unless Step 8 passed and its safe evidence was accepted. A probe failure, malformed marker, existing canary, missing provenance label, metadata mismatch, or uncertain registry result stops the release; correct the reviewed workflow or tests in a new PR and repeat Tasks 1 through 8.

## Plan Self-Check

- Spec coverage: Tasks 1–4 implement the one-job read-only workflow, exact permissions, trusted tooling checkout, declared action-ref parity, strict inputs, canary derivation, exact state handling, provenance pull, safe metadata, and negative surface. Task 5 covers CI, gitleaks, Sol review, protected merge, main dispatch, and the publisher gate.
- Naming and types: Every test function, helper, workflow step, input, marker, and environment variable is defined above and retains the same spelling in later tasks.
- Scope: The only implementation files are the new workflow and its existing-module static tests. The plan contains no image publication, deploy, host, dashboard, environment, Nginx, transfer, or MCP activation work.
