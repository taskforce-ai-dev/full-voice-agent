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


def _smartpbx_service() -> dict:
    document = yaml.safe_load(_text(COMPOSE))
    assert "flico-smartpbx" in document["services"]
    assert "flico-smartpbx" not in document.get("networks", {})
    return document["services"]["flico-smartpbx"]


def test_smartpbx_compose_service_is_immutable_isolated_and_opt_in():
    service = _smartpbx_service()
    image = service["image"]

    assert service["profiles"] == ["smartpbx"]
    assert service["container_name"] == "flico-smartpbx"
    assert image.startswith("ghcr.io/taskforce-ai-dev/flico:")
    assert "${IMAGE_TAG:?" in image
    assert "immutable SHA" in image
    assert ":-latest" not in image
    assert service["ports"] == ["127.0.0.1:8005:8000"]
    assert not any("/udp" in port.lower() for port in service["ports"])
    assert "env_file" not in service
    assert service["restart"] == "unless-stopped"


def test_smartpbx_compose_has_only_required_non_legacy_environment():
    environment = _smartpbx_service()["environment"]

    assert environment["FLICO_SERVICE_MODE"] == "smartpbx"
    assert environment["ENABLE_SMARTPBX_WSS"] == "true"
    assert environment["ENABLE_ASTERISK_ARI"] == "false"
    assert not any(
        name != "ENABLE_ASTERISK_ARI" and "ASTERISK" in name
        for name in environment
    )
    assert not any(
        name.startswith(("SIP_", "ASTERISK_RTP_", "TWILIO_"))
        for name in environment
    )


def test_smartpbx_compose_mounts_only_read_only_runtime_inputs():
    volumes = _smartpbx_service()["volumes"]

    required = {
        "./server.py:/app/server.py:ro",
        "./knowledge_docs:/app/knowledge_docs:ro",
        "./kb:/app/kb:ro",
        "./smartpbx_gateway.py:/app/smartpbx_gateway.py:ro",
        "./smartpbx_mcp.py:/app/smartpbx_mcp.py:ro",
        "./smartpbx_protocol.py:/app/smartpbx_protocol.py:ro",
        "./smartpbx_transport.py:/app/smartpbx_transport.py:ro",
    }
    assert required <= set(volumes)
    assert all(volume.endswith(":ro") for volume in volumes)


def test_smartpbx_compose_bounds_logs_and_healthcheck():
    service = _smartpbx_service()

    assert service["logging"] == {
        "driver": "json-file",
        "options": {"max-size": "10m", "max-file": "3"},
    }
    assert service["healthcheck"] == {
        "test": ["CMD", "curl", "-f", "http://localhost:8000/health"],
        "interval": "30s",
        "timeout": "10s",
        "retries": 3,
        "start_period": "40s",
    }


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


def test_smartpbx_nginx_is_tls_only_and_proxies_exact_routes_with_bounds():
    nginx = _text(NGINX)

    for directive in (
        "upstream flico_smartpbx",
        "server 127.0.0.1:8005;",
        "listen 443 ssl http2;",
        "server_name smartpbx-flico.taskforceai.tech;",
        "ssl_certificate ",
        "ssl_certificate_key ",
        "ssl_protocols TLSv1.2 TLSv1.3;",
        "Strict-Transport-Security",
        "limit_conn_zone $binary_remote_addr zone=smartpbx_per_ip:10m;",
        "limit_conn smartpbx_per_ip 4;",
        "client_max_body_size 64k;",
        "client_body_timeout 10s;",
        "client_header_timeout 10s;",
        "keepalive_timeout 75s;",
        "proxy_connect_timeout 5s;",
        "proxy_read_timeout 120s;",
        "proxy_send_timeout 120s;",
        "proxy_set_header Upgrade $http_upgrade;",
        'proxy_set_header Connection "upgrade";',
        "proxy_http_version 1.1;",
        "proxy_buffering off;",
        "proxy_request_buffering off;",
        "access_log off;",
    ):
        assert directive in nginx
    assert nginx.count("location = ") == 3
    for route in (
        "location = /ws/v1/smartpbx/media",
        "location = /health",
        "location = /smartpbx/status",
        "location / {",
        "return 404;",
    ):
        assert route in nginx
    assert "Access-Control-Allow" not in nginx
    assert "log_format" not in nginx


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


def test_runbook_rollback_withdraws_dashboard_and_drains_before_stopping():
    runbook = _text(RUNBOOK).lower()
    rollback = runbook.split("## status, logs", 1)[0]

    disable_dashboard = rollback.index("disable or remove the dialog ai provider")
    verify_fallback = rollback.index("verify carrier/fallback routing")
    drain_calls = rollback.index("drain active calls")
    stop_service = rollback.index("stop flico-smartpbx")

    assert disable_dashboard < verify_fallback < drain_calls < stop_service
