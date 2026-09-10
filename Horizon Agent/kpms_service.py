"""Business logic on top of kpms_client.

All derivation, matching, validation, and customer-facing error shaping lives
here. The thin HTTP layer in kpms_client.py is the ONLY way this module talks
to the PMS — never reach around it.

Public API consumed by tools.py:
    derive_availability(check_in, check_out, num_adults, num_children, room_type_filter)
    book(check_in, check_out, room_type, guest_name, guest_email, guest_phone,
         salutation, num_adults, num_children)
    lookup(reservation_no, guest_email, arrival_from, arrival_to)
    cancel(reservation_id)
"""
from __future__ import annotations

import logging
import os
import time
import uuid
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from kpms_client import (
    KpmsError,
    list_properties,
    list_room_types,
    list_rooms,
    list_reservations,
    get_reservation,
    create_reservation,
    update_reservation,
    search_guests,
    create_guest,
)

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Constants                                                                   #
# --------------------------------------------------------------------------- #

KPMS_PROPERTY_ID_ENV = "KPMS_PROPERTY_ID"
CACHE_TTL_SECONDS = 60.0
SOURCE_TAG = "voice_agent"
BLOCKING_STATUSES: frozenset[str] = frozenset({"pending", "active"})

# Always present these rooms to callers using Kavya's vocabulary, regardless
# of what the PMS calls them (which may include "Suite"/"Chalet" suffixes).
_DISPLAY_NAMES: dict[str, str] = {
    "rt_fifi_monarch": "Mount Monarch",
    "rt_fifi_luxe": "Mount Luxe",
    "rt_fifi_sunrise": "Sunrise Vista",
    "rt_fifi_eco": "Eco Harmony",
    "rt_fifi_forest": "Forest Escape Suite",
}

KAVYA_VOCAB = ("Mount Monarch", "Mount Luxe", "Sunrise Vista", "Eco Harmony", "Forest Escape Suite")


# --------------------------------------------------------------------------- #
# Errors                                                                      #
# --------------------------------------------------------------------------- #

class ServiceError(Exception):
    """Graceful business error (no room, type unknown, etc.)."""


class NoRoomAvailable(ServiceError):
    """No physical room of the requested type free for the requested window."""


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


# --------------------------------------------------------------------------- #
# Name normalization & matching                                               #
# --------------------------------------------------------------------------- #

def _norm(name: str) -> str:
    """Lowercase, strip, drop a trailing ' suite' or ' chalet' token."""
    if not name:
        return ""
    s = name.strip().lower()
    for suffix in (" suite", " chalet"):
        if s.endswith(suffix):
            s = s[: -len(suffix)].rstrip()
            break
    return s


def _kavya_vocab_name(pms_name: str) -> str:
    """Strip ' Suite' / ' Chalet' (case-insensitive) — fallback when typeId unknown.

    NOTE: When you have the typeId, prefer _display_name_for_type() which uses
    the explicit _DISPLAY_NAMES table (Forest Escape Suite keeps its Suite).
    """
    if not pms_name:
        return ""
    s = pms_name.strip()
    low = s.lower()
    for suffix in (" suite", " chalet"):
        if low.endswith(suffix):
            return s[: -len(suffix)].rstrip()
    return s


def _display_name_for_type(type_id: str, pms_name: str = "") -> str:
    """Map a typeId to the canonical Kavya-vocab display name."""
    if type_id in _DISPLAY_NAMES:
        return _DISPLAY_NAMES[type_id]
    return _kavya_vocab_name(pms_name)


def _match_room_type(query: str, room_types: list[dict]) -> dict | None:
    """Match a Kavya-style query to a PMS room type. Raises ServiceError on ambiguity."""
    if not query:
        return None
    qn = _norm(query)
    if not qn:
        return None
    # exact-normalized match first
    exact = [rt for rt in room_types if _norm(rt.get("name", "")) == qn]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        raise ServiceError(f"Multiple room types match '{query}'.")
    # prefix match either direction
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
# Property / room-type / room fetchers (cached)                               #
# --------------------------------------------------------------------------- #

async def _get_property_id() -> str:
    env_val = os.getenv(KPMS_PROPERTY_ID_ENV, "").strip()
    if env_val:
        return env_val
    cached = _cache_get("property_id")
    if cached:
        return cached
    props = await list_properties()
    if not isinstance(props, list) or not props:
        raise ServiceError("No properties configured in the PMS.")
    if len(props) > 1:
        raise ServiceError(
            "Multiple properties found; set KPMS_PROPERTY_ID to choose one."
        )
    pid = props[0].get("id")
    if not pid:
        raise ServiceError("Property record is missing an id.")
    _cache_set("property_id", pid)
    return pid


async def _get_room_types_cached() -> list[dict]:
    cached = _cache_get("room_types")
    if cached is not None:
        return cached
    rts = await list_room_types()
    if not isinstance(rts, list):
        rts = []
    _cache_set("room_types", rts)
    return rts


async def _get_rooms_cached(property_id: str) -> list[dict]:
    key = f"rooms:{property_id}"
    cached = _cache_get(key)
    if cached is not None:
        return cached
    rooms = await list_rooms(property_id=property_id)
    if not isinstance(rooms, list):
        rooms = []
    _cache_set(key, rooms)
    return rooms


# --------------------------------------------------------------------------- #
# Date / overlap helpers                                                      #
# --------------------------------------------------------------------------- #

def _parse_date(s: str) -> date:
    return date.fromisoformat(s)


def _overlaps(ri: date, ro: date, qi: date, qo: date) -> bool:
    """Half-open interval overlap: [ri, ro) intersects [qi, qo)."""
    return ri < qo and ro > qi


def _to_decimal(val: Any) -> Decimal:
    if val is None or val == "":
        return Decimal("0")
    try:
        return Decimal(str(val))
    except Exception:
        return Decimal("0")


def _money(d: Decimal) -> float:
    return float(d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


# --------------------------------------------------------------------------- #
# Room allocation                                                             #
# --------------------------------------------------------------------------- #

async def _reservations_for_room(room_id: str) -> list[dict] | None:
    """Return reservations for a room, or None on PMS error.

    Callers MUST treat None as 'do not consider this room free' to avoid
    double-booking when the PMS is flaky (fail-closed).
    """
    try:
        res = await list_reservations(room_id=room_id)
    except KpmsError as exc:
        logger.warning("Failed to list reservations for room %s: %s", room_id, exc)
        return None
    return res if isinstance(res, list) else []


async def _allocate_room(
    property_id: str,
    type_id: str,
    check_in: date,
    check_out: date,
) -> dict | None:
    """Pick the first non-maintenance room of `type_id` at `property_id` with no
    blocking overlap. Stable order by room.number ascending."""
    rooms = await _get_rooms_cached(property_id)
    candidates = [
        r for r in rooms
        if r.get("typeId") == type_id and r.get("status") != "maintenance"
    ]
    candidates.sort(key=lambda r: str(r.get("number", "")))
    for room in candidates:
        rid = room.get("id")
        if not rid:
            continue
        reservations = await _reservations_for_room(rid)
        if reservations is None:
            # PMS fetch failed — skip this room (fail-closed)
            continue
        conflict = False
        for r in reservations:
            if r.get("status") not in BLOCKING_STATUSES:
                continue
            try:
                ri = _parse_date(r["checkIn"])
                ro = _parse_date(r["checkOut"])
            except (KeyError, ValueError):
                continue
            if _overlaps(ri, ro, check_in, check_out):
                conflict = True
                break
        if not conflict:
            return room
    return None


# --------------------------------------------------------------------------- #
# Guest upsert                                                                #
# --------------------------------------------------------------------------- #

async def _find_or_create_guest(
    name: str,
    email: str,
    phone: str,
    salutation: str = "Mr",
) -> dict:
    name = (name or "").strip()
    email = (email or "").strip()
    phone = (phone or "").strip()

    # 1. search by email (exact, case-insensitive)
    if email:
        try:
            results = await search_guests(email)
        except KpmsError as exc:
            logger.warning("Guest search by email failed: %s", exc)
            results = []
        if isinstance(results, list):
            for g in results:
                if str(g.get("email", "")).strip().lower() == email.lower():
                    return g

    # 2. search by name (exact, case-insensitive trim)
    if name:
        try:
            results = await search_guests(name)
        except KpmsError as exc:
            logger.warning("Guest search by name failed: %s", exc)
            results = []
        if isinstance(results, list):
            for g in results:
                if str(g.get("name", "")).strip().lower() == name.lower():
                    return g

    # 3. create — PMS requires client-generated id (matches seed-data convention)
    payload: dict[str, Any] = {
        "id": f"g_{uuid.uuid4().hex[:12]}",
        "name": name or "Guest",
    }
    if email:
        payload["email"] = email
    if phone:
        payload["phone"] = phone
    if salutation:
        payload["salutation"] = salutation
    return await create_guest(payload)


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
        property_id = await _get_property_id()
        room_types = await _get_room_types_cached()
        rooms = await _get_rooms_cached(property_id)
    except ServiceError as exc:
        return {"error": str(exc)}
    except KpmsError as exc:
        logger.warning("Availability fetch failed: %s", exc)
        return {"error": "We couldn't check availability just now. Please try again in a moment."}

    # Optional filter
    filter_match: dict | None = None
    if room_type_filter:
        try:
            filter_match = _match_room_type(room_type_filter, room_types)
        except ServiceError:
            filter_match = None
        if filter_match is None:
            return {
                "error": (
                    f"We don't have a room type called '{room_type_filter}'. "
                    "Please choose from Mount Monarch, Mount Luxe, Sunrise Vista, "
                    "Eco Harmony, or Forest Escape Suite."
                )
            }
        types_to_check = [filter_match]
    else:
        types_to_check = list(room_types)

    # Build per-room reservation lookup
    rooms_by_type: dict[str, list[dict]] = {}
    for r in rooms:
        if r.get("status") == "maintenance":
            continue
        rooms_by_type.setdefault(r.get("typeId", ""), []).append(r)

    # Nights iterator (each "night" represented by its start date)
    night_dates: list[date] = []
    cur = ci
    while cur < co:
        night_dates.append(cur)
        cur += timedelta(days=1)

    out_rooms: list[dict] = []
    available_count = 0

    for rt in types_to_check:
        type_id = rt.get("id", "")
        candidate_rooms = rooms_by_type.get(type_id, [])
        rate = _to_decimal(rt.get("baseRate"))
        total = _money(rate * Decimal(nights))
        # Capacity is informational only. The KB (hotel_info.txt) is the
        # source of truth for room capacities — kPMS data contradicts it
        # (chalets reported as 2, suites as 5). Same approach as yanolja_service.
        capacity = int(rt.get("capacity") or 0)
        display_name = _display_name_for_type(type_id, rt.get("name", ""))

        # For each candidate room, fetch reservations once
        room_reservations: dict[str, list[tuple[date, date]]] = {}
        for room in candidate_rooms:
            rid = room.get("id", "")
            if not rid:
                continue
            res_list = await _reservations_for_room(rid)
            if res_list is None:
                # PMS fetch failed — mark this room as not-free (fail-closed)
                room_reservations[rid] = None
                continue
            intervals: list[tuple[date, date]] = []
            for r in res_list:
                if r.get("status") not in BLOCKING_STATUSES:
                    continue
                try:
                    intervals.append((_parse_date(r["checkIn"]), _parse_date(r["checkOut"])))
                except (KeyError, ValueError):
                    continue
            room_reservations[rid] = intervals

        # Per night: count rooms free
        unavailable: list[str] = []
        all_nights_have_one = True
        for night_start in night_dates:
            night_end = night_start + timedelta(days=1)
            free_count = 0
            for room in candidate_rooms:
                rid = room.get("id", "")
                intervals = room_reservations.get(rid)
                if intervals is None:
                    # PMS fetch failed for this room; do not count as free
                    continue
                if not any(_overlaps(ri, ro, night_start, night_end) for (ri, ro) in intervals):
                    free_count += 1
            if free_count == 0:
                all_nights_have_one = False
                unavailable.append(night_start.isoformat())

        is_available = bool(candidate_rooms) and all_nights_have_one
        if is_available:
            available_count += 1

        entry: dict[str, Any] = {
            "room_type_id": type_id,
            "room_type_name": display_name,
            "available": is_available,
            "nights": nights,
            "rate_per_night_usd": _money(rate),
            "total_usd": total,
            "capacity": capacity,
        }
        if not is_available and unavailable:
            entry["unavailable_dates"] = unavailable
        out_rooms.append(entry)

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
        property_id = await _get_property_id()
        room_types = await _get_room_types_cached()
    except ServiceError as exc:
        return {"error": str(exc)}
    except KpmsError as exc:
        logger.warning("Setup fetch failed during book: %s", exc)
        return {"error": "We couldn't complete the booking just now. Please try again in a moment or call the hotel directly."}

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

    type_id = rt_match.get("id", "")
    # Capacity check NOT enforced here: kPMS capacity contradicts the KB
    # (chalets reported as 2, suites as 5). KB/LLM enforces capacity instead.
    rate = _to_decimal(rt_match.get("baseRate"))
    total_dec = rate * Decimal(nights)
    total_amt = _money(total_dec)
    rate_amt = _money(rate)
    display_room_type = _display_name_for_type(type_id, rt_match.get("name", ""))

    # Allocate room
    try:
        room = await _allocate_room(property_id, type_id, ci, co)
    except KpmsError as exc:
        logger.warning("Room allocation failed: %s", exc)
        return {"error": "We couldn't complete the booking just now. Please try again in a moment or call the hotel directly."}
    if room is None:
        return {
            "error": (
                f"Sorry, we have no {display_room_type} available for those dates. "
                "Would you like to try different dates?"
            )
        }

    # Guest upsert
    try:
        guest = await _find_or_create_guest(guest_name, guest_email, guest_phone, salutation)
    except KpmsError as exc:
        logger.warning("Guest upsert failed: %s", exc)
        return {"error": "We couldn't complete the booking just now. Please try again in a moment or call the hotel directly."}

    guest_id = guest.get("id")
    if not guest_id:
        logger.warning("Guest record missing id: %s", guest)
        return {"error": "We couldn't complete the booking just now. Please try again in a moment or call the hotel directly."}

    payload: dict[str, Any] = {
        "guestId": guest_id,
        "roomId": room.get("id"),
        "typeId": type_id,
        "propertyId": property_id,
        "checkIn": check_in,
        "checkOut": check_out,
        "adults": int(num_adults or 1),
        "children": int(num_children or 0),
        "source": SOURCE_TAG,
        "status": "pending",
        "paymentStatus": "unpaid",
        "total": f"{total_dec.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)}",
        "paid": "0.00",
    }

    # KNOWN GAP: No conflict re-check between _allocate_room and POST. PMS does
    # not enforce booking uniqueness server-side. Two concurrent voice calls
    # could in theory race on the same room. Risk is low at current call volume;
    # if it materialises, add a `list_reservations(room_id=room.id)` re-check
    # here and retry _allocate_room on conflict.
    try:
        created = await create_reservation(payload)
    except KpmsError as exc:
        logger.warning("Reservation create failed: %s", exc)
        return {"error": "We couldn't complete the booking just now. Please try again in a moment or call the hotel directly."}

    booking_ref = created.get("id") if isinstance(created, dict) else None
    if not booking_ref:
        logger.warning("Reservation response missing id: %s", created)
        return {"error": "We couldn't complete the booking just now. Please try again in a moment or call the hotel directly."}

    return {
        "success": True,
        "booking_reference": booking_ref,
        "guest_name": guest_name,
        "check_in": check_in,
        "check_out": check_out,
        "room_type": display_room_type,
        "room_number": room.get("number", ""),
        "rate_per_night_usd": rate_amt,
        "total_usd": total_amt,
        "nights": nights,
    }


# --------------------------------------------------------------------------- #
# lookup                                                                      #
# --------------------------------------------------------------------------- #

def _augment_reservation(res: dict, room_types: list[dict], rooms: list[dict]) -> dict:
    type_id = res.get("typeId", "")
    rt = next((r for r in room_types if r.get("id") == type_id), {})
    room_id = res.get("roomId", "")
    room = next((r for r in rooms if r.get("id") == room_id), {})
    augmented = dict(res)
    augmented["booking_reference"] = res.get("id", "")
    augmented["room_number"] = room.get("number", "")
    augmented["room_type_name"] = _display_name_for_type(type_id, rt.get("name", ""))
    return augmented


async def lookup(
    reservation_no: str = "",
    guest_email: str = "",
    arrival_from: str = "",
    arrival_to: str = "",
) -> dict:
    reservation_no = (reservation_no or "").strip()
    guest_email = (guest_email or "").strip()
    arrival_from = (arrival_from or "").strip()
    arrival_to = (arrival_to or "").strip()

    try:
        property_id = await _get_property_id()
        room_types = await _get_room_types_cached()
        rooms = await _get_rooms_cached(property_id)
    except ServiceError as exc:
        return {"error": str(exc)}
    except KpmsError as exc:
        logger.warning("Lookup setup failed: %s", exc)
        return {"error": "We couldn't look that up just now. Please try again in a moment."}

    # By reservation number — direct fetch
    if reservation_no:
        try:
            res = await get_reservation(reservation_no)
        except KpmsError as exc:
            logger.warning("Reservation %s lookup failed: %s", reservation_no, exc)
            return {"error": "We couldn't find that booking. Please double-check the reference."}
        if not isinstance(res, dict) or not res.get("id"):
            return {"error": "We couldn't find that booking. Please double-check the reference."}
        return _augment_reservation(res, room_types, rooms)

    # By email and/or date range
    if not guest_email and not (arrival_from or arrival_to):
        return {"error": "Please provide a booking reference or guest email to look up."}

    matches: list[dict] = []

    if guest_email:
        try:
            guests = await search_guests(guest_email)
        except KpmsError as exc:
            logger.warning("Guest search failed: %s", exc)
            guests = []
        guest_ids = [
            g.get("id") for g in (guests or [])
            if str(g.get("email", "")).strip().lower() == guest_email.lower() and g.get("id")
        ]
        for gid in guest_ids:
            try:
                res_list = await list_reservations(guest_id=gid)
            except KpmsError as exc:
                logger.warning("Reservation search by guest failed: %s", exc)
                continue
            if isinstance(res_list, list):
                matches.extend(res_list)

    # Date-range filter (uses checkIn between arrival_from and arrival_to inclusive)
    if arrival_from or arrival_to:
        try:
            af = _parse_date(arrival_from) if arrival_from else None
            at = _parse_date(arrival_to) if arrival_to else None
        except ValueError:
            return {"error": "Invalid date format. Please use YYYY-MM-DD."}

        if not matches:
            # No email filter — fetch by checkIn directly if provided
            try:
                res_list = await list_reservations(
                    check_in=arrival_from or None,
                    check_out=arrival_to or None,
                )
            except KpmsError as exc:
                logger.warning("Reservation date search failed: %s", exc)
                res_list = []
            if isinstance(res_list, list):
                matches = res_list

        def _in_range(r: dict) -> bool:
            try:
                ci = _parse_date(r.get("checkIn", ""))
            except (ValueError, TypeError):
                return False
            if af and ci < af:
                return False
            if at and ci > at:
                return False
            return True

        matches = [r for r in matches if _in_range(r)]

    # Deduplicate by id
    seen: set[str] = set()
    deduped: list[dict] = []
    for r in matches:
        rid = r.get("id", "")
        if rid and rid not in seen:
            seen.add(rid)
            deduped.append(r)

    if not deduped:
        return {"error": "We couldn't find any bookings matching those details."}
    if len(deduped) == 1:
        return _augment_reservation(deduped[0], room_types, rooms)
    return {
        "matches": [_augment_reservation(r, room_types, rooms) for r in deduped],
        "count": len(deduped),
    }


# --------------------------------------------------------------------------- #
# cancel                                                                      #
# --------------------------------------------------------------------------- #

async def cancel(reservation_id: str) -> dict:
    rid = (reservation_id or "").strip()
    if not rid:
        return {"error": "Please provide the booking reference to cancel."}

    # Check current state first so we can return distinct messages for
    # not-found vs already-cancelled vs OK-to-cancel.
    try:
        existing = await get_reservation(rid)
    except KpmsError as exc:
        if getattr(exc, "status", None) == 404:
            return {"error": "We couldn't find a booking with that reference. Please double-check the number."}
        logger.warning("Cancel pre-check failed for %s: %s", rid, exc)
        return {"error": "We couldn't cancel that booking just now. Please try again or call the hotel directly."}

    if isinstance(existing, dict) and existing.get("status") == "cancelled":
        return {"success": True, "booking_reference": rid, "status": "cancelled", "already_cancelled": True}

    try:
        updated = await update_reservation(rid, {"status": "cancelled"})
    except KpmsError as exc:
        if getattr(exc, "status", None) == 404:
            return {"error": "We couldn't find a booking with that reference. Please double-check the number."}
        logger.warning("Cancel failed for %s: %s", rid, exc)
        return {"error": "We couldn't cancel that booking just now. Please try again or call the hotel directly."}

    # Verify the status actually flipped; if not, re-fetch to be sure.
    if isinstance(updated, dict) and updated.get("status") == "cancelled":
        return {"success": True, "booking_reference": rid, "status": "cancelled"}

    try:
        confirm = await get_reservation(rid)
    except KpmsError as exc:
        logger.warning("Cancel verify failed for %s: %s", rid, exc)
        return {"error": "Cancellation submitted but we couldn't confirm it. Please call the hotel to verify."}

    if isinstance(confirm, dict) and confirm.get("status") == "cancelled":
        return {"success": True, "booking_reference": rid, "status": "cancelled"}

    logger.warning("Cancel did not flip status for %s; final: %s", rid, confirm)
    return {"error": "Cancellation submitted but the booking is still showing active. Please call the hotel to confirm."}
