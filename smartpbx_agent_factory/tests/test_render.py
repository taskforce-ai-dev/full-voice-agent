"""Renderer contracts use explicit synthetic templates, never deployed provenance."""

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from smartpbx_agent_factory.catalogue import CapabilityCatalogue
from smartpbx_agent_factory.provenance import TemplateAllowlist, TemplateFile
from smartpbx_agent_factory.render import IdentityLeakError, TemplateUnavailableError, render_backend
from smartpbx_agent_factory.resources import AllocationRegistry, derive_resources
from smartpbx_agent_factory.schema import parse_manifest


FIXTURE = Path(__file__).parent / "fixtures" / "acme-minimal.json"
CATALOGUE = Path(__file__).parent / "fixtures" / "approved-provider-catalogue.json"


@dataclass(frozen=True)
class FixtureReview:
    digest: str = "a" * 64
    approved: bool = True
    facts: tuple[str, ...] = ("Acme provides approved information.",)


def fixture_manifest():
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return parse_manifest(raw, approved_source_roots=(Path.cwd(),), catalogue=CapabilityCatalogue.load(CATALOGUE))


def fixture_resources():
    return derive_resources(fixture_manifest(), AllocationRegistry())


def fixture_templates(root: Path) -> TemplateAllowlist:
    """A deliberately synthetic, hash-pinned test seam; never production input."""
    templates = {
        "runtime/server.py.tmpl": "ROUTES = ('/smartpbx/status', '/ws/v1/smartpbx/media')\n",
        "runtime/smartpbx_gateway.py.tmpl": "# synthetic gateway marker\n",
        "runtime/smartpbx_protocol.py.tmpl": "PROTOCOL = 'smartpbx-ai-provider-v07'\n",
        "runtime/smartpbx_session.py.tmpl": "# synthetic session marker\n",
        "runtime/smartpbx_transport.py.tmpl": "# synthetic transport marker\n",
        "runtime/smartpbx_diagnostics.py.tmpl": "# synthetic diagnostics marker\n",
        "runtime/tools.py.tmpl": "TOOL_REGISTRY = {}\n",
        "runtime/website_demo.py.tmpl": "ROUTES = ('/voice/demo-incoming',)\n",
        "infrastructure/Dockerfile.tmpl": "FROM python:3.11-slim\n",
        "infrastructure/docker-compose.yml.tmpl": "services: {}\n",
        "infrastructure/nginx-smartpbx.conf.tmpl": "location /smartpbx/status {}\n",
        "infrastructure/nginx-website.conf.tmpl": "location /voice/demo-incoming {}\n",
        "infrastructure/env.example.tmpl": "SMARTPBX_WS_TOKEN\n",
        "infrastructure/README.md.tmpl": "Synthetic fixture runtime.\n",
        "infrastructure/CLIENT_CONNECT.md.tmpl": "Synthetic fixture client connect.\n",
        "infrastructure/demo-routing-activation.md.tmpl": "Synthetic fixture pending checklist.\n",
    }
    files = {}
    for relative, text in templates.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        files[f"synthetic/{relative}"] = TemplateFile(relative, "sha256:" + hashlib.sha256(text.encode()).hexdigest())
    return TemplateAllowlist(
        template_version="synthetic-test-v1",
        source_revision="a" * 40,
        image_digest="sha256:" + "b" * 64,
        oci_revision="a" * 40,
        protocol_version="smartpbx-ai-provider-v07",
        environment_schema_version="synthetic-test-v1",
        files=files,
    )


def test_real_template_path_fails_closed_until_approved_allowlist_exists(tmp_path):
    with pytest.raises(TemplateUnavailableError, match="TEMPLATE_ALLOWLIST_UNAVAILABLE"):
        render_backend(fixture_manifest(), FixtureReview(), fixture_resources(), tmp_path)


def test_inquiry_only_render_has_no_business_tools(tmp_path):
    report = render_backend(
        fixture_manifest(), FixtureReview(), fixture_resources(), tmp_path,
        template_allowlist=fixture_templates(tmp_path / "synthetic"), template_root=tmp_path / "synthetic",
    )
    tools = (tmp_path / "SmartPBX Agents/acme-inquiry/tools.py").read_text(encoding="utf-8")
    assert "create_booking" not in tools
    assert "transfer_to_human" not in tools
    assert "hangup_call" not in tools
    assert report.enabled_capabilities == ()


def test_renderer_rejects_identity_and_secret_leaks_from_review(tmp_path):
    with pytest.raises(IdentityLeakError, match="identity leak"):
        render_backend(
            fixture_manifest(), FixtureReview(facts=("Hatton Hills is a hotel",)), fixture_resources(), tmp_path,
            template_allowlist=fixture_templates(tmp_path / "synthetic"), template_root=tmp_path / "synthetic",
        )


def test_renderer_is_deterministic_for_the_same_approved_inputs(tmp_path):
    first_root, second_root = tmp_path / "first", tmp_path / "second"
    first_template, second_template = fixture_templates(first_root / "synthetic"), fixture_templates(second_root / "synthetic")
    first = render_backend(fixture_manifest(), FixtureReview(), fixture_resources(), first_root, template_allowlist=first_template, template_root=first_root / "synthetic")
    second = render_backend(fixture_manifest(), FixtureReview(), fixture_resources(), second_root, template_allowlist=second_template, template_root=second_root / "synthetic")
    assert first.artifact_digest == second.artifact_digest
    assert first.files == second.files
