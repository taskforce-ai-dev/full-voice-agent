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

    _SCALES = (("thousand", 1_000), ("million", 1_000_000), ("lakhs", 100_000),
               ("lakh", 100_000), ("k", 1_000), ("m", 1_000_000))
    _MONEY = r"(k|m|thousand|million|lakhs?)"
    _AMOUNT = r"(\d[\d,]*(?:\.\d+)?)"
    # "between 300k and 500k", "from 300 to 500 thousand", "300k-500k"
    _RANGE_RE = re.compile(r"(?:between|from)?\s*(?:rs\.?\s*)?" + _AMOUNT + r"\s*"
                           + _MONEY + r"?\s*(?:and|to|until|-)\s*(?:rs\.?\s*)?"
                           + _AMOUNT + r"\s*" + _MONEY + r"?")
    # "over 300k" -- a scale unit is REQUIRED so "more than 2 bedrooms" can
    # never be read as a rent floor of 2,000.
    _MIN_RE = re.compile(r"(?:over|above|more than|at least|minimum|starting"
                         r"\s+(?:at|from))\s*(?:rs\.?\s*)?" + _AMOUNT + r"\s*" + _MONEY)

    @staticmethod
    def _scale(val: float, unit: Optional[str]) -> float:
        unit = (unit or "").lower()
        for name, mult in QueryParser._SCALES:
            if unit == name:
                return val * mult
        # A bare figure under 10,000 is spoken shorthand ("under 300" = 300k).
        return val * 1_000 if val < 10_000 else val

    @staticmethod
    def _rent_range(low: str) -> Tuple[Optional[float], Optional[float]]:
        m = QueryParser._RANGE_RE.search(low)
        if not m:
            return None, None
        u1, u2 = m.group(2), m.group(4)
        if not u1 and not u2:
            return None, None  # bare "2 to 3" is a count, not money
        if re.match(r"\s*(bed|bd|br)", low[m.end():m.end() + 12]):
            return None, None  # "2 to 3 bedrooms"
        lo = QueryParser._scale(float(m.group(1).replace(",", "")), u1 or u2)
        hi = QueryParser._scale(float(m.group(3).replace(",", "")), u2 or u1)
        return (lo, hi) if lo <= hi else (hi, lo)

    @staticmethod
    def _min_rent(low: str) -> Optional[float]:
        m = QueryParser._MIN_RE.search(low)
        if not m:
            return None
        return QueryParser._scale(float(m.group(1).replace(",", "")), m.group(2))

    @staticmethod
    def _max_rent(low: str) -> Tuple[Optional[float], bool]:
        """Returns (ceiling, exclusive). 'under 300k' must not offer a 300k
        listing; 'up to 300k' must. Treating both as <= puts a listing the
        caller ruled out at the top of the results."""
        m = re.search(r"(under|below|less than|max|budget of|up to|no more than)"
                      r"\s*(?:rs\.?\s*)?(\d[\d,]*(?:\.\d+)?)\s*"
                      r"(k|m|thousand|million|lakhs?)?", low)
        if not m:
            return None, False
        exclusive = m.group(1) in ("under", "below", "less than")
        val = float(m.group(2).replace(",", ""))
        unit = (m.group(3) or "").lower()
        if unit in ("k", "thousand"):
            val *= 1_000
        elif unit in ("m", "million"):
            val *= 1_000_000
        elif unit.startswith("lakh"):
            val *= 100_000
        elif val < 10_000:
            val *= 1_000
        return val, exclusive

    @staticmethod
    def parse(utterance: str) -> Tuple[str, QueryFilters]:
        low = utterance.lower()
        f = QueryFilters()
        f.property_type = QueryParser._classify_type(low)
        f.zone = QueryParser._zone(low)
        lo, hi = QueryParser._rent_range(low)
        if hi is not None:
            f.min_rent, f.max_rent = lo, hi  # a stated range is inclusive both ends
        else:
            f.min_rent = QueryParser._min_rent(low)
            f.max_rent, f.max_rent_exclusive = QueryParser._max_rent(low)

        # Bedrooms only from an explicit "N-bed(room)" phrase. Bare "N people"
        # is occupancy, never a bedroom filter. The count may be a digit or a
        # word: callers SAY "two bedroom", and STT transcribes it that way, so
        # a digit-only match would silently never filter on a real call.
        bm = re.search(r"\b(\d+|" + "|".join(_NUM_WORDS) + r")\s*"
                       r"(\+|or more|or above|and above|plus)?\s*-?\s*"
                       r"(?:bed|bedroom|br|bd)s?\b", low)
        if bm:
            tok = bm.group(1)
            count = _NUM_WORDS[tok] if tok in _NUM_WORDS else int(tok)
            # "a two bedroom apartment" means EXACTLY two. Treating it as a floor
            # offers a 3-bed to someone who asked for 2 -- a wrong answer, not an
            # upsell. Only an explicit "at least 2" / "2 or more" / "2+" is a floor.
            head = low[:bm.start()].rstrip()
            if bm.group(2) or re.search(r"\b(at least|minimum|min|more than|over)$", head):
                f.min_bedrooms = count
            else:
                f.bedrooms = count

        return utterance, f

    @staticmethod
    def merge_sticky(filters: QueryFilters, sticky: dict) -> QueryFilters:
        if filters.property_type is None and sticky.get("property_type"):
            filters.property_type = sticky["property_type"]
        if filters.zone is None and sticky.get("zone"):
            filters.zone = sticky["zone"]
        # A bedroom count carries across turns like type and zone do: "a two
        # bedroom apartment" -> "what about Colombo 7?" must stay two-bedroom.
        # A count stated this turn overrides; exact and floor are exclusive.
        if filters.bedrooms is None and filters.min_bedrooms is None:
            if sticky.get("bedrooms"):
                filters.bedrooms = sticky["bedrooms"]
            elif sticky.get("min_bedrooms"):
                filters.min_bedrooms = sticky["min_bedrooms"]
        if filters.property_type:
            sticky["property_type"] = filters.property_type
        if filters.zone:
            sticky["zone"] = filters.zone
        if filters.bedrooms:
            sticky["bedrooms"] = filters.bedrooms
            sticky.pop("min_bedrooms", None)
        if filters.min_bedrooms:
            sticky["min_bedrooms"] = filters.min_bedrooms
            sticky.pop("bedrooms", None)
        return filters
