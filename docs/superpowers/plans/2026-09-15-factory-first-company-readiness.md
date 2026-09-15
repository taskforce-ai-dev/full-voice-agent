# First Test Company Readiness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Allow the review-only SmartPBX factory to prepare a non-production test company without workstation-only Docker or stale environment-variable gates, while preserving the independent provenance approval gate for rendered customer runtime code.

**Architecture:** The factory configuration is the authority for operations-lane preflight. `FactoryBootstrap` derives a non-secret, read-only checker from that configuration and supplies it to `GenerationOrchestrator`; the orchestrator no longer invents a second environment-variable configuration surface. Docker remains a repository-owned GitHub CI lifecycle requirement, not a local planning requirement. Template V07 promotion is deliberately a separate gated deliverable because it requires an exact source/image/template provenance audit, not a status-field change.

**Tech Stack:** Python 3.11 standard library, GitHub Actions, SOPS/age, Git worktrees.

## Global Constraints

- The factory remains review-only: no deployment, provider provisioning, Client Connect configuration, or production runtime changes.
- Do not touch Kavya production code, the Rakesh worktree, or any plaintext credential.
- Do not run local pytest. Use `python3 -m unittest` for the new targeted contract and GitHub CI for the repository suite.
- Keep the existing `template_provenance` gate fail-closed until a complete V07 provenance approval is independently evidenced.
- The local preflight must not run SOPS/age decryption, read credential values, or contact a provider.

## File Map

- Modify: `smartpbx_agent_factory/orchestrator.py` — accept an injected read-only operations preflight and remove the local Docker executable gate.
- Modify: `smartpbx_agent_factory/bootstrap.py` — derive the injected checker solely from the loaded immutable factory configuration.
- Create: `smartpbx_agent_factory/tests/test_factory_readiness_unittest.py` — executable local `unittest` contract for the test-first cycle.
- Create: `docs/superpowers/plans/2026-09-15-factory-v07-provenance-promotion.md` — separately scoped source/image/template approval plan.

---

### Task 1: Replace stale operations environment gates with config-bound inspection

**Files:**
- Modify: `smartpbx_agent_factory/orchestrator.py`
- Modify: `smartpbx_agent_factory/bootstrap.py`
- Create: `smartpbx_agent_factory/tests/test_factory_readiness_unittest.py`

**Interfaces:**
- `GenerationOrchestrator(..., operations_prerequisites_checker: Callable[[], str] | None = None)`.
- `FactoryBootstrap.operations_prerequisites_status() -> str` returns only `"ready"` or a bounded `"blocked: ..."` reason and never resolves a secret.

- [ ] **Step 1: Write the failing local contract.**

    ```python
    orchestrator = GenerationOrchestrator(
        tmp_path,
        catalogue_path=CATALOGUE,
        operations_prerequisites_checker=lambda: "ready",
    )
    checks = orchestrator.inspect(FIXTURE)["checks"]
    self.assertEqual(checks["operations_prerequisites"], "ready")
    self.assertNotIn("docker", checks)
    ```

- [ ] **Step 2: Run the contract before implementation.**

    Run: `python3 -B -m unittest smartpbx_agent_factory.tests.test_factory_readiness_unittest`

    Expected: failure because the orchestrator does not yet accept the injected checker and still reports a local Docker gate.

- [ ] **Step 3: Implement the smallest authority-preserving change.**

    ```python
    def _inspect_operations_prerequisites(self) -> str:
        if self._operations_prerequisites_checker is None:
            return "blocked: factory operations preflight is unavailable"
        status = self._operations_prerequisites_checker()
        return status if status == "ready" or status.startswith("blocked: ") else "blocked: factory operations preflight returned an invalid status"
    ```

    `FactoryBootstrap.operations_prerequisites_status` must structurally validate the configured operations primary, immutable remote configuration, recipient-file presence and approved recipient fingerprints, credential-policy shape, and executable SOPS/age paths. It must not call `SopsAgeSecretProvider.validate()`, `credential_reader`, or read an environment variable.

- [ ] **Step 4: Run the contract after implementation.**

    Run: `python3 -B -m unittest smartpbx_agent_factory.tests.test_factory_readiness_unittest`

    Expected: pass.

- [ ] **Step 5: Run static validation and submit for GitHub CI.**

    Run: `python3 -B -m py_compile smartpbx_agent_factory/orchestrator.py smartpbx_agent_factory/bootstrap.py smartpbx_agent_factory/tests/test_factory_readiness_unittest.py && git diff --check`

    Expected: exit 0. The full pytest suite is run only by the repository GitHub Actions workflow.

### Task 2: Preserve the V07 template provenance stop gate

**Files:**
- Create: `docs/superpowers/plans/2026-09-15-factory-v07-provenance-promotion.md`

**Interfaces:**
- The existing `validate_allowlist_metadata(raw)` remains the sole authority for `template_provenance` readiness.
- A V07 allowlist may become approved only when every emitted template/infrastructure file is hash-bound and source/image evidence references one exact immutable runtime revision.

- [ ] **Step 1: Record the complete approval sequence.**

    The follow-on plan must require: exact source revision audit against the pinned backend primary; source-range/hash reconciliation for each candidate template; a complete template-output allowlist; GHCR image digest binding; GitHub CI lifecycle attestation from that exact candidate; and a human reviewer approval. It must state that a partial candidate and a historical source revision cannot be promoted.

- [ ] **Step 2: Do not alter approval metadata in this readiness change.**

    Expected: `template_provenance` remains blocked until the dedicated provenance PR is complete and its CI evidence is reviewed.

### Task 3: Prove the test-company workflow stops safely

**Files:**
- No production-source changes beyond Tasks 1–2.

- [ ] **Step 1: Run the wrapper bootstrap and inspect using the private non-secret test manifest.**

    Run: `create-smartpbx-agent bootstrap` then `create-smartpbx-agent inspect --manifest <private-manifest>`.

    Expected: factory and operations prerequisites report ready after Task 1; template provenance remains the only release gate. No generation state, worktree, provider resource, or deployment is created.

- [ ] **Step 2: Record the result in the PR description.**

    The description must explicitly say that the test company is review-only and explain the intentional provenance stop gate.

## Self-Review

- Spec coverage: Task 1 addresses both observed false blockers without weakening secrets policy; Task 2 prevents a provenance bypass; Task 3 proves the exact safe stopping point for the first test company.
- Placeholder scan: no implementation task depends on an unspecified secret, provider, or deployment action.
- Type consistency: the injected checker returns `str`; only `"ready"` or `"blocked: ..."` is accepted by the orchestrator.
