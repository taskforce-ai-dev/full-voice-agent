"""Generate a listing's prose FROM its fields.

This inverts the pipeline. The prose used to be the source of truth, hand-authored
and regex-parsed back into typed rows by kb/migrate.py. That round-trip is where a
whole class of bugs lives: the type vocabulary was duplicated and drifted, the rent
matcher took the first "Rs N" in the paragraph, and an unreadable listing was
silently filed as an apartment. Worse, the agency's data ARRIVES structured -- it
was being destructured into prose so we could pay to reconstruct it.

So: rows are the source, prose is derived. Nothing parses prose back into fields.

The output deliberately matches the previous hand-authored paragraphs
(knowledge_docs/flico_info.txt), because whoever wrote those was already executing
this template by hand. That is also what lets the OLD parser verify this generator
during migration -- see tests/test_prose_roundtrip.py.

Uniform structure across listings is a feature, not a defect. This paragraph is
Amaya's grounding context, not words a caller hears: she rephrases per turn, per
language, per caller. Uniformity makes her extraction more reliable (the rent is
always in the same place) and makes embeddings comparable across listings. Per
listing personality belongs in `commentary`, which is human-authored data.
"""
from typing import List, Optional

from kb.schema import Property

_ONES = ("", "one", "two", "three", "four", "five", "six", "seven", "eight",
         "nine", "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen",
         "sixteen", "seventeen", "eighteen", "nineteen")
_TENS = ("", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy",
         "eighty", "ninety")
_TYPE_WORD = {"apartment": "apartment", "house": "house",
              "commercial": "commercial property", "land": "land"}
_FURNISH_WORD = {"furnished": "furnished", "semi": "semi-furnished",
                 "unfurnished": "unfurnished"}


def _under_thousand(n: int) -> str:
    if n < 20:
        return _ONES[n]
    if n < 100:
        return _TENS[n // 10] + ("-" + _ONES[n % 10] if n % 10 else "")
    head = _ONES[n // 100] + " hundred"
    return head + (" and " + _under_thousand(n % 100) if n % 100 else "")


def number_in_words(n: int) -> str:
    """Spell a whole number. The TTS voice reads these aloud, and the Sinhala
    prompt requires prices as words, so digits alone are not enough."""
    if n == 0:
        return "zero"
    parts: List[str] = []
    for scale_value, scale_name in ((1_000_000_000, "billion"), (1_000_000, "million"),
                                    (1_000, "thousand")):
        if n >= scale_value:
            parts.append(_under_thousand(n // scale_value) + " " + scale_name)
            n %= scale_value
    if n:
        parts.append(_under_thousand(n))
    out = " ".join(parts)
    # "one hundred and eighty thousand", not "one hundred eighty thousand".
    if len(parts) > 1 and parts[-1] and n and n < 100:
        out = " ".join(parts[:-1]) + " and " + parts[-1]
    return out


def _oxford(items: List[str]) -> str:
    items = [i for i in items if i]
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + ", and " + items[-1]


def _rent_clause(p: Property) -> str:
    if p.rent_on_request or p.rent_amount is None:
        return ("The monthly rent is available on request -- a Star Properties "
                "consultant will share it on follow-up.")
    period = p.rent_period or "month"
    words = number_in_words(int(p.rent_amount))
    return (f"at a {'monthly ' if period == 'month' else ''}rent of {words} rupees "
            f"(Rs {int(p.rent_amount):,}) per {period}.")


def _area_clause(p: Property) -> str:
    bits = []
    if p.floor_area_sqft:
        bits.append(f"with a floor area of {p.floor_area_sqft:,} square feet")
    if p.land_perches:
        bits.append(f"on {p.land_perches} perches of land")
    return " ".join(bits)


def _money(n: int) -> str:
    return f"{number_in_words(n)} rupees (Rs {n:,})"


def _lease_clause(p: Property) -> str:
    """Lease terms WITH the cash amounts pre-computed.

    A real caller asks "how much is the deposit?" and wants a number. The prose
    used to say only "a 2-month deposit", so answering required the LLM to
    multiply -- and an LLM doing arithmetic on facts it is holding will produce a
    confident figure whether or not it is right. Anything computable is computed
    here, in code, where it is deterministic and testable. Amaya reads it; she
    never has to do the maths.
    """
    # Only monthly rents have a meaningful deposit-in-rupees. A per-day rent with
    # a "2-month deposit" is not 2 x the daily figure, so do not invent one.
    monthly = int(p.rent_amount) if (p.rent_amount and p.rent_period == "month"
                                     and not p.rent_on_request) else None
    terms = []
    if p.deposit_months:
        term = f"a {p.deposit_months}-month deposit"
        if monthly:
            term += f" of {_money(p.deposit_months * monthly)}"
        terms.append(term)
    if p.advance_months:
        # "1 months' advance" is wrong; "1 month advance" is how it is said here.
        # Keep the quote out of the f-string expression: nested same-type quotes
        # are PEP 701 (Python 3.12+) and production runs 3.11.
        plural = "s'" if p.advance_months > 1 else ""
        term = f"{p.advance_months} month{plural} advance"
        if monthly:
            term += f" of {_money(p.advance_months * monthly)}"
        terms.append(term)
    if p.min_lease_months:
        years = p.min_lease_months // 12
        terms.append(f"a minimum lease of {years} year" if years
                     else f"a minimum lease of {p.min_lease_months} months")
    # Parking must survive a row with NO lease terms. This used to `return ""`
    # before the parking clause was reached, so 18 of the 60 real listings
    # recorded parking that the agent was never told about -- the caller asks
    # "is there parking?" and a property with three spaces answers as if it has
    # none. Parking is a property fact, not a lease term.
    park = ""
    if p.parking:
        plural = "s" if p.parking > 1 else ""
        park = f"{number_in_words(p.parking)} parking space{plural}"

    if not terms:
        return f"It has {park}." if park else ""

    out = f"Lease terms are {_oxford(terms)}"
    if park:
        out += f", with {park}"
    out += "."

    upfront = ((p.deposit_months or 0) + (p.advance_months or 0))
    if monthly and upfront:
        out += (f" The total payable before moving in is "
                f"{_money(upfront * monthly)}.")
    return out


def render(p: Property) -> str:
    """Render one listing as the paragraph the LLM reads."""
    beds = f"{p.bedrooms}-bedroom, " if p.bedrooms else ""
    baths = f"{int(p.bathrooms)}-bathroom " if p.bathrooms else ""
    furnish = f"{_FURNISH_WORD.get(p.furnishing, p.furnishing)} " if p.furnishing else ""
    kind = _TYPE_WORD.get(p.property_type, p.property_type)

    where = f"on {p.street}" if p.street else ""
    if p.zone:
        zone = f"in Colombo {p.zone}" + (f" ({p.area})" if p.area else "")
        where = f"{where} {zone}".strip()
    elif p.area:
        where = f"{where} in {p.area}".strip()

    head = (f"Star Properties has a {beds}{baths}{furnish}{kind} "
            f"for {p.transaction} {where}").rstrip()
    area = _area_clause(p)
    if area:
        head += f", {area}"

    rent = _rent_clause(p)
    head += f", {rent}" if rent.startswith("at a") else f". {rent}"

    parts = [head]
    if p.available:
        parts.append(f"It is available {p.available}."
                     if p.available in ("now", "soon") else f"Availability: {p.available}.")
    lease = _lease_clause(p)
    if lease:
        parts.append(lease)
    if p.key_features:
        parts.append(f"Features include {_oxford(p.key_features)}.")
    if p.commentary:
        parts.append(p.commentary.strip())
    parts.append(f"(Ref: {p.id})")
    return " ".join(parts)


def render_all(props: List[Property]) -> List[Property]:
    """Return the rows with `description` filled in. Mutates copies, not inputs."""
    out = []
    for p in props:
        q = p.model_copy()
        q.description = render(q)
        out.append(q)
    return out
