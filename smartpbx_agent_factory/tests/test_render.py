"""Renderer contracts use explicit synthetic templates, never deployed provenance."""

import json
from dataclasses import replace
from pathlib import Path

import pytest

from smartpbx_agent_factory.catalogue import CapabilityCatalogue
from smartpbx_agent_factory.knowledge import (
    KnowledgeConflict,
    KnowledgeDocument,
    KnowledgeFact,
    KnowledgeReview,
    recompute_knowledge_review_digest,
)
from smartpbx_agent_factory.provenance import TemplateAllowlist, TemplateFile
from smartpbx_agent_factory.render import (
    IncompleteTemplateError,
    IdentityLeakError,
    RenderError,
    ReviewNotApprovedError,
    _product_profile_payload,
    render_backend as _render_backend,
)
from smartpbx_agent_factory.resources import AllocationRegistry, derive_resources
from smartpbx_agent_factory.schema import manifest_digest, parse_manifest
from smartpbx_agent_factory.state import GenerationState, Stage
from _owned_worktree_fixture import fixture_owned_worktree


FIXTURE = Path(__file__).parent / "fixtures" / "acme-minimal.json"
CATALOGUE = Path(__file__).parents[1] / "template_v1" / "provider_catalogue.json"


def _render_backend_fixture(manifest, review, resources, output_dir: Path, **kwargs):
    """Private compatibility seam: synthetic handles only, never a checkout mutation."""
    manager, worktree = fixture_owned_worktree(output_dir)
    return _render_backend(
        manifest, review, resources, worktree, worktree_manager=manager, **kwargs
    )


render_backend = _render_backend_fixture


def fixture_review(
    *,
    facts: tuple[KnowledgeFact, ...] = (
        KnowledgeFact("Acme provides approved information.", "https://acme.example/facts", "source-001#document"),
    ),
    documents: tuple[KnowledgeDocument, ...] = (
        KnowledgeDocument(
            uri="https://acme.example/facts",
            owner="Acme Factory",
            effective_date="2026-09-10",
            classification="public",
            text="Acme provides approved information.",
        ),
    ),
    conflicts: tuple[KnowledgeConflict, ...] = (),
) -> KnowledgeReview:
    draft = KnowledgeReview(
        facts=facts,
        conflicts=conflicts,
        missing_facts=(),
        sensitive_findings=(),
        inaccessible_sources=(),
        duplicate_facts=(),
        instruction_findings=(),
        digest="",
        documents=documents,
    )
    return replace(draft, digest=recompute_knowledge_review_digest(draft))


def fixture_manifest():
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return parse_manifest(raw, approved_source_roots=(Path.cwd(),), catalogue=CapabilityCatalogue.load(CATALOGUE))


def fixture_resources():
    return derive_resources(fixture_manifest(), AllocationRegistry())


def fixture_state(review=None, *, plan_approved=True):
    review = review or fixture_review()
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
        "runtime/startup.py.tmpl": "app = object()\n",
        "runtime/product_profile.py.tmpl": "def load_product_profile(path): return object()\n",
        "runtime/provider_adapters.py.tmpl": "class ConversationProviderAdapter: pass\n",
        "runtime/turn_engine.py.tmpl": "class ConversationTurnEngine: pass\n",
        "infrastructure/Dockerfile.tmpl": "FROM python:3.11-slim\n",
        "infrastructure/docker-compose.yml.tmpl": "services: {}\n",
        "infrastructure/nginx-smartpbx.conf.tmpl": "location /smartpbx/status {}\n",
        "infrastructure/nginx-website.conf.tmpl": "location /voice/demo-incoming {}\n",
        "infrastructure/env.example.tmpl": "SMARTPBX_WS_TOKEN\n",
        "infrastructure/README.md.tmpl": "Synthetic fixture runtime.\n",
        "infrastructure/CLIENT_CONNECT.md.tmpl": "Synthetic fixture client connect.\n",
        "infrastructure/demo-routing-activation.md.tmpl": "Synthetic fixture pending checklist.\n",
        "infrastructure/requirements-prod.txt.tmpl": "# Synthetic fixture requirements.\n",
        "infrastructure/requirements-prod.lock.txt.tmpl": "# Synthetic fixture lock.\n",
        "infrastructure/ci-runtime-review.yml.tmpl": "jobs: {}\n",
        "infrastructure/scripts/deploy_runtime_image.sh.tmpl": "#!/bin/sh\nexit 1\n",
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
        review = fixture_review()
        render_backend(fixture_manifest(), review, fixture_resources(), tmp_path, state=fixture_state(review))


def test_public_backend_renderer_rejects_an_unowned_handle_before_writing(tmp_path):
    review = fixture_review()
    manager, worktree = fixture_owned_worktree(tmp_path)
    manager._handles.clear()
    with pytest.raises(RenderError, match="manager-owned"):
        _render_backend(
            fixture_manifest(), review, fixture_resources(), worktree,
            worktree_manager=manager, state=fixture_state(review),
            template_allowlist=fixture_templates(tmp_path / "synthetic"),
            template_root=tmp_path / "synthetic",
        )
    assert not (tmp_path / "SmartPBX Agents").exists()


def test_inquiry_only_render_has_no_business_tools(tmp_path):
    review = fixture_review()
    report = render_backend(
        fixture_manifest(), review, fixture_resources(), tmp_path,
        state=fixture_state(review), template_allowlist=fixture_templates(tmp_path / "synthetic"), template_root=tmp_path / "synthetic",
    )
    tools = (tmp_path / "SmartPBX Agents/acme-inquiry/tools.py").read_text(encoding="utf-8")
    assert "create_booking" not in tools
    assert "transfer_to_human" not in tools
    assert "hangup_call" not in tools
    assert report.enabled_capabilities == ()
    assert report.synthetic is True
    assert report.deployable is False


def test_generated_product_profile_binds_reviewed_manifest_identity_languages_topics_greetings_and_knowledge():
    manifest = fixture_manifest()
    profile = _product_profile_payload(
        manifest,
        {"approved-facts.md": "Acme provides approved information."},
    )

    assert profile["identity"] == {
        "display_name": "Acme Inquiry",
        "public_name": "Acme Inquiry",
        "agent_name": "Acme Guide",
        "industry": "general information",
        "purpose": "Answer approved company questions",
        "audience": "prospective customers",
    }
    assert profile["languages"]["en"]["greeting"] == "Welcome to Acme Inquiry."
    assert profile["languages"]["en"]["stt_model"] is None
    assert profile["allowed_topics"] == ["company information"]
    assert profile["refused_topics"] == ["account changes"]
    assert profile["knowledge_paths"] == ["knowledge_docs/approved-facts.md"]


def test_generated_product_profile_carries_the_explicit_sinhala_gemini_to_claude_fallback_contract():
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    raw["languages"] = [{
        "code": "si",
        "locale": "si-LK",
        "stt": {"provider": "azure"},
        "llm": {"provider": "gemini", "model": "gemini-3.7-flash"},
        "tts": {"provider": "gemini", "model": "gemini-3.1-flash-tts-preview"},
        "fallback": {"provider": "claude", "model": "claude-sonnet-4-5-20250929"},
        "greeting": "Acme welcomes Sinhala callers."
    }]
    manifest = parse_manifest(
        raw,
        approved_source_roots=(Path.cwd(),),
        catalogue=CapabilityCatalogue.load(CATALOGUE),
    )
    profile = _product_profile_payload(manifest, {"approved-facts.md": "Approved."})
    assert profile["languages"]["si"]["fallback"] == "claude"
    assert profile["languages"]["si"]["fallback_model"] == "claude-sonnet-4-5-20250929"


def test_runtime_template_loads_the_generated_product_profile_only_at_startup():
    template = (Path(__file__).parents[1] / "template_v1/runtime/server.py.tmpl").read_text(encoding="utf-8")
    assert template.count("product_profile=load_product_profile(") == 1


def test_renderer_rejects_identity_and_secret_leaks_from_review(tmp_path):
    review = fixture_review(facts=(KnowledgeFact("Hatton Hills is a hotel", "https://acme.example/facts", "source-001#document"),))
    with pytest.raises(IdentityLeakError, match="identity leak"):
        render_backend(
            fixture_manifest(), review, fixture_resources(), tmp_path,
            state=fixture_state(review), template_allowlist=fixture_templates(tmp_path / "synthetic"), template_root=tmp_path / "synthetic",
        )


def test_renderer_is_deterministic_for_the_same_approved_inputs(tmp_path):
    first_root, second_root = tmp_path / "first", tmp_path / "second"
    first_template, second_template = fixture_templates(first_root / "synthetic"), fixture_templates(second_root / "synthetic")
    first_review, second_review = fixture_review(), fixture_review()
    first = render_backend(fixture_manifest(), first_review, fixture_resources(), first_root, state=fixture_state(first_review), template_allowlist=first_template, template_root=first_root / "synthetic")
    second = render_backend(fixture_manifest(), second_review, fixture_resources(), second_root, state=fixture_state(second_review), template_allowlist=second_template, template_root=second_root / "synthetic")
    assert first.artifact_digest == second.artifact_digest
    assert first.files == second.files


def test_renderer_requires_generation_state_approval_for_the_exact_review_digest(tmp_path):
    review = fixture_review()
    with pytest.raises(ReviewNotApprovedError, match="digest"):
        render_backend(
            fixture_manifest(), review, fixture_resources(), tmp_path, state=fixture_state(replace(review, digest="b" * 64)),
            template_allowlist=fixture_templates(tmp_path / "synthetic"), template_root=tmp_path / "synthetic",
        )


def test_renderer_rejects_plan_review_state_before_plan_approval(tmp_path):
    review = fixture_review()
    with pytest.raises(ReviewNotApprovedError, match="plan approval"):
        render_backend(
            fixture_manifest(), review, fixture_resources(), tmp_path, state=fixture_state(review, plan_approved=False),
            template_allowlist=fixture_templates(tmp_path / "synthetic"), template_root=tmp_path / "synthetic",
        )


def test_renderer_rejects_state_with_a_different_canonical_manifest_digest(tmp_path):
    review = fixture_review()
    state = fixture_state(review)
    state.manifest_digest = "0" * 64
    with pytest.raises(ReviewNotApprovedError, match="manifest digest"):
        render_backend(
            fixture_manifest(), review, fixture_resources(), tmp_path, state=state,
            template_allowlist=fixture_templates(tmp_path / "synthetic"), template_root=tmp_path / "synthetic",
        )


def test_renderer_rejects_review_with_forged_digest_after_facts_change(tmp_path):
    original = fixture_review()
    forged = replace(
        original,
        facts=(KnowledgeFact("altered approved fact", "https://acme.example/facts", "source-001#document"),),
    )
    with pytest.raises(ReviewNotApprovedError, match="canonical digest"):
        render_backend(
            fixture_manifest(), forged, fixture_resources(), tmp_path, state=fixture_state(original),
            template_allowlist=fixture_templates(tmp_path / "synthetic"), template_root=tmp_path / "synthetic",
        )
    assert not (tmp_path / "SmartPBX Agents").exists()


def test_renderer_rejects_review_with_forged_digest_after_document_change(tmp_path):
    original = fixture_review()
    forged = replace(
        original,
        documents=(replace(original.documents[0], text="altered approved document"),),
    )
    with pytest.raises(ReviewNotApprovedError, match="canonical digest"):
        render_backend(
            fixture_manifest(), forged, fixture_resources(), tmp_path, state=fixture_state(original),
            template_allowlist=fixture_templates(tmp_path / "synthetic"), template_root=tmp_path / "synthetic",
        )
    assert not (tmp_path / "SmartPBX Agents").exists()


def test_renderer_rejects_review_with_forged_digest_after_conflict_change(tmp_path):
    original = fixture_review()
    forged = replace(
        original,
        conflicts=(KnowledgeConflict("hours", ("09:00", "17:00"), ("source-001#document", "source-001#document")),),
    )
    with pytest.raises(ReviewNotApprovedError, match="canonical digest"):
        render_backend(
            fixture_manifest(), forged, fixture_resources(), tmp_path, state=fixture_state(original),
            template_allowlist=fixture_templates(tmp_path / "synthetic"), template_root=tmp_path / "synthetic",
        )
    assert not (tmp_path / "SmartPBX Agents").exists()


def test_renderer_rejects_review_with_executed_instructions_even_when_digest_is_unchanged(tmp_path):
    original = fixture_review()
    forged = replace(original, executed_instructions=True)
    with pytest.raises(ReviewNotApprovedError, match="executed instructions"):
        render_backend(
            fixture_manifest(), forged, fixture_resources(), tmp_path, state=fixture_state(original),
            template_allowlist=fixture_templates(tmp_path / "synthetic"), template_root=tmp_path / "synthetic",
        )
    assert not (tmp_path / "SmartPBX Agents").exists()


def test_scan_rejects_identity_leak_in_late_review_document(tmp_path):
    review = fixture_review(documents=(replace(fixture_review().documents[0], text="Hatton Hills"),))
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
            fixture_manifest(), fixture_review(), fixture_resources(), tmp_path, state=fixture_state(),
            template_allowlist=fixture_templates(tmp_path / "synthetic"), template_root=tmp_path / "synthetic",
        )
    assert not (tmp_path / "SmartPBX Agents/acme-inquiry").exists()
