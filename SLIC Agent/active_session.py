"""
Cross-call active-session store for the SLIC demo.

Keyed by caller phone. Holds state about in-flight claims (assessor
dispatched, ETA, claim reference) so that if the same caller rings back
within TTL, the agent can pick up where they left off instead of
restarting the intake.

In-memory dict is authoritative during a process lifetime; a JSON
snapshot on disk provides resilience across restarts.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_STORE_PATH = Path(__file__).parent / "active_sessions.json"
_TTL_SECONDS = int(os.getenv("SLIC_SESSION_TTL_SECONDS", "300"))  # 5 min
_lock = threading.Lock()


def _normalize_phone(phone: str) -> str:
    if not phone:
        return ""
    cleaned = re.sub(r"[^\d+]", "", phone.strip())
    if cleaned and not cleaned.startswith("+") and cleaned.startswith("94"):
        cleaned = "+" + cleaned
    return cleaned


@dataclass
class SessionRecord:
    phone: str
    name: str
    policy_no: str
    vehicle: dict[str, Any]  # {"make", "model", "reg_no", "policy_item"}
    assessor_dispatched_at: str  # ISO 8601 UTC
    assessor_eta_minutes: int
    claim_reference: str
    reg_no: str = ""  # canonical reg_no (primary identifier in the new flow)
    assessor_name: str = ""
    last_call_sid: str = ""
    accident_description: str = ""
    location: str = ""
    injuries: str = ""
    vehicle_condition: str = ""
    expires_at: str = ""  # ISO 8601 UTC

    def is_expired(self, now: datetime | None = None) -> bool:
        if not self.expires_at:
            return True
        now = now or datetime.now(timezone.utc)
        try:
            return datetime.fromisoformat(self.expires_at) <= now
        except ValueError:
            return True


def _load() -> dict[str, SessionRecord]:
    if not _STORE_PATH.exists():
        return {}
    try:
        with _STORE_PATH.open("r", encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("active_sessions.json unreadable (%s) — starting empty", e)
        return {}
    out: dict[str, SessionRecord] = {}
    allowed = set(SessionRecord.__dataclass_fields__.keys())
    for phone, data in raw.items():
        try:
            # Drop unknown keys from older snapshots; defaults fill new fields
            clean = {k: v for k, v in data.items() if k in allowed}
            out[_normalize_phone(phone)] = SessionRecord(**clean)
        except TypeError as e:
            logger.warning("skipping malformed session for %s: %s", phone, e)
    return out


_SESSIONS: dict[str, SessionRecord] = _load()
logger.info("active_session loaded %d sessions (TTL=%ds)", len(_SESSIONS), _TTL_SECONDS)


def _persist() -> None:
    snapshot = {phone: asdict(rec) for phone, rec in _SESSIONS.items()}
    tmp = _STORE_PATH.with_suffix(".json.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(snapshot, f, indent=2)
        os.replace(tmp, _STORE_PATH)
    except OSError as e:
        logger.error("failed to persist active_sessions.json: %s", e)


def get(phone: str) -> SessionRecord | None:
    """Return the active session for `phone` if one exists and hasn't expired."""
    phone_n = _normalize_phone(phone)
    with _lock:
        record = _SESSIONS.get(phone_n)
        if record is None:
            return None
        if record.is_expired():
            _SESSIONS.pop(phone_n, None)
            _persist()
            return None
        return record


def set(  # noqa: A001 — intentional "set" verb for API clarity
    phone: str,
    *,
    name: str,
    policy_no: str,
    vehicle: dict[str, Any],
    assessor_eta_minutes: int,
    claim_reference: str,
    reg_no: str = "",
    assessor_name: str = "",
    last_call_sid: str = "",
    accident_description: str = "",
    location: str = "",
    injuries: str = "",
    vehicle_condition: str = "",
) -> SessionRecord:
    """Create or refresh the active session for `phone`. TTL resets on write."""
    now = datetime.now(timezone.utc)
    record = SessionRecord(
        phone=_normalize_phone(phone),
        name=name,
        policy_no=policy_no,
        vehicle=vehicle,
        assessor_dispatched_at=now.isoformat(),
        assessor_eta_minutes=int(assessor_eta_minutes),
        claim_reference=claim_reference,
        reg_no=reg_no,
        assessor_name=assessor_name,
        last_call_sid=last_call_sid,
        accident_description=accident_description,
        location=location,
        injuries=injuries,
        vehicle_condition=vehicle_condition,
        expires_at=(now + timedelta(seconds=_TTL_SECONDS)).isoformat(),
    )
    with _lock:
        _SESSIONS[record.phone] = record
        _persist()
    logger.info("active session SET for %s (expires %s)", record.phone, record.expires_at)
    return record


def get_by_reg_no(reg_no: str) -> SessionRecord | None:
    """Return an active session matching `reg_no` (secondary lookup)."""
    if not reg_no:
        return None
    reg_norm = reg_no.strip().upper()
    with _lock:
        for rec in list(_SESSIONS.values()):
            if rec.reg_no and rec.reg_no.upper() == reg_norm:
                if rec.is_expired():
                    _SESSIONS.pop(rec.phone, None)
                    _persist()
                    continue
                return rec
    return None


def clear(phone: str) -> None:
    phone_n = _normalize_phone(phone)
    with _lock:
        if _SESSIONS.pop(phone_n, None) is not None:
            _persist()
            logger.info("active session CLEARED for %s", phone_n)


def remaining_eta_minutes(record: SessionRecord, now: datetime | None = None) -> int:
    """Given an existing dispatch record, estimate remaining ETA."""
    now = now or datetime.now(timezone.utc)
    try:
        dispatched = datetime.fromisoformat(record.assessor_dispatched_at)
    except ValueError:
        return record.assessor_eta_minutes
    elapsed = (now - dispatched).total_seconds() / 60
    remaining = max(1, int(round(record.assessor_eta_minutes - elapsed)))
    return remaining
