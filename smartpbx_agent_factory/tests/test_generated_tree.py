"""Generated-tree security and deployment-isolation contracts."""

from pathlib import Path

from test_render import FixtureReview, fixture_manifest, fixture_resources, fixture_templates
from smartpbx_agent_factory.render import render_backend


def render_fixture(tmp_path: Path):
    template_root = tmp_path / "synthetic"
    return render_backend(
        fixture_manifest(), FixtureReview(), fixture_resources(), tmp_path,
        template_allowlist=fixture_templates(template_root), template_root=template_root,
    )


def test_backend_has_two_separate_service_profiles_and_bidirectional_isolation(tmp_path):
    render_fixture(tmp_path)
    root = tmp_path / "SmartPBX Agents/acme-inquiry"
    compose = (root / "docker-compose.yml").read_text(encoding="utf-8")
    smartpbx = (root / "server.py").read_text(encoding="utf-8")
    website = (root / "website_demo.py").read_text(encoding="utf-8")
    assert "smartpbx-acme-inquiry-website" in compose
    assert "smartpbx-acme-inquiry" in compose
    assert "SMARTPBX_WS_TOKEN" in compose
    assert "TWILIO_AUTH_TOKEN" not in compose
    assert "/smartpbx/status" in smartpbx
    assert "/voice/demo-incoming" not in smartpbx
    assert "/voice/demo-incoming" in website
    assert "/api/voice-token" not in website
    assert "SMARTPBX_WS_TOKEN" not in website


def test_generated_contract_declares_preaccept_auth_and_pending_activation(tmp_path):
    render_fixture(tmp_path)
    root = tmp_path / "SmartPBX Agents/acme-inquiry"
    gateway = (root / "smartpbx_gateway.py").read_text(encoding="utf-8")
    sheet = (root / "CLIENT_CONNECT.md").read_text(encoding="utf-8")
    env_example = (root / ".env.example").read_text(encoding="utf-8")
    assert "compare_digest" in gateway
    assert "await websocket.close(code=1008, reason=\"unauthorized\")" in gateway
    assert "await websocket.accept()" in gateway
    assert "wss://smartpbx-acme-inquiry.taskforceai.tech/ws/v1/smartpbx/media" in sheet
    assert "X-Acme-Guide-SmartPBX-Token" in sheet
    assert "reachable_after_provisioning: false" in sheet
    assert "SMARTPBX_WS_TOKEN" in env_example
    assert "SMARTPBX_WS_TOKEN=" not in env_example


def test_generated_tree_contains_non_deploy_ci_gate_and_pending_activation_checklist(tmp_path):
    render_fixture(tmp_path)
    root = tmp_path / "SmartPBX Agents/acme-inquiry"
    fragment = (root / ".github-workflow-fragment.yml").read_text(encoding="utf-8")
    checklist = (root / "demo-routing-activation.md").read_text(encoding="utf-8")
    assert "smartpbx-generated-agent" in fragment
    assert "pytest" in fragment
    assert "deploy" not in fragment.lower()
    assert "release_allowed: false" in checklist
    assert "pending" in checklist
