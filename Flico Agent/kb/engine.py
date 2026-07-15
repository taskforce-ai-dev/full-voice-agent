from typing import List, Optional

import numpy as np

from kb.config import DB_PATH
from kb.database import KBDatabase
from kb.embeddings import EmbeddingEngine
from kb.formatter import ContextFormatter
from kb.query_parser import QueryParser
from kb.schema import Property, QueryFilters


class RealEstateKB:
    def __init__(self, db_path: str = DB_PATH, preamble: str = ""):
        self.db = KBDatabase(db_path)
        self.embedder = EmbeddingEngine()
        self.preamble = preamble

    def add_properties(self, props: List[Property]) -> None:
        if not props:
            return
        embs = self.embedder.get_embeddings(
            [p.description + " " + " ".join(p.key_features) for p in props]
        )
        self.db.insert_properties_batch([(p, embs[i]) for i, p in enumerate(props)])
        self.db.reconcile(keep_ids={p.id for p in props})

    def get_count(self) -> int:
        return self.db.get_count()

    def _rank(self, query: str, candidates, n: int) -> List[Property]:
        if not candidates:
            return []
        qv = self.embedder.get_embedding(query)
        mat = np.stack([e for _, e in candidates])
        sims = mat @ qv
        order = np.argsort(-sims)[:n]
        return [candidates[i][0] for i in order]

    def _candidates(self, filters: QueryFilters):
        rows = self.db.query_properties(filters)
        if rows:
            return rows
        # Relaxation ladder
        if filters.property_type and filters.zone is not None:
            zone_only = QueryFilters(zone=filters.zone, transaction=filters.transaction)
            rows = self.db.query_properties(zone_only)
            if rows:
                return rows
            return []  # requested zone empty -> honest empty, never drop zone
        if filters.property_type and filters.zone is None:
            return self.db.query_properties(QueryFilters(transaction=filters.transaction))
        return []

    def retrieve(self, query: str, n_results: int = 6, sticky: Optional[dict] = None) -> str:
        semantic, filters = QueryParser.parse(query)
        if sticky is not None:
            filters = QueryParser.merge_sticky(filters, sticky)
        props = self._rank(semantic, self._candidates(filters), n_results)
        body = ContextFormatter.format(props)
        if self.preamble and body:
            return self.preamble + "\n\n" + body
        if self.preamble:
            return self.preamble
        return body
