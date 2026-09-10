"""Generated-tree security and deployment-isolation contracts."""

from pathlib import Path

from test_render import fixture_manifest, fixture_resources, fixture_review, fixture_state, fixture_templates
from smartpbx_agent_factory.render import render_backend
from _owned_worktree_fixture import fixture_owned_worktree


def render_fixture(tmp_path: Path):
    template_root = tmp_path / "synthetic"
    manager, worktree = fixture_owned_worktree(tmp_path)
    return render_backend(
        fixture_manifest(), fixture_review(), fixture_resources(), worktree,
        worktree_manager=manager, state=fixture_state(), template_allowlist=fixture_templates(template_root), template_root=template_root,
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
    assert "TWILIO_AUTH_TOKEN" in compose
    assert 'profiles: ["smartpbx"]' in compose
    assert 'profiles: ["website-demo"]' in compose
    assert '127.0.0.1:' in compose
    assert "/smartpbx/status" in smartpbx
    assert "/voice/demo-incoming" not in smartpbx
    assert "/voice/demo-incoming" in website
    assert "/api/voice-token" in website
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


def test_root_generated_agent_workflow_has_exact_safe_triggers_and_stable_check_name():
    workflow = Path(".github/workflows/smartpbx-generated-agents.yml").read_text(encoding="utf-8")
    assert 'paths: ["SmartPBX Agents/**", "smartpbx_agent_factory/**", ".github/workflows/smartpbx-generated-agents.yml"]' in workflow
    assert "branches: [main]" in workflow
    assert "smartpbx-generated-agent:" in workflow
    assert "python -m pytest smartpbx_agent_factory/tests" in workflow
    assert 'if [ -d "SmartPBX Agents" ]; then' in workflow
    assert 'find "SmartPBX Agents" -mindepth 1 -maxdepth 1 -type d -print0' in workflow
    assert "while IFS= read -r -d '' agent_dir; do" in workflow
    assert "workflow_call" not in workflow
    assert "gh workflow run" not in workflow
    assert "docker push" not in workflow
    assert "test -s /tmp/smartpbx-agent-dirs" not in workflow
