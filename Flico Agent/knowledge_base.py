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
