from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from smartpbx_agent_factory.knowledge import (
    KnowledgeApprovalRequired,
    KnowledgeBuilderImpl,
    KnowledgeError,
    URLFetchResponse,
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


@dataclass
class FakeResolver:
    answers: dict[str, tuple[str, ...]]
    calls: list[tuple[str, int]] = field(default_factory=list)

    def resolve(self, hostname: str, port: int) -> tuple[str, ...]:
        self.calls.append((hostname, port))
        return self.answers[hostname]


@dataclass
class FakeTransport:
    responses: dict[str, URLFetchResponse]
    calls: list[tuple[str, tuple[str, ...]]] = field(default_factory=list)

    def fetch(
        self, url: str, addresses: tuple[str, ...], timeout_seconds: float, max_bytes: int
    ) -> URLFetchResponse:
        self.calls.append((url, addresses))
        return self.responses[url]


def response(status: int, *, headers: dict[str, str], body: bytes = b"") -> URLFetchResponse:
    return URLFetchResponse(status=status, headers=headers, body=body)


def fake_url_builder(
    responses: dict[str, URLFetchResponse],
    addresses: dict[str, tuple[str, ...]] | None = None,
) -> tuple[KnowledgeBuilderImpl, FakeResolver, FakeTransport]:
    resolver = FakeResolver(addresses or {"allowed.example": ("8.8.8.8",), "other.example": ("1.1.1.1",)})
    transport = FakeTransport(responses)
    return KnowledgeBuilderImpl(resolver=resolver, transport=transport), resolver, transport


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


def test_url_redirect_cannot_escape_allowlisted_origin(tmp_path):
    source = url_source("https://allowed.example/redirect", origins=("https://allowed.example",))
    builder, _, _ = fake_url_builder(
        {source.url: response(302, headers={"Location": "https://other.example/escaped"})}
    )
    with pytest.raises(KnowledgeError, match="origin"):
        builder.build((source,), output_dir=tmp_path / "out")


def test_url_path_prefix_and_content_type_are_enforced_with_fake_transport(tmp_path):
    source = url_source(
        "https://allowed.example/allowed/page",
        origins=("https://allowed.example",),
        path_prefixes=("/allowed",),
    )
    builder, _, _ = fake_url_builder(
        {
            source.url: response(
                200,
                headers={"Content-Type": "text/plain; charset=utf-8", "Content-Length": "42"},
                body=b"Hours: Monday to Friday, 09:00-17:00 UTC.",
            )
        }
    )
    review = builder.build((source,), output_dir=tmp_path / "out")
    assert review.facts[0].source_uri == source.url
    assert (tmp_path / "out" / "knowledge_docs" / "source-001.md").is_file()


@pytest.mark.parametrize("escaped_path", ("/public/%252e%252e/private", "/public%252fprivate"))
def test_ambiguous_double_encoded_path_is_rejected_before_transport(escaped_path, tmp_path):
    source = url_source(
        f"https://allowed.example{escaped_path}",
        origins=("https://allowed.example",),
        path_prefixes=("/public/",),
    )
    builder, _, transport = fake_url_builder({}, {"allowed.example": ("8.8.8.8",)})
    with pytest.raises(KnowledgeError, match="encoded|ambiguous"):
        builder.build((source,), output_dir=tmp_path / "out")
    assert transport.calls == []


def test_ambiguous_double_encoded_redirect_is_rejected_before_second_transport(tmp_path):
    source = url_source(
        "https://allowed.example/public/start",
        origins=("https://allowed.example",),
        path_prefixes=("/public/",),
    )
    builder, _, transport = fake_url_builder(
        {source.url: response(302, headers={"Location": "/public/%252e%252e/private"})}
    )
    with pytest.raises(KnowledgeError, match="encoded|ambiguous"):
        builder.build((source,), output_dir=tmp_path / "out")
    assert transport.calls == [(source.url, ("8.8.8.8",))]


@pytest.mark.parametrize(
    "malformed",
    (
        URLFetchResponse(status="200", headers={}, body=b""),
        URLFetchResponse(status=200, headers=[], body=b""),
        URLFetchResponse(status=200, headers={}, body="not-bytes"),
        object(),
    ),
)
def test_malformed_transport_response_is_rejected_as_knowledge_error(malformed, tmp_path):
    source = url_source("https://allowed.example/faq", origins=("https://allowed.example",))

    class MalformedTransport:
        def fetch(self, url, addresses, timeout_seconds, max_bytes):
            return malformed

    builder = KnowledgeBuilderImpl(
        resolver=FakeResolver({"allowed.example": ("8.8.8.8",)}), transport=MalformedTransport()
    )
    with pytest.raises(KnowledgeError, match="response"):
        builder.build((source,), output_dir=tmp_path / "out")


def test_oversized_redirect_location_is_rejected_as_knowledge_error(tmp_path):
    source = url_source("https://allowed.example/start", origins=("https://allowed.example",))
    builder, _, _ = fake_url_builder(
        {source.url: response(302, headers={"Location": "/" + ("a" * 8192)})}
    )
    with pytest.raises(KnowledgeError, match="header|location"):
        builder.build((source,), output_dir=tmp_path / "out")


def test_url_origin_requires_exact_scheme_and_port_without_network_access(tmp_path):
    source = url_source("http://allowed.example:8443/faq", origins=("http://allowed.example:443",))
    with pytest.raises(KnowledgeError, match="origin"):
        KnowledgeBuilderImpl().build((source,), output_dir=tmp_path / "out")
    downgraded = url_source("http://allowed.example/faq", origins=("https://allowed.example",))
    with pytest.raises(KnowledgeError, match="origin"):
        KnowledgeBuilderImpl().build((downgraded,), output_dir=tmp_path / "out-two")


@pytest.mark.parametrize("host", ("localhost", "127.0.0.1", "10.0.0.1", "169.254.1.1"))
def test_default_network_policy_rejects_non_public_ip_literals_without_network_access(host, tmp_path):
    source = url_source(f"http://{host}:8080/faq", origins=(f"http://{host}:8080",))
    with pytest.raises(KnowledgeError, match="private|loopback"):
        KnowledgeBuilderImpl().build((source,), output_dir=tmp_path / "out")


def test_resolver_rejects_alternate_loopback_literal_before_transport(tmp_path):
    source = url_source("https://127.1/faq", origins=("https://127.1",))
    builder, _, transport = fake_url_builder({}, {"127.1": ("127.0.0.1",)})
    with pytest.raises(KnowledgeError, match="non-global"):
        builder.build((source,), output_dir=tmp_path / "out")
    assert transport.calls == []


def test_resolver_rejects_any_private_record_before_transport(tmp_path):
    source = url_source("https://allowed.example/faq", origins=("https://allowed.example",))
    builder, _, transport = fake_url_builder(
        {}, {"allowed.example": ("8.8.8.8", "169.254.169.254")}
    )
    with pytest.raises(KnowledgeError, match="non-global"):
        builder.build((source,), output_dir=tmp_path / "out")
    assert transport.calls == []


def test_transport_receives_only_validated_pinned_addresses(tmp_path):
    source = url_source("https://allowed.example/faq", origins=("https://allowed.example",))
    builder, resolver, transport = fake_url_builder(
        {
            source.url: response(
                200,
                headers={"Content-Type": "text/plain", "Content-Length": "13"},
                body=b"Approved fact",
            )
        },
        {"allowed.example": ("8.8.8.8", "2001:4860:4860::8888")},
    )
    builder.build((source,), output_dir=tmp_path / "out")
    assert resolver.calls == [("allowed.example", 443)]
    assert transport.calls == [(source.url, ("8.8.8.8", "2001:4860:4860::8888"))]


def test_redirect_re_resolves_and_blocks_rebinding_before_second_transport(tmp_path):
    source = url_source("https://allowed.example/start", origins=("https://allowed.example",))

    @dataclass
    class RebindingResolver:
        calls: int = 0

        def resolve(self, hostname: str, port: int) -> tuple[str, ...]:
            self.calls += 1
            return ("8.8.8.8",) if self.calls == 1 else ("169.254.169.254",)

    resolver = RebindingResolver()
    transport = FakeTransport(
        {source.url: response(302, headers={"Location": "https://allowed.example/next"})}
    )
    builder = KnowledgeBuilderImpl(resolver=resolver, transport=transport)
    with pytest.raises(KnowledgeError, match="non-global"):
        builder.build((source,), output_dir=tmp_path / "out")
    assert resolver.calls == 2
    assert transport.calls == [(source.url, ("8.8.8.8",))]


def test_url_query_is_not_persisted_in_review_or_source_document(tmp_path):
    source = url_source(
        "https://allowed.example/allowed/page?ticket=benign-query-value",
        origins=("https://allowed.example",),
    )
    builder, _, _ = fake_url_builder(
        {
            source.url: response(
                200,
                headers={"Content-Type": "text/plain", "Content-Length": "42"},
                body=b"Hours: Monday to Friday, 09:00-17:00 UTC.",
            )
        }
    )
    review = builder.build((source,), output_dir=tmp_path / "out")
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
