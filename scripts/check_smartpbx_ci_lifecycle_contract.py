#!/usr/bin/env python3
"""Static, dependency-free contract checks for the CI lifecycle harness."""

from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VERIFY = ROOT / "smartpbx_agent_factory" / "verify.py"
RUNNER = ROOT / "scripts" / "run_smartpbx_ci_lifecycle.py"
MATERIALIZER = ROOT / "scripts" / "materialize_smartpbx_ci_fixture.py"
WORKFLOW = ROOT / ".github" / "workflows" / "smartpbx-generated-agents.yml"


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(f"CI lifecycle contract failed: {message}")


def main() -> int:
    verify_source = VERIFY.read_text(encoding="utf-8")
    verify_tree = ast.parse(verify_source, filename=str(VERIFY))
    verify_function = next(
        (node for node in verify_tree.body if isinstance(node, ast.FunctionDef) and node.name == "verify_generated_backend"),
        None,
    )
    require(verify_function is not None, "verify_generated_backend is required")
    names = {argument.arg for argument in verify_function.args.args + verify_function.args.kwonlyargs}
    require("lifecycle_adapter" not in names, "verification must not accept a caller lifecycle adapter")
    require("DisposableLifecycleAdapter" not in verify_source, "caller-forgeable lifecycle protocol must be absent")

    runner_source = RUNNER.read_text(encoding="utf-8")
    for required in (
        "127.0.0.1", "docker", "health", "smartpbx/status", "HTTP/1.1 403", "sec-websocket-accept",
        "cross_agent_rejection", "account-cross-agent", "missing or wrong authentication", "stop", "hangup",
        "PROTOCOL_FIXTURE", "active_sessions", "active_tasks", "active_resources", "admitted_total", "released_total",
        "HEALTH_WAIT_SECONDS", "finally", "network\", \"rm", "image\", \"rm", "nondeployable",
        "--canonical-fixture", "canonical_ci_fixture", "template_allowlist_digest", "candidate_provenance_digest", "8000/tcp", "127.0.0.1::8000",
        "SMARTPBX_RUNTIME_MODE=synthetic", "SMARTPBX_ALLOW_SYNTHETIC_FOR_CI=1",
        "SMARTPBX_PRODUCT_PROFILE_PATH=/app/config/product_profile.json",
        "SMARTPBX_KNOWLEDGE_DIR=/app/knowledge_docs",
        "SMARTPBX_PROVIDER_PROFILE_PATH=/app/config/provider_profile.json",
        "validate_allowlist_metadata", "CANDIDATE_PROVENANCE", "_normal_runtime_binding", "_canonical_fixture_binding",
        "rejected_status", "status authentication", "--attestation", "--lane", "--repository", "--head-sha", "--run-id", "observed_cases",
        "container-state-inspect", "port-inspect", "CONTAINER_STATE_STATUSES", "{{json .NetworkSettings.Ports}}",
        "_container_state_from_inspect", "_mapped_port_from_inspect",
    ):
        require(required in runner_source, f"runner missing {required!r}")
    for forbidden in (
        "print(response", "print(body", "communicate(input=", "GOOGLE_APPLICATION_CREDENTIALS",
        "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "ELEVENLABS_API_KEY",
        '"docker", "port"',
    ):
        require(forbidden not in runner_source, f"runner contains unsafe or unowned execution form {forbidden!r}")
    require('provenance.get("release_state") == "review-only"' in runner_source, "review-only release state must be accepted for the canonical fixture")
    require("Authorization" not in runner_source, "status must use the integrated agent-specific authentication header")
    require(runner_source.count("time.sleep(") == 1, "only bounded health waiting may sleep")
    materializer_source = MATERIALIZER.read_text(encoding="utf-8")
    for required in ("candidate_provenance_digest", "runtime_template_digests", "provenance_for", "require_ci_runtime", "differs from the exact repository template"):
        require(required in materializer_source, f"fixture materializer missing {required!r}")

    fixture = (ROOT / "smartpbx_agent_factory" / "tests" / "fixtures" / "protocol_messages.json").read_text(encoding="utf-8")
    for required in ("callId", "otherLegCallId", "callerIdNumber", "calleeIdNumber", "mediaFormat", "g711_ulaw", "sampleRate", "normal_clearing"):
        require(required in fixture, f"canonical protocol fixture missing {required!r}")
    require("<synthetic-silence>" not in fixture, "fixture must carry valid base64 ulaw silence")

    workflow = WORKFLOW.read_text(encoding="utf-8")
    require("check_smartpbx_ci_lifecycle_contract.py" in workflow, "workflow must execute static contract check")
    require("materialize_smartpbx_ci_fixture.py" in workflow, "workflow must materialize or verify the canonical fixture")
    require("run_smartpbx_ci_lifecycle.py" in workflow, "workflow must invoke repository-owned runner")
    require("SmartPBX Agents/**" in workflow, "workflow must cover generated-tree changes")
    require("scripts/check_smartpbx_ci_lifecycle_contract.py" in workflow and "scripts/run_smartpbx_ci_lifecycle.py" in workflow, "workflow paths must cover both lifecycle scripts")
    require("timeout-minutes: 10" in workflow, "workflow lifecycle job must be bounded")
    require('branches: [main, "smartpbx-agent-factory/**"]' in workflow, "workflow must allow only main and exact factory review branches")
    require('branches: ["**"]' not in workflow, "workflow must not run lifecycle proof on arbitrary branches")
    require("github.event_name" in workflow and "github.event.before" in workflow and "github.event.pull_request.base.sha" in workflow, "workflow must select a robust diff base")
    require("canonical_fixture=\"SmartPBX Agents/.ci-lifecycle-canonical\"" in workflow, "workflow must name the canonical review-only fixture")
    require("canonical review-only CI fixture is required" in workflow, "workflow must fail rather than pass lifecycle-relevant changes without the fixture")
    require("--canonical-fixture" in workflow and "relevant_changed" in workflow, "workflow must run the canonical fixture on every lifecycle-relevant change")
    require("--attestation" in workflow and "actions/upload-artifact@v4" in workflow, "workflow must publish redacted lifecycle attestation evidence")
    require("--lane backend" in workflow and "--repository" in workflow and "--head-sha" in workflow and "--run-id" in workflow, "workflow attestations must bind the backend repository, head, and run")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
