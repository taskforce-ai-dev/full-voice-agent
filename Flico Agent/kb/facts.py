"""Portfolio facts generated FROM the inventory, for injection into the prompt.

The system prompt used to hard-code what the portfolio contains ("our apartments
start at three bedrooms", "we do not currently have any one-bedroom or two-
bedroom apartments"). When the inventory changed, those lines became confident
lies and Fiona denied listings that retrieval had correctly handed her. The facts
and the rows must come from ONE source, so they are derived here.

Keep this cheap: it runs per call, over a dozen-ish rows.
"""
from typing import List

from kb.schema import Property

_NUM_WORD = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five",
             6: "six", 7: "seven", 8: "eight", 9: "nine", 10: "ten"}
_TYPE_PLURAL = {"apartment": "apartments", "house": "houses",
                "commercial": "commercial and office spaces", "land": "land plots"}


def _join(words: List[str]) -> str:
    words = [str(w) for w in words]
    if len(words) <= 1:
        return "".join(words)
    return ", ".join(words[:-1]) + " and " + words[-1]


def _word(n: int) -> str:
    return _NUM_WORD.get(n, str(n))


def _describe(p: Property) -> str:
    """Enough to identify a listing out loud, without repeating its whole prose."""
    bits = []
    if p.bedrooms:
        bits.append(f"{_word(p.bedrooms)}-bedroom")
    bits.append(p.property_type)
    if p.zone:
        bits.append(f"in Colombo {p.zone}")
    return " ".join(bits)


def portfolio_facts(props: List[Property]) -> str:
    """A block of literally-true statements about the current inventory.

    Returns "" for an empty inventory: saying nothing is always safer than
    asserting something false about what we have.
    """
    if not props:
        return ""

    counts = {}
    for p in props:
        counts[p.property_type] = counts.get(p.property_type, 0) + 1
    breakdown = _join([f"{n} {_TYPE_PLURAL.get(t, t)}" for t, n in sorted(counts.items())])

    beds = sorted({p.bedrooms for p in props if p.bedrooms is not None})
    zones = sorted({p.zone for p in props if p.zone is not None})
    on_request = sum(1 for p in props if p.rent_on_request)

    lines = [
        "PORTFOLIO FACTS (generated from the live knowledge base -- these are the "
        "ONLY properties Rodrigo Realtors has right now; never contradict them "
        "and never invent a property outside them):",
        f"- We currently have {len(props)} listing(s) for rent: {breakdown}.",
    ]

    if beds:
        lines.append(
            f"- Bedroom counts available: {_join([_word(b) for b in beds])}. "
            f"The largest is {_word(max(beds))}-bedroom and the smallest is "
            f"{_word(min(beds))}-bedroom. If a caller wants a size we do not "
            f"have, say so honestly and offer the closest option from the "
            f"REFERENCE CONTEXT -- never invent one.")
    if zones:
        lines.append(
            f"- Areas: Colombo {_join(zones)} only. If a caller names any other "
            f"area, tell them we have nothing there rather than offering another "
            f"area as if it were theirs.")
    # Cheapest / dearest, computed. "What's the cheapest place you have?" makes the
    # LLM scan every listing and compare -- an aggregation, not a lookup. It gets
    # this right in English and got it WRONG in Sinhala, naming a Rs 150,000 unit
    # as the cheapest when a Rs 130,000 one exists. Same lesson as the deposit
    # figures: anything computable is computed here, never left to the model.
    priced = [p for p in props if p.rent_amount and p.rent_period == "month"
              and not p.rent_on_request]
    if priced:
        low = min(priced, key=lambda p: p.rent_amount)
        high = max(priced, key=lambda p: p.rent_amount)
        if low.id != high.id:
            lines.append(
                f"- CHEAPEST listing: {low.id} at Rs {int(low.rent_amount):,} per "
                f"month ({_describe(low)}). MOST EXPENSIVE: {high.id} at "
                f"Rs {int(high.rent_amount):,} per month ({_describe(high)}). If a "
                f"caller asks for the cheapest or the most expensive, use these -- "
                f"do NOT work it out yourself.")

    if on_request == 0:
        lines.append("- Every listing has a fixed, quoted monthly rent.")
    elif on_request == len(props):
        lines.append("- Every listing's rent is quoted on request; a consultant "
                     "confirms the figure on follow-up.")
    else:
        lines.append(f"- {len(props) - on_request} listing(s) have a fixed quoted "
                     f"rent; {on_request} are quoted on request.")
    return "\n".join(lines) + "\n\n"
