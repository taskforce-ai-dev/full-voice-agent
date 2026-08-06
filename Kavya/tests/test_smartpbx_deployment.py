"""Static deployment contracts for the isolated Dialog SmartPBX service."""

from pathlib import Path

import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def read_text(name: str) -> str:
    return (PROJECT_ROOT / name).read_text(encoding="utf-8")


def test_kavya_smartpbx_is_loopback_only_and_uses_its_own_port():
    compose = yaml.safe_load(read_text("docker-compose.yml"))
    service = compose["services"]["kavya-smartpbx"]

    assert service["profiles"] == ["smartpbx"]
    assert service["image"] == "ghcr.io/taskforce-ai-dev/kavya:${IMAGE_TAG:-latest}"
    assert "127.0.0.1:8006:8000" in service["ports"]
    assert service["environment"]["KAVYA_SERVICE_MODE"] == "smartpbx"
    assert service["environment"]["ENABLE_SMARTPBX_WSS"] == "true"
    assert service["environment"]["SMARTPBX_AUTH_HEADER_NAME"] == "X-Kavya-SmartPBX-Token"
    assert service["environment"]["SMARTPBX_MAX_CALLS"] == "4"


def test_examples_never_enable_or_populate_live_transfer():
    example = read_text(".env.example")

    assert "ENABLE_SMARTPBX_WSS=false" in example
    assert "SMARTPBX_WS_TOKEN=" in example
    assert "SMARTPBX_ACCOUNT_ID=" in example
    assert "SMARTPBX_API_KEY=" in example
    assert "SMARTPBX_TRANSFER_DESTINATIONS_JSON={}" in example
    assert "SMARTPBX_MCP_ACCOUNT_HEADER=account_id" in example
    assert "SMARTPBX_MCP_ACCOUNT_HEADER=X-Account-ID" not in example


def test_dockerfile_locks_dependencies_and_copies_every_smartpbx_runtime_module():
    dockerfile = read_text("Dockerfile")

    assert "requirements-prod.lock.txt" in dockerfile
    for module in (
        "smartpbx_gateway.py",
        "smartpbx_mcp.py",
        "smartpbx_protocol.py",
        "smartpbx_session.py",
        "smartpbx_transport.py",
    ):
        assert module in dockerfile


def test_nginx_exposes_only_the_bounded_smartpbx_surface_with_tls():
    nginx = read_text("nginx-smartpbx.conf")

    assert "server_name smartpbx-kavya.taskforceai.tech;" in nginx
    assert "ssl_protocols TLSv1.2 TLSv1.3;" in nginx
    assert "access_log off;" in nginx
    assert nginx.count("location = /ws/v1/smartpbx/media") == 1
    assert nginx.count("location = /health") == 1
    assert nginx.count("location = /smartpbx/status") == 1
    assert "proxy_pass http://127.0.0.1:8006;" in nginx
    assert "proxy_read_timeout 120s;" in nginx
    assert "proxy_send_timeout 120s;" in nginx
    assert "location / { return 404; }" in nginx


def test_runbook_keeps_credentials_server_side_and_documents_dialog_cutover():
    runbook = read_text("SMARTPBX_RUNBOOK.md")

    for required in (
        "wss://smartpbx-kavya.taskforceai.tech/ws/v1/smartpbx/media",
        "X-Kavya-SmartPBX-Token",
        "g711_ulaw",
        "8000",
        "SMARTPBX_MCP_ACCOUNT_HEADER=account_id",
        "SMARTPBX_MCP_URL",
        "server-only",
        "dashboard WSS headers",
        "transfer-disabled",
        "non-production transfer drill",
        "Flico",
        "rollback",
    ):
        assert required in runbook
    assert "SMARTPBX_MCP_ACCOUNT_HEADER=X-Account-ID" not in runbook
