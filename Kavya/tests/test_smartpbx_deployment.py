"""Static deployment contracts for the isolated Dialog SmartPBX service."""

import os
from pathlib import Path
import subprocess
import re
import textwrap

import pytest
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
        "smartpbx_handover.py",
        "smartpbx_mcp.py",
        "smartpbx_protocol.py",
        "smartpbx_session.py",
        "smartpbx_transport.py",
    ):
        assert module in dockerfile


def test_dockerfile_copies_the_neutral_diagnostics_module_for_runtime_imports():
    dockerfile = read_text("Dockerfile")

    copy_line = next(line for line in dockerfile.splitlines() if line.startswith("COPY server.py"))
    assert "smartpbx_diagnostics.py" in copy_line


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

    tls_start = runbook.find("## TLS bootstrap, local service validation, then public proxy")
    positions = [
        runbook.find("getent ahostsv4 smartpbx-kavya.taskforceai.tech"),
        runbook.find("nginx-smartpbx-acme.conf"),
        runbook.find("sudo nginx -t"),
        runbook.find("sudo systemctl reload nginx"),
        runbook.find("certbot certonly --webroot"),
        runbook.find("fullchain.pem"),
        runbook.find("docker compose --env-file .env.smartpbx --profile smartpbx config > /dev/null", tls_start),
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


def test_tls_and_mcp_recreates_fail_fast_and_wait_for_bounded_loopback_readiness():
    runbook = read_text("SMARTPBX_RUNBOOK.md")
    recreate = (
        'SMARTPBX_IMAGE_TAG="$REVIEWED_CI_SHORT_SHA" docker compose '
        "--env-file .env.smartpbx --profile smartpbx up -d --force-recreate "
        "--pull never kavya-smartpbx"
    )
    readiness = "wait_for_smartpbx_ready"

    tls = runbook.split("## TLS bootstrap, local service validation, then public proxy", 1)[1].split("## Cutover gates", 1)[0]
    enable = runbook.split("Enable a supervised non-production transfer drill", 1)[1].split("Perform one observed drill", 1)[0]
    revoke = runbook.split("Perform one observed drill", 1)[1].split("## Withdraw", 1)[0]

    for block in (tls, enable, revoke):
        assert "set -euo pipefail" in block
        assert "deadline=$((SECONDS + 90))" in block
        assert "curl --silent --show-error --fail --connect-timeout 2 --max-time 5 http://127.0.0.1:8006/health" in block
        assert "curl --silent --show-error --fail --connect-timeout 2 --max-time 5 http://127.0.0.1:8006/smartpbx/status" in block
        assert "sleep 2" in block
        assert "exit 1" in block
        invocations = [
            match.start()
            for match in re.finditer(r"(?m)^wait_for_smartpbx_ready\s*$", block)
        ]
        assert len(invocations) == 1
        assert block.find(recreate) < invocations[0]

    tls_invocation = re.search(r"(?m)^wait_for_smartpbx_ready\s*$", tls).start()
    enable_invocation = re.search(r"(?m)^wait_for_smartpbx_ready\s*$", enable).start()
    revoke_invocation = re.search(r"(?m)^wait_for_smartpbx_ready\s*$", revoke).start()
    assert tls_invocation < tls.find("sudo install -m 0644 nginx-smartpbx.conf")
    assert runbook.find("Enable a supervised non-production transfer drill") + enable_invocation < runbook.find("Perform one observed drill")
    assert revoke_invocation < revoke.find(".transfer_enabled == false")


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


def test_canonical_voice_configuration_covers_both_kavya_services_and_stays_disabled():
    example = read_text(".env.example")
    compose = yaml.safe_load(read_text("docker-compose.yml"))
    runbook = read_text("SMARTPBX_RUNBOOK.md")
    legacy = compose["services"]["kavya"]
    smartpbx = compose["services"]["kavya-smartpbx"]
    environment = smartpbx["environment"]

    assert re.search(r"^KAVYA_EN_ELEVENLABS_VOICE_ID=$", example, re.MULTILINE)
    assert re.search(r"^KAVYA_EN_ELEVENLABS_VOICE_ID=.+$", example, re.MULTILINE) is None
    assert legacy["env_file"] == [".env"]
    assert environment["KAVYA_EN_ELEVENLABS_VOICE_ID"] == "${KAVYA_EN_ELEVENLABS_VOICE_ID}"
    assert environment["SMARTPBX_API_KEY"] == "${SMARTPBX_API_KEY}"
    assert environment["SMARTPBX_MCP_ACCOUNT_HEADER"] == "${SMARTPBX_MCP_ACCOUNT_HEADER}"
    assert environment["SMARTPBX_TRANSFER_DESTINATIONS_JSON"] == "${SMARTPBX_TRANSFER_DESTINATIONS_JSON}"
    assert re.search(r"^SMARTPBX_API_KEY=$", example, re.MULTILINE)
    assert re.search(r"^SMARTPBX_MCP_ACCOUNT_HEADER=$", example, re.MULTILINE)
    assert re.search(r"^SMARTPBX_TRANSFER_DESTINATIONS_JSON=\{\}$", example, re.MULTILINE)
    for required in (
        "## Canonical English voice provisioning",
        "sudo chown root:root /opt/kavya/.env /opt/kavya/.env.smartpbx",
        "sudo chmod 600 /opt/kavya/.env /opt/kavya/.env.smartpbx",
        "sudo /opt/kavya/scripts/validate_english_voice_env.sh /opt/kavya/.env /opt/kavya/.env.smartpbx",
        "canonical_voice_match=ok",
        "SMARTPBX_TRANSFER_DESTINATIONS_JSON={}",
        "transfer-disabled",
    ):
        assert required in runbook
    assert "sha" + "256sum" not in runbook


def test_cutover_gates_require_fixed_private_protocol_diagnostics_and_preserve_operations():
    runbook = read_text("SMARTPBX_RUNBOOK.md")
    cutover = runbook.split("## Cutover gates", 1)[1].split("## ", 1)[0]
    diagnostics = cutover.split("\n\n1.", 1)[0]
    normalized_diagnostics = re.sub(r"\s+", " ", diagnostics).lower()

    assert "fingerprint" not in normalized_diagnostics
    assert "event=smartpbx_protocol_diagnostic" in diagnostics
    assert diagnostics.count("event=") == 1
    assert "exactly seven fields" in normalized_diagnostics
    for field in (
        "correlation_id",
        "stage",
        "outcome",
        "failure_class",
        "active_sessions",
        "duration_ms",
    ):
        assert f"`{field}`" in diagnostics
    assert "opaque, local, randomly generated" in normalized_diagnostics
    assert "never derived from dialog" in normalized_diagnostics
    for forbidden in (
        "payload",
        "audio",
        "transcript",
        "credential",
        "exception",
        "stack",
        "session_id",
        "call_fingerprint",
        "counter",
        "raw call",
        "call id",
        "dialog id",
    ):
        assert forbidden not in normalized_diagnostics

    assert "transfer-disabled" in runbook
    assert "Test endpoint-down fallback before shifting traffic." in cutover
    assert "**4 accepted + 5th rejected**" in cutover
    for required in ("REVIEWED_CI_SHORT_SHA", "REVIEWED_FULL_COMMIT_SHA", "--pull never", "kavya-smartpbx"):
        assert required in runbook

    rollback = runbook.split("## Withdraw and rollback without dropping calls", 1)[1]
    withdraw = rollback.find("Withdraw the Dialog dashboard/carrier route")
    drain = rollback.find("`active_sessions` is zero")
    stop = rollback.find("stop kavya-smartpbx")
    assert withdraw >= 0
    assert withdraw < drain < stop


BUILD_KAVYA_IMAGE_WORKFLOW = PROJECT_ROOT.parent / ".github/workflows/build-kavya-image.yml"
DEPLOY_WORKFLOW = PROJECT_ROOT.parent / ".github/workflows/deploy.yml"


def read_build_kavya_image_workflow():
    assert BUILD_KAVYA_IMAGE_WORKFLOW.is_file(), (
        "missing build-only Kavya image publisher workflow"
    )
    text = BUILD_KAVYA_IMAGE_WORKFLOW.read_text(encoding="utf-8")
    document = yaml.load(text, Loader=yaml.BaseLoader)
    assert isinstance(document, dict)
    return document, text


def build_kavya_image_job():
    document, text = read_build_kavya_image_workflow()
    jobs = document.get("jobs", {})
    assert set(jobs) == {"build"}
    job = jobs["build"]
    return document, job, job.get("steps", []), text


def workflow_step(steps, name):
    step = next((step for step in steps if step.get("name") == name), None)
    assert step is not None, f"missing workflow step: {name}"
    return step


def test_build_kavya_image_publisher_is_dispatch_only_and_least_privilege():
    document, job, steps, text = build_kavya_image_job()

    assert document["name"] == "Build Kavya image (no deploy)"
    assert set(document["on"]) == {"workflow_dispatch"}
    inputs = document["on"]["workflow_dispatch"]["inputs"]
    assert set(inputs) == {"ref", "expected_sha"}
    assert inputs["ref"]["required"] == "true"
    assert inputs["expected_sha"]["required"] == "true"
    assert job["permissions"] == {"contents": "read", "packages": "write"}
    assert "environment" not in job

    login = workflow_step(steps, "Log in to GHCR")
    assert login["uses"].startswith("docker/login-action@")
    assert login["with"] == {
        "registry": "ghcr.io",
        "username": "${{ github.actor }}",
        "password": "${{ secrets.GITHUB_TOKEN }}",
    }
    assert re.findall(r"secrets\.([A-Za-z0-9_]+)", text) == ["GITHUB_TOKEN"]

    for forbidden in (
        "workflow_run",
        "docker compose",
        "systemctl",
        "nginx",
        "rsync",
        "scp",
        "ssh ",
        "curl ",
        "VPS_HOST",
        "VPS_USER",
    ):
        assert forbidden not in text


def test_build_kavya_image_publisher_validates_the_checked_out_review_before_registry_access():
    _document, _job, steps, _text = build_kavya_image_job()

    publisher_checkout = workflow_step(steps, "Checkout trusted publisher tooling")
    source_checkout = workflow_step(steps, "Checkout reviewed source")
    validation = workflow_step(steps, "Validate reviewed checkout")
    login = workflow_step(steps, "Log in to GHCR")
    build = workflow_step(steps, "Build and push immutable image")

    assert publisher_checkout["uses"].startswith("actions/checkout@")
    assert publisher_checkout["with"] == {
        "ref": "${{ github.workflow_sha }}",
        "path": ".publisher",
        "persist-credentials": "false",
    }
    assert source_checkout["uses"].startswith("actions/checkout@")
    assert source_checkout["with"] == {
        "ref": "${{ inputs.ref }}",
        "path": "source",
        "persist-credentials": "false",
    }
    assert steps.index(publisher_checkout) < steps.index(source_checkout) < steps.index(validation)
    assert steps.index(validation) < steps.index(login) < steps.index(build)
    assert validation.get("working-directory") == "source"
    assert "[[ $expected_sha =~ ^[0-9a-f]{40}$ ]]" in validation["run"]
    assert "actual_sha=\"$(git rev-parse HEAD)\"" in validation["run"]
    assert "test -z \"$(git status --porcelain)\"" in validation["run"]
    assert "test \"$actual_sha\" = \"$expected_sha\"" in validation["run"]

def test_build_kavya_image_publisher_uses_one_immutable_checked_out_tag_and_verifies_digest():
    _document, job, steps, text = build_kavya_image_job()

    identity = workflow_step(steps, "Validate reviewed checkout")
    probe = workflow_step(steps, "Probe immutable tag")
    build = workflow_step(steps, "Build and push immutable image")
    verify = workflow_step(steps, "Verify pushed digest and revision")

    assert "image=\"ghcr.io/taskforce-ai-dev/kavya\"" in identity["run"]
    assert "tag=\"${image}:${actual_sha::7}\"" in identity["run"]
    assert build["uses"].startswith("docker/build-push-action@")
    assert build["with"]["context"] == "source/Kavya"
    assert build["with"]["file"] == "source/Kavya/Dockerfile"
    assert build["with"]["push"] == "true"
    assert build["with"]["tags"] == "${{ steps.identity.outputs.tag }}"
    assert build["with"]["labels"] == (
        "org.opencontainers.image.revision=${{ steps.identity.outputs.actual_sha }}"
    )
    assert "${{ steps.digest.outputs.digest }}" in verify["env"]["DIGEST"]
    assert "image_ref=\"${IMAGE}@${DIGEST}\"" in verify["run"]
    assert "docker pull \"$image_ref\"" in verify["run"]
    assert "docker image inspect \"$image_ref\"" in verify["run"]
    assert "test \"$actual_revision\" = \"$EXPECTED_SHA\"" in verify["run"]
    assert "test \"$actual_revision\" = \"$ACTUAL_SHA\"" in verify["run"]
    assert set(job["outputs"]) == {"digest", "tag", "revision"}
    assert ":latest" not in text


def test_build_kavya_image_publisher_summary_is_limited_to_safe_build_identity():
    _document, _job, steps, _text = build_kavya_image_job()

    summary = workflow_step(steps, "Write safe build summary")
    assert summary.get("if") == "${{ always() }}"
    for safe_field in ("revision", "tag", "digest", "run_id", "status"):
        assert safe_field in summary["run"]
    for forbidden in ("expected_sha", "secrets", "github.actor", "ref"):
        assert forbidden not in summary["run"]


KAVYA_IMAGE_TAG_PROBE = PROJECT_ROOT.parent / ".github/scripts/check-kavya-image-tag.sh"
KAVYA_IMAGE_TARGET = "ghcr.io/taskforce-ai-dev/kavya:deadbee"


def workflow_run_strings(value):
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "run" and isinstance(child, str):
                yield child
            yield from workflow_run_strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from workflow_run_strings(child)


def run_kavya_image_tag_probe(tmp_path, docker_exit, docker_output):
    assert KAVYA_IMAGE_TAG_PROBE.is_file(), "missing executable Kavya image tag probe"
    assert os.access(KAVYA_IMAGE_TAG_PROBE, os.X_OK)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_docker = fake_bin / "docker"
    fake_docker.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "if [[ \"$1\" != \"buildx\" || \"$2\" != \"imagetools\" || \"$3\" != \"inspect\" || \"$4\" != \"$EXPECTED_TARGET\" ]]; then\n"
        "  echo unexpected-docker-invocation >&2\n"
        "  exit 99\n"
        "fi\n"
        "echo \"$DOCKER_OUTPUT\" >&2\n"
        "exit \"$DOCKER_EXIT\"\n",
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)
    environment = os.environ | {
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "DOCKER_EXIT": str(docker_exit),
        "DOCKER_OUTPUT": docker_output,
        "EXPECTED_TARGET": KAVYA_IMAGE_TARGET,
    }
    return subprocess.run(
        [str(KAVYA_IMAGE_TAG_PROBE), KAVYA_IMAGE_TARGET],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )


def test_build_kavya_image_publisher_uses_static_concurrency_and_env_only_input_flow():
    document, _job, steps, _text = build_kavya_image_job()

    publisher_checkout = workflow_step(steps, "Checkout trusted publisher tooling")
    source_checkout = workflow_step(steps, "Checkout reviewed source")
    validation = workflow_step(steps, "Validate reviewed checkout")
    assert "concurrency" in document
    concurrency = document["concurrency"]

    assert publisher_checkout["with"]["persist-credentials"] == "false"
    assert source_checkout["with"]["persist-credentials"] == "false"
    assert concurrency["group"] == "kavya-image-publisher"
    assert concurrency["cancel-in-progress"] == "false"
    assert "${{" not in concurrency["group"]
    assert "env" in validation
    assert validation["env"] == {"EXPECTED_SHA": "${{ inputs.expected_sha }}"}
    assert "$EXPECTED_SHA" in validation["run"]
    assert "${{ inputs." not in validation["run"]
    assert all("${{ inputs." not in run for run in workflow_run_strings(document))


def test_build_kavya_image_publisher_probes_registry_before_build_with_an_executable_script():
    _document, _job, steps, text = build_kavya_image_job()

    buildx = workflow_step(steps, "Set up Buildx")
    login = workflow_step(steps, "Log in to GHCR")
    probe = workflow_step(steps, "Probe immutable tag")
    build = workflow_step(steps, "Build and push immutable image")

    assert KAVYA_IMAGE_TAG_PROBE.is_file()
    probe_script = KAVYA_IMAGE_TAG_PROBE.read_text(encoding="utf-8")
    assert "docker buildx imagetools inspect \"$TAG\"" in probe_script
    assert "docker manifest inspect" not in probe_script
    assert os.access(KAVYA_IMAGE_TAG_PROBE, os.X_OK)
    assert "bash .publisher/.github/scripts/check-kavya-image-tag.sh \"$TAG\"" in probe["run"]
    assert "source/.github/scripts" not in probe["run"]
    assert not re.search(r"(?:bash|sh)\s+source/.github/scripts/", text)
    assert steps.index(buildx) < steps.index(login) < steps.index(probe) < steps.index(build)


@pytest.mark.parametrize(
    ("docker_exit", "docker_output", "expected_exit", "expected_state"),
    [
        (
            0,
            "Name: ghcr.io/taskforce-ai-dev/kavya:deadbee\nMediaType: application/vnd.oci.image.index.v1+json\nManifests: linux/amd64, linux/arm64, provenance",
            10,
            "existing",
        ),
        (1, f"manifest unknown: {KAVYA_IMAGE_TARGET}", 0, "absent"),
        (1, f"no such manifest: {KAVYA_IMAGE_TARGET}", 0, "absent"),
        (1, f"failed to resolve source metadata for {KAVYA_IMAGE_TARGET}: not found", 0, "absent"),
        (1, f"denied: manifest unknown: {KAVYA_IMAGE_TARGET}", 1, "probe_failed"),
        (1, f"authentication required: no such manifest: {KAVYA_IMAGE_TARGET}", 1, "probe_failed"),
        (1, f"token expired: {KAVYA_IMAGE_TARGET}: not found", 1, "probe_failed"),
        (1, f"proxy returned 404 for {KAVYA_IMAGE_TARGET}", 1, "probe_failed"),
        (1, f"timeout: manifest unknown: {KAVYA_IMAGE_TARGET}", 1, "probe_failed"),
        (1, f"dial tcp: lookup registry: no such host: {KAVYA_IMAGE_TARGET}: not found", 1, "probe_failed"),
        (1, f"429 rate limit: no such manifest: {KAVYA_IMAGE_TARGET}", 1, "probe_failed"),
        (1, f"500 internal server error: {KAVYA_IMAGE_TARGET}: not found", 1, "probe_failed"),
        (1, "manifest unknown", 1, "probe_failed"),
        (1, "no such manifest", 1, "probe_failed"),
        (1, "not found", 1, "probe_failed"),
        (1, "no such manifest: ghcr.io/taskforce-ai-dev/kavya:othertag", 1, "probe_failed"),
        (1, "failed to resolve source metadata for ghcr.io/taskforce-ai-dev/kavya:othertag: not found", 1, "probe_failed"),
        (1, "registry returned 404", 1, "probe_failed"),
        (1, "malformed registry response", 1, "probe_failed"),
    ],
)
def test_kavya_image_tag_probe_fails_closed_without_echoing_registry_errors(
    tmp_path, docker_exit, docker_output, expected_exit, expected_state
):
    result = run_kavya_image_tag_probe(tmp_path, docker_exit, docker_output)

    assert result.returncode == expected_exit
    assert result.stdout == f"image_tag_state={expected_state}\n"
    assert docker_output not in result.stdout
    assert docker_output not in result.stderr


def test_build_kavya_image_publisher_verifies_existing_tags_without_overwriting_them():
    _document, _job, steps, _text = build_kavya_image_job()

    probe = workflow_step(steps, "Probe immutable tag")
    build = workflow_step(steps, "Build and push immutable image")
    resolve = workflow_step(steps, "Resolve image digest")
    verify = workflow_step(steps, "Verify pushed digest and revision")

    assert "mode=existing" in probe["run"]
    assert "mode=absent" in probe["run"]
    assert build["if"] == "${{ steps.probe.outputs.mode == 'absent' }}"
    assert "${{ steps.probe.outputs.mode }}" in resolve["env"]["MODE"]
    assert "${{ steps.build.outputs.digest }}" in resolve["env"]["BUILT_DIGEST"]
    assert "docker buildx imagetools inspect \"$TAG\"" in resolve["run"]
    assert "pushed_digest=" in resolve["run"]
    assert "test \"$pushed_digest\" = \"$BUILT_DIGEST\"" in resolve["run"]
    assert resolve["run"].find("test \"$pushed_digest\" = \"$BUILT_DIGEST\"") < resolve["run"].find("echo \"digest=$digest\"")
    assert "[[ $digest =~ ^sha256:[0-9a-f]{64}$ ]]" in resolve["run"]
    assert "digest=$digest" in resolve["run"]
    assert "${{ steps.digest.outputs.digest }}" in verify["env"]["DIGEST"]
    assert "docker pull \"$image_ref\"" in verify["run"]
    assert "test \"$actual_revision\" = \"$EXPECTED_SHA\"" in verify["run"]
    assert "test \"$actual_revision\" = \"$ACTUAL_SHA\"" in verify["run"]



def test_build_kavya_image_publisher_uses_only_trusted_tooling_and_honest_writer_scope_note():
    document, _job, steps, text = build_kavya_image_job()

    source_checkout = workflow_step(steps, "Checkout reviewed source")
    probe = workflow_step(steps, "Probe immutable tag")

    assert source_checkout["with"]["ref"] == "${{ inputs.ref }}"
    assert "github.workflow_sha" not in source_checkout["with"]["ref"]
    assert ".publisher/.github/scripts/check-kavya-image-tag.sh" in probe["run"]
    assert "source/.github/scripts" not in "\n".join(workflow_run_strings(document))
    assert "single-writer" not in text.lower()
    assert "out-of-band writers are possible" in text
    assert "consumers use the verified digest" in text


def test_deploy_workflow_rejects_kavya_image_mode_before_any_publisher_or_host_step():
    text = DEPLOY_WORKFLOW.read_text(encoding="utf-8")
    document = yaml.load(text, Loader=yaml.BaseLoader)
    steps = document["jobs"]["deploy"]["steps"]

    guard = workflow_step(steps, "Reject Kavya image publishing")
    guard_run = guard["run"]
    assert '[[ "$AGENT" == "kavya" && "$MODE" == "image" ]]' in guard_run
    assert "Kavya image mode is disabled; use the build-only Kavya image publisher." in guard_run
    assert "exit 1" in guard_run
    for later_name in (
        "Set up Buildx",
        "Log in to GHCR",
        "Build & push image",
        "Set up SSH",
        "Sync agent files to the VPS (preserves .env + runtime state)",
    ):
        assert steps.index(guard) < steps.index(workflow_step(steps, later_name))


SMARTPBX_IMAGE_DEPLOY_SCRIPT = PROJECT_ROOT / "scripts" / "deploy_smartpbx_image.sh"


def read_smartpbx_image_deploy_script():
    assert SMARTPBX_IMAGE_DEPLOY_SCRIPT.is_file(), "missing SmartPBX image deployment helper"
    return SMARTPBX_IMAGE_DEPLOY_SCRIPT.read_text(encoding="utf-8")


def test_smartpbx_image_deploy_helper_is_root_only_sourceable_and_uses_a_fixed_target():
    script = read_smartpbx_image_deploy_script()

    assert os.access(SMARTPBX_IMAGE_DEPLOY_SCRIPT, os.X_OK)
    assert script.startswith("#!/usr/bin/env bash\nset -Eeuo pipefail")
    for required in (
        "[[ $EUID -eq 0 ]]",
        "APP_DIR=/opt/kavya",
        "cd \"$APP_DIR\"",
        "flock -n 9",
        "/var/lock/kavya-smartpbx-image-deploy.lock",
        "NEW_TAG=$1",
        "EXPECTED_SHA=$2",
        "EXPECTED_DIGEST=$3",
        "validate_inputs",
        "if [[ \"${BASH_SOURCE[0]}\" == \"$0\" ]]; then",
        "main \"$@\"",
    ):
        assert required in script
    for forbidden in ("TEST_MODE", "set +e", "sudo ", "eval "):
        assert forbidden not in script


def test_smartpbx_image_deploy_helper_completes_all_preflights_before_arming_rollback():
    script = read_smartpbx_image_deploy_script()

    for required in (
        "capture_baseline",
        "check_loopback_preflight",
        "check_env_files",
        "validate_english_voice_env.sh",
        "canonical_voice_match=ok",
        "check_legacy_flico",
        "docker compose --env-file .env.smartpbx --profile smartpbx config > /dev/null 2>&1",
        "docker pull \"$IMAGE@$EXPECTED_DIGEST\" > /dev/null 2>&1",
        "verify_candidate_image",
    ):
        assert required in script
    main_block = script.split("main(){", 1)[1].split("\nif [[", 1)[0]
    assert "trap rollback ERR" in main_block
    assert main_block.find("verify_candidate_image") < main_block.find("trap rollback ERR") < main_block.find("recreate_smartpbx")
    assert "ROLLBACK_TAG=" in script
    assert "ROLLBACK_DIGEST=" in script
    assert "ROLLBACK_REVISION=" in script
    assert "docker image tag \"$ROLLBACK_IMAGE_ID\" \"$IMAGE:$ROLLBACK_TAG\"" in script
    assert "verify_local_rollback" in script


def test_smartpbx_image_deploy_helper_recreates_only_smartpbx_and_checks_json_readiness():
    script = read_smartpbx_image_deploy_script()

    mutation = "SMARTPBX_IMAGE_TAG=\"$TAG\" docker compose --env-file .env.smartpbx --profile smartpbx up -d --force-recreate --pull never kavya-smartpbx"
    assert mutation in script
    assert script.count("--force-recreate --pull never kavya-smartpbx") == 1
    assert "deadline=$((SECONDS + 90))" in script
    assert "http://127.0.0.1:8006/health" in script
    assert "http://127.0.0.1:8006/smartpbx/status" in script
    assert ".status == \"ok\" and .service_mode == \"smartpbx\"" in script
    assert ".active_sessions == 0 and .transfer_enabled == false" in script
    assert "docker inspect --format \"{{.Image}}\" kavya-smartpbx" in script
    assert "verify_running_image" in script
    assert "verify_legacy_flico_unchanged" in script
    for forbidden in (
        "docker system prune",
        "docker image prune",
        "docker compose down",
        "nginx",
        "systemctl",
        "rsync",
        "ssh ",
        "flico-voice-agent up",
        "kavya-smartpbx flico",
    ):
        assert forbidden not in script.lower()


def test_smartpbx_image_deploy_helper_rolls_back_exact_local_baseline_without_sensitive_output():
    script = read_smartpbx_image_deploy_script()

    assert "rollback()" in script
    assert "TAG=$ROLLBACK_TAG" in script
    assert "wait_for_smartpbx_ready" in script
    assert "SMARTPBX_ROLLBACK_ESCALATION_REQUIRED" in script
    assert "return 1" in script[script.find("rollback()"):]
    for forbidden in (
        "cat .env",
        "printenv",
        "docker compose config$",
        "curl http://127.0.0.1:8006/smartpbx/status",
        "docker compose logs",
    ):
        assert forbidden not in script


def test_smartpbx_runbook_uses_the_guarded_smartpbx_image_deploy_helper():
    runbook = read_text("SMARTPBX_RUNBOOK.md")

    assert "deploy_smartpbx_image.sh" in runbook
    assert "NEW_TAG EXPECTED_SHA EXPECTED_DIGEST" in runbook
    assert "authenticated integration probe" in runbook



def test_smartpbx_image_deploy_functions_fail_closed_under_fake_path(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "curl").write_text(
        "#!/usr/bin/env bash\n"
        "if [[ $FAKE_HEALTH_FAIL == 1 && $* == *health* ]]; then exit 1; fi\n"
        "echo status\n",
        encoding="utf-8",
    )
    (fake_bin / "jq").write_text("#!/usr/bin/env bash\nexec /usr/bin/jq \"$@\"\n", encoding="utf-8")
    for command in (fake_bin / "curl", fake_bin / "jq"):
        command.chmod(0o755)
    env = os.environ | {"PATH": str(fake_bin) + ":" + os.environ["PATH"], "FAKE_HEALTH_FAIL": "1"}
    source = f"source {SMARTPBX_IMAGE_DEPLOY_SCRIPT}; check_loopback_preflight"
    health = subprocess.run(["bash", "-c", source], env=env, text=True, capture_output=True, check=False)
    assert health.returncode != 0
    bad_sha = "f" * 40
    bad_digest = "a" * 64

    unrelated = subprocess.run(
        ["bash", "-c", f"source {SMARTPBX_IMAGE_DEPLOY_SCRIPT}; validate_inputs deadbee {bad_sha} sha256:{bad_digest}"],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert unrelated.returncode != 0


@pytest.mark.parametrize("arguments", [(), ("deadbee",), ("deadbee", "f" * 40), ("deadbee", "f" * 40, "sha256:" + "a" * 64, "extra")])
def test_deploy_helper_requires_exactly_three_arguments_before_mutation(tmp_path, arguments):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log = tmp_path / "commands"
    for name in ("docker", "curl", "jq", "flock", "stat"):
        command = fake_bin / name
        command.write_text("#!/usr/bin/env bash\nprintf '%s\\n' \"$0 $*\" >> \"$FAKE_LOG\"\nexit 0\n", encoding="utf-8")
        command.chmod(0o755)
    env = os.environ | {"PATH": str(fake_bin) + ":" + os.environ["PATH"], "FAKE_LOG": str(log)}
    command = "source %s; main %s" % (SMARTPBX_IMAGE_DEPLOY_SCRIPT, " ".join(arguments))
    result = subprocess.run(["bash", "-c", command], env=env, text=True, capture_output=True, check=False)
    assert result.returncode != 0
    assert not log.exists() or " compose " not in log.read_text(encoding="utf-8")
