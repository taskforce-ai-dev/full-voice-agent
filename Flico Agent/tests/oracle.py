"""An independent definition of "correct" for the filter layer.

Written from the retrieval spec in plain Python over tests/truth_table.py. It
imports NOTHING from kb/. Exhaustive tests diff kb's SQL against this; if this
were derived from database.py it would only prove the code equals itself.

THE SPEC THIS IMPLEMENTS (the part a human must sign off on):
  1. property_type, if stated, is exact.
  2. zone, if stated, is exact. Zone is NEVER relaxed.
  3. bedrooms: a plain count is EXACT; "at least N"/"N or more"/"N+" is a floor.
  4. max_rent: "under/below/less than" is EXCLUSIVE (<); "up to/max/budget"
     INCLUSIVE (<=). Rows with no quoted rent (on request) survive a ceiling,
     because the caller is told to ask rather than being hidden the listing.
  5. min_rent is inclusive (>=). Rows with no quoted rent do NOT survive a floor.
  6. Constraints conjoin: a row must satisfy all stated constraints.
"""


def satisfies(row, *, property_type=None, zone=None, bedrooms=None,
              min_bedrooms=None, max_rent=None, max_rent_exclusive=False,
              min_rent=None):
    if property_type is not None and row["type"] != property_type:
        return False
    if zone is not None and row["zone"] != zone:
        return False
    if bedrooms is not None and row["beds"] != bedrooms:
        return False
    if min_bedrooms is not None and row["beds"] < min_bedrooms:
        return False

    rent = row.get("rent")  # None == quoted on request
    if max_rent is not None and rent is not None:
        if rent >= max_rent if max_rent_exclusive else rent > max_rent:
            return False
    if min_rent is not None:
        if rent is None or rent < min_rent:
            return False
    return True


def expected_ids(rows, **constraints):
    return sorted(r["id"] for r in rows if satisfies(r, **constraints))
