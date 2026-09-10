from __future__ import annotations

import json
from pathlib import Path

import pytest

from smartpbx_agent_factory.operations import SecretLeakError, render_operations_artifacts
from smartpbx_agent_factory.resources import AllocationRegistry, derive_resources
from smartpbx_agent_factory.schema import parse_manifest


class FakeSecretProvider:
    def __init__(self, *, ciphertext: bytes = b"sops:\n  age: encrypted", error: Exception | None = None) -> None:
        self.ciphertext = ciphertext
        self.error = error
        self.generated = {}

    def validate(self) -> None:
        if self.error:
            raise self.error

    def generate(self, name: str, *, length: int = 32) -> str:
        return self.generated.setdefault(name, "fixture-only-generated-value")

    def encrypt_yaml(self, plaintext: bytes, *, path: Path) -> bytes:
        assert b"fixture-only-generated-value" in plaintext
        return self.ciphertext

    def audit_report(self):
        return {"generated_names": tuple(self.generated), "plaintext_paths": ()}


def fixture_manifest():
    raw = json.loads((Path(__file__).parent / "fixtures" / "acme-minimal.json").read_text(encoding="utf-8"))
    from smartpbx_agent_factory.catalogue import CapabilityCatalogue

    return parse_manifest(
        raw,
        approved_source_roots=(Path.cwd(),),
        catalogue=CapabilityCatalogue.load(Path(__file__).parent / "fixtures" / "approved-provider-catalogue.json"),
    )


def fixture_resources():
    return derive_resources(fixture_manifest(), AllocationRegistry())


def test_operations_output_contains_ciphertext_only(tmp_path: Path):
    provider = FakeSecretProvider()
    report = render_operations_artifacts(fixture_manifest(), fixture_resources(), provider, tmp_path)
    agent_dir = tmp_path / "agents/acme-inquiry"
    assert (agent_dir / "secrets.sops.yaml").read_bytes() == provider.ciphertext
    assert report.plaintext_paths == ()
    assert "fixture-only-generated-value" not in (agent_dir / "metadata.yaml").read_text(encoding="utf-8")


def test_missing_age_recipient_blocks_before_output(tmp_path: Path):
    from smartpbx_agent_factory.secrets import SecretError

    provider = FakeSecretProvider(error=SecretError("age recipient validation failed"))
    with pytest.raises(SecretError, match="age recipient"):
        render_operations_artifacts(fixture_manifest(), fixture_resources(), provider, tmp_path)
    assert not list(tmp_path.rglob("*.sops.yaml"))


def test_leak_scan_removes_only_this_generation_artifacts(tmp_path: Path):
    provider = FakeSecretProvider(ciphertext=b"sops:\n  token: fixture-only-generated-value")
    unrelated = tmp_path / "agents" / "other" / "keep.txt"
    unrelated.parent.mkdir(parents=True)
    unrelated.write_text("keep", encoding="utf-8")
    with pytest.raises(SecretLeakError):
        render_operations_artifacts(fixture_manifest(), fixture_resources(), provider, tmp_path)
    assert unrelated.read_text(encoding="utf-8") == "keep"
    assert not (tmp_path / "agents/acme-inquiry").exists()
