"""Static contract for the isolated, fail-closed website demo candidate."""

import ast
import json
from pathlib import Path


ROOT = Path(__file__).parents[1] / "template_v1"
INFRASTRUCTURE = ROOT / "infrastructure"
RUNTIME = ROOT / "runtime"


def test_website_demo_has_a_separate_loopback_profile_without_smartpbx_credentials():
    compose = (INFRASTRUCTURE / "docker-compose.yml.tmpl").read_text(encoding="utf-8")
    website = compose[compose.index("{{website_service}}:"):]
    assert 'profiles: ["website-demo"]' in website
    assert 'container_name: {{website_service}}' in website
    assert '"127.0.0.1:{{website_port}}:8081"' in website
    assert 'command: ["uvicorn", "website_demo:app"' in website
    for forbidden in ("SMARTPBX_WS_TOKEN", "SMARTPBX_ACCOUNT_ID", "SMARTPBX_AUTH_HEADER_NAME"):
        assert forbidden not in website
    for required in (
        "TWILIO_ACCOUNT_SID",
        "TWILIO_API_KEY_SID",
        "TWILIO_API_KEY_SECRET",
        "TWILIO_AUTH_TOKEN",
        "TWILIO_TWIML_APP_SID",
    ):
        assert required in website


def test_website_demo_uses_the_shared_issuer_response_shape_and_bounded_relay_transport():
    source = (RUNTIME / "website_demo.py.tmpl").read_text(encoding="utf-8")
    module = ast.parse(source)
    assert '@app.get("/api/voice-token")' in source
    assert 'serialized_token = token.to_jwt()' in source
    assert 'return {"token": serialized_token, "identity": identity}' in source
    assert '@app.post("/voice/demo-incoming")' in source
    assert '@app.websocket("/ws/v1/website-demo/conversation")' in source
    assert "RequestValidator" in source
    assert "WebsiteDemoConfigurationError" in source
    assert "IssuedBrowserIdentities" in source
    assert "SessionTickets" in source
    assert "WEBSITE_DEMO_MAX_SESSIONS" in source
    assert "ttl=300" in source
    constants = {
        target.id: value.value
        for node in module.body
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(target := node.targets[0], ast.Name)
        and isinstance(value := node.value, ast.Constant)
    }
    assert constants["_MAX_RELAY_MESSAGE_CHARS"] == 8_192
    assert constants["_MAX_PROMPT_CHARS"] == 4_000
    imported_roots: set[str] = set()
    for node in ast.walk(module):
        if isinstance(node, ast.Import):
            imported_roots.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_roots.add(node.module.split(".", 1)[0])
    assert imported_roots.isdisjoint({"handover", "post_call", "booking_api"})
    assert not any(
        isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "create_booking"
        for node in ast.walk(module)
    )
    assert any(
        isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "deque"
        and any(keyword.arg == "maxlen" and isinstance(keyword.value, ast.Constant) and keyword.value.value == 12 for keyword in node.keywords)
        for node in ast.walk(module)
    )
    assert any(
        isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "consume"
        and isinstance(node.func.value, ast.Name) and node.func.value.id == "tickets"
        and {keyword.arg for keyword in node.keywords} >= {"agent", "language"}
        for node in ast.walk(module)
    )


def test_candidate_records_the_website_demo_boundary_and_its_source_evidence():
    candidate = json.loads((ROOT / "runtime_infrastructure_candidate.json").read_text(encoding="utf-8"))
    assert candidate["status"] == "partial-candidate-not-approved-for-rendering"
    assert "website_demo.py" in candidate["runtime_outputs"]
    assert any(item["template_path"] == "runtime/website_demo.py.tmpl" for item in candidate["artifacts"])
    provenance = json.loads((ROOT / "candidate_runtime_provenance.json").read_text(encoding="utf-8"))
    assert set(provenance["website_transport_source_revisions"]) == {
        "full-voice-agent",
        "Taskforce_AI_Website",
    }
    website_component = next(
        item for item in provenance["components"]
        if item["template_path"] == "runtime/website_demo.py.tmpl"
    )
    assert website_component["source_path"] == "Flico Agent/server.py"
    assert website_component["source_evidence"]
