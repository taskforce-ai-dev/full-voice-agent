from kb.schema import Property, QueryFilters


def test_property_minimal_rental():
    p = Property(
        id="P15", transaction="rent", property_type="apartment",
        zone=3, area="Kollupitiya", building="606 The Address",
        bedrooms=3, bathrooms=3.0,
        rent_amount=600000.0, rent_period="month", rent_on_request=False,
        furnishing="unfurnished", floor_area_sqft=2138, parking=1,
        key_features=["sea views", "swimming pool"],
        description="Rodrigo Realtors has a 3-bedroom apartment for rent in Colombo 3.",
    )
    assert p.zone == 3
    assert p.rent_period == "month"
    assert p.rent_on_request is False


def test_property_rent_on_request_allows_null_amount():
    p = Property(
        id="P02", transaction="rent", property_type="apartment", zone=5,
        area="Havelock Town", bedrooms=3, bathrooms=2.0,
        rent_amount=None, rent_period=None, rent_on_request=True,
        description="Rodrigo Realtors has a 3-bedroom apartment for rent in Colombo 5.",
    )
    assert p.rent_amount is None
    assert p.rent_on_request is True


def test_queryfilters_all_optional():
    f = QueryFilters()
    assert f.property_type is None
    assert f.zone is None
    assert f.max_rent is None
