#!/usr/bin/env python3
"""Verify the canonical CI fixture against repository runtime-template evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
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


MATERIALIZATION = ".smartpbx-ci-fixture-materialization.json"


def expected_materialization() -> dict[str, object]:
    candidate = _repository_document(CANDIDATE_PROVENANCE, "candidate runtime provenance")
    template = _repository_document(TEMPLATE_ALLOWLIST, "template allowlist")
    components = candidate.get("components")
    if not isinstance(components, list):
        raise LifecycleError("repository candidate provenance lacks runtime template evidence")
    rendered_runtime: dict[str, str] = {}
    for component in components:
        if not isinstance(component, dict):
            raise LifecycleError("repository candidate provenance has invalid runtime template evidence")
        template_path = component.get("template_path")
        if not isinstance(template_path, str) or not template_path.startswith("runtime/") or not template_path.endswith(".tmpl"):
            continue
        output = Path(template_path).name.removesuffix(".tmpl")
        source = CANDIDATE_PROVENANCE.parent / template_path
        if not source.is_file() or source.is_symlink():
            raise LifecycleError("repository candidate runtime template is unavailable")
        rendered_runtime[output] = hashlib.sha256(source.read_bytes()).hexdigest()
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
    for output_name, expected_digest in expected["runtime_template_digests"].items():
        rendered = fixture / output_name
        if not rendered.is_file() or rendered.is_symlink() or hashlib.sha256(rendered.read_bytes()).hexdigest() != expected_digest:
            raise LifecycleError("canonical fixture runtime output differs from the exact repository template")


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify a deterministic, review-only SmartPBX CI fixture")
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    verify_fixture(args.fixture, args.output)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except LifecycleError as exc:
        print(f"smartpbx fixture: blocked reason={exc}", file=sys.stderr)
        raise SystemExit(1)
