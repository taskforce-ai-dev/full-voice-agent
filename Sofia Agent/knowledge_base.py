"""
knowledge_base.py â€” Optimized RAG / Knowledge-Base module for the Abans Voice Agent (Sofia).

Provides semantic search over hotel documentation using ChromaDB for vector
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
COLLECTION_NAME: str = "abans_kb"
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

def chunk_text(
    text: str,
    chunk_size: int = 500,
    overlap: int = 50,
) -> List[str]:
    """Split *text* into overlapping chunks, respecting natural boundaries.

    Strategy (in order of preference):
      1. Split on paragraph boundaries (double newlines).
      2. Split on sentence boundaries (period / question-mark / exclamation-mark).
      3. Split on word boundaries (whitespace).

    Each chunk is at most *chunk_size* characters long.  Consecutive chunks
    share *overlap* trailing characters to preserve context across boundaries.
    """
    if not text or not text.strip():
        return []

    text = text.strip()
    if len(text) <= chunk_size:
        return [text]

    chunks: List[str] = []
    start = 0

    while start < len(text):
        end = start + chunk_size

        if end >= len(text):
            chunks.append(text[start:].strip())
            break

        # Look for the best split point inside the window.
        window = text[start:end]

        # 1. Paragraph break
        split_pos = window.rfind("\n\n")
        if split_pos == -1:
            # 2. Sentence break
            for pattern in (". ", "? ", "! "):
                split_pos = window.rfind(pattern)
                if split_pos != -1:
                    split_pos += 1  # include the punctuation
                    break
        if split_pos == -1:
            # 3. Word break
            split_pos = window.rfind(" ")
        if split_pos == -1:
            # Hard cut as last resort
            split_pos = chunk_size

        actual_end = start + split_pos
        chunk = text[start:actual_end].strip()
        if chunk:
            chunks.append(chunk)

        # Move forward, applying overlap
        start = max(actual_end - overlap, start + 1)

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
        metadatas = [{"source": filename, "chunk_index": i} for i in range(len(chunks))]

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

def retrieve_context(query: str, n_results: int = 3) -> str:
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

    # Use the cached embedding path
    query_embedding = _cached_embed_query(query)
    if not query_embedding:
        logger.warning("Query embedding failed for: %s", query)
        return ""

    try:
        results = collection.query(
            query_embeddings=[list(query_embedding)],
            n_results=n_results,
        )
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
        "Abans offers a wide range of electronics and home appliances "
        "with island-wide delivery and after-sales service. Customers can "
        "browse products online or visit any Abans showroom.",
        metadata={"source": "self-test"},
    )

    # Run a few sample queries
    sample_queries = [
        "What products does Abans offer?",
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
