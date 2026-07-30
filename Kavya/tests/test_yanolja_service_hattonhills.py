"""Offline unit tests for yanolja_service in Hatton Hills SINGLE-PROPERTY mode.

Why this file exists: `tests/test_kpms_service.py` covers `kpms_service.py`, which
nothing imports any more (dead code, still Mosvold-era). Its 21 green tests say
nothing about the live booking path, which is
`tools` -> `booking_api` -> `yanolja_service` -> `yanolja_client`.

These tests pin the load-bearing parts of the 2026-07-30 single-property collapse,
each of which would have been a silent production break:

  * the resolvers must ALWAYS resolve, never None — returning None would fail
    every tool call closed on an unanswerable "which property?" question;
  * `_property_of` must return "" for names outside the catalogue, because that ""
    is what filters retired ex-Mosvold types and the Default Unmapped Room out of
    availability;
  * the module's room vocabulary and rate card must agree with `tools.py`, since a
    mismatch silently removes a room type from Kavya's inventory.

`yanolja_client` calls are mocked via monkeypatch on the `yanolja_service`
namespace, because that module imports the names directly
(`from yanolja_client import ...`).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_KAVYA_DIR = Path(__file__).resolve().parent.parent
if str(_KAVYA_DIR) not in sys.path:
    sys.path.insert(0, str(_KAVYA_DIR))

import yanolja_service as ys  # noqa: E402

PROPERTY = "Hatton Hills"

EXPECTED_ROOMS: dict[str, int] = {
    "Forest Escape Suite": 700,
    "Eco Harmony Suite": 800,
    "Sunrise Vista Premium Suite": 950,
    "Mount Luxe Chalet": 1150,
    "Mount Monarch Chalet": 1400,
}

# Shaped like the PMS /rooms payload: each physical room carries its roomType.
# Mirrors the post-migration layout — 2 rooms per type except Mount Monarch's 1.
_ROOM_LAYOUT = [
    ("HH-101", "Forest Escape Suite", 1),
    ("HH-102", "Forest Escape Suite", 1),
    ("HH-201", "Eco Harmony Suite", 2),
    ("HH-202", "Eco Harmony Suite", 2),
    ("HH-301", "Sunrise Vista Premium Suite", 3),
    ("HH-302", "Sunrise Vista Premium Suite", 3),
    ("HH-401", "Mount Luxe Chalet", 4),
    ("HH-402", "Mount Luxe Chalet", 4),
    ("HH-501", "Mount Monarch Chalet", 5),
]


def _rooms(extra: list[tuple[str, str, int]] | None = None) -> list[dict]:
    out = []
    for i, (number, type_name, type_id) in enumerate(_ROOM_LAYOUT + (extra or []), 1):
        out.append({
            "id": i,
            "roomNumber": number,
            # Top-level roomTypeId is what derive_availability reads to decide
            # which types are free — mirrors the real PMS /rooms payload, which
            # carries it alongside the nested roomType object.
            "roomTypeId": type_id,
            "floor": 1,
            "status": "available",
            "housekeepingStatus": "clean",
            "roomType": {
                "id": type_id,
                "name": type_name,
                "code": "HH-TEST",
                "description": "Hatton Hills, Central Province",
                "basePrice": f"{EXPECTED_ROOMS.get(type_name, 0)}.00",
                "maxOccupancy": 2 if "Suite" in type_name else 5,
                "isActive": True,
            },
        })
    return out


@pytest.fixture(autouse=True)
def _clear_cache():
    ys._cache.clear()
    yield
    ys._cache.clear()


@pytest.fixture
def pms(monkeypatch):
    """Mock the PMS with the post-migration Hatton Hills layout and no bookings."""
    state = {"rooms": _rooms(), "reservations": []}

    async def fake_list_rooms():
        return state["rooms"]

    async def fake_list_reservations():
        return state["reservations"]

    monkeypatch.setattr(ys, "list_rooms", fake_list_rooms)
    monkeypatch.setattr(ys, "list_reservations", fake_list_reservations)
    return state


# --------------------------------------------------------------------------- #
# Vocabulary / rate card agreement                                            #
# --------------------------------------------------------------------------- #

def test_exactly_five_room_types():
    assert len(ys.ALL_ROOM_TYPES) == 5
    assert set(ys.ALL_ROOM_TYPES) == set(EXPECTED_ROOMS)


def test_single_property_only():
    assert list(ys.ROOM_TYPES_BY_PROPERTY) == [PROPERTY]


def test_rate_card_covers_every_room_type_and_matches():
    assert ys.DEMO_NIGHTLY_RATE_USD == EXPECTED_ROOMS


def test_vocabulary_agrees_with_tools_module():
    """A mismatch here silently removes a room type from Kavya's inventory."""
    import tools
    assert tools.ROOM_TYPES_BY_PROPERTY == ys.ROOM_TYPES_BY_PROPERTY
    assert tools.RESERVATIONS_PHONE == ys.RESERVATIONS_PHONE


def test_no_room_name_is_a_prefix_of_another():
    """Guarantees the prefix/extends matching below can never be ambiguous."""
    for a in ys.ALL_ROOM_TYPES:
        for b in ys.ALL_ROOM_TYPES:
            if a is not b:
                assert not ys._norm(b).startswith(ys._norm(a)), f"{a!r} prefixes {b!r}"


# --------------------------------------------------------------------------- #
# Resolvers must always resolve                                               #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("value", ["", "   ", "Hatton Hills", "hatton", "hill country",
                                   "some other hotel", "Sundara by Mosvold"])
def test_resolve_property_always_resolves(value):
    """Returning None here would fail every tool call closed."""
    assert ys.resolve_property(value) == PROPERTY


def test_resolve_property_never_returns_none_for_falsy():
    assert ys.resolve_property(None) == PROPERTY  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# _property_of is the availability filter                                     #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("name", list(EXPECTED_ROOMS))
def test_property_of_recognises_catalogue_names(name):
    assert ys._property_of({"name": name}) == PROPERTY


@pytest.mark.parametrize("name", [
    "Retired Room Type A", "Default Unmapped Room", "Deluxe Double Room",
    "Beach Villa", "Founders Suite", "", "Some Nonsense",
])
def test_property_of_rejects_everything_else(name):
    """"" is load-bearing: it is what keeps these rows out of availability."""
    assert ys._property_of({"name": name}) == ""


def test_property_of_ignores_pms_supplied_property_fields():
    """Regression guard for the subtle bug in the single-property collapse.

    `resolve_property` now always returns "Hatton Hills", so if `_property_of`
    still consulted a PMS property field it would launder ANY string into a match
    and drag retired room types back into bookable availability."""
    assert ys._property_of({
        "name": "Retired Room Type A",
        "propertyName": "Hatton Hills",
        "hotel": {"name": "Hatton Hills"},
    }) == ""


# --------------------------------------------------------------------------- #
# Room-name matching                                                          #
# --------------------------------------------------------------------------- #

@pytest.fixture
def room_type_dicts():
    return [{"id": i, "name": n} for i, n in enumerate(ys.ALL_ROOM_TYPES, 1)]


@pytest.mark.parametrize("query,expected", [
    ("Mount Monarch Chalet", "Mount Monarch Chalet"),
    ("mount monarch", "Mount Monarch Chalet"),
    ("MOUNT MONARCH CHALET", "Mount Monarch Chalet"),
    ("forest escape", "Forest Escape Suite"),
    ("eco harmony", "Eco Harmony Suite"),
    ("sunrise vista", "Sunrise Vista Premium Suite"),
    ("mount luxe", "Mount Luxe Chalet"),
    # query EXTENDS the canonical name — the fallback added for single-property mode
    ("mount monarch chalet with plunge pool", "Mount Monarch Chalet"),
    ("sunrise vista premium suite with terrace", "Sunrise Vista Premium Suite"),
])
def test_match_room_type(query, expected, room_type_dicts):
    match = ys._match_room_type(query, room_type_dicts, PROPERTY)
    assert match is not None, f"{query!r} did not match"
    assert match["name"] == expected


@pytest.mark.parametrize("query", ["chalet", "suite", "room", "deluxe double room"])
def test_ambiguous_or_unknown_queries_do_not_match(query, room_type_dicts):
    """Must not silently pick a room — returns None or raises so Kavya asks."""
    try:
        assert ys._match_room_type(query, room_type_dicts, PROPERTY) is None
    except ys.ServiceError:
        pass  # explicit ambiguity is also an acceptable outcome


# --------------------------------------------------------------------------- #
# Availability                                                                #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_availability_needs_no_property_argument(pms):
    """The old code short-circuited to None here and failed closed."""
    result = await ys.derive_availability("2026-09-01", "2026-09-03", num_adults=2)
    assert "error" not in result, result
    assert result.get("property") == PROPERTY


@pytest.mark.asyncio
async def test_availability_returns_all_five_types(pms):
    result = await ys.derive_availability("2026-09-01", "2026-09-03", num_adults=2)
    names = {r["room_type_name"] for r in result["rooms"]}
    assert names == set(EXPECTED_ROOMS)
    assert all(r["available"] for r in result["rooms"]), "nothing is booked"
    assert result["available_room_types"] == 5


@pytest.mark.asyncio
async def test_availability_quotes_correct_rate_and_total(pms):
    result = await ys.derive_availability("2026-09-01", "2026-09-03", num_adults=2)
    by_name = {r["room_type_name"]: r for r in result["rooms"]}
    for name, nightly in EXPECTED_ROOMS.items():
        assert by_name[name]["rate_per_night_usd"] == nightly
        assert by_name[name]["total_usd"] == nightly * 2, f"{name}: 2 nights"


@pytest.mark.asyncio
async def test_retired_and_unmapped_types_never_surface(pms):
    """The PMS still holds these rows post-migration; they must stay invisible."""
    pms["rooms"] = _rooms(extra=[
        ("RET-901", "Retired Room Type A", 7),
        ("DUR-001", "Default Unmapped Room", 6),
    ])
    result = await ys.derive_availability("2026-09-01", "2026-09-03", num_adults=2)
    names = {r["room_type_name"] for r in result["rooms"]}
    assert names == set(EXPECTED_ROOMS)
    assert "Retired Room Type A" not in names
    assert "Default Unmapped Room" not in names
    assert result["total_room_types"] == 5


@pytest.mark.asyncio
async def test_booked_room_reduces_but_does_not_remove_a_two_unit_type(pms):
    """Why each type got 2 rooms: one booking must not exhaust a room type."""
    pms["reservations"] = [{
        "status": "confirmed", "roomId": 1,
        "checkIn": "2026-09-01", "checkOut": "2026-09-03",
    }]
    result = await ys.derive_availability("2026-09-01", "2026-09-03", num_adults=2)
    entry = next(r for r in result["rooms"] if r["room_type_name"] == "Forest Escape Suite")
    assert entry["available"] is True, "the second Forest Escape unit is still free"


@pytest.mark.asyncio
async def test_single_unit_flagship_can_sell_out(pms):
    """Mount Monarch Chalet has exactly one unit, on purpose."""
    monarch_id = next(r["id"] for r in pms["rooms"] if r["roomNumber"] == "HH-501")
    pms["reservations"] = [{
        "status": "confirmed", "roomId": monarch_id,
        "checkIn": "2026-09-01", "checkOut": "2026-09-03",
    }]
    result = await ys.derive_availability("2026-09-01", "2026-09-03", num_adults=2)
    entry = next(r for r in result["rooms"] if r["room_type_name"] == "Mount Monarch Chalet")
    assert entry["available"] is False, "the only Mount Monarch unit is booked"
    assert result["available_room_types"] == 4


@pytest.mark.asyncio
async def test_invalid_dates_still_rejected(pms):
    assert "error" in await ys.derive_availability("nonsense", "2026-09-03")
    assert "error" in await ys.derive_availability("2026-09-05", "2026-09-03")


# --------------------------------------------------------------------------- #
# No stale-brand leakage                                                      #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_no_mosvold_string_reaches_the_caller(pms):
    result = await ys.derive_availability("2026-09-01", "2026-09-03", num_adults=2)
    blob = repr(result).lower()
    for stale in ("mosvold", "sundara", "balapitiya", "ahangama", "deluxe", "founders"):
        assert stale not in blob, f"{stale!r} leaked into a caller-facing payload"
