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
# Commercial vocabulary, tiered by evidential strength. Word boundaries are
# load-bearing everywhere in this classifier: bare substring matching read
# "Slave Island" and "Iceland Residence" as property_type=land, "penthouse" as
# a house, and -- the live bug this replaces -- "shopping district", "workshop",
# and "police officer" as commercial property.
#
# STRONG phrases name the product itself ("office space", "commercial
# building"). They outrank everything, including an explicit residential noun:
# "commercial house" is not a house, and a commercial listing's prose may
# mention the "sea-facing apartments" next door in its commentary.
#
# WEAK single words ("office", "shop", "retail", "warehouse") routinely
# describe a caller's SURROUNDINGS ("a flat near my office", "a house close to
# the shops"), so they classify commercial only when the utterance names no
# explicit residential or land type. A caller who said "apartment" gets an
# apartment, whatever else the sentence mentions.
#
# Even with nothing explicit named, a weak marker GOVERNED by a proximity
# phrase is the neighbourhood, not the ask: "somewhere close to shops and
# restaurants" is a renter describing surroundings, and filtering commercial
# there would hide every home from them. A governed marker contributes
# nothing; if that leaves no signal the parse is None (unfiltered), which with
# n_results=None hands the LLM the complete inventory -- the safe failure.
# Proximity governs THROUGH a short coordinated list ("near restaurants,
# cafes and shops") but never across more than four words, so "near the
# beach, I want a shop" keeps its product reading. A proximity phrase AFTER
# the marker does not disarm it ("a shop close to the station" is still a
# commercial ask) -- only the trailing adverbs "nearby" / "close by" /
# "next door" / "within walking distance" do.
_COMMERCIAL_STRONG_RE = re.compile(
    r"\b(?:office\s+spaces?|commercial\s+"
    r"(?:buildings?|propert(?:y|ies)|houses?|spaces?|units?))\b")
_COMMERCIAL_WEAK_RE = re.compile(r"\b(?:offices?|shops?|retail|warehouses?)\b")
_APARTMENT_RE = re.compile(r"\b(?:apartments?|flats?|penthouses?|condos?|condominiums?)\b")
_HOUSE_RE = re.compile(r"\b(?:houses?|town\s*houses?|villas?|bungalows?|annexe?s?)\b")
_LAND_RE = re.compile(r"\b(?:lands?|plots?)\b")

_PROXIMITY_HEAD = (
    r"\b(?:near(?:\s+to)?|closer?\s+to|next\s+to|beside|opposite|"
    r"adjacent\s+to|across\s+from|(?:within\s+)?walking\s+distance\s+"
    r"(?:to|from|of)|a\s+short\s+walk\s+(?:to|from)|around\s+the\s+corner\s+"
    r"from|\w+\s+minutes?\s+(?:to|from)|steps\s+from|surrounded\s+by)"
)
# Anchored at the end of the text PRECEDING a weak marker: proximity phrase,
# then at most four filler words (articles, adjectives, a coordinated list).
_PROXIMITY_BEFORE_RE = re.compile(_PROXIMITY_HEAD + r"(?:[\s,]+\w+){0,4}[\s,]+$")
# Anchored at the start of the text FOLLOWING a weak marker.
_PROXIMITY_AFTER_RE = re.compile(
    r"^(?:[\s,]+\w+){0,2}?[\s,]+(?:nearby|close\s+by|next\s+door|"
    r"within\s+walking\s+distance)\b")


def _weak_commercial_ask(low: str) -> bool:
    """True only if some bare commercial marker is NOT proximity-governed."""
    for m in _COMMERCIAL_WEAK_RE.finditer(low):
        if (_PROXIMITY_BEFORE_RE.search(low[:m.start()])
                or _PROXIMITY_AFTER_RE.match(low[m.end():])):
            continue  # surroundings, not the product
        return True
    return False


def classify_type(low: str) -> Optional[str]:
    """The ONE property-type classifier -- caller utterances (QueryParser) and
    listing prose (kb.migrate._classify) both resolve through this function, so
    the two vocabularies can never drift apart again. Precedence:

      1. strong commercial phrase            -> commercial
      2. explicit residential noun           -> apartment / house
      3. explicit land noun                  -> land
      4. weak marker, NOT proximity-governed -> commercial
      5. nothing recognised (or only
         proximity-governed weak markers)    -> None (unfiltered, never guessed)
    """
    if _COMMERCIAL_STRONG_RE.search(low):
        return "commercial"
    if _APARTMENT_RE.search(low):
        return "apartment"
    if _HOUSE_RE.search(low):
        return "house"
    if _LAND_RE.search(low):
        return "land"
    if _weak_commercial_ask(low):
        return "commercial"
    return None


class QueryParser:
    @staticmethod
    def _classify_type(low: str) -> Optional[str]:
        return classify_type(low)

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
    # A figure followed by a SIZE or COUNT unit is not money. Without this,
    # "under 1000 square feet" parsed as a rent ceiling of Rs 1,000,000.
    _NOT_MONEY = re.compile(r"\s*(sq\b|sq\.|sqft|square|feet|foot|ft\b|perch|"
                            r"bed|bd\b|br\b|bath|room|people|person)")
    # "between 300k and 500k", "from 300 to 500 thousand", "300k-500k"
    _RANGE_RE = re.compile(r"(?:between|from)?\s*(?:rs\.?\s*)?" + _AMOUNT + r"\s*"
                           + _MONEY + r"?\s*(?:and|to|until|-)\s*(?:rs\.?\s*)?"
                           + _AMOUNT + r"\s*" + _MONEY + r"?")
    # "over 300k" -- a scale unit is REQUIRED so "more than 2 bedrooms" can
    # never be read as a rent floor of 2,000.
    # The negation lookbehinds are load-bearing: "not more than 300k" contains
    # "more than 300k", so without them it set a rent FLOOR of 300k as well as
    # the ceiling, and the query collapsed to rows costing exactly 300k.
    _MIN_RE = re.compile(r"(?<!not )(?<!no )"
                         r"(?:over|above|more than|at least|minimum|starting"
                         r"\s+(?:at|from))\s*(?:rs\.?\s*)?" + _AMOUNT + r"\s*" + _MONEY)
    # Longer alternatives first. The floor regex accepted "minimum" while this
    # one accepted only "max", so "maximum 300k" and "my budget is 300k" parsed
    # to no ceiling at all -- the vocabularies were asymmetric by accident.
    _MAX_RE = re.compile(r"(under|below|less than|not more than|no more than|"
                         r"maximum|max|budget of|budget is|budget|up to|within)"
                         r"\s*(?:rs\.?\s*)?" + _AMOUNT + r"\s*" + _MONEY + r"?")

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
        if not m or QueryParser._NOT_MONEY.match(low[m.end():]):
            return None
        return QueryParser._scale(float(m.group(1).replace(",", "")), m.group(2))

    @staticmethod
    def _max_rent(low: str) -> Tuple[Optional[float], bool]:
        """Returns (ceiling, exclusive). 'under 300k' must not offer a 300k
        listing; 'up to 300k' must. Treating both as <= puts a listing the
        caller ruled out at the top of the results."""
        m = QueryParser._MAX_RE.search(low)
        if not m or QueryParser._NOT_MONEY.match(low[m.end():]):
            return None, False
        exclusive = m.group(1) in ("under", "below", "less than")
        return QueryParser._scale(float(m.group(2).replace(",", "")), m.group(3)), exclusive

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
        # A budget carries across turns for the same reason a bedroom count does:
        # "an apartment under 200k" -> "what about Colombo 5?" must not start
        # offering 280k units. A budget stated this turn overrides.
        if filters.max_rent is None and sticky.get("max_rent"):
            filters.max_rent = sticky["max_rent"]
            filters.max_rent_exclusive = sticky.get("max_rent_exclusive", False)
        if filters.min_rent is None and sticky.get("min_rent"):
            filters.min_rent = sticky["min_rent"]
        if filters.property_type:
            sticky["property_type"] = filters.property_type
        if filters.zone:
            sticky["zone"] = filters.zone
        if filters.max_rent:
            sticky["max_rent"] = filters.max_rent
            sticky["max_rent_exclusive"] = filters.max_rent_exclusive
        if filters.min_rent:
            sticky["min_rent"] = filters.min_rent
        if filters.bedrooms:
            sticky["bedrooms"] = filters.bedrooms
            sticky.pop("min_bedrooms", None)
        if filters.min_bedrooms:
            sticky["min_bedrooms"] = filters.min_bedrooms
            sticky.pop("bedrooms", None)
        return filters
