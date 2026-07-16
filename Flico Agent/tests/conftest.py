"""Shared test scaffolding.

The end-to-end retrieval tests used to `importorskip("sentence_transformers")`,
so they SKIPPED locally and in CI and only ran if someone happened to execute
them inside the container. That is the worst possible place for a gap: the
acceptance tests are exactly the ones that must gate a deploy. One of them was in
fact failing in the container while CI stayed green.

They can run without the model. `n_results=None` means ranking never truncates,
so it only ORDERS the SQL-matched set -- every set-membership assertion is
independent of the embedding. A zero-vector embedder therefore gives identical
results for everything these tests assert, with no torch and no model download.

Only use this where assertions are about WHICH listings come back. If you ever
assert relevance ORDER, you need the real model.
"""
import contextlib

import numpy as np
import pytest

from kb import engine as kb_engine


class ZeroEmbedder:
    """Stand-in for EmbeddingEngine. Every vector is zero, so cosine similarity
    is uniform and ranking degenerates to a stable pass-through."""

    def get_embeddings(self, texts):
        return np.zeros((len(texts), 384), dtype=np.float32)

    def get_embedding(self, text):
        return np.zeros(384, dtype=np.float32)


@contextlib.contextmanager
def zero_embedder():
    """Patch EmbeddingEngine for the duration of KB construction.

    RealEstateKB binds its embedder in __init__, so the patch only needs to cover
    construction -- the instance keeps the fake afterwards.
    """
    original = kb_engine.EmbeddingEngine
    kb_engine.EmbeddingEngine = ZeroEmbedder
    try:
        yield
    finally:
        kb_engine.EmbeddingEngine = original


def build_kb(db_path, props, preamble=""):
    with zero_embedder():
        kb = kb_engine.RealEstateKB(db_path=str(db_path), preamble=preamble)
    kb.add_properties(props)
    return kb


@pytest.fixture
def no_embedding_model():
    with zero_embedder():
        yield
