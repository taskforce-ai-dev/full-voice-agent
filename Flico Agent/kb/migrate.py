import re
from typing import List, Optional, Tuple

from kb.query_parser import _COMMERCIAL_MARKERS
from kb.schema import Property

_WORD_NUM = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
             "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}
_TEXT_RENT = {"fifteen thousand": 15000.0}  # extend as needed; numeric Rs is primary


def _classify(low: str) -> Optional[str]:
    # Mirrors QueryParser._classify_type -- same word-boundary rules, same
    # vocabulary. A listing whose type cannot be read is REJECTED (None) rather
    # than silently filed as an apartment: a misfiled row is invisible to the
    # type filter forever, and the prose is hand-authored.
    if any(m in low for m in _COMMERCIAL_MARKERS):
        return "commercial"
    if re.search(r"\b(apartments?|flats?|penthouses?|condos?|condominiums?)\b", low):
        return "apartment"
    if re.search(r"\b(houses?|town\s*houses?|villas?|bungalows?|annexe?s?)\b", low):
        return "house"
    if re.search(r"\b(lands?|plots?)\b", low):
        return "land"
    return None


def _int(s: Optional[str]) -> Optional[int]:
    return int(s.replace(",", "")) if s else None


def _parse_listing(para: str) -> Optional[Property]:
    low = para.lower()
    ref = re.search(r"\(ref:\s*(p\d+)\)", low)
    if not ref:
        return None
    pid = ref.group(1).upper()

    ptype = _classify(low)
    if ptype is None:
        return None  # unreadable type -> skipped, never guessed

    beds = re.search(r"(\d+)\s*-\s*bedroom", low)
    baths = re.search(r"(\d+)\s*-\s*bathroom", low)
    zone = re.search(r"colombo\s+(\d{1,2})", low)
    area = re.search(r"colombo\s+\d{1,2}\s*\(([^)]+)\)", low)
    bld = re.search(r"for\s+(?:rent|sale)\s+at\s+([^,]+?)\s+in\s+colombo", para, re.I)
    sqft = re.search(r"floor area of ([\d,]+)\s*square feet", low)

    if "semi-furnished" in low or "semi furnished" in low:
        furnishing = "semi"
    elif "unfurnished" in low:
        furnishing = "unfurnished"
    elif "furnished" in low:
        furnishing = "furnished"
    else:
        furnishing = None

    on_request = "available on request" in low or "rent on request" in low
    rent_amount = None
    rent_period = None
    if not on_request:
        rs = re.search(r"rs\s*([\d,]+)", low)
        if rs:
            rent_amount = float(rs.group(1).replace(",", ""))
        else:
            for phrase, val in _TEXT_RENT.items():
                if phrase in low:
                    rent_amount = val
                    break
        if "per day" in low:
            rent_period = "day"
        elif "per month" in low or "monthly rent" in low:
            rent_period = "month"

    pk = re.search(r"(one|two|three|four|\d+)\s+(?:covered\s+)?parking", low)
    parking = None
    if pk:
        tok = pk.group(1)
        parking = _WORD_NUM.get(tok, None) if tok in _WORD_NUM else int(tok)

    dep = re.search(r"(\d+)\s*-?\s*month[s']*\s+deposit", low)
    adv = re.search(r"(\d+)\s+months?['’]?\s+advance", low)
    lease = re.search(r"minimum lease of (\d+)\s*year", low)

    return Property(
        id=pid, transaction="rent", property_type=ptype,
        zone=_int(zone.group(1)) if zone else None,
        area=area.group(1).title() if area else "",
        building=bld.group(1).strip() if bld else None,
        bedrooms=_int(beds.group(1)) if beds else None,
        bathrooms=float(baths.group(1)) if baths else None,
        rent_amount=rent_amount, rent_period=rent_period, rent_on_request=on_request,
        furnishing=furnishing,
        floor_area_sqft=_int(sqft.group(1)) if sqft else None,
        parking=parking,
        deposit_months=_int(dep.group(1)) if dep else None,
        advance_months=_int(adv.group(1)) if adv else None,
        min_lease_months=(int(lease.group(1)) * 12) if lease else None,
        key_features=[],
        description=para.strip(),
    )


def parse_prose(text: str) -> Tuple[List[Property], List[str], str]:
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    rows, skipped, preamble_parts = [], [], []
    for para in paras:
        if para.startswith("Rodrigo Realtors has"):
            prop = _parse_listing(para)
            if prop:
                rows.append(prop)
            else:
                skipped.append(para[:80])
        else:
            # Non-listing prose (intro, AREAS COVERED, NEXT STEPS, section headers)
            preamble_parts.append(para)
    return rows, skipped, "\n\n".join(preamble_parts)


def migrate_file(prose_path: str) -> Tuple[List[Property], str]:
    with open(prose_path, "r", encoding="utf-8") as fh:
        text = fh.read()
    paras = [p for p in re.split(r"\n\s*\n", text) if p.strip()]
    rows, skipped, preamble = parse_prose(text)
    print(f"[migrate] {len(paras)} paragraphs -> {len(rows)} rows, {len(skipped)} skipped")
    for s in skipped:
        print(f"[migrate]   SKIPPED: {s}...")
    return rows, preamble
