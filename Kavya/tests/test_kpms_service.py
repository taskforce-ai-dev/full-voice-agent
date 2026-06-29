"""Offline unit tests for kpms_service business logic.

All kpms_client calls are mocked with monkeypatch.setattr on the
kpms_service module's namespace (since kpms_service imports names
directly via `from kpms_client import ...`).
"""
from __future__ import annotations

import os
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
# Canned fixtures (real shape from the live API)                              #
# --------------------------------------------------------------------------- #

PROPERTIES = [{"id": "p_fifi", "name": "Fifi Resorts (Pvt) Ltd"}]

ROOM_TYPES = [
    {"id": "rt_fifi_eco", "name": "Eco Harmony Suite", "baseRate": "119.00", "capacity": 2},
    {"id": "rt_fifi_sunrise", "name": "Sunrise Vista Suite", "baseRate": "139.00", "capacity": 2},
    {"id": "rt_fifi_forest", "name": "Forest Escape Suite", "baseRate": "175.00", "capacity": 3},
    {"id": "rt_fifi_luxe", "name": "Mount Luxe Chalet", "baseRate": "225.00", "capacity": 2},
    {"id": "rt_fifi_monarch", "name": "Mount Monarch Chalet", "baseRate": "225.00", "capacity": 2},
]

ROOMS = [
    {"id": "room_fifi_c01", "number": "C01", "typeId": "rt_fifi_monarch", "status": "occupied", "propertyId": "p_fifi"},
    {"id": "room_fifi_c02", "number": "C02", "typeId": "rt_fifi_luxe", "status": "dirty", "propertyId": "p_fifi"},
    {"id": "room_fifi_s01", "number": "S01", "typeId": "rt_fifi_sunrise", "status": "available", "propertyId": "p_fifi"},
    {"id": "room_fifi_s02", "number": "S02", "typeId": "rt_fifi_forest", "status": "occupied", "propertyId": "p_fifi"},
    {"id": "room_fifi_s03", "number": "S03", "typeId": "rt_fifi_eco", "status": "dirty", "propertyId": "p_fifi"},
]


# --------------------------------------------------------------------------- #
# Fixtures                                                                    #
# --------------------------------------------------------------------------- #

@pytest.fixture(autouse=True)
def _clear_cache_and_env(monkeypatch):
    """Reset module-level cache and force property id via env var."""
    kpms_service._cache.clear()
    monkeypatch.setenv("KPMS_PROPERTY_ID", "p_fifi")
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
# Availability tests                                                          #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_derive_availability_all_free(monkeypatch):
    _Patcher(monkeypatch)
    result = await kpms_service.derive_availability("2026-07-01", "2026-07-03")

    assert "error" not in result
    assert result["total_room_types"] == 5
    assert result["available_room_types"] == 5
    assert result["nights"] == 2
    assert len(result["rooms"]) == 5
    for entry in result["rooms"]:
        assert entry["available"] is True, f"{entry['room_type_name']} should be available"
    # Rate sanity
    rates = {r["room_type_name"]: r["rate_per_night_usd"] for r in result["rooms"]}
    assert rates["Sunrise Vista"] == 139.00
    assert rates["Eco Harmony"] == 119.00
    assert rates["Forest Escape Suite"] == 175.00
    assert rates["Mount Luxe"] == 225.00
    assert rates["Mount Monarch"] == 225.00


@pytest.mark.asyncio
async def test_derive_availability_partial_overlap(monkeypatch):
    reservations = {
        "room_fifi_s01": [
            {"roomId": "room_fifi_s01", "checkIn": "2026-07-02", "checkOut": "2026-07-04", "status": "pending"}
        ]
    }
    _Patcher(monkeypatch, reservations_by_room=reservations)
    result = await kpms_service.derive_availability("2026-07-01", "2026-07-05")

    assert "error" not in result
    by_name = {r["room_type_name"]: r for r in result["rooms"]}
    sunrise = by_name["Sunrise Vista"]
    assert sunrise["available"] is False
    assert "unavailable_dates" in sunrise
    assert "2026-07-02" in sunrise["unavailable_dates"]
    assert "2026-07-03" in sunrise["unavailable_dates"]
    # Others stay available
    for name in ("Eco Harmony", "Forest Escape Suite", "Mount Luxe", "Mount Monarch"):
        assert by_name[name]["available"] is True, f"{name} should stay available"
    assert result["available_room_types"] == 4


@pytest.mark.asyncio
async def test_overlap_touching_is_not_overlap(monkeypatch):
    # Reservation ends on 2026-07-01 exactly when new stay begins.
    reservations = {
        "room_fifi_s01": [
            {"roomId": "room_fifi_s01", "checkIn": "2026-06-29", "checkOut": "2026-07-01", "status": "pending"}
        ]
    }
    _Patcher(monkeypatch, reservations_by_room=reservations)
    result = await kpms_service.derive_availability("2026-07-01", "2026-07-03")

    by_name = {r["room_type_name"]: r for r in result["rooms"]}
    assert by_name["Sunrise Vista"]["available"] is True


@pytest.mark.asyncio
async def test_cancelled_reservation_doesnt_block(monkeypatch):
    reservations = {
        "room_fifi_s01": [
            {"roomId": "room_fifi_s01", "checkIn": "2026-07-01", "checkOut": "2026-07-04", "status": "cancelled"}
        ]
    }
    _Patcher(monkeypatch, reservations_by_room=reservations)
    result = await kpms_service.derive_availability("2026-07-01", "2026-07-03")

    by_name = {r["room_type_name"]: r for r in result["rooms"]}
    assert by_name["Sunrise Vista"]["available"] is True


@pytest.mark.asyncio
async def test_maintenance_room_excluded(monkeypatch):
    rooms = deepcopy(ROOMS)
    for r in rooms:
        if r["id"] == "room_fifi_s01":
            r["status"] = "maintenance"
    _Patcher(monkeypatch, rooms=rooms)
    result = await kpms_service.derive_availability("2026-07-01", "2026-07-03")

    by_name = {r["room_type_name"]: r for r in result["rooms"]}
    assert by_name["Sunrise Vista"]["available"] is False
    # Other types unaffected
    for name in ("Eco Harmony", "Forest Escape Suite", "Mount Luxe", "Mount Monarch"):
        assert by_name[name]["available"] is True


@pytest.mark.asyncio
async def test_capacity_filter(monkeypatch):
    # capacity filter currently happens only through the response data — the
    # service still returns all types but with capacity field. The test should
    # verify only Forest Escape Suite (capacity 3) is suitable for 3 adults.
    _Patcher(monkeypatch)
    result = await kpms_service.derive_availability(
        "2026-07-01", "2026-07-03", num_adults=3, num_children=0
    )

    by_name = {r["room_type_name"]: r for r in result["rooms"]}
    # Forest Escape Suite has capacity 3 — should be available.
    assert by_name["Forest Escape Suite"]["available"] is True
    # All others (capacity 2) should be marked unavailable due to capacity.
    for name in ("Eco Harmony", "Sunrise Vista", "Mount Luxe", "Mount Monarch"):
        assert by_name[name]["available"] is False, f"{name} (cap 2) should be unavailable for 3 adults"


@pytest.mark.asyncio
async def test_room_type_filter_matches_kavya_vocab(monkeypatch):
    _Patcher(monkeypatch)
    result = await kpms_service.derive_availability(
        "2026-07-01", "2026-07-03", room_type_filter="Mount Luxe"
    )

    assert "error" not in result
    assert len(result["rooms"]) == 1
    entry = result["rooms"][0]
    assert entry["room_type_name"] == "Mount Luxe"
    assert entry["room_type_id"] == "rt_fifi_luxe"


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
# Booking tests                                                               #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_book_happy_path(monkeypatch):
    p = _Patcher(
        monkeypatch,
        search_guests_result=[],
        create_guest_result={"id": "g_new", "name": "Test", "email": "t@x.com"},
        create_reservation_result={
            "id": "FR-30200-9999",
            "roomId": "room_fifi_s01",
            "checkIn": "2026-07-01",
            "checkOut": "2026-07-03",
            "total": "278.00",
        },
    )

    result = await kpms_service.book(
        check_in="2026-07-01",
        check_out="2026-07-03",
        room_type="Sunrise Vista",
        guest_name="Test",
        guest_email="t@x.com",
        guest_phone="",
        salutation="Mr",
        num_adults=1,
        num_children=0,
    )

    assert result.get("success") is True, f"Booking failed: {result}"
    assert result["booking_reference"] == "FR-30200-9999"
    assert result["room_type"] == "Sunrise Vista"
    assert result["room_number"] == "S01"
    assert result["total_usd"] == 278.0
    assert result["nights"] == 2

    # Verify create_reservation payload
    assert len(p.create_reservation_calls) == 1
    payload = p.create_reservation_calls[0]
    assert payload["status"] == "pending"
    assert payload["paymentStatus"] == "unpaid"
    assert payload["paid"] == "0.00"
    assert payload["source"] == "voice_agent"
    assert payload["guestId"] == "g_new"
    assert payload["roomId"] == "room_fifi_s01"
    assert payload["typeId"] == "rt_fifi_sunrise"
    assert payload["total"] == "278.00"
    assert payload["adults"] == 1
    assert payload["children"] == 0


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
    # Friendly message naming the five valid types.
    for vocab in ("Mount Monarch", "Mount Luxe", "Sunrise Vista", "Eco Harmony", "Forest Escape Suite"):
        assert vocab in msg, f"Error message should name '{vocab}': {msg}"
    assert p.create_reservation_calls == []


@pytest.mark.asyncio
async def test_book_no_rooms_available_returns_error(monkeypatch):
    # Only sunrise room is s01 — block it with an overlapping pending reservation.
    reservations = {
        "room_fifi_s01": [
            {"roomId": "room_fifi_s01", "checkIn": "2026-07-01", "checkOut": "2026-07-05", "status": "pending"}
        ]
    }
    p = _Patcher(monkeypatch, reservations_by_room=reservations)
    result = await kpms_service.book(
        check_in="2026-07-02",
        check_out="2026-07-03",
        room_type="Sunrise Vista",
        guest_name="Test",
        guest_email="t@x.com",
    )

    assert "error" in result
    msg = result["error"]
    # User-friendly message naming the requested Kavya-vocab type.
    assert "Sunrise Vista" in msg
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
            "id": "FR-30200-7777",
            "roomId": "room_fifi_s01",
            "checkIn": "2026-07-01",
            "checkOut": "2026-07-03",
            "total": "278.00",
        },
    )

    result = await kpms_service.book(
        check_in="2026-07-01",
        check_out="2026-07-03",
        room_type="Sunrise Vista",
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
            "id": "FR-30200-1010",
            "status": "cancelled",
        },
    )
    result = await kpms_service.cancel("FR-30200-1010")

    assert result == {"success": True, "booking_reference": "FR-30200-1010", "status": "cancelled"}
    assert p.update_reservation_calls == [("FR-30200-1010", {"status": "cancelled"})]
    assert p.delete_reservation_calls == []
