"""
Mock customer database for the SLIC demo.

Loads `mock_customers.json` once at import time and exposes
phone-number-keyed lookups. In production this would be replaced with a
real DB query against the SLIC policy-admin system.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_DB_PATH = Path(__file__).parent / "mock_customers.json"


def _normalize_phone(phone: str) -> str:
    """Strip non-digit/+ chars so '+94 77 123 4567' matches '+94771234567'."""
    if not phone:
        return ""
    cleaned = re.sub(r"[^\d+]", "", phone.strip())
    if cleaned and not cleaned.startswith("+") and cleaned.startswith("94"):
        cleaned = "+" + cleaned
    return cleaned


def _normalize_reg_no(reg_no: str) -> str:
    """Canonical reg-no form: uppercase, collapse whitespace/hyphens to a single '-'.

    Matches 'cab 1234', 'CAB-1234', 'CAB1234' -> 'CAB-1234'.
    """
    if not reg_no:
        return ""
    cleaned = re.sub(r"[^A-Za-z0-9]", "", reg_no).upper()
    # Split at first digit so 'CAB1234' -> 'CAB-1234'
    m = re.match(r"([A-Z]+)(\d+)$", cleaned)
    if m:
        return f"{m.group(1)}-{m.group(2)}"
    return cleaned


def _load() -> dict[str, dict[str, Any]]:
    if not _DB_PATH.exists():
        logger.warning("mock_customers.json not found at %s — DB is empty", _DB_PATH)
        return {}
    with _DB_PATH.open("r", encoding="utf-8") as f:
        raw = json.load(f)
    return {_normalize_phone(k): v for k, v in raw.items()}


def _build_reg_index(
    customers: dict[str, dict[str, Any]],
) -> dict[str, tuple[str, dict[str, Any]]]:
    """Build reg_no -> (phone, vehicle_dict) index from the customer table."""
    index: dict[str, tuple[str, dict[str, Any]]] = {}
    for phone, record in customers.items():
        for vehicle in record.get("vehicles", []):
            key = _normalize_reg_no(vehicle.get("reg_no", ""))
            if key:
                index[key] = (phone, vehicle)
    return index


_CUSTOMERS: dict[str, dict[str, Any]] = _load()
_REG_INDEX: dict[str, tuple[str, dict[str, Any]]] = _build_reg_index(_CUSTOMERS)
logger.info(
    "mock_db loaded %d customers (%d vehicles indexed by reg_no)",
    len(_CUSTOMERS), len(_REG_INDEX),
)


def lookup_by_phone(phone: str) -> dict[str, Any] | None:
    """Return the customer record for `phone`, or None if not on file."""
    return _CUSTOMERS.get(_normalize_phone(phone))


def lookup_by_reg_no(reg_no: str) -> dict[str, Any] | None:
    """Return {customer, vehicle, phone} for `reg_no`, or None if not on file."""
    entry = _REG_INDEX.get(_normalize_reg_no(reg_no))
    if entry is None:
        return None
    phone, vehicle = entry
    customer = _CUSTOMERS.get(phone)
    if customer is None:
        return None
    return {"customer": customer, "vehicle": vehicle, "phone": phone}


def get_vehicles(phone: str) -> list[dict[str, Any]]:
    """Return the list of vehicles insured under `phone`. Empty if unknown."""
    record = lookup_by_phone(phone)
    return list(record.get("vehicles", [])) if record else []


def is_known(phone: str) -> bool:
    return lookup_by_phone(phone) is not None
