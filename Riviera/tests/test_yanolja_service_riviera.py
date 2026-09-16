"""Offline unit tests for yanolja_service in Riviera Resort SINGLE-PROPERTY mode.

Why this file exists: `tests/test_kpms_service.py` covers `kpms_service.py`, which
nothing imports any more (dead code, still Mosvold-era). Its green tests say
nothing about the live booking path, which is
`tools` -> `booking_api` -> `yanolja_service` -> `yanolja_client`.

These tests pin the load-bearing parts of the single-property collapse and the
Riviera Resort rate card, each of which would be a silent production break:

  * the resolvers must ALWAYS resolve, never None — returning None would fail
    every tool call closed on an unanswerable "which property?" question;
  * `_property_of` must return "" for names outside the catalogue, because that ""
    is what filters retired types (ex-Mosvold, ex-Hatton Hills) and the Default
    Unmapped Room out of availability;
  * the module's room vocabulary and rate card must agree with `tools.py`, since a
    mismatch silently removes a room type from Riya's inventory;
  * rates are Sri Lankan resident LKR figures resolved by DATE-RANGE season
    (mid 2026-09-01..2027-06-30, high 2027-07-01..2027-08-31) — a stay that
    straddles the two, or sits outside both, must carry NO rate at all.

`yanolja_client` calls are mocked via monkeypatch on the `yanolja_service`
namespace, because that module imports the names directly
(`from yanolja_client import ...`).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_RIVIERA_DIR = Path(__file__).resolve().parent.parent
if str(_RIVIERA_DIR) not in sys.path:
    sys.path.insert(0, str(_RIVIERA_DIR))

import yanolja_service as ys  # noqa: E402

PROPERTY = "Riviera Resort"

# Sri Lankan resident MID-season rates, LKR per room per night, room only.
EXPECTED_ROOMS: dict[str, int] = {
    "Family Chalet": 32100,
    "Basic Room Single": 8800,
    "Wooden Cabana": 11000,
    "Lagoon View Steel Cabana": 14600,
    "Double Lagoon or Garden View": 17500,
    "Triple Garden View": 20400,
    "Family Cottage": 24800,
}

EXPECTED_HIGH_RATES: dict[str, int] = {
    "Family Chalet": 46900,
    "Basic Room Single": 12800,
    "Wooden Cabana": 16000,
    "Lagoon View Steel Cabana": 21300,
    "Double Lagoon or Garden View": 25600,
    "Triple Garden View": 29800,
    "Family Cottage": 36200,
}

# The two room types with exactly one physical unit, on purpose.
SINGLE_UNIT_TYPES = ("Wooden Cabana", "Family Cottage")

MID_STAY = ("2026-09-01", "2026-09-03")      # 2 nights, mid season
HIGH_STAY = ("2027-07-10", "2027-07-12")     # 2 nights, high season
MIXED_STAY = ("2027-06-29", "2027-07-02")    # straddles 30 Jun / 1 Jul
OUTSIDE_STAY = ("2026-08-20", "2026-08-22")  # before any published season

# Shaped like the PMS /rooms payload: each physical room carries its roomType.
# Mirrors the resort's rate sheet — 2+ units per type except the two single-unit
# types (Wooden Cabana and Family Cottage).
_ROOM_LAYOUT = [
    ("36", "Family Chalet", 1),
    ("37", "Family Chalet", 1),
    ("7", "Basic Room Single", 2),
    ("10", "Basic Room Single", 2),
    ("12", "Wooden Cabana", 3),
    ("31", "Lagoon View Steel Cabana", 4),
    ("32", "Lagoon View Steel Cabana", 4),
    ("4", "Double Lagoon or Garden View", 5),
    ("5", "Double Lagoon or Garden View", 5),
    ("20", "Triple Garden View", 6),
    ("9", "Triple Garden View", 6),
    ("8", "Family Cottage", 7),
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
                "code": "RIV-TEST",
                "description": "Riviera Resort, Kallady, Batticaloa",
                "basePrice": f"{EXPECTED_ROOMS.get(type_name, 0)}.00",
                "maxOccupancy": ys.ROOM_OCCUPANCY.get(type_name, {}).get("max_guests", 2),
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
    """Mock the PMS with the Riviera Resort layout and no bookings."""
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

def test_exactly_seven_room_types():
    assert len(ys.ALL_ROOM_TYPES) == 7
    assert set(ys.ALL_ROOM_TYPES) == set(EXPECTED_ROOMS)


def test_single_property_only():
    assert list(ys.ROOM_TYPES_BY_PROPERTY) == [PROPERTY]


def test_rate_card_covers_every_room_type_and_matches():
    assert set(ys.NIGHTLY_RATE_LKR) == set(EXPECTED_ROOMS)
    assert {n: r["mid"] for n, r in ys.NIGHTLY_RATE_LKR.items()} == EXPECTED_ROOMS
    assert {n: r["high"] for n, r in ys.NIGHTLY_RATE_LKR.items()} == EXPECTED_HIGH_RATES
    for name in EXPECTED_ROOMS:
        assert set(ys.NIGHTLY_RATE_LKR[name]) == {"mid", "high"}
        assert ys.NIGHTLY_RATE_LKR[name]["high"] > ys.NIGHTLY_RATE_LKR[name]["mid"]
    assert ys.RATE_CURRENCY == "LKR"


def test_demo_rate_for_is_the_mid_season_compat_rate():
    for name, mid in EXPECTED_ROOMS.items():
        assert ys.demo_rate_for(name) == mid
    assert ys.demo_rate_for("Mount Monarch Chalet") is None


def test_occupancy_covers_every_room_type():
    assert set(ys.ROOM_OCCUPANCY) == set(EXPECTED_ROOMS)
    for name, occ in ys.ROOM_OCCUPANCY.items():
        assert occ["max_guests"] >= occ["base_adults"] >= 1, name


def test_rate_seasons_are_two_contiguous_date_ranges():
    keys = [k for k, _first, _last in ys.RATE_SEASONS]
    assert keys == ["mid", "high"]
    (_m, mid_first, mid_last), (_h, high_first, high_last) = ys.RATE_SEASONS
    assert mid_first.isoformat() == "2026-09-01"
    assert mid_last.isoformat() == "2027-06-30"
    assert high_first.isoformat() == "2027-07-01"
    assert high_last.isoformat() == "2027-08-31"
    assert (high_first - mid_last).days == 1, "no gap between the two seasons"


def test_vocabulary_agrees_with_tools_module():
    """A mismatch here silently removes a room type from Riya's inventory."""
    import tools
    assert tools.ROOM_TYPES_BY_PROPERTY == ys.ROOM_TYPES_BY_PROPERTY
    assert tools.RESERVATIONS_PHONE == ys.RESERVATIONS_PHONE == "065 222 2164"
    assert tools._PROPERTY_ALIASES == ys._PROPERTY_ALIASES


def test_no_room_name_is_a_prefix_of_another():
    """Guarantees the prefix/extends matching below can never be ambiguous."""
    for a in ys.ALL_ROOM_TYPES:
        for b in ys.ALL_ROOM_TYPES:
            if a is not b:
                assert not ys._norm(b).startswith(ys._norm(a)), f"{a!r} prefixes {b!r}"


# --------------------------------------------------------------------------- #
# Season resolution                                                           #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("check_in,check_out,expected", [
    (*MID_STAY, "mid"),
    ("2026-12-20", "2026-12-22", "mid"),          # December is MID at Riviera
    ("2027-06-30", "2027-07-01", "mid"),          # checkout on the first high day
    (*HIGH_STAY, "high"),
    ("2027-08-31", "2027-09-01", "high"),         # last high night
    (*MIXED_STAY, "mixed_period"),
    (*OUTSIDE_STAY, None),
    ("2027-08-31", "2027-09-02", None),           # runs past the published card
    ("2026-09-03", "2026-09-01", None),           # inverted dates
])
def test_season_for_stay(check_in, check_out, expected):
    ci, co = ys._parse_date(check_in), ys._parse_date(check_out)
    assert ys.season_for_stay(ci, co) == expected


def test_rate_for_stay_follows_the_season():
    ci, co = (ys._parse_date(d) for d in MID_STAY)
    assert ys.rate_for_stay("Family Cottage", ci, co) == 24800
    ci, co = (ys._parse_date(d) for d in HIGH_STAY)
    assert ys.rate_for_stay("Family Cottage", ci, co) == 36200
    ci, co = (ys._parse_date(d) for d in MIXED_STAY)
    assert ys.rate_for_stay("Family Cottage", ci, co) is None
    ci, co = (ys._parse_date(d) for d in OUTSIDE_STAY)
    assert ys.rate_for_stay("Family Cottage", ci, co) is None
    ci, co = (ys._parse_date(d) for d in MID_STAY)
    assert ys.rate_for_stay("Not A Room", ci, co) is None


# --------------------------------------------------------------------------- #
# Resolvers must always resolve                                               #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("value", ["", "   ", "Riviera Resort", "riviera", "kallady",
                                   "batticaloa", "hatton", "hill country",
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
    # The previous (Hatton Hills) catalogue must be filtered out too.
    "Forest Escape Suite", "Eco Harmony Suite", "Sunrise Vista Premium Suite",
    "Mount Luxe Chalet", "Mount Monarch Chalet",
])
def test_property_of_rejects_everything_else(name):
    """"" is load-bearing: it is what keeps these rows out of availability."""
    assert ys._property_of({"name": name}) == ""


def test_property_of_ignores_pms_supplied_property_fields():
    """Regression guard for the subtle bug in the single-property collapse.

    `resolve_property` now always returns "Riviera Resort", so if `_property_of`
    still consulted a PMS property field it would launder ANY string into a match
    and drag retired room types back into bookable availability."""
    assert ys._property_of({
        "name": "Retired Room Type A",
        "propertyName": "Riviera Resort",
        "hotel": {"name": "Riviera Resort"},
    }) == ""


# --------------------------------------------------------------------------- #
# Room-name matching                                                          #
# --------------------------------------------------------------------------- #

@pytest.fixture
def room_type_dicts():
    return [{"id": i, "name": n} for i, n in enumerate(ys.ALL_ROOM_TYPES, 1)]


@pytest.mark.parametrize("query,expected", [
    # exact / case-insensitive
    ("Family Cottage", "Family Cottage"),
    ("family cottage", "Family Cottage"),
    ("FAMILY COTTAGE", "Family Cottage"),
    ("lagoon view steel cabana", "Lagoon View Steel Cabana"),
    ("basic room single", "Basic Room Single"),
    # query is an ABBREVIATION (prefix) of the canonical name
    ("basic room", "Basic Room Single"),
    ("lagoon view steel", "Lagoon View Steel Cabana"),
    ("double lagoon", "Double Lagoon or Garden View"),
    ("wooden", "Wooden Cabana"),
    ("triple", "Triple Garden View"),
    # The abbreviation rule runs BEFORE the any-order word fallback, so "lagoon
    # view" is the Steel Cabana even though the Double also carries both words.
    ("lagoon view", "Lagoon View Steel Cabana"),
    # query EXTENDS the canonical name — the fallback added for single-property mode
    ("family cottage on the lagoon", "Family Cottage"),
    ("lagoon view steel cabana with a terrace", "Lagoon View Steel Cabana"),
    # every query word appears in exactly one canonical name, any order — the
    # fallback that lets guests use the distinguishing word in the middle/end
    ("steel cabana", "Lagoon View Steel Cabana"),
    ("cottage", "Family Cottage"),
    ("chalet", "Family Chalet"),
    ("garden view triple", "Triple Garden View"),
    ("steel", "Lagoon View Steel Cabana"),
])
def test_match_room_type(query, expected, room_type_dicts):
    match = ys._match_room_type(query, room_type_dicts, PROPERTY)
    assert match is not None, f"{query!r} did not match"
    assert match["name"] == expected


@pytest.mark.parametrize("query", ["cabana", "family", "view", "garden", "garden view"])
def test_ambiguous_queries_raise_so_riya_asks_which(query, room_type_dicts):
    """Two or more rooms share the word: never silently pick one."""
    with pytest.raises(ys.ServiceError):
        ys._match_room_type(query, room_type_dicts, PROPERTY)


@pytest.mark.parametrize("query", ["penthouse", "suite", "deluxe double room", "villa",
                                   "mount monarch chalet", "forest escape"])
def test_unknown_queries_do_not_match(query, room_type_dicts):
    assert ys._match_room_type(query, room_type_dicts, PROPERTY) is None


def test_empty_query_does_not_match(room_type_dicts):
    assert ys._match_room_type("", room_type_dicts, PROPERTY) is None
    assert ys._match_room_type("   ", room_type_dicts, PROPERTY) is None


# --------------------------------------------------------------------------- #
# Availability                                                                #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_availability_needs_no_property_argument(pms):
    """The old code short-circuited to None here and failed closed."""
    result = await ys.derive_availability(*MID_STAY, num_adults=2)
    assert "error" not in result, result
    assert result.get("property") == PROPERTY


@pytest.mark.asyncio
async def test_availability_returns_all_seven_types(pms):
    result = await ys.derive_availability(*MID_STAY, num_adults=2)
    names = {r["room_type_name"] for r in result["rooms"]}
    assert names == set(EXPECTED_ROOMS)
    assert all(r["available"] for r in result["rooms"]), "nothing is booked"
    assert result["available_room_types"] == 7
    assert result["total_room_types"] == 7
    assert result["nights"] == 2


@pytest.mark.asyncio
async def test_availability_quotes_mid_season_lkr_rate_total_and_occupancy(pms):
    result = await ys.derive_availability(*MID_STAY, num_adults=2)
    by_name = {r["room_type_name"]: r for r in result["rooms"]}
    for name, nightly in EXPECTED_ROOMS.items():
        entry = by_name[name]
        assert entry["rate_per_night_lkr"] == nightly
        assert entry["total_lkr"] == nightly * 2, f"{name}: 2 nights"
        assert entry["max_guests"] == ys.ROOM_OCCUPANCY[name]["max_guests"]
        assert "rate_per_night_usd" not in entry
        assert "total_usd" not in entry
    note = result["rates_note"].lower()
    assert "resident" in note
    assert "room only" in note
    assert "mid season" in note
    assert ys.RESERVATIONS_PHONE in result["rates_note"]


@pytest.mark.asyncio
async def test_availability_quotes_high_season_lkr_rate(pms):
    result = await ys.derive_availability(*HIGH_STAY, num_adults=2)
    by_name = {r["room_type_name"]: r for r in result["rooms"]}
    for name, nightly in EXPECTED_HIGH_RATES.items():
        assert by_name[name]["rate_per_night_lkr"] == nightly
        assert by_name[name]["total_lkr"] == nightly * 2
    assert "high season" in result["rates_note"].lower()


@pytest.mark.asyncio
async def test_availability_for_a_mixed_period_stay_carries_no_rate(pms):
    """A stay straddling 30 Jun / 1 Jul 2027 must not get a single nightly figure."""
    result = await ys.derive_availability(*MIXED_STAY, num_adults=2)
    assert "error" not in result, result
    assert len(result["rooms"]) == 7
    for entry in result["rooms"]:
        assert "rate_per_night_lkr" not in entry, entry
        assert "total_lkr" not in entry, entry
        assert "rate_per_night_usd" not in entry
        assert "total_usd" not in entry
    note = result["rates_note"].lower()
    assert "reservations" in note
    assert ys.RESERVATIONS_PHONE in result["rates_note"]
    assert "straddles" in note or "two rate seasons" in note


@pytest.mark.asyncio
async def test_availability_outside_published_seasons_carries_no_rate(pms):
    result = await ys.derive_availability(*OUTSIDE_STAY, num_adults=2)
    assert "error" not in result, result
    for entry in result["rooms"]:
        assert "rate_per_night_lkr" not in entry, entry
        assert "total_lkr" not in entry, entry
    note = result["rates_note"].lower()
    assert "reservations" in note
    assert "outside" in note


@pytest.mark.asyncio
async def test_availability_room_type_filter_accepts_the_distinguishing_word(pms):
    result = await ys.derive_availability(*MID_STAY, room_type_filter="steel cabana")
    assert "error" not in result, result
    assert [r["room_type_name"] for r in result["rooms"]] == ["Lagoon View Steel Cabana"]
    assert result["rooms"][0]["rate_per_night_lkr"] == 14600


@pytest.mark.asyncio
async def test_availability_room_type_filter_rejects_an_ambiguous_cabana(pms):
    """A bare "cabana" matches two rooms: Riya must ask, not guess."""
    result = await ys.derive_availability(*MID_STAY, room_type_filter="cabana")
    assert "error" in result
    assert "Wooden Cabana" in result["error"]
    assert "Lagoon View Steel Cabana" in result["error"]


@pytest.mark.asyncio
async def test_retired_and_unmapped_types_never_surface(pms):
    """The PMS still holds these rows post-migration; they must stay invisible."""
    pms["rooms"] = _rooms(extra=[
        ("RET-901", "Retired Room Type A", 8),
        ("DUR-001", "Default Unmapped Room", 9),
        ("HH-501", "Mount Monarch Chalet", 10),
    ])
    result = await ys.derive_availability(*MID_STAY, num_adults=2)
    names = {r["room_type_name"] for r in result["rooms"]}
    assert names == set(EXPECTED_ROOMS)
    assert "Retired Room Type A" not in names
    assert "Default Unmapped Room" not in names
    assert "Mount Monarch Chalet" not in names
    assert result["total_room_types"] == 7


@pytest.mark.asyncio
async def test_booked_room_reduces_but_does_not_remove_a_two_unit_type(pms):
    """Why most types got 2 rooms: one booking must not exhaust a room type."""
    first_chalet = next(r["id"] for r in pms["rooms"] if r["roomNumber"] == "36")
    pms["reservations"] = [{
        "status": "confirmed", "roomId": first_chalet,
        "checkIn": MID_STAY[0], "checkOut": MID_STAY[1],
    }]
    result = await ys.derive_availability(*MID_STAY, num_adults=2)
    entry = next(r for r in result["rooms"] if r["room_type_name"] == "Family Chalet")
    assert entry["available"] is True, "the second Family Chalet unit is still free"
    assert result["available_room_types"] == 7


@pytest.mark.asyncio
@pytest.mark.parametrize("room_type", SINGLE_UNIT_TYPES)
async def test_single_unit_types_can_sell_out(pms, room_type):
    """Wooden Cabana and Family Cottage have exactly one unit each, on purpose."""
    only_unit = [r for r in pms["rooms"] if r["roomType"]["name"] == room_type]
    assert len(only_unit) == 1
    pms["reservations"] = [{
        "status": "confirmed", "roomId": only_unit[0]["id"],
        "checkIn": MID_STAY[0], "checkOut": MID_STAY[1],
    }]
    result = await ys.derive_availability(*MID_STAY, num_adults=2)
    entry = next(r for r in result["rooms"] if r["room_type_name"] == room_type)
    assert entry["available"] is False, f"the only {room_type} unit is booked"
    assert result["available_room_types"] == 6


@pytest.mark.asyncio
async def test_invalid_dates_still_rejected(pms):
    assert "error" in await ys.derive_availability("nonsense", "2026-09-03")
    assert "error" in await ys.derive_availability("2026-09-05", "2026-09-03")


# --------------------------------------------------------------------------- #
# Booking                                                                     #
# --------------------------------------------------------------------------- #

@pytest.fixture
def booking_pms(pms, monkeypatch):
    """Extend the PMS mock with guest/reservation creation that records payloads."""
    calls: dict[str, list[dict]] = {"guests": [], "reservations": []}

    async def fake_create_guest(payload):
        calls["guests"].append(payload)
        return {"id": 501, **payload}

    async def fake_create_reservation(payload):
        calls["reservations"].append(payload)
        return {"id": 9001, **payload}

    monkeypatch.setattr(ys, "create_guest", fake_create_guest)
    monkeypatch.setattr(ys, "create_reservation", fake_create_reservation)
    pms["calls"] = calls
    return pms


@pytest.mark.asyncio
async def test_book_emits_season_and_lkr_rate_for_a_mid_season_stay(booking_pms):
    result = await ys.book(
        *MID_STAY, room_type="steel cabana", guest_name="Nimal Perera",
        guest_phone="0771234567", num_adults=2,
    )
    assert result.get("success") is True, result
    assert result["room_type"] == "Lagoon View Steel Cabana"
    assert result["property"] == PROPERTY
    assert result["nights"] == 2
    assert result["season"] == "mid"
    assert result["rate_per_night_lkr"] == 14600
    assert result["total_lkr"] == 14600 * 2
    assert "rate_per_night_usd" not in result
    assert "total_usd" not in result
    note = result["rates_note"].lower()
    assert "room only" in note
    assert "resident" in note
    assert ys.RESERVATIONS_PHONE in result["rates_note"]
    # PMS payloads carry the canonical number and the matched type id.
    assert booking_pms["calls"]["guests"][0]["phone"] == "94771234567"
    assert booking_pms["calls"]["reservations"][0]["roomTypeId"] == 4


@pytest.mark.asyncio
async def test_book_emits_high_season_rate_in_july(booking_pms):
    result = await ys.book(*HIGH_STAY, room_type="Family Cottage", guest_name="Kamal Silva")
    assert result.get("success") is True, result
    assert result["season"] == "high"
    assert result["rate_per_night_lkr"] == 36200
    assert result["total_lkr"] == 36200 * 2


@pytest.mark.asyncio
async def test_book_for_a_mixed_period_stay_carries_no_rate(booking_pms):
    result = await ys.book(*MIXED_STAY, room_type="Family Cottage", guest_name="Kamal Silva")
    assert result.get("success") is True, result
    for key in ("season", "rate_per_night_lkr", "total_lkr", "rate_per_night_usd", "total_usd"):
        assert key not in result, key
    assert "reservations" in result["rates_note"].lower()
    assert ys.RESERVATIONS_PHONE in result["rates_note"]


@pytest.mark.asyncio
async def test_book_rejects_an_ambiguous_or_unknown_room_type(booking_pms):
    for query in ("cabana", "penthouse"):
        result = await ys.book(*MID_STAY, room_type=query, guest_name="Kamal Silva")
        assert "error" in result, query
        assert PROPERTY in result["error"]
    assert booking_pms["calls"]["reservations"] == []


@pytest.mark.asyncio
async def test_book_a_sold_out_single_unit_type_fails_gracefully(booking_pms):
    only_unit = next(r for r in booking_pms["rooms"] if r["roomType"]["name"] == "Wooden Cabana")
    booking_pms["reservations"] = [{
        "status": "confirmed", "roomId": only_unit["id"],
        "checkIn": MID_STAY[0], "checkOut": MID_STAY[1],
    }]
    result = await ys.book(*MID_STAY, room_type="Wooden Cabana", guest_name="Kamal Silva")
    assert "error" in result
    assert "Wooden Cabana" in result["error"]
    assert booking_pms["calls"]["reservations"] == []


# --------------------------------------------------------------------------- #
# No stale-brand leakage                                                      #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_no_mosvold_or_hatton_string_reaches_the_caller(pms, monkeypatch):
    availability = await ys.derive_availability(*MID_STAY, num_adults=2)

    async def fake_create_guest(payload):
        return {"id": 1, **payload}

    async def fake_create_reservation(payload):
        return {"id": 2, **payload}

    monkeypatch.setattr(ys, "create_guest", fake_create_guest)
    monkeypatch.setattr(ys, "create_reservation", fake_create_reservation)
    booking = await ys.book(*MID_STAY, room_type="Family Chalet", guest_name="Kamal Silva")
    rejection = await ys.book(*MID_STAY, room_type="penthouse", guest_name="Kamal Silva")

    blob = repr((availability, booking, rejection)).lower()
    for stale in (
        "mosvold", "sundara", "balapitiya", "ahangama", "deluxe", "founders",
        "hatton", "hill country", "forest escape", "eco harmony", "sunrise vista",
        "mount luxe", "mount monarch", "usd",
    ):
        assert stale not in blob, f"{stale!r} leaked into a caller-facing payload"
    assert "riviera resort" in blob


# --------------------------------------------------------------------------- #
# Phone stored on a booking: validate, never manufacture, fall back to caller  #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("guest, caller, expected", [
    # A good dictated number is kept (and canonicalised to 94 + 9-digit NSN).
    ("0774294451", "", "94774294451"),
    ("077 429 4451", "0711754668", "94774294451"),   # guest good -> caller ignored
    # A wrong-length dictated number is NOT padded into a plausible wrong one;
    # it falls back to the line the guest is calling from (Booking 80 defect).
    ("074294451", "0711754668", "94711754668"),      # one digit dropped
    ("07742944510", "+94711754668", "94711754668"),  # one digit too many
    ("", "0711754668", "94711754668"),               # none dictated
    # Both unusable -> store nothing; downstream confirms on the caller's line.
    ("074294451", "", ""),
    ("", "", ""),
    # A wrong-length CALLER id is not trusted either.
    ("074294451", "12", ""),
])
def test_resolve_stored_phone(guest, caller, expected):
    assert ys._resolve_stored_phone(guest, caller) == expected
