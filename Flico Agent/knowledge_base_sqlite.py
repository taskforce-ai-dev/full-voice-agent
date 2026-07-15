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
