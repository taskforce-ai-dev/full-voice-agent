"""HTTP client for the new PMS at booking-pms.taskforceai.tech.

Thin async wrapper over the API. NO business logic — all derivation,
matching, and validation lives in kpms_service.py. This module only
owns the aiohttp session, request building, X-API-Key header injection,
and structured error raising.

Source: OpenAPI 3.0.3 spec at https://booking-pms.taskforceai.tech/api/docs.json
"""
from __future__ import annotations

import logging
import os
from typing import Any, Optional

import aiohttp

logger = logging.getLogger(__name__)

KPMS_BASE_URL: str = os.getenv("KPMS_BASE_URL", "https://booking-pms.taskforceai.tech").rstrip("/")
KPMS_API_KEY: str = os.getenv("KPMS_API_KEY", "")
KPMS_TIMEOUT: float = float(os.getenv("KPMS_TIMEOUT", "20"))

_session: Optional[aiohttp.ClientSession] = None


class KpmsError(Exception):
    """Raised on any non-2xx response or transport failure."""
    def __init__(self, message: str, *, status: Optional[int] = None, body: Any = None):
        super().__init__(message)
        self.status = status
        self.body = body


def is_configured() -> bool:
    return bool(KPMS_API_KEY) and bool(KPMS_BASE_URL)


async def get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        connector = aiohttp.TCPConnector(limit=10, keepalive_timeout=30)
        timeout = aiohttp.ClientTimeout(total=KPMS_TIMEOUT)
        headers = {"X-API-Key": KPMS_API_KEY, "Accept": "application/json"}
        _session = aiohttp.ClientSession(connector=connector, timeout=timeout, headers=headers)
        logger.info("Created aiohttp session for KPMS")
    return _session


async def close_session() -> None:
    global _session
    if _session is not None and not _session.closed:
        await _session.close()
        _session = None
        logger.info("Closed KPMS aiohttp session")


async def _request(method: str, path: str, *, params: dict | None = None, json_body: dict | None = None) -> Any:
    if not is_configured():
        raise KpmsError("KPMS not configured (set KPMS_API_KEY)")
    session = await get_session()
    url = f"{KPMS_BASE_URL}{path}"
    try:
        async with session.request(method, url, params=params, json=json_body) as resp:
            text = await resp.text()
            body: Any
            try:
                body = await resp.json(content_type=None) if text else None
            except Exception:
                body = text
            if resp.status >= 400:
                msg = body.get("error") if isinstance(body, dict) and "error" in body else f"HTTP {resp.status}"
                logger.warning("KPMS %s %s -> %s: %s", method, path, resp.status, str(body)[:300])
                raise KpmsError(str(msg), status=resp.status, body=body)
            return body
    except aiohttp.ClientError as exc:
        logger.exception("KPMS transport error on %s %s", method, path)
        raise KpmsError(f"Transport error: {exc}") from exc


# --------- endpoint methods (each ~1 line; no logic) ---------

async def health() -> dict:
    return await _request("GET", "/api/health")

async def init_bootstrap() -> dict:
    return await _request("GET", "/api/init")

async def list_properties() -> list[dict]:
    return await _request("GET", "/api/properties")

async def list_room_types() -> list[dict]:
    return await _request("GET", "/api/room-types")

async def list_rooms(*, property_id: str | None = None, type_id: str | None = None, status: str | None = None) -> list[dict]:
    params = {k: v for k, v in {"propertyId": property_id, "typeId": type_id, "status": status}.items() if v is not None}
    return await _request("GET", "/api/rooms", params=params or None)

async def list_reservations(*, room_id: str | None = None, guest_id: str | None = None, check_in: str | None = None, check_out: str | None = None, status: str | None = None, source: str | None = None) -> list[dict]:
    params = {k: v for k, v in {"roomId": room_id, "guestId": guest_id, "checkIn": check_in, "checkOut": check_out, "status": status, "source": source}.items() if v is not None}
    return await _request("GET", "/api/reservations", params=params or None)

async def get_reservation(reservation_id: str) -> dict:
    return await _request("GET", f"/api/reservations/{reservation_id}")

async def create_reservation(payload: dict) -> dict:
    return await _request("POST", "/api/reservations", json_body=payload)

async def update_reservation(reservation_id: str, payload: dict) -> dict:
    return await _request("PUT", f"/api/reservations/{reservation_id}", json_body=payload)

async def delete_reservation(reservation_id: str) -> dict:
    return await _request("DELETE", f"/api/reservations/{reservation_id}")

async def search_guests(query: str) -> list[dict]:
    return await _request("GET", "/api/guests", params={"q": query})

async def create_guest(payload: dict) -> dict:
    return await _request("POST", "/api/guests", json_body=payload)
