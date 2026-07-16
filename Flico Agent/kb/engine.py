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

    def _rank(self, query: str, candidates, n: Optional[int]) -> List[Property]:
        if not candidates:
            return []
        qv = self.embedder.get_embedding(query)
        mat = np.stack([e for _, e in candidates])
        sims = mat @ qv
        order = np.argsort(-sims)[:n]
        return [candidates[i][0] for i in order]

    # Relaxation ladder, loosest constraint first. Each step states what was
    # given up: dropping a constraint silently is how the KB ends up handing the
    # LLM a house for an apartment request with nothing marking it a mismatch.
    # The caller's ZONE is never dropped -- offering another area unasked was a
    # real production bug.
    _RELAXATIONS = (
        ("max_rent",
         "NOTE: nothing matches the caller's stated budget. Every listing below "
         "costs MORE than they asked for. Say so plainly before offering one."),
        ("bedrooms",
         "NOTE: nothing has the exact bedroom count the caller asked for. Every "
         "listing below has a DIFFERENT number of bedrooms. State each one's real "
         "bedroom count and acknowledge it is not what they asked for."),
        ("property_type",
         "NOTE: we have nothing of the property type the caller asked for. Every "
         "listing below is a DIFFERENT property type. Never describe one as the "
         "type they asked for -- say we have none, then offer these as alternatives."),
    )

    def _candidates(self, filters: QueryFilters):
        rows = self.db.query_properties(filters)
        if rows:
            return rows, ""

        vals = filters.model_dump()
        notes = []
        for field, note in self._RELAXATIONS:
            if field == "bedrooms":
                if vals["bedrooms"] is None and vals["min_bedrooms"] is None:
                    continue
                vals["bedrooms"] = vals["min_bedrooms"] = None
            else:
                if vals[field] is None:
                    continue
                vals[field] = None
            notes.append(note)
            rows = self.db.query_properties(QueryFilters(**vals))
            if rows:
                return rows, "\n".join(notes)
        return [], ""

    def retrieve(self, query: str, n_results: Optional[int] = None,
                 sticky: Optional[dict] = None) -> str:
        """n_results=None returns every row the SQL filter matched, ranked.

        Truncating below the matched set silently hides listings the caller
        qualifies for -- at the old default of 6, "anything under 300k" showed
        6 of the 9 matches. The filter is the layer we can prove correct, so it,
        not an arbitrary cap, decides what the caller is told about.
        """
        semantic, filters = QueryParser.parse(query)
        if sticky is not None:
            filters = QueryParser.merge_sticky(filters, sticky)
        candidates, note = self._candidates(filters)
        body = ContextFormatter.format(self._rank(semantic, candidates, n_results))
        if body and note:
            body = note + "\n\n" + body
        return "\n\n".join(p for p in (self.preamble, body) if p)
