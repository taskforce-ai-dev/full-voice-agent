"""An independent definition of "correct" for the filter layer.

Written from the retrieval spec in plain Python over tests/truth_table.py. It
imports NOTHING from kb/. Exhaustive tests diff kb's SQL against this; if this
were derived from database.py it would only prove the code equals itself.

THE SPEC THIS IMPLEMENTS (the part a human must sign off on):
  1. property_type, if stated, is exact.
  2. zone, if stated, is exact. Zone is NEVER relaxed.
  3. bedrooms: a plain count is EXACT; "at least N"/"N or more"/"N+" is a floor.
     A listing with NO stated bedroom count (the commercial rows) never matches
     any bedroom constraint -- a caller asking for "two bedrooms" is not asking
     for an office.
  4. max_rent: "under/below/less than" is EXCLUSIVE (<); "up to/max/budget"
     INCLUSIVE (<=). Rows with no quoted rent (on request) survive a ceiling,
     because the caller is told to ask rather than being hidden the listing.
  5. min_rent is inclusive (>=). Rows with no quoted rent do NOT survive a floor.
  6. Constraints conjoin: a row must satisfy all stated constraints.

DELIBERATE ASYMMETRY, inherited from the shipped SQL and encoded on purpose:
a rent CEILING keeps price-on-request rows (rent_amount <= ? OR rent_amount IS
NULL) while a rent FLOOR drops them. The rationale in rule 4/5: a budget-capped
caller should still hear about a POR listing and be told to ask, whereas a
caller filtering for premium stock ("over a million") gets only rows proven to
qualify. With 24 of 60 rows now POR this asymmetry is load-bearing -- see the
2026-07-30 truth-table report for an assessment of whether it remains right.
"""


def satisfies(row, *, property_type=None, zone=None, bedrooms=None,
              min_bedrooms=None, max_rent=None, max_rent_exclusive=False,
              min_rent=None):
    if property_type is not None and row["type"] != property_type:
        return False
    if zone is not None and row["zone"] != zone:
        return False

    beds = row.get("beds")  # None == no stated bedroom count (commercial rows)
    if bedrooms is not None and beds != bedrooms:
        return False
    if min_bedrooms is not None and (beds is None or beds < min_bedrooms):
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
