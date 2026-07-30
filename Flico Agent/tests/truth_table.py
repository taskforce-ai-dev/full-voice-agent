"""The portfolio as plain data — the independent oracle's source of truth.

Transcribed BY HAND from knowledge_docs/_source_feed_2026-07-30.txt (the frozen
operator paste), NOT from listings.json or the importer that generated it. It
deliberately does NOT import kb/: if the oracle were derived from the same
parser it verifies, the two would agree on their shared mistakes and prove
nothing. tests/test_truth_crosscheck.py asserts kb.migrate agrees with this
table, which simultaneously proves the prose parser and pins the prose to
something a human audited.

Sixty rows, audited against the raw feed line by line on 2026-07-30.
Deliberately absent, per the operator rulings recorded in the feed header:
  - P01: DROPPED — its two stated prices disagree (title Rs 170,000/mo vs price
    field Rs 150,000/mo) and 750,000 sq.ft. on 60 perches is physically
    implausible.
  - P04: EXCLUDED — marked "Rented", not available.

Field conventions (what a careful human reading the feed records):
  - type: Apartment -> "apartment", House -> "house", and BOTH "Office space"
    and "Building" -> "commercial".
  - zone: the Colombo postal number from the location column.
  - beds/baths: from the NBR/NBA notation. Commercial entries often state no
    BR -> None. P30 and P35 state no BA either -> None.
  - rent: the numeric Rupee amount. POR (price on request) -> None. Where both
    Rs and $ figures appear, the Rs figure is authoritative.
  - period: the stated rent period. "month" everywhere a rent is quoted EXCEPT
    P03, which the feed prices "Rs. 15,000 Per Day" -> "day". POR rows state no
    period -> None. Any consumer comparing rents against a monthly budget must
    not treat P03's 15,000 as a monthly figure.
  - furnishing: Furnished -> "furnished", Semi-furnished -> "semi",
    Unfurnished -> "unfurnished", "-" -> None.
  - sqft: the floor area. The land-perches figure is ignored.
"""

# id, type, zone, beds, baths, rent, period, furnishing, sqft
TRUTH = [
    {"id": "P02", "type": "apartment",  "zone": 5,  "beds": 3,    "baths": 2,    "rent": None,    "period": None,    "furnishing": "furnished",   "sqft": 1442},
    {"id": "P03", "type": "apartment",  "zone": 4,  "beds": 3,    "baths": 3,    "rent": 15000,   "period": "day",   "furnishing": "furnished",   "sqft": 1300},
    {"id": "P05", "type": "house",      "zone": 3,  "beds": 4,    "baths": 4,    "rent": None,    "period": None,    "furnishing": "furnished",   "sqft": 12000},
    {"id": "P06", "type": "apartment",  "zone": 2,  "beds": 4,    "baths": 4,    "rent": None,    "period": None,    "furnishing": "furnished",   "sqft": 2286},
    {"id": "P07", "type": "apartment",  "zone": 1,  "beds": 4,    "baths": 3,    "rent": None,    "period": None,    "furnishing": "furnished",   "sqft": 2745},
    {"id": "P08", "type": "house",      "zone": 7,  "beds": 4,    "baths": 3,    "rent": None,    "period": None,    "furnishing": "unfurnished", "sqft": 2000},
    {"id": "P09", "type": "apartment",  "zone": 1,  "beds": 4,    "baths": 3,    "rent": None,    "period": None,    "furnishing": "semi",        "sqft": 3477},
    {"id": "P10", "type": "apartment",  "zone": 1,  "beds": 4,    "baths": 3,    "rent": None,    "period": None,    "furnishing": "unfurnished", "sqft": 3500},
    {"id": "P11", "type": "apartment",  "zone": 2,  "beds": 3,    "baths": 2,    "rent": None,    "period": None,    "furnishing": "unfurnished", "sqft": 1000},
    {"id": "P12", "type": "apartment",  "zone": 2,  "beds": 3,    "baths": 3,    "rent": None,    "period": None,    "furnishing": "furnished",   "sqft": 2096},
    {"id": "P13", "type": "apartment",  "zone": 2,  "beds": 3,    "baths": 3,    "rent": None,    "period": None,    "furnishing": "furnished",   "sqft": 1690},
    {"id": "P14", "type": "apartment",  "zone": 8,  "beds": 3,    "baths": 2,    "rent": None,    "period": None,    "furnishing": "furnished",   "sqft": 1840},
    {"id": "P15", "type": "apartment",  "zone": 3,  "beds": 3,    "baths": 3,    "rent": 600000,  "period": "month", "furnishing": "unfurnished", "sqft": 2138},
    {"id": "P16", "type": "house",      "zone": 5,  "beds": 4,    "baths": 4,    "rent": 1100000, "period": "month", "furnishing": "unfurnished", "sqft": 6500},
    {"id": "P17", "type": "apartment",  "zone": 7,  "beds": 3,    "baths": 2,    "rent": None,    "period": None,    "furnishing": "furnished",   "sqft": 1423},
    {"id": "P18", "type": "apartment",  "zone": 3,  "beds": 3,    "baths": 3,    "rent": 775000,  "period": "month", "furnishing": "furnished",   "sqft": 2600},
    {"id": "P19", "type": "apartment",  "zone": 2,  "beds": 4,    "baths": 3,    "rent": 2645000, "period": "month", "furnishing": "furnished",   "sqft": 2750},
    {"id": "P20", "type": "apartment",  "zone": 3,  "beds": 4,    "baths": 4,    "rent": 1200000, "period": "month", "furnishing": "furnished",   "sqft": 2500},
    {"id": "P21", "type": "apartment",  "zone": 2,  "beds": 3,    "baths": 3,    "rent": 625000,  "period": "month", "furnishing": "furnished",   "sqft": 1275},
    {"id": "P22", "type": "house",      "zone": 5,  "beds": 4,    "baths": 4,    "rent": 1128000, "period": "month", "furnishing": "furnished",   "sqft": 4500},
    {"id": "P23", "type": "apartment",  "zone": 1,  "beds": 3,    "baths": 3,    "rent": 750000,  "period": "month", "furnishing": "furnished",   "sqft": 2300},
    {"id": "P24", "type": "apartment",  "zone": 7,  "beds": 4,    "baths": 4,    "rent": 1200000, "period": "month", "furnishing": "furnished",   "sqft": 3800},
    {"id": "P25", "type": "apartment",  "zone": 2,  "beds": 4,    "baths": 4,    "rent": 1260030, "period": "month", "furnishing": "furnished",   "sqft": 2321},
    {"id": "P26", "type": "apartment",  "zone": 2,  "beds": 3,    "baths": 2,    "rent": 150000,  "period": "month", "furnishing": "unfurnished", "sqft": 1200},
    {"id": "P27", "type": "apartment",  "zone": 7,  "beds": 3,    "baths": 2,    "rent": None,    "period": None,    "furnishing": "furnished",   "sqft": 1428},
    {"id": "P28", "type": "commercial", "zone": 5,  "beds": None, "baths": 4,    "rent": 600000,  "period": "month", "furnishing": None,          "sqft": 6000},
    {"id": "P29", "type": "commercial", "zone": 10, "beds": None, "baths": 2,    "rent": 1000000, "period": "month", "furnishing": None,          "sqft": 7928},
    {"id": "P30", "type": "commercial", "zone": 3,  "beds": None, "baths": None, "rent": 125000,  "period": "month", "furnishing": None,          "sqft": 1050},
    {"id": "P31", "type": "house",      "zone": 8,  "beds": 5,    "baths": 2,    "rent": 250000,  "period": "month", "furnishing": None,          "sqft": 3100},
    {"id": "P32", "type": "apartment",  "zone": 7,  "beds": 3,    "baths": 2,    "rent": 250000,  "period": "month", "furnishing": None,          "sqft": 1460},
    {"id": "P33", "type": "commercial", "zone": 2,  "beds": None, "baths": 10,   "rent": 6000000, "period": "month", "furnishing": None,          "sqft": 33100},
    {"id": "P34", "type": "commercial", "zone": 3,  "beds": None, "baths": 2,    "rent": 200000,  "period": "month", "furnishing": None,          "sqft": 1600},
    {"id": "P35", "type": "commercial", "zone": 3,  "beds": None, "baths": None, "rent": 750000,  "period": "month", "furnishing": None,          "sqft": 4500},
    {"id": "P36", "type": "commercial", "zone": 5,  "beds": None, "baths": 1,    "rent": 195000,  "period": "month", "furnishing": None,          "sqft": 1400},
    {"id": "P37", "type": "apartment",  "zone": 7,  "beds": 3,    "baths": 2,    "rent": 576000,  "period": "month", "furnishing": "furnished",   "sqft": 1950},
    {"id": "P38", "type": "apartment",  "zone": 3,  "beds": 3,    "baths": 3,    "rent": 500000,  "period": "month", "furnishing": "semi",        "sqft": 2400},
    {"id": "P39", "type": "apartment",  "zone": 6,  "beds": 3,    "baths": 2,    "rent": None,    "period": None,    "furnishing": "furnished",   "sqft": 1100},
    {"id": "P40", "type": "house",      "zone": 5,  "beds": 5,    "baths": 4,    "rent": None,    "period": None,    "furnishing": "unfurnished", "sqft": 5000},
    {"id": "P41", "type": "house",      "zone": 5,  "beds": 4,    "baths": 4,    "rent": None,    "period": None,    "furnishing": "furnished",   "sqft": 5500},
    {"id": "P42", "type": "house",      "zone": 5,  "beds": 3,    "baths": 3,    "rent": None,    "period": None,    "furnishing": "furnished",   "sqft": 5500},
    {"id": "P43", "type": "apartment",  "zone": 2,  "beds": 3,    "baths": 2,    "rent": None,    "period": None,    "furnishing": "furnished",   "sqft": 1627},
    {"id": "P44", "type": "apartment",  "zone": 8,  "beds": 3,    "baths": 3,    "rent": 300000,  "period": "month", "furnishing": "semi",        "sqft": 3100},
    {"id": "P45", "type": "apartment",  "zone": 7,  "beds": 4,    "baths": 3,    "rent": None,    "period": None,    "furnishing": "furnished",   "sqft": 2686},
    {"id": "P46", "type": "apartment",  "zone": 1,  "beds": 3,    "baths": 3,    "rent": None,    "period": None,    "furnishing": "furnished",   "sqft": 2271},
    {"id": "P47", "type": "apartment",  "zone": 2,  "beds": 3,    "baths": 3,    "rent": None,    "period": None,    "furnishing": "furnished",   "sqft": 2200},
    {"id": "P48", "type": "apartment",  "zone": 2,  "beds": 3,    "baths": 2,    "rent": None,    "period": None,    "furnishing": "furnished",   "sqft": 1627},
    {"id": "P49", "type": "apartment",  "zone": 2,  "beds": 3,    "baths": 2,    "rent": None,    "period": None,    "furnishing": "furnished",   "sqft": 1020},
    {"id": "P50", "type": "apartment",  "zone": 3,  "beds": 3,    "baths": 3,    "rent": None,    "period": None,    "furnishing": "furnished",   "sqft": 2550},
    {"id": "P51", "type": "apartment",  "zone": 2,  "beds": 1,    "baths": 1,    "rent": 180000,  "period": "month", "furnishing": "furnished",   "sqft": 620},
    {"id": "P52", "type": "apartment",  "zone": 3,  "beds": 1,    "baths": 1,    "rent": 220000,  "period": "month", "furnishing": "furnished",   "sqft": 700},
    {"id": "P53", "type": "apartment",  "zone": 6,  "beds": 1,    "baths": 1,    "rent": 150000,  "period": "month", "furnishing": "semi",        "sqft": 650},
    {"id": "P54", "type": "apartment",  "zone": 5,  "beds": 2,    "baths": 2,    "rent": 280000,  "period": "month", "furnishing": "furnished",   "sqft": 1100},
    {"id": "P55", "type": "apartment",  "zone": 7,  "beds": 2,    "baths": 2,    "rent": 350000,  "period": "month", "furnishing": "semi",        "sqft": 1250},
    {"id": "P56", "type": "apartment",  "zone": 2,  "beds": 2,    "baths": 2,    "rent": 300000,  "period": "month", "furnishing": "furnished",   "sqft": 1050},
    {"id": "P57", "type": "house",      "zone": 5,  "beds": 1,    "baths": 1,    "rent": 160000,  "period": "month", "furnishing": "furnished",   "sqft": 850},
    {"id": "P58", "type": "house",      "zone": 8,  "beds": 1,    "baths": 1,    "rent": 140000,  "period": "month", "furnishing": "semi",        "sqft": 900},
    {"id": "P59", "type": "house",      "zone": 6,  "beds": 1,    "baths": 1,    "rent": 130000,  "period": "month", "furnishing": "unfurnished", "sqft": 750},
    {"id": "P60", "type": "house",      "zone": 5,  "beds": 2,    "baths": 2,    "rent": 260000,  "period": "month", "furnishing": "semi",        "sqft": 1600},
    {"id": "P61", "type": "house",      "zone": 7,  "beds": 2,    "baths": 2,    "rent": 400000,  "period": "month", "furnishing": "furnished",   "sqft": 1800},
    {"id": "P62", "type": "house",      "zone": 8,  "beds": 2,    "baths": 2,    "rent": 220000,  "period": "month", "furnishing": "unfurnished", "sqft": 1500},
]

ALL_IDS = sorted(r["id"] for r in TRUTH)
ZONES = sorted({r["zone"] for r in TRUTH})
TYPES = sorted({r["type"] for r in TRUTH})
# Commercial rows state no bedroom count (beds=None); POR rows have rent=None.
# The derived vocabularies below cover only the STATED values -- None is a
# semantic ("not stated" / "on request"), not a member of either domain.
BEDROOMS = sorted({r["beds"] for r in TRUTH if r["beds"] is not None})
RENTS = sorted({r["rent"] for r in TRUTH if r["rent"] is not None})


def rent_equivalence_grid():
    """Every threshold worth testing.

    SQL rent comparisons are monotone, so between two adjacent distinct rents
    every threshold behaves identically. Testing each distinct rent, +/-1 rupee,
    and the midpoints is therefore EXHAUSTIVE over all real-valued thresholds --
    this is what makes "100%" a claim about the whole space, not a sample.

    Rows with rent=None (price on request) are outside this grid by design:
    no threshold interacts with them numerically; their inclusion/exclusion
    under a ceiling/floor is a fixed rule tested at every grid point anyway.
    """
    grid = set()
    for r in RENTS:
        grid.update({r - 1, r, r + 1})
    for a, b in zip(RENTS, RENTS[1:]):
        grid.add((a + b) // 2)
    grid.update({0, min(RENTS) - 100000, max(RENTS) + 100000})
    return sorted(grid)
