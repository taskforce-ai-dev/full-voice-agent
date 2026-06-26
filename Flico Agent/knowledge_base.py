"""
knowledge_base.py â€” Optimized RAG / Knowledge-Base module for the Flico Voice Agent.

Provides semantic search over product documentation using ChromaDB for vector
storage and sentence-transformers for embeddings.

Key optimizations over the baseline implementation:
  * prewarm()            â€” eagerly loads ChromaDB + embedding model to cut cold-start latency.
  * _cached_embed_query  â€” LRU-cached query embeddings so repeated questions are instant.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from functools import lru_cache
from typing import Dict, List, Optional

# ---------------------------------------------------------------------------
# Graceful / optional imports
# ---------------------------------------------------------------------------
try:
    import chromadb
    from chromadb.config import Settings
    CHROMADB_AVAILABLE = True
except ImportError:
    chromadb = None          # type: ignore[assignment]
    Settings = None          # type: ignore[assignment,misc]
    CHROMADB_AVAILABLE = False

try:
    from sentence_transformers import SentenceTransformer
    SENTENCE_TRANSFORMERS_AVAILABLE = True
except ImportError:
    SentenceTransformer = None  # type: ignore[assignment,misc]
    SENTENCE_TRANSFORMERS_AVAILABLE = False

try:
    import PyPDF2
    PYPDF2_AVAILABLE = True
except ImportError:
    PyPDF2 = None  # type: ignore[assignment]
    PYPDF2_AVAILABLE = False

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
EMBEDDING_MODEL_NAME: str = "all-MiniLM-L6-v2"
COLLECTION_NAME: str = "flico_kb"
PERSIST_DIRECTORY: str = "./chroma_db"
SUPPORTED_EXTENSIONS: set = {".txt", ".md", ".pdf", ".json"}
DEFAULT_DOCS_DIRECTORY: str = "knowledge_docs"

# ---------------------------------------------------------------------------
# Lazy singletons
# ---------------------------------------------------------------------------
_chroma_client = None
_embedding_model = None


def _get_chroma_client():
    """Return (or create) a persistent ChromaDB client â€” lazy singleton."""
    global _chroma_client
    if _chroma_client is not None:
        return _chroma_client

    if not CHROMADB_AVAILABLE:
        logger.warning("chromadb is not installed â€” knowledge base disabled.")
        return None

    os.makedirs(PERSIST_DIRECTORY, exist_ok=True)

    # Try the modern API first (chromadb >= 0.4.0)
    try:
        _chroma_client = chromadb.PersistentClient(path=PERSIST_DIRECTORY)
        logger.info(
            "ChromaDB PersistentClient initialised (path=%s).", PERSIST_DIRECTORY
        )
        return _chroma_client
    except (AttributeError, TypeError):
        # PersistentClient doesn't exist in very old versions â€” fall through
        pass
    except Exception as exc:
        logger.error("Failed to create ChromaDB PersistentClient: %s", exc)

    # Fallback to legacy API (chromadb < 0.4.0)
    try:
        _chroma_client = chromadb.Client(
            Settings(
                chroma_db_impl="duckdb+parquet",
                persist_directory=PERSIST_DIRECTORY,
                anonymized_telemetry=False,
            )
        )
        logger.info("ChromaDB legacy client initialised (persist=%s).", PERSIST_DIRECTORY)
    except Exception as exc:
        logger.error("Failed to create ChromaDB client: %s", exc)
        _chroma_client = None

    return _chroma_client


def _get_embedding_model():
    """Return (or create) the SentenceTransformer embedding model â€” lazy singleton."""
    global _embedding_model
    if _embedding_model is not None:
        return _embedding_model

    if not SENTENCE_TRANSFORMERS_AVAILABLE:
        logger.warning(
            "sentence-transformers is not installed â€” embeddings unavailable."
        )
        return None

    try:
        _embedding_model = SentenceTransformer(EMBEDDING_MODEL_NAME)
        logger.info("Loaded embedding model: %s", EMBEDDING_MODEL_NAME)
    except Exception as exc:
        logger.error("Failed to load embedding model %s: %s", EMBEDDING_MODEL_NAME, exc)
        _embedding_model = None

    return _embedding_model


# ---------------------------------------------------------------------------
# Embedding helpers
# ---------------------------------------------------------------------------

def _embed_texts(texts: List[str]) -> List[List[float]]:
    """Embed a list of strings. Returns a list of float-lists (one per input)."""
    model = _get_embedding_model()
    if model is None:
        logger.warning("Embedding model unavailable â€” returning empty embeddings.")
        return [[] for _ in texts]

    try:
        embeddings = model.encode(texts, show_progress_bar=False)
        return [emb.tolist() for emb in embeddings]
    except Exception as exc:
        logger.error("Embedding failed: %s", exc)
        return [[] for _ in texts]


@lru_cache(maxsize=100)
def _cached_embed_query(query: str) -> tuple:
    """Cache query embeddings. Returns a tuple (hashable) for LRU caching.

    Using a tuple instead of a list makes the return value hashable so
    ``functools.lru_cache`` can store it.  Convert back to a list with
    ``list(...)`` before handing it to ChromaDB.
    """
    model = _get_embedding_model()
    if model is None:
        return ()

    try:
        embedding = model.encode([query], show_progress_bar=False)
        return tuple(embedding[0].tolist())
    except Exception as exc:
        logger.error("Cached query embedding failed: %s", exc)
        return ()


# ---------------------------------------------------------------------------
# ID generation
# ---------------------------------------------------------------------------

def _stable_id(text: str, index: int) -> str:
    """Generate a deterministic, SHA-256-based chunk ID.

    Using a stable ID means re-indexing the same content is idempotent â€”
    ChromaDB will simply upsert rather than duplicate.
    """
    hash_input = f"{text}::{index}"
    return hashlib.sha256(hash_input.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Text chunking
# ---------------------------------------------------------------------------

def _split_oversized(para: str, chunk_size: int) -> List[str]:
    """Split a single over-long paragraph into <= chunk_size pieces.

    Forward-only (no backward overlap), so it cannot loop on a boundary the
    way the old windowed splitter did. Prefers sentence boundaries, then word
    boundaries, then a hard cut.
    """
    out: List[str] = []
    start = 0
    n = len(para)
    while start < n:
        end = start + chunk_size
        if end >= n:
            out.append(para[start:].strip())
            break
        window = para[start:end]
        split_pos = -1
        for pattern in (". ", "? ", "! "):
            pos = window.rfind(pattern)
            if pos != -1:
                split_pos = pos + 1  # include the punctuation
                break
        if split_pos == -1:
            split_pos = window.rfind(" ")
        if split_pos == -1:
            split_pos = chunk_size  # hard cut
        out.append(para[start:start + split_pos].strip())
        start = start + split_pos  # advance forward only
    return [c for c in out if c]


def chunk_text(
    text: str,
    chunk_size: int = 500,
    overlap: int = 50,  # kept for signature compatibility; no longer used
) -> List[str]:
    """Split *text* into chunks on paragraph boundaries.

    Paragraphs (separated by blank lines) are the atomic unit: each is kept
    whole so a self-contained record — e.g. one property listing — stays in a
    single chunk. Small consecutive paragraphs are packed together up to
    *chunk_size*; a paragraph longer than *chunk_size* is split on sentence /
    word boundaries via :func:`_split_oversized`.

    This replaces the previous overlapping-window splitter, which spiralled
    into hundreds of tiny fragments on paragraph-separated content (the
    backward `overlap` repeatedly re-found the same `\\n\\n` boundary).
    """
    if not text or not text.strip():
        return []

    text = text.strip()
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]

    # One paragraph == one chunk. A self-contained record (e.g. a single
    # property listing) stays whole and is never merged with a neighbour, so
    # per-chunk metadata (type / zone) maps to exactly one listing. Only an
    # over-long paragraph is split, on sentence / word boundaries.
    chunks: List[str] = []
    for para in paragraphs:
        if len(para) > chunk_size:
            chunks.extend(_split_oversized(para, chunk_size))
        else:
            chunks.append(para)

    return chunks


# ---------------------------------------------------------------------------
# File reading
# ---------------------------------------------------------------------------

def _read_file(path: str) -> Optional[str]:
    """Read the textual content of a file.

    Supports:
      * .txt / .md  â€” read as UTF-8 text (with latin-1 fallback).
      * .pdf        â€” extract text via PyPDF2 (if installed).
      * .json       â€” parse and re-serialize as indented JSON text.

    Returns ``None`` when the file cannot be read.
    """
    ext = os.path.splitext(path)[1].lower()

    if ext in (".txt", ".md"):
        for encoding in ("utf-8", "latin-1"):
            try:
                with open(path, "r", encoding=encoding) as fh:
                    return fh.read()
            except UnicodeDecodeError:
                continue
            except Exception as exc:
                logger.error("Could not read %s: %s", path, exc)
                return None
        logger.error("Failed to decode %s with any supported encoding.", path)
        return None

    if ext == ".pdf":
        if not PYPDF2_AVAILABLE:
            logger.warning("PyPDF2 not installed â€” cannot read %s", path)
            return None
        try:
            with open(path, "rb") as fh:
                reader = PyPDF2.PdfReader(fh)
                pages = [page.extract_text() or "" for page in reader.pages]
                return "\n".join(pages)
        except Exception as exc:
            logger.error("Could not read PDF %s: %s", path, exc)
            return None

    if ext == ".json":
        for encoding in ("utf-8", "latin-1"):
            try:
                with open(path, "r", encoding=encoding) as fh:
                    data = json.load(fh)
                    return json.dumps(data, indent=2, ensure_ascii=False)
            except UnicodeDecodeError:
                continue
            except json.JSONDecodeError as exc:
                logger.error("Invalid JSON in %s: %s", path, exc)
                return None
            except Exception as exc:
                logger.error("Could not read %s: %s", path, exc)
                return None
        logger.error("Failed to decode %s with any supported encoding.", path)
        return None

    logger.warning("Unsupported file extension '%s' for %s", ext, path)
    return None


# ---------------------------------------------------------------------------
# Knowledge-base initialisation
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Property type / zone tagging (for precise metadata-filtered retrieval)
# ---------------------------------------------------------------------------

# Colombo area names -> postal zone, so callers can say the neighbourhood.
_AREA_TO_ZONE = {
    "fort": "1", "galle face": "1",
    "slave island": "2", "union place": "2",
    "kollupitiya": "3", "kollupitya": "3", "colpetty": "3",
    "bambalapitiya": "4",
    "havelock town": "5", "havelock city": "5", "havelock": "5", "narahenpita": "5",
    # Common STT mis-hearings of "Havelock" (Havelock Town/City).
    "havoc town": "5", "havoc city": "5", "havoc": "5", "haverlock": "5",
    "wellawatte": "6", "wellawatta": "6",
    "cinnamon gardens": "7",
    "borella": "8",
    "maradana": "10",
}

_NUM_WORDS = {
    "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
    "six": "6", "seven": "7", "eight": "8", "nine": "9", "ten": "10",
}

# Markers that mean a non-residential listing. Checked before house/apartment
# so "house-type office space" classifies as commercial, not house.
_COMMERCIAL_MARKERS = (
    "office space", "commercial building", "commercial property",
    "commercial house", "commercial space", "commercial unit",
)


def _classify_type(text_lower: str) -> str:
    if any(m in text_lower for m in _COMMERCIAL_MARKERS):
        return "commercial"
    if "apartment" in text_lower:
        return "apartment"
    if "house" in text_lower or "villa" in text_lower or "bungalow" in text_lower:
        return "house"
    return "info"


def _extract_metadata(text: str) -> Dict[str, object]:
    """Derive ``{property_type, zone[, bedrooms]}`` for a single chunk.

    Only chunks that are an actual listing (they start with
    ``"Rodrigo Realtors has"``) get a real property type and zone. Descriptive
    / intro chunks are tagged ``property_type="info"`` with an empty zone so
    they never satisfy a type/zone filter.
    """
    body = text.strip()
    meta: Dict[str, object] = {"property_type": "info", "zone": ""}
    if not body.startswith("Rodrigo Realtors has"):
        return meta
    low = body.lower()
    meta["property_type"] = _classify_type(low)
    m = re.search(r"colombo\s+(\d{1,2})", low)
    if m:
        meta["zone"] = m.group(1)
    bm = re.search(r"(\d+)\s*-\s*bedroom", low)
    if bm:
        meta["bedrooms"] = int(bm.group(1))
    rm = re.search(r"\(ref:\s*(p\d+)\)", low)
    if rm:
        meta["property_id"] = rm.group(1).upper()
    return meta


def _parse_query_filters(query: str):
    """Parse a caller utterance into ``(property_type | None, zone | None)``.

    Used to build a ChromaDB ``where`` filter so retrieval is constrained to
    the property type and Colombo zone the caller actually asked about.
    """
    low = query.lower()

    ptype = None
    if any(w in low for w in ("office", "commercial", "shop", "retail", "warehouse")):
        ptype = "commercial"
    elif "apartment" in low or "flat" in low:
        ptype = "apartment"
    elif "house" in low or "villa" in low or "bungalow" in low:
        ptype = "house"

    zone = None
    # Tolerate common speech-to-text mis-spellings of "Colombo" -- the telephony
    # STT frequently returns "Columbo", "Columbus" or "Colombus". Match the digit
    # form first ("colombo 5"), then the spelled-out form ("colombo five").
    m = re.search(r"col[ou]mb[ou]s?[\s\-]*(\d{1,2})", low)
    if m:
        zone = m.group(1)
    else:
        m2 = re.search(r"col[ou]mb[ou]s?[\s\-]+([a-z]+)", low)
        if m2 and m2.group(1) in _NUM_WORDS:
            zone = _NUM_WORDS[m2.group(1)]
        else:
            for area, z in _AREA_TO_ZONE.items():
                if re.search(r"\b" + re.escape(area) + r"\b", low):
                    zone = z
                    break

    return ptype, zone


def initialize_kb(docs_directory: str = DEFAULT_DOCS_DIRECTORY) -> bool:
    """Scan *docs_directory*, chunk every supported file, embed, and upsert
    into ChromaDB.

    The operation is **idempotent** â€” chunks are assigned stable IDs so
    re-running will not create duplicates.

    Returns ``True`` on success, ``False`` if the KB could not be initialised.
    """
    client = _get_chroma_client()
    if client is None:
        logger.error("ChromaDB client unavailable â€” cannot initialise KB.")
        return False

    if not os.path.isdir(docs_directory):
        logger.warning(
            "Docs directory '%s' does not exist. Creating empty KB.", docs_directory
        )
        os.makedirs(docs_directory, exist_ok=True)
        return True

    try:
        collection = client.get_or_create_collection(name=COLLECTION_NAME)
    except Exception as exc:
        logger.error("Could not get/create ChromaDB collection: %s", exc)
        return False

    total_chunks = 0

    for filename in sorted(os.listdir(docs_directory)):
        ext = os.path.splitext(filename)[1].lower()
        if ext not in SUPPORTED_EXTENSIONS:
            continue

        filepath = os.path.join(docs_directory, filename)
        content = _read_file(filepath)
        if not content:
            logger.warning("Skipping empty / unreadable file: %s", filepath)
            continue

        chunks = chunk_text(content)
        if not chunks:
            continue

        ids = [_stable_id(filename + chunk, i) for i, chunk in enumerate(chunks)]
        embeddings = _embed_texts(chunks)
        metadatas = [
            {"source": filename, "chunk_index": i, **_extract_metadata(chunk)}
            for i, chunk in enumerate(chunks)
        ]

        # Only pass embeddings if they were successfully generated
        has_embeddings = embeddings and all(len(e) > 0 for e in embeddings)

        try:
            collection.delete(where={"source": filename})
            logger.info("Cleared stale chunks for %s", filename)
        except Exception as del_exc:
            logger.warning("Could not clear stale chunks for %s: %s", filename, del_exc)

        try:
            upsert_kwargs = dict(ids=ids, documents=chunks, metadatas=metadatas)
            if has_embeddings:
                upsert_kwargs["embeddings"] = embeddings
            collection.upsert(**upsert_kwargs)
            total_chunks += len(chunks)
            logger.info("Indexed %d chunks from %s", len(chunks), filename)
        except Exception as exc:
            logger.error("Failed to upsert chunks from %s: %s", filename, exc)

    logger.info(
        "KB initialisation complete â€” %d total chunks in collection '%s'.",
        total_chunks,
        COLLECTION_NAME,
    )
    return True


# ---------------------------------------------------------------------------
# Semantic retrieval
# ---------------------------------------------------------------------------

def retrieve_context(query: str, n_results: int = 6, sticky: Optional[Dict[str, object]] = None) -> str:
    """Run a semantic search against the knowledge base.

    Uses the LRU-cached ``_cached_embed_query`` so that repeated identical
    queries skip the embedding step entirely.

    Returns a formatted string of the top *n_results* chunks, or an empty
    string when the KB is unavailable / empty.
    """
    client = _get_chroma_client()
    if client is None:
        return ""

    try:
        collection = client.get_or_create_collection(name=COLLECTION_NAME)
    except Exception as exc:
        logger.error("Could not access collection '%s': %s", COLLECTION_NAME, exc)
        return ""

    # Build a metadata filter from the caller's intent (type + Colombo zone)
    # so retrieval is constrained to matching listings instead of relying on
    # embedding similarity alone (which mixes offices into apartment queries).
    #
    # STICKY CONSTRAINTS: a value the caller states THIS turn wins; otherwise we
    # inherit the value remembered from earlier turns via *sticky*. This stops
    # retrieval from losing "apartment" when a later utterance only names a zone
    # (e.g. "I'd love Colombo 5"), which previously surfaced a house. Occupancy
    # ("4 people") is deliberately NOT turned into a bedrooms filter -- every
    # apartment in the KB is 3-4 bed, so that is handled in the system prompt.
    turn_ptype, turn_zone = _parse_query_filters(query)
    if sticky is not None:
        ptype = turn_ptype if turn_ptype else sticky.get("property_type")
        zone = turn_zone if turn_zone else sticky.get("zone")
        if ptype:
            sticky["property_type"] = ptype
        if zone:
            sticky["zone"] = zone
    else:
        ptype, zone = turn_ptype, turn_zone

    # Keep the embedding query on-topic even when THIS utterance omitted the
    # type/zone words, by appending any constraint carried over from an earlier
    # turn (only when it was not restated this turn).
    search_query = query
    carried = []
    if ptype and not turn_ptype:
        carried.append(str(ptype))
    if zone and not turn_zone:
        carried.append(f"colombo {zone}")
    if carried:
        search_query = query + " " + " ".join(carried)

    # Use the cached embedding path (embed exactly once, on the augmented text).
    query_embedding = _cached_embed_query(search_query)
    if not query_embedding:
        logger.warning("Query embedding failed for: %s", search_query)
        return ""

    conds = []
    if ptype:
        conds.append({"property_type": ptype})
    if zone:
        conds.append({"zone": zone})
    where = {"$and": conds} if len(conds) == 2 else (conds[0] if conds else None)

    def _run(where_clause):
        return collection.query(
            query_embeddings=[list(query_embedding)],
            n_results=n_results,
            where=where_clause,
        )

    def _empty(res):
        return (
            not res
            or not res.get("documents")
            or not res["documents"]
            or not res["documents"][0]
        )

    try:
        results = _run(where)
        # Graceful degradation. If the precise filter found nothing:
        #  - type+zone empty -> retry zone-only (other types in that zone).
        #  - if a zone was requested and still empty, return empty (we honestly
        #    have nothing in that zone) to preserve zone precision.
        #  - if only a type was requested and empty, drop the filter.
        if where is not None and _empty(results):
            if ptype and zone:
                results = _run({"zone": zone})
            if _empty(results) and zone is None and ptype:
                results = _run(None)
    except Exception as exc:
        logger.error("ChromaDB query failed: %s", exc)
        return ""

    if not results or not results.get("documents"):
        return ""

    documents = results["documents"][0]
    metadatas = results.get("metadatas", [[]])[0]

    if not documents:
        return ""

    formatted_parts: List[str] = []
    for i, doc in enumerate(documents):
        source = (
            metadatas[i].get("source", "unknown") if i < len(metadatas) else "unknown"
        )
        formatted_parts.append(f"[Source: {source}]\n{doc}")

    return "\n\n---\n\n".join(formatted_parts)


# ---------------------------------------------------------------------------
# Ad-hoc document insertion
# ---------------------------------------------------------------------------

def add_document(
    text: str,
    metadata: Optional[Dict[str, str]] = None,
) -> bool:
    """Add a single document (or chunk) to the knowledge base.

    *metadata* is an optional dict of key-value pairs stored alongside the
    vector (e.g. ``{"source": "front-desk-notes"}``).

    Returns ``True`` on success.
    """
    client = _get_chroma_client()
    if client is None:
        logger.error("ChromaDB client unavailable â€” cannot add document.")
        return False

    try:
        collection = client.get_or_create_collection(name=COLLECTION_NAME)
    except Exception as exc:
        logger.error("Could not get/create collection: %s", exc)
        return False

    chunks = chunk_text(text)
    if not chunks:
        logger.warning("add_document called with empty text â€” nothing to add.")
        return False

    ids = [_stable_id(text + chunk, i) for i, chunk in enumerate(chunks)]
    embeddings = _embed_texts(chunks)
    metadatas_list = [metadata or {} for _ in chunks]

    has_embeddings = embeddings and all(len(e) > 0 for e in embeddings)

    try:
        upsert_kwargs = dict(ids=ids, documents=chunks, metadatas=metadatas_list)
        if has_embeddings:
            upsert_kwargs["embeddings"] = embeddings
        collection.upsert(**upsert_kwargs)
        logger.info("Added %d chunk(s) to KB.", len(chunks))
        return True
    except Exception as exc:
        logger.error("Failed to add document to KB: %s", exc)
        return False


# ---------------------------------------------------------------------------
# Pre-warm (cold-start optimisation)
# ---------------------------------------------------------------------------

def prewarm():
    """Pre-load ChromaDB client and embedding model to avoid cold-start latency.

    Call this early in application startup (e.g. before the first user
    request) so that the first ``retrieve_context`` call does not stall
    while downloading / loading heavy model weights.
    """
    logger.info("Pre-warming knowledge base components...")
    _get_chroma_client()
    _get_embedding_model()
    logger.info("Pre-warm complete.")
def reload_kb_from_content(content: str, filename: str = "hotel_info.txt") -> bool:
    """Write *content* to `knowledge_docs/<filename>` and rebuild the vector store."""
    import pathlib
    docs_dir = pathlib.Path(DEFAULT_DOCS_DIRECTORY)
    docs_dir.mkdir(parents=True, exist_ok=True)
    target = docs_dir / filename
    try:
        target.write_text(content, encoding="utf-8")
        logger.info("KB file written to %s (%d chars)", target, len(content))
    except OSError as exc:
        logger.error("Failed to write KB file %s: %s", target, exc)
        return False
    return initialize_kb(str(docs_dir))



# ---------------------------------------------------------------------------
# Module-level initialisation â€” ensure the ChromaDB client is ready early.
# ---------------------------------------------------------------------------
_get_chroma_client()


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")

    print("=" * 60)
    print("Knowledge Base â€” self-test")
    print("=" * 60)

    # Pre-warm both singletons
    prewarm()

    # Initialise KB from the default docs directory
    print("\n>> Initialising knowledge base...")
    success = initialize_kb()
    print(f"   Initialisation {'succeeded' if success else 'FAILED'}.")

    # Add a sample document
    print("\n>> Adding sample document...")
    add_document(
        "Flico offers a wide range of smartphones, laptops, tablets, accessories, "
        "and smart home devices with island-wide delivery and after-sales support. "
        "Customers can browse products online at flico.lk or visit a Flico store.",
        metadata={"source": "self-test"},
    )

    # Run a few sample queries
    sample_queries = [
        "What products does Flico offer?",
        "How can I contact customer service?",
        "Tell me about delivery options.",
    ]

    for q in sample_queries:
        print(f"\n>> Query: {q}")
        result = retrieve_context(q)
        if result:
            print(f"   Result:\n   {result[:200]}{'...' if len(result) > 200 else ''}")
        else:
            print("   (no results)")

    print("\n" + "=" * 60)
    print("Self-test complete.")
    print("=" * 60)
