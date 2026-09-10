"""Argument parsing and redacted output for the SmartPBX agent factory."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from .bootstrap import FactoryBootstrap, FactoryConfig, FactoryConfigError, inspect_config
from .orchestrator import (
    GenerationBlockedError,
    GenerationInfrastructureError,
    GenerationOrchestrator,
)
from .gitops import DirtyWorktreeError, WorktreeConflictError


EXIT_SUCCESS = 0
EXIT_INVALID_INPUT = 2
EXIT_BLOCKED_GATE = 3
EXIT_INFRASTRUCTURE = 4
EXIT_DIRTY_OR_CONFLICT = 5


@dataclass(frozen=True)
class CLIResult:
    exit_code: int
    stdout: str = ""
    stderr: str = ""


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="create-smartpbx-agent")
    commands = parser.add_subparsers(dest="command", required=True)
    bootstrap = commands.add_parser("bootstrap", help="validate strict non-secret factory configuration")
    _add_runtime_arguments(bootstrap)
    for name in ("inspect", "plan"):
        command = commands.add_parser(name)
        command.add_argument("--manifest", required=True, type=Path)
        _add_runtime_arguments(command)
    generate = commands.add_parser("generate")
    generate.add_argument("generation_id")
    _add_runtime_arguments(generate)
    verify = commands.add_parser("verify")
    verify.add_argument("generation_id")
    _add_runtime_arguments(verify)
    resume = commands.add_parser("resume")
    resume.add_argument("generation_id")
    resume.add_argument("--approve-knowledge")
    resume.add_argument("--approve-plan")
    _add_runtime_arguments(resume)
    abandon = commands.add_parser("abandon")
    abandon.add_argument("generation_id")
    _add_runtime_arguments(abandon)
    open_pr = commands.add_parser("open-pr")
    open_pr.add_argument("generation_id")
    _add_runtime_arguments(open_pr)
    return parser


def _add_runtime_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", type=Path, help="strict JSON factory configuration (contains no credential values)")


def invoke_cli(argv: Sequence[str] | None = None) -> CLIResult:
    parser = _parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as error:
        return CLIResult(EXIT_INVALID_INPUT if error.code else EXIT_SUCCESS)
    if not args.config:
        return CLIResult(EXIT_INVALID_INPUT, stderr="--config is required; the factory will not guess repositories, revisions, or credential sources\n")
    try:
        config = FactoryConfig.load(args.config.resolve())
    except FactoryConfigError as error:
        return CLIResult(EXIT_INVALID_INPUT, stderr=f"invalid non-secret config: {error}\n")
    bootstrap = FactoryBootstrap(config)
    if args.command == "bootstrap":
        checks = inspect_config(config)
        return CLIResult(EXIT_SUCCESS if all(value == "ready" for value in checks.values()) else EXIT_BLOCKED_GATE, f"{checks}\n")
    orchestrator = bootstrap.orchestrator()
    try:
        if args.command == "inspect":
            report = {"factory": inspect_config(config), "orchestrator": orchestrator.inspect(args.manifest)}
            ready = all(value == "ready" for value in report["factory"].values()) and report["orchestrator"]["ok"]
            return CLIResult(EXIT_SUCCESS if ready else EXIT_BLOCKED_GATE, f"{report}\n")
        if args.command == "plan":
            report = orchestrator.plan(args.manifest)
            return CLIResult(EXIT_SUCCESS, report.rendered_plan + "\n")
        if args.command == "generate":
            state = orchestrator.generate(
                args.generation_id, binding=bootstrap.binding_for(args.generation_id), secret_provider=bootstrap.secret_provider()
            )
        elif args.command == "verify":
            path = orchestrator.verify_generation(args.generation_id)
            return CLIResult(EXIT_SUCCESS, f"readiness_record={path}\n")
        elif args.command == "resume":
            state = orchestrator.resume(
                args.generation_id,
                knowledge_approval=args.approve_knowledge,
                plan_approval=args.approve_plan,
                binding=bootstrap.binding_for(args.generation_id),
                secret_provider=bootstrap.secret_provider(),
            )
        elif args.command == "abandon":
            state = orchestrator.abandon(args.generation_id)
        elif args.command == "open-pr":
            state = orchestrator.open_pr(args.generation_id)
        else:
            raise GenerationBlockedError("unsupported CLI command")
        return CLIResult(EXIT_SUCCESS, f"generation_id={state.generation_id}\nstage={state.stage.value}\n")
    except GenerationBlockedError as error:
        return CLIResult(EXIT_BLOCKED_GATE, stderr=f"{error}\n")
    except GenerationInfrastructureError as error:
        return CLIResult(EXIT_INFRASTRUCTURE, stderr=f"{error}\n")
    except (DirtyWorktreeError, WorktreeConflictError) as error:
        return CLIResult(EXIT_DIRTY_OR_CONFLICT, stderr=f"{error}\n")
    except (FactoryConfigError, ValueError, OSError) as error:
        return CLIResult(EXIT_INVALID_INPUT, stderr=f"{error}\n")


def main(argv: Sequence[str] | None = None) -> int:
    result = invoke_cli(argv)
    if result.stdout:
        print(result.stdout, end="")
    if result.stderr:
        import sys

        print(result.stderr, end="", file=sys.stderr)
    return result.exit_code
