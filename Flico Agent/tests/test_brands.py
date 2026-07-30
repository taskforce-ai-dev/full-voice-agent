"""The brand registry: one agent process, more than one storefront.

Imports `brands` only — never `server`. The deploy gate installs just
pytest/numpy/pydantic, so a test that imports server is skipped there and
cannot protect anything.
"""
import pytest

import brands


def test_default_brand_is_rodrigo():
    # Rodrigo is the real client on the real phone number. If the default ever
    # changes, that number silently rebrands.
    assert brands.DEFAULT_BRAND == "rodrigo"


def test_rodrigo_identity():
    b = brands.BRANDS["rodrigo"]
    assert b["agency"] == "Rodrigo Realtors"
    assert b["agent"] == "Fiona"


def test_startproperty_identity():
    b = brands.BRANDS["startproperty"]
    assert b["agency"] == "Start Property"
    assert b["agent"] == "Amaya"


@pytest.mark.parametrize("brand,expected", [("rodrigo", True), ("startproperty", False)])
def test_transfer_flag(brand, expected):
    # Amaya has no human consultant behind her; she must not offer a transfer.
    assert brands.BRANDS[brand]["transfer"] is expected


def test_every_brand_has_an_english_greeting():
    # _build_system_prompt falls back to ["en"], so its absence is a KeyError
    # at call time on a live call.
    for name, b in brands.BRANDS.items():
        assert b["greeting"]["en"].strip(), name


def test_every_brand_greeting_names_its_own_agency():
    # A greeting naming the wrong agency is the exact bug this registry exists
    # to prevent.
    for name, b in brands.BRANDS.items():
        assert b["agency"] in b["greeting"]["en"], name


def test_every_brand_declares_all_keys():
    for name, b in brands.BRANDS.items():
        assert set(b) == {"agency", "agent", "transfer", "greeting"}, name


@pytest.mark.parametrize("value", ["nope", "", "   ", None, "RODRIGO"])
def test_resolve_brand_falls_back_or_normalizes(value):
    got = brands.resolve_brand(value)
    if value and value.strip().lower() in brands.BRANDS:
        assert got is brands.BRANDS[value.strip().lower()]
    else:
        assert got is brands.BRANDS[brands.DEFAULT_BRAND]


def test_resolve_brand_returns_the_startproperty_config():
    assert brands.resolve_brand("startproperty") is brands.BRANDS["startproperty"]
