"""Business logic on top of yanolja_client.

Mirrors the public API of kpms_service.py exactly. booking_api.py consumes:
    derive_availability(check_in, check_out, num_adults, num_children, room_type_filter)
    book(check_in, check_out, room_type, guest_name, guest_email, guest_phone,
         salutation, num_adults, num_children)
    lookup(reservation_no, guest_email, arrival_from, arrival_to)
    cancel(reservation_id)
"""
from __future__ import annotations

import logging
import time
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from yanolja_client import (
    YanoljaError,
    list_rooms,
    get_availability,
    list_reservations,
    create_guest,
    create_reservation,
    get_reservation,
    cancel_reservation,
)

# Reservation statuses that block a room for the requested window.
# Mirrors the n8n chat agent's "Compute Availability" logic.
_ACTIVE_RES_STATUSES = {"pending", "confirmed", "checked_in", "checked-in", "checkedin"}


async def _free_rooms_from_reservations(
    all_rooms: list[dict], check_in: str, check_out: str
) -> list[dict]:
    """Return the subset of physical rooms with no overlapping active reservation
    in [check_in, check_out). Computed locally from /reservations to avoid the
    PMS's broken /rooms/availability endpoint (which uses room.status as a
    sticky flag instead of doing date-range overlap)."""
    try:
        reservations = await list_reservations()
    except YanoljaError:
        raise
    occupied: set[Any] = set()
    for r in reservations:
        status = str(r.get("status", "")).lower()
        if status not in _ACTIVE_RES_STATUSES:
            continue
        ci = str(r.get("checkIn", ""))[:10]
        co = str(r.get("checkOut", ""))[:10]
        room_id = r.get("roomId")
        if not ci or not co or room_id is None:
            continue
        # Overlap with [check_in, check_out): ci < check_out AND co > check_in
        if ci < check_out and co > check_in:
            occupied.add(room_id)
    return [r for r in all_rooms if r.get("id") not in occupied]

logger = logging.getLogger(__name__)

CACHE_TTL_SECONDS = 60.0

KAVYA_VOCAB = ("Mount Monarch", "Mount Luxe", "Sunrise Vista", "Eco Harmony", "Forest Escape Suite")


class ServiceError(Exception):
    """Graceful business error."""


# --------------------------------------------------------------------------- #
# Cache                                                                       #
# --------------------------------------------------------------------------- #

_cache: dict[str, tuple[float, Any]] = {}


def _cache_get(key: str) -> Any | None:
    entry = _cache.get(key)
    if entry is None:
        return None
    expires_at, value = entry
    if time.monotonic() >= expires_at:
        _cache.pop(key, None)
        return None
    return value


def _cache_set(key: str, value: Any, ttl: float = CACHE_TTL_SECONDS) -> None:
    _cache[key] = (time.monotonic() + ttl, value)


def _cache_invalidate(key: str) -> None:
    _cache.pop(key, None)


# --------------------------------------------------------------------------- #
# Name normalization                                                          #
# --------------------------------------------------------------------------- #

def _norm(name: str) -> str:
    if not name:
        return ""
    s = name.strip().lower()
    for suffix in (" suite", " chalet"):
        if s.endswith(suffix):
            s = s[: -len(suffix)].rstrip()
            break
    return s


def _kavya_vocab_name(pms_name: str) -> str:
    if not pms_name:
        return ""
    s = pms_name.strip()
    low = s.lower()
    for suffix in (" suite", " chalet"):
        if low.endswith(suffix):
            return s[: -len(suffix)].rstrip()
    return s


def _display_name(rt: dict) -> str:
    """Canonical Kavya-vocab name. Match PMS name case-insensitively to vocab."""
    pms = (rt.get("name") or "").strip()
    pn = _norm(pms)
    for v in KAVYA_VOCAB:
        if _norm(v) == pn:
            return v
    return _kavya_vocab_name(pms)


def _match_room_type(query: str, room_types: list[dict]) -> dict | None:
    """Match a Kavya-style query to a room type dict. Raises ServiceError on ambiguity."""
    if not query:
        return None
    qn = _norm(query)
    if not qn:
        return None
    exact = [rt for rt in room_types if _norm(rt.get("name", "")) == qn]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        raise ServiceError(f"Multiple room types match '{query}'.")
    prefix = []
    for rt in room_types:
        n = _norm(rt.get("name", ""))
        if not n:
            continue
        if n.startswith(qn) or qn.startswith(n):
            prefix.append(rt)
    if len(prefix) == 1:
        return prefix[0]
    if len(prefix) > 1:
        raise ServiceError(f"Multiple room types match '{query}'.")
    return None


# --------------------------------------------------------------------------- #
# Fetchers                                                                    #
# --------------------------------------------------------------------------- #

async def _get_rooms_cached() -> list[dict]:
    cached = _cache_get("rooms")
    if cached is not None:
        return cached
    rooms = await list_rooms()
    if not isinstance(rooms, list):
        rooms = []
    _cache_set("rooms", rooms)
    return rooms


def _room_types_from_rooms(rooms: list[dict]) -> list[dict]:
    """Extract unique roomType dicts from rooms list, keyed by id."""
    seen: dict[Any, dict] = {}
    for r in rooms:
        rt = r.get("roomType")
        if not isinstance(rt, dict):
            continue
        rid = rt.get("id")
        if rid is not None and rid not in seen:
            seen[rid] = rt
    return list(seen.values())


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #

def _parse_date(s: str) -> date:
    return date.fromisoformat(s)


def _to_decimal(val: Any) -> Decimal:
    if val is None or val == "":
        return Decimal("0")
    try:
        return Decimal(str(val))
    except Exception:
        return Decimal("0")


def _money(d: Decimal) -> float:
    return float(d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _split_name(full: str) -> tuple[str, str]:
    # Yanolja /guests requires both firstName and lastName, so we never return
    # an empty last name — fall back to a placeholder for single-token inputs.
    full = (full or "").strip()
    if not full:
        return ("Guest", ".")
    parts = full.split(None, 1)
    if len(parts) == 1:
        return (parts[0], ".")
    return (parts[0], parts[1])


# --------------------------------------------------------------------------- #
# derive_availability                                                         #
# --------------------------------------------------------------------------- #

async def derive_availability(
    check_in: str,
    check_out: str,
    num_adults: int = 1,
    num_children: int = 0,
    room_type_filter: str = "",
) -> dict:
    try:
        ci = _parse_date(check_in)
        co = _parse_date(check_out)
    except (ValueError, TypeError):
        return {"error": "Invalid date format. Please use YYYY-MM-DD."}
    if co <= ci:
        return {"error": "Check-out must be after check-in."}

    nights = (co - ci).days

    try:
        all_rooms = await _get_rooms_cached()
        avail_rooms = await _free_rooms_from_reservations(all_rooms, check_in, check_out)
    except YanoljaError as exc:
        logger.warning("Availability fetch failed: %s", exc)
        return {"error": "We couldn't check availability just now. Please try again in a moment."}

    room_types = _room_types_from_rooms(all_rooms)

    if room_type_filter:
        try:
            match = _match_room_type(room_type_filter, room_types)
        except ServiceError:
            match = None
        if match is None:
            return {
                "error": (
                    f"We don't have a room type called '{room_type_filter}'. "
                    "Please choose from Mount Monarch, Mount Luxe, Sunrise Vista, "
                    "Eco Harmony, or Forest Escape Suite."
                )
            }
        types_to_check = [match]
    else:
        types_to_check = room_types

    # Which roomTypeIds appear in available rooms
    available_type_ids: set[Any] = set()
    for r in avail_rooms:
        rtid = r.get("roomTypeId")
        if rtid is not None:
            available_type_ids.add(rtid)

    # PMS is consulted ONLY for availability and booking creation. Rates,
    # capacity, and room descriptions come from the KB (hotel_info.txt) —
    # the PMS data for those fields is unreliable. So we strip everything
    # but the available flag and room name from the tool result.
    out_rooms: list[dict] = []
    available_count = 0
    for rt in types_to_check:
        type_id = rt.get("id")
        display_name = _display_name(rt)
        is_available = type_id in available_type_ids
        if is_available:
            available_count += 1
        out_rooms.append({
            "room_type_name": display_name,
            "available": is_available,
        })

    return {
        "check_in": check_in,
        "check_out": check_out,
        "nights": nights,
        "total_room_types": len(room_types),
        "available_room_types": available_count,
        "rooms": out_rooms,
    }


# --------------------------------------------------------------------------- #
# book                                                                        #
# --------------------------------------------------------------------------- #

async def book(
    check_in: str,
    check_out: str,
    room_type: str,
    guest_name: str,
    guest_email: str = "",
    guest_phone: str = "",
    salutation: str = "Mr",
    num_adults: int = 1,
    num_children: int = 0,
) -> dict:
    try:
        ci = _parse_date(check_in)
        co = _parse_date(check_out)
    except (ValueError, TypeError):
        return {"error": "Invalid date format. Please use YYYY-MM-DD."}
    if co <= ci:
        return {"error": "Check-out must be after check-in."}

    nights = (co - ci).days

    if not (guest_name or "").strip() and not (guest_email or "").strip():
        return {"error": "We need a guest name to make the reservation."}

    try:
        all_rooms = await _get_rooms_cached()
    except YanoljaError as exc:
        logger.warning("Setup fetch failed during book: %s", exc)
        return {"error": "We couldn't complete the booking just now. Please try again in a moment or call the hotel directly."}

    room_types = _room_types_from_rooms(all_rooms)
    try:
        rt_match = _match_room_type(room_type, room_types)
    except ServiceError:
        rt_match = None
    if not rt_match:
        return {
            "error": (
                f"We don't have a room type called '{room_type}'. "
                "Please choose from Mount Monarch, Mount Luxe, Sunrise Vista, "
                "Eco Harmony, or Forest Escape Suite."
            )
        }

    type_id = rt_match.get("id")
    display_room_type = _display_name(rt_match)

    # Capacity NOT enforced here: PMS maxOccupancy contradicts the KB
    # (e.g. Mount Luxe is up to 5 in the KB, 2 in the PMS). The LLM uses
    # the KB to decide pax fit before calling this tool.

    rate = _to_decimal(rt_match.get("basePrice"))
    rate_amt = _money(rate)
    total_amt = _money(rate * Decimal(nights))

    # Find a free room of this type
    try:
        all_rooms = await _get_rooms_cached()
        avail_rooms = await _free_rooms_from_reservations(all_rooms, check_in, check_out)
    except YanoljaError as exc:
        logger.warning("Availability fetch failed during book: %s", exc)
        return {"error": "We couldn't complete the booking just now. Please try again in a moment or call the hotel directly."}

    candidates = [r for r in avail_rooms if r.get("roomTypeId") == type_id]
    candidates.sort(key=lambda r: str(r.get("roomNumber", "")))
    if not candidates:
        return {
            "error": (
                f"Sorry, we have no {display_room_type} available for those dates. "
                "Would you like to try different dates?"
            )
        }
    room = candidates[0]
    room_id = room.get("id")

    # Create guest
    first, last = _split_name(guest_name)
    guest_payload: dict[str, Any] = {"firstName": first, "lastName": last}
    if guest_email:
        guest_payload["email"] = guest_email.strip()
    if guest_phone:
        guest_payload["phone"] = guest_phone.strip()

    try:
        guest = await create_guest(guest_payload)
    except YanoljaError as exc:
        logger.warning("Guest create failed: %s", exc)
        return {"error": "We couldn't complete the booking just now. Please try again in a moment or call the hotel directly."}

    guest_id = guest.get("id")
    if not guest_id:
        logger.warning("Guest response missing id: %s", guest)
        return {"error": "We couldn't complete the booking just now. Please try again in a moment or call the hotel directly."}

    res_payload: dict[str, Any] = {
        "guestId": guest_id,
        "roomId": room_id,
        "roomTypeId": type_id,
        "checkIn": check_in,
        "checkOut": check_out,
        "adults": int(num_adults or 1),
        "children": int(num_children or 0),
    }

    try:
        created = await create_reservation(res_payload)
    except YanoljaError as exc:
        logger.warning("Reservation create failed: %s", exc)
        return {"error": "We couldn't complete the booking just now. Please try again in a moment or call the hotel directly."}

    booking_ref = created.get("id") if isinstance(created, dict) else None
    if not booking_ref:
        logger.warning("Reservation response missing id: %s", created)
        return {"error": "We couldn't complete the booking just now. Please try again in a moment or call the hotel directly."}

    # Invalidate rooms cache so next call sees fresh status
    _cache_invalidate("rooms")

    return {
        "success": True,
        "booking_reference": str(booking_ref),
        "guest_name": guest_name,
        "check_in": check_in,
        "check_out": check_out,
        "room_type": display_room_type,
        "room_number": room.get("roomNumber", ""),
        "rate_per_night_usd": rate_amt,
        "total_usd": total_amt,
        "nights": nights,
    }


# --------------------------------------------------------------------------- #
# lookup                                                                      #
# --------------------------------------------------------------------------- #

def _augment(res: dict) -> dict:
    augmented = dict(res)
    augmented["booking_reference"] = str(res.get("id", "") or res.get("reservationNumber", ""))
    room = res.get("room") or {}
    rt = res.get("roomType") or {}
    augmented["room_number"] = room.get("roomNumber", "")
    augmented["room_type_name"] = _display_name(rt) if rt else ""
    return augmented


async def lookup(
    reservation_no: str = "",
    guest_email: str = "",
    arrival_from: str = "",
    arrival_to: str = "",
) -> dict:
    reservation_no = (reservation_no or "").strip()

    if not reservation_no:
        return {"error": "Please provide a booking reference to look up a booking."}

    try:
        res = await get_reservation(reservation_no)
    except YanoljaError as exc:
        if getattr(exc, "status", None) == 404:
            return {"error": "We couldn't find that booking. Please double-check the reference."}
        logger.warning("Reservation %s lookup failed: %s", reservation_no, exc)
        return {"error": "We couldn't look that up just now. Please try again in a moment."}

    if not isinstance(res, dict) or not res.get("id"):
        return {"error": "We couldn't find that booking. Please double-check the reference."}

    return _augment(res)


# --------------------------------------------------------------------------- #
# cancel                                                                      #
# --------------------------------------------------------------------------- #

async def cancel(reservation_id: str) -> dict:
    rid = (str(reservation_id) if reservation_id is not None else "").strip()
    if not rid:
        return {"error": "Please provide the booking reference to cancel."}

    try:
        existing = await get_reservation(rid)
    except YanoljaError as exc:
        if getattr(exc, "status", None) == 404:
            return {"error": "We couldn't find a booking with that reference. Please double-check the number."}
        logger.warning("Cancel pre-check failed for %s: %s", rid, exc)
        return {"error": "We couldn't cancel that booking just now. Please try again or call the hotel directly."}

    if isinstance(existing, dict) and existing.get("status") == "cancelled":
        return {"success": True, "booking_reference": rid, "status": "cancelled", "already_cancelled": True}

    try:
        updated = await cancel_reservation(rid)
    except YanoljaError as exc:
        if getattr(exc, "status", None) == 404:
            return {"error": "We couldn't find a booking with that reference. Please double-check the number."}
        logger.warning("Cancel failed for %s: %s", rid, exc)
        return {"error": "We couldn't cancel that booking just now. Please try again or call the hotel directly."}

    if isinstance(updated, dict) and updated.get("status") == "cancelled":
        _cache_invalidate("rooms")
        return {"success": True, "booking_reference": rid, "status": "cancelled"}

    # verify
    try:
        confirm = await get_reservation(rid)
    except YanoljaError as exc:
        logger.warning("Cancel verify failed for %s: %s", rid, exc)
        return {"error": "Cancellation submitted but we couldn't confirm it. Please call the hotel to verify."}

    if isinstance(confirm, dict) and confirm.get("status") == "cancelled":
        _cache_invalidate("rooms")
        return {"success": True, "booking_reference": rid, "status": "cancelled"}

    logger.warning("Cancel did not flip status for %s; final: %s", rid, confirm)
    return {"error": "Cancellation submitted but the booking is still showing active. Please call the hotel to confirm."}
