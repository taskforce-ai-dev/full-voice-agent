"""Argument parsing and redacted output for the SmartPBX agent factory."""

from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
from datetime import date
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from .bootstrap import FactoryBootstrap, FactoryConfig, FactoryConfigError, inspect_config
from .catalogue import CapabilityCatalogue, ProviderModel
from .orchestrator import (
    GenerationBlockedError,
    GenerationInfrastructureError,
    GenerationOrchestrator,
)
from .gitops import DirtyWorktreeError, WorktreeConflictError
from .schema import ManifestError, parse_manifest


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
    wizard.add_argument(
        "--approved-source-root",
        required=True,
        action="append",
        type=Path,
        help="absolute non-secret knowledge root; repeat for each approved root",
    )
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
            path = create_manifest_wizard(
                args.output,
                approved_source_roots=tuple(args.approved_source_root),
            )
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


_DEFAULT_CATALOGUE = Path(__file__).with_name("template_v1") / "provider_catalogue.json"
_NATIVE_GREETING_DEFAULTS = {
    "en": "Welcome to {company}. How may I help you?",
    "si": "ආයුබෝවන්. {company} වෙත ඔබ සාදරයෙන් පිළිගනිමු. මට ඔබට උදව් කළ හැක්කේ කෙසේද?",
}
_LANGUAGE_MENU_LABELS = {"en": "English", "si": "සිංහල"}
_CAPABILITY_NAMES = (
    "booking", "handover", "whatsapp", "crm", "payment",
    "post_call_reporting", "recording", "transcript_retention",
)


def _ask_wizard_text(input_fn, label: str, pattern: str, *, default: str | None = None) -> str:
    value = input_fn(label).strip()
    if not value and default is not None:
        return default
    if not re.fullmatch(pattern, value):
        raise ValueError(f"invalid {label.rstrip(': ')}")
    return value


def _ask_wizard_bool(input_fn, label: str) -> bool:
    value = input_fn(label).strip().lower()
    if value not in {"yes", "no"}:
        raise ValueError(f"invalid {label.rstrip(': ')}; answer yes or no")
    return value == "yes"


def _ask_wizard_list(input_fn, label: str, *, allow_empty: bool = False) -> list[str]:
    value = input_fn(label).strip()
    if not value and allow_empty:
        return []
    entries = [item.strip() for item in value.split(",") if item.strip()]
    if not entries or any("\x00" in item for item in entries):
        raise ValueError(f"invalid {label.rstrip(': ')}")
    return entries


def _ask_provider_choice(
    input_fn,
    *,
    language: str,
    component: str,
    options: tuple[ProviderModel, ...],
    optional: bool = False,
) -> ProviderModel | None:
    runnable = tuple(option for option in options if option.generated_runnable)
    if not runnable:
        if optional:
            return None
        raise ValueError(f"no generated-runnable {component} choices are approved for {language}")
    rendered = ", ".join(
        f"{index}={option.provider}{':' + option.model if option.model else ''}"
        for index, option in enumerate(runnable, start=1)
    )
    none = "; 0=none" if optional else ""
    selected = input_fn(f"{_LANGUAGE_MENU_LABELS.get(language, language)} {component} ({rendered}{none}): ").strip()
    if optional and selected == "0":
        return None
    if not selected.isdigit() or not 1 <= int(selected) <= len(runnable):
        raise ValueError(f"invalid selection for {language} {component}")
    return runnable[int(selected) - 1]


def _pipeline_field(document: dict[str, object], name: str, selection: ProviderModel) -> None:
    document[name] = selection.provider
    if selection.model is not None:
        document[f"{name}_model"] = selection.model


def _parse_operating_hours(value: str) -> dict[str, str]:
    entries = [item.strip() for item in value.split(",") if item.strip()]
    hours: dict[str, str] = {}
    for entry in entries:
        day, separator, period = entry.partition("=")
        if not separator or not re.fullmatch(r"[a-z]{3}(?:-[a-z]{3})?", day) or not period.strip():
            raise ValueError("invalid operating hours; use day=HH:MM-HH:MM")
        hours[day] = period.strip()
    if not hours:
        raise ValueError("invalid operating hours; use day=HH:MM-HH:MM")
    return hours


def _atomic_manifest_write(output: Path, serialized: str) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{output.name}.", suffix=".tmp", dir=output.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        if output.exists() or output.is_symlink():
            raise ValueError("manifest output must be a new non-symlink absolute path")
        os.replace(temporary, output)
        directory_descriptor = os.open(output.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except BaseException:
        if temporary.exists():
            temporary.unlink()
        raise


def create_manifest_wizard(
    output: Path,
    *,
    input_fn=input,
    approved_source_roots: Sequence[Path],
    catalogue_path: Path = _DEFAULT_CATALOGUE,
) -> Path:
    """Collect non-secret manifest inputs from an approved catalogue and write atomically.

    The language prompts are native-language menu labels; the schema itself has only
    a per-language greeting field, so no invented menu field is emitted.
    """
    if not output.is_absolute() or output.is_symlink():
        raise ValueError("manifest output must be a new non-symlink absolute path")
    output = output.resolve(strict=False)
    if output.exists() or output.is_symlink():
        raise ValueError("manifest output must be a new non-symlink absolute path")
    catalogue = CapabilityCatalogue.load(catalogue_path)
    source_roots: list[Path] = []
    for root in approved_source_roots:
        candidate = Path(root)
        if not candidate.is_absolute():
            raise ValueError("approved knowledge roots must be absolute")
        source_roots.append(candidate.resolve(strict=False))
    if not source_roots:
        raise ValueError("at least one approved knowledge root is required")
    company = _ask_wizard_text(input_fn, "Company display name: ", r"[^\x00]{2,80}")
    public_name = _ask_wizard_text(input_fn, "Public company name: ", r"[^\x00]{2,80}")
    slug = _ask_wizard_text(input_fn, "Company slug: ", r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*")
    agent_name = _ask_wizard_text(input_fn, "Agent name: ", r"[^\x00]{2,80}")
    industry = _ask_wizard_text(input_fn, "Industry: ", r"[^\x00]{2,120}")
    purpose = _ask_wizard_text(input_fn, "Agent purpose: ", r"[^\x00]{2,240}")
    audience = _ask_wizard_text(input_fn, "Target audience: ", r"[^\x00]{2,160}")
    profile = _ask_wizard_text(input_fn, "Profile (demo or production-intent): ", r"demo|production-intent")
    timezone = _ask_wizard_text(input_fn, "Timezone: ", r"[A-Za-z_+/-]{1,64}")
    operating_hours = _parse_operating_hours(input_fn("Operating hours (day=HH:MM-HH:MM, comma-separated): ").strip())
    technical_owner = _ask_wizard_text(input_fn, "Technical owner email: ", r"[^\s@]+@[^\s@]+")
    languages = _ask_wizard_list(input_fn, "Languages (comma-separated catalogue codes): ")
    if len(set(languages)) != len(languages) or any(language not in catalogue.languages for language in languages):
        raise ValueError("languages must be unique approved catalogue codes")

    profiles: list[dict[str, object]] = []
    for language in languages:
        locales = tuple(catalogue.languages[language])
        rendered = ", ".join(f"{index}={locale}" for index, locale in enumerate(locales, start=1))
        locale_selection = input_fn(f"{_LANGUAGE_MENU_LABELS.get(language, language)} locale ({rendered}): ").strip()
        if not locale_selection.isdigit() or not 1 <= int(locale_selection) <= len(locales):
            raise ValueError(f"invalid selection for {language} locale")
        locale = locales[int(locale_selection) - 1]
        pipeline = catalogue.languages[language][locale]
        language_document: dict[str, object] = {"code": language, "locale": locale}
        for component in ("stt", "llm", "tts"):
            selection = _ask_provider_choice(
                input_fn, language=language, component=component, options=pipeline[component]
            )
            assert selection is not None
            _pipeline_field(language_document, component, selection)
        fallback = _ask_provider_choice(
            input_fn, language=language, component="fallback", options=pipeline["fallback"], optional=True
        )
        if fallback is not None:
            _pipeline_field(language_document, "fallback", fallback)
        greeting = _ask_wizard_text(
            input_fn,
            f"{_LANGUAGE_MENU_LABELS.get(language, language)} greeting (blank for native default): ",
            r"[^\x00]{1,240}",
            default=_NATIVE_GREETING_DEFAULTS.get(language, "Welcome to {company}. How may I help you?").format(company=public_name),
        )
        language_document["greeting"] = greeting
        profiles.append(language_document)

    allowed_topics = _ask_wizard_list(input_fn, "Allowed topics (comma-separated): ")
    refused_topics = _ask_wizard_list(input_fn, "Refused topics (comma-separated): ")
    explicit_consent = _ask_wizard_bool(input_fn, "Explicit PII consent available (yes/no): ")
    collect_name = _ask_wizard_bool(input_fn, "Collect customer name (yes/no): ")
    collect_phone = _ask_wizard_bool(input_fn, "Collect customer phone (yes/no): ")
    collect_other = _ask_wizard_list(input_fn, "Other PII to collect (comma-separated; blank for none): ", allow_empty=True)
    capabilities: dict[str, object] = {}
    for capability in _CAPABILITY_NAMES:
        enabled = _ask_wizard_bool(input_fn, f"Enable {capability} (yes/no): ")
        capability_document: dict[str, object] = {"enabled": enabled}
        if enabled and capability in {"booking", "handover"}:
            capability_document["destination"] = _ask_wizard_text(
                input_fn, f"{capability} destination: ", r"[^\x00]{1,180}"
            )
        capabilities[capability] = capability_document

    knowledge_path = Path(_ask_wizard_text(input_fn, "Approved local knowledge path: ", r"/[^\x00]{1,220}")).resolve(strict=False)
    if any(marker in str(knowledge_path).lower() for marker in ("secret", "token", "password", "api_key")):
        raise ValueError("knowledge path may not identify secret material")
    if not any(knowledge_path == root or root in knowledge_path.parents for root in source_roots):
        raise ValueError("knowledge path must be under an approved knowledge root")
    knowledge_owner = _ask_wizard_text(input_fn, "Knowledge owner: ", r"[^\x00]{2,120}")
    effective_date = _ask_wizard_text(input_fn, "Knowledge effective date (YYYY-MM-DD): ", r"\d{4}-\d{2}-\d{2}")
    try:
        date.fromisoformat(effective_date)
    except ValueError as error:
        raise ValueError("invalid Knowledge effective date") from error
    classification = _ask_wizard_text(input_fn, "Knowledge classification: ", r"[A-Za-z][A-Za-z _-]{0,80}")
    account_id = _ask_wizard_text(input_fn, "SmartPBX account ID: ", r"[A-Za-z0-9._-]{1,120}")
    capacity = int(_ask_wizard_text(input_fn, "SmartPBX capacity (1-4): ", r"[1-4]"))
    alert_owner = _ask_wizard_text(input_fn, "Operations alert owner email: ", r"[^\s@]+@[^\s@]+")
    support_contact = _ask_wizard_text(input_fn, "Operations support contact email: ", r"[^\s@]+@[^\s@]+")
    website_enabled = _ask_wizard_bool(input_fn, "Enable review-only website demo (yes/no): ")

    document: dict[str, object] = {
        "schema_version": 1,
        "display_name": company,
        "public_name": public_name,
        "slug": slug,
        "agent_name": agent_name,
        "industry": industry,
        "purpose": purpose,
        "audience": audience,
        "profile": profile,
        "timezone": timezone,
        "operating_hours": operating_hours,
        "technical_owner": technical_owner,
        "languages": profiles,
        "allowed_topics": allowed_topics,
        "refused_topics": refused_topics,
        "pii_policy": {
            "explicit_consent": explicit_consent,
            "collect_name": collect_name,
            "collect_phone": collect_phone,
            "collect_other": collect_other,
            "confirmation_policy": "confirm-uncertain",
        },
        "capabilities": capabilities,
        "knowledge_sources": [{
            "kind": "local",
            "path": str(knowledge_path),
            "owner": knowledge_owner,
            "effective_date": effective_date,
            "classification": classification,
        }],
        "smartpbx": {
            "account_id": account_id,
            "capacity": capacity,
            "protocol_profile": "smartpbx-ai-provider-v07",
            "status_authentication": True,
        },
        "operations": {"alert_owner": alert_owner, "support_contact": support_contact},
        "website_demo": {
            "enabled": website_enabled,
            "visibility": "pending",
            "supported_languages": languages,
        },
    }
    serialized = json.dumps(document, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
    try:
        parse_manifest(
            json.loads(serialized), approved_source_roots=tuple(source_roots), catalogue=catalogue
        )
    except (ManifestError, json.JSONDecodeError) as error:
        raise ValueError(f"manifest wizard blocked: {error}") from error
    _atomic_manifest_write(output, serialized)
    return output
