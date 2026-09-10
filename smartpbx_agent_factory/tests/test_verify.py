"""Static verification contracts; Docker lifecycle proof belongs to CI only."""

from __future__ import annotations

import hashlib
import inspect
import json
from pathlib import Path

import pytest

from smartpbx_agent_factory.provenance import TemplateAllowlist, TemplateFile
from smartpbx_agent_factory.resources import AllocationRegistry, derive_resources
from smartpbx_agent_factory.schema import parse_manifest
from smartpbx_agent_factory.verify import VerificationBinding, VerificationError, VerificationReport, readiness_report, verify_generated_backend


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


def test_readiness_report_contains_provenance_and_no_secret_values(tmp_path):
    agent, binding = fixture_backend(tmp_path / "agent")
    report = verify_generated_backend(agent, fixture_resources(), binding=binding)
    rendered = readiness_report({"backend": report}, secret_values=("marker",))
    assert "template_version" in rendered
    assert "source_revision" in rendered
    assert "marker" not in rendered
    assert "transcript" not in rendered


def test_readiness_report_allows_canonical_lf_separators(tmp_path):
    agent, binding = fixture_backend(tmp_path / "agent")
    rendered = readiness_report({"backend": verify_generated_backend(agent, fixture_resources(), binding=binding)})
    assert rendered.endswith("\n")
    assert "\r" not in rendered
    assert all(ord(character) >= 32 or character == "\n" for character in rendered)


def test_contract_verifier_requires_agent_specific_ci_job(tmp_path):
    agent, binding = fixture_backend(tmp_path / "agent", ci=False)
    with pytest.raises(VerificationError, match="blocking CI"):
        verify_generated_backend(agent, fixture_resources(), binding=binding)


def test_verifier_has_no_caller_injectable_lifecycle_success_seam():
    assert "lifecycle_adapter" not in inspect.signature(verify_generated_backend).parameters


def test_verifier_requires_preaccept_constant_time_wss_authentication(tmp_path):
    agent, binding = fixture_backend(tmp_path / "agent")
    gateway = agent / "smartpbx_gateway.py"
    gateway.write_text("async def handle(websocket):\n    await websocket.accept()\n", encoding="utf-8")
    with pytest.raises(VerificationError, match="constant-time"):
        verify_generated_backend(agent, fixture_resources(), binding=binding)


def test_protocol_fixture_has_no_customer_or_media_content():
    fixture = json.loads((FIXTURES / "protocol_messages.json").read_text(encoding="utf-8"))
    assert set(fixture) == {"stop", "hangup"}
    assert [item["event"] for item in fixture["stop"]] == ["connected", "start", "media", "stop"]
    assert [item["event"] for item in fixture["hangup"]] == ["connected", "start", "media", "hangup"]
    assert fixture["stop"][1]["start"]["mediaFormat"] == {"encoding": "g711_ulaw", "sampleRate": 8000}
    assert fixture["hangup"][3]["hangup"]["callId"] == fixture["hangup"][1]["start"]["callId"]
    assert fixture["hangup"][3]["hangup"]["otherLegCallId"] == fixture["hangup"][1]["start"]["otherLegCallId"]
    assert fixture["stop"][2]["media"]["payload"].endswith("/w==")
    assert all("fields" not in item for scenario in fixture.values() for item in scenario)


def test_ci_lifecycle_runner_requires_the_runtime_integration_contract():
    runner = (Path(__file__).parents[2] / "scripts" / "run_smartpbx_ci_lifecycle.py").read_text(encoding="utf-8")
    for required in (
        '"8000/tcp"', '"127.0.0.1::8000"', "SMARTPBX_RUNTIME_MODE=synthetic",
        "SMARTPBX_ALLOW_SYNTHETIC_FOR_CI=1", "SMARTPBX_PRODUCT_PROFILE_PATH=/app/config/product_profile.json",
        "SMARTPBX_KNOWLEDGE_DIR=/app/knowledge_docs", "SMARTPBX_PROVIDER_PROFILE_PATH=/app/config/provider_profile.json",
        "active_sessions", "active_tasks", "active_resources", "admitted_total", "released_total",
        "canonical_ci_fixture", "template_allowlist_digest", "candidate_provenance_digest", "--canonical-fixture",
        "validate_allowlist_metadata", "_normal_runtime_binding", "_canonical_fixture_binding",
        "rejected_status", "--attestation", "observed_cases",
    ):
        assert required in runner
    assert "ANTHROPIC_API_KEY" not in runner
    assert "GEMINI_API_KEY" not in runner
    materializer = (Path(__file__).parents[2] / "scripts" / "materialize_smartpbx_ci_fixture.py").read_text(encoding="utf-8")
    assert "runtime_template_digests" in materializer
    assert "differs from the exact repository template" in materializer


def test_ci_workflow_cannot_pass_without_the_canonical_review_only_fixture():
    workflow = (Path(__file__).parents[2] / ".github" / "workflows" / "smartpbx-generated-agents.yml").read_text(encoding="utf-8")
    assert 'canonical_fixture="SmartPBX Agents/.ci-lifecycle-canonical"' in workflow
    assert "canonical review-only CI fixture is required" in workflow
    assert "--canonical-fixture" in workflow
    assert "--attestation" in workflow
    assert "actions/upload-artifact@v4" in workflow


def test_verifier_exposes_a_redacted_attestation_reader_without_caller_booleans():
    source = inspect.getsource(__import__("smartpbx_agent_factory.verify", fromlist=["load_lifecycle_attestation"]))
    assert "def load_lifecycle_attestation" in source
    assert "observed_cases" in source
    assert "lifecycle_succeeded" not in source


def test_provenance_cannot_be_rewritten_to_match_a_modified_artifact(tmp_path):
    agent, binding = fixture_backend(tmp_path / "agent")
    (agent / "server.py").write_text("ROUTES = ('/health', '/smartpbx/status', '/ws/v1/smartpbx/media')\nSTATUS_AUTHENTICATED = True\n# tampered\n", encoding="utf-8")
    rewritten = binding.provenance_for(fixture_resources()) | {"artifact_digest": _artifact_digest(agent)}
    (agent / ".smartpbx-factory-provenance.json").write_text(json.dumps(rewritten), encoding="utf-8")
    with pytest.raises(VerificationError, match="approved artifact"):
        verify_generated_backend(agent, fixture_resources(), binding=binding)


def test_static_verifier_never_self_attests_runtime_lifecycle(tmp_path):
    agent, binding = fixture_backend(tmp_path / "agent")
    report = verify_generated_backend(agent, fixture_resources(), binding=binding)
    assert report.ready_for_pr is False
    assert report.runtime_lifecycle_verified is False


def test_verifier_rejects_arbitrary_lifecycle_result_argument(tmp_path):
    agent, binding = fixture_backend(tmp_path / "agent")
    with pytest.raises(TypeError):
        verify_generated_backend(agent, fixture_resources(), binding=binding, lifecycle=object())


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
    with pytest.raises(VerificationError, match="safe report"):
        VerificationReport(
            agent_slug="acme",
            artifact_digest="a" * 64,
            template_version="api_key=marker",
            source_revision="b" * 40,
            ci_identifier="smartpbx-acme",
            protocol_events=("connected", "start", "media", "stop", "hangup"),
            static_contracts_passed=True,
            runtime_lifecycle_verified=False,
            ready_for_pr=False,
            runtime_status="CI_LIFECYCLE_REQUIRED",
            evidence=("Dockerfile",),
        )
