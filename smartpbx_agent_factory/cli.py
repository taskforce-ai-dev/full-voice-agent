"""Argument parsing and redacted output for the SmartPBX agent factory."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

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
    for name in ("inspect", "plan"):
        command = commands.add_parser(name)
        command.add_argument("--manifest", required=True, type=Path)
        _add_runtime_arguments(command)
    generate = commands.add_parser("generate")
    generate.add_argument("generation_id")
    _add_runtime_arguments(generate)
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
    parser.add_argument("--state-root", type=Path, default=Path(".smartpbx-generations"))
    parser.add_argument("--catalogue", type=Path)


def invoke_cli(argv: Sequence[str] | None = None) -> CLIResult:
    parser = _parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as error:
        return CLIResult(EXIT_INVALID_INPUT if error.code else EXIT_SUCCESS)
    state_root = args.state_root if args.state_root.is_absolute() else (Path.cwd() / args.state_root)
    catalogue = args.catalogue.resolve() if args.catalogue else None
    if args.command in {"generate", "open-pr"}:
        return CLIResult(
            EXIT_BLOCKED_GATE,
            stderr=(
                f"{args.command} is library-only: configure non-secret lane adapters, "
                "an injected secret provider, and a coordinator-owned verifier in Python; "
                "the CLI never guesses repositories, credentials, or readiness.\n"
            ),
        )
    orchestrator = GenerationOrchestrator(state_root.resolve(), catalogue_path=catalogue)
    try:
        if args.command == "inspect":
            report = orchestrator.inspect(args.manifest)
            return CLIResult(EXIT_SUCCESS if report["ok"] else EXIT_BLOCKED_GATE, f"{report}\n")
        if args.command == "plan":
            report = orchestrator.plan(args.manifest)
            return CLIResult(EXIT_SUCCESS, report.rendered_plan + "\n")
        if args.command == "resume":
            state = orchestrator.resume(
                args.generation_id,
                knowledge_approval=args.approve_knowledge,
                plan_approval=args.approve_plan,
            )
        elif args.command == "abandon":
            state = orchestrator.abandon(args.generation_id)
        else:
            raise GenerationBlockedError("unsupported CLI command")
        return CLIResult(EXIT_SUCCESS, f"generation_id={state.generation_id}\nstage={state.stage.value}\n")
    except GenerationBlockedError as error:
        return CLIResult(EXIT_BLOCKED_GATE, stderr=f"{error}\n")
    except GenerationInfrastructureError as error:
        return CLIResult(EXIT_INFRASTRUCTURE, stderr=f"{error}\n")
    except (DirtyWorktreeError, WorktreeConflictError) as error:
        return CLIResult(EXIT_DIRTY_OR_CONFLICT, stderr=f"{error}\n")
    except (ValueError, OSError) as error:
        return CLIResult(EXIT_INVALID_INPUT, stderr=f"{error}\n")


def main(argv: Sequence[str] | None = None) -> int:
    result = invoke_cli(argv)
    if result.stdout:
        print(result.stdout, end="")
    if result.stderr:
        import sys

        print(result.stderr, end="", file=sys.stderr)
    return result.exit_code
