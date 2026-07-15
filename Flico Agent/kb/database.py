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
                    "transaction" TEXT NOT NULL,
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
            conn.execute('CREATE INDEX IF NOT EXISTS idx_txn ON properties("transaction")')
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
        quoted_columns = ", ".join(f'"{c}"' if c == "transaction" else c for c in _COLUMNS)
        placeholders = ", ".join(["?"] * (len(_COLUMNS) + 1))
        sql = f"INSERT OR REPLACE INTO properties ({quoted_columns}, embedding) VALUES ({placeholders})"
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
            conditions.append('"transaction" = ?'); params.append(filters.transaction)
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
