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


def test_explicit_bedrooms_is_an_exact_filter():
    # "a 3 bedroom apartment" means three -- not "three or more".
    _, f = QueryParser.parse("a 3 bedroom apartment")
    assert f.bedrooms == 3
    assert f.min_bedrooms is None


def test_spoken_bedroom_count_is_a_filter():
    # Callers say "two bedroom" and STT transcribes the word, not a digit.
    # A digit-only match let a 2-bedroom request surface 1-bedroom units.
    _, f = QueryParser.parse("i need a two bedroom apartment")
    assert f.bedrooms == 2


def test_at_least_is_a_floor():
    for q in ("at least 2 bedrooms", "2 or more bedrooms", "2+ bedrooms",
              "minimum two bedrooms"):
        _, f = QueryParser.parse(q)
        assert f.min_bedrooms == 2, q
        assert f.bedrooms is None, q


def test_spoken_occupancy_is_still_not_bedrooms():
    _, f = QueryParser.parse("a place for the four of us")
    assert f.min_bedrooms is None
    assert f.bedrooms is None


def test_bedroom_count_is_sticky_across_turns():
    sticky = {}
    _, f1 = QueryParser.parse("a two bedroom apartment")
    QueryParser.merge_sticky(f1, sticky)
    # Next turn names only an area -- the bedroom need must survive.
    _, f2 = QueryParser.parse("what about colombo 7")
    QueryParser.merge_sticky(f2, sticky)
    assert f2.bedrooms == 2
    assert f2.property_type == "apartment"


def test_restated_bedroom_count_overrides_sticky():
    sticky = {"bedrooms": 1}
    _, f = QueryParser.parse("actually make it a two bedroom")
    QueryParser.merge_sticky(f, sticky)
    assert f.bedrooms == 2
    assert sticky["bedrooms"] == 2


def test_rent_range_month():
    _, f = QueryParser.parse("apartment under 500k a month")
    assert f.max_rent == 500000.0


def test_under_is_exclusive_but_up_to_is_inclusive():
    # "under 300k" must not offer a listing that costs exactly 300k.
    _, f = QueryParser.parse("an apartment under 300k")
    assert (f.max_rent, f.max_rent_exclusive) == (300000.0, True)
    _, f = QueryParser.parse("an apartment up to 300k")
    assert (f.max_rent, f.max_rent_exclusive) == (300000.0, False)


def test_rent_range_sets_both_bounds():
    for q in ("between 300k and 500k", "from 300 to 500 thousand", "300k-500k"):
        _, f = QueryParser.parse(q)
        assert (f.min_rent, f.max_rent) == (300000.0, 500000.0), q


def test_rent_floor_from_over():
    _, f = QueryParser.parse("something over 300k")
    assert f.min_rent == 300000.0


def test_counts_are_never_parsed_as_money():
    # "2 to 3 bedroom" and "more than 2 bedrooms" must not become a rent range
    # of 2,000-3,000 or a rent floor of 2,000.
    for q in ("a 2 to 3 bedroom apartment", "more than 2 bedrooms"):
        _, f = QueryParser.parse(q)
        assert f.min_rent is None and f.max_rent is None, q


def test_rent_accepts_comma_and_lakh_forms():
    _, f = QueryParser.parse("under Rs 300,000")
    assert f.max_rent == 300000.0
    _, f = QueryParser.parse("budget of 3 lakhs")
    assert f.max_rent == 300000.0


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
