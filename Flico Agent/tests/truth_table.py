"""The portfolio as plain data — the independent oracle's source of truth.

Transcribed BY HAND from knowledge_docs/flico_info.txt. It deliberately does NOT
import kb/: if the oracle were derived from the same parser it verifies, the two
would agree on their shared mistakes and prove nothing. tests/test_truth_crosscheck.py
asserts kb.migrate agrees with this table, which simultaneously proves the prose
parser and pins the prose to something a human audited.

Twelve rows. A five-minute audit against the source. When the real portfolio
lands, regenerate this from the verified sheet and re-sign it.
"""

# id, type, zone, beds, baths, rent, furnishing, sqft
TRUTH = [
    {"id": "P51", "type": "apartment", "zone": 2, "beds": 1, "baths": 1, "rent": 180000, "furnishing": "furnished",   "sqft": 620},
    {"id": "P52", "type": "apartment", "zone": 3, "beds": 1, "baths": 1, "rent": 220000, "furnishing": "furnished",   "sqft": 700},
    {"id": "P53", "type": "apartment", "zone": 6, "beds": 1, "baths": 1, "rent": 150000, "furnishing": "semi",        "sqft": 650},
    {"id": "P54", "type": "apartment", "zone": 5, "beds": 2, "baths": 2, "rent": 280000, "furnishing": "furnished",   "sqft": 1100},
    {"id": "P55", "type": "apartment", "zone": 7, "beds": 2, "baths": 2, "rent": 350000, "furnishing": "semi",        "sqft": 1250},
    {"id": "P56", "type": "apartment", "zone": 2, "beds": 2, "baths": 2, "rent": 300000, "furnishing": "furnished",   "sqft": 1050},
    {"id": "P57", "type": "house",     "zone": 5, "beds": 1, "baths": 1, "rent": 160000, "furnishing": "furnished",   "sqft": 850},
    {"id": "P58", "type": "house",     "zone": 8, "beds": 1, "baths": 1, "rent": 140000, "furnishing": "semi",        "sqft": 900},
    {"id": "P59", "type": "house",     "zone": 6, "beds": 1, "baths": 1, "rent": 130000, "furnishing": "unfurnished", "sqft": 750},
    {"id": "P60", "type": "house",     "zone": 5, "beds": 2, "baths": 2, "rent": 260000, "furnishing": "semi",        "sqft": 1600},
    {"id": "P61", "type": "house",     "zone": 7, "beds": 2, "baths": 2, "rent": 400000, "furnishing": "furnished",   "sqft": 1800},
    {"id": "P62", "type": "house",     "zone": 8, "beds": 2, "baths": 2, "rent": 220000, "furnishing": "unfurnished", "sqft": 1500},
]

ALL_IDS = sorted(r["id"] for r in TRUTH)
ZONES = sorted({r["zone"] for r in TRUTH})
TYPES = sorted({r["type"] for r in TRUTH})
BEDROOMS = sorted({r["beds"] for r in TRUTH})
RENTS = sorted({r["rent"] for r in TRUTH})


def rent_equivalence_grid():
    """Every threshold worth testing.

    SQL rent comparisons are monotone, so between two adjacent distinct rents
    every threshold behaves identically. Testing each distinct rent, +/-1 rupee,
    and the midpoints is therefore EXHAUSTIVE over all real-valued thresholds --
    this is what makes "100%" a claim about the whole space, not a sample.
    """
    grid = set()
    for r in RENTS:
        grid.update({r - 1, r, r + 1})
    for a, b in zip(RENTS, RENTS[1:]):
        grid.add((a + b) // 2)
    grid.update({0, min(RENTS) - 100000, max(RENTS) + 100000})
    return sorted(grid)
