"""Deployment contracts for the isolated Dialog SmartPBX service."""

from __future__ import annotations

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "docker-compose.yml"
ENV_EXAMPLE = ROOT / ".env.example"
NGINX = ROOT / "nginx-smartpbx.conf"
RUNBOOK = ROOT / "SMARTPBX_RUNBOOK.md"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _environment_values(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        if not line or line.lstrip().startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value
    return values


def test_smartpbx_compose_service_is_explicitly_isolated_and_opt_in():
    compose = _text(COMPOSE)

    assert "flico-smartpbx:" in compose
    service = compose.split("  flico-smartpbx:", 1)[1]
    assert "profiles: [\"smartpbx\"]" in service
    assert "container_name: flico-smartpbx" in service
    assert "image: ghcr.io/taskforce-ai-dev/flico:${IMAGE_TAG:-latest}" in service
    assert '"127.0.0.1:8005:8000"' in service
    assert "/udp" not in service.lower()
    assert "FLICO_SERVICE_MODE: smartpbx" in service
    assert "ENABLE_SMARTPBX_WSS: \"true\"" in service
    assert "ENABLE_ASTERISK_ARI: \"false\"" in service
    assert "ASTERISK_ARI_" not in service
    assert "ASTERISK_RTP_" not in service
    assert "restart: unless-stopped" in service
    assert 'max-size: "10m"' in service
    assert 'max-file: "3"' in service
    assert 'curl", "-f", "http://localhost:8000/health"' in service

    # SmartPBX is profile-gated, so an ordinary `docker compose up -d` does not
    # select it; it must only be started with `--profile smartpbx`.
    assert "profiles:" not in compose.split("  flico-smartpbx:", 1)[0]


def test_smartpbx_compose_keeps_runtime_inputs_read_only():
    service = _text(COMPOSE).split("  flico-smartpbx:", 1)[1]

    for mount in (
        "./server.py:/app/server.py:ro",
        "./knowledge_docs:/app/knowledge_docs:ro",
        "./kb:/app/kb:ro",
        "./smartpbx_gateway.py:/app/smartpbx_gateway.py:ro",
        "./smartpbx_mcp.py:/app/smartpbx_mcp.py:ro",
        "./smartpbx_protocol.py:/app/smartpbx_protocol.py:ro",
        "./smartpbx_transport.py:/app/smartpbx_transport.py:ro",
    ):
        assert mount in service


def test_smartpbx_environment_template_is_safe_and_complete():
    environment = _environment_values(_text(ENV_EXAMPLE))

    expected = {
        "ENABLE_SMARTPBX_WSS": "false",
        "SMARTPBX_WS_TOKEN": "",
        "SMARTPBX_ACCOUNT_ID": "",
        "SMARTPBX_MAX_CALLS": "4",
        "SMARTPBX_MAX_MESSAGE_CHARS": "65536",
        "SMARTPBX_MAX_AUDIO_BYTES": "32768",
        "SMARTPBX_MAX_OUTBOUND_FRAMES": "128",
        "SMARTPBX_START_TIMEOUT_SECONDS": "10",
        "SMARTPBX_IDLE_TIMEOUT_SECONDS": "90",
        "SMARTPBX_MCP_URL": "https://dialog.cybergate.lk:9443/ucp/v2/mcp",
        "SMARTPBX_API_KEY": "",
        "SMARTPBX_TRANSFER_DESTINATIONS_JSON": "{}",
        "SMARTPBX_MCP_ACCOUNT_HEADER": "",
        "SMARTPBX_MCP_CONNECT_TIMEOUT_SECONDS": "3",
        "SMARTPBX_MCP_READ_TIMEOUT_SECONDS": "8",
        "SMARTPBX_MCP_RETRIES": "1",
    }
    assert {key: environment.get(key) for key in expected} == expected
    assert environment["SMARTPBX_WS_TOKEN"] == ""
    assert environment["SMARTPBX_API_KEY"] == ""


def test_smartpbx_nginx_is_tls_only_and_proxies_exactly_three_routes():
    nginx = _text(NGINX)

    assert "upstream flico_smartpbx" in nginx
    assert "server 127.0.0.1:8005;" in nginx
    assert "listen 443 ssl http2;" in nginx
    assert "server_name smartpbx-flico.taskforceai.tech;" in nginx
    assert "Strict-Transport-Security" in nginx
    assert "limit_conn_zone" in nginx
    assert "location = /ws/v1/smartpbx/media" in nginx
    assert "location = /health" in nginx
    assert "location = /smartpbx/status" in nginx
    assert nginx.count("location = ") == 3
    assert "location / {" in nginx
    assert "return 404;" in nginx
    assert "proxy_set_header Upgrade $http_upgrade;" in nginx
    assert 'proxy_set_header Connection "upgrade";' in nginx
    assert "proxy_http_version 1.1;" in nginx
    assert "proxy_buffering off;" in nginx
    assert "client_max_body_size 64k;" in nginx
    assert "proxy_read_timeout 120s;" in nginx
    assert "proxy_send_timeout 120s;" in nginx
    assert "access_log off;" in nginx
    assert "Access-Control-Allow-Origin" not in nginx


def test_runbook_marks_external_readiness_as_operator_gates():
    runbook = _text(RUNBOOK).lower()

    for required in (
        "dns",
        "tls",
        "rotated api key",
        "transfer uri",
        "source ips",
        "dashboard update",
        "carrier fallback",
        "operator gate",
        "revoke",
        "never reused",
        "fake non-production",
        "four-call",
        "mcp transfer",
        "endpoint-down",
    ):
        assert required in runbook
    assert "wss://smartpbx-flico.taskforceai.tech/ws/v1/smartpbx/media" in runbook


def test_smartpbx_is_a_compose_service_not_a_network():
    document = yaml.safe_load(_text(COMPOSE))

    assert "flico-smartpbx" in document["services"]
    assert "flico-smartpbx" not in document.get("networks", {})
    service = document["services"]["flico-smartpbx"]
    assert service["profiles"] == ["smartpbx"]
    assert service["environment"]["FLICO_SERVICE_MODE"] == "smartpbx"
