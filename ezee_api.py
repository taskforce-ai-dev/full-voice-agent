"""
eZee PMS (iPMS247) API integration module for the hotel voice agent.

Provides async functions for:
  - Checking room availability (cached)
  - Creating bookings
  - Retrieving bookings (by reservation number, email, or date range)
  - Cancelling bookings

All HTTP calls share a single module-level aiohttp session with connection pooling.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime
from typing import Any, Optional

import aiohttp

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration from environment
# ---------------------------------------------------------------------------
EZEE_BASE_URL: str = os.getenv(
    "EZEE_BASE_URL",
    "https://live.ipms247.com/booking/reservation_api/listing.php",
)
EZEE_HOTEL_CODE: str = os.getenv("EZEE_HOTEL_CODE", "")
EZEE_AUTH_CODE: str = os.getenv("EZEE_AUTH_CODE", "")
EZEE_API_KEY: str = os.getenv("EZEE_API_KEY", "")

REQUEST_TIMEOUT: int = 10  # seconds

# ---------------------------------------------------------------------------
# Connection-pooled session management
# ---------------------------------------------------------------------------
_session: Optional[aiohttp.ClientSession] = None


async def get_session() -> aiohttp.ClientSession:
    """Return the shared ``aiohttp.ClientSession``, creating it on first use.

    The session is backed by a ``TCPConnector`` with a pool of up to 10
    connections and a 30-second keep-alive timeout so connections are reused
    across rapid successive API calls.
    """
    global _session
    if _session is None or _session.closed:
        connector = aiohttp.TCPConnector(limit=10, keepalive_timeout=30)
        timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
        _session = aiohttp.ClientSession(connector=connector, timeout=timeout)
        logger.info("Created new aiohttp session with pooled connector (limit=10)")
    return _session


async def close_session() -> None:
    """Gracefully close the shared session (call on application shutdown)."""
    global _session
    if _session is not None and not _session.closed:
        await _session.close()
        _session = None
        logger.info("Closed aiohttp session")


# ---------------------------------------------------------------------------
# Availability cache
# ---------------------------------------------------------------------------
_availability_cache: dict[str, tuple[float, dict]] = {}
CACHE_TTL: int = 120  # seconds


def _cache_key(
    check_in: str, check_out: str, num_rooms: int, num_adults: int
) -> str:
    return f"{check_in}:{check_out}:{num_rooms}:{num_adults}"


def _get_cached(key: str) -> Optional[dict]:
    """Return cached result if still fresh, otherwise ``None``."""
    entry = _availability_cache.get(key)
    if entry is None:
        return None
    cached_at, data = entry
    if time.time() - cached_at > CACHE_TTL:
        del _availability_cache[key]
        logger.debug("Cache expired for key %s", key)
        return None
    logger.debug("Cache hit for key %s", key)
    return data


def _set_cache(key: str, data: dict) -> None:
    _availability_cache[key] = (time.time(), data)
    logger.debug("Cached result for key %s", key)


# ---------------------------------------------------------------------------
# Helper utilities
# ---------------------------------------------------------------------------

def is_configured() -> bool:
    """Return ``True`` when all required eZee credentials are present."""
    return bool(EZEE_HOTEL_CODE and EZEE_AUTH_CODE and EZEE_API_KEY)


def _auth_block() -> dict[str, str]:
    """Return the standard authentication dict for POST payloads."""
    return {
        "HotelCode": EZEE_HOTEL_CODE,
        "AuthCode": EZEE_AUTH_CODE,
    }


def _night_count(check_in: str, check_out: str) -> int:
    """Calculate the number of nights between two ``YYYY-MM-DD`` date strings."""
    fmt = "%Y-%m-%d"
    d_in = datetime.strptime(check_in, fmt)
    d_out = datetime.strptime(check_out, fmt)
    delta = (d_out - d_in).days
    return max(delta, 0)


# ---------------------------------------------------------------------------
# Response parsers
# ---------------------------------------------------------------------------

def _parse_availability(data: Any) -> dict[str, Any]:
    """Parse the raw eZee RoomAvailability response into a friendly dict.

    Returns a dict with either an ``"error"`` key on failure, or
    ``"rooms"`` containing a list of available room-type dicts.
    """
    # eZee may return error structures in several shapes
    if isinstance(data, dict):
        # Direct error object: {"Errors": {"ErrorCode": ..., "ErrorMessage": ...}}
        errors = data.get("Errors") or data.get("errors")
        if errors:
            code = errors.get("ErrorCode", "")
            msg = errors.get("ErrorMessage", "Unknown error")
            logger.warning("Availability error %s: %s", code, msg)
            return {"error": msg, "error_code": str(code)}

    # Successful response is typically a list (or dict keyed by room type)
    rooms: list[dict[str, Any]] = []
    try:
        # Normalise to iterable of room entries
        entries: Any = data
        if isinstance(data, dict):
            # Some responses nest rooms under a key
            entries = data.get("RoomTypes") or data.get("roomtypes") or data.values()

        if isinstance(entries, dict):
            entries = entries.values()

        for entry in entries:
            if not isinstance(entry, dict):
                continue
            room: dict[str, Any] = {
                "room_type_id": str(entry.get("RoomTypeID", entry.get("roomtypeid", ""))),
                "room_type_name": entry.get("RoomTypeName", entry.get("roomtypename", "")),
                "available": int(entry.get("Availability", entry.get("availability", 0))),
                "rate": entry.get("RatePerNight", entry.get("ratepernight", entry.get("Rate", ""))),
                "total": entry.get("TotalRate", entry.get("totalrate", "")),
            }
            rooms.append(room)
    except Exception:
        logger.exception("Failed to parse availability payload")
        return {"error": "Unable to parse availability response"}

    if not rooms:
        return {"error": "No rooms returned in availability response"}

    return {"rooms": rooms}


def _parse_booking(data: Any) -> dict[str, Any]:
    """Parse the raw eZee InsertBooking response.

    Returns a dict with ``"reservation_no"`` on success, or ``"error"`` on
    failure.
    """
    if isinstance(data, dict):
        errors = data.get("Errors") or data.get("errors")
        if errors:
            code = errors.get("ErrorCode", "")
            msg = errors.get("ErrorMessage", "Unknown error")
            logger.warning("Booking error %s: %s", code, msg)
            return {"error": msg, "error_code": str(code)}

        res_no = (
            data.get("ReservationNo")
            or data.get("reservationno")
            or data.get("Reservation_no")
        )
        if res_no:
            return {"reservation_no": str(res_no), "status": "confirmed"}

    # Fallback: try to find reservation number in a stringified response
    text = str(data)
    if "ReservationNo" in text or "reservationno" in text:
        logger.info("Booking response (raw): %s", text[:500])

    return {"error": "Unable to parse booking confirmation", "raw": text[:500]}


def _parse_retrieve_booking(data: Any) -> dict[str, Any]:
    """Parse a ReadBooking or BookingList response into a friendly dict.

    Extracts: reservation_no, guest_name, check_in, check_out, room_type,
    status, and total_amount.
    """
    # Handle known error structures
    if isinstance(data, dict):
        errors = data.get("Errors") or data.get("errors")
        if errors:
            code = str(errors.get("ErrorCode", ""))
            msg = errors.get("ErrorMessage", "Unknown error")
            logger.warning("Retrieve booking error %s: %s", code, msg)
            return {"error": msg, "error_code": code}

        # Error code at top level (e.g. {"ErrorCode": -1, "ErrorMessage": ...})
        if "ErrorCode" in data or "errorcode" in data:
            code = str(data.get("ErrorCode", data.get("errorcode", "")))
            msg = data.get("ErrorMessage", data.get("errormessage", "Unknown error"))
            return {"error": msg, "error_code": code}

    if isinstance(data, str):
        lowered = data.strip().lower()
        if lowered in ("reservationnotexist", "noresacc", "unauthreq"):
            friendly = {
                "reservationnotexist": "Reservation does not exist",
                "noresacc": "No reservation found matching the criteria",
                "unauthreq": "Unauthorized request — check API credentials",
            }
            return {"error": friendly.get(lowered, data), "error_code": data.strip()}
        if data.strip() == "-1":
            return {"error": "No data found", "error_code": "-1"}

    # Successful single-booking response (ReadBooking)
    def _extract_booking(entry: dict) -> dict[str, Any]:
        return {
            "reservation_no": str(
                entry.get("ReservationNo")
                or entry.get("reservationno")
                or entry.get("ResNo")
                or ""
            ),
            "guest_name": (
                entry.get("GuestName")
                or entry.get("guestname")
                or entry.get("Salutation", "")
                + " "
                + entry.get("FirstName", "")
                + " "
                + entry.get("LastName", "")
            ).strip(),
            "check_in": entry.get("Checkin", entry.get("checkin", "")),
            "check_out": entry.get("Checkout", entry.get("checkout", "")),
            "room_type": entry.get("RoomTypeName", entry.get("roomtypename", "")),
            "status": entry.get("Status", entry.get("status", "")),
            "total_amount": entry.get(
                "TotalAmount",
                entry.get("totalamount", entry.get("GrandTotal", "")),
            ),
        }

    if isinstance(data, dict):
        # Could be a single booking or keyed by reservation number
        if "ReservationNo" in data or "reservationno" in data or "ResNo" in data:
            return _extract_booking(data)

        # Might be nested under a key
        bookings_raw = (
            data.get("Reservations")
            or data.get("reservations")
            or data.get("Bookings")
            or data.get("bookings")
        )
        if bookings_raw:
            if isinstance(bookings_raw, list):
                bookings = [_extract_booking(b) for b in bookings_raw if isinstance(b, dict)]
            elif isinstance(bookings_raw, dict):
                bookings = [_extract_booking(b) for b in bookings_raw.values() if isinstance(b, dict)]
            else:
                bookings = []
            if bookings:
                return {"bookings": bookings} if len(bookings) > 1 else bookings[0]

        # Try treating all dict values as booking entries
        possible = [v for v in data.values() if isinstance(v, dict)]
        if possible:
            bookings = [_extract_booking(b) for b in possible]
            if bookings:
                return {"bookings": bookings} if len(bookings) > 1 else bookings[0]

    if isinstance(data, list):
        bookings = [_extract_booking(b) for b in data if isinstance(b, dict)]
        if bookings:
            return {"bookings": bookings} if len(bookings) > 1 else bookings[0]

    return {"error": "Unable to parse booking retrieval response", "raw": str(data)[:500]}


def _parse_cancel_response(data: Any) -> dict[str, Any]:
    """Parse a CancelBooking response.

    Success: ``{"status": "Successful"}`` (or similar).
    """
    if isinstance(data, dict):
        # Check for errors first
        errors = data.get("Errors") or data.get("errors")
        if errors:
            code = str(errors.get("ErrorCode", ""))
            msg = errors.get("ErrorMessage", "Unknown error")
            logger.warning("Cancel booking error %s: %s", code, msg)
            return {"error": msg, "error_code": code}

        status = data.get("status") or data.get("Status") or ""
        if status.lower() in ("successful", "success"):
            return {"status": "Successful"}

        if "ErrorCode" in data or "errorcode" in data:
            code = str(data.get("ErrorCode", data.get("errorcode", "")))
            msg = data.get("ErrorMessage", data.get("errormessage", "Unknown error"))
            return {"error": msg, "error_code": code}

    if isinstance(data, str):
        lowered = data.strip().lower()
        if lowered in ("successful", "success"):
            return {"status": "Successful"}
        if lowered in ("reservationnotexist", "noresacc", "unauthreq"):
            friendly = {
                "reservationnotexist": "Reservation does not exist",
                "noresacc": "No reservation found matching the criteria",
                "unauthreq": "Unauthorized request — check API credentials",
            }
            return {"error": friendly.get(lowered, data), "error_code": data.strip()}

    return {"error": "Unexpected cancellation response", "raw": str(data)[:500]}


# ---------------------------------------------------------------------------
# Public API functions
# ---------------------------------------------------------------------------

async def check_availability(
    check_in: str,
    check_out: str,
    num_rooms: int = 1,
    num_adults: int = 1,
    room_type_id: Optional[str] = None,
) -> dict[str, Any]:
    """Check room availability for the given dates.

    Uses the eZee Reservation API (HTTP GET, APIKey auth, request_type=RoomList).
    Results are cached for ``CACHE_TTL`` seconds to reduce duplicate calls
    during a single voice conversation turn.
    """
    if not is_configured():
        return {"error": "eZee PMS credentials are not configured"}

    # --- cache lookup ---
    key = _cache_key(check_in, check_out, num_rooms, num_adults)
    cached = _get_cached(key)
    if cached is not None:
        return cached

    nights = _night_count(check_in, check_out)
    if nights <= 0:
        return {"error": "check_out must be after check_in"}

    # eZee Reservation API uses HTTP GET with query parameters
    params: dict[str, str] = {
        "request_type": "RoomList",
        "HotelCode": EZEE_HOTEL_CODE,
        "APIKey": EZEE_API_KEY,
        "language": "en",
        "check_in_date": check_in,
        "check_out_date": check_out,
        "num_rooms": str(num_rooms),
        "number_adults": str(num_adults),
    }
    if room_type_id:
        params["roomtypeunkid"] = room_type_id

    logger.info(
        "Checking availability %s -> %s (%d nights, %d rooms, %d adults)",
        check_in, check_out, nights, num_rooms, num_adults,
    )

    try:
        session = await get_session()
        async with session.get(EZEE_BASE_URL, params=params) as resp:
            resp.raise_for_status()
            data = await resp.json(content_type=None)
            logger.info("Availability raw response: %s", str(data)[:500])
    except aiohttp.ClientError as exc:
        logger.exception("HTTP error during availability check")
        return {"error": f"API request failed: {exc}"}
    except Exception as exc:
        logger.exception("Unexpected error during availability check")
        return {"error": f"Unexpected error: {exc}"}

    result = _parse_availability(data)

    # Only cache successful results
    if "error" not in result:
        _set_cache(key, result)

    return result


async def create_booking(
    check_in: str,
    check_out: str,
    guest_name: str,
    guest_email: str = "",
    guest_phone: str = "",
    room_type_id: str = "",
    num_rooms: int = 1,
    num_adults: int = 1,
    special_requests: str = "",
) -> dict[str, Any]:
    """Create a new reservation via the eZee API."""
    if not is_configured():
        return {"error": "eZee PMS credentials are not configured"}

    nights = _night_count(check_in, check_out)
    if nights <= 0:
        return {"error": "check_out must be after check_in"}

    # Split guest name heuristically
    parts = guest_name.strip().split(None, 1)
    first_name = parts[0] if parts else guest_name
    last_name = parts[1] if len(parts) > 1 else ""

    payload: dict[str, Any] = {
        "RES_Request": {
            "Request_Type": "InsertBooking",
            **_auth_block(),
            "Salutation": "Mr./Ms.",
            "FirstName": first_name,
            "LastName": last_name,
            "Gender": "",
            "Email": guest_email,
            "Phone": guest_phone,
            "CheckIn": check_in,
            "CheckOut": check_out,
            "NoOfRooms": str(num_rooms),
            "NoOfAdults": str(num_adults),
            "NoOfNights": str(nights),
            "SpecialRequests": special_requests,
        }
    }
    if room_type_id:
        payload["RES_Request"]["RoomTypeID"] = room_type_id

    logger.info(
        "Creating booking for %s, %s -> %s (%d nights)",
        guest_name, check_in, check_out, nights,
    )

    try:
        session = await get_session()
        async with session.post(EZEE_BASE_URL, json=payload) as resp:
            resp.raise_for_status()
            data = await resp.json(content_type=None)
            logger.debug("Booking raw response: %s", str(data)[:300])
    except aiohttp.ClientError as exc:
        logger.exception("HTTP error during booking creation")
        return {"error": f"API request failed: {exc}"}
    except Exception as exc:
        logger.exception("Unexpected error during booking creation")
        return {"error": f"Unexpected error: {exc}"}

    return _parse_booking(data)


async def retrieve_booking(
    reservation_no: Optional[str] = None,
    guest_email: Optional[str] = None,
    arrival_from: Optional[str] = None,
    arrival_to: Optional[str] = None,
) -> dict[str, Any]:
    """Retrieve an existing booking from eZee.

    Lookup priority:
      1. ``reservation_no`` -- exact reservation lookup via ReadBooking.
      2. ``guest_email`` and/or ``arrival_from``/``arrival_to`` -- list search
         via BookingList.

    At least one parameter must be provided.
    """
    if not is_configured():
        return {"error": "eZee PMS credentials are not configured"}

    if not any([reservation_no, guest_email, arrival_from, arrival_to]):
        return {"error": "At least one search parameter is required (reservation_no, guest_email, or date range)"}

    params: dict[str, str] = {
        "HotelCode": EZEE_HOTEL_CODE,
        "APIKey": EZEE_API_KEY,
        "language": "en",
    }

    if reservation_no:
        # Exact lookup by reservation number
        params["request_type"] = "ReadBooking"
        params["ResNo"] = reservation_no
        logger.info("Retrieving booking by reservation_no=%s", reservation_no)
    else:
        # Search by email and/or date range
        params["request_type"] = "BookingList"
        if guest_email:
            params["EmailId"] = guest_email
        if arrival_from:
            params["arrival_from"] = arrival_from
        if arrival_to:
            params["arrival_to"] = arrival_to
        logger.info(
            "Retrieving bookings (email=%s, from=%s, to=%s)",
            guest_email, arrival_from, arrival_to,
        )

    try:
        session = await get_session()
        async with session.get(EZEE_BASE_URL, params=params) as resp:
            resp.raise_for_status()
            data = await resp.json(content_type=None)
            logger.debug("Retrieve booking raw response: %s", str(data)[:300])
    except aiohttp.ClientError as exc:
        logger.exception("HTTP error during booking retrieval")
        return {"error": f"API request failed: {exc}"}
    except Exception as exc:
        logger.exception("Unexpected error during booking retrieval")
        return {"error": f"Unexpected error: {exc}"}

    return _parse_retrieve_booking(data)


async def cancel_booking(reservation_no: str) -> dict[str, Any]:
    """Cancel an existing reservation by its reservation number.

    Returns ``{"status": "Successful"}`` on success, or a dict with an
    ``"error"`` key describing the failure.
    """
    if not is_configured():
        return {"error": "eZee PMS credentials are not configured"}

    if not reservation_no:
        return {"error": "reservation_no is required"}

    params: dict[str, str] = {
        "request_type": "CancelBooking",
        "HotelCode": EZEE_HOTEL_CODE,
        "APIKey": EZEE_API_KEY,
        "language": "en",
        "ResNo": reservation_no,
    }

    logger.info("Cancelling booking reservation_no=%s", reservation_no)

    try:
        session = await get_session()
        async with session.get(EZEE_BASE_URL, params=params) as resp:
            resp.raise_for_status()
            data = await resp.json(content_type=None)
            logger.debug("Cancel booking raw response: %s", str(data)[:300])
    except aiohttp.ClientError as exc:
        logger.exception("HTTP error during booking cancellation")
        return {"error": f"API request failed: {exc}"}
    except Exception as exc:
        logger.exception("Unexpected error during booking cancellation")
        return {"error": f"Unexpected error: {exc}"}

    return _parse_cancel_response(data)
