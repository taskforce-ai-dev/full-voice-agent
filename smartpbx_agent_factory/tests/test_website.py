import json
from pathlib import Path

import pytest

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
