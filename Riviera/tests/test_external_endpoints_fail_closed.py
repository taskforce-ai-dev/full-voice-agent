"""Riviera must never reach a shared (another property's) endpoint by default.

Isolation contract for the three outbound integrations that used to inherit
Kavya's defaults: the Yanolja-style PMS (`YANOLJA_BASE_URL`), the post-call
call-log webhook and the handover WhatsApp webhook (both on `N8N_BASE_URL`).

Every one of them now fails CLOSED: a missing or blank env var resolves to
"unconfigured", the booking tools are withheld, and NO outbound request is
made until a Riviera-owned destination is explicitly configured.

Module-level defaults are checked in a subprocess (they are import-time
constants); runtime behaviour is checked in-process by pinning the module
attribute to blank and asserting that no HTTP session is ever touched.
"""
from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import booking_api  # noqa: E402
import handover  # noqa: E402
import post_call  # noqa: E402
import tools as tools_mod  # noqa: E402
import yanolja_client  # noqa: E402

SHARED_HOSTS = ("yanolja.taskforceai.tech", "automation.taskforceai.tech")


def _import_attr(module: str, attr: str, env_overrides: dict[str, str]) -> str:
    """Import `module` fresh in a subprocess with `env_overrides` and return repr(attr)."""
    env = {
        k: v for k, v in os.environ.items()
        if k not in (
            "YANOLJA_BASE_URL", "N8N_BASE_URL", "N8N_POSTCALL_WEBHOOK", "N8N_HANDOVER_WEBHOOK",
        )
    }
    env.update(env_overrides)
    result = subprocess.run(
        [sys.executable, "-c", f"import {module}; print(repr({module}.{attr}))"],
        cwd=PROJECT_ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    return ast.literal_eval(result.stdout.strip().splitlines()[-1])


async def _session_must_not_be_used():
    raise AssertionError("an outbound HTTP session was requested while the endpoint is unconfigured")


# ---------------------------------------------------------------------------
# Import-time defaults: missing AND blank both mean "unconfigured"
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("env", [{}, {"YANOLJA_BASE_URL": ""}, {"YANOLJA_BASE_URL": "   "}])
def test_yanolja_base_url_has_no_default(env):
    assert _import_attr("yanolja_client", "YANOLJA_BASE_URL", env) == ""


@pytest.mark.parametrize("env", [{}, {"N8N_BASE_URL": ""}, {"N8N_BASE_URL": "   "}])
def test_post_call_n8n_base_url_has_no_default(env):
    assert _import_attr("post_call", "N8N_BASE_URL", env) == ""


@pytest.mark.parametrize(
    "env", [{}, {"N8N_POSTCALL_WEBHOOK": ""}, {"N8N_POSTCALL_WEBHOOK": "   "}]
)
def test_post_call_webhook_path_has_no_default(env):
    """The shared workflow path is not a default either -- host AND path are explicit."""
    assert _import_attr("post_call", "N8N_POSTCALL_WEBHOOK", env) == ""


@pytest.mark.parametrize("env", [{}, {"N8N_BASE_URL": ""}, {"N8N_BASE_URL": "   "}])
def test_handover_n8n_base_url_has_no_default(env):
    assert _import_attr("handover", "N8N_BASE_URL", env) == ""


@pytest.mark.parametrize(
    "env", [{}, {"N8N_HANDOVER_WEBHOOK": ""}, {"N8N_HANDOVER_WEBHOOK": "   "}]
)
def test_handover_webhook_path_has_no_default(env):
    """The handover path is explicit too -- no hard-coded /webhook/... fallback."""
    assert _import_attr("handover", "N8N_HANDOVER_WEBHOOK", env) == ""


def test_explicit_handover_webhook_path_is_honoured():
    assert _import_attr(
        "handover", "N8N_HANDOVER_WEBHOOK", {"N8N_HANDOVER_WEBHOOK": " /webhook/riviera-handover "}
    ) == "/webhook/riviera-handover"


def test_explicit_riviera_endpoints_are_honoured():
    assert _import_attr(
        "yanolja_client", "YANOLJA_BASE_URL", {"YANOLJA_BASE_URL": "https://pms.riviera.example/api/"}
    ) == "https://pms.riviera.example/api"
    assert _import_attr(
        "post_call", "N8N_BASE_URL", {"N8N_BASE_URL": "https://n8n.riviera.example/"}
    ) == "https://n8n.riviera.example"


# ---------------------------------------------------------------------------
# PMS: credentials without a dedicated URL must NOT enable anything
# ---------------------------------------------------------------------------

def test_pms_credentials_without_base_url_leave_pms_unconfigured(monkeypatch):
    monkeypatch.setattr(yanolja_client, "YANOLJA_BASE_URL", "")
    monkeypatch.setattr(yanolja_client, "YANOLJA_USERNAME", "riya")
    monkeypatch.setattr(yanolja_client, "YANOLJA_PASSWORD", "secret")

    assert yanolja_client.is_configured() is False
    assert booking_api.is_configured() is False
    # No booking tools are offered to any LLM provider.
    assert tools_mod.get_tools() == []
    assert tools_mod.get_tools_openai() == []
    assert tools_mod.get_tools_gemini() == []


@pytest.mark.asyncio
async def test_pms_login_and_requests_make_no_http_call_without_base_url(monkeypatch):
    monkeypatch.setattr(yanolja_client, "YANOLJA_BASE_URL", "")
    monkeypatch.setattr(yanolja_client, "YANOLJA_USERNAME", "riya")
    monkeypatch.setattr(yanolja_client, "YANOLJA_PASSWORD", "secret")
    monkeypatch.setattr(yanolja_client, "_token", None)

    with patch.object(yanolja_client, "_get_session", _session_must_not_be_used):
        with pytest.raises(yanolja_client.YanoljaError):
            await yanolja_client.login()
        with pytest.raises(yanolja_client.YanoljaError):
            await yanolja_client._request("GET", "/rooms")
        with pytest.raises(yanolja_client.YanoljaError):
            await yanolja_client.list_rooms()


@pytest.mark.asyncio
async def test_availability_check_degrades_cleanly_without_pms_url(monkeypatch):
    """The booking tool path returns an error dict -- it never guesses an endpoint."""
    import yanolja_service

    monkeypatch.setattr(yanolja_client, "YANOLJA_BASE_URL", "")
    monkeypatch.setattr(yanolja_client, "YANOLJA_USERNAME", "riya")
    monkeypatch.setattr(yanolja_client, "YANOLJA_PASSWORD", "secret")
    monkeypatch.setattr(yanolja_client, "_token", None)

    with patch.object(yanolja_client, "_get_session", _session_must_not_be_used):
        result = await yanolja_service.derive_availability(
            "2026-11-10", "2026-11-12", num_adults=2, num_children=0
        )
    assert result.get("error"), result


# ---------------------------------------------------------------------------
# Post-call: no transcript leaves the box until a destination is configured
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("privacy_safe", [False, True])
async def test_post_call_dispatch_is_skipped_without_n8n_base_url(monkeypatch, caplog, privacy_safe):
    monkeypatch.setattr(post_call, "N8N_BASE_URL", "")
    assert post_call.is_post_call_dispatch_configured() is False

    with patch.object(booking_api, "get_session", _session_must_not_be_used):
        await post_call._post_to_n8n({"call_sid": "CA-unconfigured"}, privacy_safe=privacy_safe)

    if privacy_safe:
        assert "event=n8n_skipped reason=unconfigured" in caplog.text
        assert "CA-unconfigured" not in caplog.text
    else:
        assert "not configured" in caplog.text


@pytest.mark.asyncio
async def test_post_call_dispatch_is_skipped_when_only_the_path_is_missing(monkeypatch):
    """A configured host with a blank path must not POST to the host root either."""
    monkeypatch.setattr(post_call, "N8N_BASE_URL", "https://n8n.riviera.example")
    monkeypatch.setattr(post_call, "N8N_POSTCALL_WEBHOOK", "")
    assert post_call.is_post_call_dispatch_configured() is False

    with patch.object(booking_api, "get_session", _session_must_not_be_used):
        await post_call._post_to_n8n({"call_sid": "CA-no-path"})


@pytest.mark.asyncio
async def test_post_call_orchestrator_never_posts_without_n8n_base_url(monkeypatch):
    """End-to-end through process_post_call_data: extraction runs, nothing is sent."""
    monkeypatch.setattr(post_call, "N8N_BASE_URL", "")
    monkeypatch.setattr(post_call, "dashboard_client", None)

    async def extraction(**_kwargs):
        return {"call_outcome": "other", "follow_up_needed": "No"}

    monkeypatch.setattr(post_call, "extract_booking_details", extraction)

    with patch.object(booking_api, "get_session", _session_must_not_be_used):
        await post_call.process_post_call_data(
            call_sid="CA-unconfigured",
            lang="en",
            caller_phone="+94771234567",
            full_transcript=[{"role": "user", "text": "hello"}],
            call_start_time="2026-09-15T00:00:00+00:00",
            call_end_time="2026-09-15T00:01:00+00:00",
            llm_provider="openai",
        )


@pytest.mark.asyncio
async def test_post_call_posts_only_to_the_explicitly_configured_destination(monkeypatch):
    monkeypatch.setattr(post_call, "N8N_BASE_URL", "https://n8n.riviera.example")
    seen: list[str] = []

    class _Resp:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def text(self):
            return ""

    class _Session:
        def post(self, url, json=None, **kwargs):  # noqa: A002
            seen.append(url)
            return _Resp()

    async def _get_session():
        return _Session()

    with patch.object(booking_api, "get_session", _get_session):
        await post_call._post_to_n8n({"call_sid": "CA1"})

    assert seen == ["https://n8n.riviera.example/webhook/post-call-data"]


# ---------------------------------------------------------------------------
# Handover: guest contact details never go to an unconfigured host
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("privacy_safe", [False, True])
async def test_handover_notification_refuses_without_n8n_base_url(monkeypatch, privacy_safe):
    monkeypatch.setattr(handover, "N8N_BASE_URL", "")
    assert handover.is_handover_webhook_configured() is False

    with patch.object(booking_api, "get_session", _session_must_not_be_used):
        result = await handover.send_handover_notification(
            call_sid="CA999",
            customer_name="Chanya",
            customer_whatsapp="0771234567",
            call_summary="Wanted a group discount.",
            human_agent_whatsapp="+94711754668",
            privacy_safe=privacy_safe,
        )

    assert result["ok"] is False
    assert result["error"] == "handover_webhook_not_configured"
    if privacy_safe:
        assert "payload" not in result


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["", "   "])
async def test_handover_notification_refuses_when_only_the_path_is_missing(monkeypatch, path):
    """A configured host with a blank handover path must not POST to the host root."""
    monkeypatch.setattr(handover, "N8N_BASE_URL", "https://n8n.riviera.example")
    monkeypatch.setattr(handover, "N8N_HANDOVER_WEBHOOK", path)
    assert handover.is_handover_webhook_configured() is False

    with patch.object(booking_api, "get_session", _session_must_not_be_used):
        result = await handover.send_handover_notification(
            call_sid="CA998",
            customer_name="Chanya",
            customer_whatsapp="0771234567",
            call_summary="Wanted a group discount.",
            human_agent_whatsapp="+94711754668",
        )

    assert result["ok"] is False
    assert result["error"] == "handover_webhook_not_configured"


# ---------------------------------------------------------------------------
# Deployment surface: compose / env template carry no shared-host fallback
# ---------------------------------------------------------------------------

def test_compose_env_passthroughs_have_no_shared_host_defaults():
    compose_text = (PROJECT_ROOT / "docker-compose.yml").read_text()
    compose = yaml.safe_load(compose_text)
    for service_name, service in compose["services"].items():
        env = service.get("environment") or {}
        if "YANOLJA_BASE_URL" in env:
            assert env["YANOLJA_BASE_URL"] == "${YANOLJA_BASE_URL:-}", service_name
        if "N8N_BASE_URL" in env:
            assert env["N8N_BASE_URL"] == "${N8N_BASE_URL:-}", service_name
        if "N8N_POSTCALL_WEBHOOK" in env:
            assert env["N8N_POSTCALL_WEBHOOK"] == "${N8N_POSTCALL_WEBHOOK:-}", service_name
        if "N8N_HANDOVER_WEBHOOK" in env:
            assert env["N8N_HANDOVER_WEBHOOK"] == "${N8N_HANDOVER_WEBHOOK:-}", service_name
    for host in SHARED_HOSTS:
        assert host not in compose_text
    # The SmartPBX service uses an explicit allowlist (no env_file); every
    # fail-closed destination must be passed through or it can never be set.
    smartpbx_env = compose["services"]["riviera-smartpbx"]["environment"]
    for key in ("YANOLJA_BASE_URL", "N8N_BASE_URL", "N8N_POSTCALL_WEBHOOK", "N8N_HANDOVER_WEBHOOK"):
        assert key in smartpbx_env, f"{key} missing from the riviera-smartpbx allowlist"


def test_env_example_leaves_external_destinations_blank():
    lines = (PROJECT_ROOT / ".env.example").read_text().splitlines()
    values = {
        line.split("=", 1)[0]: line.split("=", 1)[1]
        for line in lines
        if "=" in line and not line.lstrip().startswith("#")
    }
    assert values.get("YANOLJA_BASE_URL", "") == ""
    assert values.get("N8N_BASE_URL", "") == ""
    assert values.get("N8N_POSTCALL_WEBHOOK", "") == ""
    assert values.get("N8N_HANDOVER_WEBHOOK", "") == ""


def test_runbook_env_template_leaves_external_destinations_blank():
    runbook = (PROJECT_ROOT / "SMARTPBX_RUNBOOK.md").read_text()
    for key in ("YANOLJA_BASE_URL", "N8N_BASE_URL", "N8N_POSTCALL_WEBHOOK", "N8N_HANDOVER_WEBHOOK"):
        assert f"\n{key}=\n" in runbook, f"{key} must be blank in the runbook env template"


def test_runtime_modules_do_not_hardcode_shared_hosts():
    """No Riviera runtime .py may mention another agent's endpoints at all."""
    offenders = []
    for path in PROJECT_ROOT.glob("*.py"):
        text = path.read_text(errors="replace")
        for host in SHARED_HOSTS:
            if host in text:
                offenders.append(f"{path.name}: {host}")
    assert not offenders, offenders
