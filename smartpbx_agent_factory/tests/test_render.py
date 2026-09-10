"""Renderer contracts use explicit synthetic templates, never deployed provenance."""

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from smartpbx_agent_factory.catalogue import CapabilityCatalogue
from smartpbx_agent_factory.provenance import TemplateAllowlist, TemplateFile
from smartpbx_agent_factory.render import IncompleteTemplateError, IdentityLeakError, ReviewNotApprovedError, render_backend
from smartpbx_agent_factory.resources import AllocationRegistry, derive_resources
from smartpbx_agent_factory.schema import manifest_digest, parse_manifest
from smartpbx_agent_factory.state import GenerationState, Stage


FIXTURE = Path(__file__).parent / "fixtures" / "acme-minimal.json"
CATALOGUE = Path(__file__).parent / "fixtures" / "approved-provider-catalogue.json"


@dataclass(frozen=True)
class FixtureFact:
    text: str


@dataclass(frozen=True)
class FixtureReview:
    facts: tuple[FixtureFact, ...] = (FixtureFact("Acme provides approved information."),)
    documents: dict[str, str] | None = None
    claimed_digest: str | None = None

    @property
    def digest(self):
        payload = "\n".join(fact.text for fact in self.facts)
        if self.documents:
            payload += "\n" + "\n".join(f"{name}\0{self.documents[name]}" for name in sorted(self.documents))
        return self.claimed_digest or hashlib.sha256(payload.encode("utf-8")).hexdigest()


def fixture_manifest():
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return parse_manifest(raw, approved_source_roots=(Path.cwd(),), catalogue=CapabilityCatalogue.load(CATALOGUE))


def fixture_resources():
    return derive_resources(fixture_manifest(), AllocationRegistry())


def fixture_state(review=None, *, plan_approved=True):
    review = review or FixtureReview()
    state = GenerationState.start("generation-fixture", manifest_digest(fixture_manifest()))
    state.transition(Stage.INPUT_COLLECTED)
    state.transition(Stage.SECRETS_RESOLVED)
    state.transition(Stage.KNOWLEDGE_REVIEW_REQUIRED)
    state.record_knowledge_review_digest(review.digest)
    state.approve_knowledge(review.digest)
    if plan_approved:
        state.record_plan_digest("plan-fixture")
        state.approve_plan("plan-fixture")
    return state


def fixture_templates(root: Path) -> TemplateAllowlist:
    """A deliberately synthetic, hash-pinned test seam; never production input."""
    templates = {
        "runtime/server.py.tmpl": "ROUTES = ('/smartpbx/status', '/ws/v1/smartpbx/media')\n",
        "runtime/smartpbx_gateway.py.tmpl": "# synthetic gateway marker\n",
        "runtime/smartpbx_protocol.py.tmpl": "PROTOCOL = 'smartpbx-ai-provider-v07'\n",
        "runtime/smartpbx_session.py.tmpl": "# synthetic session marker\n",
        "runtime/smartpbx_transport.py.tmpl": "# synthetic transport marker\n",
        "runtime/smartpbx_diagnostics.py.tmpl": "# synthetic diagnostics marker\n",
        "runtime/tools.py.tmpl": "TOOL_REGISTRY = {}\n",
        "runtime/website_demo.py.tmpl": "ROUTES = ('/voice/demo-incoming',)\n",
        "infrastructure/Dockerfile.tmpl": "FROM python:3.11-slim\n",
        "infrastructure/docker-compose.yml.tmpl": "services: {}\n",
        "infrastructure/nginx-smartpbx.conf.tmpl": "location /smartpbx/status {}\n",
        "infrastructure/nginx-website.conf.tmpl": "location /voice/demo-incoming {}\n",
        "infrastructure/env.example.tmpl": "SMARTPBX_WS_TOKEN\n",
        "infrastructure/README.md.tmpl": "Synthetic fixture runtime.\n",
        "infrastructure/CLIENT_CONNECT.md.tmpl": "Synthetic fixture client connect.\n",
        "infrastructure/demo-routing-activation.md.tmpl": "Synthetic fixture pending checklist.\n",
    }
    files = {}
    for relative, text in templates.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        files[f"synthetic/{relative}"] = TemplateFile(relative, "sha256:" + hashlib.sha256(text.encode()).hexdigest())
    return TemplateAllowlist(
        template_version="synthetic-test-v1",
        source_revision="a" * 40,
        image_digest="sha256:" + "b" * 64,
        oci_revision="a" * 40,
        protocol_version="smartpbx-ai-provider-v07",
        environment_schema_version="synthetic-test-v1",
        files=files,
    )


def test_partial_v06_provenance_fails_closed_without_complete_runtime_template(tmp_path):
    with pytest.raises(IncompleteTemplateError, match="INCOMPLETE_TEMPLATE"):
        review = FixtureReview()
        render_backend(fixture_manifest(), review, fixture_resources(), tmp_path, state=fixture_state(review))


def test_inquiry_only_render_has_no_business_tools(tmp_path):
    report = render_backend(
        fixture_manifest(), FixtureReview(), fixture_resources(), tmp_path,
        state=fixture_state(), template_allowlist=fixture_templates(tmp_path / "synthetic"), template_root=tmp_path / "synthetic",
    )
    tools = (tmp_path / "SmartPBX Agents/acme-inquiry/tools.py").read_text(encoding="utf-8")
    assert "create_booking" not in tools
    assert "transfer_to_human" not in tools
    assert "hangup_call" not in tools
    assert report.enabled_capabilities == ()
    assert report.synthetic is True
    assert report.deployable is False


def test_renderer_rejects_identity_and_secret_leaks_from_review(tmp_path):
    review = FixtureReview(facts=(FixtureFact("Hatton Hills is a hotel"),))
    with pytest.raises(IdentityLeakError, match="identity leak"):
        render_backend(
            fixture_manifest(), review, fixture_resources(), tmp_path,
            state=fixture_state(review), template_allowlist=fixture_templates(tmp_path / "synthetic"), template_root=tmp_path / "synthetic",
        )


def test_renderer_is_deterministic_for_the_same_approved_inputs(tmp_path):
    first_root, second_root = tmp_path / "first", tmp_path / "second"
    first_template, second_template = fixture_templates(first_root / "synthetic"), fixture_templates(second_root / "synthetic")
    first = render_backend(fixture_manifest(), FixtureReview(), fixture_resources(), first_root, state=fixture_state(), template_allowlist=first_template, template_root=first_root / "synthetic")
    second = render_backend(fixture_manifest(), FixtureReview(), fixture_resources(), second_root, state=fixture_state(), template_allowlist=second_template, template_root=second_root / "synthetic")
    assert first.artifact_digest == second.artifact_digest
    assert first.files == second.files


def test_renderer_requires_generation_state_approval_for_the_exact_review_digest(tmp_path):
    review = FixtureReview()
    with pytest.raises(ReviewNotApprovedError, match="digest"):
        render_backend(
            fixture_manifest(), review, fixture_resources(), tmp_path, state=fixture_state(FixtureReview(claimed_digest="b" * 64)),
            template_allowlist=fixture_templates(tmp_path / "synthetic"), template_root=tmp_path / "synthetic",
        )


def test_renderer_rejects_plan_review_state_before_plan_approval(tmp_path):
    review = FixtureReview()
    with pytest.raises(ReviewNotApprovedError, match="plan approval"):
        render_backend(
            fixture_manifest(), review, fixture_resources(), tmp_path, state=fixture_state(review, plan_approved=False),
            template_allowlist=fixture_templates(tmp_path / "synthetic"), template_root=tmp_path / "synthetic",
        )


def test_renderer_rejects_state_with_a_different_canonical_manifest_digest(tmp_path):
    review = FixtureReview()
    state = fixture_state(review)
    state.manifest_digest = "0" * 64
    with pytest.raises(ReviewNotApprovedError, match="manifest digest"):
        render_backend(
            fixture_manifest(), review, fixture_resources(), tmp_path, state=state,
            template_allowlist=fixture_templates(tmp_path / "synthetic"), template_root=tmp_path / "synthetic",
        )


def test_renderer_rejects_review_with_forged_digest_after_facts_change(tmp_path):
    original = FixtureReview()
    forged = FixtureReview(facts=(FixtureFact("altered approved fact"),), claimed_digest=original.digest)
    with pytest.raises(ReviewNotApprovedError, match="canonical digest"):
        render_backend(
            fixture_manifest(), forged, fixture_resources(), tmp_path, state=fixture_state(original),
            template_allowlist=fixture_templates(tmp_path / "synthetic"), template_root=tmp_path / "synthetic",
        )


def test_scan_rejects_identity_leak_in_late_review_document(tmp_path):
    review = FixtureReview(documents={"early.md": "approved", "late.md": "Hatton Hills"})
    with pytest.raises(IdentityLeakError, match="identity leak"):
        render_backend(
            fixture_manifest(), review, fixture_resources(), tmp_path, state=fixture_state(review),
            template_allowlist=fixture_templates(tmp_path / "synthetic"), template_root=tmp_path / "synthetic",
        )


def test_failed_write_removes_only_the_new_generation_root(tmp_path, monkeypatch):
    original = Path.write_text
    writes = 0

    def fail_after_first(path, content, *args, **kwargs):
        nonlocal writes
        writes += 1
        if writes > 1 and "SmartPBX Agents" in path.as_posix():
            raise OSError("synthetic write failure")
        return original(path, content, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", fail_after_first)
    with pytest.raises(OSError, match="synthetic write failure"):
        render_backend(
            fixture_manifest(), FixtureReview(), fixture_resources(), tmp_path, state=fixture_state(),
            template_allowlist=fixture_templates(tmp_path / "synthetic"), template_root=tmp_path / "synthetic",
        )
    assert not (tmp_path / "SmartPBX Agents/acme-inquiry").exists()
