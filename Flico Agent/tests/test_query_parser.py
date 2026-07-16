from kb.query_parser import QueryParser
from kb.schema import QueryFilters


def test_type_and_zone_digit():
    _, f = QueryParser.parse("a 3 bedroom apartment in colombo 7")
    assert f.property_type == "apartment"
    assert f.zone == 7


def test_stt_mishearing_columbo():
    _, f = QueryParser.parse("show me houses in columbus 5")
    assert f.property_type == "house"
    assert f.zone == 5


def test_area_name_to_zone():
    _, f = QueryParser.parse("something in kollupitiya")
    assert f.zone == 3


def test_havelock_mishearing():
    _, f = QueryParser.parse("an apartment near havoc town")
    assert f.zone == 5


def test_spelled_out_zone():
    _, f = QueryParser.parse("apartment in colombo five")
    assert f.zone == 5


def test_occupancy_is_not_bedrooms():
    _, f = QueryParser.parse("we are 4 people looking for an apartment")
    assert f.min_bedrooms is None


def test_explicit_bedrooms_is_a_filter():
    _, f = QueryParser.parse("a 3 bedroom apartment")
    assert f.min_bedrooms == 3


def test_spoken_bedroom_count_is_a_filter():
    # Callers say "two bedroom" and STT transcribes the word, not a digit.
    # A digit-only match let a 2-bedroom request surface 1-bedroom units.
    _, f = QueryParser.parse("i need a two bedroom apartment")
    assert f.min_bedrooms == 2


def test_spoken_occupancy_is_still_not_bedrooms():
    _, f = QueryParser.parse("a place for the four of us")
    assert f.min_bedrooms is None


def test_rent_range_month():
    _, f = QueryParser.parse("apartment under 500k a month")
    assert f.max_rent == 500000.0


def test_commercial_before_house():
    _, f = QueryParser.parse("office space in colombo 1")
    assert f.property_type == "commercial"


def test_sticky_inherits_type_when_only_zone_stated():
    sticky = {"property_type": "apartment", "zone": 7}
    _, f = QueryParser.parse("actually I'd love colombo 5")
    merged = QueryParser.merge_sticky(f, sticky)
    assert merged.property_type == "apartment"
    assert merged.zone == 5
    assert sticky["zone"] == 5  # updated in place


def test_sticky_this_turn_overrides():
    sticky = {"property_type": "apartment", "zone": 7}
    _, f = QueryParser.parse("show me a house instead")
    merged = QueryParser.merge_sticky(f, sticky)
    assert merged.property_type == "house"
