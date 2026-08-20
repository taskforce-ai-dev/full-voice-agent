"""Deterministic room-rate resolution for Kavya.

Rates are deliberately resolved from canonical booking state rather than semantic
knowledge retrieval.  The catalog returns a non-quotable result whenever a
required input is missing, unknown, or spans resident seasons.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import re

from yanolja_service import DEMO_NIGHTLY_RATE_USD


RESIDENT = "resident"
FOREIGN = "foreign"

_RESIDENT_NIGHTLY_RATE_LKR: dict[str, tuple[int, int]] = {
    "Forest Escape Suite": (158000, 185000),
    "Eco Harmony Suite": (180000, 211000),
    "Sunrise Vista Premium Suite": (214000, 250000),
    "Mount Luxe Chalet": (259000, 303000),
    "Mount Monarch Chalet": (315000, 368000),
}
_OFF_PEAK_MONTHS = frozenset({2, 3, 5, 6, 7, 8, 9, 10, 11})
_PEAK_MONTHS = frozenset({4, 12})


@dataclass(frozen=True)
class RateResolution:
    room: str | None
    residency: str | None
    currency: str | None
    nightly_rate: int | None
    reason: str | None = None

    @property
    def is_quotable(self) -> bool:
        return self.nightly_rate is not None and self.reason is None

    def authoritative_context(self) -> str:
        """Render the single record an LLM may use for a rate answer."""
        if self.is_quotable:
            return (
                "AUTHORITATIVE RATE RECORD (this overrides all rate-like reference prose):\n"
                f"- room: {self.room}\n"
                f"- residency: {self.residency}\n"
                f"- currency: {self.currency}\n"
                f"- rate_per_room_per_night: {self.nightly_rate}\n"
                "- quote only this exact record; do not select a price from reference context."
            )
        if self.reason == "mixed_period":
            return (
                "AUTHORITATIVE RATE RECORD:\n"
                f"- room: {self.room}\n"
                f"- residency: {self.residency}\n"
                "- status: mixed_period\n"
                "- do not quote one nightly rate; ask for clarification or offer human confirmation."
            )
        return ""


def recognize_residency(utterance: str) -> str | None:
    """Recognize only explicit, unambiguous residency statements.

    Negation is evaluated before the local-resident phrases so "not a Sri
    Lankan resident" never becomes a resident rate.  Names, phone numbers,
    accents, and other indirect signals cannot reach this seam.
    """
    normalized = " ".join(re.findall(r"[a-z]+", str(utterance).lower()))
    if not normalized:
        return None
    foreign_phrases = (
        "not a sri lankan resident",
        "not sri lankan resident",
        "not local",
        "foreign guest",
        "foreign visitor",
        "international guest",
        "from overseas",
        "non resident",
    )
    if any(phrase in normalized for phrase in foreign_phrases):
        return FOREIGN
    resident_phrases = (
        "sri lankan resident",
        "local resident",
        "i am local",
        "im local",
        "we are local",
        "were local",
    )
    if any(phrase in normalized for phrase in resident_phrases):
        return RESIDENT
    return None


def _normalize_residency(value: str) -> str | None:
    normalized = " ".join(re.findall(r"[a-z]+", str(value).lower()))
    if normalized in {RESIDENT, "sri lankan resident", "local resident"}:
        return RESIDENT
    if normalized in {FOREIGN, "foreign guest", "foreign visitor", "international guest"}:
        return FOREIGN
    return recognize_residency(normalized)


def _parse_stay(check_in: str, check_out: str) -> tuple[date, date] | None:
    try:
        start = date.fromisoformat(str(check_in))
        end = date.fromisoformat(str(check_out))
    except (TypeError, ValueError):
        return None
    return (start, end) if start < end else None


def _resident_season(start: date, end: date) -> str | None:
    months = {start.month}
    cursor = start
    while cursor < end:
        months.add(cursor.month)
        cursor = date(cursor.year + (cursor.month == 12), cursor.month % 12 + 1, 1)
    if months <= _OFF_PEAK_MONTHS:
        return "off_peak"
    if months <= _PEAK_MONTHS:
        return "peak"
    if len(months) > 1:
        return "mixed_period"
    return None


def resolve_rate(
    *, room: str, residency: str, check_in: str, check_out: str
) -> RateResolution:
    """Resolve one exact nightly rate or a deliberately non-quotable result."""
    canonical_room = str(room).strip()
    normalized_residency = _normalize_residency(residency)
    stay = _parse_stay(check_in, check_out)
    if not canonical_room or canonical_room not in DEMO_NIGHTLY_RATE_USD:
        return RateResolution(None, normalized_residency, None, None, "unknown_room")
    if normalized_residency is None:
        return RateResolution(canonical_room, None, None, None, "unknown_residency")
    if stay is None:
        return RateResolution(canonical_room, normalized_residency, None, None, "invalid_dates")
    if normalized_residency == FOREIGN:
        return RateResolution(
            canonical_room,
            FOREIGN,
            "USD",
            DEMO_NIGHTLY_RATE_USD[canonical_room],
        )

    season = _resident_season(*stay)
    if season == "mixed_period":
        return RateResolution(canonical_room, RESIDENT, None, None, season)
    if season is None:
        return RateResolution(canonical_room, RESIDENT, None, None, "unsupported_period")
    off_peak, peak = _RESIDENT_NIGHTLY_RATE_LKR[canonical_room]
    return RateResolution(
        canonical_room,
        RESIDENT,
        "LKR",
        peak if season == "peak" else off_peak,
    )
