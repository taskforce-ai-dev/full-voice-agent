import json
import os
from pathlib import Path

import pytest

from smartpbx_agent_factory.orchestrator import (
    GenerationBlockedError,
    GenerationInfrastructureError,
    GenerationOrchestrator,
)
from smartpbx_agent_factory.state import Stage


FIXTURE = Path(__file__).parent / "fixtures" / "acme-minimal.json"
CATALOGUE = Path(__file__).parent / "fixtures" / "approved-provider-catalogue.json"


def test_plan_writes_only_private_state_without_claiming_secret_resolution(tmp_path):
    orchestrator = GenerationOrchestrator(tmp_path, catalogue_path=CATALOGUE)
    report = orchestrator.plan(FIXTURE)
    state_file = tmp_path / f"{report.generation_id}.json"
    assert report.state.stage is Stage.INPUT_COLLECTED
    assert state_file.exists()
    assert state_file.stat().st_mode & 0o777 == 0o600
    assert "wss_token" not in report.rendered_plan
    assert list(tmp_path.iterdir()) == [state_file]


def test_resume_refuses_changed_manifest_digest(tmp_path):
    fixture = tmp_path / "manifest.json"
    fixture.write_bytes(FIXTURE.read_bytes())
    orchestrator = GenerationOrchestrator(tmp_path, catalogue_path=CATALOGUE)
    report = orchestrator.plan(fixture)
    raw = json.loads(fixture.read_text(encoding="utf-8"))
    raw["purpose"] = "Changed purpose"
    fixture.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(GenerationBlockedError, match="manifest digest changed"):
        orchestrator.resume(report.generation_id)


def test_generate_fails_closed_before_any_artifact_without_both_approvals(tmp_path):
    orchestrator = GenerationOrchestrator(tmp_path, catalogue_path=CATALOGUE)
    report = orchestrator.plan(FIXTURE)
    with pytest.raises(GenerationBlockedError, match="secret resolution"):
        orchestrator.generate(report.generation_id)


def test_abandon_marks_generation_without_removing_state_audit(tmp_path):
    orchestrator = GenerationOrchestrator(tmp_path, catalogue_path=CATALOGUE)
    report = orchestrator.plan(FIXTURE)
    state = orchestrator.abandon(report.generation_id)
    assert state.stage is Stage.ABANDONED
    assert (tmp_path / f"{report.generation_id}.json").exists()


def test_state_write_is_atomic_and_uses_replace(tmp_path, monkeypatch):
    replacements: list[tuple[Path, Path]] = []
    original_replace = os.replace

    def replace(source, destination):
        replacements.append((Path(source), Path(destination)))
        return original_replace(source, destination)

    monkeypatch.setattr(os, "replace", replace)
    report = GenerationOrchestrator(tmp_path, catalogue_path=CATALOGUE).plan(FIXTURE)
    state_file = tmp_path / f"{report.generation_id}.json"
    assert len(replacements) == 1
    assert replacements[0][1] == state_file
    assert state_file.stat().st_mode & 0o777 == 0o600


def test_state_loader_rejects_symlinked_state_file(tmp_path):
    orchestrator = GenerationOrchestrator(tmp_path, catalogue_path=CATALOGUE)
    report = orchestrator.plan(FIXTURE)
    state_file = tmp_path / f"{report.generation_id}.json"
    target = tmp_path / "other.json"
    target.write_bytes(state_file.read_bytes())
    state_file.unlink()
    state_file.symlink_to(target)
    with pytest.raises(GenerationInfrastructureError, match="symlink"):
        orchestrator.resume(report.generation_id)


def test_plan_rejects_knowledge_path_authorized_only_by_process_cwd(tmp_path):
    manifest = tmp_path / "manifest.json"
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    raw["knowledge_sources"][0]["path"] = "smartpbx_agent_factory/tests/fixtures/acme-faq.txt"
    manifest.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(GenerationBlockedError, match="approved root"):
        GenerationOrchestrator(tmp_path / "state", catalogue_path=CATALOGUE).plan(manifest)


def test_inspect_reports_explicit_provenance_repository_and_operations_gates(tmp_path):
    report = GenerationOrchestrator(tmp_path, catalogue_path=CATALOGUE).inspect(FIXTURE)
    checks = report["checks"]
    assert {"source_repository", "origin_main", "template_provenance", "operations_prerequisites"} <= set(checks)
    assert checks["template_provenance"].startswith(("ready", "blocked:"))
    assert checks["operations_prerequisites"].startswith(("ready", "blocked:"))


def test_abandon_refuses_legacy_state_without_cleanup_inventory(tmp_path):
    orchestrator = GenerationOrchestrator(tmp_path, catalogue_path=CATALOGUE)
    report = orchestrator.plan(FIXTURE)
    state_file = tmp_path / f"{report.generation_id}.json"
    raw = json.loads(state_file.read_text(encoding="utf-8"))
    raw.pop("cleanup_inventory", None)
    state_file.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(GenerationBlockedError, match="cleanup inventory"):
        orchestrator.abandon(report.generation_id)
