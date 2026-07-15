from kb.formatter import ContextFormatter
from kb.schema import Property


def _p(**kw):
    base = dict(id="P1", transaction="rent", property_type="apartment", zone=7,
                area="Cinnamon Gardens", bedrooms=3, bathrooms=2.0, description="")
    base.update(kw)
    return Property(**base)


def test_empty_returns_empty_string():
    assert ContextFormatter.format([]) == ""


def test_period_stated_verbatim_per_day():
    out = ContextFormatter.format([_p(rent_amount=15000.0, rent_period="day")])
    assert "per day" in out
    assert "per month" not in out


def test_rent_on_request_marked():
    out = ContextFormatter.format([_p(rent_on_request=True, rent_amount=None, rent_period=None)])
    assert "on request" in out.lower()


def test_includes_zone_and_type():
    out = ContextFormatter.format([_p()])
    assert "Colombo 7" in out
    assert "apartment" in out
