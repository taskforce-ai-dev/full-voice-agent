"""The entry-point registry: one agency, two ways in.

Imports `brands` only — never `server`. The deploy gate installs just
pytest/numpy/pydantic, so a test that imports server is skipped there and
cannot protect anything.

There is one brand now (Star Properties / Amaya). What differs between entry
points is whether a human can be reached: the phone line has HUMAN_AGENT_PHONE
configured, the website demo has nobody behind it.
"""
import pytest

import brands

PHONE = "starproperties"
DEMO = "starproperties_demo"


def test_default_is_the_phone_entry_point():
    # Every call site that passes only `lang` resolves here, and that is the
    # one real inbound callers use. If the default flips to the demo entry
    # point, the live line silently loses its human handoff.
    assert brands.DEFAULT_BRAND == PHONE


def test_exactly_two_entry_points():
    assert set(brands.BRANDS) == {PHONE, DEMO}


@pytest.mark.parametrize("key", [PHONE, DEMO])
def test_both_entry_points_are_star_properties(key):
    # The whole point of the collapse: one agency, one agent name, both ways in.
    b = brands.BRANDS[key]
    assert b["agency"] == "Star Properties"
    assert b["agent"] == "Amaya"


def test_no_trace_of_the_retired_brand():
    blob = repr(brands.BRANDS)
    assert "Rodrigo" not in blob
    assert "Fiona" not in blob


@pytest.mark.parametrize("key,expected", [(PHONE, True), (DEMO, False)])
def test_transfer_is_a_property_of_the_entry_point(key, expected):
    # The demo has no consultant standing behind it; it must never offer a
    # handoff it cannot perform. The phone line must keep the one it has.
    assert brands.BRANDS[key]["transfer"] is expected


def test_every_entry_point_has_an_english_greeting():
    for name, b in brands.BRANDS.items():
        assert b["greeting"]["en"].strip(), name


def test_every_greeting_names_its_own_agency():
    for name, b in brands.BRANDS.items():
        assert b["agency"] in b["greeting"]["en"], name


def test_every_entry_point_declares_all_keys():
    for name, b in brands.BRANDS.items():
        assert set(b) == {"agency", "agent", "transfer", "post_call", "greeting"}, name


@pytest.mark.parametrize("value", ["nope", "", "   ", None, "RODRIGO"])
def test_resolve_falls_back_to_the_default(value):
    assert brands.resolve_brand(value) is brands.BRANDS[brands.DEFAULT_BRAND]


@pytest.mark.parametrize(
    "value", [PHONE, DEMO, "  STARPROPERTIES  ", "StarProperties_Demo"])
def test_resolve_normalizes_case_and_whitespace(value):
    assert brands.resolve_brand(value) is brands.BRANDS[value.strip().lower()]


@pytest.mark.parametrize("key,expected", [(PHONE, True), (DEMO, False)])
def test_post_call_is_a_property_of_the_entry_point(key, expected):
    # The post-call webhook drives the "Real Estate Post-Call Processor" n8n
    # workflow, which WhatsApps the caller AND a manager and appends a Google
    # Sheet row. A website demo click must not send real messages to real
    # numbers; a real phone lead still should.
    assert brands.BRANDS[key]["post_call"] is expected


def test_the_two_entry_points_differ_only_in_transfer_and_post_call():
    phone = dict(brands.BRANDS[PHONE])
    demo = dict(brands.BRANDS[DEMO])
    for flag in ("transfer", "post_call"):
        assert phone.pop(flag) != demo.pop(flag), flag
    assert phone == demo
