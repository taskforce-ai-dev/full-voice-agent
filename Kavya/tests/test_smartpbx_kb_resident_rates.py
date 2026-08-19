"""Deterministic KB retrieval test: a Sri Lankan resident-style rate enquiry
must retrieve the LKR resident-rate section, not the USD foreign-guest
section. No LLM is involved — this exercises the real ChromaDB +
sentence-transformers retrieval path (`knowledge_base.retrieve_context`)
against the real `knowledge_docs/hotel_info.txt`, pointed at a throwaway
persistence directory so it never touches the repo's own `chroma_db`.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import knowledge_base as kb

DOCS_DIRECTORY = str(Path(__file__).resolve().parent.parent / "knowledge_docs")

pytestmark = pytest.mark.skipif(
    not (kb.CHROMADB_AVAILABLE and kb.SENTENCE_TRANSFORMERS_AVAILABLE),
    reason="chromadb + sentence-transformers are required for real KB retrieval",
)


@pytest.fixture
def isolated_kb(tmp_path, monkeypatch):
    """Point the KB singletons at a throwaway directory/collection and reset
    them afterwards so this test cannot leak state into any other test or
    into the repo's real chroma_db/."""
    monkeypatch.setattr(kb, "PERSIST_DIRECTORY", str(tmp_path / "chroma_db_test"))
    monkeypatch.setattr(kb, "COLLECTION_NAME", "test_kb_resident_rates")
    monkeypatch.setattr(kb, "_chroma_client", None)
    monkeypatch.setattr(kb, "_embedding_model", None)
    kb._cached_embed_query.cache_clear()

    # retrieve_context() requires the real sentence-transformers weights for
    # the QUERY embedding (see its call to _get_embedding_model()), even when
    # ChromaDB's own bundled default embedder could index documents without
    # them. Loading the model here — rather than letting initialize_kb()
    # silently swallow the failure — lets a genuine network-restricted
    # environment (no egress to huggingface.co) skip with a clear reason
    # instead of failing on an empty-string assertion that would look like a
    # retrieval regression.
    if kb._get_embedding_model() is None:
        pytest.skip(
            "sentence-transformers could not load all-MiniLM-L6-v2 "
            "(no network access to huggingface.co in this environment); "
            "this test exercises the real embedding + retrieval path and "
            "needs that access (present in CI)."
        )

    assert kb.initialize_kb(docs_directory=DOCS_DIRECTORY) is True

    yield

    monkeypatch.setattr(kb, "_chroma_client", None)
    monkeypatch.setattr(kb, "_embedding_model", None)
    kb._cached_embed_query.cache_clear()


def test_resident_rate_query_returns_lkr_section(isolated_kb, caplog):
    with caplog.at_level("INFO", logger="knowledge_base"):
        context = kb.retrieve_context(
            "What are the rates for Sri Lankan residents?",
            n_results=3,
            call_sid="test-call-resident-rates",
        )

    assert "SRI LANKAN RESIDENT RATES" in context
    assert "rupees" in context.lower()
    # The foreign/USD rate card must not be what gets surfaced ahead of the
    # resident section for a resident-rate question.
    foreign_idx = context.find("FOREIGN GUEST RATES")
    resident_idx = context.find("SRI LANKAN RESIDENT RATES")
    if foreign_idx != -1:
        assert resident_idx < foreign_idx

    # Chunk-traceability log line (Fix #3): identifiers only, never rates/text.
    retrieval_logs = [
        r for r in caplog.records if "kb_retrieval event=chunks_returned" in r.getMessage()
    ]
    assert retrieval_logs, "expected a kb_retrieval structured log line"
    log_message = retrieval_logs[-1].getMessage()
    assert "call_sid=test-call-resident-rates" in log_message
    assert "chunk_ids=" in log_message
    # Privacy: the log line must not contain the rate figures themselves.
    assert "158,000" not in log_message
    assert "rupees" not in log_message.lower()


def test_foreign_rate_query_returns_usd_section(isolated_kb):
    context = kb.retrieve_context(
        "What are the rates for foreign guests in US dollars?",
        n_results=3,
        call_sid="test-call-foreign-rates",
    )

    assert "FOREIGN GUEST RATES" in context
    assert "US dollars" in context
