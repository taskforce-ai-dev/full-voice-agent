import json
import shutil
import subprocess
from pathlib import Path

import pytest

import smartpbx_agent_factory.website as website
from smartpbx_agent_factory.catalogue import CapabilityCatalogue
from smartpbx_agent_factory.resources import AllocationRegistry, derive_resources
from smartpbx_agent_factory.schema import parse_manifest
from smartpbx_agent_factory.website import render_website_artifacts


FIXTURE = Path(__file__).parent / "fixtures" / "acme-minimal.json"
CATALOGUE = Path(__file__).parent / "fixtures" / "approved-provider-catalogue.json"


def fixture_manifest(*, profile: str = "demo"):
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    raw["profile"] = profile
    return parse_manifest(
        raw,
        approved_source_roots=(Path.cwd(),),
        catalogue=CapabilityCatalogue.load(CATALOGUE),
    )


def fixture_resources():
    return derive_resources(fixture_manifest(), AllocationRegistry())


def named_manifest(slug: str, **changes):
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    raw.update({"slug": slug, **changes})
    return parse_manifest(
        raw,
        approved_source_roots=(Path.cwd(),),
        catalogue=CapabilityCatalogue.load(CATALOGUE),
    )


def write_website_target(path: Path) -> None:
    page = path / "components" / "pages" / "BookDemo.tsx"
    page.parent.mkdir(parents=True)
    page.write_text(
        "import { Device } from '@twilio/voice-sdk';\n"
        "import { contactPageSchema } from '../../lib/schema';\n\n"
        "const TOKEN_URL = 'https://hattonhills.taskforceai.tech/api/voice-token';\n\n"
        "type Lang = 'en' | 'ar' | 'ru' | 'si';\n"
        "interface Agent {\n"
        "  id: string; brand: string; agentName: string; role: string; location: string;\n"
        "  description: string; images: string[]; trainedOn: string[]; langs: Lang[];\n"
        "  callLabel: string; askHint: string; steps: Array<{ bold: string; rest: string }>;\n"
        "}\n\n"
        "const AGENTS: Agent[] = [\n"
        "  { id: 'hatton', brand: 'Hatton Hills', agentName: 'Tanya', role: 'Reservation Agent', "
        "location: 'Hatton', description: 'Existing demo', images: [], trainedOn: [], langs: ['en'], "
        "callLabel: 'Call Hatton Hills', askHint: 'rooms', steps: [] },\n"
        "];\n\n"
        "const langMeta = (value: Lang) => value;\n\n"
        "async function startDemo(agent: Agent, lang: Lang, token: string) {\n"
        "  await fetch(`${TOKEN_URL}?agent=${encodeURIComponent(agent.id)}`);\n"
        "  const device = new Device(token);\n"
        "  return device.connect({ params: { agent: agent.id, lang } });\n"
        "}\n",
        encoding="utf-8",
    )
    (path / "package.json").write_text(
        json.dumps({"name": "fixture-site", "scripts": {"build": "vite build"}}, indent=2) + "\n",
        encoding="utf-8",
    )


def test_website_artifact_has_public_fields_only(tmp_path):
    write_website_target(tmp_path)
    render_website_artifacts(
        fixture_manifest(),
        fixture_resources(),
        backend_artifact_digest="a" * 64,
        backend_branch_sha="b" * 40,
        output_dir=tmp_path,
    )
    source = (tmp_path / "data" / "smartpbx-agents.generated.mjs").read_text(encoding="utf-8")
    assert "wss://" not in source
    assert "SMARTPBX_WS_TOKEN" not in source
    assert "api-key" not in source
    assert "acme-inquiry" in source


def test_website_artifact_binds_only_immutable_backend_dependency(tmp_path):
    write_website_target(tmp_path)
    result = render_website_artifacts(
        fixture_manifest(),
        fixture_resources(),
        backend_artifact_digest="a" * 64,
        backend_branch_sha="b" * 40,
        output_dir=tmp_path,
    )
    source = (tmp_path / "data" / "smartpbx-agents.generated.mjs").read_text(encoding="utf-8")
    assert result.dependency.backend_artifact_digest == "a" * 64
    assert result.dependency.backend_branch_sha == "b" * 40
    assert not hasattr(result.dependency, "backend_pr")
    assert result.dependency.release_order == ("backend", "website")
    assert 'id: "acme-inquiry"' in source
    assert "tokenUrl" not in source
    assert "backendHost" not in source
    assert "DEMO_AGENT_HOSTS" not in source
    assert "github.com/example/pr" not in source


def test_pending_card_is_not_rendered_but_active_card_is(tmp_path):
    write_website_target(tmp_path)
    render_website_artifacts(
        fixture_manifest(),
        fixture_resources(),
        backend_artifact_digest="a" * 64,
        backend_branch_sha="b" * 40,
        output_dir=tmp_path,
    )
    source = (tmp_path / "data" / "smartpbx-agents.generated.mjs").read_text(encoding="utf-8")
    book_demo = (tmp_path / "components" / "pages" / "BookDemo.tsx").read_text(encoding="utf-8")
    assert 'releaseState: "pending"' in source
    assert 'SMARTPBX_AGENT_CARDS.filter((card) => card.releaseState === "active")' in book_demo
    assert "TOKEN_URL = 'https://hattonhills.taskforceai.tech/api/voice-token'" in book_demo
    assert "encodeURIComponent(agent.id)" in book_demo
    assert "params: { agent: agent.id, lang }" in book_demo
    cards = (
        {"id": "pending-agent", "releaseState": "pending"},
        {"id": "active-agent", "releaseState": "active"},
    )
    rendered_cards = [card for card in cards if card["releaseState"] == "active"]
    assert [card["id"] for card in rendered_cards] == ["active-agent"]


def test_renderer_writes_validator_and_package_contract_in_isolated_target(tmp_path):
    write_website_target(tmp_path)
    result = render_website_artifacts(
        fixture_manifest(),
        fixture_resources(),
        backend_artifact_digest="a" * 64,
        backend_branch_sha="b" * 40,
        output_dir=tmp_path,
    )
    validator = (tmp_path / "scripts" / "validate-smartpbx-card.mjs").read_text(encoding="utf-8")
    package = json.loads((tmp_path / "package.json").read_text(encoding="utf-8"))
    assert package["scripts"]["test:smartpbx"] == "node scripts/validate-smartpbx-card.mjs"
    assert "pending-agent" in validator
    assert "active-agent" in validator
    assert "callableCards" in validator
    assert "releaseState" in validator
    assert "BookDemo.tsx" in validator
    assert result.output_paths == (
        Path("data/smartpbx-agents.generated.mjs"),
        Path("scripts/validate-smartpbx-card.mjs"),
        Path("components/pages/BookDemo.tsx"),
        Path("package.json"),
    )


def test_generated_module_is_deterministic_and_rejects_invalid_digests(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    write_website_target(first)
    write_website_target(second)
    args = {
        "backend_artifact_digest": "a" * 64,
        "backend_branch_sha": "b" * 40,
    }
    render_website_artifacts(fixture_manifest(), fixture_resources(), output_dir=first, **args)
    render_website_artifacts(fixture_manifest(), fixture_resources(), output_dir=second, **args)
    assert (first / "data/smartpbx-agents.generated.mjs").read_bytes() == (
        second / "data/smartpbx-agents.generated.mjs"
    ).read_bytes()
    with pytest.raises(ValueError, match="backend_artifact_digest"):
        render_website_artifacts(
            fixture_manifest(), fixture_resources(), output_dir=first,
            backend_artifact_digest="not-a-digest", backend_branch_sha="b" * 40,
        )


def test_repeated_generations_preserve_existing_cards_and_sort_by_id(tmp_path):
    write_website_target(tmp_path)
    render_website_artifacts(
        fixture_manifest(),
        fixture_resources(),
        backend_artifact_digest="a" * 64,
        backend_branch_sha="b" * 40,
        output_dir=tmp_path,
    )
    beta = named_manifest(
        "beta-inquiry",
        display_name="Beta Inquiry",
        public_name="Beta Inquiry",
        agent_name="Bea",
    )
    render_website_artifacts(
        beta,
        derive_resources(beta, AllocationRegistry()),
        backend_artifact_digest="c" * 64,
        backend_branch_sha="d" * 40,
        output_dir=tmp_path,
    )
    source = (tmp_path / "data" / "smartpbx-agents.generated.mjs").read_text(encoding="utf-8")
    assert 'id: "acme-inquiry"' in source
    assert 'id: "beta-inquiry"' in source
    assert source.index('id: "acme-inquiry"') < source.index('id: "beta-inquiry"')
    assert source.count("backendArtifactDigest") == 2
    assert "eval(" not in source


def test_conflicting_existing_card_fails_without_mutating_any_artifact(tmp_path):
    write_website_target(tmp_path)
    render_website_artifacts(
        fixture_manifest(),
        fixture_resources(),
        backend_artifact_digest="a" * 64,
        backend_branch_sha="b" * 40,
        output_dir=tmp_path,
    )
    paths = (
        tmp_path / "data" / "smartpbx-agents.generated.mjs",
        tmp_path / "scripts" / "validate-smartpbx-card.mjs",
        tmp_path / "components" / "pages" / "BookDemo.tsx",
        tmp_path / "package.json",
    )
    before = {path: path.read_bytes() for path in paths}
    with pytest.raises(ValueError, match="conflicting generated website card"):
        render_website_artifacts(
            fixture_manifest(),
            fixture_resources(),
            backend_artifact_digest="c" * 64,
            backend_branch_sha="d" * 40,
            output_dir=tmp_path,
        )
    assert {path: path.read_bytes() for path in paths} == before


@pytest.mark.parametrize(
    "changes",
    (
        {"public_name": "Acme sk-abcdefghijklmnopqrstuvwxyz0123456789"},
        {"agent_name": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJhY21lIn0.signature"},
        {"purpose": "-----BEGIN " + "PRIVATE KEY-----"},
        {"allowed_topics": ["api_key=super-secret-value"]},
    ),
)
def test_manifest_derived_credential_like_values_are_rejected_before_output(tmp_path, changes):
    write_website_target(tmp_path)
    manifest = named_manifest("acme-inquiry", **changes)
    with pytest.raises(ValueError, match="credential-like|non-public"):
        render_website_artifacts(
            manifest,
            derive_resources(manifest, AllocationRegistry()),
            backend_artifact_digest="a" * 64,
            backend_branch_sha="b" * 40,
            output_dir=tmp_path,
        )
    assert not (tmp_path / "data" / "smartpbx-agents.generated.mjs").exists()
    assert not (tmp_path / "scripts" / "validate-smartpbx-card.mjs").exists()


def test_book_demo_validation_failure_leaves_no_partial_output(tmp_path):
    write_website_target(tmp_path)
    page = tmp_path / "components" / "pages" / "BookDemo.tsx"
    page.write_text(page.read_text(encoding="utf-8").replace("hattonhills", "invalid"), encoding="utf-8")
    package = tmp_path / "package.json"
    before_page = page.read_bytes()
    before_package = package.read_bytes()
    with pytest.raises(ValueError, match="shared HattonHills token issuer"):
        render_website_artifacts(
            fixture_manifest(),
            fixture_resources(),
            backend_artifact_digest="a" * 64,
            backend_branch_sha="b" * 40,
            output_dir=tmp_path,
        )
    assert page.read_bytes() == before_page
    assert package.read_bytes() == before_package
    assert not (tmp_path / "data").exists()
    assert not (tmp_path / "scripts").exists()


def test_atomic_write_failure_restores_every_website_artifact(tmp_path, monkeypatch):
    write_website_target(tmp_path)
    paths = (
        tmp_path / "data" / "smartpbx-agents.generated.mjs",
        tmp_path / "scripts" / "validate-smartpbx-card.mjs",
        tmp_path / "components" / "pages" / "BookDemo.tsx",
        tmp_path / "package.json",
    )
    before = {path: path.read_bytes() if path.exists() else None for path in paths}
    original_replace = website.os.replace
    page = tmp_path / "components" / "pages" / "BookDemo.tsx"

    def fail_at_page(source, destination):
        if Path(destination) == page:
            raise OSError("simulated BookDemo replace failure")
        return original_replace(source, destination)

    monkeypatch.setattr(website.os, "replace", fail_at_page)
    with pytest.raises(OSError, match="simulated BookDemo"):
        render_website_artifacts(
            fixture_manifest(),
            fixture_resources(),
            backend_artifact_digest="a" * 64,
            backend_branch_sha="b" * 40,
            output_dir=tmp_path,
        )
    assert {path: path.read_bytes() if path.exists() else None for path in paths} == before
    assert not (tmp_path / "data").exists()
    assert not (tmp_path / "scripts").exists()


def test_generated_validator_has_one_reserved_declaration_and_parses_with_node(tmp_path):
    write_website_target(tmp_path)
    render_website_artifacts(
        fixture_manifest(),
        fixture_resources(),
        backend_artifact_digest="a" * 64,
        backend_branch_sha="b" * 40,
        output_dir=tmp_path,
    )
    validator = tmp_path / "scripts" / "validate-smartpbx-card.mjs"
    source = validator.read_text(encoding="utf-8")
    assert source.count("const RESERVED =") == 1
    assert "export function validateCards" in source
    node = shutil.which("node")
    if node is not None:
        result = subprocess.run([node, "--check", str(validator)], capture_output=True, text=True, check=False)
        assert result.returncode == 0, result.stderr


def test_manifest_derived_bearer_authorization_value_is_rejected_before_output(tmp_path):
    write_website_target(tmp_path)
    manifest = named_manifest("acme-inquiry", purpose="Authorization: Bearer opaque-session-value")
    with pytest.raises(ValueError, match="credential-like"):
        render_website_artifacts(
            manifest,
            derive_resources(manifest, AllocationRegistry()),
            backend_artifact_digest="a" * 64,
            backend_branch_sha="b" * 40,
            output_dir=tmp_path,
        )
    assert not (tmp_path / "data" / "smartpbx-agents.generated.mjs").exists()


def write_interrupted_transaction(root: Path, originals: dict[str, bytes | None], records: list[dict[str, object]] | None = None):
    stage = root / ".smartpbx-agent-factory-website-txn" / "interrupted"
    stage.mkdir(parents=True)
    if records is None:
        records = []
        for index, (target, original) in enumerate(originals.items()):
            record = {"target": target, "existed": original is not None, "backup": None}
            if original is not None:
                backup = f"backup-{index}.bin"
                (stage / backup).write_bytes(original)
                record["backup"] = backup
            records.append(record)
    marker = {
        "version": 1,
        "stage": "interrupted",
        "files": records,
    }
    (root / ".smartpbx-agent-factory-website-transaction.json").write_text(
        json.dumps(marker), encoding="utf-8"
    )


def test_next_render_recovers_an_interrupted_owned_transaction_before_rejecting_new_input(tmp_path):
    write_website_target(tmp_path)
    paths = {
        "data/smartpbx-agents.generated.mjs": None,
        "scripts/validate-smartpbx-card.mjs": None,
        "components/pages/BookDemo.tsx": (tmp_path / "components/pages/BookDemo.tsx").read_bytes(),
        "package.json": (tmp_path / "package.json").read_bytes(),
    }
    (tmp_path / "data").mkdir()
    (tmp_path / "data/smartpbx-agents.generated.mjs").write_text("partial generated data", encoding="utf-8")
    page = tmp_path / "components/pages/BookDemo.tsx"
    page.write_text("partial BookDemo", encoding="utf-8")
    write_interrupted_transaction(tmp_path, paths)
    invalid = named_manifest("acme-inquiry", purpose="Authorization: Bearer opaque-session-value")
    with pytest.raises(ValueError, match="credential-like"):
        render_website_artifacts(
            invalid,
            derive_resources(invalid, AllocationRegistry()),
            backend_artifact_digest="a" * 64,
            backend_branch_sha="b" * 40,
            output_dir=tmp_path,
        )
    assert not (tmp_path / "data/smartpbx-agents.generated.mjs").exists()
    assert page.read_bytes() == paths["components/pages/BookDemo.tsx"]
    assert (tmp_path / "package.json").read_bytes() == paths["package.json"]
    assert not (tmp_path / ".smartpbx-agent-factory-website-transaction.json").exists()
    assert not (tmp_path / ".smartpbx-agent-factory-website-txn" / "interrupted").exists()


def test_malformed_recovery_marker_cannot_overwrite_outside_owned_targets(tmp_path):
    write_website_target(tmp_path)
    page = tmp_path / "components/pages/BookDemo.tsx"
    before = page.read_bytes()
    write_interrupted_transaction(
        tmp_path,
        {},
        records=[{"target": "../outside", "existed": False, "backup": None}],
    )
    with pytest.raises(ValueError, match="transaction marker"):
        render_website_artifacts(
            fixture_manifest(),
            fixture_resources(),
            backend_artifact_digest="a" * 64,
            backend_branch_sha="b" * 40,
            output_dir=tmp_path,
        )
    assert page.read_bytes() == before
    assert not (tmp_path / "data").exists()
