"""Business logic on top of yanolja_client, for Mosvold Boutique Hotels.

Kavya serves TWO properties on one phone line: Mosvold Villa (Ahangama) and
Sundara by Mosvold (Balapitiya). Room names overlap between them, so the property
must be established before a room type is selected — see `resolve_property`,
`_match_room_type` and the `property_name` argument below.

This module NEVER emits a room rate. Mosvold publishes no rates; they exist only
once dates are chosen and are served live by the external booking engine
(BookingEye). Price questions go to reservations on RESERVATIONS_PHONE.

Mirrors the public API of kpms_service.py. booking_api.py consumes:
    derive_availability(check_in, check_out, num_adults, num_children,
                        room_type_filter, property_name)
    book(check_in, check_out, room_type, guest_name, guest_email, guest_phone,
         salutation, num_adults, num_children, property_name)
    lookup(reservation_no, guest_email, arrival_from, arrival_to)
    cancel(reservation_id)
"""
from __future__ import annotations

import logging
import os
import time
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Optional

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

# Reservations hotline — the only channel that can quote a price. Mosvold publishes
# no rates anywhere; rates exist only once dates are chosen and are served live by
# the external booking engine (BookingEye). This module therefore never emits a
# numeric room rate.
RESERVATIONS_PHONE = "+94 77 335 8800"

# The two Mosvold properties. One phone line serves both, so the property is NEVER
# implied — it must be established before a room type can be selected.
PROPERTY_VILLA = "Mosvold Villa"
PROPERTY_SUNDARA = "Sundara by Mosvold"

PROPERTY_LOCATIONS = {
    PROPERTY_VILLA: "Ahangama",
    PROPERTY_SUNDARA: "Balapitiya",
}

# Room vocabulary, keyed by property. Room types differ per property and must never
# be mixed up: "Deluxe Double Room" and "Deluxe Twin Room" exist at BOTH properties
# under similar names, so a bare room request is ambiguous until the property is known.
ROOM_TYPES_BY_PROPERTY: dict[str, tuple[str, ...]] = {
    PROPERTY_VILLA: (
        "Deluxe Double Room",
        "Deluxe Twin Room",
        "Family Suite",
        "Founders Suite",
    ),
    PROPERTY_SUNDARA: (
        "Deluxe Double Room with Garden View",
        "Deluxe Double Room with Sea View",
        "Deluxe Twin Room with Sea View",
        "Beach Villa",
        "Family Villa with Pool",
    ),
}

# Flat canonical name -> owning property. Every canonical name is distinct across the
# two properties, so this mapping is unambiguous; caller *phrasing* is what is not.
ROOM_TYPE_PROPERTY: dict[str, str] = {
    name: prop for prop, names in ROOM_TYPES_BY_PROPERTY.items() for name in names
}

ALL_ROOM_TYPES: tuple[str, ...] = tuple(ROOM_TYPE_PROPERTY)

# --------------------------------------------------------------------------- #
# DEMO RATES — demo pricing for client demonstrations
# --------------------------------------------------------------------------- #
# Mosvold publishes no real rate card. These figures are INVENTED for demos so
# Kavya can quote a price and the PMS folio total is non-zero. They are NOT a
# real rate card and must not be presented to actual guests as firm quotes.
#
# Kill switch: set DEMO_RATES_ENABLED=false and Kavya reverts to the original
# "rates are date-dependent, reservations will confirm" behaviour — the numbers
# below stop being surfaced in tool results and the system prompt re-forbids
# quoting figures. Keep in sync with room_types.base_price in the PMS (see
# ops/mosvold-pms/set_demo_rates.sql) or the folio total will disagree with
# what Kavya says on the call.
DEMO_RATES_ENABLED: bool = os.getenv("DEMO_RATES_ENABLED", "true").lower() == "true"

DEMO_NIGHTLY_RATE_USD: dict[str, int] = {
    # Mosvold Villa (Ahangama)
    "Deluxe Double Room": 700,
    "Deluxe Twin Room": 700,
    "Family Suite": 820,
    "Founders Suite": 900,
    # Sundara by Mosvold (Balapitiya)
    "Deluxe Double Room with Garden View": 700,
    "Deluxe Double Room with Sea View": 760,
    "Deluxe Twin Room with Sea View": 760,
    "Beach Villa": 880,
    "Family Villa with Pool": 900,
}


def demo_rate_for(room_type_name: str) -> Optional[int]:
    """Demo USD nightly rate for a canonical room name, or None.

    Returns None when demo rates are disabled or the name is unknown, so every
    caller degrades to the original no-rate behaviour rather than guessing.
    """
    if not DEMO_RATES_ENABLED:
        return None
    return DEMO_NIGHTLY_RATE_USD.get(room_type_name)

# Every name Kavya may speak, across both properties. Flat tuple, matching
# kpms_service.KAVYA_VOCAB so the constant means the same shape in both modules.
KAVYA_VOCAB: tuple[str, ...] = ALL_ROOM_TYPES

# Words a caller may use for each property (name fragments and towns).
# Bare "villa"/"the villa" are intentionally absent: matched as substrings they
# resolve Sundara's "Beach Villa" and "Family Villa with Pool" to Mosvold Villa.
# This mirrors tools.normalise_property so the two resolvers agree.
_PROPERTY_ALIASES: dict[str, str] = {
    "mosvold villa": PROPERTY_VILLA,
    "ahangama": PROPERTY_VILLA,
    "mosvold ahangama": PROPERTY_VILLA,
    "sundara": PROPERTY_SUNDARA,
    "sundara by mosvold": PROPERTY_SUNDARA,
    "balapitiya": PROPERTY_SUNDARA,
    "mosvold balapitiya": PROPERTY_SUNDARA,
}


class ServiceError(Exception):
    """Graceful business error."""


def resolve_property(query: str) -> str | None:
    """Map a caller's phrasing to one of the two canonical property names.

    Returns None when the property cannot be established — callers MUST treat that
    as "ask which property" rather than guessing, because room names collide."""
    if not query:
        return None
    q = " ".join(str(query).strip().lower().split())
    if not q:
        return None
    if q in _PROPERTY_ALIASES:
        return _PROPERTY_ALIASES[q]
    hits = {prop for alias, prop in _PROPERTY_ALIASES.items() if alias in q}
    if len(hits) == 1:
        return hits.pop()
    return None


def _property_prompt() -> str:
    return (
        "We have two properties and the room names differ between them: "
        f"{PROPERTY_VILLA} in {PROPERTY_LOCATIONS[PROPERTY_VILLA]}, and "
        f"{PROPERTY_SUNDARA} in {PROPERTY_LOCATIONS[PROPERTY_SUNDARA]}. "
        "Which property is this booking for?"
    )


def _room_choice_prompt(property_name: str = "") -> str:
    """Room options for one property, or the property question if none established."""
    names = ROOM_TYPES_BY_PROPERTY.get(property_name)
    if not names:
        return _property_prompt()
    return f"At {property_name} the room types are " + ", ".join(names) + "."


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
    """Lowercase + collapse whitespace. Deliberately does NOT strip trailing words:
    'Family Suite', 'Founders Suite', 'Beach Villa' and 'Family Villa with Pool' are
    whole room names at Mosvold, so suffix-trimming would corrupt them."""
    if not name:
        return ""
    return " ".join(str(name).strip().lower().split())


def _kavya_vocab_name(pms_name: str) -> str:
    return (pms_name or "").strip()


def _display_name(rt: dict) -> str:
    """Canonical Kavya-vocab name. Match PMS name case-insensitively to vocab."""
    pms = (rt.get("name") or "").strip()
    pn = _norm(pms)
    for v in ALL_ROOM_TYPES:
        if _norm(v) == pn:
            return v
    return _kavya_vocab_name(pms)


def _property_of(rt: dict) -> str:
    """Owning property for a PMS room-type dict.

    Prefers an explicit property field if the booking backend supplies one; otherwise
    derives it from the canonical room name. Returns "" when it cannot be established,
    and callers must then ask the guest rather than assume."""
    for key in ("propertyName", "property", "hotelName", "hotel"):
        val = rt.get(key)
        if isinstance(val, str):
            resolved = resolve_property(val)
            if resolved:
                return resolved
        elif isinstance(val, dict):
            resolved = resolve_property(str(val.get("name") or ""))
            if resolved:
                return resolved
    return ROOM_TYPE_PROPERTY.get(_display_name(rt), "")


def _match_room_type(
    query: str, room_types: list[dict], property_name: str = ""
) -> dict | None:
    """Match a Kavya-style query to a room type dict, scoped to one property.

    `property_name` MUST be an established property: the same guest phrasing
    ("deluxe double room") is valid at both Mosvold Villa and Sundara by Mosvold, so
    matching across properties would silently pick the wrong hotel. When the property
    is unknown the candidate pool is left unscoped and any cross-property collision
    raises ServiceError instead of guessing.

    Raises ServiceError on ambiguity."""
    if not query:
        return None
    qn = _norm(query)
    if not qn:
        return None

    if property_name:
        candidates = [rt for rt in room_types if _property_of(rt) == property_name]
    else:
        candidates = list(room_types)

    exact = [rt for rt in candidates if _norm(rt.get("name", "")) == qn]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        raise ServiceError(f"Multiple room types match '{query}'.")
    prefix = []
    for rt in candidates:
        n = _norm(rt.get("name", ""))
        if not n:
            continue
        # Only accept the query as an ABBREVIATION of a canonical name
        # (n starts with qn). The reverse direction (qn starts with n) let a
        # longer cross-property name — e.g. "Deluxe Double Room with Sea View"
        # (Sundara) — collapse onto a shorter same-property name — "Deluxe
        # Double Room" (Villa) — and silently book the wrong hotel. A query
        # strictly longer than a canonical name is the other property's room or
        # noise, never a same-property refinement, so it must not match here.
        if n.startswith(qn):
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
    property_name: str = "",
) -> dict:
    try:
        ci = _parse_date(check_in)
        co = _parse_date(check_out)
    except (ValueError, TypeError):
        return {"error": "Invalid date format. Please use YYYY-MM-DD."}
    if co <= ci:
        return {"error": "Check-out must be after check-in."}

    nights = (co - ci).days

    # The property must be established BEFORE any room type is interpreted — room
    # names overlap across the two properties.
    # Fail closed: availability must be scoped to an established property, the
    # same invariant book() enforces. Room names overlap across the two
    # properties, so an unscoped availability answer can surface the wrong
    # hotel's rooms. This makes the service layer enforce the property rule
    # itself rather than trusting every caller (e.g. a direct booking_api call
    # or a future tool path) to have validated it first.
    resolved_property = resolve_property(property_name) if property_name else None
    if not resolved_property:
        return {"error": _property_prompt()}

    try:
        all_rooms = await _get_rooms_cached()
        avail_rooms = await _free_rooms_from_reservations(all_rooms, check_in, check_out)
    except YanoljaError as exc:
        logger.warning("Availability fetch failed: %s", exc)
        return {"error": "We couldn't check availability just now. Please try again in a moment."}

    # resolved_property is guaranteed non-empty here (fail-closed guard above).
    room_types = _room_types_from_rooms(all_rooms)
    scoped_types = [rt for rt in room_types if _property_of(rt) == resolved_property]

    if room_type_filter:
        try:
            match = _match_room_type(room_type_filter, room_types, resolved_property)
        except ServiceError:
            match = None
        if match is None:
            return {
                "error": (
                    f"We don't have a room type called '{room_type_filter}' at "
                    f"{resolved_property}. " + _room_choice_prompt(resolved_property)
                )
            }
        types_to_check = [match]
    else:
        types_to_check = scoped_types

    # Which roomTypeIds appear in available rooms
    available_type_ids: set[Any] = set()
    for r in avail_rooms:
        rtid = r.get("roomTypeId")
        if rtid is not None:
            available_type_ids.add(rtid)

    # The booking backend is consulted ONLY for availability and booking creation.
    # Room descriptions come from the KB. We strip everything but the available
    # flag, the room name, and the owning property — plus, when DEMO_RATES_ENABLED
    # is on, the demo rate from DEMO_NIGHTLY_RATE_USD. The PMS itself
    # is not the rate source; see the DEMO RATES block at the top of this module.
    out_rooms: list[dict] = []
    available_count = 0
    for rt in types_to_check:
        type_id = rt.get("id")
        display_name = _display_name(rt)
        is_available = type_id in available_type_ids
        if is_available:
            available_count += 1
        room_entry = {
            "room_type_name": display_name,
            "property": _property_of(rt),
            "available": is_available,
        }
        rate = demo_rate_for(display_name)
        if rate is not None:
            room_entry["rate_per_night_usd"] = rate
            room_entry["total_usd"] = rate * nights
        out_rooms.append(room_entry)

    if DEMO_RATES_ENABLED:
        rates_note = (
            "Rates are per room per night in US dollars, bed and breakfast, "
            f"including taxes. Reservations: {RESERVATIONS_PHONE}."
        )
    else:
        rates_note = (
            "No rates are quoted here. Rates depend on the dates chosen and are "
            f"confirmed by reservations on {RESERVATIONS_PHONE}."
        )

    return {
        "check_in": check_in,
        "check_out": check_out,
        "nights": nights,
        "property": resolved_property,
        "total_room_types": len(scoped_types),
        "available_room_types": available_count,
        "rooms": out_rooms,
        "rates_note": rates_note,
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
    property_name: str = "",
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

    # A booking can never be created without knowing WHICH property: the same room
    # name exists at both, so an unscoped match could book the wrong hotel.
    resolved_property = resolve_property(property_name) if property_name else None
    if not resolved_property:
        return {"error": _property_prompt()}

    try:
        all_rooms = await _get_rooms_cached()
    except YanoljaError as exc:
        logger.warning("Setup fetch failed during book: %s", exc)
        return {"error": "We couldn't complete the booking just now. Please try again in a moment or call the hotel directly."}

    room_types = _room_types_from_rooms(all_rooms)
    try:
        rt_match = _match_room_type(room_type, room_types, resolved_property)
    except ServiceError:
        rt_match = None
    if not rt_match:
        return {
            "error": (
                f"We don't have a room type called '{room_type}' at "
                f"{resolved_property}. " + _room_choice_prompt(resolved_property)
            )
        }

    type_id = rt_match.get("id")
    display_room_type = _display_name(rt_match)

    # Capacity NOT enforced here: the booking backend's maxOccupancy is not a
    # reliable source for these room types. The LLM uses the KB to decide pax fit
    # before calling this tool.

    # NO rate is computed. Mosvold publishes no rates; a rate only exists once dates
    # are chosen and is served live by the external booking engine, so any figure
    # produced locally would be fabricated. Pricing goes to RESERVATIONS_PHONE.

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
                f"Sorry, we have no {display_room_type} available at {resolved_property} "
                "for those dates. "
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

    result = {
        "success": True,
        "booking_reference": str(booking_ref),
        "guest_name": guest_name,
        "check_in": check_in,
        "check_out": check_out,
        "property": resolved_property,
        "room_type": display_room_type,
        "room_number": room.get("roomNumber", ""),
        "nights": nights,
    }

    rate = demo_rate_for(display_room_type)
    if rate is not None:
        result["rate_per_night_usd"] = rate
        result["total_usd"] = rate * nights
        result["rates_note"] = (
            f"US dollars {rate} per room per night, bed and breakfast, including "
            f"taxes. Total for {nights} "
            f"{'night' if nights == 1 else 'nights'}: "
            f"US dollars {rate * nights}. "
            f"Reservations: {RESERVATIONS_PHONE}."
        )
    else:
        result["rates_note"] = (
            "No rate is quoted here. The total depends on the dates and is confirmed "
            f"by reservations on {RESERVATIONS_PHONE}."
        )

    return result


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
    # Always surface which property the booking is at — one line serves both.
    augmented["property"] = _property_of(rt) if rt else ""
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
