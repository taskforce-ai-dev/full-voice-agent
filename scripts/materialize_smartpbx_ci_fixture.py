#!/usr/bin/env python3
"""Verify the canonical CI fixture against repository runtime-template evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import tempfile
from pathlib import Path

from run_smartpbx_ci_lifecycle import (
    CANDIDATE_PROVENANCE,
    TEMPLATE_ALLOWLIST,
    LifecycleError,
    _canonical_fixture_binding,
    _document_digest,
    _repository_document,
    provenance_for,
    require_ci_runtime,
)
from smartpbx_agent_factory.provenance import render_template_text


MATERIALIZATION = ".smartpbx-ci-fixture-materialization.json"
PROVENANCE = ".smartpbx-factory-provenance.json"
_TEMPLATE_ROOT = CANDIDATE_PROVENANCE.parent
_FIXTURE_IDENTITY = "Canonical CI Fixture"
_FIXTURE_VARIABLES: dict[str, object] = {
    "agent_slug": "canonical-ci-fixture",
    "ghcr_repository": "ghcr.io/taskforce-ai-dev/smartpbx-canonical-ci-fixture",
    "provider_environment": (
        '      AZURE_SPEECH_KEY: "${AZURE_SPEECH_KEY:-}"\n'
        '      AZURE_SPEECH_REGION: "${AZURE_SPEECH_REGION:-}"\n'
        '      ANTHROPIC_API_KEY: "${ANTHROPIC_API_KEY:-}"\n'
        '      ELEVENLABS_API_KEY: "${ELEVENLABS_API_KEY:-}"\n'
        '      ELEVENLABS_VOICE_ID: "${ELEVENLABS_VOICE_ID:-}"'
    ),
    "provider_requirements": "azure-cognitiveservices-speech==1.51.1\nanthropic==0.120.2\nhttpx==0.28.1",
    "provider_volumes": "",
    "smartpbx_cpus": "2.0",
    "smartpbx_hostname": "smartpbx-canonical-ci-fixture.invalid",
    "smartpbx_memory_limit": "1536m",
    "smartpbx_pids_limit": 256,
    "smartpbx_port": 19999,
    "smartpbx_service": "smartpbx-canonical-ci-fixture",
    "tls_certificate_key_path": "/run/secrets/canonical-ci-key.pem",
    "tls_certificate_path": "/run/secrets/canonical-ci-cert.pem",
    "website_hostname": "demo-canonical-ci-fixture.invalid",
    "website_port": 18999,
    "website_service": "smartpbx-canonical-ci-fixture-website",
    "website_provider_environment": '      ANTHROPIC_API_KEY: "${ANTHROPIC_API_KEY:-}"',
    "wss_header": "X-SmartPBX-Canonical-CI-Token",
}


def _compact_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n"


def _template(path: str) -> str:
    source = _TEMPLATE_ROOT / path
    if not source.is_file() or source.is_symlink():
        raise LifecycleError(f"canonical fixture template is unavailable: {path}")
    return source.read_text(encoding="utf-8")


def _fixture_profile() -> dict[str, object]:
    return {
        "identity": {
            "display_name": _FIXTURE_IDENTITY,
            "public_name": _FIXTURE_IDENTITY,
            "agent_name": _FIXTURE_IDENTITY,
            "industry": "testing",
            "purpose": "Exercise the review-only SmartPBX lifecycle contract.",
            "audience": "CI",
        },
        "languages": {
            "en": {
                "locale": "en-US",
                "stt": "azure",
                "stt_model": "synthetic",
                "llm": "claude",
                "llm_model": "synthetic",
                "tts": "elevenlabs",
                "tts_model": "synthetic",
                "fallback": None,
                "fallback_model": None,
                "prompt_block": "Answer approved test inquiries only.",
                "greeting": "Hello.",
                "menu_prompt": "",
                "recovery_line": "Please try again.",
                "reprompt": "Are you still there?",
                "filler_phrases": ["One moment, please."],
            }
        },
        "default_language": "en",
        "allowed_topics": [],
        "refused_topics": [],
        "knowledge_paths": ["knowledge_docs/fixture.md"],
    }


def _fixture_provider_profile() -> dict[str, object]:
    return {
        "languages": {
            "en": {
                "locale": "en-US",
                "stt": "azure",
                "stt_model": "synthetic",
                "llm": "claude",
                "llm_model": "synthetic",
                "tts": "elevenlabs",
                "tts_model": "synthetic",
            }
        },
        "required_environment": [
            "ANTHROPIC_API_KEY",
            "AZURE_SPEECH_KEY",
            "AZURE_SPEECH_REGION",
            "ELEVENLABS_API_KEY",
            "ELEVENLABS_VOICE_ID",
        ],
    }


def fixture_files() -> dict[str, str]:
    """Materialize the complete non-secret CI tree from checked-in templates."""
    files: dict[str, str] = {
        "AGENTS.md": "# Canonical SmartPBX CI fixture\n\nSynthetic, nondeployable, and review-only.\n",
        "README.md": "# Canonical SmartPBX CI fixture\n\nSynthetic, nondeployable, and review-only.\n",
        ".dockerignore": ".env\n__pycache__/\n.pytest_cache/\n",
        ".smartpbx-nondeployable": "canonical review-only CI fixture\n",
        ".smartpbx-synthetic-runtime": "synthetic runtime enabled only by CI environment gates\n",
        "knowledge_docs/fixture.md": "# Fixture knowledge\n\nNo customer or production information.\n",
        "config/product_profile.json": _compact_json(_fixture_profile()),
        "config/provider_profile.json": _compact_json(_fixture_provider_profile()),
        "product_profile.json": _compact_json(_fixture_profile()),
    }
    for template in sorted((_TEMPLATE_ROOT / "runtime").glob("*.tmpl")):
        files[template.name.removesuffix(".tmpl")] = _template(f"runtime/{template.name}")
    infrastructure_outputs = {
        "Dockerfile.tmpl": "Dockerfile",
        "CLIENT_CONNECT.md.tmpl": "CLIENT_CONNECT.md",
        "SMARTPBX_RUNBOOK.md.tmpl": "SMARTPBX_RUNBOOK.md",
        "WEBSITE_DEMO_RUNBOOK.md.tmpl": "WEBSITE_DEMO_RUNBOOK.md",
        "ci-runtime-review.yml.tmpl": ".github-workflow-fragment.yml",
        "docker-compose.yml.tmpl": "docker-compose.yml",
        "env.example.tmpl": ".env.example",
        "nginx-smartpbx.conf.tmpl": "nginx-smartpbx.conf",
        "nginx-website-demo.conf.tmpl": "nginx-website-demo.conf",
        "requirements-prod.lock.txt.tmpl": "requirements-prod.lock.txt",
        "requirements-prod.txt.tmpl": "requirements-prod.txt",
        "scripts/deploy_runtime_image.sh.tmpl": "scripts/deploy_smartpbx_image.sh",
    }
    for template_name, output_name in infrastructure_outputs.items():
        files[output_name] = render_template_text(
            _template(f"infrastructure/{template_name}"), _FIXTURE_VARIABLES
        )
    return dict(sorted(files.items()))


def _artifact_digest_from_files(files: dict[str, str]) -> str:
    digest = hashlib.sha256()
    for name in sorted(files):
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(files[name].encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def _fixture_provenance(files: dict[str, str]) -> dict[str, object]:
    candidate = _repository_document(CANDIDATE_PROVENANCE, "candidate runtime provenance")
    template = _repository_document(TEMPLATE_ALLOWLIST, "template allowlist")
    source_revision = candidate.get("source_revision")
    template_version = template.get("template_version")
    if not isinstance(source_revision, str) or not isinstance(template_version, str):
        raise LifecycleError("repository candidate fixture metadata is incomplete")
    return {
        "schema_version": 1,
        "artifact_digest": _artifact_digest_from_files(files),
        "source_revision": source_revision,
        "template_version": template_version,
        "template_allowlist_digest": _document_digest(template),
        "candidate_provenance_digest": _document_digest(candidate),
        "runtime_status": "synthetic-nondeployable-ci",
        "runtime": "synthetic-nondeployable-ci",
        "release_state": "review-only",
        "canonical_ci_fixture": True,
        "deployable": False,
        "protocol_version": candidate.get("protocol_version"),
    }


def _write_tree(destination: Path, files: dict[str, str]) -> None:
    if destination.exists():
        raise LifecycleError("refusing to overwrite canonical fixture")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".canonical-fixture-", dir=destination.parent))
    try:
        for name, content in files.items():
            target = temporary / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        materialization = expected_materialization()
        (temporary / MATERIALIZATION).write_text(_compact_json(materialization), encoding="utf-8")
        content_with_materialization = dict(files)
        content_with_materialization[MATERIALIZATION] = _compact_json(materialization)
        (temporary / PROVENANCE).write_text(
            _compact_json(_fixture_provenance(content_with_materialization)), encoding="utf-8"
        )
        temporary.replace(destination)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def expected_materialization() -> dict[str, object]:
    candidate = _repository_document(CANDIDATE_PROVENANCE, "candidate runtime provenance")
    template = _repository_document(TEMPLATE_ALLOWLIST, "template allowlist")
    rendered_runtime: dict[str, str] = {}
    for source in sorted((_TEMPLATE_ROOT / "runtime").glob("*.tmpl")):
        if not source.is_file() or source.is_symlink():
            raise LifecycleError("repository candidate runtime template is unavailable")
        rendered_runtime[source.name.removesuffix(".tmpl")] = hashlib.sha256(source.read_bytes()).hexdigest()
    if not rendered_runtime:
        raise LifecycleError("repository candidate provenance has no runtime template outputs")
    source_revision = candidate.get("source_revision")
    template_version = template.get("template_version")
    if not isinstance(source_revision, str) or not isinstance(template_version, str):
        raise LifecycleError("repository candidate fixture metadata is incomplete")
    return {
        "schema_version": 1,
        "candidate_provenance_digest": _document_digest(candidate),
        "source_revision": source_revision,
        "template_version": template_version,
        "runtime_template_digests": dict(sorted(rendered_runtime.items())),
        "infrastructure_variables_digest": hashlib.sha256(
            json.dumps(_FIXTURE_VARIABLES, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
        ).hexdigest(),
    }


def verify_fixture(fixture: Path, output: Path) -> None:
    fixture = fixture.resolve()
    provenance = provenance_for(fixture)
    require_ci_runtime(fixture, provenance, canonical_fixture=True)
    _canonical_fixture_binding(provenance)
    expected = expected_materialization()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(expected, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    committed = fixture / MATERIALIZATION
    try:
        actual = json.loads(committed.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LifecycleError("canonical fixture lacks deterministic materialization evidence") from exc
    if actual != expected:
        raise LifecycleError("canonical fixture differs from repository runtime-template materialization")
    expected_files = fixture_files()
    actual_files: dict[str, Path] = {}
    for source in fixture.rglob("*"):
        relative = source.relative_to(fixture).as_posix()
        if source.is_symlink():
            raise LifecycleError("canonical fixture contains a symlink")
        if source.is_file() and relative not in {PROVENANCE, MATERIALIZATION}:
            actual_files[relative] = source
    if set(actual_files) != set(expected_files):
        raise LifecycleError("canonical fixture file set differs from deterministic materialization")
    for output_name, expected_content in expected_files.items():
        rendered = actual_files[output_name]
        if rendered.read_text(encoding="utf-8") != expected_content:
            raise LifecycleError("canonical fixture output differs from the exact repository template materialization")


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify a deterministic, review-only SmartPBX CI fixture")
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--write-fixture",
        action="store_true",
        help="materialize the fixture once from the exact checked-in candidate templates",
    )
    args = parser.parse_args()
    if args.write_fixture:
        _write_tree(args.fixture.resolve(), fixture_files())
    verify_fixture(args.fixture, args.output)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except LifecycleError as exc:
        print(f"smartpbx fixture: blocked reason={exc}", file=sys.stderr)
        raise SystemExit(1)
