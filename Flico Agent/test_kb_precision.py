"""Precision tests for KB metadata tagging + type/zone query filtering.

Run inside the Flico container (has chromadb + sentence-transformers + a built
collection):  docker exec flico-voice-agent python /app/test_kb_precision.py

Exit code 0 = all pass, 1 = failures.
"""
import sys

import knowledge_base as kb

PASS = 0
FAIL = 0
FAILURES = []


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        FAILURES.append(f"{name} :: {detail}")
        print(f"  FAIL  {name}  -- {detail}")


# ---------------------------------------------------------------------------
# 1. _extract_metadata (index-time tagging of a single chunk)
# ---------------------------------------------------------------------------
print("\n[_extract_metadata]")
EM = getattr(kb, "_extract_metadata", None)
if EM is None:
    check("_extract_metadata exists", False, "function missing")
else:
    cases = [
        ("Rodrigo Realtors has a 3-bedroom, 3-bathroom semi-furnished apartment for rent in Colombo 3 (Kollupitiya), with a floor area of 2,400 square feet.",
         "apartment", "3"),
        ("Rodrigo Realtors has office space for urgent rent in Colombo 3 (Kollupitiya), on 60 perches of land with 10 bathrooms.",
         "commercial", "3"),
        ("Rodrigo Realtors has a 4-bedroom, 4-bathroom furnished luxury house for rent in Colombo 3 (Kollupitiya), with a floor area of 12,000 square feet.",
         "house", "3"),
        # 'house-type office space' must classify as commercial, NOT house
        ("Rodrigo Realtors has a 7,928-square-foot commercial property (house-type office space) for rent on Maligawatte Road, Colombo 10 (Maradana), with 2 bathrooms.",
         "commercial", "10"),
        ("Rodrigo Realtors has 1,400 square feet of house-type office space for rent in Colombo 5 (Havelock Town), with 1 bathroom.",
         "commercial", "5"),
        # info chunks must NOT get a property_type or a single zone
        ("Rodrigo Realtors is a trusted Sri Lankan real estate agency specialising in rental properties across the City of Colombo.",
         "info", ""),
        ("Colombo 1 (Fort and Galle Face) is the corporate hub. Colombo 2 (Slave Island) is home to luxury towers. Colombo 7 (Cinnamon Gardens) is the most prestigious.",
         "info", ""),
    ]
    for text, exp_type, exp_zone in cases:
        m = EM(text)
        check(f"type={exp_type} for: {text[:48]}...", m.get("property_type") == exp_type,
              f"got {m.get('property_type')}")
        check(f"zone={exp_zone!r} for: {text[:48]}...", str(m.get("zone", "")) == exp_zone,
              f"got {m.get('zone')!r}")

# ---------------------------------------------------------------------------
# 2. _parse_query_filters (query-time intent parsing)
# ---------------------------------------------------------------------------
print("\n[_parse_query_filters]")
PQ = getattr(kb, "_parse_query_filters", None)
if PQ is None:
    check("_parse_query_filters exists", False, "function missing")
else:
    qcases = [
        ("apartment for rent in Colombo 3", "apartment", "3"),
        ("do you have any houses in Colombo 7", "house", "7"),
        ("office space in colombo 2", "commercial", "2"),
        ("I'm looking at apartments in the Colombo three area", "apartment", "3"),
        ("what do you have available", None, None),
        ("anything in Kollupitiya", None, "3"),
        ("apartments in Cinnamon Gardens", "apartment", "7"),
        ("commercial property", "commercial", None),
        ("a flat in colombo 5", "apartment", "5"),
    ]
    for q, et, ez in qcases:
        t, z = PQ(q)
        check(f"type for {q!r}", t == et, f"got {t!r} exp {et!r}")
        check(f"zone for {q!r}", z == ez, f"got {z!r} exp {ez!r}")

# ---------------------------------------------------------------------------
# 3. Integration: retrieve_context must return only matching type+zone
# ---------------------------------------------------------------------------
print("\n[retrieve_context precision]")


def listing_blocks(formatted):
    """Yield (property_type, zone) for each LISTING block in a result string."""
    out = []
    for block in formatted.split("\n\n---\n\n"):
        # strip the [Source: ...] header line
        body = block.split("\n", 1)[-1].strip()
        if body.lstrip().startswith("Rodrigo Realtors has"):
            m = kb._extract_metadata(body) if hasattr(kb, "_extract_metadata") else {}
            out.append((m.get("property_type"), str(m.get("zone", ""))))
    return out


def precision_case(query, exp_type, exp_zone):
    r = kb.retrieve_context(query) or ""
    blocks = listing_blocks(r)
    # must return at least one listing
    check(f"{query!r} returns listings", len(blocks) > 0, f"{len(blocks)} listings")
    bad = [b for b in blocks if (exp_type and b[0] != exp_type) or (exp_zone and b[1] != exp_zone)]
    check(f"{query!r} all match type={exp_type} zone={exp_zone}", not bad,
          f"leaks: {bad}")


precision_case("apartment for rent in Colombo 3", "apartment", "3")
precision_case("office space in Colombo 2", "commercial", "2")
precision_case("houses for rent in Colombo 5", "house", "5")
precision_case("3 bedroom apartment in Colombo 2", "apartment", "2")
precision_case("apartments in Colombo 7", "apartment", "7")

# negative: a zone we do not cover should not crash
try:
    r = kb.retrieve_context("apartment in Colombo 9") or ""
    check("Colombo 9 query does not crash", True)
except Exception as exc:  # noqa
    check("Colombo 9 query does not crash", False, str(exc))

print(f"\n==== {PASS} passed, {FAIL} failed ====")
if FAIL:
    print("FAILURES:")
    for f in FAILURES:
        print("  -", f)
sys.exit(1 if FAIL else 0)
