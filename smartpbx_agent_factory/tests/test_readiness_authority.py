"""Specification coverage for coordinator-owned PR readiness evidence.

These tests intentionally exercise only local files and fake provider seams.
They never contact a provider, a network service, or a production system.
"""

from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

from smartpbx_agent_factory.prs import (
    GenerationOwnershipEvidence,
    GenerationWorktree,
    PRReadiness,
)
from smartpbx_agent_factory.readiness import ReadinessAuthority, ReadinessError
from smartpbx_agent_factory.state import GenerationState, Stage


def _state() -> GenerationState:
    state = GenerationState.start("gen-001", "a" * 64)
    state.transition(Stage.INPUT_COLLECTED)
    state.transition(Stage.SECRETS_RESOLVED)
    state.transition(Stage.KNOWLEDGE_REVIEW_REQUIRED)
    state.record_knowledge_review_digest("b" * 64)
    state.approve_knowledge("b" * 64)
    state.record_plan_digest("c" * 64)
    state.approve_plan("c" * 64)
    for name, digest in _stage_digests().items():
        state.record_stage_digest(name, digest)
    state.transition(Stage.VERIFIED)
    return state


def _readiness() -> PRReadiness:
    return PRReadiness(
        readiness_report_path=Path(".smartpbx-generations/gen-001/readiness.json"),
        readiness_verified=True,
        secret_scan_passed=True,
        ci_registered=True,
        provenance_source_revision="a" * 40,
        template_revision="b" * 40,
        artifact_digests={"backend": "c" * 64, "operations": "d" * 64, "website": "e" * 64},
        review_label="Acme review",
        wss_url="wss://smartpbx-acme.taskforceai.tech/ws/v1/smartpbx/media",
        expected_wss_hostname="smartpbx-acme.taskforceai.tech",
        allowed_wss_paths=("/ws/v1/smartpbx/media",),
        readiness_digest="f" * 64,
        secret_scan_digest="1" * 64,
        ci_registration_digest="2" * 64,
        provenance_digest="3" * 64,
        worktree_ownership_digest="4" * 64,
    )


def _stage_digests() -> dict[str, str]:
    readiness = _readiness()
    return {
        "readiness": readiness.readiness_digest,
        "secret_scan": readiness.secret_scan_digest,
        "ci_registration": readiness.ci_registration_digest,
        "provenance": readiness.provenance_digest,
        "worktree_ownership": readiness.worktree_ownership_digest,
        "artifact_backend": readiness.artifact_digests["backend"],
        "artifact_operations": readiness.artifact_digests["operations"],
        "artifact_website": readiness.artifact_digests["website"],
    }


def _worktrees() -> tuple[GenerationWorktree, ...]:
    root = Path("/tmp/smartpbx-readiness-spec/gen-001")
    return tuple(
        GenerationWorktree(
            role=role,
            repository=f"taskforce/{role}",
            branch=f"{role}/gen-001",
            branch_sha=letter * 40,
            path=root / role,
            clean=True,
            ownership=GenerationOwnershipEvidence("gen-001", root, f"{role}-handle", "4" * 64),
        )
        for role, letter in (("backend", "f"), ("operations", "1"), ("website", "2"))
    )


def test_authority_persists_canonical_private_generation_contained_record(tmp_path: Path) -> None:
    authority = ReadinessAuthority(tmp_path / ".smartpbx-generations")
    record_path = authority.persist(_state(), readiness=_readiness(), worktrees=_worktrees())

    assert record_path == tmp_path / ".smartpbx-generations" / "gen-001" / "readiness.json"
    assert record_path.stat().st_mode & 0o777 == 0o600
    document = json.loads(record_path.read_text(encoding="utf-8"))
    assert document["generation_id"] == "gen-001"
    assert document["manifest_digest"] == "a" * 64
    assert set(document["artifact_digests"]) == {"backend", "operations", "website"}
    assert set(document["worktrees"]) == {"backend", "operations", "website"}
    assert len(document["record_digest"]) == 64


def test_authority_rejects_tampered_or_wrong_generation_record_before_provider_use(tmp_path: Path) -> None:
    authority = ReadinessAuthority(tmp_path / ".smartpbx-generations")
    record_path = authority.persist(_state(), readiness=_readiness(), worktrees=_worktrees())
    document = json.loads(record_path.read_text(encoding="utf-8"))
    document["artifact_digests"]["backend"] = "0" * 64
    record_path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(ReadinessError, match="digest"):
        authority.load(_state(), worktrees=_worktrees())


def test_authority_rejects_symlinked_record_or_parent(tmp_path: Path) -> None:
    root = tmp_path / ".smartpbx-generations"
    authority = ReadinessAuthority(root)
    authority.persist(_state(), readiness=_readiness(), worktrees=_worktrees())
    record_path = authority.record_path("gen-001")
    replacement = tmp_path / "replacement.json"
    replacement.write_text("{}", encoding="utf-8")
    record_path.unlink()
    record_path.symlink_to(replacement)

    with pytest.raises(ReadinessError, match="symlink"):
        authority.load(_state(), worktrees=_worktrees())


def test_authority_rejects_record_if_role_worktree_binding_changes(tmp_path: Path) -> None:
    authority = ReadinessAuthority(tmp_path / ".smartpbx-generations")
    authority.persist(_state(), readiness=_readiness(), worktrees=_worktrees())
    changed = list(_worktrees())
    changed[0] = replace(changed[0], branch_sha="0" * 40)

    with pytest.raises(ReadinessError, match="worktree"):
        authority.load(_state(), worktrees=tuple(changed))


def test_authority_requires_private_record_permissions(tmp_path: Path) -> None:
    authority = ReadinessAuthority(tmp_path / ".smartpbx-generations")
    record_path = authority.persist(_state(), readiness=_readiness(), worktrees=_worktrees())
    os.chmod(record_path, 0o644)

    with pytest.raises(ReadinessError, match="0600"):
        authority.load(_state(), worktrees=_worktrees())
