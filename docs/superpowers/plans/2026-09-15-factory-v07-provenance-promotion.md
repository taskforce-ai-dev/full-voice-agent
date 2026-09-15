# Factory V07 Provenance Promotion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Promote the SmartPBX V07 factory runtime template only when every generated file is bound to the exact reviewed source and immutable image that GitHub CI lifecycle-tested.

**Architecture:** This is intentionally independent of first-company local readiness. The candidate provenance, complete allowlist, and generated canonical fixture must describe the same pinned backend revision and image digest. GitHub CI produces the redacted lifecycle attestation; a reviewer decides whether those facts permit changing the allowlist status to approved.

**Tech Stack:** Python 3.11, SHA-256, GitHub Container Registry, GitHub Actions, Docker in CI.

## Global Constraints

- Do not copy mutable Kavya code, alter production routing, or use a branch name as evidence.
- Do not mark `file_allowlist.json` approved merely because a fixture starts.
- Never record credentials or transcript content in provenance, tests, or attestation.
- The authoritative runtime source and image are immutable full identifiers, not local HEAD.

### Task 1: Reconcile source mappings

**Files:**
- Modify: `smartpbx_agent_factory/template_v1/candidate_runtime_provenance.json`
- Modify: `smartpbx_agent_factory/template_v1/runtime/*.tmpl` only where an audited source difference requires it
- Test: `smartpbx_agent_factory/tests/test_provenance.py`

- [ ] **Step 1: Write tests that reject a candidate whose source revision differs from the pinned runtime revision or whose source range hash is stale.**
- [ ] **Step 2: Run the new test in GitHub CI and observe the expected red result.**
- [ ] **Step 3: Audit every candidate source path/range against the pinned full backend SHA and update only mapped templates whose source differs.**
- [ ] **Step 4: Update candidate revision and per-range hashes from the audited source, then verify the test passes in CI.**

### Task 2: Produce a complete render allowlist

**Files:**
- Modify: `smartpbx_agent_factory/template_v1/file_allowlist.json`
- Test: `smartpbx_agent_factory/tests/test_render.py`

- [ ] **Step 1: Write a test that compares the exact complete set of files emitted by the renderer with the allowlist and rejects missing, extra, or hash-mismatched entries.**
- [ ] **Step 2: Observe red CI against the current partial allowlist.**
- [ ] **Step 3: Generate entries for every runtime and infrastructure template with an exact SHA-256 and stable output path; retain only truthful source references.**
- [ ] **Step 4: Verify green CI; do not change allowlist approval status yet.**

### Task 3: Bind the image and lifecycle evidence

**Files:**
- Modify: `smartpbx_agent_factory/template_v1/file_allowlist.json`
- Modify: `.github/workflows/smartpbx-generated-agents.yml` only if it lacks an assertion that candidate revision, image digest and attestation revision are identical
- Test: `smartpbx_agent_factory/tests/test_ci_lifecycle_diagnostics_unittest.py`

- [ ] **Step 1: Add a test that rejects a lifecycle attestation when its source revision or image digest differs from the candidate and complete allowlist.**
- [ ] **Step 2: Observe the expected red CI result.**
- [ ] **Step 3: Run the existing repository-owned Docker lifecycle workflow from the exact candidate commit and retain only its redacted attestation digest, source revision, image digest, and pass/fail observations.**
- [ ] **Step 4: Submit a reviewer decision that either marks the complete V07 allowlist approved or leaves it blocked; no automated task makes that decision.**
