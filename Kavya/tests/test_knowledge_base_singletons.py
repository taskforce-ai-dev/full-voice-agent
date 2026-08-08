"""The KB lazy singletons must survive concurrent cold entry.

retrieve_context now runs under asyncio.to_thread, so several calls can enter
these loaders at once on a cold start. Unguarded check-then-set would let two
threads each construct the embedding model -- 400MB-1GB apiece inside a 1536m
container -- and OOM every call at once. This protects the live Twilio path too.
"""

from __future__ import annotations

import threading
import time

import pytest

import knowledge_base


@pytest.fixture(autouse=True)
def _reset_singletons():
    original_client = knowledge_base._chroma_client
    original_model = knowledge_base._embedding_model
    knowledge_base._chroma_client = None
    knowledge_base._embedding_model = None
    yield
    knowledge_base._chroma_client = original_client
    knowledge_base._embedding_model = original_model


def _hammer(target, threads=8):
    """Call `target` from several threads released as simultaneously as possible."""
    start = threading.Barrier(threads)
    results: list[object] = []
    errors: list[BaseException] = []

    def run():
        try:
            start.wait(timeout=5)
            results.append(target())
        except BaseException as error:  # noqa: BLE001 - surfaced by the assertions
            errors.append(error)

    workers = [threading.Thread(target=run) for _ in range(threads)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=10)
    assert not errors, errors
    return results


def test_embedding_model_is_loaded_once_under_concurrent_cold_entry(monkeypatch):
    loads = []

    def slow_loader(name):
        # A real SentenceTransformer load is seconds long; that window is exactly
        # when a second thread slips past an unguarded check-then-set.
        loads.append(name)
        time.sleep(0.05)
        return f"model::{name}"

    monkeypatch.setattr(knowledge_base, "SENTENCE_TRANSFORMERS_AVAILABLE", True)
    monkeypatch.setattr(knowledge_base, "SentenceTransformer", slow_loader)

    results = _hammer(knowledge_base._get_embedding_model)

    assert len(loads) == 1, (
        f"the embedding model was constructed {len(loads)} times concurrently; "
        "two copies of a 400MB-1GB model will OOM the container"
    )
    assert len(set(map(id, results))) == 1, "every caller must get the same instance"


def test_chroma_client_is_created_once_under_concurrent_cold_entry(monkeypatch, tmp_path):
    created = []

    class FakeChromadb:
        @staticmethod
        def PersistentClient(path):
            created.append(path)
            time.sleep(0.05)
            return f"client::{path}"

    monkeypatch.setattr(knowledge_base, "CHROMADB_AVAILABLE", True)
    monkeypatch.setattr(knowledge_base, "chromadb", FakeChromadb)
    monkeypatch.setattr(knowledge_base, "PERSIST_DIRECTORY", str(tmp_path / "chroma"))

    results = _hammer(knowledge_base._get_chroma_client)

    assert len(created) == 1, (
        f"the Chroma client was created {len(created)} times concurrently"
    )
    assert len(set(map(id, results))) == 1


def test_a_failed_load_still_returns_none_without_deadlocking(monkeypatch):
    def broken_loader(_name):
        raise RuntimeError("model weights unavailable")

    monkeypatch.setattr(knowledge_base, "SENTENCE_TRANSFORMERS_AVAILABLE", True)
    monkeypatch.setattr(knowledge_base, "SentenceTransformer", broken_loader)

    assert _hammer(knowledge_base._get_embedding_model) == [None] * 8
    assert knowledge_base._get_embedding_model() is None


def test_unavailable_dependencies_short_circuit_without_locking(monkeypatch):
    monkeypatch.setattr(knowledge_base, "SENTENCE_TRANSFORMERS_AVAILABLE", False)
    monkeypatch.setattr(knowledge_base, "CHROMADB_AVAILABLE", False)

    assert knowledge_base._get_embedding_model() is None
    assert knowledge_base._get_chroma_client() is None
