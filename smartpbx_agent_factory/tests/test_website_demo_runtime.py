"""Static contract for the isolated, fail-closed website demo candidate."""

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
        "WEBSITE_DEMO_TWILIO_ACCOUNT_SID",
        "WEBSITE_DEMO_TWILIO_API_KEY_SID",
        "WEBSITE_DEMO_TWILIO_API_KEY_SECRET",
        "WEBSITE_DEMO_TWILIO_AUTH_TOKEN",
        "WEBSITE_DEMO_TWIML_APP_SID",
    ):
        assert required in website


def test_website_demo_uses_the_shared_issuer_response_shape_and_fails_closed_without_transport():
    source = (RUNTIME / "website_demo.py.tmpl").read_text(encoding="utf-8")
    assert '@app.get("/api/voice-token")' in source
    assert 'return {"token": token.to_jwt(), "identity": identity}' in source
    assert '@app.api_route("/voice/demo-incoming", methods=["GET", "POST"])' in source
    assert '@app.websocket("/ws/website-demo/media")' in source
    assert "RequestValidator" in source
    assert "WebsiteDemoTransportUnavailable" in source
    assert "raise WebsiteDemoTransportUnavailable" in source
    assert "WEBSITE_DEMO_MAX_ACTIVE_SESSIONS" in source
    assert "WEBSITE_DEMO_TOKEN_TTL_SECONDS" in source
    assert "create_booking" not in source
    assert "handover" not in source.lower()
    assert "post_call" not in source.lower()


def test_candidate_records_the_website_demo_boundary_and_its_source_evidence():
    candidate = json.loads((ROOT / "runtime_infrastructure_candidate.json").read_text(encoding="utf-8"))
    assert candidate["status"] == "partial-candidate-not-approved-for-rendering"
    assert "website_demo.py" in candidate["runtime_outputs"]
    assert any(item["template_path"] == "runtime/website_demo.py.tmpl" for item in candidate["artifacts"])
    provenance = json.loads((ROOT / "candidate_runtime_provenance.json").read_text(encoding="utf-8"))
    assert "website_demo_source" in provenance
    assert provenance["website_demo_source"]["transport_status"] == "fail-closed-adapter-boundary"
