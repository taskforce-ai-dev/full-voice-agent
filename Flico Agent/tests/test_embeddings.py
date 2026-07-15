import numpy as np
import pytest
from kb.embeddings import EmbeddingEngine

st = pytest.importorskip("sentence_transformers")


@pytest.fixture(scope="module")
def engine():
    return EmbeddingEngine()


def test_single_is_unit_norm_float32(engine):
    v = engine.get_embedding("modern apartment with sea views")
    assert v.dtype == np.float32
    assert v.shape == (384,)
    assert abs(np.linalg.norm(v) - 1.0) < 1e-4


def test_cache_returns_same_object_for_repeat(engine):
    a = engine.get_embedding("colombo 7 penthouse")
    b = engine.get_embedding("colombo 7 penthouse")
    assert a is b  # LRU cache hit


def test_semantic_closer_than_unrelated(engine):
    q = engine.get_embedding("swimming pool and gym")
    near = engine.get_embedding("apartment with a pool and fitness centre")
    far = engine.get_embedding("bare land plot with no buildings")
    assert float(np.dot(q, near)) > float(np.dot(q, far))
