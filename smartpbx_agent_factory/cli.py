"""Argument parsing and redacted output for the SmartPBX agent factory."""

from __future__ import annotations

import argparse
import json
import os
from datetime import date
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
    wizard = commands.add_parser("new", help="interactively write a reviewable non-secret manifest")
    wizard.add_argument("--output", required=True, type=Path)
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
    if args.command == "new":
        try:
            path = create_manifest_wizard(args.output.resolve())
            return CLIResult(EXIT_SUCCESS, f"manifest={path}\nnext: inspect --config <factory.json> --manifest {path}\n")
        except (ValueError, OSError) as error:
            return CLIResult(EXIT_INVALID_INPUT, stderr=f"manifest wizard blocked: {error}\n")
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
            result = orchestrator.last_pr_set
            urls = (getattr(result, "backend_url", None), getattr(result, "operations_url", None), getattr(result, "website_url", None))
            if not all(isinstance(url, str) for url in urls):
                raise GenerationBlockedError("PR coordinator returned no validated URLs")
            return CLIResult(EXIT_SUCCESS, "backend_pr=" + urls[0] + "\noperations_pr=" + urls[1] + "\nwebsite_pr=" + urls[2] + "\nreview_order=backend,operations,website\nstage=THREE_PRS_OPENED\n")
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


def create_manifest_wizard(output: Path, *, input_fn=input) -> Path:
    """Ask only non-secret generation inputs and atomically write mode-0600 JSON."""
    if not output.is_absolute() or output.exists() or output.is_symlink():
        raise ValueError("manifest output must be a new non-symlink absolute path")
    def ask(label: str, pattern: str) -> str:
        value = input_fn(label).strip()
        if not __import__("re").fullmatch(pattern, value):
            raise ValueError(f"invalid {label.rstrip(': ')}")
        return value
    company = ask("Company display name: ", r"[A-Za-z0-9][A-Za-z0-9 .,'&()-]{1,79}")
    slug = ask("Company slug: ", r"[a-z0-9]+(?:-[a-z0-9]+){0,10}")
    languages = ask("Languages (comma-separated ISO codes): ", r"[a-z]{2}(?:,[a-z]{2}){0,5}").split(",")
    provider = ask("Approved provider set (azure-claude-elevenlabs): ", r"azure-claude-elevenlabs")
    knowledge = ask("Approved local knowledge path: ", r"/[A-Za-z0-9._/-]{1,220}")
    if "secret" in knowledge.lower() or "token" in knowledge.lower():
        raise ValueError("knowledge path may not identify secret material")
    locale = {"en": "en-US", "si": "si-LK"}
    if any(code not in locale for code in languages):
        raise ValueError("only configured en and si locales are available to the wizard")
    greetings = {code: ask(f"{code} native greeting: ", r"[^\x00]{1,180}") for code in languages}
    document = {
        "schema_version": 1, "display_name": company, "public_name": company, "slug": slug,
        "agent_name": f"{company} Guide", "industry": "general information", "purpose": "Answer approved company questions",
        "audience": "prospective customers", "profile": "demo", "timezone": "UTC", "operating_hours": {"mon-fri": "09:00-17:00"},
        "technical_owner": "review-required@example.invalid",
        "languages": [{"code": code, "locale": locale[code], "stt": {"provider": "azure"}, "llm": {"provider": "claude", "model": "claude-sonnet-4-5-20250929"}, "tts": {"provider": "elevenlabs", "model": "eleven_flash_v2_5"}, "greeting": greetings[code]} for code in languages],
        "allowed_topics": ["company information"], "refused_topics": ["account changes"],
        "pii_policy": {"explicit_consent": False, "collect_name": False, "collect_phone": False}, "capabilities": {},
        "knowledge_sources": [{"kind": "local", "path": knowledge, "owner": company, "effective_date": date.today().isoformat(), "classification": "public"}],
        "smartpbx": {"account_id": "review-required", "capacity": 1, "protocol_profile": "smartpbx-ai-provider-v07", "status_authentication": True},
        "operations": {"alert_owner": "review-required@example.invalid", "support_contact": "review-required@example.invalid"}, "website_demo": {"enabled": True, "visibility": "pending"},
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(document, handle, sort_keys=True, indent=2)
        handle.write("\n")
    return output
