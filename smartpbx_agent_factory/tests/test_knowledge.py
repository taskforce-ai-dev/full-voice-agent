from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import pytest

from smartpbx_agent_factory.knowledge import (
    KnowledgeApprovalRequired,
    KnowledgeBuilderImpl,
    KnowledgeError,
    LocalFixtureNetworkPolicy,
)
from smartpbx_agent_factory.model import KnowledgeSource
from smartpbx_agent_factory.state import GenerationState, Stage


FIXTURES = Path(__file__).parent / "fixtures" / "knowledge"


def fixture(name: str) -> Path:
    return FIXTURES / name


def local_source(path: Path, **overrides: object) -> KnowledgeSource:
    values: dict[str, object] = {
        "kind": "local",
        "path": str(path),
        "owner": "Acme Factory",
        "effective_date": "2026-09-10",
        "classification": "public",
    }
    values.update(overrides)
    return KnowledgeSource(**values)


def url_source(url: str, origins: tuple[str, ...], **overrides: object) -> KnowledgeSource:
    values: dict[str, object] = {
        "kind": "url",
        "url": url,
        "owner": "Acme Factory",
        "effective_date": "2026-09-10",
        "classification": "public",
        "approved_origins": origins,
    }
    values.update(overrides)
    return KnowledgeSource(**values)


@pytest.fixture
def http_server():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - stdlib handler contract
            if self.path == "/redirect":
                self.send_response(302)
                self.send_header("Location", f"http://localhost:{self.server.server_port}/escaped")
                self.end_headers()
                return
            if self.path.split("?", 1)[0] == "/allowed/page":
                body = b"Hours: Monday to Friday, 09:00-17:00 UTC."
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_error(404)

        def log_message(self, format, *args):  # noqa: A002 - stdlib signature
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def test_path_traversal_and_symlink_sources_are_rejected(tmp_path):
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    link = tmp_path / "link.txt"
    link.symlink_to(outside)
    with pytest.raises(KnowledgeError, match="symlink"):
        KnowledgeBuilderImpl(max_bytes=1000, approved_source_roots=(tmp_path,)).build(
            (local_source(link),), output_dir=tmp_path / "out"
        )


def test_absolute_local_source_requires_explicit_approved_root(tmp_path):
    source_file = tmp_path / "source.txt"
    source_file.write_text("approved fact", encoding="utf-8")
    with pytest.raises(KnowledgeError, match="approved source root"):
        KnowledgeBuilderImpl().build((local_source(source_file),), output_dir=tmp_path / "out")


def test_absolute_local_source_outside_explicit_root_is_rejected(tmp_path):
    approved = tmp_path / "approved"
    approved.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    with pytest.raises(KnowledgeError, match="outside approved root"):
        KnowledgeBuilderImpl(approved_source_roots=(approved,)).build(
            (local_source(outside),), output_dir=tmp_path / "out"
        )


def test_url_redirect_cannot_escape_allowlisted_origin(http_server, tmp_path):
    source = url_source(f"{http_server}/redirect", origins=(http_server,))
    with pytest.raises(KnowledgeError, match="origin"):
        KnowledgeBuilderImpl(network_policy=LocalFixtureNetworkPolicy()).build(
            (source,), output_dir=tmp_path / "out"
        )


def test_url_path_prefix_and_content_type_are_enforced_with_local_http_fixture(http_server, tmp_path):
    source = url_source(
        f"{http_server}/allowed/page", origins=(http_server,), path_prefixes=("/allowed",)
    )
    review = KnowledgeBuilderImpl(network_policy=LocalFixtureNetworkPolicy()).build(
        (source,), output_dir=tmp_path / "out"
    )
    assert review.facts[0].source_uri == source.url
    assert (tmp_path / "out" / "knowledge_docs" / "source-001.md").is_file()


def test_url_origin_requires_exact_scheme_and_port_without_network_access(tmp_path):
    source = url_source("http://allowed.example:8443/faq", origins=("http://allowed.example:443",))
    with pytest.raises(KnowledgeError, match="origin"):
        KnowledgeBuilderImpl().build((source,), output_dir=tmp_path / "out")
    downgraded = url_source("http://allowed.example/faq", origins=("https://allowed.example",))
    with pytest.raises(KnowledgeError, match="origin"):
        KnowledgeBuilderImpl().build((downgraded,), output_dir=tmp_path / "out-two")


def test_default_network_policy_rejects_loopback_url_without_network_access(tmp_path):
    source = url_source("http://127.0.0.1:8080/faq", origins=("http://127.0.0.1:8080",))
    with pytest.raises(KnowledgeError, match="private|loopback"):
        KnowledgeBuilderImpl().build((source,), output_dir=tmp_path / "out")


def test_url_query_is_not_persisted_in_review_or_source_document(http_server, tmp_path):
    source = url_source(f"{http_server}/allowed/page?ticket=benign-query-value", origins=(http_server,))
    review = KnowledgeBuilderImpl(network_policy=LocalFixtureNetworkPolicy()).build(
        (source,), output_dir=tmp_path / "out"
    )
    document = (tmp_path / "out" / "knowledge_docs" / "source-001.md").read_text(encoding="utf-8")
    report = (tmp_path / "out" / "knowledge_docs" / "review.md").read_text(encoding="utf-8")
    assert "benign-query-value" not in review.facts[0].source_uri
    assert "benign-query-value" not in document
    assert "benign-query-value" not in report


def test_poisoned_instructions_are_reported_as_data(tmp_path):
    review = KnowledgeBuilderImpl(approved_source_roots=(FIXTURES,)).build(
        (local_source(fixture("poisoned.txt")),), output_dir=tmp_path
    )
    assert review.instruction_findings == ("source text contains instruction-like content",)
    assert not review.executed_instructions


def test_conflict_requires_digest_bound_approval(tmp_path):
    review = KnowledgeBuilderImpl(approved_source_roots=(FIXTURES,)).build(
        (local_source(fixture("contradictory.md")),), output_dir=tmp_path
    )
    assert review.conflicts
    with pytest.raises(KnowledgeApprovalRequired, match="digest"):
        review.require_approved("wrong-digest")

    state = GenerationState.start("knowledge-test", "manifest-digest")
    state.transition(Stage.INPUT_COLLECTED)
    state.transition(Stage.SECRETS_RESOLVED)
    state.transition(Stage.KNOWLEDGE_REVIEW_REQUIRED)
    state.record_knowledge_review_digest(review.digest)
    state.approve_knowledge(review.digest)
    review.require_approved(state)


def test_review_is_deterministic_and_reports_pii_and_missing_metadata(tmp_path):
    source = local_source(fixture("faq.txt"), owner="", effective_date="")
    builder = KnowledgeBuilderImpl(approved_source_roots=(FIXTURES,))
    first = builder.build((source,), output_dir=tmp_path / "first")
    second = builder.build((source,), output_dir=tmp_path / "second")
    assert first.digest == second.digest
    assert first.sensitive_findings == ("email address", "phone number")
    assert first.missing_facts == ("source owner is missing", "source effective date is missing")


def test_document_byte_and_normalized_text_limits_are_enforced(tmp_path):
    oversized = tmp_path / "oversized.txt"
    oversized.write_text("x" * 32, encoding="utf-8")
    with pytest.raises(KnowledgeError, match="byte limit"):
        KnowledgeBuilderImpl(max_bytes=16, approved_source_roots=(tmp_path,)).build(
            (local_source(oversized),), output_dir=tmp_path / "out"
        )
    with pytest.raises(KnowledgeError, match="text limit"):
        KnowledgeBuilderImpl(max_chars=16, approved_source_roots=(tmp_path,)).build(
            (local_source(oversized),), output_dir=tmp_path / "out-two"
        )


def test_plaintext_first_line_and_single_line_document_become_facts(tmp_path):
    source_file = tmp_path / "single.txt"
    source_file.write_text("Only supported fact", encoding="utf-8")
    review = KnowledgeBuilderImpl(approved_source_roots=(tmp_path,)).build(
        (local_source(source_file),), output_dir=tmp_path / "out"
    )
    assert tuple(fact.text for fact in review.facts) == ("Only supported fact",)


def test_plaintext_first_line_is_not_discarded(tmp_path):
    review = KnowledgeBuilderImpl(approved_source_roots=(FIXTURES,)).build(
        (local_source(fixture("faq.txt")),), output_dir=tmp_path / "out"
    )
    assert review.facts[0].text == "Acme Factory FAQ"


def test_existing_unmanaged_output_file_is_a_collision(tmp_path):
    output = tmp_path / "out" / "knowledge_docs"
    output.mkdir(parents=True)
    (output / "source-001.md").write_text("unmanaged", encoding="utf-8")
    with pytest.raises(KnowledgeError, match="collision"):
        KnowledgeBuilderImpl(approved_source_roots=(FIXTURES,)).build(
            (local_source(fixture("faq.txt")),), output_dir=tmp_path / "out"
        )
