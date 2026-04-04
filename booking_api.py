"""
booking_api.py -- n8n webhook-based booking integration.

Replaces direct eZee PMS API calls with an async queue-based system
routed through n8n workflows. A browser extension ("IPMS247 Extractor")
scrapes data from the eZee web UI and posts results back to n8n.

Flow (check_availability):
  1. Kavya POSTs to n8n /webhook/make-availability-request
  2. n8n queues request in DataTable (checked=false)
  3. Browser extension polls /webhook/pending-requests, picks up work
  4. Extension scrapes eZee web UI, POSTs result to /webhook/availability-response
  5. n8n updates DataTable row (checked=true, response=data)
  6. Kavya polls /webhook/eezy-check-results until checked=true

Currently implemented:
  - check_availability  (full n8n async polling)
  - create_booking      (n8n endpoint exists but incomplete -- returns graceful message)
  - retrieve_booking    (not yet in n8n -- returns graceful message)
  - cancel_booking      (not yet in n8n -- returns graceful message)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timedelta
from typing import Any, Optional

import aiohttp

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration from environment
# ---------------------------------------------------------------------------
N8N_BASE_URL: str = os.getenv(
    "N8N_BASE_URL", "https://automation.taskforceai.tech"
)

# Polling behaviour -- tune after real-world latency testing
N8N_POLL_INTERVAL: float = float(os.getenv("N8N_POLL_INTERVAL", "2"))   # seconds between polls
N8N_POLL_TIMEOUT: float = float(os.getenv("N8N_POLL_TIMEOUT", "60"))    # max seconds to wait

# ---------------------------------------------------------------------------
# n8n webhook paths
# ---------------------------------------------------------------------------
WEBHOOK_AVAILABILITY = "/webhook/make-availability-request"
WEBHOOK_BOOKING = "/webhook/make-booking"
WEBHOOK_CHECK_RESULTS = "/webhook/eezy-check-results"

# ---------------------------------------------------------------------------
# Room type ID -> display name mapping
# TODO: Update when the hotel confirms the eZee room type ID mapping.
#       The IDs below come from the n8n availability response data.
# ---------------------------------------------------------------------------
ROOM_TYPE_NAMES: dict[str, str] = {
    "3020000000000000003": "Mount Monarch",
    "3020000000000000004": "Mount Luxe",
    "3020000000000000005": "Sunrise Vista",
    "3020000000000000006": "Eco Harmony",
    "3020000000000000007": "Forest Escape Suite",
}

# ---------------------------------------------------------------------------
# Connection-pooled session management
# ---------------------------------------------------------------------------
_session: Optional[aiohttp.ClientSession] = None


async def get_session() -> aiohttp.ClientSession:
    """Return the shared aiohttp session, creating on first use."""
    global _session
    if _session is None or _session.closed:
        connector = aiohttp.TCPConnector(limit=10, keepalive_timeout=30)
        timeout = aiohttp.ClientTimeout(total=30)
        _session = aiohttp.ClientSession(connector=connector, timeout=timeout)
        logger.info("Created aiohttp session for n8n webhooks")
    return _session


async def close_session() -> None:
    """Gracefully close the shared session (call on app shutdown)."""
    global _session
    if _session is not None and not _session.closed:
        await _session.close()
        _session = None
        logger.info("Closed aiohttp session")


# ---------------------------------------------------------------------------
# Configuration check
# ---------------------------------------------------------------------------
def is_configured() -> bool:
    """Return True when the n8n base URL is set."""
    return bool(N8N_BASE_URL)


# ---------------------------------------------------------------------------
# n8n submit + poll helper
# ---------------------------------------------------------------------------
async def _submit_and_poll(
    submit_url: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Submit a request to n8n and poll until the result is ready.

    1. POST payload to submit_url -> get back row with ID
    2. Poll /eezy-check-results with that ID until checked=true
    3. Return parsed response data, or error dict on timeout/failure
    """
    session = await get_session()

    # -- Step 1: Submit request to n8n --
    try:
        async with session.post(submit_url, json=payload) as resp:
            resp.raise_for_status()
            submit_data = await resp.json(content_type=None)
            logger.info("n8n submit response: %s", str(submit_data)[:500])
    except aiohttp.ClientError as exc:
        logger.exception("HTTP error submitting to n8n")
        return {"error": f"Failed to submit request: {exc}"}
    except Exception as exc:
        logger.exception("Unexpected error submitting to n8n")
        return {"error": f"Failed to submit request: {exc}"}

    # -- Extract the row ID from the insert response --
    # n8n DataTable insert returns the created row (dict or list of dicts)
    request_id = None
    if isinstance(submit_data, dict):
        request_id = submit_data.get("id")
    elif isinstance(submit_data, list) and submit_data:
        first = submit_data[0]
        if isinstance(first, dict):
            request_id = first.get("id")

    if request_id is None:
        logger.error("No row ID in n8n submit response: %s", submit_data)
        return {"error": "Failed to get request ID from n8n"}

    logger.info("Request queued with ID: %s", request_id)

    # -- Step 2: Poll for result --
    poll_url = f"{N8N_BASE_URL}{WEBHOOK_CHECK_RESULTS}"
    start_time = time.time()

    while (time.time() - start_time) < N8N_POLL_TIMEOUT:
        await asyncio.sleep(N8N_POLL_INTERVAL)

        try:
            async with session.post(poll_url, json={"id": request_id}) as resp:
                resp.raise_for_status()
                poll_data = await resp.json(content_type=None)
        except Exception as exc:
            logger.warning("Poll request failed (will retry): %s", exc)
            continue

        # The response is the DataTable row (dict or list wrapping a dict)
        row = None
        if isinstance(poll_data, dict):
            row = poll_data
        elif isinstance(poll_data, list) and poll_data:
            first = poll_data[0]
            if isinstance(first, dict):
                row = first

        if row is None:
            logger.debug("Empty poll response, retrying...")
            continue

        # Check if result is ready -- look for populated response field
        # (n8n may not always set checked=true, but response gets populated)
        response_field = row.get("response")
        checked = row.get("checked", False)
        has_response = response_field is not None and response_field != "" and response_field != "null"

        if checked or has_response:
            logger.info(
                "Result ready for request ID %s (%.1fs elapsed, checked=%s)",
                request_id,
                time.time() - start_time,
                checked,
            )

            if not has_response:
                return {"error": "Result marked as checked but no response data"}

            # Parse JSON string -> dict
            if isinstance(response_field, str):
                try:
                    return json.loads(response_field)
                except json.JSONDecodeError:
                    logger.error("Failed to parse response JSON: %s", response_field[:300])
                    return {"error": "Failed to parse response data"}
            elif isinstance(response_field, dict):
                return response_field  # already parsed
            else:
                return {"error": "Unexpected response format"}

        elapsed = time.time() - start_time
        logger.debug(
            "Waiting for result... (%.1fs / %.1fs)",
            elapsed,
            N8N_POLL_TIMEOUT,
        )

    # -- Timeout --
    elapsed = time.time() - start_time
    logger.error(
        "Polling timed out after %.1fs for request ID %s",
        elapsed,
        request_id,
    )
    return {
        "error": (
            f"Request timed out after {int(N8N_POLL_TIMEOUT)} seconds. "
            "The system is still processing your request. "
            "Please try again in a moment."
        )
    }


# ---------------------------------------------------------------------------
# Availability response parser
# ---------------------------------------------------------------------------
def _parse_n8n_availability(
    data: dict[str, Any],
    check_in: str,
    check_out: str,
) -> dict[str, Any]:
    """Parse n8n availability data into a Claude-friendly format.

    The raw data contains per-date availability (0 or 1) for each room type ID.
    A room is considered "available" for a stay only if it shows available (1)
    on every night from check_in through check_out - 1.

    Response structure returned to Claude:
    {
        "check_in": "2026-04-01",
        "check_out": "2026-04-03",
        "nights": 2,
        "rooms": [
            {
                "room_type_id": "302000...",
                "room_type_name": "...",
                "available": true/false,
                "nights": 2,
                "unavailable_dates": [...]   # only if not available
            }
        ]
    }
    """
    available_rooms = data.get("AvailableRooms", {})
    occupancy = data.get("Occupancy", {})

    if not available_rooms:
        return {"error": "No availability data returned"}

    # Build the list of dates the guest needs (each night of the stay)
    fmt = "%Y-%m-%d"
    try:
        d_in = datetime.strptime(check_in, fmt)
        d_out = datetime.strptime(check_out, fmt)
    except ValueError as exc:
        return {"error": f"Invalid date format: {exc}"}

    nights = (d_out - d_in).days
    if nights <= 0:
        return {"error": "check_out must be after check_in"}

    dates_needed = [
        (d_in + timedelta(days=i)).strftime(fmt)
        for i in range(nights)
    ]

    rooms: list[dict[str, Any]] = []

    for room_type_id, date_avail in available_rooms.items():
        # Check every requested date
        unavailable_dates = []
        for d in dates_needed:
            avail = date_avail.get(d)
            if avail is None or int(avail) == 0:
                unavailable_dates.append(d)

        all_available = len(unavailable_dates) == 0
        room_name = ROOM_TYPE_NAMES.get(room_type_id, f"Room {room_type_id[-3:]}")

        room_info: dict[str, Any] = {
            "room_type_id": room_type_id,
            "room_type_name": room_name,
            "available": all_available,
            "nights": nights,
        }

        if not all_available:
            room_info["unavailable_dates"] = unavailable_dates

        # Occupancy details (if present)
        occ = occupancy.get(room_type_id, {})
        if occ and dates_needed[0] in occ:
            first_occ = occ[dates_needed[0]]
            if isinstance(first_occ, dict):
                room_info["total_inventory"] = first_occ.get("TotalAvailableRooms", "unknown")

        rooms.append(room_info)

    available_count = sum(1 for r in rooms if r["available"])

    return {
        "check_in": check_in,
        "check_out": check_out,
        "nights": nights,
        "total_room_types": len(rooms),
        "available_room_types": available_count,
        "rooms": rooms,
    }


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
    """Check room availability via n8n async polling.

    Converts check_in/check_out to the n8n format (start_date + days),
    submits the request, polls until the browser extension processes it,
    then parses and returns availability for the requested date range.
    """
    if not is_configured():
        return {"error": "n8n webhook URL is not configured"}

    # Validate and calculate days
    fmt = "%Y-%m-%d"
    try:
        d_in = datetime.strptime(check_in, fmt)
        d_out = datetime.strptime(check_out, fmt)
    except ValueError as exc:
        return {"error": f"Invalid date format: {exc}. Use YYYY-MM-DD."}

    nights = (d_out - d_in).days
    if nights <= 0:
        return {"error": "check_out must be after check_in"}

    # Build n8n payload
    payload = {
        "start_date": check_in,
        "days": nights,
        "room_type": room_type_id or "",
    }

    submit_url = f"{N8N_BASE_URL}{WEBHOOK_AVAILABILITY}"
    logger.info(
        "Checking availability %s -> %s (%d nights) via n8n",
        check_in, check_out, nights,
    )

    # Submit and poll
    raw_data = await _submit_and_poll(submit_url, payload)

    # If polling returned an error, pass it through
    if "error" in raw_data:
        return raw_data

    # Parse the availability response for the requested date range
    return _parse_n8n_availability(raw_data, check_in, check_out)


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
    """Create a booking -- NOT YET FULLY AVAILABLE.

    The n8n webhook endpoint exists (/webhook/make-booking) but the
    browser extension handler for processing bookings is still under
    development. Returns a helpful message directing the guest to
    contact the hotel directly.
    """
    return {
        "error": (
            "Online booking creation is being set up and will be available soon. "
            "To complete this reservation, please contact the hotel directly "
            "at +94 71 707 3529 or email info@treehousechalets.com. "
            f"Booking details: {guest_name}, {check_in} to {check_out}, "
            f"{num_adults} adult(s), {num_rooms} room(s)."
        )
    }


async def retrieve_booking(
    reservation_no: Optional[str] = None,
    guest_email: Optional[str] = None,
    arrival_from: Optional[str] = None,
    arrival_to: Optional[str] = None,
) -> dict[str, Any]:
    """Retrieve a booking -- NOT YET AVAILABLE via n8n."""
    return {
        "error": (
            "Booking retrieval is not yet available through our system. "
            "Please contact the hotel directly at +94 71 707 3529 or "
            "email info@treehousechalets.com for booking inquiries."
        )
    }


async def cancel_booking(reservation_no: str) -> dict[str, Any]:
    """Cancel a booking -- NOT YET AVAILABLE via n8n."""
    return {
        "error": (
            "Booking cancellation is not yet available through our system. "
            "Please contact the hotel directly at +94 71 707 3529 or "
            "email info@treehousechalets.com to cancel a reservation."
        )
    }
