"""Deterministic room-rate resolution for Kavya.

Rates are deliberately resolved from canonical booking state rather than semantic
knowledge retrieval.  The catalog returns a non-quotable result whenever a
required input is missing, unknown, or spans resident seasons.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import re

import yanolja_service


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
_ROOM_SELECTION_PREFIXES = (
    ("i", "choose"), ("we", "choose"),
    ("i", "would", "like"), ("we", "would", "like"),
    ("i", "want"), ("we", "want"),
    ("i", "will", "take"), ("we", "will", "take"),
    ("ill", "take"), ("well", "take"),
)
_ROOM_AMOUNT_SUFFIXES = frozenset({
    (),
    ("per", "night"),
    ("per", "room", "per", "night"),
    ("for", "one", "night"),
})
_NON_ROOM_PRICE_SUBJECTS = frozenset({
    "spa", "dinner", "activity", "activities", "restaurant",
})


@dataclass(frozen=True)
class RoomRateIntent:
    """A room-price request with an explicit target, ambiguity, or neither."""

    kind: str
    rooms: tuple[str, ...] = ()
    unresolved: bool = False


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
        return (
            "AUTHORITATIVE RATE RECORD:\n"
            "- status: no_quote\n"
            f"- reason: {self.reason}\n"
            "- do not quote a rate from reference context; ask for the missing "
            "detail or offer human confirmation."
        )


def recognize_residency(utterance: str) -> str | None:
    """Recognize only explicit, unambiguous residency statements.

    Negation is evaluated before the local-resident phrases so "not a Sri
    Lankan resident" never becomes a resident rate.  Names, phone numbers,
    accents, and other indirect signals cannot reach this seam.
    """
    normalized = " ".join(re.findall(r"[a-z]+", str(utterance).lower()))
    if not normalized:
        return None
    if (
        " but " in normalized
        and any(term in normalized for term in ("local", "sri lankan resident"))
        and any(term in normalized for term in ("foreign", "overseas", "non resident"))
    ):
        return None
    subject = r"(?:i am|i m|im|we are|we re|my party is|our party is)"
    candidates: set[str] = set()
    if re.search(rf"\b{subject}\s+(?:(?:a|an)\s+)?(?:sri lankan resident|local resident|local)\b", normalized):
        candidates.add(RESIDENT)
    if re.search(
        rf"\b{subject}\s+(?:not\s+(?:(?:a|an)\s+)?(?:sri lankan resident|local)|"
        r"(?:(?:a|an)\s+)?(?:foreign guest|foreign visitor|foreign|international guest|non resident))\b",
        normalized,
    ):
        candidates.add(FOREIGN)
    # A terse direct answer is safe only when it contains no other subject or
    # competing residency classification.
    if normalized in {"sri lankan resident", "local resident"}:
        candidates.add(RESIDENT)
    if normalized in {"foreign guest", "foreign visitor", "international guest", "non resident"}:
        candidates.add(FOREIGN)
    if normalized in {"not a sri lankan resident", "not sri lankan resident", "not local"}:
        candidates.add(FOREIGN)
    if len(candidates) != 1:
        return None
    return candidates.pop()


def _matching_canonical_rooms(tokens: tuple[str, ...]) -> list[str]:
    matches: list[tuple[int, str]] = []
    for room in yanolja_service.DEMO_NIGHTLY_RATE_USD:
        room_tokens = tuple(re.findall(r"[a-z]+", room.lower()))
        for index in range(len(tokens) - len(room_tokens) + 1):
            if tokens[index:index + len(room_tokens)] == room_tokens:
                matches.append((index, room))
                break
    return [room for _index, room in sorted(matches)]


def _contains_tokens(tokens: tuple[str, ...], phrase: tuple[str, ...]) -> bool:
    return any(
        tokens[index:index + len(phrase)] == phrase
        for index in range(len(tokens) - len(phrase) + 1)
    )


def _room_after(
    tokens: tuple[str, ...],
    prefix: tuple[str, ...],
    room_tokens: tuple[str, ...],
) -> tuple[str, ...] | None:
    for index in range(len(tokens) - len(prefix) - len(room_tokens) + 1):
        room_start = index + len(prefix)
        room_end = room_start + len(room_tokens)
        if (
            tokens[index:room_start] == prefix
            and tokens[room_start:room_end] == room_tokens
        ):
            return tokens[room_end:]
    return None


def _is_room_rate_form(tokens: tuple[str, ...], room: str) -> bool:
    room_tokens = tuple(re.findall(r"[a-z]+", room.lower()))
    for prefix, suffixes in (
        (("how", "much", "is"), _ROOM_AMOUNT_SUFFIXES),
        (("how", "much", "is", "the"), _ROOM_AMOUNT_SUFFIXES),
        (("how", "much", "does"), frozenset({("cost",)})),
        (("how", "much", "does", "the"), frozenset({("cost",)})),
        (("price", "of"), frozenset({()})),
        (("price", "of", "the"), frozenset({()})),
        (("cost", "of"), frozenset({()})),
        (("cost", "of", "the"), frozenset({()})),
        (("rate", "for"), frozenset({()})),
        (("rate", "for", "the"), frozenset({()})),
    ):
        suffix = _room_after(tokens, prefix, room_tokens)
        if suffix in suffixes:
            return True
    return _contains_tokens(tokens, ("room", "rate")) or _contains_tokens(
        tokens, ("nightly", "rate")
    )


def _looks_like_rate_request(tokens: tuple[str, ...]) -> bool:
    return (
        _contains_tokens(tokens, ("room", "rate"))
        or _contains_tokens(tokens, ("nightly", "rate"))
        or tokens[:2] == ("how", "much")
        or "price" in tokens
        or "cost" in tokens
        or "rate" in tokens
        or "rates" in tokens
    )


def classify_room_rate_intent(utterance: str) -> RoomRateIntent:
    """Classify only grammar-bound room-price requests, never price-like prose."""
    tokens = tuple(re.findall(r"[a-z]+", str(utterance).lower()))
    if not tokens or _NON_ROOM_PRICE_SUBJECTS.intersection(tokens):
        return RoomRateIntent("NONE")
    rooms = tuple(_matching_canonical_rooms(tokens))
    if len(rooms) > 1:
        return (
            RoomRateIntent("AMBIGUOUS_RATE", rooms)
            if _looks_like_rate_request(tokens)
            else RoomRateIntent("NONE")
        )
    if len(rooms) == 1 and _is_room_rate_form(tokens, rooms[0]):
        return RoomRateIntent("RATE", rooms)
    if not rooms and _looks_like_rate_request(tokens):
        return RoomRateIntent(
            "RATE",
            unresolved=_contains_tokens(tokens, ("room", "rate", "for")),
        )
    return RoomRateIntent("NONE")


def is_room_rate_intent(
    utterance: str, *, has_grounded_rate_state: bool = False,
) -> bool:
    """Compatibility wrapper for callers that need only a boolean result."""
    return classify_room_rate_intent(utterance).kind != "NONE"


def is_room_rate_follow_up(utterance: str) -> bool:
    return tuple(re.findall(r"[a-z]+", str(utterance).lower())) == (
        "how", "much", "is", "it",
    )


def recognize_selected_room(utterance: str) -> str | None:
    """Return one canonical room only from a clear guest selection statement."""
    tokens = tuple(re.findall(r"[a-z]+", str(utterance).lower()))
    if not tokens:
        return None
    matches = _matching_canonical_rooms(tokens)
    if len(matches) != 1:
        return None
    room_tokens = tuple(re.findall(r"[a-z]+", matches[0].lower()))
    if tokens == room_tokens:
        return matches[0]
    for prefix in _ROOM_SELECTION_PREFIXES:
        if tokens[:len(prefix)] != prefix:
            continue
        room_end = len(prefix) + len(room_tokens)
        if tokens[len(prefix):room_end] == room_tokens and tokens[room_end:] == ():
            return matches[0]
    for index in range(len(tokens) - len(room_tokens) + 1):
        room_end = index + len(room_tokens)
        if (
            tokens[index:room_end] == room_tokens
            and tokens[room_end:room_end + 1] in {("available",), ("availability",)}
        ):
            return matches[0]
    if classify_room_rate_intent(utterance).kind == "RATE":
        return matches[0]
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
    if not canonical_room or canonical_room not in yanolja_service.DEMO_NIGHTLY_RATE_USD:
        return RateResolution(None, normalized_residency, None, None, "unknown_room")
    if normalized_residency is None:
        return RateResolution(canonical_room, None, None, None, "unknown_residency")
    if stay is None:
        return RateResolution(canonical_room, normalized_residency, None, None, "invalid_dates")
    if not yanolja_service.DEMO_RATES_ENABLED:
        return RateResolution(canonical_room, normalized_residency, None, None, "rates_disabled")
    if normalized_residency == FOREIGN:
        rate = yanolja_service.demo_rate_for(canonical_room)
        if rate is None:
            return RateResolution(canonical_room, FOREIGN, None, None, "rates_disabled")
        return RateResolution(
            canonical_room,
            FOREIGN,
            "USD",
            rate,
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
