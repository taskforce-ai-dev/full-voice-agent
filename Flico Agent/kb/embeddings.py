from functools import lru_cache
from typing import List

import numpy as np

from kb.config import EMBEDDING_MODEL_NAME


class EmbeddingEngine:
    def __init__(self, model_name: str = EMBEDDING_MODEL_NAME):
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:  # no silent hash fallback in production
            raise RuntimeError(
                "sentence-transformers is required for KB_BACKEND=sqlite. "
                "Install it or set KB_BACKEND=chroma."
            ) from exc
        self._model = SentenceTransformer(model_name)
        # bind the cache per-instance so tests can construct fresh engines
        self._cache = lru_cache(maxsize=256)(self._embed_uncached)

    def _embed_uncached(self, text: str) -> np.ndarray:
        v = self._model.encode(text, convert_to_numpy=True).astype(np.float32)
        n = np.linalg.norm(v)
        if n > 0:
            v = v / n
        return v

    def get_embedding(self, text: str) -> np.ndarray:
        return self._cache(text)

    def get_embeddings(self, texts: List[str]) -> np.ndarray:
        embs = self._model.encode(texts, convert_to_numpy=True).astype(np.float32)
        norms = np.linalg.norm(embs, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return embs / norms
