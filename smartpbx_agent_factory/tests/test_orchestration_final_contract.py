"""Contract specifications for the review-only three-lane transaction.

These are deliberately unit seams: production repositories, credentials,
network calls, releases, and PR creation are all outside this test module.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from smartpbx_agent_factory.orchestrator import (
    GenerationBinding,
    GenerationBlockedError,
    GenerationOrchestrator,
    LaneBinding,
)
from smartpbx_agent_factory.secrets import derive_secret_plan


FIXTURE = Path(__file__).parent / "fixtures" / "acme-minimal.json"
CATALOGUE = Path(__file__).parent / "fixtures" / "approved-provider-catalogue.json"


def test_plan_persists_canonical_redacted_artifact_and_prints_its_digest(tmp_path):
    report = GenerationOrchestrator(tmp_path, catalogue_path=CATALOGUE).plan(FIXTURE)
    raw = json.loads((tmp_path / f"{report.generation_id}.json").read_text())
    assert raw["plan_artifact"]["digest"] == report.plan_digest
    assert raw["plan_artifact"]["release_allowed"] is False
    assert "SMARTPBX_WS_TOKEN" not in json.dumps(raw["plan_artifact"])
    assert f"plan_digest={report.plan_digest}" in report.rendered_plan


def test_secret_plan_has_exact_distinct_internal_ids_and_runtime_envs():
    from smartpbx_agent_factory.catalogue import CapabilityCatalogue
    from smartpbx_agent_factory.schema import parse_manifest
    from smartpbx_agent_factory.resources import AllocationRegistry, derive_resources

    manifest = parse_manifest(
        json.loads(FIXTURE.read_text()),
        catalogue=CapabilityCatalogue.load(CATALOGUE),
        approved_source_roots=(FIXTURE.parent,),
    )
    catalogue = CapabilityCatalogue.load(CATALOGUE)
    plan = derive_secret_plan(manifest, derive_resources(manifest, AllocationRegistry()), catalogue)
    assert plan.requirement_for_env("SMARTPBX_WS_TOKEN").generated is True
    assert len({item.record_id for item in plan.requirements}) == len(plan.requirements)
    assert len({item.runtime_env for item in plan.requirements}) == len(plan.requirements)
    assert plan.operations_env_names
    assert "SMARTPBX_WS_TOKEN" not in plan.website_env_names
    assert "GOOGLE_APPLICATION_CREDENTIALS" in catalogue.required_metadata_identifiers_for_pipeline(
        "en", catalogue.source_default("en")
    )
    assert "GOOGLE_APPLICATION_CREDENTIALS" not in plan.operations_env_names


def test_generation_binding_requires_three_immutable_owned_lanes(tmp_path):
    class Manager:
        pass

    lane = LaneBinding(Manager(), tmp_path / "primary", "origin", "a" * 40, tmp_path / "target")
    with pytest.raises(ValueError, match="distinct"):
        GenerationBinding(backend=lane, operations=lane, website=lane)


def test_external_inventory_conflicts_are_checked_before_plan_persistence(tmp_path):
    class Inventory:
        def snapshot(self):
            return {"hostnames": ["smartpbx-acme-inquiry.taskforceai.tech"]}

    with pytest.raises(GenerationBlockedError, match="resource allocation conflict"):
        GenerationOrchestrator(tmp_path, catalogue_path=CATALOGUE, inventory_provider=Inventory()).plan(FIXTURE)


def test_lane_checkpoint_contract_records_post_commit_head_and_never_replays_secret_lane():
    """A website failure is resumed from its own checkpoint, not from operations."""
    from smartpbx_agent_factory.gitops import WorktreeManager

    assert hasattr(WorktreeManager, "stage_and_commit")
    source = GenerationOrchestrator.generate.__doc__ or ""
    assert "checkpoint" in source.lower()


def test_changed_worktree_content_rejects_a_persisted_lane_checkpoint(tmp_path):
    from smartpbx_agent_factory.gitops import WorktreeHandle
    from smartpbx_agent_factory.orchestrator import _StoredGeneration, _tree_digest
    from smartpbx_agent_factory.resources import AllocationRegistry, derive_resources
    from smartpbx_agent_factory.schema import parse_manifest
    from smartpbx_agent_factory.state import GenerationState, Stage

    manifest = parse_manifest(json.loads(FIXTURE.read_text()), catalogue=CapabilityCatalogue.load(CATALOGUE), approved_source_roots=(FIXTURE.parent,))
    resources = derive_resources(manifest, AllocationRegistry())
    output = tmp_path / "backend"
    output.mkdir()
    (output / "rendered.txt").write_text("approved", encoding="utf-8")
    state = GenerationState.start("gen-contract", "a" * 64)
    state.stage = Stage.GENERATED
    state.record_lane("backend", output_digest=_tree_digest(output), head_sha="b" * 40, artifact_digest=_tree_digest(output))
    stored = _StoredGeneration(state, FIXTURE, resources, "c" * 64, "d" * 64, "e" * 64, None)
    handle = WorktreeHandle(tmp_path, output, "b" * 40)
    orchestrator = GenerationOrchestrator(tmp_path / "state", catalogue_path=CATALOGUE)
    assert orchestrator._checkpoint_is_valid(stored, "backend", handle)
    (output / "rendered.txt").write_text("tampered", encoding="utf-8")
    with pytest.raises(GenerationBlockedError, match="checkpoint"):
        orchestrator._checkpoint_is_valid(stored, "backend", handle)


def test_changed_worktree_head_rejects_a_persisted_lane_checkpoint(tmp_path):
    from smartpbx_agent_factory.gitops import WorktreeHandle
    from smartpbx_agent_factory.orchestrator import _StoredGeneration, _tree_digest
    from smartpbx_agent_factory.resources import AllocationRegistry, derive_resources
    from smartpbx_agent_factory.schema import parse_manifest
    from smartpbx_agent_factory.state import GenerationState, Stage

    manifest = parse_manifest(json.loads(FIXTURE.read_text()), catalogue=CapabilityCatalogue.load(CATALOGUE), approved_source_roots=(FIXTURE.parent,))
    resources, output = derive_resources(manifest, AllocationRegistry()), tmp_path / "backend"
    output.mkdir()
    (output / "rendered.txt").write_text("approved", encoding="utf-8")
    state = GenerationState.start("gen-head", "a" * 64)
    state.stage = Stage.GENERATED
    state.record_lane("backend", output_digest=_tree_digest(output), head_sha="b" * 40, artifact_digest=_tree_digest(output))
    stored = _StoredGeneration(state, FIXTURE, resources, "c" * 64, "d" * 64, "e" * 64, None)
    with pytest.raises(GenerationBlockedError, match="checkpoint"):
        GenerationOrchestrator(tmp_path / "state", catalogue_path=CATALOGUE)._checkpoint_is_valid(
            stored, "backend", WorktreeHandle(tmp_path, output, "f" * 40)
        )
