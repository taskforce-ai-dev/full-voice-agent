from kb.migrate import parse_prose

SAMPLE = """RODRIGO REALTORS — RENTAL PROPERTY KNOWLEDGE BASE

Star Properties is a trusted Sri Lankan real estate agency.

Star Properties has a 3-bedroom, 3-bathroom furnished apartment for rent at Adamaly Place in Colombo 4 (Bambalapitiya), with a floor area of 1,300 square feet, at a rent of fifteen thousand rupees (Rs 15,000) per day. Lease terms are a 3-month deposit, 3 months' advance, and a minimum lease of 1 year, with one parking space. (Ref: P03)

Star Properties has a 3-bedroom, 2-bathroom furnished apartment for rent at Havelock City in Colombo 5 (Havelock Town), with a floor area of 1,442 square feet. The monthly rent is available on request. (Ref: P02)
"""


def test_preamble_captures_non_listing():
    _, _, preamble = parse_prose(SAMPLE)
    assert "trusted Sri Lankan real estate agency" in preamble
    assert "Ref: P03" not in preamble


def test_two_rows_parsed():
    rows, skipped, _ = parse_prose(SAMPLE)
    assert {r.id for r in rows} == {"P03", "P02"}
    assert skipped == []


def test_per_day_period_preserved():
    rows, _, _ = parse_prose(SAMPLE)
    p03 = next(r for r in rows if r.id == "P03")
    assert p03.rent_period == "day"
    assert p03.rent_amount == 15000.0


def test_on_request_flagged():
    rows, _, _ = parse_prose(SAMPLE)
    p02 = next(r for r in rows if r.id == "P02")
    assert p02.rent_on_request is True
    assert p02.rent_amount is None


def test_zone_and_fields():
    rows, _, _ = parse_prose(SAMPLE)
    p03 = next(r for r in rows if r.id == "P03")
    assert p03.zone == 4
    assert p03.bedrooms == 3
    assert p03.furnishing == "furnished"
    assert p03.floor_area_sqft == 1300
    assert p03.parking == 1
