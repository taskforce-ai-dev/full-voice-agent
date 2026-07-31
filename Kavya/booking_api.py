"""booking_api.py -- thin shim mapping legacy Kavya tool contract to yanolja_service.

This file replaces the n8n-webhook polling implementation (moved to
legacy_pms/booking_api.py on 2026-05-13) with a direct call into the new
Yanolja PMS at https://yanolja.taskforceai.tech/api.

The public surface (function names, signatures, and return-dict shapes)
is preserved so tools.py and server.py need no changes. Extra fields
introduced by the new PMS (rate_per_night_usd, total_usd, room_number)
are additive -- old consumers that ignore unknown keys keep working.

Hatton Hills is a SINGLE property with five distinct room types, so
check_availability() and create_booking() still take a `property_name` and
forward it to yanolja_service, but it no longer needs to be supplied: an empty
value resolves to "Hatton Hills". The parameter is retained so a second property
can be reintroduced without changing this layer's public surface.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

import aiohttp

import yanolja_client
import yanolja_service

logger = logging.getLogger(__name__)

# Generic aiohttp session for non-PMS HTTP (e.g. post_call.py posting to n8n).
# Kept separate from the Yanolja-authenticated session so we never leak the PMS
# API key to other hosts.
_generic_session: Optional[aiohttp.ClientSession] = None


async def get_session() -> aiohttp.ClientSession:
    """Shared aiohttp session for non-PMS calls (post_call → n8n webhook).

    Back-compat shim: post_call.py imports this from booking_api.
    """
    global _generic_session
    if _generic_session is None or _generic_session.closed:
        connector = aiohttp.TCPConnector(limit=10, keepalive_timeout=30)
        timeout = aiohttp.ClientTimeout(total=30)
        _generic_session = aiohttp.ClientSession(connector=connector, timeout=timeout)
    return _generic_session

_UNAVAILABLE = {
    "error": "Our reservation system is currently unavailable. Please call the hotel directly."
}
_AVAIL_FAIL = {
    "error": "Sorry, we couldn't check availability just now. Please try again in a moment."
}
_BOOK_FAIL = {
    "error": "Sorry, we couldn't complete the booking just now. Please try again in a moment or call the hotel directly."
}
_LOOKUP_FAIL = {
    "error": "Sorry, we couldn't find that booking just now. Please try again or call the hotel."
}
_CANCEL_FAIL = {
    "error": "Sorry, we couldn't cancel that booking just now. Please call the hotel to confirm cancellation."
}


def is_configured() -> bool:
    """True when the new PMS API key is set."""
    return yanolja_client.is_configured()


async def close_session() -> None:
    """Close both the Yanolja session and the generic n8n-posting session."""
    global _generic_session
    await yanolja_client.close_session()
    if _generic_session is not None and not _generic_session.closed:
        await _generic_session.close()
        _generic_session = None


async def check_availability(
    check_in: str,
    check_out: str,
    room_type: str = "",
    room_name: str = "",          # accepted for signature compat; ignored
    num_adults: int = 1,
    num_children: int = 0,
    rate_type: str = "BB",        # accepted for signature compat; ignored
    salutation: str = "Mr",
    guest_name: str = "",
    guest_phone: str = "",
    guest_email: str = "",
    property_name: str = "",   # "Hatton Hills" (single property; may be empty)
) -> dict[str, Any]:
    if not is_configured():
        return dict(_UNAVAILABLE)
    try:
        return await yanolja_service.derive_availability(
            check_in=check_in,
            check_out=check_out,
            num_adults=num_adults,
            num_children=num_children,
            room_type_filter=room_type or "",
            property_name=property_name or "",
        )
    except yanolja_client.YanoljaError as exc:
        logger.warning("Yanolja availability check failed: %s", exc)
        return dict(_AVAIL_FAIL)
    except Exception:
        logger.exception("Unexpected error in check_availability")
        return dict(_AVAIL_FAIL)


async def create_booking(
    check_in: str,
    check_out: str,
    room_type: str,
    guest_name: str,
    salutation: str = "Mr",
    guest_email: str = "",
    guest_phone: str = "",
    num_adults: int = 1,
    num_children: int = 0,
    rate_type: str = "BB",
    room_name: str = "",
    property_name: str = "",   # "Hatton Hills" (single property; may be empty)
) -> dict[str, Any]:
    if not is_configured():
        return dict(_UNAVAILABLE)
    try:
        return await yanolja_service.book(
            check_in=check_in,
            check_out=check_out,
            room_type=room_type,
            guest_name=guest_name,
            guest_email=guest_email,
            guest_phone=guest_phone,
            salutation=salutation,
            num_adults=num_adults,
            num_children=num_children,
            property_name=property_name or "",
        )
    except yanolja_client.YanoljaError as exc:
        logger.warning("Yanolja booking failed: %s", exc)
        return dict(_BOOK_FAIL)
    except Exception:
        logger.exception("Unexpected error in create_booking")
        return dict(_BOOK_FAIL)


async def retrieve_booking(
    reservation_no: Optional[str] = None,
    guest_email: Optional[str] = None,
    arrival_from: Optional[str] = None,
    arrival_to: Optional[str] = None,
) -> dict[str, Any]:
    if not is_configured():
        return dict(_UNAVAILABLE)
    try:
        return await yanolja_service.lookup(
            reservation_no=reservation_no or "",
            guest_email=guest_email or "",
            arrival_from=arrival_from or "",
            arrival_to=arrival_to or "",
        )
    except yanolja_client.YanoljaError as exc:
        logger.warning("Yanolja retrieve failed: %s", exc)
        return dict(_LOOKUP_FAIL)
    except Exception:
        logger.exception("Unexpected error in retrieve_booking")
        return dict(_LOOKUP_FAIL)


async def cancel_booking(reservation_no: str) -> dict[str, Any]:
    if not is_configured():
        return dict(_UNAVAILABLE)
    if not reservation_no:
        return {"error": "We need a reservation number to cancel the booking."}
    try:
        return await yanolja_service.cancel(reservation_no)
    except yanolja_client.YanoljaError as exc:
        logger.warning("Yanolja cancel failed: %s", exc)
        return dict(_CANCEL_FAIL)
    except Exception:
        logger.exception("Unexpected error in cancel_booking")
        return dict(_CANCEL_FAIL)
