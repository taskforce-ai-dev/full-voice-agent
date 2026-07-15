# Rodrigo Realtors SQLite + Local-Vector Hybrid KB — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the Rodrigo Realtors ChromaDB knowledge base with a flag-guarded SQLite + in-memory MiniLM-cosine hybrid, localized to the Sri Lanka rental portfolio, without modifying `server.py`.

**Architecture:** SQLite stores structured listing columns *and* the 384-dim embedding blob per row. Each turn: a rule parser extracts filters (rent range, beds, Colombo zone, type) + a semantic query; SQL narrows candidates; NumPy cosine ranks within them; a relaxation ladder handles empties; the top-N are returned as a compact **context string** the LLM speaks in the caller's language. `knowledge_base.py` becomes a dispatcher routing to the new engine or the retained ChromaDB code by the `KB_BACKEND` env var.

**Tech Stack:** Python 3.11, `sqlite3` (stdlib), `numpy`, `pydantic>=2`, `sentence-transformers` (`all-MiniLM-L6-v2`), `pytest`.

## Global Constraints

- **Work only on branch `feature/rodrigo-sqlite-hybrid-kb`. NEVER `git push`** — pushing `main` auto-deploys the live agent.
- All new code lives under `Flico Agent/` (the Rodrigo agent directory).
- **Do not modify `Flico Agent/server.py`.** Compatibility is via the preserved dispatcher interface only.
- The four contract functions must keep these exact signatures:
  - `initialize_kb(docs_directory: str = DEFAULT_DOCS_DIRECTORY) -> bool`
  - `prewarm() -> None`
  - `reload_kb_from_content(content: str, filename: str = "flico_info.txt") -> bool`
  - `retrieve_context(query: str, n_results: int = 6, sticky: Optional[dict] = None) -> str`
- `retrieve_context` returns a **context string for the LLM**, never caller-facing speech.
- `KB_BACKEND` defaults to `chroma`. The `sqlite` path is opt-in; a merge must be inert until the flag is flipped on the VPS.
- Embedding model: `all-MiniLM-L6-v2`, dimension `384`, unit-normalized at write time.
- In `KB_BACKEND=sqlite`, a missing `sentence-transformers` **raises** — no silent hash fallback.
- Run tests with the repo venv: `cd "Flico Agent" && PYTHONPATH=. ../.venv/bin/pytest` (the dev-rig venv with deps is at repo-root `.venv`; if absent, create with `python3 -m venv .venv && .venv/bin/pip install pydantic numpy sentence-transformers pytest`).
- Commit after every task. Conventional-commit messages. End each commit body with:
  `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`

---

## File Structure

Created under `Flico Agent/`:

- `kb/__init__.py` — package marker.
- `kb/config.py` — paths, model name, dimension.
- `kb/schema.py` — Pydantic `Property`, `QueryFilters`.
- `kb/database.py` — `KBDatabase`: DDL, indices, reconcile-on-load upsert, parameterized filter query.
- `kb/embeddings.py` — `EmbeddingEngine`: MiniLM, normalized, LRU query cache, no silent fallback.
- `kb/query_parser.py` — `QueryParser`: STT maps, area→zone, spelled numbers, occupancy rule, rent range, sticky merge.
- `kb/formatter.py` — `ContextFormatter`: rows → compact LLM context string.
- `kb/engine.py` — `RealEstateKB`: parse → filter → rank → relax → format; loads the DB; static preamble.
- `kb/migrate.py` — prose → structured rows (+ regenerated JSON), coverage report.
- `knowledge_base_chroma.py` — the current `knowledge_base.py`, moved verbatim.
- `knowledge_base_sqlite.py` — adapter mapping the four contract functions onto `kb/engine.py`.
- `knowledge_base.py` — dispatcher routing by `KB_BACKEND`.
- `tests/test_schema.py`, `tests/test_database.py`, `tests/test_embeddings.py`, `tests/test_query_parser.py`, `tests/test_formatter.py`, `tests/test_engine.py`, `tests/test_migrate.py`, `tests/test_dispatcher.py`.
- `data/` — holds the generated `rodrigo_kb.db` and `rodrigo_listings.json` (gitignore the `.db`).

---

## Task 1: Package scaffold, config, and schema

**Files:**
- Create: `Flico Agent/kb/__init__.py`, `Flico Agent/kb/config.py`, `Flico Agent/kb/schema.py`
- Test: `Flico Agent/tests/test_schema.py`

**Interfaces:**
- Produces: `Property` (Pydantic model, fields below), `QueryFilters` (Pydantic model), and config constants `DB_PATH`, `EMBEDDING_MODEL_NAME="all-MiniLM-L6-v2"`, `EMBEDDING_DIMENSION=384`, `DEFAULT_DOCS_DIRECTORY="knowledge_docs"`.

- [ ] **Step 1: Write the failing test**

`Flico Agent/tests/test_schema.py`:
```python
from kb.schema import Property, QueryFilters


def test_property_minimal_rental():
    p = Property(
        id="P15", transaction="rent", property_type="apartment",
        zone=3, area="Kollupitiya", building="606 The Address",
        bedrooms=3, bathrooms=3.0,
        rent_amount=600000.0, rent_period="month", rent_on_request=False,
        furnishing="unfurnished", floor_area_sqft=2138, parking=1,
        key_features=["sea views", "swimming pool"],
        description="Rodrigo Realtors has a 3-bedroom apartment for rent in Colombo 3.",
    )
    assert p.zone == 3
    assert p.rent_period == "month"
    assert p.rent_on_request is False


def test_property_rent_on_request_allows_null_amount():
    p = Property(
        id="P02", transaction="rent", property_type="apartment", zone=5,
        area="Havelock Town", bedrooms=3, bathrooms=2.0,
        rent_amount=None, rent_period=None, rent_on_request=True,
        description="Rodrigo Realtors has a 3-bedroom apartment for rent in Colombo 5.",
    )
    assert p.rent_amount is None
    assert p.rent_on_request is True


def test_queryfilters_all_optional():
    f = QueryFilters()
    assert f.property_type is None
    assert f.zone is None
    assert f.max_rent is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd "Flico Agent" && PYTHONPATH=. ../.venv/bin/pytest tests/test_schema.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'kb'`.

- [ ] **Step 3: Write the implementation**

`Flico Agent/kb/__init__.py`: (empty file)

`Flico Agent/kb/config.py`:
```python
import os

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "rodrigo_kb.db")
LISTINGS_JSON = os.path.join(DATA_DIR, "rodrigo_listings.json")

EMBEDDING_MODEL_NAME = "all-MiniLM-L6-v2"
EMBEDDING_DIMENSION = 384

DEFAULT_DOCS_DIRECTORY = "knowledge_docs"

os.makedirs(DATA_DIR, exist_ok=True)
```

`Flico Agent/kb/schema.py`:
```python
from typing import List, Optional
from pydantic import BaseModel, Field


class Property(BaseModel):
    id: str
    transaction: str = Field(description="'rent' or 'sale'")
    property_type: str = Field(description="apartment | house | commercial | land")
    zone: Optional[int] = Field(default=None, description="Colombo postal zone 1-10")
    area: str = ""
    building: Optional[str] = None
    bedrooms: Optional[int] = None
    bathrooms: Optional[float] = None
    rent_amount: Optional[float] = None
    rent_period: Optional[str] = Field(default=None, description="'month' or 'day'")
    rent_on_request: bool = False
    sale_price: Optional[float] = None
    furnishing: Optional[str] = Field(default=None, description="furnished | semi | unfurnished")
    floor_area_sqft: Optional[int] = None
    parking: Optional[int] = None
    deposit_months: Optional[int] = None
    advance_months: Optional[int] = None
    min_lease_months: Optional[int] = None
    key_features: List[str] = Field(default_factory=list)
    description: str = ""


class QueryFilters(BaseModel):
    transaction: Optional[str] = None
    property_type: Optional[str] = None
    zone: Optional[int] = None
    min_bedrooms: Optional[int] = None
    min_rent: Optional[float] = None
    max_rent: Optional[float] = None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd "Flico Agent" && PYTHONPATH=. ../.venv/bin/pytest tests/test_schema.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add "Flico Agent/kb/__init__.py" "Flico Agent/kb/config.py" "Flico Agent/kb/schema.py" "Flico Agent/tests/test_schema.py"
git commit -m "feat(kb): add package scaffold, config, and localized schema"
```

---

## Task 2: SQLite database layer with reconcile-on-load

**Files:**
- Create: `Flico Agent/kb/database.py`
- Test: `Flico Agent/tests/test_database.py`

**Interfaces:**
- Consumes: `Property`, `QueryFilters` from `kb.schema`.
- Produces: `KBDatabase(db_path: str)` with methods:
  - `insert_properties_batch(rows: list[tuple[Property, np.ndarray]]) -> None`
  - `reconcile(keep_ids: set[str]) -> int` (deletes rows whose id ∉ keep_ids; returns count deleted)
  - `query_properties(filters: QueryFilters) -> list[tuple[Property, np.ndarray]]`
  - `get_count() -> int`
  - `clear() -> None`

- [ ] **Step 1: Write the failing test**

`Flico Agent/tests/test_database.py`:
```python
import numpy as np
import pytest
from kb.database import KBDatabase
from kb.schema import Property, QueryFilters


def _prop(pid, ptype="apartment", zone=7, beds=3, rent=500000.0):
    return Property(
        id=pid, transaction="rent", property_type=ptype, zone=zone,
        area="Cinnamon Gardens", bedrooms=beds, bathrooms=2.0,
        rent_amount=rent, rent_period="month", rent_on_request=False,
        description=f"listing {pid}",
    )


@pytest.fixture
def db(tmp_path):
    return KBDatabase(str(tmp_path / "t.db"))


def _vec():
    v = np.ones(384, dtype=np.float32)
    return v / np.linalg.norm(v)


def test_insert_and_count(db):
    db.insert_properties_batch([(_prop("P1"), _vec()), (_prop("P2"), _vec())])
    assert db.get_count() == 2


def test_filter_by_type_and_zone(db):
    db.insert_properties_batch([
        (_prop("P1", ptype="apartment", zone=7), _vec()),
        (_prop("P2", ptype="house", zone=7), _vec()),
        (_prop("P3", ptype="apartment", zone=5), _vec()),
    ])
    rows = db.query_properties(QueryFilters(property_type="apartment", zone=7))
    assert [p.id for p, _ in rows] == ["P1"]


def test_filter_by_max_rent(db):
    db.insert_properties_batch([
        (_prop("P1", rent=300000.0), _vec()),
        (_prop("P2", rent=900000.0), _vec()),
    ])
    rows = db.query_properties(QueryFilters(max_rent=500000.0))
    assert [p.id for p, _ in rows] == ["P1"]


def test_reconcile_removes_absent_ids(db):
    db.insert_properties_batch([(_prop("P1"), _vec()), (_prop("P2"), _vec())])
    deleted = db.reconcile(keep_ids={"P1"})
    assert deleted == 1
    assert db.get_count() == 1


def test_embedding_roundtrip(db):
    v = _vec()
    db.insert_properties_batch([(_prop("P1"), v)])
    _, got = db.query_properties(QueryFilters())[0]
    assert got.dtype == np.float32
    assert np.allclose(got, v)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd "Flico Agent" && PYTHONPATH=. ../.venv/bin/pytest tests/test_database.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'kb.database'`.

- [ ] **Step 3: Write the implementation**

`Flico Agent/kb/database.py`:
```python
import json
import sqlite3
from contextlib import closing
from typing import List, Optional, Tuple

import numpy as np

from kb.schema import Property, QueryFilters

_COLUMNS = [
    "id", "transaction", "property_type", "zone", "area", "building",
    "bedrooms", "bathrooms", "rent_amount", "rent_period", "rent_on_request",
    "sale_price", "furnishing", "floor_area_sqft", "parking",
    "deposit_months", "advance_months", "min_lease_months",
    "key_features", "description",
]


class KBDatabase:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS properties (
                    id TEXT PRIMARY KEY,
                    transaction TEXT NOT NULL,
                    property_type TEXT NOT NULL,
                    zone INTEGER,
                    area TEXT,
                    building TEXT,
                    bedrooms INTEGER,
                    bathrooms REAL,
                    rent_amount REAL,
                    rent_period TEXT,
                    rent_on_request INTEGER NOT NULL DEFAULT 0,
                    sale_price REAL,
                    furnishing TEXT,
                    floor_area_sqft INTEGER,
                    parking INTEGER,
                    deposit_months INTEGER,
                    advance_months INTEGER,
                    min_lease_months INTEGER,
                    key_features TEXT,
                    description TEXT NOT NULL,
                    embedding BLOB NOT NULL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_txn ON properties(transaction)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_type ON properties(property_type)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_zone ON properties(zone)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_beds ON properties(bedrooms)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_rent ON properties(rent_amount)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_sale ON properties(sale_price)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_type_zone ON properties(property_type, zone)")

    def _row_values(self, p: Property, embedding: np.ndarray) -> tuple:
        return (
            p.id, p.transaction, p.property_type, p.zone, p.area, p.building,
            p.bedrooms, p.bathrooms, p.rent_amount, p.rent_period, int(p.rent_on_request),
            p.sale_price, p.furnishing, p.floor_area_sqft, p.parking,
            p.deposit_months, p.advance_months, p.min_lease_months,
            json.dumps(p.key_features), p.description,
            embedding.astype(np.float32).tobytes(),
        )

    def insert_properties_batch(self, rows: List[Tuple[Property, np.ndarray]]) -> None:
        placeholders = ", ".join(["?"] * (len(_COLUMNS) + 1))
        sql = f"INSERT OR REPLACE INTO properties ({', '.join(_COLUMNS)}, embedding) VALUES ({placeholders})"
        with closing(self._connect()) as conn, conn:
            conn.executemany(sql, [self._row_values(p, e) for p, e in rows])

    def reconcile(self, keep_ids: set) -> int:
        with closing(self._connect()) as conn, conn:
            cur = conn.execute("SELECT id FROM properties")
            all_ids = {r["id"] for r in cur.fetchall()}
            stale = all_ids - keep_ids
            for sid in stale:
                conn.execute("DELETE FROM properties WHERE id = ?", (sid,))
            return len(stale)

    def clear(self) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute("DELETE FROM properties")

    def get_count(self) -> int:
        with closing(self._connect()) as conn:
            return conn.execute("SELECT COUNT(*) AS c FROM properties").fetchone()["c"]

    def query_properties(self, filters: QueryFilters) -> List[Tuple[Property, np.ndarray]]:
        conditions, params = [], []
        if filters.transaction is not None:
            conditions.append("transaction = ?"); params.append(filters.transaction)
        if filters.property_type is not None:
            conditions.append("property_type = ?"); params.append(filters.property_type)
        if filters.zone is not None:
            conditions.append("zone = ?"); params.append(filters.zone)
        if filters.min_bedrooms is not None:
            conditions.append("bedrooms >= ?"); params.append(filters.min_bedrooms)
        if filters.min_rent is not None:
            conditions.append("rent_amount >= ?"); params.append(filters.min_rent)
        if filters.max_rent is not None:
            # rent_on_request rows (NULL amount) survive a max_rent filter so
            # premium listings are still offered; the caller is told to ask.
            conditions.append("(rent_amount <= ? OR rent_amount IS NULL)"); params.append(filters.max_rent)

        sql = "SELECT * FROM properties"
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)

        results = []
        with closing(self._connect()) as conn:
            for row in conn.execute(sql, params):
                emb = np.frombuffer(row["embedding"], dtype=np.float32)
                p = Property(
                    id=row["id"], transaction=row["transaction"],
                    property_type=row["property_type"], zone=row["zone"],
                    area=row["area"] or "", building=row["building"],
                    bedrooms=row["bedrooms"], bathrooms=row["bathrooms"],
                    rent_amount=row["rent_amount"], rent_period=row["rent_period"],
                    rent_on_request=bool(row["rent_on_request"]),
                    sale_price=row["sale_price"], furnishing=row["furnishing"],
                    floor_area_sqft=row["floor_area_sqft"], parking=row["parking"],
                    deposit_months=row["deposit_months"], advance_months=row["advance_months"],
                    min_lease_months=row["min_lease_months"],
                    key_features=json.loads(row["key_features"] or "[]"),
                    description=row["description"],
                )
                results.append((p, emb))
        return results
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd "Flico Agent" && PYTHONPATH=. ../.venv/bin/pytest tests/test_database.py -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add "Flico Agent/kb/database.py" "Flico Agent/tests/test_database.py"
git commit -m "feat(kb): add SQLite layer with reconcile-on-load and parameterized filters"
```

---

## Task 3: Embedding engine (MiniLM, normalized, LRU cache, no silent fallback)

**Files:**
- Create: `Flico Agent/kb/embeddings.py`
- Test: `Flico Agent/tests/test_embeddings.py`

**Interfaces:**
- Consumes: config `EMBEDDING_MODEL_NAME`, `EMBEDDING_DIMENSION`.
- Produces: `EmbeddingEngine(model_name=EMBEDDING_MODEL_NAME)` with:
  - `get_embedding(text: str) -> np.ndarray` (float32, unit norm, cached via LRU on text)
  - `get_embeddings(texts: list[str]) -> np.ndarray` (2-D, each row unit norm)
  - raises `RuntimeError` at construction if `sentence-transformers` is unavailable.

- [ ] **Step 1: Write the failing test**

`Flico Agent/tests/test_embeddings.py`:
```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd "Flico Agent" && PYTHONPATH=. ../.venv/bin/pytest tests/test_embeddings.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'kb.embeddings'`.

- [ ] **Step 3: Write the implementation**

`Flico Agent/kb/embeddings.py`:
```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd "Flico Agent" && PYTHONPATH=. ../.venv/bin/pytest tests/test_embeddings.py -v`
Expected: PASS (3 tests). First run downloads the model (~25 s).

- [ ] **Step 5: Commit**

```bash
git add "Flico Agent/kb/embeddings.py" "Flico Agent/tests/test_embeddings.py"
git commit -m "feat(kb): add MiniLM embedding engine with LRU cache, no silent fallback"
```

---

## Task 4: Query parser (voice lessons + rent range + sticky merge)

**Files:**
- Create: `Flico Agent/kb/query_parser.py`
- Test: `Flico Agent/tests/test_query_parser.py`

**Interfaces:**
- Consumes: `QueryFilters` from `kb.schema`.
- Produces: `QueryParser` with staticmethods:
  - `parse(utterance: str) -> tuple[str, QueryFilters]` — returns (semantic_query, filters). `filters.property_type/zone` may be None; occupancy is never a bedroom filter.
  - `merge_sticky(filters: QueryFilters, sticky: dict) -> QueryFilters` — this-turn value wins, else inherit `sticky["property_type"]` / `sticky["zone"]`; updates `sticky` in place with any non-None values.

Port the maps from `Flico Agent/knowledge_base_chroma.py` (`_AREA_TO_ZONE`, `_NUM_WORDS`, `_COMMERCIAL_MARKERS`, the `col[ou]mb[ou]s?` regex).

- [ ] **Step 1: Write the failing test**

`Flico Agent/tests/test_query_parser.py`:
```python
from kb.query_parser import QueryParser
from kb.schema import QueryFilters


def test_type_and_zone_digit():
    _, f = QueryParser.parse("a 3 bedroom apartment in colombo 7")
    assert f.property_type == "apartment"
    assert f.zone == 7


def test_stt_mishearing_columbo():
    _, f = QueryParser.parse("show me houses in columbus 5")
    assert f.property_type == "house"
    assert f.zone == 5


def test_area_name_to_zone():
    _, f = QueryParser.parse("something in kollupitiya")
    assert f.zone == 3


def test_havelock_mishearing():
    _, f = QueryParser.parse("an apartment near havoc town")
    assert f.zone == 5


def test_spelled_out_zone():
    _, f = QueryParser.parse("apartment in colombo five")
    assert f.zone == 5


def test_occupancy_is_not_bedrooms():
    _, f = QueryParser.parse("we are 4 people looking for an apartment")
    assert f.min_bedrooms is None


def test_explicit_bedrooms_is_a_filter():
    _, f = QueryParser.parse("a 3 bedroom apartment")
    assert f.min_bedrooms == 3


def test_rent_range_month():
    _, f = QueryParser.parse("apartment under 500k a month")
    assert f.max_rent == 500000.0


def test_commercial_before_house():
    _, f = QueryParser.parse("office space in colombo 1")
    assert f.property_type == "commercial"


def test_sticky_inherits_type_when_only_zone_stated():
    sticky = {"property_type": "apartment", "zone": 7}
    _, f = QueryParser.parse("actually I'd love colombo 5")
    merged = QueryParser.merge_sticky(f, sticky)
    assert merged.property_type == "apartment"
    assert merged.zone == 5
    assert sticky["zone"] == 5  # updated in place


def test_sticky_this_turn_overrides():
    sticky = {"property_type": "apartment", "zone": 7}
    _, f = QueryParser.parse("show me a house instead")
    merged = QueryParser.merge_sticky(f, sticky)
    assert merged.property_type == "house"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd "Flico Agent" && PYTHONPATH=. ../.venv/bin/pytest tests/test_query_parser.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'kb.query_parser'`.

- [ ] **Step 3: Write the implementation**

`Flico Agent/kb/query_parser.py`:
```python
import re
from typing import Optional, Tuple

from kb.schema import QueryFilters

_AREA_TO_ZONE = {
    "fort": 1, "galle face": 1,
    "slave island": 2, "union place": 2,
    "kollupitiya": 3, "kollupitya": 3, "colpetty": 3,
    "bambalapitiya": 4,
    "havelock town": 5, "havelock city": 5, "havelock": 5, "narahenpita": 5,
    "havoc town": 5, "havoc city": 5, "havoc": 5, "haverlock": 5,
    "wellawatte": 6, "wellawatta": 6,
    "cinnamon gardens": 7,
    "borella": 8,
    "maradana": 10,
}
_NUM_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
              "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}
_COMMERCIAL_MARKERS = (
    "office space", "commercial building", "commercial property",
    "commercial house", "commercial space", "commercial unit", "office", "shop",
    "retail", "warehouse",
)


class QueryParser:
    @staticmethod
    def _classify_type(low: str) -> Optional[str]:
        if any(m in low for m in _COMMERCIAL_MARKERS):
            return "commercial"
        if "apartment" in low or "flat" in low:
            return "apartment"
        if "house" in low or "villa" in low or "bungalow" in low:
            return "house"
        if "land" in low or "plot" in low or "bare land" in low:
            return "land"
        return None

    @staticmethod
    def _zone(low: str) -> Optional[int]:
        m = re.search(r"col[ou]mb[ou]s?[\s\-]*(\d{1,2})", low)
        if m:
            return int(m.group(1))
        m2 = re.search(r"col[ou]mb[ou]s?[\s\-]+([a-z]+)", low)
        if m2 and m2.group(1) in _NUM_WORDS:
            return _NUM_WORDS[m2.group(1)]
        for area, z in _AREA_TO_ZONE.items():
            if re.search(r"\b" + re.escape(area) + r"\b", low):
                return z
        return None

    @staticmethod
    def _max_rent(low: str) -> Optional[float]:
        m = re.search(r"(?:under|below|less than|max|budget of|up to)\s*(?:rs\.?\s*)?"
                      r"(\d+(?:\.\d+)?)\s*(k|m|thousand|million|lakhs?)?", low)
        if not m:
            return None
        val = float(m.group(1))
        unit = (m.group(2) or "").lower()
        if unit in ("k", "thousand"):
            return val * 1_000
        if unit in ("m", "million"):
            return val * 1_000_000
        if unit.startswith("lakh"):
            return val * 100_000
        return val * 1_000 if val < 10_000 else val

    @staticmethod
    def parse(utterance: str) -> Tuple[str, QueryFilters]:
        low = utterance.lower()
        f = QueryFilters()
        f.property_type = QueryParser._classify_type(low)
        f.zone = QueryParser._zone(low)
        f.max_rent = QueryParser._max_rent(low)

        # Bedrooms only from an explicit "N-bed(room)" phrase. Bare "N people"
        # is occupancy, never a bedroom filter.
        bm = re.search(r"(\d+)\s*-?\s*(?:bed|bedroom|br|bd)s?\b", low)
        if bm:
            f.min_bedrooms = int(bm.group(1))

        return utterance, f

    @staticmethod
    def merge_sticky(filters: QueryFilters, sticky: dict) -> QueryFilters:
        if filters.property_type is None and sticky.get("property_type"):
            filters.property_type = sticky["property_type"]
        if filters.zone is None and sticky.get("zone"):
            filters.zone = sticky["zone"]
        if filters.property_type:
            sticky["property_type"] = filters.property_type
        if filters.zone:
            sticky["zone"] = filters.zone
        return filters
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd "Flico Agent" && PYTHONPATH=. ../.venv/bin/pytest tests/test_query_parser.py -v`
Expected: PASS (11 tests).

- [ ] **Step 5: Commit**

```bash
git add "Flico Agent/kb/query_parser.py" "Flico Agent/tests/test_query_parser.py"
git commit -m "feat(kb): add rule parser with STT maps, rent range, sticky merge"
```

---

## Task 5: Context formatter (rows → LLM context string)

**Files:**
- Create: `Flico Agent/kb/formatter.py`
- Test: `Flico Agent/tests/test_formatter.py`

**Interfaces:**
- Consumes: `Property` from `kb.schema`.
- Produces: `ContextFormatter.format(props: list[Property]) -> str`. Each listing becomes one self-contained line; `rent_on_request` renders "rent on request — a consultant will confirm on follow-up"; `rent_period` is stated verbatim ("per day" / "per month"), never assumed. Returns `""` for an empty list.

- [ ] **Step 1: Write the failing test**

`Flico Agent/tests/test_formatter.py`:
```python
from kb.formatter import ContextFormatter
from kb.schema import Property


def _p(**kw):
    base = dict(id="P1", transaction="rent", property_type="apartment", zone=7,
                area="Cinnamon Gardens", bedrooms=3, bathrooms=2.0, description="")
    base.update(kw)
    return Property(**base)


def test_empty_returns_empty_string():
    assert ContextFormatter.format([]) == ""


def test_period_stated_verbatim_per_day():
    out = ContextFormatter.format([_p(rent_amount=15000.0, rent_period="day")])
    assert "per day" in out
    assert "per month" not in out


def test_rent_on_request_marked():
    out = ContextFormatter.format([_p(rent_on_request=True, rent_amount=None, rent_period=None)])
    assert "on request" in out.lower()


def test_includes_zone_and_type():
    out = ContextFormatter.format([_p()])
    assert "Colombo 7" in out
    assert "apartment" in out
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd "Flico Agent" && PYTHONPATH=. ../.venv/bin/pytest tests/test_formatter.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'kb.formatter'`.

- [ ] **Step 3: Write the implementation**

`Flico Agent/kb/formatter.py`:
```python
from typing import List

from kb.schema import Property


class ContextFormatter:
    @staticmethod
    def _rent(p: Property) -> str:
        if p.rent_on_request or p.rent_amount is None:
            return "rent on request — a consultant will confirm on follow-up"
        period = p.rent_period or "month"
        return f"Rs {int(p.rent_amount):,} per {period}"

    @staticmethod
    def _line(p: Property) -> str:
        loc = f"Colombo {p.zone}" if p.zone else (p.area or "")
        where = f" at {p.building}" if p.building else ""
        beds = f"{p.bedrooms}-bedroom " if p.bedrooms else ""
        furnish = f"{p.furnishing} " if p.furnishing else ""
        area = f", {p.floor_area_sqft} sq ft" if p.floor_area_sqft else ""
        feats = f" Features: {', '.join(p.key_features)}." if p.key_features else ""
        return (
            f"[{p.id}] Rodrigo Realtors has a {beds}{furnish}{p.property_type} "
            f"for {p.transaction}{where} in {loc}{area}. {ContextFormatter._rent(p)}.{feats}"
        )

    @staticmethod
    def format(props: List[Property]) -> str:
        if not props:
            return ""
        return "\n\n".join(ContextFormatter._line(p) for p in props)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd "Flico Agent" && PYTHONPATH=. ../.venv/bin/pytest tests/test_formatter.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add "Flico Agent/kb/formatter.py" "Flico Agent/tests/test_formatter.py"
git commit -m "feat(kb): add context formatter with verbatim period and on-request handling"
```

---

## Task 6: Engine (parse → filter → rank → relax → format) + static preamble

**Files:**
- Create: `Flico Agent/kb/engine.py`
- Test: `Flico Agent/tests/test_engine.py`

**Interfaces:**
- Consumes: `KBDatabase`, `EmbeddingEngine`, `QueryParser`, `ContextFormatter`, `QueryFilters`, `Property`.
- Produces: `RealEstateKB(db_path=DB_PATH, preamble="")` with:
  - `add_properties(props: list[Property]) -> None` (embeds `description`, batch-inserts, reconciles to the given id set)
  - `get_count() -> int`
  - `retrieve(query: str, n_results: int = 6, sticky: Optional[dict] = None) -> str` — full pipeline; prepends the static preamble; empty candidates after relaxation → returns just the preamble (or "").
  - The **relaxation ladder**: filter `{type, zone, rent}`; if empty and both type+zone set → retry `{zone}` only; if a zone was requested and still empty → honest empty (do not drop zone); if only type set and empty → drop the filter.

- [ ] **Step 1: Write the failing test**

`Flico Agent/tests/test_engine.py`:
```python
import pytest
from kb.engine import RealEstateKB
from kb.schema import Property

pytest.importorskip("sentence_transformers")


def _p(pid, ptype, zone, beds=3, desc="a lovely home", rent=500000.0, req=False, period="month"):
    return Property(
        id=pid, transaction="rent", property_type=ptype, zone=zone,
        area="Area", bedrooms=beds, bathrooms=2.0,
        rent_amount=None if req else rent, rent_period=None if req else period,
        rent_on_request=req, description=desc,
    )


@pytest.fixture(scope="module")
def kb(tmp_path_factory):
    path = tmp_path_factory.mktemp("kb") / "e.db"
    k = RealEstateKB(db_path=str(path), preamble="PREAMBLE.")
    k.add_properties([
        _p("P1", "apartment", 7, desc="bright apartment with a swimming pool and gym"),
        _p("P2", "house", 7, desc="large family house with a garden"),
        _p("P3", "apartment", 5, desc="cozy flat near the school"),
        _p("P4", "apartment", 2, desc="luxury tower unit", req=True),
    ])
    return k


def test_type_and_zone_filter_excludes_other_types(kb):
    out = kb.retrieve("a 3 bedroom apartment in colombo 7")
    assert "[P1]" in out
    assert "[P2]" not in out  # house excluded
    assert "[P3]" not in out  # wrong zone


def test_preamble_prepended(kb):
    out = kb.retrieve("apartment in colombo 7")
    assert out.startswith("PREAMBLE.")


def test_relaxation_zone_only_when_type_zone_empty(kb):
    # No commercial in zone 7 -> ladder retries zone 7 (any type)
    out = kb.retrieve("office space in colombo 7")
    assert "[P1]" in out or "[P2]" in out


def test_requested_zone_never_dropped(kb):
    # Nothing in zone 10 -> honest empty (only preamble), never leak other zones
    out = kb.retrieve("apartment in colombo 10")
    assert "[P1]" not in out and "[P3]" not in out


def test_on_request_listing_surfaces(kb):
    out = kb.retrieve("apartment in colombo 2")
    assert "[P4]" in out
    assert "on request" in out.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd "Flico Agent" && PYTHONPATH=. ../.venv/bin/pytest tests/test_engine.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'kb.engine'`.

- [ ] **Step 3: Write the implementation**

`Flico Agent/kb/engine.py`:
```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd "Flico Agent" && PYTHONPATH=. ../.venv/bin/pytest tests/test_engine.py -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add "Flico Agent/kb/engine.py" "Flico Agent/tests/test_engine.py"
git commit -m "feat(kb): add engine with relaxation ladder and static preamble"
```

---

## Task 7: Prose → structured rows migration

**Files:**
- Create: `Flico Agent/kb/migrate.py`
- Test: `Flico Agent/tests/test_migrate.py`

**Interfaces:**
- Consumes: `Property` from `kb.schema`.
- Produces:
  - `parse_prose(text: str) -> tuple[list[Property], list[str], str]` — returns (rows, skipped_paragraph_snippets, preamble). Listing paragraphs start with `"Rodrigo Realtors has"`; non-listing paragraphs (intro, AREAS COVERED, NEXT STEPS, lease-terms) are joined into the returned `preamble`. Each listing derives: `id` (from `(Ref: Pxx)`), `bedrooms`/`bathrooms`, `property_type`, `zone` (from `Colombo N`), `furnishing`, `floor_area_sqft`, `rent_amount`+`rent_period`+`rent_on_request`, `parking`, `deposit/advance/min_lease` when present, `key_features` (best-effort), `description` = the paragraph.
  - `migrate_file(prose_path: str) -> tuple[list[Property], str]` — reads a file, parses, prints a coverage report (`N paragraphs → M rows, K skipped`), returns (rows, preamble).

Parsing rules (derive with regex on each paragraph, lowercased copy for matching):
- `id`: `\(ref:\s*(p\d+)\)` → upper.
- `bedrooms`: `(\d+)\s*-\s*bedroom`; `bathrooms`: `(\d+)\s*-\s*bathroom`.
- `property_type`: `_classify_type` logic (commercial markers → apartment → house → land).
- `zone`: `colombo\s+(\d{1,2})`.
- `furnishing`: `semi-furnished`→`semi`, else `unfurnished`→`unfurnished`, else `furnished`→`furnished`, else None.
- `floor_area_sqft`: `floor area of ([\d,]+)\s*square feet` (strip commas).
- rent: if `available on request` or `rent on request` present → `rent_on_request=True`, amount/period None. Else `rent of ... \(rs\s*([\d,]+)` OR `rent of ... rs\s*([\d,]+)` → amount; period `per day` if "per day" in text else `per month` if "per month" present, else None.
- `parking`: `(one|two|three|\d+)\s+parking` → map word/number.
- `deposit_months`: `(\d+)-month deposit` or `(\d+) month.{0,6}deposit`; `advance_months`: `(\d+) months?['’]? advance`; `min_lease_months`: `minimum lease of (\d+)\s*year` × 12.

- [ ] **Step 1: Write the failing test**

`Flico Agent/tests/test_migrate.py`:
```python
from kb.migrate import parse_prose

SAMPLE = """RODRIGO REALTORS — RENTAL PROPERTY KNOWLEDGE BASE

Rodrigo Realtors is a trusted Sri Lankan real estate agency.

Rodrigo Realtors has a 3-bedroom, 3-bathroom furnished apartment for rent at Adamaly Place in Colombo 4 (Bambalapitiya), with a floor area of 1,300 square feet, at a rent of fifteen thousand rupees (Rs 15,000) per day. Lease terms are a 3-month deposit, 3 months' advance, and a minimum lease of 1 year, with one parking space. (Ref: P03)

Rodrigo Realtors has a 3-bedroom, 2-bathroom furnished apartment for rent at Havelock City in Colombo 5 (Havelock Town), with a floor area of 1,442 square feet. The monthly rent is available on request. (Ref: P02)
"""


def test_preamble_captures_non_listing():
    _, _, preamble = parse_prose(SAMPLE)
    assert "trusted Sri Lankan real estate agency" in preamble
    assert "Ref: P03" not in preamble


def test_two_rows_parsed():
    rows, skipped, _ = parse_prose(SAMPLE)
    assert {r.id for r in rows} == {"P03", "P02"}
    assert skipped == []


def test_per_day_period_preserved():
    rows, _, _ = parse_prose(SAMPLE)
    p03 = next(r for r in rows if r.id == "P03")
    assert p03.rent_period == "day"
    assert p03.rent_amount == 15000.0


def test_on_request_flagged():
    rows, _, _ = parse_prose(SAMPLE)
    p02 = next(r for r in rows if r.id == "P02")
    assert p02.rent_on_request is True
    assert p02.rent_amount is None


def test_zone_and_fields():
    rows, _, _ = parse_prose(SAMPLE)
    p03 = next(r for r in rows if r.id == "P03")
    assert p03.zone == 4
    assert p03.bedrooms == 3
    assert p03.furnishing == "furnished"
    assert p03.floor_area_sqft == 1300
    assert p03.parking == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd "Flico Agent" && PYTHONPATH=. ../.venv/bin/pytest tests/test_migrate.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'kb.migrate'`.

- [ ] **Step 3: Write the implementation**

`Flico Agent/kb/migrate.py`:
```python
import re
from typing import List, Optional, Tuple

from kb.query_parser import _COMMERCIAL_MARKERS
from kb.schema import Property

_WORD_NUM = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
             "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}
_TEXT_RENT = {"fifteen thousand": 15000.0}  # extend as needed; numeric Rs is primary


def _classify(low: str) -> str:
    if any(m in low for m in _COMMERCIAL_MARKERS):
        return "commercial"
    if "apartment" in low or "flat" in low:
        return "apartment"
    if "house" in low or "villa" in low or "bungalow" in low:
        return "house"
    if "bare land" in low or "land" in low or "plot" in low:
        return "land"
    return "apartment"


def _int(s: Optional[str]) -> Optional[int]:
    return int(s.replace(",", "")) if s else None


def _parse_listing(para: str) -> Optional[Property]:
    low = para.lower()
    ref = re.search(r"\(ref:\s*(p\d+)\)", low)
    if not ref:
        return None
    pid = ref.group(1).upper()

    beds = re.search(r"(\d+)\s*-\s*bedroom", low)
    baths = re.search(r"(\d+)\s*-\s*bathroom", low)
    zone = re.search(r"colombo\s+(\d{1,2})", low)
    area = re.search(r"colombo\s+\d{1,2}\s*\(([^)]+)\)", low)
    bld = re.search(r"for\s+(?:rent|sale)\s+at\s+([^,]+?)\s+in\s+colombo", para, re.I)
    sqft = re.search(r"floor area of ([\d,]+)\s*square feet", low)

    if "semi-furnished" in low or "semi furnished" in low:
        furnishing = "semi"
    elif "unfurnished" in low:
        furnishing = "unfurnished"
    elif "furnished" in low:
        furnishing = "furnished"
    else:
        furnishing = None

    on_request = "available on request" in low or "rent on request" in low
    rent_amount = None
    rent_period = None
    if not on_request:
        rs = re.search(r"rs\s*([\d,]+)", low)
        if rs:
            rent_amount = float(rs.group(1).replace(",", ""))
        else:
            for phrase, val in _TEXT_RENT.items():
                if phrase in low:
                    rent_amount = val
                    break
        if "per day" in low:
            rent_period = "day"
        elif "per month" in low or "monthly rent" in low:
            rent_period = "month"

    pk = re.search(r"(one|two|three|four|\d+)\s+(?:covered\s+)?parking", low)
    parking = None
    if pk:
        tok = pk.group(1)
        parking = _WORD_NUM.get(tok, None) if tok in _WORD_NUM else int(tok)

    dep = re.search(r"(\d+)\s*-?\s*month[s']*\s+deposit", low)
    adv = re.search(r"(\d+)\s+months?['’]?\s+advance", low)
    lease = re.search(r"minimum lease of (\d+)\s*year", low)

    return Property(
        id=pid, transaction="rent", property_type=_classify(low),
        zone=_int(zone.group(1)) if zone else None,
        area=area.group(1).title() if area else "",
        building=bld.group(1).strip() if bld else None,
        bedrooms=_int(beds.group(1)) if beds else None,
        bathrooms=float(baths.group(1)) if baths else None,
        rent_amount=rent_amount, rent_period=rent_period, rent_on_request=on_request,
        furnishing=furnishing,
        floor_area_sqft=_int(sqft.group(1)) if sqft else None,
        parking=parking,
        deposit_months=_int(dep.group(1)) if dep else None,
        advance_months=_int(adv.group(1)) if adv else None,
        min_lease_months=(int(lease.group(1)) * 12) if lease else None,
        key_features=[],
        description=para.strip(),
    )


def parse_prose(text: str) -> Tuple[List[Property], List[str], str]:
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    rows, skipped, preamble_parts = [], [], []
    for para in paras:
        if para.startswith("Rodrigo Realtors has"):
            prop = _parse_listing(para)
            if prop:
                rows.append(prop)
            else:
                skipped.append(para[:80])
        else:
            # Non-listing prose (intro, AREAS COVERED, NEXT STEPS, section headers)
            preamble_parts.append(para)
    return rows, skipped, "\n\n".join(preamble_parts)


def migrate_file(prose_path: str) -> Tuple[List[Property], str]:
    with open(prose_path, "r", encoding="utf-8") as fh:
        text = fh.read()
    paras = [p for p in re.split(r"\n\s*\n", text) if p.strip()]
    rows, skipped, preamble = parse_prose(text)
    print(f"[migrate] {len(paras)} paragraphs -> {len(rows)} rows, {len(skipped)} skipped")
    for s in skipped:
        print(f"[migrate]   SKIPPED: {s}...")
    return rows, preamble
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd "Flico Agent" && PYTHONPATH=. ../.venv/bin/pytest tests/test_migrate.py -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Run the real migration against the live prose and eyeball coverage**

Run:
```bash
cd "Flico Agent" && PYTHONPATH=. ../.venv/bin/python -c "from kb.migrate import migrate_file; rows, pre = migrate_file('knowledge_docs/flico_info.txt'); print('rows:', len(rows)); print('per-day rows:', [r.id for r in rows if r.rent_period=='day']); print('on-request:', sum(r.rent_on_request for r in rows))"
```
Expected: ~48 rows, `P03` in the per-day list, a nonzero on-request count, and zero (or a small, logged) skipped count. If many rows skip, fix the regex before continuing.

- [ ] **Step 6: Commit**

```bash
git add "Flico Agent/kb/migrate.py" "Flico Agent/tests/test_migrate.py"
git commit -m "feat(kb): add prose->rows migration with coverage report"
```

---

## Task 8: Move ChromaDB code and build the sqlite adapter

**Files:**
- Create: `Flico Agent/knowledge_base_chroma.py` (copy of current `knowledge_base.py`)
- Create: `Flico Agent/knowledge_base_sqlite.py`
- Test: (covered by Task 9's dispatcher test)

**Interfaces:**
- `knowledge_base_sqlite.py` produces the four contract functions backed by `RealEstateKB`:
  - `initialize_kb(docs_directory=DEFAULT_DOCS_DIRECTORY) -> bool`
  - `prewarm() -> None`
  - `reload_kb_from_content(content, filename="flico_info.txt") -> bool`
  - `retrieve_context(query, n_results=6, sticky=None) -> str`
- Uses a module-level singleton `RealEstateKB`. `initialize_kb` migrates the prose in `docs_directory`, loads rows, and stores the preamble on the engine. `reload_kb_from_content` writes the content to disk (parity with the chroma path) then re-migrates.

- [ ] **Step 1: Copy the current implementation verbatim**

Run:
```bash
cd "Flico Agent" && cp knowledge_base.py knowledge_base_chroma.py
```
(We overwrite `knowledge_base.py` with the dispatcher in Task 9; this preserves the ChromaDB code as the `chroma` backend.)

- [ ] **Step 2: Write the sqlite adapter**

`Flico Agent/knowledge_base_sqlite.py`:
```python
"""SQLite hybrid backend adapter — maps the KB contract onto kb.engine."""
import logging
import os
from typing import Optional

from kb.config import DEFAULT_DOCS_DIRECTORY, DB_PATH
from kb.engine import RealEstateKB
from kb.migrate import parse_prose

logger = logging.getLogger(__name__)
_engine: Optional[RealEstateKB] = None


def _get_engine() -> RealEstateKB:
    global _engine
    if _engine is None:
        _engine = RealEstateKB(db_path=DB_PATH)
    return _engine


def _load_from_text(text: str) -> bool:
    engine = _get_engine()
    rows, skipped, preamble = parse_prose(text)
    if skipped:
        logger.warning("KB migration skipped %d paragraph(s)", len(skipped))
    engine.preamble = preamble
    engine.add_properties(rows)
    logger.info("SQLite KB loaded %d listings", engine.get_count())
    return True


def initialize_kb(docs_directory: str = DEFAULT_DOCS_DIRECTORY) -> bool:
    path = os.path.join(docs_directory, "flico_info.txt")
    if not os.path.isfile(path):
        logger.warning("KB prose not found at %s", path)
        return False
    with open(path, "r", encoding="utf-8") as fh:
        return _load_from_text(fh.read())


def prewarm() -> None:
    _get_engine()  # constructs the model + DB connection


def reload_kb_from_content(content: str, filename: str = "flico_info.txt") -> bool:
    docs_dir = DEFAULT_DOCS_DIRECTORY
    os.makedirs(docs_dir, exist_ok=True)
    try:
        with open(os.path.join(docs_dir, filename), "w", encoding="utf-8") as fh:
            fh.write(content)
    except OSError as exc:
        logger.error("Failed to write KB file: %s", exc)
        return False
    return _load_from_text(content)


def retrieve_context(query: str, n_results: int = 6, sticky: Optional[dict] = None) -> str:
    try:
        return _get_engine().retrieve(query, n_results=n_results, sticky=sticky)
    except Exception as exc:  # never crash a live call on a KB fault
        logger.error("sqlite retrieve_context failed: %s", exc)
        return ""
```

- [ ] **Step 3: Commit**

```bash
git add "Flico Agent/knowledge_base_chroma.py" "Flico Agent/knowledge_base_sqlite.py"
git commit -m "feat(kb): retain ChromaDB backend and add sqlite adapter"
```

---

## Task 9: Dispatcher + interface parity test

**Files:**
- Modify: `Flico Agent/knowledge_base.py` (replace body with the dispatcher)
- Create: `Flico Agent/tests/test_dispatcher.py`
- Modify: `Flico Agent/.gitignore` (add `data/*.db`)

**Interfaces:**
- Consumes: `knowledge_base_chroma` and `knowledge_base_sqlite`.
- Produces the four contract functions, selected by `KB_BACKEND` (default `chroma`) plus re-exported `DEFAULT_DOCS_DIRECTORY`.

- [ ] **Step 1: Write the failing test**

`Flico Agent/tests/test_dispatcher.py`:
```python
import importlib
import os


def _reload(backend):
    os.environ["KB_BACKEND"] = backend
    import knowledge_base
    return importlib.reload(knowledge_base)


def test_default_is_chroma(monkeypatch):
    monkeypatch.delenv("KB_BACKEND", raising=False)
    import knowledge_base
    kb = importlib.reload(knowledge_base)
    assert kb.ACTIVE_BACKEND == "chroma"


def test_all_four_functions_present_both_backends():
    for backend in ("chroma", "sqlite"):
        kb = _reload(backend)
        for name in ("retrieve_context", "initialize_kb", "prewarm", "reload_kb_from_content"):
            assert callable(getattr(kb, name)), f"{name} missing in {backend}"


def test_sqlite_selected_when_flagged():
    kb = _reload("sqlite")
    assert kb.ACTIVE_BACKEND == "sqlite"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd "Flico Agent" && PYTHONPATH=. ../.venv/bin/pytest tests/test_dispatcher.py -v`
Expected: FAIL (`AttributeError: module 'knowledge_base' has no attribute 'ACTIVE_BACKEND'`).

- [ ] **Step 3: Replace `knowledge_base.py` with the dispatcher**

`Flico Agent/knowledge_base.py` (entire new contents):
```python
"""Knowledge-base dispatcher.

Routes the four KB contract functions to a backend selected by the KB_BACKEND
env var:
  * chroma (default) -> knowledge_base_chroma  (the original ChromaDB RAG)
  * sqlite           -> knowledge_base_sqlite  (SQLite + local-vector hybrid)

server.py imports from this module unchanged; switching backends is one env var.
"""
import logging
import os

logger = logging.getLogger(__name__)

ACTIVE_BACKEND = os.environ.get("KB_BACKEND", "chroma").strip().lower()

if ACTIVE_BACKEND == "sqlite":
    from knowledge_base_sqlite import (
        initialize_kb, prewarm, reload_kb_from_content, retrieve_context,
    )
    logger.info("KB backend: sqlite (local-vector hybrid)")
else:
    ACTIVE_BACKEND = "chroma"
    from knowledge_base_chroma import (
        initialize_kb, prewarm, reload_kb_from_content, retrieve_context,
    )
    logger.info("KB backend: chroma (default)")

DEFAULT_DOCS_DIRECTORY = "knowledge_docs"

__all__ = [
    "initialize_kb", "prewarm", "reload_kb_from_content", "retrieve_context",
    "DEFAULT_DOCS_DIRECTORY", "ACTIVE_BACKEND",
]
```

- [ ] **Step 4: Add the DB to gitignore**

Append to `Flico Agent/.gitignore` (create if missing):
```
data/*.db
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd "Flico Agent" && PYTHONPATH=. ../.venv/bin/pytest tests/test_dispatcher.py -v`
Expected: PASS (3 tests). (The `sqlite` reload requires `sentence-transformers`; it is installed in the venv.)

- [ ] **Step 6: Commit**

```bash
git add "Flico Agent/knowledge_base.py" "Flico Agent/tests/test_dispatcher.py" "Flico Agent/.gitignore"
git commit -m "feat(kb): add KB_BACKEND dispatcher, default chroma"
```

---

## Task 10: Full suite green + end-to-end smoke on real data

**Files:** none created; verification only.

- [ ] **Step 1: Run the whole KB test suite**

Run: `cd "Flico Agent" && PYTHONPATH=. ../.venv/bin/pytest tests/ -v`
Expected: all tests PASS.

- [ ] **Step 2: End-to-end smoke through the sqlite backend on the live prose**

Run:
```bash
cd "Flico Agent" && KB_BACKEND=sqlite PYTHONPATH=. ../.venv/bin/python -c "
import knowledge_base as kb
kb.initialize_kb('knowledge_docs')
sticky = {}
print('--- Q1: apartment in colombo 7 ---')
print(kb.retrieve_context('a 3 bedroom apartment in colombo 7', sticky=sticky)[:600])
print('--- Q2 (sticky): I would love colombo 5 ---')
print(kb.retrieve_context('actually I would love colombo 5', sticky=sticky)[:600])
print('sticky now:', sticky)
"
```
Expected: Q1 returns apartments in Colombo 7 with a preamble; Q2 (thanks to sticky) returns **apartments** in Colombo 5, not houses; `sticky == {'property_type': 'apartment', 'zone': 5}`.

- [ ] **Step 3: Confirm the default backend is still ChromaDB (inert merge)**

Run:
```bash
cd "Flico Agent" && PYTHONPATH=. ../.venv/bin/python -c "import knowledge_base as kb; print('ACTIVE_BACKEND =', kb.ACTIVE_BACKEND)"
```
Expected: `ACTIVE_BACKEND = chroma`.

- [ ] **Step 4: Final commit (if any verification tweaks were needed)**

```bash
git add -A "Flico Agent"
git commit -m "test(kb): full suite green and e2e smoke on live prose" || echo "nothing to commit"
```

**Do NOT push. Do NOT merge to main.** Hand back to the operator for the flag-flip cutover on the VPS.

---

## Self-Review

**Spec coverage:**
- Data source / prose→rows migration → Task 7 (+ real-data run in Step 5) and Task 8/10 wiring. ✓
- Interface contract (4 functions, exact signatures) → Task 8 adapter + Task 9 dispatcher + Task 9 parity test. ✓
- `retrieve_context` returns context string, not speech → Task 5 formatter + Task 6 engine. ✓
- Localized schema (zone, rent+period+on_request, furnishing tri-state, sale-capable) → Task 1. ✓
- Reconcile-on-load (stale-row fix) → Task 2 `reconcile` + Task 6 `add_properties`. ✓
- No silent embedding fallback → Task 3 raises. ✓
- LRU query cache → Task 3. ✓
- Ported voice lessons (STT maps, area→zone, spelled numbers, occupancy rule, sticky) → Task 4. ✓
- Relaxation ladder (zone never silently dropped) → Task 6. ✓
- rent_on_request surfaces + verbatim period → Task 5 formatter + Task 2 max_rent NULL-survival + Task 6 test. ✓
- Static preamble for non-listing prose → Task 6 engine + Task 7 preamble extraction. ✓
- Flag-guarded, default chroma, inert merge → Task 9 + Task 10 Step 3. ✓
- Tests offline (importorskip where model needed) → Tasks 1–9. ✓
- No push / branch safety → Global Constraints + Task 10 footer. ✓

**Placeholder scan:** No TBD/TODO; every code step shows complete code. ✓

**Type consistency:** `QueryFilters` fields (`transaction, property_type, zone, min_bedrooms, min_rent, max_rent`) are consistent across parser (Task 4), database (Task 2), and engine (Task 6). `Property` fields consistent across schema (1), database (2), formatter (5), migrate (7). `RealEstateKB.retrieve/add_properties/get_count` signatures consistent between Task 6 and the Task 8 adapter. The four contract function signatures are identical in Task 8, Task 9, and the spec. ✓
