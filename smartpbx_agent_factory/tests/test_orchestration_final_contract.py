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
    plan = derive_secret_plan(manifest, derive_resources(manifest, AllocationRegistry()))
    assert plan.requirement_for_env("SMARTPBX_WS_TOKEN").generated is True
    assert len({item.record_id for item in plan.requirements}) == len(plan.requirements)
    assert len({item.runtime_env for item in plan.requirements}) == len(plan.requirements)
    assert plan.operations_env_names
    assert "SMARTPBX_WS_TOKEN" not in plan.website_env_names


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
