import json
from pathlib import Path

import pytest

from smartpbx_agent_factory.orchestrator import GenerationBlockedError, GenerationOrchestrator
from smartpbx_agent_factory.state import Stage


FIXTURE = Path(__file__).parent / "fixtures" / "acme-minimal.json"
CATALOGUE = Path(__file__).parent / "fixtures" / "approved-provider-catalogue.json"


def test_plan_writes_only_private_state_and_requires_knowledge_approval(tmp_path):
    orchestrator = GenerationOrchestrator(tmp_path, catalogue_path=CATALOGUE)
    report = orchestrator.plan(FIXTURE)
    state_file = tmp_path / f"{report.generation_id}.json"
    assert report.state.stage is Stage.KNOWLEDGE_REVIEW_REQUIRED
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
    with pytest.raises(GenerationBlockedError, match="knowledge approval"):
        orchestrator.generate(report.generation_id)


def test_abandon_marks_generation_without_removing_state_audit(tmp_path):
    orchestrator = GenerationOrchestrator(tmp_path, catalogue_path=CATALOGUE)
    report = orchestrator.plan(FIXTURE)
    state = orchestrator.abandon(report.generation_id)
    assert state.stage is Stage.ABANDONED
    assert (tmp_path / f"{report.generation_id}.json").exists()
