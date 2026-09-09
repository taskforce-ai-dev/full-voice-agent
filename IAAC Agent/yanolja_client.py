"""Async HTTP client for Yanolja Eezy PMS.

Thin aiohttp wrapper. JWT auth: lazy login, cache token, refresh+retry once on 401.
NO business logic — that lives in yanolja_service.py.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Optional

import aiohttp

logger = logging.getLogger(__name__)

YANOLJA_BASE_URL: str = os.getenv("YANOLJA_BASE_URL", "https://yanolja.taskforceai.tech/api").rstrip("/")
YANOLJA_USERNAME: str = os.getenv("YANOLJA_USERNAME", "")
YANOLJA_PASSWORD: str = os.getenv("YANOLJA_PASSWORD", "")
# A key present but blank (e.g. an unset compose ``${YANOLJA_TIMEOUT:-30}``
# passthrough) must resolve to the default exactly like a missing key --
# float("") would otherwise raise at import and crash-loop the container.
YANOLJA_TIMEOUT: float = float(os.getenv("YANOLJA_TIMEOUT") or "30")

_session: Optional[aiohttp.ClientSession] = None
_token: Optional[str] = None


class YanoljaError(Exception):
    """Raised on any non-2xx response or transport failure."""
    def __init__(self, message: str, *, status: Optional[int] = None, body: Any = None):
        super().__init__(message)
        self.status = status
        self.body = body


def is_configured() -> bool:
    return bool(YANOLJA_USERNAME) and bool(YANOLJA_PASSWORD) and bool(YANOLJA_BASE_URL)


async def _get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        connector = aiohttp.TCPConnector(limit=10, keepalive_timeout=30)
        timeout = aiohttp.ClientTimeout(total=YANOLJA_TIMEOUT)
        _session = aiohttp.ClientSession(
            connector=connector,
            timeout=timeout,
            headers={"Accept": "application/json"},
        )
        logger.info("Created aiohttp session for Yanolja")
    return _session


async def close_session() -> None:
    global _session, _token
    if _session is not None and not _session.closed:
        await _session.close()
        logger.info("Closed Yanolja aiohttp session")
    _session = None
    _token = None


async def login() -> str:
    """POST /auth/login → token. Updates module-level cache."""
    global _token
    if not is_configured():
        raise YanoljaError("Yanolja not configured (set YANOLJA_USERNAME/PASSWORD)")
    session = await _get_session()
    url = f"{YANOLJA_BASE_URL}/auth/login"
    payload = {"username": YANOLJA_USERNAME, "password": YANOLJA_PASSWORD}
    try:
        async with session.post(url, json=payload) as resp:
            text = await resp.text()
            try:
                body = await resp.json(content_type=None) if text else None
            except Exception:
                body = text
            if resp.status >= 400:
                logger.warning("Yanolja login failed %s: %s", resp.status, str(body)[:300])
                raise YanoljaError(f"Login failed (HTTP {resp.status})", status=resp.status, body=body)
            tok = body.get("token") if isinstance(body, dict) else None
            if not tok:
                raise YanoljaError("Login response missing token", body=body)
            _token = tok
            logger.info("Yanolja login OK")
            return tok
    except aiohttp.ClientError as exc:
        logger.exception("Yanolja transport error on login")
        raise YanoljaError(f"Transport error: {exc}") from exc


async def _request(
    method: str,
    path: str,
    *,
    params: dict | None = None,
    json_body: dict | None = None,
) -> Any:
    """Authenticated request with one auto-retry on 401."""
    global _token
    if not is_configured():
        raise YanoljaError("Yanolja not configured")
    if _token is None:
        await login()

    session = await _get_session()
    url = f"{YANOLJA_BASE_URL}{path}"

    for attempt in (1, 2):
        headers = {"Authorization": f"Bearer {_token}"}
        try:
            async with session.request(method, url, params=params, json=json_body, headers=headers) as resp:
                text = await resp.text()
                try:
                    body = await resp.json(content_type=None) if text else None
                except Exception:
                    body = text
                if resp.status == 401 and attempt == 1:
                    logger.info("Yanolja 401 on %s %s; refreshing token", method, path)
                    _token = None
                    await login()
                    continue
                if resp.status >= 400:
                    msg = body.get("message") if isinstance(body, dict) and "message" in body else f"HTTP {resp.status}"
                    logger.warning("Yanolja %s %s -> %s: %s", method, path, resp.status, str(body)[:300])
                    raise YanoljaError(str(msg), status=resp.status, body=body)
                logger.debug("Yanolja %s %s -> %s", method, path, resp.status)
                return body
        except aiohttp.ClientError as exc:
            logger.exception("Yanolja transport error on %s %s", method, path)
            raise YanoljaError(f"Transport error: {exc}") from exc

    raise YanoljaError("Yanolja request failed after retry", status=401)


# --------- endpoint wrappers ---------

async def list_rooms() -> list[dict]:
    body = await _request("GET", "/rooms")
    if isinstance(body, dict):
        rooms = body.get("rooms", [])
    else:
        rooms = body or []
    return rooms if isinstance(rooms, list) else []


async def get_availability(check_in: str, check_out: str) -> list[dict]:
    body = await _request(
        "GET",
        "/rooms/availability",
        params={"checkIn": check_in, "checkOut": check_out},
    )
    if isinstance(body, dict):
        rooms = body.get("availableRooms", [])
    else:
        rooms = body or []
    return rooms if isinstance(rooms, list) else []


async def list_reservations() -> list[dict]:
    body = await _request("GET", "/reservations")
    if isinstance(body, dict):
        for key in ("reservations", "data"):
            if isinstance(body.get(key), list):
                return body[key]
        return []
    return body if isinstance(body, list) else []


async def create_guest(payload: dict) -> dict:
    body = await _request("POST", "/guests", json_body=payload)
    if isinstance(body, dict) and "guest" in body:
        return body["guest"] or {}
    return body if isinstance(body, dict) else {}


async def create_reservation(payload: dict) -> dict:
    body = await _request("POST", "/reservations", json_body=payload)
    if isinstance(body, dict) and "reservation" in body:
        return body["reservation"] or {}
    return body if isinstance(body, dict) else {}


async def get_reservation(reservation_id: Any) -> dict:
    body = await _request("GET", f"/reservations/{reservation_id}")
    if isinstance(body, dict) and "reservation" in body:
        return body["reservation"] or {}
    return body if isinstance(body, dict) else {}


async def cancel_reservation(reservation_id: Any) -> dict:
    body = await _request("POST", f"/reservations/{reservation_id}/cancel")
    if isinstance(body, dict) and "reservation" in body:
        return body["reservation"] or {}
    return body if isinstance(body, dict) else {}
