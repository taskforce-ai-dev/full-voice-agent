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
        # is occupancy, never a bedroom filter. The count may be a digit or a
        # word: callers SAY "two bedroom", and STT transcribes it that way, so
        # a digit-only match would silently never filter on a real call.
        bm = re.search(r"\b(\d+|" + "|".join(_NUM_WORDS) + r")\s*-?\s*"
                       r"(?:bed|bedroom|br|bd)s?\b", low)
        if bm:
            tok = bm.group(1)
            f.min_bedrooms = _NUM_WORDS[tok] if tok in _NUM_WORDS else int(tok)

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
