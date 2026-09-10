#!/usr/bin/env python3
"""Static, dependency-free contract checks for the CI lifecycle harness."""

from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VERIFY = ROOT / "smartpbx_agent_factory" / "verify.py"
RUNNER = ROOT / "scripts" / "run_smartpbx_ci_lifecycle.py"
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
        "127.0.0.1", "docker", "health", "smartpbx/status", "cross-agent", "missing", "wrong",
        "stop", "hangup", "active_sessions", "active_tasks", "active_resources", "finally", "network\", \"rm",
        "image\", \"rm", "review-only",
    ):
        require(required in runner_source, f"runner missing {required!r}")
    for forbidden in ("print(response", "print(body", "communicate(input="):
        require(forbidden not in runner_source, f"runner contains unsafe or unowned execution form {forbidden!r}")

    workflow = WORKFLOW.read_text(encoding="utf-8")
    require("check_smartpbx_ci_lifecycle_contract.py" in workflow, "workflow must execute static contract check")
    require("run_smartpbx_ci_lifecycle.py" in workflow, "workflow must invoke repository-owned runner")
    require("SmartPBX Agents/**" in workflow, "workflow must cover generated-tree changes")
    require("no generated agent directory" in workflow, "workflow must fail rather than pass empty generated-tree changes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
