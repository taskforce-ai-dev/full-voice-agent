"""Disposable lifecycle verification contracts are enforced in CI.

These tests use an in-process fixture adapter only.  They never use Docker,
network services, customer data, or credentials.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from smartpbx_agent_factory.provenance import TemplateAllowlist, TemplateFile
from smartpbx_agent_factory.resources import AllocationRegistry, DerivedResources, derive_resources
from smartpbx_agent_factory.schema import parse_manifest
from smartpbx_agent_factory.verify import (
    DisposableClientResult,
    VerificationBinding,
    VerificationError,
    VerificationReport,
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


def fixture_allowlist() -> TemplateAllowlist:
    return TemplateAllowlist(
        template_version="synthetic-test-v1",
        source_revision="a" * 40,
        image_digest="sha256:" + "b" * 64,
        oci_revision="a" * 40,
        protocol_version="smartpbx-ai-provider-v07",
        environment_schema_version="synthetic-test-v1",
        files={"runtime/server.py": TemplateFile("runtime/server.py.tmpl", "sha256:" + "c" * 64)},
    )


def fixture_backend(root: Path, *, ci: bool = True) -> tuple[Path, VerificationBinding]:
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
        "# A redacted transcript count would be safe documentation, not emitted data.\n"
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
    binding = VerificationBinding(
        template_allowlist=fixture_allowlist(),
        manifest_digest="d" * 64,
        artifact_digest=_artifact_digest(root),
    )
    (root / ".smartpbx-factory-provenance.json").write_text(
        json.dumps(binding.provenance_for(fixture_resources())), encoding="utf-8"
    )
    return root, binding


class FixtureLifecycleAdapter:
    def __init__(self) -> None:
        self.auth_cases: list[str] = []

    def exercise(
        self,
        *,
        agent_dir: Path,
        resources: DerivedResources,
        auth_case: str,
        messages: tuple[dict[str, object], ...],
    ) -> DisposableClientResult:
        self.auth_cases.append(auth_case)
        wrong = auth_case != "valid"
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
    agent, binding = fixture_backend(tmp_path / "agent")
    report = verify_generated_backend(agent, fixture_resources(), binding=binding)
    rendered = readiness_report({"backend": report}, secret_values=("marker",))
    assert "template_version" in rendered
    assert "source_revision" in rendered
    assert "marker" not in rendered
    assert "transcript" not in rendered


def test_contract_verifier_requires_agent_specific_ci_job(tmp_path):
    agent, binding = fixture_backend(tmp_path / "agent", ci=False)
    with pytest.raises(VerificationError, match="blocking CI"):
        verify_generated_backend(agent, fixture_resources(), binding=binding)


def test_protocol_fixture_rejects_wrong_auth_before_start():
    result = run_disposable_client(
        FixtureLifecycleAdapter(), agent_dir=Path("/synthetic"), resources=fixture_resources(), auth_case="wrong"
    )
    assert result.close_code == 1008
    assert result.start_sent is False
    assert result.invalid_auth_rejected is True


def test_verifier_requires_preaccept_constant_time_wss_authentication(tmp_path):
    agent, binding = fixture_backend(tmp_path / "agent")
    gateway = agent / "smartpbx_gateway.py"
    gateway.write_text("async def handle(websocket):\n    await websocket.accept()\n", encoding="utf-8")
    with pytest.raises(VerificationError, match="constant-time"):
        verify_generated_backend(agent, fixture_resources(), binding=binding)


def test_protocol_fixture_has_no_customer_or_media_content():
    fixture = json.loads((FIXTURES / "protocol_messages.json").read_text(encoding="utf-8"))
    assert [item["event"] for item in fixture] == ["connected", "start", "media", "stop", "hangup"]
    assert fixture[2]["media"]["payload"] == "<synthetic-silence>"
    assert all("fields" not in item for item in fixture)


def test_provenance_cannot_be_rewritten_to_match_a_modified_artifact(tmp_path):
    agent, binding = fixture_backend(tmp_path / "agent")
    (agent / "server.py").write_text("ROUTES = ('/health', '/smartpbx/status', '/ws/v1/smartpbx/media')\nSTATUS_AUTHENTICATED = True\n# tampered\n", encoding="utf-8")
    rewritten = binding.provenance_for(fixture_resources()) | {"artifact_digest": _artifact_digest(agent)}
    (agent / ".smartpbx-factory-provenance.json").write_text(json.dumps(rewritten), encoding="utf-8")
    with pytest.raises(VerificationError, match="approved artifact"):
        verify_generated_backend(agent, fixture_resources(), binding=binding)


def test_verifier_invokes_adapter_for_all_authentication_cases(tmp_path):
    agent, binding = fixture_backend(tmp_path / "agent")
    adapter = FixtureLifecycleAdapter()
    report = verify_generated_backend(agent, fixture_resources(), binding=binding, lifecycle_adapter=adapter)
    assert adapter.auth_cases == ["missing", "wrong", "cross-agent", "valid"]
    assert report.ready_for_pr is True
    assert report.runtime_lifecycle_verified is True


def test_verifier_rejects_arbitrary_lifecycle_result_argument(tmp_path):
    agent, binding = fixture_backend(tmp_path / "agent")
    forged = DisposableClientResult(True, True, True, True, True, False, True, 1000, 0, 0)
    with pytest.raises(TypeError):
        verify_generated_backend(agent, fixture_resources(), binding=binding, lifecycle=forged)


def test_report_metadata_rejects_control_characters_and_secret_like_values():
    with pytest.raises(VerificationError, match="safe report"):
        VerificationReport(
            agent_slug="acme\nmalicious",
            artifact_digest="a" * 64,
            template_version="synthetic-test-v1",
            source_revision="b" * 40,
            ci_identifier="smartpbx-acme",
            protocol_events=("connected", "start", "media", "stop", "hangup"),
            static_contracts_passed=True,
            runtime_lifecycle_verified=False,
            ready_for_pr=False,
            runtime_status="CI_LIFECYCLE_REQUIRED",
            evidence=("Dockerfile",),
        )
