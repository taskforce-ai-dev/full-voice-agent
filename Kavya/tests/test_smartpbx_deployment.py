"""Static deployment contracts for the isolated Dialog SmartPBX service."""

from pathlib import Path
import re

import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def read_text(name: str) -> str:
    return (PROJECT_ROOT / name).read_text(encoding="utf-8")


def test_kavya_smartpbx_is_loopback_only_and_uses_its_own_port():
    compose = yaml.safe_load(read_text("docker-compose.yml"))
    service = compose["services"]["kavya-smartpbx"]

    assert service["profiles"] == ["smartpbx"]
    assert service["image"] == "ghcr.io/taskforce-ai-dev/kavya:${SMARTPBX_IMAGE_TAG:-disabled}"
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
    assert "SMARTPBX_MCP_URL=https://dialog.cybergate.lk:9443/ucp/v2/mcp" in example
    assert "SMARTPBX_MCP_ACCOUNT_HEADER=" in example
    assert "SMARTPBX_MCP_ACCOUNT_HEADER=account_id" not in example
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
    normalized_runbook = re.sub(r"\s+", " ", runbook)

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
        assert required in normalized_runbook
    assert "SMARTPBX_MCP_ACCOUNT_HEADER=X-Account-ID" in runbook


def test_smartpbx_reviewed_isolation_and_operations_contract():
    compose = yaml.safe_load(read_text("docker-compose.yml"))
    service = compose["services"]["kavya-smartpbx"]
    environment = service["environment"]

    assert "env_file" not in service
    assert service["command"] == ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--ws-max-size", "65536"]
    assert (service["mem_limit"], service["cpus"], service["pids_limit"]) == ("1536m", 2.0, 256)
    assert "./knowledge_docs:/app/knowledge_docs:ro" in service["volumes"]
    assert "./chroma_db_smartpbx:/app/chroma_db" in service["volumes"]
    assert "./chroma_db:/app/chroma_db" not in service["volumes"]
    for required in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "ELEVENLABS_API_KEY", "AZURE_SPEECH_KEY", "YANOLJA_BASE_URL", "YANOLJA_USERNAME", "YANOLJA_PASSWORD", "N8N_BASE_URL", "DASHBOARD_API_URL", "SMARTPBX_WS_TOKEN", "SMARTPBX_ACCOUNT_ID", "SMARTPBX_API_KEY", "SMARTPBX_HUMAN_AGENT_WHATSAPP"):
        assert required in environment
    assert not {"TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "HUMAN_AGENT_PHONE"} & environment.keys()

    dockerfile = read_text("Dockerfile")

    nginx = read_text("nginx-smartpbx.conf")
    for required in ("limit_conn kavya_smartpbx_per_ip 8;", "client_max_body_size 64k;", "client_header_timeout 10s;", "client_body_timeout 10s;", "proxy_connect_timeout 5s;", "proxy_buffering off;", "proxy_request_buffering off;"):
        assert required in nginx
    assert nginx.count("proxy_read_timeout 15s;") == 2
    assert nginx.count("proxy_send_timeout 15s;") == 2
    assert "limit_conn_zone $binary_remote_addr zone=kavya_smartpbx_per_ip:10m;" in nginx

    runbook = read_text("SMARTPBX_RUNBOOK.md")
    for required in ("cd /opt/kavya", "openssl rand -hex 32", "chmod 600 /opt/kavya/.env.smartpbx", "docker compose --env-file .env.smartpbx --profile smartpbx config > /dev/null", "REVIEWED_CI_SHORT_SHA", "REVIEWED_FULL_COMMIT_SHA", "Only the dedicated WSS token is pasted into the Dialog dashboard", "Kavya accepts", "4 accepted + 5th rejected", "source-IP allowlist", "drain active calls", "Flico untouched"):
        assert required in runbook

    server = read_text("server.py")
    assert 'if KAVYA_SERVICE_MODE != "smartpbx":' in server


def test_smartpbx_runbook_selects_ci_short_sha_and_verifies_full_oci_revision():
    workflow = (PROJECT_ROOT.parent / ".github/workflows/deploy.yml").read_text(encoding="utf-8")
    runbook = read_text("SMARTPBX_RUNBOOK.md")
    build_step = workflow.split("- name: Build & push image", 1)[1].split("- name:", 1)[0]

    assert 'sha="$(git rev-parse --short HEAD)"' in workflow
    assert "${{ steps.img.outputs.repo }}:${{ steps.img.outputs.sha }}" in build_step
    assert "org.opencontainers.image.revision=${{ github.sha }}" in build_step
    assert "REVIEWED_CI_SHORT_SHA" in runbook
    assert "REVIEWED_FULL_COMMIT_SHA" in runbook
    assert "org.opencontainers.image.revision" in runbook
    assert "--pull never" in runbook
    assert "<REVIEWED_COMMIT_SHA>" not in runbook


def test_mcp_enable_and_revoke_recreate_only_the_pinned_service_and_prove_runtime_disabled():
    runbook = read_text("SMARTPBX_RUNBOOK.md")
    recreate = (
        'SMARTPBX_IMAGE_TAG="$REVIEWED_CI_SHORT_SHA" docker compose '
        "--env-file .env.smartpbx --profile smartpbx up -d --force-recreate "
        "--pull never kavya-smartpbx"
    )
    enable = runbook.find("Enable a supervised non-production transfer drill")
    first_recreate = runbook.find(recreate, enable)
    restore = runbook.find("Restore `SMARTPBX_TRANSFER_DESTINATIONS_JSON={}`")
    second_recreate = runbook.find(recreate, restore)
    runtime_disabled = runbook.find(".transfer_enabled == false", second_recreate)

    assert enable >= 0
    assert enable < first_recreate < restore < second_recreate < runtime_disabled
    assert "docker compose restart" not in runbook


def test_dashboard_fields_are_not_confused_with_carrier_events_or_local_admission():
    runbook = read_text("SMARTPBX_RUNBOOK.md")
    normalized_runbook = re.sub(r"\s+", " ", runbook)
    dashboard = runbook.split("## Dialog dashboard fields", 1)[1].split("## ", 1)[0]

    rows = [row for row in re.findall(r"^\| ([^|]+) \|", dashboard, re.MULTILINE) if row != "---"]
    assert rows == [
        "Field",
        "Name",
        "Media format",
        "Sample rate",
        "Media WebSocket URL",
        "WebSocket headers",
    ]
    assert "Account ID in start event" not in dashboard
    assert "Maximum concurrent calls" not in dashboard
    assert "`SMARTPBX_ACCOUNT_ID` is server-side" in normalized_runbook
    assert "carrier-emitted `start.accountId`" in normalized_runbook
    assert "four-call limit is enforced locally" in normalized_runbook


def test_acme_bootstrap_precedes_loopback_health_and_final_tls_proxy():
    bootstrap = read_text("nginx-smartpbx-acme.conf")
    nginx = read_text("nginx-smartpbx.conf")
    runbook = read_text("SMARTPBX_RUNBOOK.md")

    assert "listen 80;" in bootstrap
    assert "ssl_certificate" not in bootstrap
    assert "location /.well-known/acme-challenge/" in bootstrap
    assert "return 404;" in bootstrap
    assert "listen 80;" in nginx
    assert "return 301 https://$host$request_uri;" in nginx
    assert "location /.well-known/acme-challenge/" in nginx

    positions = [
        runbook.find("getent ahostsv4 smartpbx-kavya.taskforceai.tech"),
        runbook.find("nginx-smartpbx-acme.conf"),
        runbook.find("sudo nginx -t"),
        runbook.find("sudo systemctl reload nginx"),
        runbook.find("certbot certonly --webroot"),
        runbook.find("fullchain.pem"),
        runbook.find("docker compose --env-file .env.smartpbx --profile smartpbx config > /dev/null"),
        runbook.find("up -d --force-recreate --pull never kavya-smartpbx"),
        runbook.find("http://127.0.0.1:8006/health"),
        runbook.find("sudo install -m 0644 nginx-smartpbx.conf"),
        runbook.rfind("sudo nginx -t"),
        runbook.rfind("sudo systemctl reload nginx"),
        runbook.rfind("https://smartpbx-kavya.taskforceai.tech/health"),
    ]
    assert all(position >= 0 for position in positions)
    assert positions == sorted(positions)
    assert "already issued" in runbook


def test_yanolja_credentials_are_blank_and_smartpbx_chroma_state_is_ignored():
    example = read_text(".env.example")
    client = read_text("yanolja_client.py")
    pms_runbook = read_text("ops/hattonhills-pms/RUNBOOK.md")
    root_ignore = (PROJECT_ROOT.parent / ".gitignore").read_text(encoding="utf-8")
    kavya_ignore = read_text(".gitignore")

    assert re.search(r"^YANOLJA_USERNAME=$", example, re.MULTILINE)
    assert re.search(r"^YANOLJA_PASSWORD=$", example, re.MULTILINE)
    assert 'os.getenv("YANOLJA_USERNAME", "")' in client
    assert 'os.getenv("YANOLJA_PASSWORD", "")' in client
    assert "YANOLJA_USERNAME" in pms_runbook
    assert "YANOLJA_PASSWORD" in pms_runbook
    assert "-d '{\"username\":" not in pms_runbook
    assert "chroma_db_smartpbx/" in root_ignore
    assert "chroma_db_smartpbx/" in kavya_ignore
