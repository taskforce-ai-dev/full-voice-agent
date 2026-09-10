"""Disposable lifecycle verification contracts are enforced in CI.

These tests use an in-process fixture adapter only.  They never use Docker,
network services, customer data, or credentials.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from smartpbx_agent_factory.resources import AllocationRegistry, derive_resources
from smartpbx_agent_factory.schema import parse_manifest
from smartpbx_agent_factory.verify import (
    DisposableClientResult,
    VerificationError,
    readiness_report,
    run_disposable_client,
    verify_generated_backend,
)


FIXTURES = Path(__file__).parent / "fixtures"


def fixture_resources():
    raw = json.loads((FIXTURES / "acme-minimal.json").read_text(encoding="utf-8"))
    return derive_resources(parse_manifest(raw, approved_source_roots=(Path.cwd(),)), AllocationRegistry())


def _artifact_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.name != ".smartpbx-factory-provenance.json":
            digest.update(path.relative_to(root).as_posix().encode("utf-8"))
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
    return digest.hexdigest()


def fixture_backend(root: Path, *, ci: bool = True) -> Path:
    root.mkdir()
    (root / "Dockerfile").write_text("FROM python:3.11-slim\nCOPY . .\n", encoding="utf-8")
    (root / "server.py").write_text(
        "ROUTES = ('/health', '/smartpbx/status', '/ws/v1/smartpbx/media')\n"
        "STATUS_AUTHENTICATED = True\n",
        encoding="utf-8",
    )
    (root / "smartpbx_gateway.py").write_text(
        "import secrets\n\n"
        "async def handle(websocket, token, candidate):\n"
        "    if not secrets.compare_digest(token, candidate):\n"
        "        await websocket.close(code=1008)\n"
        "        return\n"
        "    await websocket.accept()\n",
        encoding="utf-8",
    )
    (root / "smartpbx_protocol.py").write_text("PROTOCOL_VERSION = 'smartpbx-ai-provider-v07'\n", encoding="utf-8")
    (root / "smartpbx_transport.py").write_text("class SmartPBXMediaTransport: pass\n", encoding="utf-8")
    (root / "smartpbx_diagnostics.py").write_text(
        "def redacted_status(active_sessions=0):\n    return {'active_sessions': active_sessions}\n",
        encoding="utf-8",
    )
    (root / "docker-compose.yml").write_text(
        "services:\n  smartpbx-acme-inquiry:\n    environment:\n"
        "      SMARTPBX_AUTH_HEADER_NAME: X-Acme-Guide-SmartPBX-Token\n"
        "      SMARTPBX_STATUS_TOKEN: ${SMARTPBX_STATUS_TOKEN?required}\n",
        encoding="utf-8",
    )
    if ci:
        (root / ".github-workflow-fragment.yml").write_text(
            "jobs:\n  smartpbx-acme-inquiry:\n    runs-on: ubuntu-latest\n",
            encoding="utf-8",
        )
    provenance = {
        "template_version": "synthetic-test-v1",
        "source_revision": "a" * 40,
        "artifact_digest": _artifact_digest(root),
    }
    (root / ".smartpbx-factory-provenance.json").write_text(json.dumps(provenance), encoding="utf-8")
    return root


class FixtureBackend:
    def exercise(self, *, header: str, messages: tuple[dict[str, object], ...]) -> DisposableClientResult:
        wrong = header == "wrong"
        return DisposableClientResult(
            connected=not wrong,
            start_sent=not wrong and any(item["event"] == "start" for item in messages),
            start_accepted=not wrong,
            media_sent=not wrong and any(item["event"] == "media" for item in messages),
            media_accepted=not wrong,
            invalid_auth_rejected=wrong,
            terminal_event_observed=not wrong,
            close_code=1008 if wrong else 1000,
            active_tasks_after_close=0,
            resources_after_close=0,
        )


def test_readiness_report_contains_provenance_and_no_secret_values(tmp_path):
    report = verify_generated_backend(fixture_backend(tmp_path / "agent"), fixture_resources())
    rendered = readiness_report({"backend": report}, secret_values=("marker",))
    assert "template_version" in rendered
    assert "source_revision" in rendered
    assert "marker" not in rendered
    assert "transcript" not in rendered


def test_contract_verifier_requires_agent_specific_ci_job(tmp_path):
    with pytest.raises(VerificationError, match="blocking CI"):
        verify_generated_backend(fixture_backend(tmp_path / "agent", ci=False), fixture_resources())


def test_protocol_fixture_rejects_wrong_auth_before_start():
    result = run_disposable_client(FixtureBackend(), header="wrong")
    assert result.close_code == 1008
    assert result.start_sent is False
    assert result.invalid_auth_rejected is True


def test_verifier_requires_preaccept_constant_time_wss_authentication(tmp_path):
    agent = fixture_backend(tmp_path / "agent")
    gateway = agent / "smartpbx_gateway.py"
    gateway.write_text("async def handle(websocket):\n    await websocket.accept()\n", encoding="utf-8")
    with pytest.raises(VerificationError, match="constant-time"):
        verify_generated_backend(agent, fixture_resources())


def test_protocol_fixture_has_no_customer_or_media_content():
    fixture = json.loads((FIXTURES / "protocol_messages.json").read_text(encoding="utf-8"))
    assert [item["event"] for item in fixture] == ["connected", "start", "media", "stop", "hangup"]
    assert all(set(item) == {"event", "fields"} for item in fixture)
