"""Business logic on top of yanolja_client, for Hatton Hills.

Kavya serves ONE property: Hatton Hills, a luxury boutique eco retreat in Sri
Lanka's central hill country. It has exactly five room types, all distinct, so
nothing about a room request is ambiguous and the guest is NEVER asked which
property they mean.

SINGLE-PROPERTY MODE (2026-07-30). This module used to serve two Mosvold
properties whose room names collided, so it was deliberately fail-closed: the
property had to be established before a room could be matched, and
`resolve_property` returned None to force an "ask which property" turn. That
protection is now inert by construction — `resolve_property` always resolves to
PROPERTY_HATTON, because with one property there is no wrong hotel to pick. The
`property_name` plumbing is retained end to end (booking_api and tools still
thread it) so a second property can be reintroduced by restoring the alias map
and letting `resolve_property` return None again. Do not delete it.

This module DOES emit room rates while DEMO_RATES_ENABLED is true — see
DEMO_NIGHTLY_RATE_USD below. Those figures are invented for demonstrations.

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

from handover import expand_spoken_repeats

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

# Reservations hotline — for anything the agent cannot answer or quote itself.
RESERVATIONS_PHONE = "+94 77 220 4400"

# The single property. Retained as a keyed structure (rather than flattened) so a
# second property can be reintroduced without reshaping every consumer.
PROPERTY_HATTON = "Hatton Hills"

PROPERTY_LOCATIONS = {
    PROPERTY_HATTON: "Central Province hill country",
}

# Room vocabulary, keyed by property. All five names are distinct and none is a
# prefix of another, so a room request needs no property context to disambiguate.
# These strings are the single source of property identity in the PMS schema (it
# has no property column) and MUST match room_types.name there byte-for-byte —
# see ops/hattonhills-pms/rename_to_hattonhills.sql.
ROOM_TYPES_BY_PROPERTY: dict[str, tuple[str, ...]] = {
    PROPERTY_HATTON: (
        "Forest Escape Suite",
        "Eco Harmony Suite",
        "Sunrise Vista Premium Suite",
        "Mount Luxe Chalet",
        "Mount Monarch Chalet",
    ),
}

# Flat canonical name -> owning property. Trivially unambiguous with one property;
# kept so `_property_of` and `_match_room_type` keep working unchanged.
ROOM_TYPE_PROPERTY: dict[str, str] = {
    name: prop for prop, names in ROOM_TYPES_BY_PROPERTY.items() for name in names
}

ALL_ROOM_TYPES: tuple[str, ...] = tuple(ROOM_TYPE_PROPERTY)

# --------------------------------------------------------------------------- #
# DEMO RATES — demo pricing for client demonstrations
# --------------------------------------------------------------------------- #
# Hatton Hills is an INVENTED demo property, so this whole rate card is invented
# too. It exists so Kavya can quote a confident price on a demo call and the PMS
# folio total is non-zero. It is not a real rate card.
#
# Currency: US dollars, per room per night, half board (breakfast and dinner),
# taxes included. A luxury ladder: 700 entry suite -> 1400 flagship chalet.
#
# Kill switch: set DEMO_RATES_ENABLED=false and Kavya reverts to "rates are
# date-dependent, reservations will confirm" behaviour — the numbers below stop
# being surfaced in tool results and the system prompt re-forbids quoting
# figures. Keep in sync with room_types.base_price in the PMS (see
# ops/hattonhills-pms/rename_to_hattonhills.sql) or the folio total will
# disagree with what Kavya says on the call.
DEMO_RATES_ENABLED: bool = os.getenv("DEMO_RATES_ENABLED", "true").lower() == "true"

DEMO_NIGHTLY_RATE_USD: dict[str, int] = {
    # Hatton Hills — suites sleep 2, chalets sleep 5.
    "Forest Escape Suite": 700,
    "Eco Harmony Suite": 800,
    "Sunrise Vista Premium Suite": 950,
    "Mount Luxe Chalet": 1150,
    "Mount Monarch Chalet": 1400,
}


def demo_rate_for(room_type_name: str) -> Optional[int]:
    """Demo USD nightly rate for a canonical room name, or None.

    Returns None when demo rates are disabled or the name is unknown, so every
    caller degrades to the original no-rate behaviour rather than guessing.
    """
    if not DEMO_RATES_ENABLED:
        return None
    return DEMO_NIGHTLY_RATE_USD.get(room_type_name)

# Every name Kavya may speak. Flat tuple, matching kpms_service.KAVYA_VOCAB so the
# constant means the same shape in both modules.
KAVYA_VOCAB: tuple[str, ...] = ALL_ROOM_TYPES

# Words a caller may use for the property (name fragments and locality).
# Only used to recognise an explicit mention; resolution no longer depends on it
# because `resolve_property` falls back to the single property regardless.
_PROPERTY_ALIASES: dict[str, str] = {
    "hatton hills": PROPERTY_HATTON,
    "hatton": PROPERTY_HATTON,
    "hatton hills resort": PROPERTY_HATTON,
    "hill country": PROPERTY_HATTON,
}


class ServiceError(Exception):
    """Graceful business error."""


def resolve_property(query: str) -> str:
    """Resolve a caller's phrasing to the canonical property name.

    SINGLE-PROPERTY MODE: always returns PROPERTY_HATTON and never None. Hatton
    Hills is the only property, so there is no wrong hotel to route to and
    nothing to fail closed on. The alias map is consulted only so an explicit
    mention still round-trips; anything unrecognised — including "" and None —
    resolves to the one property rather than triggering an "ask which property"
    turn that the guest could not meaningfully answer.

    If a second property is ever added, restore the alias-miss branch to return
    None and re-widen the return type to `str | None`; every caller already
    handles a falsy result."""
    if query:
        q = " ".join(str(query).strip().lower().split())
        if q in _PROPERTY_ALIASES:
            return _PROPERTY_ALIASES[q]
        hits = {prop for alias, prop in _PROPERTY_ALIASES.items() if alias in q}
        if len(hits) == 1:
            return hits.pop()
    return PROPERTY_HATTON


def _property_prompt() -> str:
    """Never asks the guest to choose — there is only one property. Retained
    because `_room_choice_prompt` falls back to it for an unknown property."""
    return (
        f"{PROPERTY_HATTON} has five room types: "
        + ", ".join(ROOM_TYPES_BY_PROPERTY[PROPERTY_HATTON])
        + ". Which one would you like?"
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
    'Suite' and 'Chalet' are part of the whole room name at Hatton Hills (e.g.
    'Forest Escape Suite', 'Mount Monarch Chalet'), so suffix-trimming would
    corrupt them and 'Mount Luxe'/'Mount Monarch' would stop being distinct."""
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
    """Owning property for a PMS room-type dict, or "" if it is not one of ours.

    Derived SOLELY from the canonical room name via ROOM_TYPE_PROPERTY. It must
    NOT fall back to `resolve_property` on a PMS-supplied property field: that
    function now always returns PROPERTY_HATTON (single-property mode), so any
    non-empty string would be laundered into a match and room types that are not
    part of the Hatton Hills catalogue — the 'Default Unmapped Room' fallback and
    the retired ex-Mosvold types still present in the database — would be pulled
    back into availability and could be quoted or booked.

    Returning "" for anything unrecognised is what keeps those rows filtered out
    of `derive_availability`, so this is load-bearing, not defensive."""
    return ROOM_TYPE_PROPERTY.get(_display_name(rt), "")


def _match_room_type(
    query: str, room_types: list[dict], property_name: str = ""
) -> dict | None:
    """Match a Kavya-style query to a room type dict, scoped to one property.

    `property_name` scopes the candidate pool. In single-property mode it is
    always "Hatton Hills" and the scoping is a no-op, but it is kept so a second
    property can be reintroduced without touching this function.

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
        # Accept the query as an ABBREVIATION of a canonical name (n starts with
        # qn), e.g. "mount monarch" -> "Mount Monarch Chalet".
        if n.startswith(qn):
            prefix.append(rt)
    if len(prefix) == 1:
        return prefix[0]
    if len(prefix) > 1:
        raise ServiceError(f"Multiple room types match '{query}'.")

    # Final fallback: the query EXTENDS a canonical name (qn starts with n), e.g.
    # "mount monarch chalet with plunge pool" -> "Mount Monarch Chalet". This
    # direction was previously forbidden because a longer cross-property name
    # ("Deluxe Double Room with Sea View") could collapse onto a shorter
    # same-property one ("Deluxe Double Room") and silently book the wrong hotel.
    # With a single property there is no wrong hotel to reach, and all five names
    # are distinct with none a prefix of another, so the collapse hazard is gone —
    # while guests describing a room more fully than its catalogue name is common.
    # Reinstate the restriction if a second property is ever added.
    extends = [
        rt for rt in candidates
        if (n := _norm(rt.get("name", ""))) and qn.startswith(n)
    ]
    if len(extends) == 1:
        return extends[0]
    if len(extends) > 1:
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

    # Single-property mode: this always resolves to Hatton Hills, including when
    # the caller passed nothing. Note the missing `if property_name else None`
    # guard that used to wrap this call — with one property, an absent property
    # argument is normal (nothing asks the guest for it any more), so short-
    # circuiting to None would have made every availability check fail closed
    # with an unanswerable "which property?" prompt.
    resolved_property = resolve_property(property_name)

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
            "Rates are per room per night in US dollars, half board "
            f"(breakfast and dinner), including taxes. "
            f"Reservations: {RESERVATIONS_PHONE}."
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

    # Single-property mode: always resolves to Hatton Hills, even with no
    # property argument. See the matching note in derive_availability().
    resolved_property = resolve_property(property_name)

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

    # The rate is attached further down from DEMO_NIGHTLY_RATE_USD (see the
    # rates_note / total_usd block), not derived from the PMS. While
    # DEMO_RATES_ENABLED is false no figure is emitted at all and pricing goes to
    # RESERVATIONS_PHONE.

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
        # Expand spoken "double"/"triple" shorthand ("double seven" -> "77")
        # before storing, in case the model passed the words through verbatim.
        guest_payload["phone"] = expand_spoken_repeats(guest_phone).strip()

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
            f"US dollars {rate} per room per night, half board, including "
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
