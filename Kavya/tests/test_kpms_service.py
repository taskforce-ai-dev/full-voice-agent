"""Offline unit tests for kpms_service business logic (Mosvold Boutique Hotels).

All kpms_client calls are mocked with monkeypatch.setattr on the
kpms_service module's namespace (since kpms_service imports names
directly via `from kpms_client import ...`).

Mosvold runs TWO properties off one phone line — Mosvold Villa (Ahangama) and
Sundara by Mosvold (Balapitiya) — and their room names overlap in phrasing
("Deluxe Double Room" vs "Deluxe Double Room with Sea View"). The fixtures
below therefore carry BOTH properties so the property-disambiguation guards in
kpms_service can actually be exercised.

PRICING — deliberate fixture decision
-------------------------------------
Mosvold publishes no rates anywhere; BookingEye quotes them live per date.
`derive_availability()` reflects that: it returns no rate field at all, only
`PRICING_MESSAGE`. `book()` however still reads `baseRate` off the PMS room type
to compute the `total` it writes into the PMS reservation record — that is
bookkeeping internal to the PMS and is never returned to the caller.

To keep that arithmetic path covered without inventing Mosvold pricing, every
room type here carries the same neutral placeholder `PLACEHOLDER_BASE_RATE`
("1.00"). It is a unit multiplier, not a price: it cannot be mistaken for a
published Mosvold rate, yet 1.00 x nights still proves the nights multiplication
works. `test_no_currency_figures_leak_to_caller` asserts the value never reaches
any caller-facing string.
"""
from __future__ import annotations

import re
import sys
from copy import deepcopy
from pathlib import Path

import pytest

# Make the Kavya package importable when running pytest from the repo root.
_KAVYA_DIR = Path(__file__).resolve().parent.parent
if str(_KAVYA_DIR) not in sys.path:
    sys.path.insert(0, str(_KAVYA_DIR))

import kpms_service  # noqa: E402


# --------------------------------------------------------------------------- #
# Canned fixtures — Mosvold, both properties                                   #
# --------------------------------------------------------------------------- #

PROPERTY_ID_VILLA = "p_mosvold_villa"
PROPERTY_ID_SUNDARA = "p_sundara_mosvold"

PROPERTIES = [
    {"id": PROPERTY_ID_VILLA, "name": "Mosvold Villa"},
    {"id": PROPERTY_ID_SUNDARA, "name": "Sundara by Mosvold"},
]

# Neutral unit placeholder — see the module docstring. NOT a Mosvold rate.
PLACEHOLDER_BASE_RATE = "1.00"

# The PMS room-type endpoint is not property-scoped (`GET /api/room-types` takes
# no propertyId), so a single deployment sees every type from both hotels. Rooms
# are what carry propertyId. Capacities follow the room descriptions in the
# source doc: doubles/twins sleep two; the Family Suite, Beach Villa and Family
# Villa with Pool are two-bedroom / family-of-four rooms.
ROOM_TYPES = [
    # Mosvold Villa — Ahangama
    {"id": "rt_villa_deluxe_double", "name": "Deluxe Double Room",
     "baseRate": PLACEHOLDER_BASE_RATE, "capacity": 2},
    {"id": "rt_villa_deluxe_twin", "name": "Deluxe Twin Room",
     "baseRate": PLACEHOLDER_BASE_RATE, "capacity": 2},
    {"id": "rt_villa_family_suite", "name": "Family Suite",
     "baseRate": PLACEHOLDER_BASE_RATE, "capacity": 4},
    {"id": "rt_villa_founders_suite", "name": "Founders Suite",
     "baseRate": PLACEHOLDER_BASE_RATE, "capacity": 2},
    # Sundara by Mosvold — Balapitiya
    {"id": "rt_sundara_double_garden", "name": "Deluxe Double Room with Garden View",
     "baseRate": PLACEHOLDER_BASE_RATE, "capacity": 2},
    {"id": "rt_sundara_double_sea", "name": "Deluxe Double Room with Sea View",
     "baseRate": PLACEHOLDER_BASE_RATE, "capacity": 2},
    {"id": "rt_sundara_twin_sea", "name": "Deluxe Twin Room with Sea View",
     "baseRate": PLACEHOLDER_BASE_RATE, "capacity": 2},
    {"id": "rt_sundara_beach_villa", "name": "Beach Villa",
     "baseRate": PLACEHOLDER_BASE_RATE, "capacity": 4},
    {"id": "rt_sundara_family_villa", "name": "Family Villa with Pool",
     "baseRate": PLACEHOLDER_BASE_RATE, "capacity": 4},
]

VILLA_TYPE_NAMES = (
    "Deluxe Double Room",
    "Deluxe Twin Room",
    "Family Suite",
    "Founders Suite",
)
SUNDARA_TYPE_NAMES = (
    "Deluxe Double Room with Garden View",
    "Deluxe Double Room with Sea View",
    "Deluxe Twin Room with Sea View",
    "Beach Villa",
    "Family Villa with Pool",
)

# One physical room per type keeps availability deterministic: blocking a room
# blocks its whole type.
ROOMS = [
    # Mosvold Villa
    {"id": "room_villa_v01", "number": "V01", "typeId": "rt_villa_deluxe_double",
     "status": "available", "propertyId": PROPERTY_ID_VILLA},
    {"id": "room_villa_v02", "number": "V02", "typeId": "rt_villa_deluxe_twin",
     "status": "dirty", "propertyId": PROPERTY_ID_VILLA},
    {"id": "room_villa_v03", "number": "V03", "typeId": "rt_villa_family_suite",
     "status": "occupied", "propertyId": PROPERTY_ID_VILLA},
    {"id": "room_villa_v04", "number": "V04", "typeId": "rt_villa_founders_suite",
     "status": "available", "propertyId": PROPERTY_ID_VILLA},
    # Sundara by Mosvold
    {"id": "room_sundara_s01", "number": "S01", "typeId": "rt_sundara_double_garden",
     "status": "available", "propertyId": PROPERTY_ID_SUNDARA},
    {"id": "room_sundara_s02", "number": "S02", "typeId": "rt_sundara_double_sea",
     "status": "occupied", "propertyId": PROPERTY_ID_SUNDARA},
    {"id": "room_sundara_s03", "number": "S03", "typeId": "rt_sundara_twin_sea",
     "status": "dirty", "propertyId": PROPERTY_ID_SUNDARA},
    {"id": "room_sundara_s04", "number": "S04", "typeId": "rt_sundara_beach_villa",
     "status": "available", "propertyId": PROPERTY_ID_SUNDARA},
    {"id": "room_sundara_s05", "number": "S05", "typeId": "rt_sundara_family_villa",
     "status": "available", "propertyId": PROPERTY_ID_SUNDARA},
]


# --------------------------------------------------------------------------- #
# Fixtures                                                                    #
# --------------------------------------------------------------------------- #

@pytest.fixture(autouse=True)
def _clear_cache_and_env(monkeypatch):
    """Reset module-level cache and pin the booking backend to one property.

    kpms_service._get_property_id() refuses to guess when the PMS reports more
    than one property, so a deployment must pin KPMS_PROPERTY_ID. The default
    here is Mosvold Villa; tests that exercise Sundara re-set it.
    """
    kpms_service._cache.clear()
    monkeypatch.setenv("KPMS_PROPERTY_ID", PROPERTY_ID_VILLA)
    yield
    kpms_service._cache.clear()


class _Patcher:
    """Helper that records POSTs to create_reservation/create_guest and serves
    list/search results from in-memory state. Patches names on kpms_service."""

    def __init__(self, monkeypatch, *,
                 room_types=None, rooms=None,
                 reservations_by_room=None,
                 search_guests_result=None,
                 create_guest_result=None,
                 create_reservation_result=None,
                 update_reservation_result=None,
                 get_reservation_result=None):
        self.monkeypatch = monkeypatch
        self.room_types = deepcopy(room_types) if room_types is not None else deepcopy(ROOM_TYPES)
        self.rooms = deepcopy(rooms) if rooms is not None else deepcopy(ROOMS)
        self.reservations_by_room = deepcopy(reservations_by_room or {})
        self.search_guests_result = search_guests_result if search_guests_result is not None else []
        self.create_guest_result = create_guest_result
        self.create_reservation_result = create_reservation_result
        self.update_reservation_result = update_reservation_result
        self.get_reservation_result = get_reservation_result

        # Recorded calls
        self.create_reservation_calls: list[dict] = []
        self.create_guest_calls: list[dict] = []
        self.update_reservation_calls: list[tuple[str, dict]] = []
        self.delete_reservation_calls: list[str] = []
        self.list_rooms_calls: list[dict] = []
        self.search_guests_calls: list[str] = []

        self._install()

    def _install(self):
        mp = self.monkeypatch

        async def list_properties():
            return deepcopy(PROPERTIES)

        async def list_room_types():
            return deepcopy(self.room_types)

        async def list_rooms(*, property_id=None, type_id=None, status=None):
            self.list_rooms_calls.append({"property_id": property_id, "type_id": type_id, "status": status})
            result = deepcopy(self.rooms)
            if property_id is not None:
                result = [r for r in result if r.get("propertyId") == property_id]
            if type_id is not None:
                result = [r for r in result if r.get("typeId") == type_id]
            if status is not None:
                result = [r for r in result if r.get("status") == status]
            return result

        async def list_reservations(*, room_id=None, guest_id=None, check_in=None, check_out=None, status=None, source=None):
            if room_id is not None:
                return deepcopy(self.reservations_by_room.get(room_id, []))
            return []

        async def get_reservation(reservation_id):
            return deepcopy(self.get_reservation_result or {})

        async def create_reservation(payload):
            self.create_reservation_calls.append(deepcopy(payload))
            if self.create_reservation_result is None:
                raise AssertionError("create_reservation should not have been called")
            return deepcopy(self.create_reservation_result)

        async def update_reservation(reservation_id, payload):
            self.update_reservation_calls.append((reservation_id, deepcopy(payload)))
            if self.update_reservation_result is None:
                # Default: echo the cancelled status
                return {"id": reservation_id, **payload}
            return deepcopy(self.update_reservation_result)

        async def delete_reservation(reservation_id):
            self.delete_reservation_calls.append(reservation_id)
            return {}

        async def search_guests(query):
            self.search_guests_calls.append(query)
            return deepcopy(self.search_guests_result)

        async def create_guest(payload):
            self.create_guest_calls.append(deepcopy(payload))
            if self.create_guest_result is None:
                raise AssertionError("create_guest should not have been called")
            return deepcopy(self.create_guest_result)

        mp.setattr(kpms_service, "list_properties", list_properties)
        mp.setattr(kpms_service, "list_room_types", list_room_types)
        mp.setattr(kpms_service, "list_rooms", list_rooms)
        mp.setattr(kpms_service, "list_reservations", list_reservations)
        mp.setattr(kpms_service, "get_reservation", get_reservation)
        mp.setattr(kpms_service, "create_reservation", create_reservation)
        mp.setattr(kpms_service, "update_reservation", update_reservation)
        mp.setattr(kpms_service, "search_guests", search_guests)
        mp.setattr(kpms_service, "create_guest", create_guest)


# --------------------------------------------------------------------------- #
# Pricing guard                                                               #
# --------------------------------------------------------------------------- #

# "119.00", "1.00", "$225" — anything that reads as a money figure.
_MONEY_RE = re.compile(r"\d+[.,]\d{2}\b|[$€£]\s*\d|\b(?:usd|lkr|eur|rs)\b\s*\.?\s*\d", re.I)


def _assert_no_money(value, path="result"):
    """Recursively assert no currency figure appears in a caller-facing payload."""
    if isinstance(value, dict):
        for k, v in value.items():
            _assert_no_money(v, f"{path}[{k!r}]")
    elif isinstance(value, (list, tuple)):
        for i, v in enumerate(value):
            _assert_no_money(v, f"{path}[{i}]")
    elif isinstance(value, str):
        assert not _MONEY_RE.search(value), f"currency figure leaked at {path}: {value!r}"
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        # Counts (nights, capacity, totals-of-things) are fine; a rate is not.
        assert "rate" not in path.lower() and "price" not in path.lower() \
            and "total_usd" not in path.lower(), f"rate-like numeric at {path}: {value!r}"


# --------------------------------------------------------------------------- #
# Availability tests                                                          #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_derive_availability_all_free(monkeypatch):
    """With the backend pinned to Mosvold Villa, every Villa type is free.

    The PMS room-type list is not property-scoped, so all nine Mosvold types are
    reported; only the four with physical rooms at the pinned property can be
    available.
    """
    p = _Patcher(monkeypatch)
    result = await kpms_service.derive_availability("2026-07-01", "2026-07-03")

    assert "error" not in result
    assert result["total_room_types"] == 9
    assert result["available_room_types"] == 4
    assert result["nights"] == 2
    assert len(result["rooms"]) == 9

    by_name = {r["room_type_name"]: r for r in result["rooms"]}
    for name in VILLA_TYPE_NAMES:
        assert by_name[name]["available"] is True, f"{name} should be available"
        assert by_name[name]["property"] == "Mosvold Villa"
    for name in SUNDARA_TYPE_NAMES:
        assert by_name[name]["property"] == "Sundara by Mosvold"

    # Rooms were fetched scoped to the pinned property.
    assert p.list_rooms_calls
    assert all(c["property_id"] == PROPERTY_ID_VILLA for c in p.list_rooms_calls)

    # Mosvold publishes no rates — the payload carries a pricing message instead.
    assert result["pricing"] == kpms_service.PRICING_MESSAGE
    for entry in result["rooms"]:
        assert "rate_per_night_usd" not in entry
        assert "rate" not in entry
    _assert_no_money(result)


@pytest.mark.asyncio
async def test_derive_availability_sundara_when_pinned_there(monkeypatch):
    """Flipping the pinned property flips which hotel's rooms are bookable."""
    monkeypatch.setenv("KPMS_PROPERTY_ID", PROPERTY_ID_SUNDARA)
    _Patcher(monkeypatch)
    result = await kpms_service.derive_availability("2026-07-01", "2026-07-03")

    assert "error" not in result
    by_name = {r["room_type_name"]: r for r in result["rooms"]}
    for name in SUNDARA_TYPE_NAMES:
        assert by_name[name]["available"] is True, f"{name} should be available at Sundara"
    for name in VILLA_TYPE_NAMES:
        assert by_name[name]["available"] is False, f"{name} is not a Sundara room"
    assert result["available_room_types"] == 5


@pytest.mark.asyncio
async def test_derive_availability_partial_overlap(monkeypatch):
    reservations = {
        "room_villa_v01": [
            {"roomId": "room_villa_v01", "checkIn": "2026-07-02", "checkOut": "2026-07-04", "status": "pending"}
        ]
    }
    _Patcher(monkeypatch, reservations_by_room=reservations)
    result = await kpms_service.derive_availability("2026-07-01", "2026-07-05")

    assert "error" not in result
    by_name = {r["room_type_name"]: r for r in result["rooms"]}
    double = by_name["Deluxe Double Room"]
    assert double["available"] is False
    assert "unavailable_dates" in double
    assert "2026-07-02" in double["unavailable_dates"]
    assert "2026-07-03" in double["unavailable_dates"]
    # Other Villa types stay available
    for name in ("Deluxe Twin Room", "Family Suite", "Founders Suite"):
        assert by_name[name]["available"] is True, f"{name} should stay available"
    assert result["available_room_types"] == 3


@pytest.mark.asyncio
async def test_overlap_touching_is_not_overlap(monkeypatch):
    # Reservation ends on 2026-07-01 exactly when the new stay begins.
    reservations = {
        "room_villa_v01": [
            {"roomId": "room_villa_v01", "checkIn": "2026-06-29", "checkOut": "2026-07-01", "status": "pending"}
        ]
    }
    _Patcher(monkeypatch, reservations_by_room=reservations)
    result = await kpms_service.derive_availability("2026-07-01", "2026-07-03")

    by_name = {r["room_type_name"]: r for r in result["rooms"]}
    assert by_name["Deluxe Double Room"]["available"] is True


@pytest.mark.asyncio
async def test_cancelled_reservation_doesnt_block(monkeypatch):
    reservations = {
        "room_villa_v01": [
            {"roomId": "room_villa_v01", "checkIn": "2026-07-01", "checkOut": "2026-07-04", "status": "cancelled"}
        ]
    }
    _Patcher(monkeypatch, reservations_by_room=reservations)
    result = await kpms_service.derive_availability("2026-07-01", "2026-07-03")

    by_name = {r["room_type_name"]: r for r in result["rooms"]}
    assert by_name["Deluxe Double Room"]["available"] is True


@pytest.mark.asyncio
async def test_maintenance_room_excluded(monkeypatch):
    rooms = deepcopy(ROOMS)
    for r in rooms:
        if r["id"] == "room_villa_v02":  # the only Deluxe Twin Room at the Villa
            r["status"] = "maintenance"
    _Patcher(monkeypatch, rooms=rooms)
    result = await kpms_service.derive_availability("2026-07-01", "2026-07-03")

    by_name = {r["room_type_name"]: r for r in result["rooms"]}
    assert by_name["Deluxe Twin Room"]["available"] is False
    # Other Villa types unaffected
    for name in ("Deluxe Double Room", "Family Suite", "Founders Suite"):
        assert by_name[name]["available"] is True


@pytest.mark.asyncio
async def test_capacity_is_reported_but_not_enforced(monkeypatch):
    """Capacity is informational in kpms_service — the KB/LLM enforces it.

    kpms_service deliberately does not filter on party size (the PMS capacity
    contradicts the KB), so a party of four does not change availability. This
    test pins that documented behaviour and checks the capacity figure the
    caller needs in order to make the decision upstream.
    """
    _Patcher(monkeypatch)
    result = await kpms_service.derive_availability(
        "2026-07-01", "2026-07-03", num_adults=4, num_children=0
    )

    by_name = {r["room_type_name"]: r for r in result["rooms"]}
    # Family-of-four rooms per the source doc.
    assert by_name["Family Suite"]["capacity"] == 4
    assert by_name["Beach Villa"]["capacity"] == 4
    assert by_name["Family Villa with Pool"]["capacity"] == 4
    # Two-guest rooms.
    assert by_name["Deluxe Double Room"]["capacity"] == 2
    assert by_name["Deluxe Twin Room with Sea View"]["capacity"] == 2
    # Party size did not filter anything out.
    assert result["available_room_types"] == 4


@pytest.mark.asyncio
async def test_room_type_filter_matches_kavya_vocab(monkeypatch):
    _Patcher(monkeypatch)
    result = await kpms_service.derive_availability(
        "2026-07-01", "2026-07-03",
        room_type_filter="Founders Suite", property_name="Mosvold Villa",
    )

    assert "error" not in result
    assert len(result["rooms"]) == 1
    entry = result["rooms"][0]
    assert entry["room_type_name"] == "Founders Suite"
    assert entry["room_type_id"] == "rt_villa_founders_suite"
    assert entry["property"] == "Mosvold Villa"
    assert result["property"] == "Mosvold Villa"


@pytest.mark.asyncio
async def test_room_type_filter_scoped_to_sundara(monkeypatch):
    """The same words ("Deluxe Twin Room") resolve to the Sundara room when the
    caller names Sundara."""
    monkeypatch.setenv("KPMS_PROPERTY_ID", PROPERTY_ID_SUNDARA)
    _Patcher(monkeypatch)
    result = await kpms_service.derive_availability(
        "2026-07-01", "2026-07-03",
        room_type_filter="Deluxe Twin Room", property_name="Sundara",
    )

    assert "error" not in result
    assert len(result["rooms"]) == 1
    entry = result["rooms"][0]
    assert entry["room_type_name"] == "Deluxe Twin Room with Sea View"
    assert entry["room_type_id"] == "rt_sundara_twin_sea"
    assert entry["property"] == "Sundara by Mosvold"


@pytest.mark.asyncio
async def test_invalid_date_format(monkeypatch):
    p = _Patcher(monkeypatch)
    result = await kpms_service.derive_availability("not-a-date", "2026-01-01")
    assert "error" in result
    assert "date" in result["error"].lower()
    assert p.list_rooms_calls == []


@pytest.mark.asyncio
async def test_check_out_before_check_in(monkeypatch):
    _Patcher(monkeypatch)
    result = await kpms_service.derive_availability("2026-07-05", "2026-07-01")
    assert "error" in result


# --------------------------------------------------------------------------- #
# Two-property disambiguation                                                 #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_availability_ambiguous_room_without_property_is_refused(monkeypatch):
    """'Deluxe Double Room' exists in some form at both hotels — must ask, not guess."""
    p = _Patcher(monkeypatch)
    result = await kpms_service.derive_availability(
        "2026-07-01", "2026-07-03", room_type_filter="Deluxe Double Room"
    )

    assert "error" in result, f"ambiguous room silently resolved: {result}"
    msg = result["error"]
    assert "Mosvold Villa" in msg and "Sundara by Mosvold" in msg
    assert p.create_reservation_calls == []


@pytest.mark.asyncio
async def test_book_ambiguous_room_without_property_is_refused(monkeypatch):
    p = _Patcher(monkeypatch)
    result = await kpms_service.book(
        check_in="2026-07-01",
        check_out="2026-07-03",
        room_type="Deluxe Twin Room",   # Villa "Deluxe Twin Room" vs Sundara "... with Sea View"
        guest_name="Test",
        guest_email="t@x.com",
    )

    assert "error" in result, f"ambiguous room silently booked: {result}"
    msg = result["error"]
    assert "Mosvold Villa" in msg and "Sundara by Mosvold" in msg
    assert p.create_reservation_calls == []
    assert p.create_guest_calls == []


@pytest.mark.asyncio
async def test_book_room_from_other_property_is_refused(monkeypatch):
    """A Sundara room requested at Mosvold Villa must be refused outright."""
    p = _Patcher(monkeypatch)
    result = await kpms_service.book(
        check_in="2026-07-01",
        check_out="2026-07-03",
        room_type="Beach Villa",          # Sundara only
        property_name="Mosvold Villa",    # ...asked for at the Villa
        guest_name="Test",
        guest_email="t@x.com",
    )

    assert "error" in result, f"cross-property room silently booked: {result}"
    msg = result["error"]
    assert "Beach Villa" in msg
    # Refusal is scoped to the named property and lists only that hotel's rooms.
    assert "Mosvold Villa" in msg
    for name in VILLA_TYPE_NAMES:
        assert name in msg
    assert p.create_reservation_calls == []
    assert p.create_guest_calls == []


@pytest.mark.asyncio
async def test_unknown_property_hint_is_refused(monkeypatch):
    p = _Patcher(monkeypatch)
    result = await kpms_service.derive_availability(
        "2026-07-01", "2026-07-03", property_name="Treehouse Chalets"
    )

    assert "error" in result
    msg = result["error"]
    assert "Mosvold Villa" in msg and "Sundara by Mosvold" in msg
    assert p.list_rooms_calls == []


# --------------------------------------------------------------------------- #
# Booking tests                                                               #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_book_happy_path(monkeypatch):
    p = _Patcher(
        monkeypatch,
        search_guests_result=[],
        create_guest_result={"id": "g_new", "name": "Test", "email": "t@x.com"},
        create_reservation_result={
            "id": "MOSV-2026-000101",
            "roomId": "room_villa_v01",
            "checkIn": "2026-07-01",
            "checkOut": "2026-07-03",
        },
    )

    result = await kpms_service.book(
        check_in="2026-07-01",
        check_out="2026-07-03",
        room_type="Deluxe Double Room",
        property_name="Mosvold Villa",
        guest_name="Test",
        guest_email="t@x.com",
        guest_phone="",
        salutation="Mr",
        num_adults=1,
        num_children=0,
    )

    assert result.get("success") is True, f"Booking failed: {result}"
    assert result["booking_reference"] == "MOSV-2026-000101"
    assert result["room_type"] == "Deluxe Double Room"
    assert result["property"] == "Mosvold Villa"
    assert result["room_number"] == "V01"
    assert result["nights"] == 2
    # No money reaches the caller — only the live-quote message.
    assert result["pricing"] == kpms_service.PRICING_MESSAGE
    assert "total_usd" not in result
    _assert_no_money(result)

    # Verify create_reservation payload
    assert len(p.create_reservation_calls) == 1
    payload = p.create_reservation_calls[0]
    assert payload["status"] == "pending"
    assert payload["paymentStatus"] == "unpaid"
    assert payload["paid"] == "0.00"
    assert payload["source"] == "voice_agent"
    assert payload["guestId"] == "g_new"
    assert payload["roomId"] == "room_villa_v01"
    assert payload["typeId"] == "rt_villa_deluxe_double"
    assert payload["propertyId"] == PROPERTY_ID_VILLA
    assert payload["adults"] == 1
    assert payload["children"] == 0
    # PMS-internal bookkeeping only: placeholder base rate x 2 nights.
    assert payload["total"] == "2.00"


@pytest.mark.asyncio
async def test_no_currency_figures_leak_to_caller(monkeypatch):
    """The placeholder baseRate must stay inside the PMS payload."""
    p = _Patcher(
        monkeypatch,
        search_guests_result=[],
        create_guest_result={"id": "g_new", "name": "Test"},
        create_reservation_result={"id": "MOSV-2026-000102", "roomId": "room_villa_v04"},
    )

    booking = await kpms_service.book(
        check_in="2026-07-01",
        check_out="2026-07-04",
        room_type="Founders Suite",
        property_name="Mosvold Villa",
        guest_name="Test",
    )
    assert booking.get("success") is True, f"Booking failed: {booking}"
    _assert_no_money(booking)

    availability = await kpms_service.derive_availability("2026-07-01", "2026-07-04")
    _assert_no_money(availability)

    # The rate did exist internally — 1.00 x 3 nights — it just never surfaced.
    assert p.create_reservation_calls[0]["total"] == "3.00"


@pytest.mark.asyncio
async def test_book_unknown_room_type(monkeypatch):
    p = _Patcher(monkeypatch)
    result = await kpms_service.book(
        check_in="2026-07-01",
        check_out="2026-07-03",
        room_type="Banana Suite",
        guest_name="Test",
        guest_email="t@x.com",
    )

    assert "error" in result
    msg = result["error"]
    # Friendly message naming every valid type across both properties.
    for vocab in VILLA_TYPE_NAMES + SUNDARA_TYPE_NAMES:
        assert vocab in msg, f"Error message should name '{vocab}': {msg}"
    assert p.create_reservation_calls == []


@pytest.mark.asyncio
async def test_book_no_rooms_available_returns_error(monkeypatch):
    # V01 is the only Deluxe Double Room at the Villa — block it.
    reservations = {
        "room_villa_v01": [
            {"roomId": "room_villa_v01", "checkIn": "2026-07-01", "checkOut": "2026-07-05", "status": "pending"}
        ]
    }
    p = _Patcher(monkeypatch, reservations_by_room=reservations)
    result = await kpms_service.book(
        check_in="2026-07-02",
        check_out="2026-07-03",
        room_type="Deluxe Double Room",
        property_name="Mosvold Villa",
        guest_name="Test",
        guest_email="t@x.com",
    )

    assert "error" in result
    msg = result["error"]
    # User-friendly message naming the requested room and the right hotel.
    assert "Deluxe Double Room" in msg
    assert "Mosvold Villa" in msg
    assert p.create_reservation_calls == []


@pytest.mark.asyncio
async def test_book_guest_match_by_email_first(monkeypatch):
    p = _Patcher(
        monkeypatch,
        search_guests_result=[
            {"id": "g1", "email": "john@x.com", "name": "John"},
            {"id": "g2", "email": "other@x.com", "name": "John"},
        ],
        # create_guest_result intentionally None — must not be called.
        create_reservation_result={
            "id": "MOSV-2026-000103",
            "roomId": "room_villa_v03",
            "checkIn": "2026-07-01",
            "checkOut": "2026-07-03",
        },
    )

    result = await kpms_service.book(
        check_in="2026-07-01",
        check_out="2026-07-03",
        room_type="Family Suite",
        property_name="Mosvold Villa",
        guest_name="John",
        guest_email="john@x.com",
    )

    assert result.get("success") is True, f"Booking failed: {result}"
    assert p.create_guest_calls == [], "create_guest should not be called when email matches"
    assert len(p.create_reservation_calls) == 1
    assert p.create_reservation_calls[0]["guestId"] == "g1"


# --------------------------------------------------------------------------- #
# Cancel tests                                                                #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_cancel_happy_path(monkeypatch):
    p = _Patcher(
        monkeypatch,
        update_reservation_result={
            "id": "MOSV-2026-000104",
            "status": "cancelled",
        },
    )
    result = await kpms_service.cancel("MOSV-2026-000104")

    assert result == {"success": True, "booking_reference": "MOSV-2026-000104", "status": "cancelled"}
    assert p.update_reservation_calls == [("MOSV-2026-000104", {"status": "cancelled"})]
    assert p.delete_reservation_calls == []
