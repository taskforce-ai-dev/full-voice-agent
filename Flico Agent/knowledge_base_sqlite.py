"""SQLite hybrid backend adapter — maps the KB contract onto kb.engine."""
import json
import logging
import os
from typing import Optional

from kb.config import DEFAULT_DOCS_DIRECTORY, DB_PATH
from kb.engine import RealEstateKB
from kb.facts import portfolio_facts as facts_from_rows
from kb.migrate import parse_prose
from kb.prose import render_all
from kb.schema import Property

logger = logging.getLogger(__name__)
_engine: Optional[RealEstateKB] = None


def _get_engine() -> RealEstateKB:
    global _engine
    if _engine is None:
        _engine = RealEstateKB(db_path=DB_PATH)
    return _engine


_VALID_TYPES = {"apartment", "house", "commercial", "land"}


def _validate(rows, skipped) -> Optional[str]:
    """Returns a rejection reason, or None if the batch is safe to load.

    The admin portal publishes KB content straight to /kb-reload, which never
    passes through CI. Without this, a malformed paste silently becomes the
    inventory a live caller is quoted from. A listing that cannot be read is a
    listing the filter can never match, so reject the batch rather than serve it.
    """
    if skipped:
        return f"{len(skipped)} paragraph(s) failed to parse: {skipped[:3]}"
    if not rows:
        return "no listings parsed"
    for r in rows:
        if r.property_type not in _VALID_TYPES:
            return f"{r.id}: unknown property_type {r.property_type!r}"
        if r.zone is None:
            return f"{r.id}: no Colombo zone -> the zone filter can never match it"
        if r.rent_amount is None and not r.rent_on_request:
            return f"{r.id}: no rent and not marked on request"
    ids = [r.id for r in rows]
    if len(set(ids)) != len(ids):
        return "duplicate listing ids"
    return None


def _load_from_text(text: str) -> bool:
    engine = _get_engine()
    rows, skipped, preamble = parse_prose(text)
    reason = _validate(rows, skipped)
    if reason:
        # Keep the previous KB: a live agent with a stale-but-valid inventory is
        # strictly safer than one with a corrupted inventory.
        logger.error("KB reload REJECTED, keeping previous inventory: %s", reason)
        return False
    engine.preamble = preamble
    engine.add_properties(rows)
    logger.info("SQLite KB loaded %d listings", engine.get_count())
    return True


def _load_from_listings(path: str, docs_directory: str) -> bool:
    """Load typed rows and GENERATE their prose.

    This is the inverted pipeline: rows are the source, prose is derived. The old
    path hand-authored prose and regex-parsed it back into rows, which is where
    the type-vocabulary drift and the first-Rs-wins rent bug lived.
    """
    with open(path, "r", encoding="utf-8") as fh:
        doc = json.load(fh)

    rows = [Property(**r) for r in doc.get("listings", [])]
    reason = _validate(rows, skipped=[])
    if reason:
        logger.error("listings.json REJECTED, keeping previous inventory: %s", reason)
        return False
    rows = render_all(rows)  # description is generated, never hand-written

    preamble = ""
    pre_name = doc.get("preamble_file")
    if pre_name:
        pre_path = os.path.join(docs_directory, pre_name)
        if os.path.isfile(pre_path):
            with open(pre_path, "r", encoding="utf-8") as fh:
                preamble = fh.read().strip()
        else:
            logger.warning("preamble_file %s not found", pre_path)

    engine = _get_engine()
    engine.preamble = preamble
    engine.add_properties(rows)
    logger.info("SQLite KB loaded %d listings from %s (prose generated)",
                engine.get_count(), os.path.basename(path))
    return True


def initialize_kb(docs_directory: str = DEFAULT_DOCS_DIRECTORY) -> bool:
    # Structured source first. The prose file remains a fallback so a rollback to
    # the previous KB is one file away and cannot be blocked by this code path.
    listings = os.path.join(docs_directory, "listings.json")
    if os.path.isfile(listings):
        try:
            return _load_from_listings(listings, docs_directory)
        except Exception as exc:
            logger.exception("listings.json failed to load (%s); falling back to prose", exc)

    path = os.path.join(docs_directory, "flico_info.txt")
    if not os.path.isfile(path):
        logger.warning("KB prose not found at %s", path)
        return False
    with open(path, "r", encoding="utf-8") as fh:
        return _load_from_text(fh.read())


def prewarm() -> None:
    _get_engine()  # constructs the model + DB connection


def reload_kb_from_content(content: str, filename: str = "flico_info.txt") -> bool:
    # Validate BEFORE touching disk. Writing first would leave rejected content
    # on the filesystem for the next restart to load, so a bad paste would take
    # effect anyway the next time the container bounced.
    rows, skipped, _ = parse_prose(content)
    reason = _validate(rows, skipped)
    if reason:
        logger.error("KB reload REJECTED (not written to disk): %s", reason)
        return False

    docs_dir = DEFAULT_DOCS_DIRECTORY
    os.makedirs(docs_dir, exist_ok=True)
    try:
        with open(os.path.join(docs_dir, filename), "w", encoding="utf-8") as fh:
            fh.write(content)
    except OSError as exc:
        logger.error("Failed to write KB file: %s", exc)
        return False
    return _load_from_text(content)


def portfolio_facts(agency: str = "Rodrigo Realtors") -> str:
    """Literally-true statements about the live inventory, for the system prompt.

    Returns "" on any fault: a prompt with no portfolio claims is safe, one with
    stale claims is not. `agency` is forwarded to kb.facts.portfolio_facts so
    the generated text names whichever brand is calling.
    """
    try:
        return facts_from_rows(_get_engine().all_properties(), agency=agency)
    except Exception as exc:
        logger.error("portfolio_facts failed: %s", exc)
        return ""


def retrieve_context(query: str, n_results: Optional[int] = None,
                     sticky: Optional[dict] = None) -> str:
    try:
        return _get_engine().retrieve(query, n_results=n_results, sticky=sticky)
    except Exception as exc:  # never crash a live call on a KB fault
        logger.error("sqlite retrieve_context failed: %s", exc)
        return ""
