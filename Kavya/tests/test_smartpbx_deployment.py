"""Static deployment contracts for the isolated Dialog SmartPBX service."""

import os
from pathlib import Path
import shutil
import subprocess
import re
import json
import textwrap
import signal
import time

import pytest
import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def read_text(name: str) -> str:
    return (PROJECT_ROOT / name).read_text(encoding="utf-8")


def active_env_assignments(text: str, name: str) -> list[str]:
    """Return active values for one dotenv key, ignoring blank/comment lines."""
    values = []
    for line in text.splitlines():
        active = line.strip()
        if not active or active.startswith("#") or "=" not in active:
            continue
        key, value = active.split("=", 1)
        if key == name:
            values.append(value)
    return values


def assert_exactly_one_blank_env_assignment(text: str, name: str) -> None:
    assert active_env_assignments(text, name) == [""], (
        f"{name} must appear exactly once as an active blank assignment"
    )


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


def test_startup_preroll_is_explicitly_default_off_and_has_a_documented_two_second_canary_rollback():
    compose = yaml.safe_load(read_text("docker-compose.yml"))
    environment = compose["services"]["kavya-smartpbx"]["environment"]
    example = read_text(".env.example")
    runbook = read_text("SMARTPBX_RUNBOOK.md")

    assert environment["SMARTPBX_STARTUP_PREROLL_MS"] == "${SMARTPBX_STARTUP_PREROLL_MS:-0}"
    assert re.search(r"^SMARTPBX_STARTUP_PREROLL_MS=0$", example, re.MULTILINE)
    assert "20 ms multiples in [0, 2000]" in example
    assert "SMARTPBX_STARTUP_PREROLL_MS=2000" in runbook
    assert "SMARTPBX_STARTUP_PREROLL_MS=100" in runbook
    assert "SMARTPBX_STARTUP_PREROLL_MS=0" in runbook
    assert "not an Opus migration" in runbook


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
    # Unauthenticated connect/close cycling floods the diagnostic log ring with
    # one JSON line per rejection and no nginx record of the source.
    assert "access_log off;" not in nginx
    assert "access_log /var/log/nginx/smartpbx-kavya-access.log" in nginx
    assert "limit_req_zone $binary_remote_addr zone=kavya_smartpbx_req:10m rate=30r/m;" in nginx
    media = nginx.split("location = /ws/v1/smartpbx/media", 1)[1].split("location =", 1)[0]
    assert "limit_req zone=kavya_smartpbx_req burst=10 nodelay;" in media
    assert "limit_req_status 429;" in nginx
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
    assert "finite approved event allowlist" in normalized_diagnostics
    # This is the complete direct-SmartPBX runtime taxonomy, not the obsolete
    # seven-event subset. Keeping the list here makes a new emitted event a
    # conscious privacy/runbook decision rather than undocumented log drift.
    for event_name in (
        "smartpbx_protocol_diagnostic",
        "stt_digit_class_state",
        "stt_interim_shape",
        "turn_stage",
        "turn_summary",
        "session_summary",
        "echo_rejected",
        "agent_response",
        "assistant_turn_delivery",
        "audio_dump_written",
        "bad_tool_json",
        "llm_round",
        "llm_round_complete",
        "llm_empty_response",
        "llm_error",
        "llm_provider_degraded",
        "llm_provider_failover",
        "tool_execute",
        "tool_result",
        "tool_error",
        "tool_batch",
        "tool_round_limit",
        "tts_failure",
        "tts_interrupted",
        "barge_in",
        "guest_utterance",
        "kb_error",
        "silence_reprompt",
        "stt_final",
        "stt_post_dispatch_result",
        "stt_provider_final",
        "stt_provider_interim",
        "capture_buffer_bounded",
        "capture_final_buffered",
        "capture_deferred_rearm",
        "capture_forced_dispatch",
        "stt_callback_drain",
        "capture_mode_enter",
        "capture_mode_exit",
        "dtmf_collect_start",
        "dtmf_collect_done",
    ):
        assert f"`{event_name}`" in diagnostics
    assert "unlisted event names are not permitted" in normalized_diagnostics
    for field in (
        "correlation_id",
        "stage",
        "outcome",
        "failure_class",
        "active_sessions",
        "duration_ms",
        "turn_id",
        "session_trace_id",
        "provider",
    ):
        assert f"`{field}`" in diagnostics
    assert "opaque, local, randomly generated" in normalized_diagnostics
    assert "never derived from dialog" in normalized_diagnostics
    assert "only seven" not in normalized_diagnostics
    assert "bounded provider enum" in normalized_diagnostics
    for provider in ("openai", "gemini", "claude", "elevenlabs", "azure"):
        assert f"`{provider}`" in diagnostics
    assert (
        "must not contain transcript text, audio, call ids, exception bodies, or secrets"
        in normalized_diagnostics
    )

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


def test_new_smartpbx_caller_rhythm_knobs_are_blank_safe_passthroughs():
    compose = yaml.safe_load(read_text("docker-compose.yml"))
    smartpbx_env = compose["services"]["kavya-smartpbx"]["environment"]

    assert smartpbx_env.get("SMARTPBX_MAX_TOKENS") == "${SMARTPBX_MAX_TOKENS:-}"
    assert (
        smartpbx_env.get("SMARTPBX_INITIAL_FILLER_DELAY_SECONDS")
        == "${SMARTPBX_INITIAL_FILLER_DELAY_SECONDS:-}"
    )

    example = read_text(".env.example")
    assert re.search(r"^SMARTPBX_MAX_TOKENS=$", example, re.MULTILINE)
    assert re.search(
        r"^SMARTPBX_INITIAL_FILLER_DELAY_SECONDS=$", example, re.MULTILINE
    )
    assert re.search(r"^SMARTPBX_MAX_TOKENS=\d", example, re.MULTILINE) is None
    assert re.search(
        r"^SMARTPBX_INITIAL_FILLER_DELAY_SECONDS=\d", example, re.MULTILINE
    ) is None


def test_claude_output_budget_knob_is_a_blank_safe_passthrough_everywhere():
    """The Claude-only canary budget follows the same blank-safe convention as
    the shared one: allowlisted in the kavya-smartpbx service, present but
    unset in both templates, so a deploy inherits the in-code default rather
    than a stale pinned number."""
    compose = yaml.safe_load(read_text("docker-compose.yml"))
    smartpbx_env = compose["services"]["kavya-smartpbx"]["environment"]

    assert (
        smartpbx_env.get("SMARTPBX_CLAUDE_MAX_TOKENS")
        == "${SMARTPBX_CLAUDE_MAX_TOKENS:-}"
    )

    example = read_text(".env.example")
    assert re.search(r"^SMARTPBX_CLAUDE_MAX_TOKENS=$", example, re.MULTILINE)
    assert re.search(r"^SMARTPBX_CLAUDE_MAX_TOKENS=\d", example, re.MULTILINE) is None

    runbook = read_text("SMARTPBX_RUNBOOK.md")
    template = runbook.split("```dotenv", 1)[1].split("```", 1)[0]
    assert re.search(r"^SMARTPBX_CLAUDE_MAX_TOKENS=$", template, re.MULTILINE)
    assert re.search(r"^SMARTPBX_CLAUDE_MAX_TOKENS=\d", template, re.MULTILINE) is None

    # The knob is Claude-only by documentation as well as by code.
    assert "SMARTPBX_CLAUDE_MAX_TOKENS" in runbook


def test_runbook_smartpbx_template_keeps_caller_rhythm_knobs_blank_and_private():
    runbook = read_text("SMARTPBX_RUNBOOK.md")
    template = runbook.split("```dotenv", 1)[1].split("```", 1)[0]

    assert re.search(r"^SMARTPBX_MAX_TOKENS=$", template, re.MULTILINE)
    assert re.search(
        r"^SMARTPBX_INITIAL_FILLER_DELAY_SECONDS=$", template, re.MULTILINE
    )
    assert re.search(r"^SMARTPBX_MAX_TOKENS=\d", template, re.MULTILINE) is None
    assert re.search(
        r"^SMARTPBX_INITIAL_FILLER_DELAY_SECONDS=\d", template, re.MULTILINE
    ) is None
    assert "$(" not in template
    assert "`" not in template


def test_smartpbx_stream_timeout_knobs_are_blank_safe_and_documented():
    # Phase B: the direct SmartPBX timeout knobs must be deliverable through
    # the smartpbx service's explicit environment allowlist (not env_file),
    # blank in .env.example/the runbook template (resolved in code), and
    # documented in the runbook narrative. Claude's thinking grace defaults
    # to 12.0s and is still subject to the effective general-stall maximum.
    compose = yaml.safe_load(read_text("docker-compose.yml"))
    smartpbx_env = compose["services"]["kavya-smartpbx"]["environment"]

    for name in (
        "SMARTPBX_LLM_INITIAL_RESPONSE_TIMEOUT_SECONDS",
        "SMARTPBX_LLM_STALL_TIMEOUT_SECONDS",
        "SMARTPBX_CLAUDE_THINKING_STALL_TIMEOUT_SECONDS",
    ):
        assert smartpbx_env.get(name) == f"${{{name}:-}}", (
            f"{name} must be in the smartpbx environment allowlist to reach the container"
        )

    example = read_text(".env.example")
    for name in (
        "SMARTPBX_LLM_INITIAL_RESPONSE_TIMEOUT_SECONDS",
        "SMARTPBX_LLM_STALL_TIMEOUT_SECONDS",
        "SMARTPBX_CLAUDE_THINKING_STALL_TIMEOUT_SECONDS",
    ):
        assert re.search(rf"^{name}=$", example, re.MULTILINE)
        assert re.search(rf"^{name}=\d", example, re.MULTILINE) is None

    runbook = read_text("SMARTPBX_RUNBOOK.md")
    template = runbook.split("```dotenv", 1)[1].split("```", 1)[0]
    for name in (
        "SMARTPBX_LLM_INITIAL_RESPONSE_TIMEOUT_SECONDS",
        "SMARTPBX_LLM_STALL_TIMEOUT_SECONDS",
        "SMARTPBX_CLAUDE_THINKING_STALL_TIMEOUT_SECONDS",
    ):
        assert re.search(rf"^{name}=$", template, re.MULTILINE)
        assert re.search(rf"^{name}=\d", template, re.MULTILINE) is None

    assert "Direct SmartPBX English reliability timing" in runbook
    assert "Twilio Media Streams" in runbook.split(
        "## Direct SmartPBX English reliability timing", 1
    )[1][:600]
    timing = runbook.split("## Direct SmartPBX English reliability timing", 1)[1].split(
        "\n## ", 1
    )[0]
    assert "does not alter the separately configured Claude output ceiling" in timing
    assert "output ceiling remains `1024`" not in timing

    budget = runbook.split("## Claude direct SmartPBX output budget", 1)[1].split(
        "\n## ", 1
    )[0]
    assert "600" in budget


def test_runbook_allowlists_llm_round_outcome_with_its_exact_bounded_field_set():
    """A new emitted event is only permitted once it is in the finite approved
    allowlist WITH its closed field set. Documenting the field set is the whole
    point — `stop_reason` used to be a raw provider string, which is an open
    text channel dressed as telemetry."""
    runbook = read_text("SMARTPBX_RUNBOOK.md")
    cutover = runbook.split("## Cutover gates", 1)[1].split("\n## ", 1)[0]

    assert "`llm_round_outcome`" in cutover
    for field in ("provider", "outcome", "stop_reason", "output_tokens", "attempt"):
        assert f"`{field}`" in cutover, field
    # The complete outcome vocabulary, including the abrupt-EOF member.
    for outcome in (
        "completed",
        "max_tokens_truncated",
        "true_empty",
        "incomplete_tool_block",
        "malformed_tool_json",
        "stream_aborted",
    ):
        assert outcome in cutover, outcome
    # The complete stop_reason vocabulary plus both catch-all buckets.
    for stop_reason in (
        "end_turn",
        "max_tokens",
        "tool_use",
        "stop_sequence",
        "refusal",
        "unknown",
    ):
        assert stop_reason in cutover, stop_reason
    normalized_cutover = re.sub(r"\s+", " ", cutover).lower()
    assert "the raw provider string is never emitted" in normalized_cutover
    assert "clamped" in normalized_cutover


def test_runbook_allowlists_stt_post_dispatch_result_with_its_exact_bounded_field_set():
    """The late-STT-result event is the only place the pipeline reports that it
    threw a provider result away. It must stay a closed enum pair plus one
    clamped integer — the tempting fields (what was said, how long it was) are
    exactly the ones that would turn it into a transcript side channel — and its
    documented meaning must not over-claim: it cannot separate a provider tail
    from an immediate caller repetition, because no provider result identity
    reaches this pipeline."""
    runbook = read_text("SMARTPBX_RUNBOOK.md")
    cutover = runbook.split("## Cutover gates", 1)[1].split("\n## ", 1)[0]

    assert "`stt_post_dispatch_result`" in cutover
    assert "emits exactly three fields, and no others" in cutover
    for field in ("result_type", "action", "elapsed_ms"):
        assert f"`{field}`" in cutover, field
    for value in ("`final`", "`interim`", "`ignored_active_turn`", "`0`–`60000`"):
        assert value in cutover, value

    normalized = re.sub(r"\s+", " ", cutover).lower()
    assert "no transcript text" in normalized
    assert "no character or token counts" in normalized
    assert "no phone or call identifier" in normalized
    # A barge-in must not be miscounted here, or the operator reads the metric
    # as "missed interruptions" and retunes endpointing for the wrong reason.
    assert "genuine barge-in" in normalized
    assert "never reaches this event" in normalized

    # The event fires only for results with no material characters. The runbook
    # must say so, must say that everything else is admitted, and must state
    # plainly that this signal cannot prove tail-vs-repetition — an operator who
    # believes it can will retune endpointing on a number that does not carry
    # that information.
    assert "no material characters" in normalized
    assert "admitted" in normalized
    assert (
        "cannot prove whether a result was a provider tail or the caller "
        "repeating themselves" in normalized
    )
    assert "provider result identity" in normalized
    assert "never reaches this event" in normalized
    # The withdrawn predicate must not be described as live policy anywhere.
    assert "proven to be that turn's own tail" not in normalized
    assert "means late duplicate provider results" not in normalized
    assert "cannot distinguish a delayed tail" not in normalized
    assert "no packet-loss diagnosis" in normalized
    assert "no provider-duplication diagnosis" in normalized
    assert "undetermined" in normalized
    # And it is not a lost-speech metric: a refused result had no speech in it.
    assert "not evidence of lost caller speech" in normalized

    # Admitted speech is owned at every boundary, and the runbook must name the
    # event and the closed reason set that report the retention.
    assert "`capture_forced_dispatch`" in cutover
    for reason in ("`barge_in`", "`transfer`", "`transfer_flush`", "`session_end`", "`hangup`"):
        assert reason in cutover, reason
    assert "no boundary drops the buffer silently" in normalized

    # Volume is bounded per turn, and the per-turn totals ride the already
    # allowlisted turn_summary rather than one INFO line per ignored result.
    assert "at most once per `result_type` per owning turn" in normalized
    for field in (
        "ignored_post_dispatch_finals",
        "ignored_post_dispatch_interims",
        "ignored_post_dispatch_max_elapsed_ms",
    ):
        assert f"`{field}`" in cutover, field
    assert "`0`–`100000`" in cutover
    assert "absent from the summary" in normalized


def test_runbook_names_the_sonnet_5_canary_model_and_the_600_budget_rationale():
    """The runbook template used to hand operators `claude-sonnet-4-5-20250929`
    for `.env.smartpbx` while the canary and prod ran `claude-sonnet-5` — and
    the whole reason the Claude budget is 600 is Sonnet 5's default-on adaptive
    thinking. Resolve both in one place so the docs stop contradicting the
    deployment."""
    runbook = read_text("SMARTPBX_RUNBOOK.md")
    template = runbook.split("```dotenv", 1)[1].split("```", 1)[0]
    budget = runbook.split(
        "## Claude direct SmartPBX output budget", 1
    )[1].split("\n## ", 1)[0]
    normalized_budget = re.sub(r"\s+", " ", budget).lower()

    assert re.search(r"^CLAUDE_MODEL=claude-sonnet-5$", template, re.MULTILINE)
    assert "claude-sonnet-4-5-20250929" not in template

    assert "claude-sonnet-5" in budget
    # Thinking is default-on and kept on deliberately — never disabled anywhere.
    assert "adaptive thinking on by default" in normalized_budget
    assert "kept" in normalized_budget and "deliberately" in normalized_budget
    # The budget rationale: thinking AND a full tool block must both fit.
    assert "thinking block and a full tool block must both fit" in normalized_budget
    assert "600" in budget
    # The 4.5 default is explained, not silently contradicted.
    assert "claude-sonnet-4-5-20250929" in budget


def test_env_example_explains_the_smartpbx_model_and_budget_divergence():
    example = read_text(".env.example")

    claude_block = example.split("# === Claude API ===", 1)[1].split("# === ", 1)[0]
    assert "claude-sonnet-5" in claude_block
    assert "SMARTPBX_CLAUDE_MAX_TOKENS=600" in claude_block
    # The Twilio-path pin itself is unchanged.
    assert re.search(
        r"^CLAUDE_MODEL=claude-sonnet-4-5-20250929$", example, re.MULTILINE
    )


def test_runbook_allows_only_privacy_safe_telemetry_and_bounded_local_retention():
    runbook = read_text("SMARTPBX_RUNBOOK.md")
    normalized = re.sub(r"\s+", " ", runbook).lower()

    assert "finite approved event allowlist" in normalized
    assert "json-file" in normalized
    assert 'max-size: "10m"' in runbook
    assert 'max-file: "3"' in runbook
    assert "30 mb maximum" in normalized
    assert "aggregate-only" in normalized
    assert "do not export raw logs" in normalized
    assert "wire-delivery proxies" in normalized
    assert "session_trace_id" in normalized
    assert "bounded provider enum" in normalized
    assert "must not contain transcript text, audio, call ids, exception bodies, or secrets" in normalized


def test_pr_impact_keeps_kavya_out_of_generic_deploy_and_gives_its_real_rollback_path():
    workflow = (
        PROJECT_ROOT.parent / ".github" / "workflows" / "pr-deploy-impact.yml"
    ).read_text(encoding="utf-8")

    assert "kavya=$kavya" in workflow
    assert 'KAVYA: ${{ steps.detect.outputs.kavya }}' in workflow
    assert "Merging this PR does not deploy Kavya." in workflow
    assert "Kavya SmartPBX rollback" in workflow
    assert "SMARTPBX_RUNBOOK.md" in workflow
    assert "deploy_smartpbx_image.sh" in workflow
    assert (
        "do not call generic `deploy.yml` with `agent=kavya` and `mode=image`"
        in workflow
    )
    assert "# registry agents (all seven active ones)" not in workflow


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


KAVYA_IMAGE_PROBE_WORKFLOW = PROJECT_ROOT.parent / ".github/workflows/probe-kavya-image.yml"
KAVYA_PROBE_SPEC = (
    PROJECT_ROOT.parent
    / "docs/superpowers/specs/2026-08-08-kavya-ghcr-read-only-probe-design.md"
)


def read_kavya_image_probe_workflow():
    assert KAVYA_IMAGE_PROBE_WORKFLOW.is_file(), "missing read-only Kavya image probe workflow"
    text = KAVYA_IMAGE_PROBE_WORKFLOW.read_text(encoding="utf-8")
    document = yaml.load(text, Loader=yaml.BaseLoader)
    assert isinstance(document, dict)
    return document, text


def kavya_image_probe_job():
    document, text = read_kavya_image_probe_workflow()
    jobs = document.get("jobs", {})
    assert set(jobs) == {"probe"}
    job = jobs["probe"]
    return document, job, job.get("steps", []), text


def probe_workflow_step(steps, name):
    step = next((step for step in steps if step.get("name") == name), None)
    assert step is not None, f"missing probe workflow step: {name}"
    return step


# The four full commit pins the finalized probe design fixes for both workflows,
# with the version comment each `uses:` line must retain.
KAVYA_WORKFLOW_ACTION_PINS = {
    "actions/checkout": ("3d3c42e5aac5ba805825da76410c181273ba90b1", "v7"),
    "docker/setup-buildx-action": ("bb05f3f5519dd87d3ba754cc423b652a5edd6d2c", "v4"),
    "docker/login-action": ("dbcb813823bdd20940b903addbd779551569679f", "v4"),
    "docker/build-push-action": ("53b7df96c91f9c12dcc8a07bcb9ccacbed38856a", "v7"),
}


def workflow_uses_strings(value):
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "uses" and isinstance(child, str):
                yield child
            yield from workflow_uses_strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from workflow_uses_strings(child)


@pytest.mark.parametrize(
    "workflow", [KAVYA_IMAGE_PROBE_WORKFLOW, BUILD_KAVYA_IMAGE_WORKFLOW], ids=["probe", "publisher"]
)
def test_kavya_image_probe_and_publisher_pin_actions_to_full_commit_shas(workflow):
    text = workflow.read_text(encoding="utf-8")
    document = yaml.load(text, Loader=yaml.BaseLoader)
    references = list(workflow_uses_strings(document))

    assert references, "workflow declares no actions"
    for reference in references:
        assert re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", reference), reference
        owner_repo, _, sha = reference.partition("@")
        assert owner_repo in KAVYA_WORKFLOW_ACTION_PINS, owner_repo
        expected_sha, expected_version = KAVYA_WORKFLOW_ACTION_PINS[owner_repo]
        assert sha == expected_sha, reference
        assert f"uses: {reference} # {expected_version}\n" in text, reference


def test_kavya_image_probe_and_publisher_fix_the_runner_label_and_job_timeout():
    _probe_document, probe_job, _probe_steps, _probe_text = kavya_image_probe_job()
    _build_document, build_job, _build_steps, _build_text = build_kavya_image_job()

    for job in (probe_job, build_job):
        assert job["runs-on"] == "ubuntu-24.04"
        assert job["timeout-minutes"] == "30"


PROBE_REPOSITORY = "taskforce-ai-dev/full-voice-agent"
PROBE_WORKFLOW_REF = f"{PROBE_REPOSITORY}/.github/workflows/probe-kavya-image.yml@refs/heads/main"
PROBE_VALIDATION_STEP = "Validate dispatch trust and payload"
PROBE_EXISTING_TAG = "37bfaf0"
PROBE_EXPECTED_REVISION = "37bfaf02f04ce7614b9674b1c867b78ab3c7d414"


def test_kavya_image_probe_workflow_has_read_only_dispatch_trust_contract():
    document, job, steps, _text = kavya_image_probe_job()

    assert document["name"] == "Probe Kavya image (read-only)"
    assert document["on"] == {"repository_dispatch": {"types": ["kavya_image_read_only_probe"]}}
    assert document["concurrency"] == {"group": "kavya-image-read-only-probe", "cancel-in-progress": "false"}
    assert job["permissions"] == {"contents": "read", "packages": "read"}
    assert "environment" not in job

    checkout = probe_workflow_step(steps, "Checkout trusted probe tooling")
    assert checkout["uses"] == "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1"
    assert checkout["with"] == {
        "ref": "${{ github.workflow_sha }}",
        "path": ".probe-tools",
        "persist-credentials": "false",
    }
    assert (
        probe_workflow_step(steps, "Set up Buildx")["uses"]
        == "docker/setup-buildx-action@bb05f3f5519dd87d3ba754cc423b652a5edd6d2c"
    )
    assert (
        probe_workflow_step(steps, "Log in to GHCR")["uses"]
        == "docker/login-action@dbcb813823bdd20940b903addbd779551569679f"
    )


def test_kavya_image_probe_binds_execution_to_protected_default_branch():
    document, _job, steps, text = kavya_image_probe_job()
    validation = probe_workflow_step(steps, PROBE_VALIDATION_STEP)

    assert "workflow_dispatch" not in text
    assert "inputs." not in text
    # The repository_dispatch webhook exposes github.event.branch, but GitHub
    # documents no selection or protection semantics for it, so the workflow must
    # bind on the documented ref/workflow contexts instead.
    assert "github.event.branch" not in text
    assert validation["env"] == {
        "PROBE_EVENT_NAME": "${{ github.event_name }}",
        "PROBE_EVENT_ACTION": "${{ github.event.action }}",
        "PROBE_REPOSITORY": "${{ github.repository }}",
        "PROBE_EVENT_REPOSITORY": "${{ github.event.repository.full_name }}",
        "PROBE_DEFAULT_BRANCH": "${{ github.event.repository.default_branch }}",
        "PROBE_REF": "${{ github.ref }}",
        "PROBE_REF_NAME": "${{ github.ref_name }}",
        "PROBE_REF_TYPE": "${{ github.ref_type }}",
        "PROBE_REF_PROTECTED": "${{ github.ref_protected }}",
        "PROBE_SHA": "${{ github.sha }}",
        "PROBE_WORKFLOW_SHA": "${{ github.workflow_sha }}",
        "PROBE_WORKFLOW_REF": "${{ github.workflow_ref }}",
        "PROBE_EVENT": "${{ toJSON(github.event) }}",
    }
    for check in (
        "[[ \"$PROBE_EVENT_NAME\" == 'repository_dispatch' ]] || fail",
        "[[ \"$PROBE_EVENT_ACTION\" == 'kavya_image_read_only_probe' ]] || fail",
        '[[ "$PROBE_REPOSITORY" == "$repository" ]] || fail',
        '[[ "$PROBE_EVENT_REPOSITORY" == "$repository" ]] || fail',
        "[[ \"$PROBE_DEFAULT_BRANCH\" == 'main' ]] || fail",
        "[[ \"$PROBE_REF\" == 'refs/heads/main' ]] || fail",
        "[[ \"$PROBE_REF_NAME\" == 'main' ]] || fail",
        "[[ \"$PROBE_REF_TYPE\" == 'branch' ]] || fail",
        "[[ \"$PROBE_REF_PROTECTED\" == 'true' ]] || fail",
        '[[ "$PROBE_SHA" =~ ^[0-9a-f]{40}$ ]] || fail',
        '[[ "$PROBE_WORKFLOW_SHA" =~ ^[0-9a-f]{40}$ ]] || fail',
        '[[ "$PROBE_SHA" == "$PROBE_WORKFLOW_SHA" ]] || fail',
        '[[ "$PROBE_WORKFLOW_REF" == "$repository/.github/workflows/probe-kavya-image.yml@refs/heads/main" ]] || fail',
        f"repository='{PROBE_REPOSITORY}'",
    ):
        assert check in validation["run"], check
    assert all("${{" not in run for run in workflow_run_strings(document))


def test_kavya_image_probe_dispatcher_is_terminal_and_sends_no_ref_selector():
    spec = KAVYA_PROBE_SPEC.read_text(encoding="utf-8")
    blocks = re.findall(r"```bash\n(.*?)```", spec, flags=re.DOTALL)
    dispatcher = next((block for block in blocks if "gh api" in block), None)

    assert dispatcher is not None, "spec documents no dispatcher command"
    assert "set -Eeuo pipefail" in dispatcher
    assert "-f event_type=kavya_image_read_only_probe" in dispatcher
    assert "client_payload[existing_tag]" in dispatcher
    assert "client_payload[expected_revision]" in dispatcher
    for selector in ("--ref", "-f ref=", "gh workflow run", "workflow_dispatch", "sha="):
        assert selector not in dispatcher
    body, _, tail = dispatcher.partition("gh api")
    assert tail
    for guard in (
        '[[ "$existing_tag" =~ ^[0-9a-f]{7}$ ]] || fail',
        '[[ "$expected_revision" =~ ^[0-9a-f]{40}$ ]] || fail',
        '[[ "$existing_tag" == "${expected_revision:0:7}" ]] || fail',
    ):
        assert guard in body, guard


def test_kavya_image_probe_validation_precedes_all_tooling_and_has_no_source_checkout():
    document, _job, steps, text = kavya_image_probe_job()
    validation = probe_workflow_step(steps, PROBE_VALIDATION_STEP)
    checkout = probe_workflow_step(steps, "Checkout trusted probe tooling")
    buildx = probe_workflow_step(steps, "Set up Buildx")
    login = probe_workflow_step(steps, "Log in to GHCR")

    assert steps.index(validation) == 0
    assert steps.index(validation) < steps.index(checkout) < steps.index(buildx) < steps.index(login)
    assert validation["id"] == "validate"
    assert "uses" not in validation
    assert all(steps.index(step) > 0 for step in steps if "uses" in step)
    for command in ("docker ", "check-kavya-image-tag.sh", "curl ", "wget "):
        assert command not in validation["run"]
    assert '[[ "$existing_tag" =~ ^[0-9a-f]{7}$ ]] || fail' in validation["run"]
    assert '[[ "$expected_revision" =~ ^[0-9a-f]{40}$ ]] || fail' in validation["run"]
    assert '[[ "$existing_tag" == "${expected_revision:0:7}" ]] || fail' in validation["run"]
    assert all(step.get("with", {}).get("path") != "source" for step in steps if isinstance(step, dict))
    assert "Checkout reviewed source" not in text


def test_kavya_image_probe_requires_exact_states_provenance_and_internal_canary():
    document, _job, steps, text = kavya_image_probe_job()
    existing = probe_workflow_step(steps, "Probe known existing tag")
    verify = probe_workflow_step(steps, "Verify existing OCI revision")
    canary = probe_workflow_step(steps, "Probe generated absent canary")
    metadata = probe_workflow_step(steps, "Record safe runtime metadata")
    summary = probe_workflow_step(steps, "Write safe probe summary")

    assert KAVYA_IMAGE_TAG_PROBE.is_file()
    assert ".probe-tools/.github/scripts/check-kavya-image-tag.sh" in existing["run"]
    assert "probe_code" in existing["run"]
    assert '[[ "$probe_code" -eq 10 ]]' in existing["run"]
    assert 'image="ghcr.io/taskforce-ai-dev/kavya"' in existing["run"]
    assert '[[ "$probe_code" -eq 0 ]]' in canary["run"]
    assert 'canary="probe-${repository_id}-${run_id}-${attempt}"' in canary["run"]
    assert '[[ "$canary" =~ ^probe-[0-9]+-[0-9]+-[0-9]+$ ]]' in canary["run"]

    # Contract bytes are compared byte-for-byte against printf-written files; no
    # command substitution or line counting may stand in for that comparison.
    for step, expected in (
        (existing, "printf 'image_tag_state=existing\\n' > \"$expected_stdout\" || fail"),
        (canary, "printf 'image_tag_state=absent\\n' > \"$expected_stdout\" || fail"),
        (verify, "printf '%s\\n' \"$EXPECTED_REVISION\" > \"$expected_stdout\" || fail"),
    ):
        assert expected in step["run"], expected
        assert 'cmp -s "$' in step["run"]
        assert "wc -l" not in step["run"]
    # The existing-tag step branches on mode, so its comparisons gate an `if`
    # rather than trailing `|| fail`; both arms still compare byte-for-byte
    # against a printf-written file, and the else arm fails closed.
    assert 'cmp -s "$probe_stdout" "$expected_stdout"' in existing["run"]
    assert 'cmp -s "$probe_stdout" "$expected_absent"' in existing["run"]
    assert "printf 'image_tag_state=absent\\n' > \"$expected_absent\" || fail" in existing["run"]
    assert [line.strip() for line in existing["run"].strip().splitlines()][-3:] == [
        "else", "fail", "fi",
    ], "any unclassified existing-tag state must still fail closed"
    assert 'cmp -s "$probe_stdout" "$expected_stdout" || fail' in canary["run"]
    assert 'cmp -s "$inspect_stdout" "$expected_stdout" || fail' in verify["run"]
    assert all("wc -l" not in run for run in workflow_run_strings(document))

    # The tag is resolved to a digest once and both reads use that digest, so the
    # revision proven is the revision of one immutable manifest.
    assert 'docker buildx imagetools inspect "$image" --format' in verify["run"]
    assert '[[ "$digest" =~ ^sha256:[0-9a-f]{64}$ ]] || fail' in verify["run"]
    assert 'image_ref="${repository}@${digest}"' in verify["run"]
    assert 'docker pull "$image_ref" >"$pull_stdout" 2>"$pull_stderr"' in verify["run"]
    assert 'docker image inspect "$image_ref" --format' in verify["run"]

    # An unrecognised registry message is surfaced as a fixed workflow-authored
    # marker so the failed run says which remedy applies, without echoing bytes.
    for step in (existing, canary):
        assert (
            "if [[ \"$probe_code\" -eq 2 ]]; then "
            "printf '%s\\n' 'helper_result=unrecognized_registry_message' >&2; fi"
        ) in step["run"]

    assert "workflow_commit=%s" in metadata["run"]
    assert "awk 'NR==1{print $2}'" in metadata["run"]
    assert "ImageOS" not in text
    assert "ImageVersion" not in text
    for marker in ("probe_version=1", "probe_result=pass"):
        assert marker in summary["run"]
    # The per-check pass markers are emitted once, by the step that proved them.
    for marker in ("existing_tag_state=pass", "existing_revision=pass", "canary_state=pass"):
        assert marker not in summary["run"]
        assert text.count(marker) == 1
    assert "source/.github/scripts" not in text


def test_kavya_image_probe_has_no_write_deploy_or_sensitive_output_surface():
    _document, job, _steps, text = kavya_image_probe_job()
    runs = "\n".join(workflow_run_strings(kavya_image_probe_job()[0]))

    assert job["permissions"] == {"contents": "read", "packages": "read"}
    assert re.findall(r"secrets\.([A-Za-z0-9_]+)", text) == ["GITHUB_TOKEN"]
    for forbidden in ("packages: write", "docker/build-push-action@", "docker push", "docker build ", "docker tag ", "docker manifest", "docker compose", "ssh ", "rsync ", "nginx", "systemctl", "env |", "printenv", "set -x", "source/"):
        assert forbidden not in text.lower()
    assert "probe_result=fail" in runs
    for forbidden in ('echo "$GITHUB_TOKEN"', 'cat "$probe_stderr"', 'cat "$pull_stderr"', 'cat "$inspect_stderr"', "--format '{{json .}}'"):
        assert forbidden not in runs


PROBE_CANARY_DIGEST = "sha256:" + "0" * 64


def probe_event_payload(
    existing_tag=PROBE_EXISTING_TAG,
    expected_revision=PROBE_EXPECTED_REVISION,
    client_payload=None,
    branch="main",
    repository=None,
    bootstrap=None,
):
    if client_payload is None:
        client_payload = {"existing_tag": existing_tag, "expected_revision": expected_revision}
        if bootstrap is not None:
            client_payload["bootstrap"] = bootstrap
    return json.dumps(
        {
            "action": "kavya_image_read_only_probe",
            "branch": branch,
            "client_payload": client_payload,
            "repository": repository
            if repository is not None
            else {"full_name": PROBE_REPOSITORY, "default_branch": "main"},
        }
    )


def run_probe_workflow_step(tmp_path, name, output_files=None, **values):
    document, _job, steps, _text = kavya_image_probe_job()
    script = probe_workflow_step(steps, name)["run"]
    scripts = tmp_path / ".probe-tools" / ".github" / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    log = tmp_path / "tools.log"
    summary = tmp_path / "summary"
    outputs = tmp_path / "outputs"
    (scripts / "check-kavya-image-tag.sh").write_text(textwrap.dedent("""\
        #!/usr/bin/env bash
        set -Eeuo pipefail
        printf 'probe %s\\n' "$1" >> "$FAKE_LOG"
        if [[ "$1" == *":probe-"* ]]; then
          if [[ -n "${CANARY_OUT_FILE:-}" ]]; then cat "$CANARY_OUT_FILE"; else printf '%s' "$CANARY_OUT"; fi
          printf '%s' "$CANARY_ERR" >&2; exit "$CANARY_CODE"
        fi
        if [[ -n "${EXISTING_OUT_FILE:-}" ]]; then cat "$EXISTING_OUT_FILE"; else printf '%s' "$EXISTING_OUT"; fi
        printf '%s' "$EXISTING_ERR" >&2; exit "$EXISTING_CODE"
    """), encoding="utf-8")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(exist_ok=True)
    docker = fake_bin / "docker"
    docker.write_text(textwrap.dedent("""\
        #!/usr/bin/env bash
        set -Eeuo pipefail
        printf 'docker %s\\n' "$*" >> "$FAKE_LOG"
        # Real buildx evaluates --format against imagetools.tplInput, which has
        # .Manifest and .Image but no bare .Digest. A fake that accepts any
        # template agrees with itself and hides a broken one; this one does not.
        check_imagetools_format() {
          local fmt="" previous=""
          for argument in "$@"; do
            if [[ "$previous" == "--format" ]]; then fmt="$argument"; fi
            previous="$argument"
          done
          [[ -z "$fmt" ]] && return 0
          if [[ "$fmt" != *".Manifest"* && "$fmt" != *".Image"* ]]; then
            printf 'ERROR: template: :1:2: executing "" at <%s>: cannot evaluate field in type imagetools.tplInput\\n' "$fmt" >&2
            return 1
          fi
          return 0
        }
        case "$1 ${2:-} ${3:-}" in
          "buildx version ") printf 'github.com/docker/buildx %s\\n' "$BUILDX_VERSION"; exit "$BUILDX_CODE" ;;
          "buildx imagetools inspect")
            check_imagetools_format "$@" || exit 1
            if [[ -n "${DIGEST_OUT_FILE:-}" ]]; then cat "$DIGEST_OUT_FILE"; else printf '%s' "$DIGEST_OUT"; fi
            printf '%s' "$DIGEST_ERR" >&2; exit "$DIGEST_CODE" ;;
        esac
        if [[ "$1" == pull ]]; then printf '%s' "$PULL_OUT"; printf '%s' "$PULL_ERR" >&2; exit "$PULL_CODE"; fi
        if [[ "$1 ${2:-}" == "image inspect" ]]; then
          if [[ -n "${INSPECT_OUT_FILE:-}" ]]; then cat "$INSPECT_OUT_FILE"; else printf '%s' "$INSPECT_OUT"; fi
          printf '%s' "$INSPECT_ERR" >&2; exit "$INSPECT_CODE"
        fi
        exit 97
    """), encoding="utf-8")
    docker.chmod(0o755)
    env = os.environ | {
        "PATH": f"{fake_bin}:{os.environ['PATH']}", "FAKE_LOG": str(log),
        "GITHUB_STEP_SUMMARY": str(summary), "GITHUB_OUTPUT": str(outputs),
        "PROBE_EVENT_NAME": "repository_dispatch",
        "PROBE_EVENT_ACTION": "kavya_image_read_only_probe",
        "PROBE_REPOSITORY": PROBE_REPOSITORY,
        "PROBE_EVENT_REPOSITORY": PROBE_REPOSITORY,
        "PROBE_DEFAULT_BRANCH": "main",
        "PROBE_REF": "refs/heads/main", "PROBE_REF_NAME": "main", "PROBE_REF_TYPE": "branch",
        "PROBE_REF_PROTECTED": "true",
        "PROBE_SHA": "a" * 40, "PROBE_WORKFLOW_SHA": "a" * 40,
        "PROBE_WORKFLOW_REF": PROBE_WORKFLOW_REF,
        "PROBE_EVENT": probe_event_payload(),
        "PROBE_RUNNER_LABEL": "ubuntu-24.04", "PROBE_RUNNER_OS": "Linux", "PROBE_RUNNER_ARCH": "X64",
        "EXISTING_TAG": PROBE_EXISTING_TAG, "EXPECTED_REVISION": PROBE_EXPECTED_REVISION,
        # steps.validate.outputs.bootstrap / steps.existing.outputs.existing_image
        "PROBE_BOOTSTRAP": "false", "PROBE_EXISTING_IMAGE": "present",
        "GITHUB_REPOSITORY_ID": "123", "GITHUB_RUN_ID": "456", "GITHUB_RUN_ATTEMPT": "1",
        "BUILDX_VERSION": "v0.16.2", "BUILDX_CODE": "0",
        "EXISTING_OUT": "image_tag_state=existing\n", "EXISTING_ERR": "", "EXISTING_CODE": "10",
        "CANARY_OUT": "image_tag_state=absent\n", "CANARY_ERR": "", "CANARY_CODE": "0",
        "DIGEST_OUT": f"{PROBE_CANARY_DIGEST}\n", "DIGEST_ERR": "", "DIGEST_CODE": "0",
        "PULL_OUT": "", "PULL_ERR": "", "PULL_CODE": "0",
        "INSPECT_OUT": f"{PROBE_EXPECTED_REVISION}\n", "INSPECT_ERR": "", "INSPECT_CODE": "0",
    } | {key: str(value) for key, value in values.items()}
    for index, (key, payload) in enumerate(sorted((output_files or {}).items())):
        binary = tmp_path / f"fake-output-{index}.bin"
        binary.write_bytes(payload)
        env[key] = str(binary)
    result = subprocess.run(["bash", "-c", script], cwd=tmp_path, env=env, text=True, capture_output=True, check=False)
    return result, summary.read_text(encoding="utf-8") if summary.exists() else "", log.read_text(encoding="utf-8") if log.exists() else ""

@pytest.mark.parametrize(
    ("tag", "revision", "code"),
    [
        (PROBE_EXISTING_TAG, PROBE_EXPECTED_REVISION, 0),
        ("", PROBE_EXPECTED_REVISION, 1),
        ("37BFAF0", PROBE_EXPECTED_REVISION, 1),
        ("37bfaf00", PROBE_EXPECTED_REVISION, 1),
        (PROBE_EXISTING_TAG, "37BFAF02f04ce7614b9674b1c867b78ab3c7d414", 1),
        (PROBE_EXISTING_TAG, PROBE_EXPECTED_REVISION + "0", 1),
        (PROBE_EXISTING_TAG, "47bfaf02f04ce7614b9674b1c867b78ab3c7d414", 1),
        (PROBE_EXISTING_TAG, "", 1),
    ],
)
def test_kavya_image_probe_validation_binds_identity_before_tools(tmp_path, tag, revision, code):
    result, summary, log = run_probe_workflow_step(
        tmp_path,
        PROBE_VALIDATION_STEP,
        PROBE_EVENT=probe_event_payload(existing_tag=tag, expected_revision=revision),
    )
    outputs = tmp_path / "outputs"
    assert result.returncode == code
    assert log == ""
    if code:
        assert "probe_result=pass" not in summary
        assert "probe_result=fail" in summary
        assert not outputs.exists() or outputs.read_text(encoding="utf-8") == ""
    else:
        assert outputs.read_text(encoding="utf-8").splitlines() == [
            f"existing_tag={tag}",
            f"expected_revision={revision}",
            "bootstrap=false",
        ]


PROBE_UNTRUSTED_CONTEXTS = [
    {"PROBE_EVENT_NAME": "workflow_dispatch"},
    {"PROBE_EVENT_NAME": "push"},
    {"PROBE_EVENT_ACTION": "some_other_type"},
    {"PROBE_REPOSITORY": "attacker/full-voice-agent"},
    {"PROBE_EVENT_REPOSITORY": "attacker/full-voice-agent"},
    {"PROBE_DEFAULT_BRANCH": "rakesh"},
    {"PROBE_REF": "refs/heads/rakesh"},
    {"PROBE_REF": "refs/tags/v1"},
    {"PROBE_REF_NAME": "rakesh"},
    {"PROBE_REF_TYPE": "tag"},
    {"PROBE_REF_PROTECTED": "false"},
    {"PROBE_REF_PROTECTED": ""},
    {"PROBE_SHA": "b" * 40},
    {"PROBE_SHA": "A" * 40},
    {"PROBE_SHA": "a" * 39},
    {"PROBE_WORKFLOW_SHA": "b" * 40},
    {"PROBE_WORKFLOW_SHA": "A" * 40, "PROBE_SHA": "A" * 40},
    {"PROBE_WORKFLOW_REF": f"{PROBE_REPOSITORY}/.github/workflows/probe-kavya-image.yml@refs/heads/rakesh"},
    {"PROBE_WORKFLOW_REF": f"{PROBE_REPOSITORY}/.github/workflows/other.yml@refs/heads/main"},
    {"PROBE_WORKFLOW_REF": ""},
]


@pytest.mark.parametrize("overrides", PROBE_UNTRUSTED_CONTEXTS, ids=range(len(PROBE_UNTRUSTED_CONTEXTS)))
def test_kavya_image_probe_rejects_untrusted_dispatch_context(tmp_path, overrides):
    result, summary, log = run_probe_workflow_step(tmp_path, PROBE_VALIDATION_STEP, **overrides)
    outputs = tmp_path / "outputs"

    assert result.returncode == 1
    assert log == "", "a trust failure must precede every registry or tooling command"
    assert "probe_result=fail" in summary
    assert "probe_result=pass" not in summary
    assert not outputs.exists() or outputs.read_text(encoding="utf-8") == ""


@pytest.mark.parametrize(
    "payload",
    [
        {"existing_tag": PROBE_EXISTING_TAG},
        {"expected_revision": PROBE_EXPECTED_REVISION},
        {},
        {"existing_tag": PROBE_EXISTING_TAG, "expected_revision": PROBE_EXPECTED_REVISION, "ref": "rakesh"},
        {"existing_tag": PROBE_EXISTING_TAG, "expected_revision": None},
        {"existing_tag": ["37bfaf0"], "expected_revision": PROBE_EXPECTED_REVISION},
        {"existing_tag": PROBE_EXISTING_TAG, "expected_revision": {"value": PROBE_EXPECTED_REVISION}},
    ],
)
def test_kavya_image_probe_payload_is_exactly_two_validated_string_keys(tmp_path, payload):
    result, summary, log = run_probe_workflow_step(
        tmp_path, PROBE_VALIDATION_STEP, PROBE_EVENT=probe_event_payload(client_payload=payload)
    )

    assert result.returncode == 1
    assert log == ""
    assert "probe_result=fail" in summary


@pytest.mark.parametrize("branch", ["main", "rakesh", "refs/heads/rakesh", "", "../../attacker"])
def test_kavya_image_probe_ignores_the_webhook_branch_field(tmp_path, branch):
    result, summary, _log = run_probe_workflow_step(
        tmp_path, PROBE_VALIDATION_STEP, PROBE_EVENT=probe_event_payload(branch=branch)
    )

    assert result.returncode == 0, "webhook branch must not affect validation"
    assert summary == ""
    assert (tmp_path / "outputs").read_text(encoding="utf-8").splitlines() == [
        f"existing_tag={PROBE_EXISTING_TAG}",
        f"expected_revision={PROBE_EXPECTED_REVISION}",
        "bootstrap=false",
    ]


@pytest.mark.parametrize(
    ("step", "overrides", "code"),
    [
        ("Probe known existing tag", {"EXISTING_CODE": 10, "EXISTING_OUT": "image_tag_state=existing\n"}, 0),
        ("Probe known existing tag", {"EXISTING_CODE": 0, "EXISTING_OUT": "image_tag_state=existing\n"}, 1),
        ("Probe known existing tag", {"EXISTING_CODE": 1, "EXISTING_OUT": "image_tag_state=existing\n"}, 1),
        ("Probe known existing tag", {"EXISTING_CODE": 99, "EXISTING_OUT": "image_tag_state=existing\n"}, 1),
        ("Probe known existing tag", {"EXISTING_CODE": 10, "EXISTING_OUT": "image_tag_state=existing\nextra\n"}, 1),
        ("Probe known existing tag", {"EXISTING_CODE": 10, "EXISTING_OUT": "wrong\n"}, 1),
        ("Probe known existing tag", {"EXISTING_CODE": 10, "EXISTING_OUT": ""}, 1),
        ("Probe generated absent canary", {"CANARY_CODE": 0, "CANARY_OUT": "image_tag_state=absent\n"}, 0),
        ("Probe generated absent canary", {"CANARY_CODE": 1, "CANARY_OUT": "image_tag_state=absent\n"}, 1),
        ("Probe generated absent canary", {"CANARY_CODE": 99, "CANARY_OUT": "image_tag_state=absent\n"}, 1),
        ("Probe generated absent canary", {"CANARY_CODE": 10, "CANARY_OUT": "image_tag_state=absent\n"}, 1),
        ("Probe generated absent canary", {"CANARY_CODE": 0, "CANARY_OUT": "image_tag_state=existing\n"}, 1),
        ("Probe generated absent canary", {"CANARY_CODE": 0, "CANARY_OUT": "wrong\n"}, 1),
        ("Probe generated absent canary", {"CANARY_CODE": 0, "CANARY_OUT": ""}, 1),
        ("Probe generated absent canary", {"CANARY_CODE": 0, "CANARY_OUT": "image_tag_state=absent\nextra\n"}, 1),
        ("Probe generated absent canary", {"CANARY_CODE": 0, "CANARY_OUT": "image_tag_state=absent"}, 1),
    ],
)
def test_kavya_image_probe_accepts_only_exact_probe_states(tmp_path, step, overrides, code):
    result, summary, log = run_probe_workflow_step(tmp_path, step, EXISTING_ERR="SENTINEL_EXISTING", CANARY_ERR="SENTINEL_CANARY", **overrides)
    assert result.returncode == code
    assert "SENTINEL" not in result.stdout + result.stderr + summary
    if code:
        assert "probe_result=pass" not in summary
    if step == "Probe generated absent canary":
        assert log == "probe ghcr.io/taskforce-ai-dev/kavya:probe-123-456-1\n"


PROBE_DIGEST_INSPECT_LOG = (
    f"docker buildx imagetools inspect ghcr.io/taskforce-ai-dev/kavya:{PROBE_EXISTING_TAG}"
    " --format {{ .Manifest.Digest }}"
)
PROBE_PULL_LOG = f"docker pull ghcr.io/taskforce-ai-dev/kavya@{PROBE_CANARY_DIGEST}"
PROBE_IMAGE_INSPECT_LOG = (
    f"docker image inspect ghcr.io/taskforce-ai-dev/kavya@{PROBE_CANARY_DIGEST}"
    ' --format {{ index .Config.Labels "org.opencontainers.image.revision" }}'
)


@pytest.mark.parametrize(
    ("overrides", "code", "expected_log"),
    [
        ({}, 0, [PROBE_DIGEST_INSPECT_LOG, PROBE_PULL_LOG, PROBE_IMAGE_INSPECT_LOG]),
        ({"DIGEST_CODE": 1}, 1, [PROBE_DIGEST_INSPECT_LOG]),
        ({"DIGEST_OUT": "not-a-digest\n"}, 1, [PROBE_DIGEST_INSPECT_LOG]),
        ({"DIGEST_OUT": "SENTINEL_DIGEST\n"}, 1, [PROBE_DIGEST_INSPECT_LOG]),
        ({"PULL_CODE": 1}, 1, [PROBE_DIGEST_INSPECT_LOG, PROBE_PULL_LOG]),
        ({"INSPECT_CODE": 1}, 1, [PROBE_DIGEST_INSPECT_LOG, PROBE_PULL_LOG, PROBE_IMAGE_INSPECT_LOG]),
        (
            {"INSPECT_OUT": "47bfaf02f04ce7614b9674b1c867b78ab3c7d414\n"},
            1,
            [PROBE_DIGEST_INSPECT_LOG, PROBE_PULL_LOG, PROBE_IMAGE_INSPECT_LOG],
        ),
        (
            {"INSPECT_OUT": f"{PROBE_EXPECTED_REVISION}\nextra\n"},
            1,
            [PROBE_DIGEST_INSPECT_LOG, PROBE_PULL_LOG, PROBE_IMAGE_INSPECT_LOG],
        ),
        (
            {"INSPECT_OUT": PROBE_EXPECTED_REVISION},
            1,
            [PROBE_DIGEST_INSPECT_LOG, PROBE_PULL_LOG, PROBE_IMAGE_INSPECT_LOG],
        ),
        (
            {"INSPECT_OUT": "SENTINEL_INSPECT_STDOUT\n"},
            1,
            [PROBE_DIGEST_INSPECT_LOG, PROBE_PULL_LOG, PROBE_IMAGE_INSPECT_LOG],
        ),
    ],
)
def test_kavya_image_probe_provenance_and_metadata_suppress_sentinels(tmp_path, overrides, code, expected_log):
    result, summary, log = run_probe_workflow_step(
        tmp_path, "Verify existing OCI revision",
        PULL_OUT="SENTINEL_PULL_STDOUT", PULL_ERR="SENTINEL_PULL_STDERR",
        INSPECT_ERR="SENTINEL_INSPECT_STDERR", DIGEST_ERR="SENTINEL_DIGEST_STDERR",
        **overrides,
    )
    combined = result.stdout + result.stderr + summary
    assert result.returncode == code
    assert log.splitlines() == expected_log
    assert "SENTINEL" not in combined
    assert PROBE_EXPECTED_REVISION not in combined
    if code:
        assert "probe_result=pass" not in summary


@pytest.mark.parametrize("repository_id,run_id,attempt", [("", "456", "1"), ("abc", "456", "1"), ("123" * 50, "456", "1"), ("123", "", "1"), ("123", "456", "x")])
def test_kavya_image_probe_canary_identifier_table_fails_before_fake_tools(tmp_path, repository_id, run_id, attempt):
    result, summary, log = run_probe_workflow_step(tmp_path, "Probe generated absent canary", GITHUB_REPOSITORY_ID=repository_id, GITHUB_RUN_ID=run_id, GITHUB_RUN_ATTEMPT=attempt)
    assert result.returncode != 0
    assert log == ""
    assert "probe_result=fail" in summary
    assert "probe_result=pass" not in summary


@pytest.mark.parametrize(
    "overrides,code",
    [
        ({}, 0),
        ({"PROBE_WORKFLOW_SHA": ""}, 1),
        ({"PROBE_WORKFLOW_SHA": "A" * 40}, 1),
        ({"PROBE_WORKFLOW_SHA": "a" * 39}, 1),
        ({"PROBE_RUNNER_LABEL": "ubuntu-latest"}, 1),
        ({"PROBE_RUNNER_OS": "Windows"}, 1),
        ({"PROBE_RUNNER_OS": ""}, 1),
        ({"PROBE_RUNNER_ARCH": "ARM64"}, 1),
        ({"PROBE_RUNNER_ARCH": ""}, 1),
        ({"BUILDX_CODE": 1}, 1),
        ({"BUILDX_VERSION": "bad value"}, 1),
        ({"BUILDX_VERSION": "0.16.2"}, 1),
        ({"BUILDX_VERSION": "v" + "0" * 64}, 1),
    ],
)
def test_kavya_image_probe_metadata_table_is_safe(tmp_path, overrides, code):
    result, summary, _log = run_probe_workflow_step(tmp_path, "Record safe runtime metadata", **overrides)
    assert result.returncode == code
    assert "PATH=" not in result.stdout + result.stderr + summary
    assert "SENTINEL" not in result.stdout + result.stderr + summary
    if code:
        assert "probe_result=pass" not in summary


PROBE_NUL_CAPTURES = [
    ("Probe known existing tag", {"EXISTING_OUT_FILE": b"image_tag_state=existing\x00\n"}),
    ("Probe known existing tag", {"EXISTING_OUT_FILE": b"\x00image_tag_state=existing\n"}),
    ("Probe generated absent canary", {"CANARY_OUT_FILE": b"image_tag_state=absent\x00\n"}),
    ("Probe generated absent canary", {"CANARY_OUT_FILE": b"image_tag_state=absent\n\x00"}),
    (
        "Verify existing OCI revision",
        {"INSPECT_OUT_FILE": PROBE_EXPECTED_REVISION.encode("ascii") + b"\x00\n"},
    ),
    (
        "Verify existing OCI revision",
        {"INSPECT_OUT_FILE": b"\x00" + PROBE_EXPECTED_REVISION.encode("ascii") + b"\n"},
    ),
]


@pytest.mark.parametrize(("step", "output_files"), PROBE_NUL_CAPTURES, ids=range(len(PROBE_NUL_CAPTURES)))
def test_kavya_image_probe_rejects_nul_bearing_contract_bytes(tmp_path, step, output_files):
    # A NUL is invisible to command substitution but not to cmp -s against the
    # printf-written expected file, so the byte-exact comparison must reject it.
    result, summary, _log = run_probe_workflow_step(tmp_path, step, output_files=output_files)

    assert result.returncode == 1
    assert "probe_result=fail" in summary
    assert "probe_result=pass" not in summary
    assert "_state=pass" not in summary
    assert "existing_revision=pass" not in summary


def test_kavya_image_probe_run_scripts_parse_and_summary_is_allowlisted(tmp_path):
    document, _job, steps, _text = kavya_image_probe_job()
    for script in workflow_run_strings(document):
        parsed = subprocess.run(["bash", "-n"], input=script, text=True, capture_output=True, check=False)
        assert parsed.returncode == 0, parsed.stderr

    # The recorded action identifiers are derived from the workflow's own `uses:`
    # values, so a repointed action can never be misreported as the pinned one.
    checkout = probe_workflow_step(steps, "Checkout trusted probe tooling")["uses"]
    buildx = probe_workflow_step(steps, "Set up Buildx")["uses"]
    login = probe_workflow_step(steps, "Log in to GHCR")["uses"]
    expected = [
        f"workflow_commit={'a' * 40}",
        f"checkout_action={checkout}",
        f"setup_buildx_action={buildx}",
        f"login_action={login}",
        "runner_label=ubuntu-24.04",
        "runner_os=Linux",
        "runner_arch=X64",
        "buildx_version=v0.16.2",
        "existing_tag_state=pass",
        "existing_revision=pass",
        "canary_state=pass",
        "probe_mode=strict",
        f"existing_tag={PROBE_EXISTING_TAG}",
        f"expected_revision={PROBE_EXPECTED_REVISION}",
        "probe_version=1",
        "probe_result=pass",
    ]
    summary = ""
    for name in ("Record safe runtime metadata", "Probe known existing tag", "Verify existing OCI revision", "Probe generated absent canary", "Write safe probe summary"):
        result, summary, _log = run_probe_workflow_step(tmp_path, name)
        assert result.returncode == 0, name
    assert summary.splitlines() == expected
    assert "SENTINEL" not in summary


def test_build_kavya_image_publisher_is_dispatch_only_and_least_privilege():
    document, job, steps, text = build_kavya_image_job()

    assert document["name"] == "Build Kavya image (no deploy)"
    # The parsed trigger set is the authoritative check that no other event -- a
    # `workflow_run` chain in particular -- can start the publisher.
    assert set(document["on"]) == {"workflow_dispatch"}
    inputs = document["on"]["workflow_dispatch"]["inputs"]
    assert set(inputs) == {"ref", "expected_sha"}
    assert inputs["ref"]["required"] == "true"
    assert inputs["expected_sha"]["required"] == "true"
    # actions: read is the smallest grant that can list the probe workflow's runs.
    assert job["permissions"] == {"contents": "read", "packages": "write", "actions": "read"}
    assert "environment" not in job

    login = workflow_step(steps, "Log in to GHCR")
    assert login["uses"].startswith("docker/login-action@")
    assert login["with"] == {
        "registry": "ghcr.io",
        "username": "${{ github.actor }}",
        "password": "${{ secrets.GITHUB_TOKEN }}",
    }
    assert set(re.findall(r"secrets\.([A-Za-z0-9_]+)", text)) == {"GITHUB_TOKEN"}

    for forbidden in (
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


PUBLISHER_SHA = "c" * 40
PUBLISHER_WORKFLOW_REF = f"{PROBE_REPOSITORY}/.github/workflows/build-kavya-image.yml@refs/heads/main"
PUBLISHER_TRUST_STEP = "Validate publisher dispatch trust"
PUBLISHER_GATE_STEP = "Require a fresh successful read-only probe"
PUBLISHER_GATE_WINDOW_SECONDS = 86400
# The real index digest build-push-action reported for kavya:db7d0d1.
PUBLISHER_BUILT_DIGEST = "sha256:3d7f73715d934dbb30e7ac09f240162830ee9d8038b04a73554e9539247d88a7"


def test_build_kavya_image_publisher_binds_execution_to_protected_default_branch():
    document, _job, steps, _text = build_kavya_image_job()
    trust = workflow_step(steps, PUBLISHER_TRUST_STEP)

    assert steps.index(trust) == 0
    assert trust["id"] == "trust"
    assert "uses" not in trust
    assert all(steps.index(step) > 0 for step in steps if "uses" in step)
    assert trust["env"] == {
        "PUBLISHER_EVENT_NAME": "${{ github.event_name }}",
        "PUBLISHER_REPOSITORY": "${{ github.repository }}",
        "PUBLISHER_REF": "${{ github.ref }}",
        "PUBLISHER_REF_NAME": "${{ github.ref_name }}",
        "PUBLISHER_REF_TYPE": "${{ github.ref_type }}",
        "PUBLISHER_REF_PROTECTED": "${{ github.ref_protected }}",
        "PUBLISHER_SHA": "${{ github.sha }}",
        "PUBLISHER_WORKFLOW_SHA": "${{ github.workflow_sha }}",
        "PUBLISHER_WORKFLOW_REF": "${{ github.workflow_ref }}",
    }
    # Without this the probe-run comparison would be meaningless: a contributor
    # could dispatch a weakened publisher from their own branch.
    for check in (
        "[[ \"$PUBLISHER_EVENT_NAME\" == 'workflow_dispatch' ]] || fail",
        '[[ "$PUBLISHER_REPOSITORY" == "$repository" ]] || fail',
        "[[ \"$PUBLISHER_REF\" == 'refs/heads/main' ]] || fail",
        "[[ \"$PUBLISHER_REF_NAME\" == 'main' ]] || fail",
        "[[ \"$PUBLISHER_REF_TYPE\" == 'branch' ]] || fail",
        "[[ \"$PUBLISHER_REF_PROTECTED\" == 'true' ]] || fail",
        '[[ "$PUBLISHER_SHA" =~ ^[0-9a-f]{40}$ ]] || fail',
        '[[ "$PUBLISHER_WORKFLOW_SHA" =~ ^[0-9a-f]{40}$ ]] || fail',
        '[[ "$PUBLISHER_SHA" == "$PUBLISHER_WORKFLOW_SHA" ]] || fail',
        '[[ "$PUBLISHER_WORKFLOW_REF" == "$repository/.github/workflows/build-kavya-image.yml@refs/heads/main" ]] || fail',
    ):
        assert check in trust["run"], check
    assert "gate_sha=%s" in trust["run"]


def test_build_kavya_image_publisher_requires_a_green_probe_before_any_credentialed_step():
    document, _job, steps, text = build_kavya_image_job()
    gate = workflow_step(steps, PUBLISHER_GATE_STEP)
    trust = workflow_step(steps, PUBLISHER_TRUST_STEP)

    assert steps.index(trust) < steps.index(gate)
    assert "uses" not in gate
    # Nothing credentialed -- not even a checkout -- may run before the gate.
    assert all(steps.index(step) > steps.index(gate) for step in steps if "uses" in step)
    assert gate["id"] == "probe_gate"
    assert gate["env"] == {
        "GH_TOKEN": "${{ secrets.GITHUB_TOKEN }}",
        "GATE_REPOSITORY": "${{ github.repository }}",
        "GATE_SHA": "${{ steps.trust.outputs.gate_sha }}",
    }
    run = gate["run"]
    # `gh api -f` on a GET either 404s or is silently dropped as a filter, so the
    # query string must be part of the URL for the server-side filter to apply.
    assert "-f head_sha" not in run
    assert "-f branch" not in run
    assert 'actions/workflows/probe-kavya-image.yml/runs?' in run
    assert "branch=main" in run
    assert "status=success" in run
    assert "head_sha=$GATE_SHA" in run
    for check in (
        '[[ "$GATE_SHA" =~ ^[0-9a-f]{40}$ ]] || fail',
        '[[ "$total" =~ ^[0-9]+$ ]] || fail',
        '[[ "$total" -ge 1 ]] || fail',
        '[[ "$total" -le 100 ]] || fail',
        f'cutoff="$(( $(date -u +%s) - {PUBLISHER_GATE_WINDOW_SECONDS} ))"',
        '.path == ".github/workflows/probe-kavya-image.yml"',
        '.head_branch == "main"',
        '.status == "completed"',
        '.conclusion == "success"',
        '[[ "$gate_run_id" =~ ^[0-9]+$ ]] || fail',
        '.run_started_at',
    ):
        assert check in run, check
    # A re-run bumps updated_at without re-running the probe's checks.
    assert ".updated_at" not in run
    # The captured API response is never echoed.
    assert "cat " not in run
    assert all("${{" not in value for value in workflow_run_strings(document))
    assert "probe_gate=pass" in run


def probe_run_entry(
    run_id=4242,
    head_sha=PUBLISHER_SHA,
    age_seconds=3600,
    branch="main",
    status="completed",
    conclusion="success",
    path=".github/workflows/probe-kavya-image.yml",
    run_started_at=None,
    updated_at_age_seconds=None,
):
    if run_started_at is None:
        run_started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - age_seconds))
    updated_age = age_seconds if updated_at_age_seconds is None else updated_at_age_seconds
    return {
        "id": run_id,
        "head_sha": head_sha,
        "head_branch": branch,
        "status": status,
        "conclusion": conclusion,
        "path": path,
        "run_started_at": run_started_at,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - updated_age)),
        "note": "SENTINEL_API_BODY",
    }


def probe_runs_json(entries, total_count=None):
    entries = list(entries)
    return json.dumps(
        {
            "total_count": len(entries) if total_count is None else total_count,
            "workflow_runs": entries,
        }
    )


def run_publisher_workflow_step(tmp_path, name, gh_stdout=None, **values):
    _document, _job, steps, _text = build_kavya_image_job()
    script = workflow_step(steps, name)["run"]
    log = tmp_path / "tools.log"
    outputs = tmp_path / "outputs"
    summary = tmp_path / "summary"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(exist_ok=True)
    gh = fake_bin / "gh"
    gh.write_text(textwrap.dedent("""\
        #!/usr/bin/env bash
        set -Eeuo pipefail
        printf 'gh %s\\n' "$*" >> "$FAKE_LOG"
        if [[ -n "${GH_OUT_FILE:-}" ]]; then cat "$GH_OUT_FILE"; fi
        printf '%s' "${GH_ERR:-}" >&2
        exit "$GH_CODE"
    """), encoding="utf-8")
    gh.chmod(0o755)
    docker = fake_bin / "docker"
    docker.write_text(textwrap.dedent("""\
        #!/usr/bin/env bash
        set -Eeuo pipefail
        printf 'docker %s\\n' "$*" >> "$FAKE_LOG"
        # Same fidelity rule as the probe's fake: a --format template that does
        # not resolve against imagetools.tplInput must fail like the real one.
        fmt=""; previous=""
        for argument in "$@"; do
          if [[ "$previous" == "--format" ]]; then fmt="$argument"; fi
          previous="$argument"
        done
        if [[ -n "$fmt" && "$fmt" != *".Manifest"* && "$fmt" != *".Image"* && "$fmt" != *".Config"* && "$fmt" != *".Id"* && "$fmt" != *".State"* ]]; then
          printf 'ERROR: template: :1:2: executing "" at <%s>: cannot evaluate field in type imagetools.tplInput\\n' "$fmt" >&2
          exit 1
        fi
        if [[ "$1 ${2:-} ${3:-}" == "buildx imagetools inspect" ]]; then
          printf '%s\\n' "$DIGEST_OUT"; exit "$DIGEST_CODE"
        fi
        exit 97
    """), encoding="utf-8")
    docker.chmod(0o755)
    payload = tmp_path / "gh-stdout.json"
    payload.write_text(
        probe_runs_json([probe_run_entry()]) if gh_stdout is None else gh_stdout,
        encoding="utf-8",
    )
    env = os.environ | {
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "FAKE_LOG": str(log), "GITHUB_OUTPUT": str(outputs), "GITHUB_STEP_SUMMARY": str(summary),
        "GH_OUT_FILE": str(payload), "GH_CODE": "0", "GH_ERR": "SENTINEL_GH_STDERR",
        "GH_TOKEN": "SENTINEL_TOKEN",
        "PUBLISHER_EVENT_NAME": "workflow_dispatch",
        "PUBLISHER_REPOSITORY": PROBE_REPOSITORY,
        "PUBLISHER_REF": "refs/heads/main", "PUBLISHER_REF_NAME": "main",
        "PUBLISHER_REF_TYPE": "branch", "PUBLISHER_REF_PROTECTED": "true",
        "PUBLISHER_SHA": PUBLISHER_SHA, "PUBLISHER_WORKFLOW_SHA": PUBLISHER_SHA,
        "PUBLISHER_WORKFLOW_REF": PUBLISHER_WORKFLOW_REF,
        "GATE_REPOSITORY": PROBE_REPOSITORY, "GATE_SHA": PUBLISHER_SHA,
        "MODE": "absent", "TAG": "ghcr.io/taskforce-ai-dev/kavya:db7d0d1",
        "BUILT_DIGEST": PUBLISHER_BUILT_DIGEST, "DIGEST_OUT": PUBLISHER_BUILT_DIGEST,
        "DIGEST_CODE": "0",
    } | {key: str(value) for key, value in values.items()}
    result = subprocess.run(["bash", "-c", script], cwd=tmp_path, env=env, text=True, capture_output=True, check=False)
    return (
        result,
        outputs.read_text(encoding="utf-8") if outputs.exists() else "",
        log.read_text(encoding="utf-8") if log.exists() else "",
    )


PUBLISHER_UNTRUSTED_CONTEXTS = [
    {"PUBLISHER_EVENT_NAME": "repository_dispatch"},
    {"PUBLISHER_EVENT_NAME": "push"},
    {"PUBLISHER_REPOSITORY": "attacker/full-voice-agent"},
    {"PUBLISHER_REF": "refs/heads/rakesh"},
    {"PUBLISHER_REF": "refs/tags/v1"},
    {"PUBLISHER_REF_NAME": "rakesh"},
    {"PUBLISHER_REF_TYPE": "tag"},
    {"PUBLISHER_REF_PROTECTED": "false"},
    {"PUBLISHER_REF_PROTECTED": ""},
    {"PUBLISHER_SHA": "d" * 40},
    {"PUBLISHER_SHA": "C" * 40, "PUBLISHER_WORKFLOW_SHA": "C" * 40},
    {"PUBLISHER_SHA": "c" * 39},
    {"PUBLISHER_WORKFLOW_SHA": "d" * 40},
    {"PUBLISHER_WORKFLOW_REF": f"{PROBE_REPOSITORY}/.github/workflows/build-kavya-image.yml@refs/heads/rakesh"},
    {"PUBLISHER_WORKFLOW_REF": f"{PROBE_REPOSITORY}/.github/workflows/other.yml@refs/heads/main"},
    {"PUBLISHER_WORKFLOW_REF": ""},
]


@pytest.mark.parametrize(
    "overrides", PUBLISHER_UNTRUSTED_CONTEXTS, ids=range(len(PUBLISHER_UNTRUSTED_CONTEXTS))
)
def test_build_kavya_image_publisher_trust_step_rejects_untrusted_context(tmp_path, overrides):
    result, outputs, log = run_publisher_workflow_step(tmp_path, PUBLISHER_TRUST_STEP, **overrides)

    assert result.returncode == 1
    assert log == ""
    assert outputs == ""


def test_build_kavya_image_publisher_trust_step_publishes_the_verified_sha(tmp_path):
    result, outputs, log = run_publisher_workflow_step(tmp_path, PUBLISHER_TRUST_STEP)

    assert result.returncode == 0
    assert log == ""
    assert outputs.splitlines() == [f"gate_sha={PUBLISHER_SHA}"]


PUBLISHER_GATE_CASES = [
    # (description, gh_stdout, gh_code, expected returncode)
    ("fresh matching run", None, 0, 0),
    ("api call fails", None, 1, 1),
    ("api returns html", "<html>bad gateway</html>", 0, 1),
    ("api returns nothing", "", 0, 1),
    ("api returns truncated json", '{"total_count": 1, "workflow_runs": [', 0, 1),
    ("no runs at all", probe_runs_json([]), 0, 1),
    ("total_count missing", json.dumps({"workflow_runs": []}), 0, 1),
    ("pagination ambiguity", probe_runs_json([probe_run_entry()], total_count=101), 0, 1),
    ("run failed", probe_runs_json([probe_run_entry(conclusion="failure")]), 0, 1),
    ("run cancelled", probe_runs_json([probe_run_entry(conclusion="cancelled")]), 0, 1),
    ("run still going", probe_runs_json([probe_run_entry(status="in_progress")]), 0, 1),
    ("different commit", probe_runs_json([probe_run_entry(head_sha="d" * 40)]), 0, 1),
    ("different branch", probe_runs_json([probe_run_entry(branch="rakesh")]), 0, 1),
    ("different workflow", probe_runs_json([probe_run_entry(path=".github/workflows/deploy.yml")]), 0, 1),
    ("stale by an hour", probe_runs_json([probe_run_entry(age_seconds=PUBLISHER_GATE_WINDOW_SECONDS + 3600)]), 0, 1),
    ("unparseable timestamp", probe_runs_json([probe_run_entry(run_started_at="yesterday")]), 0, 1),
    ("empty timestamp", probe_runs_json([probe_run_entry(run_started_at="")]), 0, 1),
    # A re-run bumps updated_at without re-establishing the evidence, so freshness
    # must key on run_started_at or stale evidence looks current.
    (
        "stale run whose updated_at was refreshed",
        probe_runs_json([
            probe_run_entry(age_seconds=PUBLISHER_GATE_WINDOW_SECONDS + 3600, updated_at_age_seconds=60)
        ]),
        0,
        1,
    ),
    # Offset timestamps are rejected on purpose rather than parsed leniently.
    ("non-Z timezone offset", probe_runs_json([probe_run_entry(run_started_at="2026-08-08T12:00:00+00:00")]), 0, 1),
    (
        "one fresh run among stale and failed ones",
        probe_runs_json([
            probe_run_entry(run_id=1, age_seconds=PUBLISHER_GATE_WINDOW_SECONDS + 60),
            probe_run_entry(run_id=2, conclusion="failure"),
            probe_run_entry(run_id=3, age_seconds=120),
        ]),
        0,
        0,
    ),
]


@pytest.mark.parametrize(
    ("gh_stdout", "gh_code", "code"),
    [case[1:] for case in PUBLISHER_GATE_CASES],
    ids=[case[0] for case in PUBLISHER_GATE_CASES],
)
def test_build_kavya_image_publisher_probe_gate_fails_closed(tmp_path, gh_stdout, gh_code, code):
    result, outputs, log = run_publisher_workflow_step(
        tmp_path, PUBLISHER_GATE_STEP, gh_stdout=gh_stdout, GH_CODE=gh_code
    )
    combined = result.stdout + result.stderr + outputs

    assert result.returncode == code
    assert "SENTINEL" not in combined, "the API response and token must never be echoed"
    if code:
        assert "probe_gate=pass" not in outputs
    else:
        assert "probe_gate=pass" in outputs
    assert log.startswith("gh api ") or log == ""


def test_build_kavya_image_publisher_probe_gate_queries_the_bound_commit(tmp_path):
    result, outputs, log = run_publisher_workflow_step(tmp_path, PUBLISHER_GATE_STEP)

    assert result.returncode == 0
    assert log.splitlines() == [
        "gh api "
        f"repos/{PROBE_REPOSITORY}/actions/workflows/probe-kavya-image.yml/runs"
        f"?per_page=100&branch=main&status=success&head_sha={PUBLISHER_SHA}"
    ]
    assert outputs.splitlines() == ["probe_gate=pass", "probe_run_id=4242"]


def test_build_kavya_image_publisher_summary_records_the_probe_gate_evidence():
    _document, _job, steps, _text = build_kavya_image_job()
    summary = workflow_step(steps, "Write safe build summary")

    assert summary["env"]["PROBE_GATE"] == "${{ steps.probe_gate.outputs.probe_gate }}"
    assert summary["env"]["PROBE_RUN_ID"] == "${{ steps.probe_gate.outputs.probe_run_id }}"
    assert 'echo "- probe_gate: ${PROBE_GATE:-unavailable}"' in summary["run"]
    assert 'echo "- probe_run_id: ${PROBE_RUN_ID:-unavailable}"' in summary["run"]


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


def run_kavya_image_tag_probe(
    tmp_path,
    docker_exit,
    docker_output,
    target=KAVYA_IMAGE_TARGET,
    output_bytes=None,
    binary=False,
    argument=None,
):
    assert KAVYA_IMAGE_TAG_PROBE.is_file(), "missing executable Kavya image tag probe"
    assert os.access(KAVYA_IMAGE_TAG_PROBE, os.X_OK)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(parents=True, exist_ok=True)
    fake_docker = fake_bin / "docker"
    fake_docker.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "if [[ \"$1\" != \"buildx\" || \"$2\" != \"imagetools\" || \"$3\" != \"inspect\" || \"$4\" != \"$EXPECTED_TARGET\" ]]; then\n"
        "  echo unexpected-docker-invocation >&2\n"
        "  exit 99\n"
        "fi\n"
        "if [[ -n \"${DOCKER_OUTPUT_FILE:-}\" ]]; then\n"
        "  cat \"$DOCKER_OUTPUT_FILE\" >&2\n"
        "else\n"
        "  echo \"$DOCKER_OUTPUT\" >&2\n"
        "fi\n"
        "exit \"$DOCKER_EXIT\"\n",
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)
    environment = os.environ | {
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "DOCKER_EXIT": str(docker_exit),
        "DOCKER_OUTPUT": docker_output,
        "EXPECTED_TARGET": target,
    }
    if output_bytes is not None:
        output_file = tmp_path / "docker-output.bin"
        output_file.write_bytes(output_bytes)
        environment["DOCKER_OUTPUT_FILE"] = str(output_file)
    return subprocess.run(
        [str(KAVYA_IMAGE_TAG_PROBE), target if argument is None else argument],
        env=environment,
        text=not binary,
        capture_output=True,
        check=False,
    )


CANARY_SHAPED_TARGET = "ghcr.io/taskforce-ai-dev/kavya:probe-1050123-17284593012-1"
PUBLISHER_SHAPED_TARGET = "ghcr.io/taskforce-ai-dev/kavya:a5012bc"


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
        # Observed against live GHCR on 2026-08-08, run 31269079259. buildx prints
        # failures as "ERROR: %v", so this is the shape a real missing tag takes.
        (1, f"ERROR: {KAVYA_IMAGE_TARGET}: not found", 0, "absent"),
        # Everything the allowlist does not match exactly is "unrecognised", which
        # is a distinct, actionable outcome from a structural rejection.
        (1, f"denied: manifest unknown: {KAVYA_IMAGE_TARGET}", 2, "probe_unrecognized"),
        (1, f"authentication required: no such manifest: {KAVYA_IMAGE_TARGET}", 2, "probe_unrecognized"),
        (1, f"token expired: {KAVYA_IMAGE_TARGET}: not found", 2, "probe_unrecognized"),
        (1, f"proxy returned 404 for {KAVYA_IMAGE_TARGET}", 2, "probe_unrecognized"),
        (1, f"timeout: manifest unknown: {KAVYA_IMAGE_TARGET}", 2, "probe_unrecognized"),
        (1, f"dial tcp: lookup registry: no such host: {KAVYA_IMAGE_TARGET}: not found", 2, "probe_unrecognized"),
        (1, f"429 rate limit: no such manifest: {KAVYA_IMAGE_TARGET}", 2, "probe_unrecognized"),
        (1, f"500 internal server error: {KAVYA_IMAGE_TARGET}: not found", 2, "probe_unrecognized"),
        (1, "manifest unknown", 2, "probe_unrecognized"),
        (1, "no such manifest", 2, "probe_unrecognized"),
        (1, "not found", 2, "probe_unrecognized"),
        (1, "no such manifest: ghcr.io/taskforce-ai-dev/kavya:othertag", 2, "probe_unrecognized"),
        (1, "failed to resolve source metadata for ghcr.io/taskforce-ai-dev/kavya:othertag: not found", 2, "probe_unrecognized"),
        (1, "registry returned 404", 2, "probe_unrecognized"),
        (1, "malformed registry response", 2, "probe_unrecognized"),
        # buildx prints failures as "ERROR: %v" (docker/buildx cmd/buildx/main.go).
        # Only the exact observed wording is allowlisted; other ERROR: shapes stay
        # unrecognised until they are themselves observed.
        (1, f"ERROR: manifest unknown: {KAVYA_IMAGE_TARGET}", 2, "probe_unrecognized"),
        (1, f"ERROR: {KAVYA_IMAGE_TARGET}: not found extra", 2, "probe_unrecognized"),
        (1, f"prefix ERROR: {KAVYA_IMAGE_TARGET}: not found", 2, "probe_unrecognized"),
        (1, "ERROR: ghcr.io/taskforce-ai-dev/kavya:othertag: not found", 2, "probe_unrecognized"),
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


@pytest.mark.parametrize("target", [CANARY_SHAPED_TARGET, PUBLISHER_SHAPED_TARGET])
@pytest.mark.parametrize(
    "message_template",
    [
        "manifest unknown: {target}",
        "no such manifest: {target}",
        "failed to resolve source metadata for {target}: not found",
    ],
)
def test_kavya_image_tag_probe_classifies_digit_bearing_tags_as_absent(
    tmp_path, target, message_template
):
    # A workflow canary embeds the repository/run identifiers and a publisher tag
    # is a seven-hex commit prefix; both routinely contain a digit run that a
    # deny-list heuristic would misread as an HTTP 5xx status.
    result = run_kavya_image_tag_probe(
        tmp_path, 1, message_template.format(target=target), target=target
    )

    assert result.returncode == 0
    assert result.stdout == "image_tag_state=absent\n"
    assert result.stderr == ""


@pytest.mark.parametrize("target", [CANARY_SHAPED_TARGET, PUBLISHER_SHAPED_TARGET])
def test_kavya_image_tag_probe_still_fails_closed_on_real_server_errors(tmp_path, target):
    result = run_kavya_image_tag_probe(
        tmp_path, 1, "unexpected status from registry: 503 Service Unavailable", target=target
    )

    assert result.returncode == 2
    assert result.stdout == "image_tag_state=probe_unrecognized\n"


def test_kavya_image_tag_probe_separates_unrecognised_messages_from_rejections(tmp_path):
    # Exit 2 means "the registry said something the allowlist does not cover" --
    # actionable by reading the real capture and widening it in a reviewed change.
    # Exit 1 means the helper rejected the input or the capture structurally, which
    # is never a reason to widen anything. Both fail closed for every caller.
    unrecognised = run_kavya_image_tag_probe(
        tmp_path / "a", 1, f"ERROR: {KAVYA_IMAGE_TARGET}: unexpected status 418"
    )
    assert unrecognised.returncode == 2
    assert unrecognised.stdout == "image_tag_state=probe_unrecognized\n"

    nul_bearing = run_kavya_image_tag_probe(
        tmp_path / "b", 1, "", output_bytes=b"manifest unknown: " + KAVYA_IMAGE_TARGET.encode() + b"\x00"
    )
    assert nul_bearing.returncode == 1
    assert nul_bearing.stdout == "image_tag_state=probe_failed\n"

    mixed_case = run_kavya_image_tag_probe(
        tmp_path / "c", 1, "manifest unknown", argument=KAVYA_IMAGE_TARGET.upper()
    )
    assert mixed_case.returncode == 1
    assert mixed_case.stdout == "image_tag_state=probe_failed\n"

    no_argument = subprocess.run(
        [str(KAVYA_IMAGE_TAG_PROBE)], text=True, capture_output=True, check=False
    )
    assert no_argument.returncode == 1
    assert no_argument.stdout == "image_tag_state=probe_failed\n"


@pytest.mark.parametrize(
    ("docker_exit", "output_bytes"),
    [
        # Docker failure: the NUL-free remainder is an exact allowlist match, so a
        # command-substitution read would launder it into "absent" and exit 0.
        (1, b"manifest unknown: " + KAVYA_IMAGE_TARGET.encode("ascii") + b"\x00"),
        (1, b"\x00manifest unknown: " + KAVYA_IMAGE_TARGET.encode("ascii")),
        (1, b"no such manifest: " + KAVYA_IMAGE_TARGET.encode("ascii") + b"\x00\n"),
        # Docker success: a NUL must still fail closed rather than report existing.
        (0, b"Name: " + KAVYA_IMAGE_TARGET.encode("ascii") + b"\x00\n"),
        (0, b"\x00"),
    ],
)
def test_kavya_image_tag_probe_rejects_nul_bearing_registry_captures(
    tmp_path, docker_exit, output_bytes
):
    result = run_kavya_image_tag_probe(
        tmp_path, docker_exit, "", output_bytes=output_bytes, binary=True
    )

    assert result.returncode == 1
    assert result.stdout == b"image_tag_state=probe_failed\n"
    assert b"\x00" not in result.stdout
    assert b"\x00" not in result.stderr


@pytest.mark.parametrize(
    "argument",
    [
        "ghcr.io/taskforce-ai-dev/kavya:DEADBEE",
        "ghcr.io/taskforce-ai-dev/Kavya:deadbee",
        "GHCR.IO/taskforce-ai-dev/kavya:deadbee",
    ],
)
def test_kavya_image_tag_probe_rejects_mixed_case_arguments(tmp_path, argument):
    result = run_kavya_image_tag_probe(
        tmp_path, 1, f"manifest unknown: {argument.lower()}", argument=argument
    )

    assert result.returncode == 1
    assert result.stdout == "image_tag_state=probe_failed\n"


def test_kavya_image_tag_probe_documents_its_nul_and_case_contract():
    script = KAVYA_IMAGE_TAG_PROBE.read_text(encoding="utf-8")

    assert "LC_ALL=C od -An -tx1 -v" in script
    assert "${TAG,,}" in script
    assert 'registry_error=$(<"$captured_error")' in script
    assert script.index("LC_ALL=C od -An -tx1 -v") < script.index(
        'registry_error=$(<"$captured_error")'
    )
    assert script.index("LC_ALL=C od -An -tx1 -v") < script.index(
        'echo "image_tag_state=existing"'
    )


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
        "capture_isolation_baseline",
        "docker compose --env-file .env.smartpbx --profile smartpbx config >/dev/null",
        "docker pull \"$IMAGE@$EXPECTED_DIGEST\" >/dev/null",
        "verify_candidate_image",
    ):
        assert required in script
    main_block = script.split("main() {", 1)[1].split("\nif [[", 1)[0]
    assert "arm_rollback" in main_block
    assert main_block.find("verify_candidate_image") < main_block.find("arm_rollback") < main_block.find("recreate_smartpbx")
    assert "ROLLBACK_TAG=" in script
    assert "ROLLBACK_DIGEST=" in script
    assert "ROLLBACK_REVISION=" in script
    assert "docker image tag \"$ROLLBACK_IMAGE_ID\" \"$IMAGE:$ROLLBACK_TAG\"" in script
    assert "rollback_once" in script


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
    assert "docker inspect --format '{{.Image}}' kavya-smartpbx" in script
    assert "verify_running_image" in script
    assert "verify_isolation_baseline" in script
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

    assert "rollback_once()" in script
    assert "TAG=$ROLLBACK_TAG" in script
    assert "wait_for_smartpbx_ready" in script
    assert "SMARTPBX_ROLLBACK_ESCALATION_REQUIRED" in script
    assert "return 1" in script[script.find("rollback_once()") :]
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


class FakeDeployHost:
    """A stateful fake PATH that executes the deploy helper without Docker."""

    image = "ghcr.io/taskforce-ai-dev/kavya"
    sha = "abcdef0" + "1" * 33
    digest = "sha256:" + "d" * 64
    baseline = "sha256:" + "b" * 64
    candidate = "sha256:" + "c" * 64

    def __init__(self, tmp_path, **state):
        self.root, self.bin, self.app = tmp_path, tmp_path / "bin", tmp_path / "app"
        self.bin.mkdir(); self.app.mkdir()
        (self.app / ".env").write_text("SENTINEL_LOCAL_SECRET\n", encoding="utf-8")
        # The status endpoint is token-gated, so the deploy script must read the
        # token from here. Sentinel-named so the no-leak assertions cover it.
        (self.app / ".env.smartpbx").write_text(
            "SENTINEL_SMARTPBX_SECRET\nSMARTPBX_WS_TOKEN=SENTINEL_STATUS_TOKEN\n",
            encoding="utf-8",
        )
        scripts = self.app / "scripts"; scripts.mkdir()
        validator = scripts / "validate_english_voice_env.sh"
        validator.write_text("#!/usr/bin/env bash\n[[ ${VOICE_FAIL:-0} == 0 ]] || exit 1\nprintf '%s\\n' canonical_voice_match=ok\n", encoding="utf-8")
        validator.chmod(0o755)
        self.log, self.state = tmp_path / "log", tmp_path / "state.json"
        defaults = {
            "baseline_id": self.baseline, "baseline_digest": f"{self.image}@sha256:" + "b" * 64,
            "baseline_revision": "a" * 40, "candidate_id": self.candidate,
            "candidate_digest": f"{self.image}@{self.digest}", "candidate_revision": self.sha,
            "baseline_digests": [f"{self.image}@sha256:" + "b" * 64],
            "candidate_digests": [f"{self.image}@{self.digest}"], "alias_checks": 0,
            "current_id": self.baseline, "alias_id": "", "tag_id": "", "mutated": False,
            "flico_id": "f" * 64, "legacy_id": "e" * 64,
            "flico_health": "healthy", "legacy_health": "healthy",
        }
        defaults.update(state)
        if "baseline_digest" in state:
            defaults["baseline_digests"] = [state["baseline_digest"]]
        if "baseline_id" in state:
            defaults["current_id"] = state["baseline_id"]
        self.state.write_text(json.dumps(defaults), encoding="utf-8")
        self._fake("flock", "exit 0\n")
        self._fake("stat", """
            [[ ${BAD_STAT_PATH:-} != "${3:-}" ]] || exit 1
            printf '%s\\n' root:root:600
        """)
        self._fake("jq", "exec /usr/bin/jq \"$@\"\n")
        self._fake("curl", """
            printf 'curl %s\\n' "$*" >> "$FAKE_LOG"
            if [[ $* == *health* ]]; then
              [[ ${HEALTH_FAIL:-0} == 0 ]] || exit 1
              if [[ -v HEALTH_JSON ]]; then printf '%s\\n' "$HEALTH_JSON"; else printf '%s\\n' '{"status":"ok","service_mode":"smartpbx"}'; fi
            else
              [[ ${STATUS_FAIL:-0} == 0 ]] || exit 1
              if [[ -v STATUS_JSON ]]; then printf '%s\\n' "$STATUS_JSON"; else printf '%s\\n' '{"active_sessions":0,"transfer_enabled":false}'; fi
            fi
        """)
        self._docker()

    def _fake(self, name, body):
        path = self.bin / name
        path.write_text("#!/usr/bin/env bash\nset -Eeuo pipefail\n" + textwrap.dedent(body), encoding="utf-8")
        path.chmod(0o755)

    def _docker(self):
        path = self.bin / "docker"
        path.write_text(textwrap.dedent("""\
            #!/usr/bin/env python3
            import json, os, signal, sys, tempfile, time
            state_path=os.environ['FAKE_STATE']; state=json.load(open(state_path))
            args=sys.argv[1:]
            with open(os.environ['FAKE_LOG'],'a') as log:
                log.write('docker ' + ' '.join(args) + '\\n')
            def save():
                descriptor, temporary = tempfile.mkstemp(prefix='.deploy-state-', suffix='.tmp', dir=os.path.dirname(state_path))
                try:
                    with os.fdopen(descriptor, 'w') as handle:
                        json.dump(state, handle); handle.flush(); os.fsync(handle.fileno())
                    os.chmod(temporary, 0o644)
                    os.replace(temporary, state_path)
                except BaseException:
                    try: os.unlink(temporary)
                    except FileNotFoundError: pass
                    raise
            def image_for(ref):
                if ref == state['baseline_id'] or ref.endswith(':rollback-local'):
                    revision=state['baseline_revision']
                    if state['mutated'] and ref == state['current_id'] and os.getenv('ROLLBACK_IDENTITY_BAD') == '1': revision='9'*40
                    return state['alias_id'] if ref.endswith(':rollback-local') else state['baseline_id'], state['baseline_digest'], revision
                image_id, digest, revision = state['candidate_id'], state['candidate_digest'], state['candidate_revision']
                if not state['mutated'] and os.getenv('CANDIDATE_DIGEST_BAD') == '1': digest='ghcr.io/taskforce-ai-dev/kavya@sha256:'+'9'*64
                if not state['mutated'] and os.getenv('CANDIDATE_REVISION_BAD') == '1': revision='9'*40
                if state['mutated'] and ref == state['current_id']:
                    if os.getenv('FORWARD_DIGEST_BAD') == '1': digest='ghcr.io/taskforce-ai-dev/kavya@sha256:'+'9'*64
                    if os.getenv('FORWARD_REVISION_BAD') == '1': revision='9'*40
                    if state['current_id'] == state['alias_id'] and os.getenv('ROLLBACK_IDENTITY_BAD') == '1': revision='9'*40
                if ref.endswith(':abcdef0'): image_id=state['tag_id'] or image_id
                return image_id, digest, revision
            if args[0] == 'pull': sys.exit(1 if os.getenv('PULL_FAIL') == '1' else 0)
            if args[:2] == ['compose', '--env-file']:
                if 'config' in args: sys.exit(1 if os.getenv('CONFIG_FAIL') == '1' else 0)
                tag=os.getenv('SMARTPBX_IMAGE_TAG')
                if tag == 'rollback-local':
                    if os.getenv('ROLLBACK_RECREATE_BAD') == '1': sys.exit(1)
                    if os.getenv('ROLLBACK_DELAY'): time.sleep(float(os.getenv('ROLLBACK_DELAY')))
                    state['current_id']=state['alias_id']
                else:
                    state['mutated']=True; state['current_id']=state['candidate_id']
                    if os.getenv('FORWARD_ID_BAD') == '1': state['current_id']='sha256:'+'9'*64
                    if os.getenv('FLICO_CHANGED') == '1': state['flico_health']='unhealthy'
                    if os.getenv('FLICO_ID_CHANGED') == '1': state['flico_id']='9'*64
                    if os.getenv('LEGACY_CHANGED') == '1': state['legacy_id']='9'*64
                    if os.getenv('LEGACY_HEALTH_CHANGED') == '1': state['legacy_health']='unhealthy'
                save()
                if os.getenv('SIGNAL_AFTER') and tag != 'rollback-local': os.kill(os.getppid(), getattr(signal, 'SIG'+os.getenv('SIGNAL_AFTER')))
                sys.exit(0)
            if args[:2] == ['inspect', '--format']:
                fmt, name=args[2], args[3]
                if name == 'flico-voice-agent' and os.getenv('FLICO_ABSENT') == '1': sys.exit(1)
                values={'kavya-smartpbx': {'{{.Image}}':state['current_id']}, 'flico-voice-agent': {'{{.Id}}':state['flico_id'],'{{.State.Health.Status}}':state['flico_health']}, 'kavya-voice-agent': {'{{.Id}}':state['legacy_id'],'{{.State.Health.Status}}':state['legacy_health']}}
                print(values.get(name,{}).get(fmt,'')); sys.exit(0 if fmt in values.get(name,{}) else 1)
            if args[:2] == ['image','tag']:
                source,target=args[2],args[3]
                if target.endswith(':rollback-local'): state['alias_id']='sha256:'+'8'*64 if os.getenv('ALIAS_BAD') == '1' else source
                else: state['tag_id']='sha256:'+'8'*64 if os.getenv('TAG_BAD') == '1' else source
                save(); sys.exit(0)
            if args[:2] == ['image','inspect']:
                ref,fmt=args[2],args[4]; image_id,digest,revision=image_for(ref)
                if 'RepoDigests' in fmt:
                    digests=state['baseline_digests'] if ref == state['baseline_id'] or ref.endswith(':rollback-local') else state['candidate_digests']
                    if not state['mutated'] and os.getenv('CANDIDATE_DIGEST_BAD') == '1': digests=['other.example/kavya@sha256:'+'9'*64]
                    if state['mutated'] and ref == state['current_id'] and os.getenv('FORWARD_DIGEST_BAD') == '1': digests=['other.example/kavya@sha256:'+'9'*64]
                    print('\\n'.join(digests))
                else:
                    if ref.endswith(':rollback-local'):
                        state['alias_checks'] += 1
                        if os.getenv('ALIAS_RECHECK_BAD') == '1' and state['alias_checks'] > 1: image_id='sha256:'+'8'*64
                        save()
                    print(image_id if fmt == '{{.Id}}' else revision if 'Config.Labels' in fmt else '')
                sys.exit(0)
            sys.exit(98)
        """), encoding="utf-8")
        path.chmod(0o755)

    def run(self, *args, prelude="", **env):
        environment = os.environ | {"PATH": f"{self.bin}:{os.environ['PATH']}", "FAKE_LOG": str(self.log), "FAKE_STATE": str(self.state)} | {key: str(value) for key, value in env.items()}
        override = f"{prelude}; " if prelude else ""
        command = f"source {SMARTPBX_IMAGE_DEPLOY_SCRIPT}; APP_DIR={self.app}; LOCK_FILE={self.root / 'lock'}; {override}main \"$@\""
        root_environment = [f"{key}={value}" for key, value in environment.items()]
        return subprocess.run(["sudo", "-n", "env", *root_environment, "bash", "-c", command, "fake-deploy", *args], text=True, capture_output=True, check=False)

    def start(self, *args, **env):
        environment = {"PATH": f"{self.bin}:{os.environ['PATH']}", "FAKE_LOG": str(self.log), "FAKE_STATE": str(self.state)} | {key: str(value) for key, value in env.items()}
        command = f"source {SMARTPBX_IMAGE_DEPLOY_SCRIPT}; APP_DIR={self.app}; LOCK_FILE={self.root / 'lock'}; main \"$@\""
        root_environment = [f"{key}={value}" for key, value in environment.items()]
        return subprocess.Popen(["sudo", "-n", "env", *root_environment, "bash", "-c", command, "fake-deploy", *args], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)

    def deploy(self, **env): return self.run(self.sha[:7], self.sha, self.digest, **env)
    def logs(self): return self.log.read_text(encoding="utf-8") if self.log.exists() else ""
    def compose_count(self): return self.logs().count("up -d --force-recreate --pull never kavya-smartpbx")
    def current(self):
        deadline = time.monotonic() + 0.2
        while time.monotonic() < deadline:
            try:
                return json.loads(self.state.read_text(encoding="utf-8"))
            except (FileNotFoundError, PermissionError, json.JSONDecodeError):
                time.sleep(0.005)
        raise AssertionError("fake Docker state was not atomically readable before deadline")


@pytest.mark.parametrize("state,environment", [
    ({"baseline_id": "invalid"}, {}),
    ({"baseline_digest": "sha256:" + "b" * 64}, {}),
    ({"baseline_revision": "b" * 39}, {}),
    ({}, {"ALIAS_BAD": 1}),
])
def test_deploy_baseline_metadata_or_alias_mismatch_never_mutates(tmp_path, state, environment):
    host = FakeDeployHost(tmp_path, **state); result = host.deploy(**environment)
    assert result.returncode != 0 and host.compose_count() == 0


@pytest.mark.parametrize("environment", [
    {"PULL_FAIL": 1}, {"CANDIDATE_DIGEST_BAD": 1}, {"CANDIDATE_REVISION_BAD": 1}, {"TAG_BAD": 1},
])
def test_deploy_candidate_pull_or_tag_identity_mismatch_never_mutates(tmp_path, environment):
    host = FakeDeployHost(tmp_path); result = host.deploy(**environment)
    assert result.returncode != 0 and host.compose_count() == 0


def test_deploy_accepts_expected_digest_when_repo_digests_are_out_of_order(tmp_path):
    host = FakeDeployHost(tmp_path, candidate_digests=["other.example/kavya@sha256:" + "9" * 64, f"{FakeDeployHost.image}@{FakeDeployHost.digest}"], baseline_digests=["other.example/kavya@sha256:" + "8" * 64, f"{FakeDeployHost.image}@sha256:" + "b" * 64])
    result = host.deploy()
    assert result.returncode == 0 and host.compose_count() == 1


def test_deploy_rejects_missing_expected_digest_before_mutation(tmp_path):
    host = FakeDeployHost(tmp_path, candidate_digests=["other.example/kavya@sha256:" + "9" * 64])
    result = host.deploy()
    assert result.returncode != 0 and host.compose_count() == 0


def test_deploy_rechecks_rollback_alias_before_forward_mutation(tmp_path):
    host = FakeDeployHost(tmp_path)
    result = host.deploy(ALIAS_RECHECK_BAD=1)
    assert result.returncode != 0 and host.compose_count() == 0


@pytest.mark.parametrize("environment", [
    {"HEALTH_FAIL": 1, "STATUS_JSON": '{"active_sessions":0,"transfer_enabled":false}'},
    {"HEALTH_JSON": "not-json"}, {"HEALTH_JSON": '{"status":"bad","service_mode":"smartpbx"}'},
    {"STATUS_JSON": '{"active_sessions":1,"transfer_enabled":false}'},
    {"STATUS_JSON": '{"active_sessions":0,"transfer_enabled":true}'},
    {"VOICE_FAIL": 1}, {"CONFIG_FAIL": 1},
])
def test_deploy_preflight_failures_never_mutate(tmp_path, environment):
    host = FakeDeployHost(tmp_path); result = host.deploy(**environment)
    assert result.returncode != 0 and host.compose_count() == 0


@pytest.mark.parametrize("bad_path", [".env", ".env.smartpbx"])
def test_deploy_each_env_file_failure_is_terminal_under_conditional_main_call(tmp_path, bad_path):
    host = FakeDeployHost(tmp_path)
    result = host.deploy(BAD_STAT_PATH=bad_path)
    assert result.returncode != 0 and host.compose_count() == 0


@pytest.mark.parametrize("environment,state", [({"FLICO_ABSENT": 1}, {}), ({}, {"flico_health": "unhealthy"})])
def test_deploy_flico_absent_or_unhealthy_blocks_before_mutation(tmp_path, environment, state):
    host = FakeDeployHost(tmp_path, **state); result = host.deploy(**environment)
    assert result.returncode != 0 and host.compose_count() == 0


@pytest.mark.parametrize("arguments", [
    (), ("abcdef0",), ("abcdef0", FakeDeployHost.sha),
    ("7654321", FakeDeployHost.sha, FakeDeployHost.digest),
    ("ABCDEF0", FakeDeployHost.sha, FakeDeployHost.digest),
    ("abcdef0", FakeDeployHost.sha.upper(), FakeDeployHost.digest),
    ("abcdef0", FakeDeployHost.sha, FakeDeployHost.digest.upper()),
    ("abcdef0", FakeDeployHost.sha, FakeDeployHost.digest, "extra"),
])
def test_deploy_root_path_exercises_invalid_argument_rejection(tmp_path, arguments):
    host = FakeDeployHost(tmp_path); result = host.run(*arguments)
    assert result.returncode != 0 and host.compose_count() == 0 and host.logs() == ""


@pytest.mark.parametrize("environment,protected", [
    ({"FORWARD_ID_BAD": 1}, None), ({"FORWARD_DIGEST_BAD": 1}, None), ({"FORWARD_REVISION_BAD": 1}, None),
    ({"FLICO_ID_CHANGED": 1}, "flico_id"), ({"FLICO_CHANGED": 1}, "flico_health"),
    ({"LEGACY_CHANGED": 1}, "legacy_id"), ({"LEGACY_HEALTH_CHANGED": 1}, "legacy_health"),
])
def test_deploy_bad_forward_identity_or_isolation_rolls_back_once(tmp_path, environment, protected):
    host = FakeDeployHost(tmp_path); result = host.deploy(**environment)
    assert result.returncode != 0 and host.compose_count() == 2
    assert host.current()["current_id"] == host.baseline
    if protected:
        assert "SMARTPBX_ROLLBACK_ESCALATION_REQUIRED" in result.stderr


def test_deploy_readiness_timeout_rolls_back_exactly_once(tmp_path):
    host = FakeDeployHost(tmp_path); result = host.deploy(prelude="wait_for_smartpbx_ready(){ return 1; }")
    assert result.returncode != 0 and host.compose_count() == 2


@pytest.mark.parametrize("environment,prelude", [
    ({"ROLLBACK_RECREATE_BAD": 1}, ""),
    ({"ROLLBACK_IDENTITY_BAD": 1}, ""),
    ({}, "wait_for_smartpbx_ready(){ return 1; }"),
])
def test_deploy_rollback_failures_emit_escalation_marker(tmp_path, environment, prelude):
    host = FakeDeployHost(tmp_path); result = host.deploy(prelude=prelude, FORWARD_ID_BAD=1, **environment)
    assert result.returncode != 0 and "SMARTPBX_ROLLBACK_ESCALATION_REQUIRED" in result.stderr


@pytest.mark.parametrize("signal_name", ["TERM", "INT", "HUP"])
def test_deploy_signals_after_mutation_roll_back_once(tmp_path, signal_name):
    host = FakeDeployHost(tmp_path)
    process = host.start(host.sha[:7], host.sha, host.digest)
    deadline = time.monotonic() + 5
    while not host.current()["mutated"] and time.monotonic() < deadline:
        time.sleep(0.01)
    assert host.current()["mutated"]
    process.send_signal(getattr(signal, f"SIG{signal_name}"))
    process.communicate(timeout=5)
    assert process.returncode != 0 and host.compose_count() == 2


@pytest.mark.parametrize("attempt", range(10))
def test_traps_precede_arming_and_second_signal_cannot_interrupt_rollback(tmp_path, attempt):
    script = read_smartpbx_image_deploy_script()
    arm = script.split("arm_rollback()", 1)[1].split("disarm_rollback()", 1)[0]
    assert arm.find("trap 'on_error") < arm.find("ROLLBACK_ARMED=1")
    host = FakeDeployHost(tmp_path)
    process = host.start(host.sha[:7], host.sha, host.digest, ROLLBACK_DELAY=1)
    deadline = time.monotonic() + 5
    while not host.current()["mutated"] and time.monotonic() < deadline:
        time.sleep(0.01)
    process.send_signal(signal.SIGTERM)
    time.sleep(0.1)
    process.send_signal(signal.SIGINT)
    _, stderr = process.communicate(timeout=5)
    assert process.returncode != 0 and host.compose_count() == 2
    assert "SMARTPBX_ROLLBACK_ESCALATION_REQUIRED" not in stderr


def test_deploy_mutates_only_the_pinned_service_and_never_prints_sentinels(tmp_path):
    host = FakeDeployHost(tmp_path); result = host.deploy()
    assert result.returncode == 0
    assert host.compose_count() == 1
    assert "up -d --force-recreate --pull never kavya-smartpbx" in host.logs()
    assert not any(value in (result.stdout + result.stderr + host.logs()) for value in ("SENTINEL_LOCAL_SECRET", "SENTINEL_SMARTPBX_SECRET"))
    mutation_lines = [line for line in host.logs().splitlines() if "up -d --force-recreate" in line]
    for forbidden in (" nginx", " prune", " down", " restart", " flico-voice-agent", " kavya-voice-agent"):
        assert all(forbidden not in line for line in mutation_lines)


# Byte-exact captures from GHCR, observed 2026-08-08 in workflow run
# https://github.com/taskforce-ai-dev/full-voice-agent/actions/runs/31269079259
# on the throwaway diag/ghcr-capture branch (PR #213, closed unmerged).
OBSERVED_GHCR_ABSENT_CAPTURES = {
    "ghcr.io/taskforce-ai-dev/kavya:37bfaf0":
        "RVJST1I6IGdoY3IuaW8vdGFza2ZvcmNlLWFpLWRldi9rYXZ5YTozN2JmYWYwOiBub3QgZm91bmQK",
    "ghcr.io/taskforce-ai-dev/kavya:probe-0-0-0":
        "RVJST1I6IGdoY3IuaW8vdGFza2ZvcmNlLWFpLWRldi9rYXZ5YTpwcm9iZS0wLTAtMDogbm90IGZvdW5kCg==",
}


@pytest.mark.parametrize("target", sorted(OBSERVED_GHCR_ABSENT_CAPTURES))
def test_kavya_image_tag_probe_accepts_the_observed_ghcr_absent_capture(tmp_path, target):
    """Pin the real registry bytes, not a paraphrase of them."""
    import base64

    captured = base64.b64decode(OBSERVED_GHCR_ABSENT_CAPTURES[target])
    result = run_kavya_image_tag_probe(
        tmp_path, 1, "", target=target, output_bytes=captured, binary=True
    )

    assert result.returncode == 0, (
        f"the observed capture must classify as absent: {captured!r}"
    )
    assert result.stdout == b"image_tag_state=absent\n"


def test_kavya_image_tag_probe_allowlist_entries_are_full_capture_matches():
    script = KAVYA_IMAGE_TAG_PROBE.read_text(encoding="utf-8")
    allowlist_line = next(
        line for line in script.splitlines()
        if 'image_tag_state=absent' not in line and '"$registry_error" ==' in line
    )

    # Every comparison is == against a whole-capture literal anchored on $TAG.
    # A glob or substring here is the defect that misclassified ~82% of canaries.
    for forbidden in ("==  *", '== *"', "=~", "*]]", '"*'):
        assert forbidden not in allowlist_line, forbidden
    assert allowlist_line.count('"$registry_error" == "') == allowlist_line.count("$TAG")
    assert '"error: $TAG: not found"' in allowlist_line


# --- Bootstrap mode -------------------------------------------------------
# The probe requires a pre-existing tag and the publisher requires a green
# probe, so the very first image can never be published: a bootstrap deadlock
# the design did not cover. Bootstrap mode breaks it by letting the
# existing-tag check pass on `absent`, and discloses that it did so.


def test_kavya_image_probe_payload_accepts_an_optional_bootstrap_flag(tmp_path):
    result, summary, log = run_probe_workflow_step(
        tmp_path, PROBE_VALIDATION_STEP, PROBE_EVENT=probe_event_payload(bootstrap="true")
    )

    assert result.returncode == 0
    assert log == ""
    assert summary == ""
    assert (tmp_path / "outputs").read_text(encoding="utf-8").splitlines() == [
        f"existing_tag={PROBE_EXISTING_TAG}",
        f"expected_revision={PROBE_EXPECTED_REVISION}",
        "bootstrap=true",
    ]


def test_kavya_image_probe_defaults_to_strict_mode(tmp_path):
    result, _summary, _log = run_probe_workflow_step(tmp_path, PROBE_VALIDATION_STEP)

    assert result.returncode == 0
    assert "bootstrap=false" in (tmp_path / "outputs").read_text(encoding="utf-8")


PROBE_REJECTED_BOOTSTRAP_VALUES = ["false", "TRUE", "True", "1", "yes", "", " true", "true ", None]


@pytest.mark.parametrize(
    "value", PROBE_REJECTED_BOOTSTRAP_VALUES, ids=range(len(PROBE_REJECTED_BOOTSTRAP_VALUES))
)
def test_kavya_image_probe_rejects_any_bootstrap_value_but_exactly_true(tmp_path, value):
    # Exactly one spelling asks for bootstrap and exactly one omission declines
    # it. A typo must fail the dispatch loudly, never fall back to a mode the
    # operator did not choose.
    payload = {
        "existing_tag": PROBE_EXISTING_TAG,
        "expected_revision": PROBE_EXPECTED_REVISION,
        "bootstrap": value,
    }
    result, summary, log = run_probe_workflow_step(
        tmp_path, PROBE_VALIDATION_STEP, PROBE_EVENT=probe_event_payload(client_payload=payload)
    )

    assert result.returncode == 1, f"bootstrap={value!r} must be rejected"
    assert log == ""
    assert "probe_result=fail" in summary


@pytest.mark.parametrize("value", [True, 1, 1.0, ["true"], {"v": "true"}])
def test_kavya_image_probe_rejects_non_string_bootstrap(tmp_path, value):
    payload = {
        "existing_tag": PROBE_EXISTING_TAG,
        "expected_revision": PROBE_EXPECTED_REVISION,
        "bootstrap": value,
    }
    result, _summary, log = run_probe_workflow_step(
        tmp_path, PROBE_VALIDATION_STEP, PROBE_EVENT=probe_event_payload(client_payload=payload)
    )

    assert result.returncode == 1
    assert log == ""


def test_kavya_image_probe_bootstrap_accepts_an_absent_existing_tag(tmp_path):
    result, summary, log = run_probe_workflow_step(
        tmp_path, "Probe known existing tag",
        PROBE_BOOTSTRAP="true", EXISTING_CODE=0, EXISTING_OUT="image_tag_state=absent\n",
    )

    assert result.returncode == 0
    assert "existing_tag_state=absent_bootstrap" in summary, (
        "a bootstrap pass must be recorded as such, never as an ordinary pass"
    )
    assert "existing_tag_state=pass" not in summary
    assert log == f"probe ghcr.io/taskforce-ai-dev/kavya:{PROBE_EXISTING_TAG}\n"
    assert "existing_image=absent" in (tmp_path / "outputs").read_text(encoding="utf-8")


def test_kavya_image_probe_bootstrap_still_verifies_a_tag_that_does_exist(tmp_path):
    result, summary, _log = run_probe_workflow_step(
        tmp_path, "Probe known existing tag", PROBE_BOOTSTRAP="true",
    )

    assert result.returncode == 0
    assert "existing_tag_state=pass" in summary
    assert "absent_bootstrap" not in summary
    assert "existing_image=present" in (tmp_path / "outputs").read_text(encoding="utf-8")


def test_kavya_image_probe_strict_mode_still_rejects_an_absent_existing_tag(tmp_path):
    result, summary, _log = run_probe_workflow_step(
        tmp_path, "Probe known existing tag",
        EXISTING_CODE=0, EXISTING_OUT="image_tag_state=absent\n",
    )

    assert result.returncode == 1, "strict mode must be unchanged"
    assert "probe_result=fail" in summary


@pytest.mark.parametrize(
    ("code", "output"),
    [
        (2, "image_tag_state=probe_unrecognized\n"),
        (1, "image_tag_state=probe_failed\n"),
        (0, "image_tag_state=existing\n"),
        (0, "image_tag_state=absent"),
        (0, "image_tag_state=absent\nextra\n"),
        (10, "image_tag_state=absent\n"),
        (99, "image_tag_state=absent\n"),
    ],
)
def test_kavya_image_probe_bootstrap_still_requires_an_exact_state(tmp_path, code, output):
    # Bootstrap relaxes only the "must already exist" premise. Every other
    # uncertain result stays fail-closed.
    result, summary, _log = run_probe_workflow_step(
        tmp_path, "Probe known existing tag",
        PROBE_BOOTSTRAP="true", EXISTING_CODE=code, EXISTING_OUT=output,
    )

    assert result.returncode == 1
    assert "probe_result=fail" in summary


def test_kavya_image_probe_revision_is_skipped_with_a_reason_when_there_is_no_image(tmp_path):
    result, summary, log = run_probe_workflow_step(
        tmp_path, "Verify existing OCI revision", PROBE_EXISTING_IMAGE="absent",
    )

    assert result.returncode == 0
    assert "existing_revision=skipped_no_image" in summary, (
        "the skip must be disclosed, not silently reported as a pass"
    )
    assert "existing_revision=pass" not in summary
    assert log == "", "no registry call may be made when there is no image"


def test_kavya_image_probe_revision_still_verifies_when_the_image_exists(tmp_path):
    result, summary, log = run_probe_workflow_step(tmp_path, "Verify existing OCI revision")

    assert result.returncode == 0
    assert "existing_revision=pass" in summary
    assert "skipped_no_image" not in summary
    assert log != ""


@pytest.mark.parametrize(
    ("bootstrap", "expected_mode"), [("false", "strict"), ("true", "bootstrap")]
)
def test_kavya_image_probe_summary_discloses_the_mode(tmp_path, bootstrap, expected_mode):
    # The publisher gate accepts any green probe at its head_sha, so a
    # bootstrap-green run must say so in its own evidence.
    result, summary, _log = run_probe_workflow_step(
        tmp_path, "Write safe probe summary", PROBE_BOOTSTRAP=bootstrap,
    )

    assert result.returncode == 0
    assert f"probe_mode={expected_mode}" in summary


# --- Registry template fidelity -------------------------------------------
# `docker buildx imagetools inspect --format` evaluates against
# imagetools.tplInput, which exposes .Manifest but has no bare .Digest.
# Observed live in run 31281453563:
#   --format '{{.Digest}}'          -> ERROR ... can't evaluate field Digest   (exit 1)
#   --format '{{.Manifest.Digest}}' -> sha256:3d7f7371...                      (exit 0)
# Both the publisher's push verification and the probe's digest resolution used
# the invalid form, so each aborted before the comparison it appeared to fail.


@pytest.mark.parametrize(
    "workflow", [KAVYA_IMAGE_PROBE_WORKFLOW, BUILD_KAVYA_IMAGE_WORKFLOW], ids=["probe", "publisher"]
)
def test_imagetools_digest_is_read_through_the_manifest_field(workflow):
    text = workflow.read_text(encoding="utf-8")

    for line in text.splitlines():
        if "imagetools inspect" not in line or "--format" not in line:
            continue
        assert ".Manifest.Digest" in line or ".Manifest" in line, (
            f"imagetools --format must resolve against .Manifest: {line.strip()}"
        )
    assert not re.search(r"\{\{\s*\.Digest\s*\}\}", text), (
        "a bare {{.Digest}} is not a field of imagetools.tplInput and exits 1"
    )


def test_publisher_resolve_digest_accepts_a_matching_pushed_digest(tmp_path):
    result, outputs, log = run_publisher_workflow_step(tmp_path, "Resolve image digest")

    assert result.returncode == 0, (
        "the real registry template must resolve; this step had no dynamic test "
        "before, which is how the invalid one reached production"
    )
    assert outputs.strip() == f"digest={PUBLISHER_BUILT_DIGEST}"
    assert "imagetools inspect" in log


def test_publisher_resolve_digest_rejects_a_digest_that_differs_from_the_build(tmp_path):
    result, outputs, _log = run_publisher_workflow_step(
        tmp_path, "Resolve image digest", DIGEST_OUT="sha256:" + "b" * 64,
    )

    assert result.returncode == 1, "a pushed digest that is not the built one must stop the release"
    assert outputs == ""


def test_publisher_resolve_digest_handles_the_existing_mode(tmp_path):
    result, outputs, _log = run_publisher_workflow_step(
        tmp_path, "Resolve image digest", MODE="existing",
    )

    assert result.returncode == 0
    assert outputs.strip() == f"digest={PUBLISHER_BUILT_DIGEST}"


PUBLISHER_RESOLVE_FAILURES = [
    ({"BUILT_DIGEST": "not-a-digest"}, "built_digest_malformed"),
    ({"DIGEST_CODE": 1}, "registry_inspect_failed"),
    ({"DIGEST_OUT": "not-a-digest"}, "pushed_digest_malformed"),
    ({"DIGEST_OUT": "sha256:" + "b" * 64}, "pushed_digest_mismatch"),
    ({"MODE": "sideways"}, "unrecognized_image_mode"),
]


@pytest.mark.parametrize(
    ("overrides", "marker"), PUBLISHER_RESOLVE_FAILURES,
    ids=[marker for _overrides, marker in PUBLISHER_RESOLVE_FAILURES],
)
def test_publisher_resolve_digest_says_which_check_failed(tmp_path, overrides, marker):
    # The first live publish failed here and the log could not say which of the
    # four checks tripped, which cost a diagnostic round trip.
    result, outputs, _log = run_publisher_workflow_step(
        tmp_path, "Resolve image digest", **overrides
    )

    assert result.returncode == 1
    assert f"resolve_digest={marker}" in result.stderr, result.stderr
    assert outputs == ""


def test_stt_endpointing_knobs_are_deliverable_through_the_documented_path():
    # The smartpbx service uses an explicit environment allowlist, so an env var
    # that is set but not listed here never reaches the container -- exactly the
    # KAVYA_EN_ELEVENLABS_VOICE_ID class of bug. The kavya (Twilio) service uses
    # env_file: .env, so .env.example is its passthrough.
    compose = yaml.safe_load(read_text("docker-compose.yml"))
    smartpbx_env = compose["services"]["kavya-smartpbx"]["environment"]
    kavya = compose["services"]["kavya"]
    assert ".env" in kavya["env_file"], "the Twilio service forwards .env vars via env_file"

    for name in (
        "STT_ENDPOINTING_SILENCE_SECONDS",
        "STT_FINAL_GRACE_SECONDS",
        "CAPTURE_ENDPOINTING_SILENCE_SECONDS",
        "CAPTURE_FINAL_GRACE_SECONDS",
    ):
        assert smartpbx_env.get(name) == f"${{{name}}}", (
            f"{name} must be in the smartpbx environment allowlist to reach the container"
        )

    example = read_text(".env.example")
    runbook = read_text("SMARTPBX_RUNBOOK.md")
    for name, default in (
        ("STT_ENDPOINTING_SILENCE_SECONDS", "1.0"),
        ("STT_FINAL_GRACE_SECONDS", "0.5"),
        ("CAPTURE_ENDPOINTING_SILENCE_SECONDS", "1.5"),
        ("CAPTURE_FINAL_GRACE_SECONDS", "1.2"),
    ):
        assert f"{name}={default}" in example, f"{name} missing from .env.example"
        assert f"{name}={default}" in runbook, f"{name} missing from the runbook env template"


def test_digit_class_boost_is_allowlisted_without_a_committed_runtime_value():
    compose = yaml.safe_load(read_text("docker-compose.yml"))
    smartpbx_env = compose["services"]["kavya-smartpbx"]["environment"]

    assert smartpbx_env.get("STT_DIGIT_CLASS_BOOST") == "${STT_DIGIT_CLASS_BOOST:-}"

    example = read_text(".env.example")
    assert "STT_DIGIT_CLASS_BOOST=" in example

    runbook = read_text("SMARTPBX_RUNBOOK.md")
    assert "STT_DIGIT_CLASS_BOOST=" in runbook
    assert "docker compose --env-file .env.smartpbx config" in runbook
    assert "do not print" in runbook.lower()
    assert "digit_class_enabled" in runbook


def test_pilot_transcript_logging_is_explicit_off_by_default_and_reversible():
    compose = yaml.safe_load(read_text("docker-compose.yml"))
    smartpbx_env = compose["services"]["kavya-smartpbx"]["environment"]

    assert smartpbx_env.get("SMARTPBX_PILOT_TRANSCRIPT_LOGGING") == (
        "${SMARTPBX_PILOT_TRANSCRIPT_LOGGING:-0}"
    )

    example = read_text(".env.example")
    assert re.search(
        r"^SMARTPBX_PILOT_TRANSCRIPT_LOGGING=0$", example, re.MULTILINE
    )

    runbook = read_text("SMARTPBX_RUNBOOK.md")
    normalized = re.sub(r"\s+", " ", runbook).lower()
    assert "smartpbx_pilot_transcript" in runbook
    assert "role=guest" in runbook
    assert "role=kavya" in runbook
    assert "same pinned image" in normalized
    assert "restore" in normalized
    assert "do not export" in normalized


def test_dtmf_knobs_are_deliverable_through_the_documented_path():
    # The smartpbx service uses an explicit environment allowlist, so a knob set
    # but not listed there never reaches the container. The kavya (Twilio) service
    # forwards .env vars via env_file, so .env.example is its passthrough.
    compose = yaml.safe_load(read_text("docker-compose.yml"))
    smartpbx_env = compose["services"]["kavya-smartpbx"]["environment"]

    for name in (
        "DTMF_INTERDIGIT_TIMEOUT_SECONDS",
        "DTMF_OVERALL_TIMEOUT_SECONDS",
        "DTMF_MAX_DIGITS",
    ):
        assert smartpbx_env.get(name) == f"${{{name}}}", (
            f"{name} must be in the smartpbx environment allowlist to reach the container"
        )

    example = read_text(".env.example")
    runbook = read_text("SMARTPBX_RUNBOOK.md")
    for name, default in (
        ("DTMF_INTERDIGIT_TIMEOUT_SECONDS", "6"),
        ("DTMF_OVERALL_TIMEOUT_SECONDS", "30"),
        ("DTMF_MAX_DIGITS", "15"),
    ):
        assert f"{name}={default}" in example, f"{name} missing from .env.example"
        assert f"{name}={default}" in runbook, f"{name} missing from the runbook env template"


def test_bargein_knobs_are_deliverable_through_the_documented_path():
    compose = yaml.safe_load(read_text("docker-compose.yml"))
    smartpbx_env = compose["services"]["kavya-smartpbx"]["environment"]
    for name in ("BARGEIN_MIN_CHARS", "BARGEIN_DEBOUNCE_SECONDS"):
        assert smartpbx_env.get(name) == f"${{{name}}}", (
            f"{name} must be in the smartpbx environment allowlist to reach the container"
        )
    example = read_text(".env.example")
    runbook = read_text("SMARTPBX_RUNBOOK.md")
    for name, default in (("BARGEIN_MIN_CHARS", "12"), ("BARGEIN_DEBOUNCE_SECONDS", "0.6")):
        assert f"{name}={default}" in example, f"{name} missing from .env.example"
        assert f"{name}={default}" in runbook, f"{name} missing from the runbook env template"


def test_generic_deploy_and_pr_impact_exclude_kavya_from_image_routing():
    """Kavya images have a separately reviewed probe -> publisher -> runbook path."""
    workflows = PROJECT_ROOT.parent / ".github" / "workflows"
    deploy_on_push = (workflows / "deploy-on-push.yml").read_text(encoding="utf-8")
    pr_impact = (workflows / "pr-deploy-impact.yml").read_text(encoding="utf-8")

    for workflow in (deploy_on_push, pr_impact):
        assert '"Kavya:kavya"' in workflow
        assert 'if [ "$id" = "kavya" ]' in workflow
        assert "excluded" in workflow.lower()
        assert "probe" in workflow.lower()
        assert "publisher" in workflow.lower()
        assert "runbook" in workflow.lower()

    # The generic matrix must never carry a Kavya image row into deploy.yml.
    assert 'items="${items}{\\"agent\\":\\"${id}\\",\\"mode\\":\\"${mode}\\"},"' in deploy_on_push
    kavya_guard = deploy_on_push.split('if [ "$id" = "kavya" ]', 1)[1]
    assert kavya_guard.find('continue') < kavya_guard.find('items="${items}')


def test_kavya_exclusion_keeps_other_registry_agents_on_generic_image_routing():
    workflows = PROJECT_ROOT.parent / ".github" / "workflows"
    deploy_on_push = (workflows / "deploy-on-push.yml").read_text(encoding="utf-8")
    pr_impact = (workflows / "pr-deploy-impact.yml").read_text(encoding="utf-8")

    # The exclusion is narrow: compose-backed registry detection remains the
    # generic path for other agents and continues to report image impact.
    for workflow in (deploy_on_push, pr_impact):
        assert '"Flico Agent:flico"' in workflow
        assert "ghcr\\.io/" in workflow
        assert "mode=image" in workflow


def test_kavya_generic_rejection_and_reviewed_image_gates_remain_intact():
    workflows = PROJECT_ROOT.parent / ".github" / "workflows"
    generic_deploy = (workflows / "deploy.yml").read_text(encoding="utf-8")
    probe = (workflows / "probe-kavya-image.yml").read_text(encoding="utf-8")
    publisher = (workflows / "build-kavya-image.yml").read_text(encoding="utf-8")

    assert '"$AGENT" == "kavya" && "$MODE" == "image"' in generic_deploy
    assert "Kavya image mode is disabled" in generic_deploy

    assert "repository_dispatch:" in probe
    assert "kavya_image_read_only_probe" in probe
    assert "contents: read" in probe
    assert "workflow_dispatch:" in publisher
    assert "expected_sha" in publisher
    assert "fresh successful read-only probe" in publisher
    assert "org.opencontainers.image.revision" in publisher


SINHALA_LLM_DEFAULTS = {
    "SMARTPBX_SINHALA_LLM_PROVIDER": "gemini",
    "SMARTPBX_SINHALA_GEMINI_LLM_MODEL": "gemini-3.7-flash",
    "SMARTPBX_SINHALA_GEMINI_THINKING_LEVEL": "low",
    "SMARTPBX_SINHALA_GEMINI_MAX_TOKENS": "600",
}

SINHALA_GEMINI_TTS_DEFAULTS = {
    "SMARTPBX_SINHALA_GEMINI_TTS_MODEL": "gemini-3.1-flash-tts-preview",
    "SMARTPBX_SINHALA_GEMINI_TTS_VOICE": "Vindemiatrix",
    "SMARTPBX_SINHALA_GEMINI_TTS_TIMEOUT_SECONDS": "15.0",
}


def parse_redacted_active_env_assignments(text: str) -> dict[str, object]:
    """Return assignment metadata without retaining dotenv values."""
    counts: dict[str, int] = {}
    classifications: dict[str, list[str]] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        counts[name] = counts.get(name, 0) + 1
        classifications.setdefault(name, []).append(
            "blank" if value == "" else "whitespace" if not value.strip() else "nonblank"
        )
    return {"counts": counts, "classifications": classifications}


def smartpbx_protected_template(runbook: str) -> str:
    """Extract the dotenv fence under the isolated SmartPBX heading only."""
    heading = "## Create the isolated server-side environment"
    scoped = runbook.split(heading, 1)[1]
    match = re.search(r"(?ms)^```dotenv\n(?P<body>.*?)^```\s*$", scoped)
    assert match is not None
    return match.group("body")


def smartpbx_sinhala_rollback_transaction(runbook: str) -> str:
    marker = "```sh\n# SmartPBX Sinhala LLM rollback transaction"
    scoped = runbook.split(marker, 1)[1]
    return "# SmartPBX Sinhala LLM rollback transaction" + scoped.split("```", 1)[0]


def test_sinhala_smartpbx_settings_are_explicit_and_secret_safe():
    compose = yaml.safe_load(read_text("docker-compose.yml"))
    smartpbx = compose["services"]["kavya-smartpbx"]["environment"]
    legacy = compose["services"]["kavya"]

    assert smartpbx["SMARTPBX_LANGUAGE_SELECTION_TIMEOUT_SECONDS"] == (
        "${SMARTPBX_LANGUAGE_SELECTION_TIMEOUT_SECONDS:-8.0}"
    )
    assert {name: smartpbx.get(name) for name in SINHALA_LLM_DEFAULTS} == {
        name: f"${{{name}:-{value}}}" for name, value in SINHALA_LLM_DEFAULTS.items()
    }
    assert {name: smartpbx.get(name) for name in SINHALA_GEMINI_TTS_DEFAULTS} == {
        name: f"${{{name}:-{value}}}" for name, value in SINHALA_GEMINI_TTS_DEFAULTS.items()
    }
    assert smartpbx["GEMINI_API_KEY"] == "${GEMINI_API_KEY:-}"
    assert legacy["env_file"] == [".env"]
    assert not set(SINHALA_LLM_DEFAULTS).intersection(legacy["environment"])
    for name in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "HUMAN_AGENT_PHONE"):
        assert name not in smartpbx

    example = read_text(".env.example")
    for name, default in {
        **SINHALA_LLM_DEFAULTS,
        **SINHALA_GEMINI_TTS_DEFAULTS,
        "SMARTPBX_LANGUAGE_SELECTION_TIMEOUT_SECONDS": "8.0",
    }.items():
        assert re.search(rf"^{name}={re.escape(default)}$", example, re.MULTILINE)
    assert_exactly_one_blank_env_assignment(example, "GEMINI_API_KEY")


def test_gemini_key_assignment_contract_rejects_duplicate_whitespace_or_later_active_values():
    # Comments are documentation, not dotenv assignments. The protected
    # template permits exactly one active *blank* key assignment, while the
    # runtime gate separately requires exactly one stripped nonblank value.
    def runtime_ready(text: str) -> bool:
        parsed = parse_redacted_active_env_assignments(text)
        return (
            parsed["counts"].get("GEMINI_API_KEY") == 1
            and parsed["classifications"].get("GEMINI_API_KEY") == ["nonblank"]
        )

    commented_blank = "# GEMINI_API_KEY=ignored-comment\nGEMINI_API_KEY=\n"
    assert_exactly_one_blank_env_assignment(commented_blank, "GEMINI_API_KEY")
    assert not runtime_ready(commented_blank)
    assert runtime_ready("GEMINI_API_KEY=usable-secret\n")

    for invalid in (
        "GEMINI_API_KEY= \n",
        "GEMINI_API_KEY=\nGEMINI_API_KEY=\n",
        "GEMINI_API_KEY=\nGEMINI_API_KEY=later-active\n",
    ):
        parsed = parse_redacted_active_env_assignments(invalid)
        assert not runtime_ready(invalid)
        assert parsed["counts"]["GEMINI_API_KEY"] >= 1


def test_sinhala_smartpbx_runbook_documents_the_llm_tts_and_transaction_contract():
    runbook = read_text("SMARTPBX_RUNBOOK.md")
    section = runbook.split("## SmartPBX Sinhala menu and Gemini TTS", 1)[1].split(
        "\n## ", 1
    )[0]
    normalized = re.sub(r"\s+", " ", section)
    folded_normalized = normalized.casefold()

    for required in (
        "1 English, 2 Sinhala",
        "timeout defaults to English",
        "invalid selection replays once then defaults to English",
        "Twilio behavior is unchanged",
        "Press 1",
        "ElevenLabs",
        "Press 2",
        "Azure `si-LK`",
        "gemini-3.7-flash",
        "low",
        "600",
        "GEMINI_FAILOVER_TO_CLAUDE",
        "usable Anthropic readiness",
        "gemini-3.1-flash-tts-preview",
        "Vindemiatrix",
        "no-output",
        "stripped",
        "before exposing the bilingual menu",
        "offline-only",
        "Gemini Transcribe",
        "Chirp 2",
        "StreamingRecognize",
        "update_smartpbx_sinhala_provider.sh apply /opt/kavya/.env.smartpbx claude",
        "update_smartpbx_sinhala_provider.sh restore /opt/kavya/.env.smartpbx",
        "update_smartpbx_sinhala_provider.sh cleanup /opt/kavya/.env.smartpbx",
        "config validation failure",
        "post-recreate failure",
        "same reviewed identity",
        "health/status checks",
        "two-language canary call checklist",
    ):
        assert required.casefold() in folded_normalized
    assert "authenticated `/smartpbx/status`".casefold() in folded_normalized
    assert "bounded provider/event/outcome diagnostics".casefold() in folded_normalized
    for stale in ("Only its TTS uses Gemini", "Sinhala retains the existing Claude LLM"):
        assert stale.casefold() not in folded_normalized
    transaction = smartpbx_sinhala_rollback_transaction(runbook)
    assert re.search(r"(?m)^.*docker compose.*\bup\b", transaction) is None
    assert re.search(r"(?m)^.*docker compose.*\brestart\b", transaction) is None


def test_smartpbx_sinhala_llm_rendered_compose_and_protected_template_contract(tmp_path):
    # Use an isolated project directory. Never let Compose discover the real
    # repository .env, .env.smartpbx, Docker configuration, or any secret.
    project = tmp_path / "compose-project"
    project.mkdir()
    compose_file = project / "docker-compose.yml"
    shutil.copyfile(PROJECT_ROOT / "docker-compose.yml", compose_file)
    names = sorted(
        set(re.findall(r"\$\{([A-Z0-9_]+)(?::-[^}]*)?\}", read_text("docker-compose.yml")))
    )
    safe_env = project / ".env"
    safe_smartpbx_env = project / ".env.smartpbx"
    safe_values = {name: "" for name in names}
    safe_values["SMARTPBX_IMAGE_TAG"] = "reviewed-image"
    # The legacy service deliberately receives its ordinary `.env`, while the
    # SmartPBX profile receives the protected `.env.smartpbx` allowlist.  Keep
    # SmartPBX-only LLM controls out of the former in this isolated render.
    legacy_values = {
        name: value for name, value in safe_values.items()
        if name not in SINHALA_LLM_DEFAULTS
    }
    safe_env.write_text(
        "\n".join(f"{name}={value}" for name, value in legacy_values.items()) + "\n",
        encoding="utf-8",
    )
    safe_smartpbx_env.write_text(
        "\n".join(f"{name}={value}" for name, value in safe_values.items()) + "\n",
        encoding="utf-8",
    )

    def render(overrides: dict[str, str]) -> dict:
        values = {**safe_values, **overrides}
        safe_smartpbx_env.write_text(
            "\n".join(f"{name}={value}" for name, value in values.items()) + "\n",
            encoding="utf-8",
        )
        rendered = subprocess.run(
            [
                "docker",
                "compose",
                "-f",
                str(compose_file),
                "--env-file",
                str(safe_smartpbx_env),
                "--profile",
                "smartpbx",
                "config",
                "--format",
                "json",
            ],
            cwd=project,
            env={
                "PATH": os.environ.get("PATH", ""),
                "HOME": str(project / "empty-home"),
                "DOCKER_CONFIG": str(project / "empty-docker-config"),
            },
            text=True,
            capture_output=True,
            check=False,
        )
        assert rendered.returncode == 0, rendered.stderr
        return json.loads(rendered.stdout)

    defaults = render({})["services"]
    assert set(defaults) == {"kavya", "kavya-smartpbx"}
    assert {
        name: defaults["kavya-smartpbx"]["environment"][name]
        for name in SINHALA_LLM_DEFAULTS
    } == SINHALA_LLM_DEFAULTS
    assert not set(SINHALA_LLM_DEFAULTS).intersection(defaults["kavya"]["environment"])

    overrides = {
        "SMARTPBX_SINHALA_LLM_PROVIDER": "claude",
        "SMARTPBX_SINHALA_GEMINI_LLM_MODEL": "gemini-3.7-flash-canary",
        "SMARTPBX_SINHALA_GEMINI_THINKING_LEVEL": "high",
        "SMARTPBX_SINHALA_GEMINI_MAX_TOKENS": "777",
    }
    rendered_overrides = render(overrides)["services"]
    assert {
        name: rendered_overrides["kavya-smartpbx"]["environment"][name]
        for name in overrides
    } == overrides
    assert not set(overrides).intersection(rendered_overrides["kavya"]["environment"])

    parsed = parse_redacted_active_env_assignments(
        smartpbx_protected_template(read_text("SMARTPBX_RUNBOOK.md"))
    )
    for name in {**SINHALA_LLM_DEFAULTS, **SINHALA_GEMINI_TTS_DEFAULTS}:
        assert parsed["counts"].get(name) == 1
        assert parsed["classifications"].get(name) == ["nonblank"]
    assert parsed["counts"].get("GEMINI_API_KEY") == 1
    assert parsed["classifications"].get("GEMINI_API_KEY") == ["blank"]


def test_sinhala_provider_updater_is_a_closed_redacted_root_transaction():
    updater = PROJECT_ROOT / "scripts" / "update_smartpbx_sinhala_provider.sh"
    assert updater.is_file()
    assert os.access(updater, os.X_OK)
    script = updater.read_text(encoding="utf-8")
    for required in (
        "PROTECTED_FILE=/opt/kavya/.env.smartpbx",
        "TRANSACTION_DIR=/opt/kavya/.smartpbx-sinhala-rollback",
        "[[ $EUID -eq 0 ]]",
        'apply) [[ $# -eq 3 && $2 == "$PROTECTED_FILE" && $3 == "$PROVIDER_VALUE" ]]',
        'restore|cleanup) [[ $# -eq 2 && $2 == "$PROTECTED_FILE" ]]',
        "flock -n 9",
        "cmp -s",
        "SMARTPBX_SINHALA_PROVIDER_APPLIED",
        "SMARTPBX_SINHALA_PROVIDER_RESTORED",
        "SMARTPBX_SINHALA_PROVIDER_CLEANED",
        "SMARTPBX_SINHALA_PROVIDER_UPDATE_FAILED",
    ):
        assert required in script
    for forbidden in ("cat \"$PROTECTED_FILE\"", "diff ", "set -x"):
        assert forbidden not in script
    runbook = read_text("SMARTPBX_RUNBOOK.md")
    for required in (
        "The guarded image deployer does not install host-side scripts.",
        "sudo test -x /opt/kavya/scripts/update_smartpbx_sinhala_provider.sh",
        "sudo chown root:root /opt/kavya/scripts/update_smartpbx_sinhala_provider.sh",
        "sudo chmod 0755 /opt/kavya/scripts/update_smartpbx_sinhala_provider.sh",
        "sudo install -d -o root -g root -m 0700 /opt/kavya/.smartpbx-sinhala-rollback",
    ):
        assert required in runbook


def test_sinhala_provider_updater_transaction_uses_only_its_private_backup(tmp_path):
    """Exercise a path-rewritten copy: never /opt/kavya or a real credential.

    The production script deliberately accepts one fixed root path. Rewriting
    just the literal path and root predicate gives the same shell transaction a
    non-root, fake-command harness while preserving the closed original
    interface asserted above.
    """
    protected = tmp_path / "protected.env"
    transaction = tmp_path / "rollback"
    protected.write_text(
        "UNCHANGED_LINE=ok\nSMARTPBX_SINHALA_LLM_PROVIDER=gemini\n",
        encoding="utf-8",
    )
    transaction.mkdir(mode=0o700)

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    for name, body in {
        "stat": """#!/usr/bin/env bash
if [[ $1 == -c ]]; then
  [[ ${!#} == */rollback ]] && printf '0:0:700\\n' || printf '0:0:600\\n'
fi
""",
        "chown": "#!/usr/bin/env bash\nexit 0\n",
        "chmod": "#!/usr/bin/env bash\nexit 0\n",
        "install": "#!/usr/bin/env bash\nexit 0\n",
        "curl": "#!/usr/bin/env bash\nprintf '%s\\n' '{\"status\":\"ok\",\"service_mode\":\"smartpbx\",\"active_sessions\":0,\"transfer_enabled\":false}'\n",
        "jq": "#!/usr/bin/env bash\ncat >/dev/null\nexit 0\n",
    }.items():
        path = fake_bin / name
        path.write_text(body, encoding="utf-8")
        path.chmod(0o755)

    updater = (PROJECT_ROOT / "scripts" / "update_smartpbx_sinhala_provider.sh").read_text(
        encoding="utf-8"
    )
    updater = updater.replace("/opt/kavya/.env.smartpbx", str(protected))
    updater = updater.replace("/opt/kavya/.smartpbx-sinhala-rollback", str(transaction))
    updater = updater.replace("[[ $EUID -eq 0 ]]", "true")
    runner = tmp_path / "update.sh"
    runner.write_text(updater, encoding="utf-8")
    runner.chmod(0o755)
    env = {"PATH": f"{fake_bin}:{os.environ['PATH']}"}

    def run(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(runner), *args], text=True, capture_output=True, env=env, check=False
        )

    initial = protected.read_text(encoding="utf-8")
    rejected = run("apply", str(tmp_path / "other.env"), "claude")
    assert rejected.returncode != 0
    assert protected.read_text(encoding="utf-8") == initial
    assert not list(transaction.iterdir())

    applied = run("apply", str(protected), "claude")
    assert applied.returncode == 0
    assert applied.stdout == "SMARTPBX_SINHALA_PROVIDER_APPLIED\n"
    assert protected.read_text(encoding="utf-8") == (
        "UNCHANGED_LINE=ok\nSMARTPBX_SINHALA_LLM_PROVIDER=claude\n"
    )
    assert {entry.name for entry in transaction.iterdir()} == {"backup.env", "metadata", "pending"}

    restored = run("restore", str(protected))
    assert restored.returncode == 0
    assert restored.stdout == "SMARTPBX_SINHALA_PROVIDER_RESTORED\n"
    assert protected.read_text(encoding="utf-8") == initial
    assert {entry.name for entry in transaction.iterdir()} == {"backup.env", "metadata", "pending"}
    cleaned = run("cleanup", str(protected))
    assert cleaned.returncode == 0
    assert cleaned.stdout == "SMARTPBX_SINHALA_PROVIDER_CLEANED\n"
    assert not list(transaction.iterdir())


def test_documented_sinhala_rollback_transaction_orders_failures_and_cleanup(tmp_path):
    """Run the literal runbook transaction with fake host commands only."""
    runbook_script = smartpbx_sinhala_rollback_transaction(
        read_text("SMARTPBX_RUNBOOK.md")
    )

    def scenario(name: str, **switches: str) -> tuple[subprocess.CompletedProcess[str], list[str], Path, Path]:
        root = tmp_path / name
        app = root / "app"
        scripts = app / "scripts"
        tx = app / ".smartpbx-sinhala-rollback"
        fake_bin = root / "bin"
        scripts.mkdir(parents=True)
        tx.mkdir(mode=0o700)
        fake_bin.mkdir()
        protected = app / ".env.smartpbx"
        protected.write_text(
            "UNCHANGED_LINE=ok\nSMARTPBX_SINHALA_LLM_PROVIDER=gemini\nSMARTPBX_WS_TOKEN=fake\n",
            encoding="utf-8",
        )
        (app / ".env").write_text("SAFE=1\n", encoding="utf-8")
        log = root / "operations.log"
        marker = root / "deploy-once"

        def fake(name: str, body: str) -> None:
            path = fake_bin / name
            path.write_text("#!/usr/bin/env bash\nset -Eeuo pipefail\n" + body, encoding="utf-8")
            path.chmod(0o755)

        fake("sudo", 'printf "sudo %s\\n" "$*" >> "$FAKE_LOG"\nexec "$@"\n')
        fake(
            "docker",
            'printf "docker %s\\n" "$*" >> "$FAKE_LOG"\n'
            '[[ "$*" == *" config"* ]] || exit 1\n'
            '[[ ${FAKE_CONFIG_FAIL:-0} == 1 ]] && exit 1\n',
        )
        fake(
            "curl",
            'printf "curl %s\\n" "$*" >> "$FAKE_LOG"\n'
            "printf '%s\\n' '{\"status\":\"ok\",\"service_mode\":\"smartpbx\",\"active_sessions\":0,\"transfer_enabled\":false}'\n",
        )
        fake("jq", 'printf "jq %s\\n" "$*" >> "$FAKE_LOG"\ncat >/dev/null\n')
        for command in ("stat", "install", "mktemp", "cp", "mv", "cmp"):
            real = shutil.which(command)
            assert real is not None
            fake(
                command,
                f'printf "{command} %s\\n" "$*" >> "$FAKE_LOG"\nexec {real} "$@"\n',
            )
        fake(
            "update_smartpbx_sinhala_provider.sh",
            'printf "updater %s\\n" "$*" >> "$FAKE_LOG"\n'
            'command=$1; protected=$2\n'
            'if [[ $command == apply ]]; then\n'
            '  [[ ${FAKE_APPLY_FAIL:-0} == 1 ]] && exit 1\n'
            '  stat -c %a "$protected" >/dev/null\n'
            '  install -m 600 /dev/null "$TX/backup.env"\n'
            '  cp "$protected" "$TX/backup.env"\n'
            '  tmp=$(mktemp "$TX/.candidate.XXXXXX")\n'
            '  sed "s/^SMARTPBX_SINHALA_LLM_PROVIDER=.*/SMARTPBX_SINHALA_LLM_PROVIDER=claude/" "$protected" > "$tmp"\n'
            '  cmp -s "$protected" "$protected"\n'
            '  mv "$tmp" "$protected"\n'
            '  : > "$TX/pending"\n'
            'elif [[ $command == restore ]]; then\n'
            '  cp "$TX/backup.env" "$protected"\n'
            'elif [[ $command == cleanup ]]; then\n'
            '  [[ -f $TX/backup.env ]] || exit 1\n'
            '  printf "cleanup_backup_present\\n" >> "$FAKE_LOG"\n'
            '  curl --fail http://127.0.0.1:8006/health | jq -e . >/dev/null\n'
            '  curl --fail http://127.0.0.1:8006/smartpbx/status | jq -e . >/dev/null\n'
            '  rm -f "$TX"/*\n'
            'else exit 1\nfi\n',
        )
        fake(
            "deploy_smartpbx_image.sh",
            'printf "deploy %s\\n" "$*" >> "$FAKE_LOG"\n'
            'if [[ ${FAKE_DEPLOY_FAIL_ONCE:-0} == 1 && ! -e $DEPLOY_MARKER ]]; then touch "$DEPLOY_MARKER"; exit 1; fi\n',
        )
        # The runbook invokes fixed /opt/kavya script paths. Point those paths
        # at this scenario's fake commands without changing its command order.
        for command in ("update_smartpbx_sinhala_provider.sh", "deploy_smartpbx_image.sh"):
            target = scripts / command
            target.write_text((fake_bin / command).read_text(encoding="utf-8"), encoding="utf-8")
            target.chmod(0o755)
        driver = root / "runbook-transaction.sh"
        driver.write_text(
            "#!/usr/bin/env bash\n"
            "REVIEWED_CI_SHORT_SHA=abcdef0\n"
            "REVIEWED_FULL_COMMIT_SHA=abcdef0123456789abcdef0123456789abcdef01\n"
            "REVIEWED_IMAGE_DIGEST=sha256:" + "0" * 64 + "\n"
            + runbook_script.replace("/opt/kavya", str(app)),
            encoding="utf-8",
        )
        driver.chmod(0o755)
        env = {
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "FAKE_LOG": str(log),
            "TX": str(tx),
            "DEPLOY_MARKER": str(marker),
            **switches,
        }
        result = subprocess.run([str(driver)], text=True, capture_output=True, env=env, check=False)
        operations = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
        return result, operations, protected, tx

    apply_failed, operations, protected, tx = scenario("apply-failure", FAKE_APPLY_FAIL="1")
    assert apply_failed.returncode != 0
    assert operations == [f"sudo {protected.parent / 'scripts' / 'update_smartpbx_sinhala_provider.sh'} apply {protected} claude", f"updater apply {protected} claude"]
    assert "SMARTPBX_SINHALA_LLM_PROVIDER=gemini" in protected.read_text(encoding="utf-8")
    assert not list(tx.iterdir())

    config_failed, operations, protected, tx = scenario("config-failure", FAKE_CONFIG_FAIL="1")
    assert config_failed.returncode != 0
    assert any(line.startswith("updater apply ") for line in operations)
    restore_index = next(i for i, line in enumerate(operations) if line.startswith("updater restore "))
    cleanup_index = next(i for i, line in enumerate(operations) if line.startswith("updater cleanup "))
    assert restore_index < cleanup_index
    assert not any(line.startswith("deploy ") for line in operations)
    assert len([line for line in operations if line.startswith("updater cleanup ")]) == 1
    assert "SMARTPBX_SINHALA_LLM_PROVIDER=gemini" in protected.read_text(encoding="utf-8")
    assert not list(tx.iterdir())

    retried, operations, protected, tx = scenario("post-recreate-failure", FAKE_DEPLOY_FAIL_ONCE="1")
    assert retried.returncode == 0
    deploys = [line for line in operations if line.startswith("deploy ")]
    assert len(deploys) == 2 and deploys[0] == deploys[1]
    restore_index = next(i for i, line in enumerate(operations) if line.startswith("updater restore "))
    assert restore_index < operations.index(deploys[1])
    assert operations.index("cleanup_backup_present") < next(
        i for i, line in enumerate(operations) if line.startswith("curl ")
    )
    assert len([line for line in operations if line.startswith("updater cleanup ")]) == 1
    assert not list(tx.iterdir())

    successful, operations, _protected, tx = scenario("success")
    assert successful.returncode == 0
    assert len([line for line in operations if line.startswith("deploy ")]) == 1
    assert len([line for line in operations if line.startswith("updater cleanup ")]) == 1
    assert not list(tx.iterdir())
