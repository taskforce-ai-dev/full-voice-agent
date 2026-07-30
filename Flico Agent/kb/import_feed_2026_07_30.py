"""ONE-TIME import: the operator's pipe-delimited feed -> typed rows.

Run once, review the diff, then the rows are DATA. Never re-run against a
changed source without re-auditing -- the exhaustive filter proof is a statement
about a FIXED row set.

This emits ROWS ONLY. It never writes prose: kb/prose.py generates the
paragraph from these fields. Nothing parses prose back into fields.

Photo URLs in the feed are deliberately NOT imported. This is a voice agent;
the KB tells callers a salesperson sends photos on follow-up.

Ambiguity policy (from the operator, 2026-07-30): a field that cannot be read
unambiguously is left NULL rather than guessed. P01 is dropped entirely (two
conflicting prices, implausible floor area) and P04 is excluded (marked
"Rented").

    python3 kb/import_feed_2026_07_30.py            # writes listings.json
    python3 kb/import_feed_2026_07_30.py --dry-run  # prints a summary only
"""
import json
import pathlib
import re
import sys

HERE = pathlib.Path(__file__).resolve().parent.parent
SOURCE = HERE / "knowledge_docs" / "_source_feed_2026-07-30.txt"
TARGET = HERE / "knowledge_docs" / "listings.json"

DROP = {"P01"}      # conflicting prices + implausible floor area
EXCLUDE = {"P04"}   # marked "Rented"

# First-column values that are genuine building names, not listing titles.
BUILDINGS = {
    "Havelock City - Furnished": "Havelock City",
    "Altair": "Altair",
    "One Galle Face": "One Galle Face",
    "Capitol Twin Peaks": "Capitol Twin Peaks",
    "447 Luna": "447 Luna",
    "Trillium Residencies": "Trillium Residencies",
    "606 The Address": "606 The Address",
    "The Grand": "The Grand",
    "Shangri La Furnished Apartment for Rent- A39980": "Shangri La",
    "Iceland Residence Furnished Apartment for Rent - A38087": "Iceland Residence",
    "Capitol Twinpeaks - Furnished Apartment for Rent- A51044": "Capitol Twin Peaks",
    "Victoria Park Mansion Furnished Penthouse for Rent- A12444": "Victoria Park Mansion",
    "Altair Furnished Apartment for Rent- A48888": "Altair",
    "Metro Homes": "Metro Homes",
    "Apartment for rent - The Grand": "The Grand",
    "Legends Tower - 03 Bedroom Furnished Apartment for Rent in C": "Legends Tower",
    "Skelton Road": None,
    "3 Bedroom Apartment at Altair - Straight Tower - Top Floor": "Altair",
    "3 Bedroom Penthouse Apartment For Rent in Skyline Residencie": "Skyline Residencies",
    "4 Bedroom Apartment For Rent in The Grand, Colombo 7": "The Grand",
    "3 Bedroom Apartment For Rent in One Galle Face - West Tower": "One Galle Face",
    "3 Bedroom Apartment For Rent in Empire Residencies": "Empire Residencies",
    "3 Bedroom Apartment at Altair - Straight Tower": "Altair",
    "Trizen - Tower 1": "Trizen",
}

TYPE_MAP = {
    "Apartment": "apartment",
    "House": "house",
    "Office space": "commercial",
    "Building": "commercial",
}

FURNISH_MAP = {
    "Furnished": "furnished",
    "Semi-furnished": "semi",
    "Unfurnished": "unfurnished",
}

# Human-authored colour, keyed by zone. Rendered verbatim into the prose --
# never an LLM paraphrase. Transcribed from the feed's NEIGHBOURHOOD CONTEXT.
ZONE_COLOUR = {
    1: "Fort and Galle Face -- the corporate hub, with sea views and premium addresses.",
    2: "Slave Island, Union Place and Park Street -- luxury towers, the central business district, restaurants and easy city access.",
    3: "Kollupitiya and Marine Drive -- embassies, sea-facing residences, business districts and premium city living.",
    4: "Bambalapitiya -- central, with good schools.",
    5: "Havelock Town, Narahenpita, Thimbirigasyaya and Kirula Lane -- family-friendly residential areas near Havelock City, schools, hospitals and shopping.",
    6: "Wellawatte and Hampden Lane -- close to the beach, with strong transport links, shopping and restaurants.",
    7: "Cinnamon Gardens, Barnes Place and Rosmead Place -- prestigious and leafy, close to embassies and leading schools.",
    8: "Borella, Cotta Road and Castle Street -- established urban residential areas near hospitals, schools and major roads.",
    10: "Maradana -- a commercial district.",
}


def _int(text):
    m = re.search(r"[\d,]+", text or "")
    return int(m.group(0).replace(",", "")) if m else None


def parse_location(loc):
    """-> (zone, area, street). Zone is the Colombo postal number."""
    loc = loc.strip()
    m = re.search(r"Colombo\s+(\d+)", loc)
    zone = int(m.group(1)) if m else None
    before = loc[:m.start()].strip().rstrip(",").strip() if m else loc
    parts = [p.strip() for p in before.split(",") if p.strip()]
    street = parts[0] if parts else None
    area = parts[-1] if len(parts) > 1 else (parts[0] if parts else "")
    return zone, area, street


def parse_type_beds(text):
    """'Apartment 3BR/2BA' | 'Building 4BA' | 'Office space -' -> (type, br, ba)."""
    text = text.strip()
    ptype = None
    for label in ("Office space", "Apartment", "House", "Building"):
        if text.startswith(label):
            ptype = TYPE_MAP[label]
            rest = text[len(label):].strip()
            break
    else:
        raise ValueError(f"unknown property type in {text!r}")
    br = ba = None
    mbr = re.search(r"(\d+)BR", rest)
    if mbr:
        br = int(mbr.group(1))
    mba = re.search(r"(\d+(?:\.\d+)?)BA", rest)
    if mba:
        ba = float(mba.group(1))
    return ptype, br, ba


def parse_size(text):
    """-> (floor_area_sqft, land_perches)."""
    sqft = perch = None
    m = re.search(r"([\d,]+)\s*sq\.?\s*ft", text, re.I)
    if m:
        sqft = int(m.group(1).replace(",", ""))
    m = re.search(r"([\d,]+)\s*perches", text, re.I)
    if m:
        perch = int(m.group(1).replace(",", ""))
    return sqft, perch


def parse_price(text):
    """-> (rent_amount, rent_period, rent_on_request).

    POR means price on request. Where both Rs and $ appear, the Rs figure is
    authoritative -- the dollar figure is an approximate conversion.
    """
    text = text.strip()
    if text.upper() == "POR" or text == "-":
        return None, None, True
    m = re.search(r"Rs\.?\s*([\d,]+)", text)
    if not m:
        return None, None, True
    amount = float(m.group(1).replace(",", ""))
    period = "day" if re.search(r"per\s+day", text, re.I) else "month"
    return amount, period, False


def parse_terms(text):
    """'dep 2 Months, adv 3 Months, min 1 Years, 1p' -> dict."""
    out = {"deposit_months": None, "advance_months": None,
           "min_lease_months": None, "parking": None}
    if not text or text.strip() == "-":
        return out
    m = re.search(r"dep\s+(\d+)\s*Month", text, re.I)
    if m:
        out["deposit_months"] = int(m.group(1))
    m = re.search(r"adv\s+(\d+)\s*Month", text, re.I)
    if m:
        out["advance_months"] = int(m.group(1))
    m = re.search(r"min\s+(\d+)\s*Year", text, re.I)
    if m:
        out["min_lease_months"] = int(m.group(1)) * 12
    else:
        m = re.search(r"min\s+(\d+)\s*Month", text, re.I)
        if m:
            out["min_lease_months"] = int(m.group(1))
    m = re.search(r"(\d+)p\b", text)
    if m:
        out["parking"] = int(m.group(1))
    return out


def parse_availability(text):
    t = (text or "").strip().lower()
    if "soon" in t:
        return "soon"
    if "rented" in t:
        return "rented"
    return "now"


def parse_features(text):
    if not text or text.strip() == "-":
        return []
    return [f.strip() for f in text.split(",") if f.strip() and f.strip() != "-"]


def assert_commentary_is_type_free():
    """Neighbourhood colour must name no property TYPE.

    The commentary is appended to each listing's generated paragraph. A type
    noun in it competes with the row's real type: zone 3 once read "sea-facing
    apartments", which made P05 -- a house -- classify as an apartment, and
    zone 2's "central offices" read as commercial. Colour describes the
    neighbourhood, never the product.
    """
    # Import-time only, and only from this guard: the importer itself must stay
    # runnable as a bare script. HERE is the project root (parent of kb/).
    if str(HERE) not in sys.path:
        sys.path.insert(0, str(HERE))
    from kb.query_parser import classify_type
    bad = {z: classify_type(t.lower()) for z, t in ZONE_COLOUR.items()
           if classify_type(t.lower()) is not None}
    if bad:
        raise SystemExit(
            "commentary names a property type, which would override the row's "
            f"own type: {bad}")


def build_rows():
    rows, notes = [], []
    for line in SOURCE.read_text(encoding="utf-8").splitlines():
        if not re.match(r"^P\d{2}\s*\|", line):
            continue
        cols = [c.strip() for c in line.split("|")]
        pid = cols[0]
        if pid in DROP:
            notes.append(f"{pid}: DROPPED (conflicting prices / implausible size)")
            continue
        if pid in EXCLUDE:
            notes.append(f"{pid}: EXCLUDED (marked Rented)")
            continue

        title = cols[1]
        zone, area, street = parse_location(cols[2])
        ptype, br, ba = parse_type_beds(cols[3])
        sqft, perch = parse_size(cols[4])
        amount, period, on_request = parse_price(cols[5])
        furnishing = FURNISH_MAP.get(cols[6].strip())
        available = parse_availability(cols[7])
        terms = parse_terms(cols[8] if len(cols) > 8 else "")
        features = parse_features(cols[9] if len(cols) > 9 else "")

        rows.append({
            "id": pid,
            "transaction": "rent",
            "property_type": ptype,
            "zone": zone,
            "area": area,
            "building": BUILDINGS.get(title),
            "street": street,
            "bedrooms": br,
            "bathrooms": ba,
            "rent_amount": amount,
            "rent_period": period,
            "rent_on_request": on_request,
            "furnishing": furnishing,
            "floor_area_sqft": sqft,
            "land_perches": perch,
            "parking": terms["parking"],
            "deposit_months": terms["deposit_months"],
            "advance_months": terms["advance_months"],
            "min_lease_months": terms["min_lease_months"],
            "available": available,
            "key_features": features,
            "commentary": ZONE_COLOUR.get(zone, ""),
        })
    return rows, notes


def main():
    assert_commentary_is_type_free()
    rows, notes = build_rows()
    dry = "--dry-run" in sys.argv

    import collections
    print(f"rows: {len(rows)}")
    for key in ("property_type", "zone", "bedrooms", "furnishing", "available"):
        c = collections.Counter(r[key] for r in rows)
        print(f"  {key}: {dict(sorted(c.items(), key=lambda kv: (kv[0] is None, kv[0])))}")
    priced = [r for r in rows if not r["rent_on_request"]]
    print(f"  priced: {len(priced)} | on request: {len(rows) - len(priced)}")
    if priced:
        lo = min(priced, key=lambda r: r["rent_amount"])
        hi = max(priced, key=lambda r: r["rent_amount"])
        print(f"  cheapest: {lo['id']} Rs {lo['rent_amount']:,.0f}/{lo['rent_period']}")
        print(f"  dearest:  {hi['id']} Rs {hi['rent_amount']:,.0f}/{hi['rent_period']}")
    print(f"  per-day rents: {[r['id'] for r in rows if r['rent_period'] == 'day']}")
    for n in notes:
        print(f"  NOTE {n}")

    if dry:
        return

    doc = {
        "_comment": [
            "SOURCE OF TRUTH for the rental portfolio. Typed rows in, typed rows stored.",
            "The prose the agent speaks from is GENERATED from this by kb/prose.py -- it is",
            "a derived artifact. Nothing parses prose back into fields.",
            "",
            "Imported 2026-07-30 from knowledge_docs/_source_feed_2026-07-30.txt (frozen)",
            "by kb/import_feed_2026_07_30.py. P01 dropped (two conflicting prices, and a",
            "750,000 sq.ft. floor area on 60 perches of land); P04 excluded (marked Rented).",
            "",
            "P51-P62 are SYNTHETIC demo listings. Everything else is real agency inventory.",
            "Photo URLs from the feed are deliberately NOT stored: this is a voice agent and",
            "the KB tells callers a salesperson sends photos on follow-up.",
            "",
            "Rents marked rent_on_request have NO published price -- the agent must say so",
            "rather than estimate. P03 is priced PER DAY, not per month.",
        ],
        "preamble_file": "flico_preamble.txt",
        "listings": rows,
    }
    TARGET.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n",
                      encoding="utf-8")
    print(f"wrote {TARGET}")


if __name__ == "__main__":
    main()
