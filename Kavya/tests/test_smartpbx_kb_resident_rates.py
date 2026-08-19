"""Deterministic KB retrieval + end-to-end SmartPBX turn tests: a Sri Lankan
resident-style rate enquiry must retrieve (and be answered from) the LKR
resident-rate section, not the USD foreign-guest section.

The first two tests exercise `knowledge_base.retrieve_context` alone (real
ChromaDB + sentence-transformers, no LLM) against the real
`knowledge_docs/hotel_info.txt`, pointed at a throwaway persistence directory
so they never touch the repo's own `chroma_db`.

The third test drives a real turn of the SmartPBX runner (real KB retrieval
feeding a mocked LLM client) to prove the resident-rate section actually
reaches the model's request and that the delivered answer is LKR-grounded --
and that a resident-rate question never triggers transfer_to_human.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

import knowledge_base as kb
import server
import tools

# test_smartpbx_server.py (this directory) is not a package -- pytest's
# per-file rootdir insertion during a single-file run does not reliably put
# this directory on sys.path for a sibling test module's own imports, so add
# it explicitly rather than duplicate its direct-runner test scaffolding.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_smartpbx_server import (  # noqa: E402
    GenerationTrackingTransport,
    bind_direct_smartpbx_turn,
    direct_text_round,
    direct_tool_client,
    direct_tool_pipeline,
)

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
    kb.set_kb_trace_id("test-trace-resident-rates:turn-1")
    with caplog.at_level("INFO", logger="knowledge_base"):
        context = kb.retrieve_context(
            "What are the rates for Sri Lankan residents?", n_results=3,
        )

    assert "SRI LANKAN RESIDENT RATES" in context
    assert "rupees" in context.lower()
    # The foreign/USD rate card must not be what gets surfaced ahead of the
    # resident section for a resident-rate question.
    foreign_idx = context.find("FOREIGN GUEST RATES")
    resident_idx = context.find("SRI LANKAN RESIDENT RATES")
    if foreign_idx != -1:
        assert resident_idx < foreign_idx

    # Chunk-traceability log line (Fix #3): identifiers only, never rates/text,
    # and never the raw call/session identifier -- only the bounded local
    # trace id this test bound above.
    retrieval_logs = [
        r for r in caplog.records if "kb_retrieval event=chunks_returned" in r.getMessage()
    ]
    assert retrieval_logs, "expected a kb_retrieval structured log line"
    log_message = retrieval_logs[-1].getMessage()
    assert "trace_id=test-trace-resident-rates:turn-1" in log_message
    assert "chunk_ids=" in log_message
    # Privacy: the log line must not contain the rate figures themselves.
    assert "158,000" not in log_message
    assert "rupees" not in log_message.lower()


def test_foreign_rate_query_returns_usd_section(isolated_kb):
    context = kb.retrieve_context(
        "What are the rates for foreign guests in US dollars?", n_results=3,
    )

    assert "FOREIGN GUEST RATES" in context
    assert "US dollars" in context


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["openai", "gemini", "claude"])
async def test_smartpbx_resident_rate_turn_delivers_lkr_answer_without_transfer(
    isolated_kb, monkeypatch, provider,
):
    """A resident-rate question is answered directly from the real KB; the
    model is never even offered a reason to call transfer_to_human."""
    lkr_answer = (
        "For Sri Lankan residents, the Forest Escape Suite is one hundred "
        "and fifty eight thousand rupees per room per night."
    )
    client = direct_tool_client(provider, [direct_text_round(provider, lkr_answer)])
    pipeline = direct_tool_pipeline(server, provider, client)
    bind_direct_smartpbx_turn(server, pipeline)
    pipeline._media_transport = GenerationTrackingTransport()
    monkeypatch.setattr(pipeline, "_start_initial_smartpbx_filler", lambda **_kwargs: None)

    delivered: list[str] = []
    transfer_calls: list[tuple[str, dict]] = []

    async def tts(text, **_kwargs):
        delivered.append(text)

    real_execute = tools.execute_tool

    async def execute(name, arguments):
        if name == "transfer_to_human":
            transfer_calls.append((name, arguments))
        return await real_execute(name, arguments)

    monkeypatch.setattr(pipeline, "_tts_elevenlabs", tts)
    monkeypatch.setattr(server, "execute_tool", execute)

    await asyncio.wait_for(
        pipeline._process_utterance("What are the resident rates in rupees?"),
        timeout=5,
    )

    # The exact request the mock LLM received must carry the real,
    # KB-retrieved resident-rate section -- proving retrieval->injection
    # wiring, not just that retrieve_context() alone can find it.
    sent_request = str(client.requests[-1])
    assert "SRI LANKAN RESIDENT RATES" in sent_request
    assert "rupees" in sent_request.lower()

    # The delivered answer is the LKR-grounded response, spoken to the caller.
    assert any("rupees" in sentence.lower() for sentence in delivered)

    # A resident-rate question never triggers a human handover.
    assert transfer_calls == []
