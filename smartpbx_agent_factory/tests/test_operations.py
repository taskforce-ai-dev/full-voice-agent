from __future__ import annotations

import json
from pathlib import Path

import pytest

from smartpbx_agent_factory.catalogue import CapabilityCatalogue
from smartpbx_agent_factory.operations import SecretLeakError, render_operations_artifacts as _render_operations_artifacts
from smartpbx_agent_factory.resources import AllocationRegistry, derive_resources
from smartpbx_agent_factory.schema import parse_manifest
from smartpbx_agent_factory.secrets import SecretError, derive_secret_plan
from _owned_worktree_fixture import fixture_owned_worktree


def _render_operations_fixture(manifest, resources, sealed_ciphertext: bytes, output_dir: Path):
    """Private compatibility seam: synthetic handles only, never a checkout mutation."""
    manager, worktree = fixture_owned_worktree(output_dir)
    return _render_operations_artifacts(
        manifest,
        resources,
        worktree=worktree,
        worktree_manager=manager,
        secret_plan=fixture_secret_plan(),
        sealed_ciphertext=sealed_ciphertext,
    )


render_operations_artifacts = _render_operations_fixture


def fixture_manifest():
    raw = json.loads((Path(__file__).parent / "fixtures" / "acme-minimal.json").read_text(encoding="utf-8"))
    return parse_manifest(
        raw,
        approved_source_roots=(Path.cwd(),),
        catalogue=CapabilityCatalogue.load(Path(__file__).parent / "fixtures" / "approved-provider-catalogue.json"),
    )


def fixture_resources():
    return derive_resources(fixture_manifest(), AllocationRegistry())


def fixture_secret_plan():
    manifest = fixture_manifest()
    return derive_secret_plan(
        manifest,
        derive_resources(manifest, AllocationRegistry()),
        CapabilityCatalogue.load(Path(__file__).parent / "fixtures" / "approved-provider-catalogue.json"),
    )


def test_operations_output_contains_ciphertext_only(tmp_path: Path):
    ciphertext = b"sops:\n  age: encrypted"
    report = render_operations_artifacts(fixture_manifest(), fixture_resources(), ciphertext, tmp_path)
    agent_dir = tmp_path / "agents/acme-inquiry"
    assert (agent_dir / "secrets.sops.yaml").read_bytes() == ciphertext
    assert report.plaintext_paths == ()
    assert "fixture-only-generated-value" not in (agent_dir / "metadata.yaml").read_text(encoding="utf-8")


def test_public_operations_renderer_rejects_an_unowned_handle_before_writing(tmp_path: Path):
    manager, worktree = fixture_owned_worktree(tmp_path)
    manager._handles.clear()
    with pytest.raises(Exception, match="manager-owned"):
        _render_operations_artifacts(
            fixture_manifest(), fixture_resources(), worktree=worktree, worktree_manager=manager,
            secret_plan=fixture_secret_plan(), sealed_ciphertext=b"sops:\n  age: encrypted",
        )
    assert not (tmp_path / "agents").exists()


def test_unsealed_ciphertext_blocks_before_output(tmp_path: Path):
    with pytest.raises(SecretError, match="sealed SOPS"):
        render_operations_artifacts(fixture_manifest(), fixture_resources(), b"plaintext", tmp_path)
    assert not list(tmp_path.rglob("*.sops.yaml"))


def test_leak_scan_removes_only_this_generation_artifacts(tmp_path: Path):
    unrelated = tmp_path / "agents" / "other" / "keep.txt"
    unrelated.parent.mkdir(parents=True)
    unrelated.write_text("keep", encoding="utf-8")
    with pytest.raises(SecretLeakError):
        render_operations_artifacts(
            fixture_manifest(), fixture_resources(), b"sops:\n  token: fixture-only-generated-value", tmp_path
        )
    assert unrelated.read_text(encoding="utf-8") == "keep"
    assert not (tmp_path / "agents/acme-inquiry").exists()


def test_operations_rejects_symlinked_agents_component_before_any_write(tmp_path: Path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "agents").symlink_to(outside, target_is_directory=True)
    with pytest.raises(Exception, match="symlink"):
        render_operations_artifacts(fixture_manifest(), fixture_resources(), b"sops:\n  age: encrypted", tmp_path)
    assert not list(outside.iterdir())
