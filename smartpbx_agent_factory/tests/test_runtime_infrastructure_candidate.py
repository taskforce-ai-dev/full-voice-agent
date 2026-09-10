"""Static contract for the blocked, client-neutral runtime infrastructure candidate."""

import json
import re
from pathlib import Path


ROOT = Path(__file__).parents[1] / "template_v1"
INFRASTRUCTURE = ROOT / "infrastructure"


def _text(name: str) -> str:
    return (INFRASTRUCTURE / name).read_text(encoding="utf-8")


def test_review_only_runtime_infrastructure_candidate_is_complete_but_not_approved():
    candidate = json.loads((ROOT / "runtime_infrastructure_candidate.json").read_text(encoding="utf-8"))
    assert candidate["status"] == "partial-candidate-not-approved-for-rendering"
    assert candidate["source_revision"] == "6f6c2a3ae6f50e3ea84d293a24c37ef74808ec0e"
    assert candidate["oci_revision"] == candidate["source_revision"]
    assert candidate["approval"] == {"rendering": "blocked", "release": "blocked", "routing": "blocked"}
    assert any("website-demo/Twilio ingress" in item for item in candidate["unresolved_blockers"])
    assert candidate["runtime_outputs"] == [
        "startup.py",
        "server.py",
        "smartpbx_gateway.py",
        "smartpbx_protocol.py",
        "smartpbx_session.py",
        "smartpbx_transport.py",
        "smartpbx_diagnostics.py",
        "product_profile.py",
        "provider_adapters.py",
        "provider_builders.py",
        "stt_adapters.py",
        "llm_adapters.py",
        "tts_adapters.py",
        "turn_engine.py",
        "provider_runtime.py",
        "website_demo.py",
        "config/product_profile.json",
        "config/provider_profile.json",
    ]
    assert candidate["required_provider_adapter_outputs"] == ["provider_builders.py", "stt_adapters.py", "llm_adapters.py", "tts_adapters.py"]
    assert {entry["template_path"] for entry in candidate["artifacts"]} == {
        "infrastructure/Dockerfile.tmpl",
        "infrastructure/requirements-prod.txt.tmpl",
        "infrastructure/requirements-prod.lock.txt.tmpl",
        "infrastructure/docker-compose.yml.tmpl",
        "infrastructure/nginx-smartpbx.conf.tmpl",
        "infrastructure/env.example.tmpl",
        "infrastructure/SMARTPBX_RUNBOOK.md.tmpl",
        "infrastructure/CLIENT_CONNECT.md.tmpl",
        "infrastructure/ci-runtime-review.yml.tmpl",
        "infrastructure/scripts/deploy_runtime_image.sh.tmpl",
        "runtime/website_demo.py.tmpl",
        "infrastructure/nginx-website-demo.conf.tmpl",
        "infrastructure/WEBSITE_DEMO_RUNBOOK.md.tmpl",
    }


def test_container_template_has_explicit_runtime_copy_and_import_guard():
    dockerfile = _text("Dockerfile.tmpl")
    assert "COPY . ." not in dockerfile
    assert "COPY runtime/ ./" not in dockerfile
    assert "COPY runtime/" not in dockerfile
    assert "COPY requirements-prod.lock.txt ./" in dockerfile
    for filename in (
        "server.py",
        "smartpbx_gateway.py",
        "smartpbx_protocol.py",
        "smartpbx_session.py",
        "smartpbx_transport.py",
        "smartpbx_diagnostics.py",
        "product_profile.py",
        "provider_adapters.py",
        "provider_builders.py",
        "stt_adapters.py",
        "llm_adapters.py",
        "tts_adapters.py",
        "turn_engine.py",
        "provider_runtime.py",
        "startup.py",
        "website_demo.py",
    ):
        assert filename in dockerfile
    assert 'python -c "import startup"' in dockerfile
    assert "SMARTPBX_RUNTIME_MODE=synthetic" in dockerfile
    assert "SMARTPBX_ALLOW_SYNTHETIC_FOR_CI=1" in dockerfile
    assert 'CMD ["uvicorn", "startup:app"' in dockerfile


def test_requirements_are_exactly_pinned_and_lock_covers_input():
    requirements = _text("requirements-prod.txt.tmpl")
    lock = _text("requirements-prod.lock.txt.tmpl")
    requirement_lines = [
        line for line in requirements.splitlines()
        if line and not line.startswith("#") and line != "{{provider_requirements}}"
    ]
    assert requirement_lines
    assert all(re.fullmatch(r"[A-Za-z0-9_.-]+(?:\[[A-Za-z0-9_,.-]+\])?==[A-Za-z0-9_.!+-]+", line) for line in requirement_lines)
    assert all(line in lock for line in requirement_lines)
    assert "{{provider_requirements}}" in requirements
    assert "anthropic==" not in requirements
    assert "google-cloud-speech==" not in requirements
    assert "openai" not in requirements.lower()


def test_compose_and_proxy_are_loopback_limited_and_status_is_authenticated():
    compose = _text("docker-compose.yml.tmpl")
    nginx = _text("nginx-smartpbx.conf.tmpl")
    assert "env_file:" not in compose
    assert '"127.0.0.1:{{smartpbx_port}}:8000"' in compose
    assert "{{smartpbx_service}}:" in compose
    assert "{{ghcr_repository}}@${SMARTPBX_IMAGE_DIGEST:?immutable digest required}" in compose
    assert "container_name: {{smartpbx_service}}" in compose
    assert "mem_limit:" in compose and "cpus:" in compose and "pids_limit:" in compose
    assert "{{provider_environment}}" in compose
    assert "{{provider_volumes}}" in compose
    assert "STT_PROVIDER:" not in compose and "LLM_PROVIDER:" not in compose and "TTS_PROVIDER:" not in compose
    assert "GOOGLE_APPLICATION_CREDENTIALS:" not in compose
    assert "RIME_API_KEY:" not in compose
    assert "read_only: true" in compose
    assert "cap_drop:" in compose and "- ALL" in compose
    assert "no-new-privileges:true" in compose
    assert "tmpfs:" in compose
    assert "healthcheck:" in compose and "/health" in compose
    assert "stop_grace_period:" in compose
    assert "{{smartpbx_service}}_bridge:" in compose
    assert "internal: false" in compose
    assert "./knowledge_docs:/app/knowledge_docs:ro" in compose
    assert "SMARTPBX_WS_TOKEN:" in compose
    assert "ANTHROPIC_API_KEY:" not in compose
    assert "OPENAI_API_KEY:" not in compose
    assert "location = /ws/v1/smartpbx/media" in nginx
    assert "proxy_set_header Upgrade $http_upgrade;" in nginx
    assert "location = /smartpbx/status" in nginx
    assert "server_name {{smartpbx_hostname}};" in nginx
    assert "proxy_set_header {{wss_header}} $http_x_smartpbx_token;" in nginx
    assert "location / { return 404; }" in nginx


def test_review_artifacts_cannot_publish_deploy_or_activate_routing():
    deploy = _text("scripts/deploy_runtime_image.sh.tmpl")
    ci = _text("ci-runtime-review.yml.tmpl")
    runbook = _text("SMARTPBX_RUNBOOK.md.tmpl")
    client_connect = _text("CLIENT_CONNECT.md.tmpl")
    assert "exit 1" in deploy
    assert "docker compose" not in deploy
    assert "docker push" not in ci
    assert "deploy" not in ci.lower()
    assert "if: false" not in ci
    assert "SMARTPBX_RUNTIME_MODE: synthetic" in ci
    assert "SMARTPBX_ALLOW_SYNTHETIC_FOR_CI" in ci
    for text in (runbook, client_connect):
        assert "REVIEW-ONLY" in text
        assert "activation: blocked" in text


def test_candidate_artifacts_are_client_neutral_and_exclude_legacy_integrations():
    terms = ("tenant", "k" + "avya", "p" + "ms", "hando" + "ver", "create_" + "booking", "post_" + "call")
    artifacts = [path for path in INFRASTRUCTURE.rglob("*") if path.is_file()]
    artifacts.append(ROOT / "runtime_infrastructure_candidate.json")
    combined = "\n".join(path.read_text(encoding="utf-8").lower() for path in artifacts)
    assert all(term not in combined for term in terms)


def test_startup_composition_validates_named_environment_once_and_exports_the_asgi_app():
    startup = (ROOT / "runtime" / "startup.py.tmpl").read_text(encoding="utf-8")
    for name in (
        "SMARTPBX_WS_TOKEN",
        "SMARTPBX_ACCOUNT_ID",
        "SMARTPBX_AUTH_HEADER_NAME",
        "SMARTPBX_PRODUCT_PROFILE_PATH",
        "SMARTPBX_KNOWLEDGE_DIR",
        "SMARTPBX_PROVIDER_PROFILE_PATH",
    ):
        assert name in startup
    assert "def create_app" in startup
    assert "app = create_app()" in startup
    assert "build_service_app(load_runtime(" in startup
    assert "bind_provider_adapter" in startup
    assert "ReviewOnlyProviderAdapter" not in startup


def test_provider_requirements_and_secret_mounts_are_derived_at_render_time():
    renderer = (Path(__file__).parents[1] / "render.py").read_text(encoding="utf-8")
    assert "_provider_runtime_contract" in renderer
    assert "provider_environment" in renderer
    assert "provider_volumes" in renderer
    assert "provider_profile.json" in renderer
