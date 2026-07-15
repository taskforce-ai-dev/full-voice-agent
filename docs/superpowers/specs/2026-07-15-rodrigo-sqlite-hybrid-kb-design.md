# Rodrigo Realtors — SQLite + Local-Vector Hybrid Knowledge Base

**Status:** Approved design — ready for implementation plan
**Date:** 2026-07-15
**Agent:** Rodrigo Realtors (the `Flico Agent/` directory — rebranded from Flico electronics)
**Author:** Fable 5 (analysis + design). Build to be executed by Sonnet.

---

## Context

The Rodrigo Realtors voice agent currently grounds its answers in a ChromaDB RAG
knowledge base (`Flico Agent/knowledge_base.py`). That implementation has been
repeatedly patched to make a semantic vector store behave like a relational
filter: it regex-parses `property_type`, Colombo `zone`, and `bedrooms` **out of
prose paragraphs** (`_extract_metadata`), builds ChromaDB `where` filters from
caller utterances (`_parse_query_filters`), carries sticky cross-turn constraints,
and runs a hand-rolled fallback ladder when a filter returns nothing. All of that
is scaffolding compensating for the wrong tool doing relational work on top of
prose that was itself generated from structured data — a lossy round trip.

A **Relational-Semantic Hybrid** architecture (prototyped in `taskforce-ai`)
does the filtering properly: SQLite holds structured columns *and* the vector
embedding per listing; strict SQL narrows candidates first, then in-memory NumPy
cosine ranks only within that candidate set. This is more accurate (strict
adherence to price/beds/zone constraints that voice callers expect) and
operationally simpler (a single committable `.db` file instead of a gitignored
ChromaDB directory that has already caused stale-chunk incidents across the fleet).

This spec adapts that architecture to (a) the Sri Lanka **rental** portfolio and
(b) the **trilingual** (English / Tamil / Sinhala) voice agent, and lands it
behind a feature flag so the live, auto-deploying agent keeps a one-env-var
rollback to ChromaDB.

### Goals

- More accurate constraint handling (rent range, bedrooms, Colombo zone, property type).
- Preserve the existing `retrieve_context()` interface so `server.py` is untouched.
- Preserve trilingual behavior: the KB returns a **context string**; the LLM speaks the caller's language.
- Safe rollout: flag-guarded, ChromaDB retained as instant fallback.

### Non-goals (YAGNI)

- No LLM-based query parsing (adds a round-trip to the <1.5s voice budget).
- No ANN / approximate index (brute-force cosine is ~0.08 ms over 1,000 candidates).
- No admin-portal rebuild in this project. Keep `/kb-reload` working via a
  prose-ingest compatibility path; a structured-data portal is a **separate future project**.

---

## Data source of truth

**The structured `_rodrigo_listings.json` is stale** — it is the superseded
*for-sale* portfolio (44 records, all `transaction: sale`, `price_lkr` as free
strings like `"Rs. 44,000,000 (Negotiable)"`, District-level locations, no
Colombo zones). The **live** data exists only in the deployed **rental** prose
(`knowledge_docs/flico_info.txt` — Colombo 1–10 zones, rent per month/day,
rent-on-request, furnishing states).

Therefore the seed is a **one-time migration parsing the current rental prose
into structured rows**, producing a fresh structured dataset that becomes the new
source of truth (regenerated `data/rodrigo_listings.json` + the SQLite DB). The
prose is regularly structured and parses deterministically:

```
Rodrigo Realtors has a {beds}-bedroom, {baths}-bathroom {furnishing} {type}
for rent at {building} in Colombo {zone} ({area}), with a floor area of
{sqft} square feet[, at a monthly rent of {rent} per {period}]. [Lease terms …]
[Features …] (Ref: {id})
```

Rows that cannot be fully parsed are logged and skipped (never silently dropped
without a count), and the migration prints a coverage report (`N prose paragraphs
→ M rows`) so we can confirm nothing is lost against the ~48 known listings.

---

## Interface contract (the safety mechanism)

`server.py` imports exactly four names and calls them at fixed points:

```python
from knowledge_base import retrieve_context, initialize_kb, prewarm, reload_kb_from_content
#  initialize_kb(KB_DOCS_DIRECTORY)               @ startup   (server.py:682)
#  prewarm()                                       @ startup   (server.py:689)
#  reload_kb_from_content(content, filename)       @ admin /kb-reload, in executor (server.py:855)
#  retrieve_context(text, sticky=self.sticky_filters)  @ every turn — TA/SI (1573) and EN (2453)
```

`knowledge_base.py` becomes a **thin dispatcher** preserving these exact
signatures and routing by the `KB_BACKEND` env var:

- `KB_BACKEND=chroma` (**default**) → existing implementation, moved verbatim to `knowledge_base_chroma.py`.
- `KB_BACKEND=sqlite` → new `kb/` package (this spec).

**`server.py` is not modified.** Rollback is flipping one env var on the VPS.

`retrieve_context()` returns a **context string** (top-N matching listings as a
compact, self-contained block), never caller-facing speech. The LLM renders the
spoken reply in the caller's language, exactly as today. The `taskforce-ai`
`VoiceFormatter` English f-strings are adapted into that structured context block,
**not** used for output.

### Signatures the dispatcher and sqlite backend must implement

```python
def initialize_kb(docs_directory: str = DEFAULT_DOCS_DIRECTORY) -> bool
def prewarm() -> None
def reload_kb_from_content(content: str, filename: str = "flico_info.txt") -> bool
def retrieve_context(query: str, n_results: int = 6, sticky: Optional[dict] = None) -> str
```

---

## Localized schema (rental-first, sale-capable)

SQLite table `properties`:

| Column | Type | Indexed | Notes |
|---|---|---|---|
| `id` | TEXT PK | ✓ | ref code, e.g. `P15` |
| `transaction` | TEXT | ✓ | `rent` \| `sale` |
| `property_type` | TEXT | ✓ | `apartment` \| `house` \| `commercial` \| `land` |
| `zone` | INTEGER NULL | ✓ | Colombo 1–10, first-class column (not regex'd at query time) |
| `area` | TEXT | | e.g. "Cinnamon Gardens" |
| `building` | TEXT NULL | | e.g. "The Grand", "One Galle Face" |
| `bedrooms` | INTEGER NULL | ✓ | |
| `bathrooms` | REAL NULL | ✓ | |
| `rent_amount` | REAL NULL | ✓ | numeric; NULL when on-request |
| `rent_period` | TEXT NULL | | `month` \| `day` — **never assumed**; carried explicitly (P03 is per-day) |
| `rent_on_request` | INTEGER (bool) | | ~half the premium listings |
| `sale_price` | REAL NULL | ✓ | for `transaction=sale` rows |
| `furnishing` | TEXT NULL | | `furnished` \| `semi` \| `unfurnished` (tri-state) |
| `floor_area_sqft` | INTEGER NULL | | |
| `parking` | INTEGER NULL | | number of spaces |
| `deposit_months` | INTEGER NULL | | caller-asked fact |
| `advance_months` | INTEGER NULL | | caller-asked fact |
| `min_lease_months` | INTEGER NULL | | caller-asked fact |
| `key_features` | TEXT (JSON) | | folded into embedded text |
| `description` | TEXT | | self-contained sentence; the embedded text |
| `embedding` | BLOB | | 384-dim float32, MiniLM (`all-MiniLM-L6-v2`) |

Indices: `transaction`, `property_type`, `zone`, `bedrooms`, `rent_amount`,
`sale_price`, and a composite `(property_type, zone)` for the dominant query shape.

Embeddings are unit-normalized at write time so cosine similarity is a plain dot
product at query time.

---

## Data flow (per turn)

```
utterance
  → rule parser  → filters {transaction, type, zone, beds, rent range} + semantic query
                   (sticky carry-over merges this turn with remembered constraints)
  → SQL filter   → candidate rows (+ embedding blobs)
  → embed query  → MiniLM vector (LRU-cached; skip embed on repeat utterance)
  → NumPy cosine → rank candidates by dot product
  → relaxation   → if empty, climb the ladder (below)
  → context      → top-N as a compact self-contained string
  → LLM          → speaks it in the caller's language (unchanged)
```

### Ported from the current Flico implementation (reused, not rebuilt)

- STT mis-hearing maps: `Columbo/Columbus/Colombus → Colombo`, `havoc/haverlock → Havelock`.
- Area→zone lookup (callers say "Kollupitiya", not "Colombo 3") and spelled-out numbers ("colombo five").
- **Occupancy-vs-bedrooms rule**: "we're 4 people" is occupancy, **never** `bedrooms = 4`.
- **Sticky cross-turn filters**: a constraint stated this turn overrides; otherwise inherit the remembered one (so "I'd love Colombo 5" keeps the "apartment" said two turns ago). Owned by `server.py` via the `sticky` dict — signature preserved.
- **Relaxation ladder**: `type + zone` empty → retry `zone`-only; a *requested* zone is never silently dropped (honesty over recall); a type-only miss drops the filter.

### New in this project

- **Rent-range extraction** with correct period handling ("under 500k a month", "around Rs 300,000") → `rent_amount` bounds + `rent_period`.
- `rent_on_request` listings surface when the type/zone matches even without a numeric rent, and the context block marks them "rent on request — consultant follows up".

---

## Module layout (new `kb/` package under `Flico Agent/`)

Small, single-purpose units with defined interfaces:

- `kb/schema.py` — Pydantic `Property` (localized fields above) + `QueryFilters`.
- `kb/config.py` — DB path, model name (`all-MiniLM-L6-v2`), dimension (384).
- `kb/database.py` — SQLite: table/index DDL, batch upsert, **reconcile-on-load**
  (delete rows whose ids are absent from the incoming set — fixes the stale-row
  bug the taskforce-ai prototype shares with the old ChromaDB agents), dynamic
  parameterized `WHERE`, connections closed via `contextlib.closing`.
- `kb/embeddings.py` — MiniLM wrapper, unit-normalized output, **LRU query
  cache**. The zero-dependency hash fallback is **disabled in production**: if
  `sentence-transformers` is unavailable it raises rather than silently degrading
  (the prototype's silent fallback produced a misleading benchmark — see design notes).
- `kb/query_parser.py` — rule parser porting the Flico voice lessons above +
  rent-range extraction. The rules are English-oriented (property terms, "Colombo N",
  area names), which **matches the current agent's behavior exactly** — the existing
  `_parse_query_filters` is also English regex. On the Tamil/Sinhala paths, structured
  filtering is best-effort (callers commonly use English property terms and "Colombo"
  + a digit), and the semantic vector layer still contributes; this is neither improved
  nor regressed here. No LLM parse. (A language-aware parser is possible future work.)
- `kb/formatter.py` — builds the compact **context string** for the LLM (not speech).
- `kb/engine.py` — `RealEstateKB` coordinator: parse → filter → rank → relax → format.
- `kb/migrate.py` — one-time prose → structured rows (+ regenerated JSON), with coverage report.

Dispatcher + backends at the agent root:

- `knowledge_base.py` — dispatcher exposing the four contract functions, routing by `KB_BACKEND`.
- `knowledge_base_chroma.py` — the current implementation, moved verbatim (the `chroma` default).
- `knowledge_base_sqlite.py` — thin adapter mapping the four contract functions onto `kb/engine.py`.

Non-listing prose (agency intro, lease conventions, areas-covered, next-steps)
has no structured home, so `retrieve_context` also returns a small **static
preamble** loaded from those non-listing paragraphs, ensuring "how does viewing
work?" still gets answered.

---

## Error handling

- Missing `sentence-transformers` in `KB_BACKEND=sqlite` → raise at init (no silent hash fallback).
- Empty/again-empty SQL after the relaxation ladder → return the honest empty
  context; the LLM already handles "nothing in that zone" gracefully.
- Migration parse failures → skipped, counted, and reported; never a silent drop.
- `reload_kb_from_content` (admin `/kb-reload`) still accepts prose: it parses the
  prose to rows and rebuilds the DB, so the existing portal keeps working.
- Any uncaught backend error in the dispatcher logs and returns `""` (same
  degradation contract as today), so a KB fault never crashes a live call.

---

## Testing

Runnable offline once MiniLM is cached; no network, no telephony:

1. **Parser suite** — fixtures for STT mis-hearings, area→zone, spelled-out
   numbers, occupancy-vs-bedrooms, rent-range + period, and sticky carry-over
   (this turn overrides / inherits). These encode the regressions the Flico code
   already fixed.
2. **Migration test** — prose → rows round-trip on `flico_info.txt`; asserts row
   count matches known listings and that period-sensitive rows (P03 per-day) carry
   the right `rent_period`.
3. **Engine test** — SQL filter correctness (a "2-bedroom apartment in Colombo 7"
   never returns a house or another zone), relaxation-ladder behavior, and
   rent-on-request surfacing.
4. **Interface test** — the dispatcher exposes all four contract functions with
   the documented signatures under both `KB_BACKEND` values.

---

## Rollout & deploy safety

- All work on branch `feature/rodrigo-sqlite-hybrid-kb`. **No push** — pushing
  `main` auto-deploys the changed agent to the live VPS.
- `KB_BACKEND` defaults to `chroma`; a merge is **inert** until the flag is flipped
  to `sqlite` in `/opt/flico/.env` on the VPS.
- Cutover procedure (post-merge, human-gated): set `KB_BACKEND=sqlite`, restart the
  Flico container, verify against a set of real call-transcript queries, and keep
  `chroma` as the one-env-var rollback until confidence is established.

---

## Design notes (evidence behind the decisions)

- Honest benchmark (real MiniLM, 1,000 listings, this dev rig): warm retrieval
  ~25–50 ms/turn (median ~44), gated by **query embedding (~18 ms)** then SQL
  deserialize, not by cosine (~0.08 ms/1k). Brute-force scales to thousands; the
  thing to watch at 10k+ is deserializing all blobs on an unfiltered query →
  cache the embedding matrix in memory if it ever bites. The prototype's original
  "sub-7 ms" figure came from a **silent hash-embedding fallback** (the model was
  never installed), which is why production disables that fallback.
- LRU query-embedding cache is ported from the current Flico code
  (`_cached_embed_query`) — it removes the ~18 ms on repeated utterances.
