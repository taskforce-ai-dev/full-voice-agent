"""
Mock customer database for the Bank of Sri Lanka (BSL) demo voice agent.

Loads `mock_customers.json` once at import time and exposes account-keyed
lookups plus a lenient identity-verification helper. In production this would
be replaced with real DB queries against the BSL core banking system.

Public API (signatures locked — tools.py depends on these):
    lookup_account(account_no) -> dict | None
    verify_identity(account_no, nic, dob, mothers_maiden_name) -> dict
    get_multi_account_holder(account_holder_name) -> list[str]
    _normalize_account_no(raw) -> str | None
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from metaphone import doublemetaphone

logger = logging.getLogger(__name__)

_DB_PATH = Path(__file__).parent / "mock_customers.json"

# ---------------------------------------------------------------------------
# Load + index
# ---------------------------------------------------------------------------


def _load() -> dict[str, Any]:
    """Load the mock JSON file once. Return empty shell if missing/corrupt."""
    if not _DB_PATH.exists():
        logger.warning("mock_customers.json not found at %s — DB is empty", _DB_PATH)
        return {"accounts": {}, "_multi_account_holders": {}}
    try:
        with _DB_PATH.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Failed to load mock_customers.json: %s", exc)
        return {"accounts": {}, "_multi_account_holders": {}}
    # Defensive: ensure expected top-level keys exist.
    data.setdefault("accounts", {})
    data.setdefault("_multi_account_holders", {})
    return data


def _build_last4_index(accounts: dict[str, Any]) -> dict[str, str]:
    """Map last-4 digits → canonical account_no."""
    index: dict[str, str] = {}
    for acc_no, record in accounts.items():
        last4 = record.get("account_no_last4")
        if not last4:
            # Derive from canonical account_no if not stored.
            digits = re.sub(r"\D", "", acc_no)
            if len(digits) >= 4:
                last4 = digits[-4:]
        if last4:
            index[str(last4)] = acc_no
    return index


_DB: dict[str, Any] = _load()
_ACCOUNTS: dict[str, dict[str, Any]] = _DB.get("accounts", {})
_MULTI_HOLDERS: dict[str, list[str]] = _DB.get("_multi_account_holders", {})
_LAST4_INDEX: dict[str, str] = _build_last4_index(_ACCOUNTS)
# Case-insensitive multi-holder index so noisy STT casing still matches.
_MULTI_HOLDERS_LOWER: dict[str, list[str]] = {
    name.lower().strip(): list(accs) for name, accs in _MULTI_HOLDERS.items()
}

logger.info(
    "mock_db loaded %d accounts (%d last-4 keys, %d multi-holders)",
    len(_ACCOUNTS), len(_LAST4_INDEX), len(_MULTI_HOLDERS),
)

# ---------------------------------------------------------------------------
# Normalisation helpers
# ---------------------------------------------------------------------------

# Phrases callers tend to prepend before the digits — strip these
# (case-insensitive) BEFORE pulling out digits so "ending in 9201" → "9201".
_ACCOUNT_PHRASE_PATTERNS = [
    r"\bending\s+in\b",
    r"\bending\s+with\b",
    r"\baccount\s+number\s+is\b",
    r"\baccount\s+number\b",
    r"\baccount\s+no\.?\b",
    r"\bmy\s+account\s+is\b",
    r"\bthe\s+account\s+is\b",
    r"\baccount\s+is\b",
    r"\bit\s+is\b",
    r"\bit's\b",
    r"\bnumber\s+is\b",
    r"\blast\s+four\b",
    r"\blast\s+4\b",
]
_ACCOUNT_PHRASE_RE = re.compile("|".join(_ACCOUNT_PHRASE_PATTERNS), re.IGNORECASE)


def _normalize_account_no(raw: str) -> str | None:
    """Canonicalise raw account input to 'XXXX-XXXX-XXXX' or 4-digit last4.

    Accepts:
      - 'XXXX-XXXX-XXXX'
      - 'XXXX XXXX XXXX'
      - 'XXXXXXXXXXXX' (12 digits)
      - 'XXXX' (last 4)
      - 'ending in XXXX', 'my account is XXXX', 'account number is …', etc.

    Returns None if no extractable digits or unsupported length.
    """
    if not raw or not isinstance(raw, str):
        return None
    # Expand spoken numbers ("one zero four two" → "1042") before digit extraction.
    raw = expand_spoken_numbers(raw)
    # Strip leading conversational phrases.
    cleaned = _ACCOUNT_PHRASE_RE.sub(" ", raw)
    digits = re.sub(r"\D", "", cleaned)
    if not digits:
        return None
    if len(digits) == 12:
        return f"{digits[0:4]}-{digits[4:8]}-{digits[8:12]}"
    if len(digits) == 4:
        return digits
    # STT commonly drops a digit or 4-digit group; LLM also sometimes splices a
    # corrected last-4 onto a wrong base, producing 9/10/11-digit hybrids.
    # For any 5-11 digit input, fall back to last-4 lookup using the trailing 4.
    if 5 <= len(digits) <= 11:
        return digits[-4:]
    # Charitable fallback: if the caller leaks an extra digit (e.g. STT inserts
    # a stray '1') and we get >12 digits, take the trailing 12.
    if len(digits) > 12:
        tail = digits[-12:]
        return f"{tail[0:4]}-{tail[4:8]}-{tail[8:12]}"
    return None


def _norm_text(s: str) -> str:
    """Lowercase + collapse internal whitespace."""
    if not s:
        return ""
    return re.sub(r"\s+", " ", s).strip().lower()


# ---------------------------------------------------------------------------
# Spoken-number expansion (STT says "double five" → "55", "oh" → "0", etc.)
# Applied before digit extraction in NIC and account-number normalisation.
# ---------------------------------------------------------------------------

_SPOKEN_DIGIT_MAP: dict[str, str] = {
    "zero": "0", "oh": "0", "o": "0",
    "one": "1",
    "two": "2",
    "three": "3",
    "four": "4",
    "five": "5",
    "six": "6",
    "seven": "7",
    "eight": "8",
    "nine": "9",
}

# "double five" / "double 5" → "55";  "triple six" / "triple 6" → "666"
_DOUBLE_RE = re.compile(
    r"\bdouble\s+([0-9]|zero|oh|one|two|three|four|five|six|seven|eight|nine)\b",
    re.IGNORECASE,
)
_TRIPLE_RE = re.compile(
    r"\btriple\s+([0-9]|zero|oh|one|two|three|four|five|six|seven|eight|nine)\b",
    re.IGNORECASE,
)


def _resolve_digit_token(tok: str) -> str:
    """Return the digit character for a spoken word or bare digit token."""
    return _SPOKEN_DIGIT_MAP.get(tok.lower(), tok)


def expand_spoken_numbers(s: str) -> str:
    """Convert spoken-number phrases to digit strings.

    Examples:
        "double five"   → "55"
        "triple six"    → "666"
        "nine oh one"   → "901"
        "oh"            → "0"
    """
    if not s:
        return s
    # Expand "double X" and "triple X" first so the tokens are digits before
    # the word-digit pass runs.
    def _expand_double(m: re.Match) -> str:
        d = _resolve_digit_token(m.group(1))
        return d + d

    def _expand_triple(m: re.Match) -> str:
        d = _resolve_digit_token(m.group(1))
        return d + d + d

    s = _DOUBLE_RE.sub(_expand_double, s)
    s = _TRIPLE_RE.sub(_expand_triple, s)

    # Replace spoken digit words with their digit characters (whole-word only).
    word_re = re.compile(
        r"\b(" + "|".join(re.escape(w) for w in _SPOKEN_DIGIT_MAP) + r")\b",
        re.IGNORECASE,
    )
    s = word_re.sub(lambda m: _SPOKEN_DIGIT_MAP[m.group(1).lower()], s)
    return s


def _norm_nic(s: str) -> str:
    """Uppercase, strip non-alphanumeric, drop trailing 'V' or 'B' if present.

    Also expands spoken numbers ("double five" → "55", "oh" → "0") so STT
    transcripts of digit-by-digit NIC dictation normalise correctly.
    Twilio STT often transcribes the trailing 'V' suffix as 'B' (phonetically
    similar), so both are accepted and stripped.
    """
    if not s:
        return ""
    s = expand_spoken_numbers(s)
    cleaned = re.sub(r"[^A-Za-z0-9]", "", s).upper()
    if cleaned.endswith("V") or cleaned.endswith("B"):
        cleaned = cleaned[:-1]
    return cleaned


# ---------------------------------------------------------------------------
# DOB parsing (manual — no extra deps)
# ---------------------------------------------------------------------------

# Accepted explicit datetime formats (LK day-month-year first if ambiguous).
_DOB_FORMATS = (
    "%d %B %Y",     # 12 April 1990
    "%d %b %Y",     # 12 Apr 1990
    "%d-%m-%Y",     # 12-04-1990
    "%d/%m/%Y",     # 12/04/1990
    "%Y-%m-%d",     # 1990-04-12
    "%Y/%m/%d",     # 1990/04/12
    "%B %d %Y",     # April 12 1990
    "%b %d %Y",     # Apr 12 1990
    "%d.%m.%Y",     # 12.04.1990
)


def _strip_ordinal_suffix(s: str) -> str:
    """'12th April 1990' -> '12 April 1990'."""
    return re.sub(r"\b(\d{1,2})(st|nd|rd|th)\b", r"\1", s, flags=re.IGNORECASE)


def _parse_dob(raw: str) -> tuple[int, int, int] | None:
    """Parse a date string to (year, month, day). Returns None if unparseable."""
    if not raw:
        return None
    s = _strip_ordinal_suffix(raw).strip()
    # Normalise punctuation around month names: "April, 12 1990" → "April 12 1990"
    s = re.sub(r",+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    # Try each known format.
    for fmt in _DOB_FORMATS:
        try:
            dt = datetime.strptime(s, fmt)
            return (dt.year, dt.month, dt.day)
        except ValueError:
            continue
    # Last-ditch: pure digit groups separated by - / . or space — assume DMY.
    parts = re.split(r"[-/\s.]+", s)
    if len(parts) == 3 and all(p.isdigit() for p in parts):
        a, b, c = (int(p) for p in parts)
        if c >= 1000:  # DMY
            return (c, b, a)
        if a >= 1000:  # YMD
            return (a, b, c)
    return None


# ---------------------------------------------------------------------------
# Levenshtein for maiden-name leniency (single typo tolerated)
# ---------------------------------------------------------------------------


def _levenshtein(a: str, b: str) -> int:
    """Classic O(len(a)*len(b)) edit distance."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        curr = [i] + [0] * len(b)
        for j, cb in enumerate(b, start=1):
            cost = 0 if ca == cb else 1
            curr[j] = min(
                curr[j - 1] + 1,        # insertion
                prev[j] + 1,            # deletion
                prev[j - 1] + cost,     # substitution
            )
        prev = curr
    return prev[-1]


def _phonetic_tokens(name: str) -> set[str]:
    """Return Double Metaphone primary + secondary codes for each whitespace token."""
    codes: set[str] = set()
    if not name:
        return codes
    for tok in name.lower().split():
        primary, secondary = doublemetaphone(tok)
        if primary:
            codes.add(primary)
        if secondary:
            codes.add(secondary)
    return codes


# ---------------------------------------------------------------------------
# Maiden-name pronunciation variants
# ---------------------------------------------------------------------------
# For each stored maiden name, list alternative spellings that reflect common
# pronunciations a caller might dictate, that Google STT might output, or that
# appear in Sri Lankan documents. The verify path checks the caller's input
# against ALL variants phonetically — widening the accept-set beyond a single
# canonical spelling. Matching is still tokenised + Double Metaphone, so the
# variants just need to capture meaningful phonetic differences (different
# vowel breakdowns, dropped/added syllables, romanisation choices).
_MAIDEN_NAME_VARIANTS: dict[str, list[str]] = {
    "Jayasinghe":  ["Jayasingha", "Jayasingh", "Jayasinha", "Jayasinhe"],
    "Fernando":    ["Fernandus", "Fernanda"],
    "Seneviratne": ["Seneviratna", "Senevirathne", "Senevirathna"],
    "Rajapaksa":   ["Rajapakse", "Rajapaksha", "Rajapakshe"],
    "Gunawardena": ["Gunawardhana", "Gunawardhane", "Goonawardena", "Gunavardena"],
}


def _expected_maiden_name_pool(canonical: str) -> set[str]:
    """Return the normalised set of accepted spellings for a canonical maiden name."""
    pool: set[str] = {_norm_text(canonical)}
    for variant in _MAIDEN_NAME_VARIANTS.get(canonical, []):
        pool.add(_norm_text(variant))
    return pool


# ---------------------------------------------------------------------------
# Public lookups
# ---------------------------------------------------------------------------


def lookup_account(account_no: str) -> dict | None:
    """Return the account record, or None if not found.

    Accepts canonical, last-4, raw 12 digits, spaced, or 'ending in …' forms.
    Falls back to last-4 lookup when a 12-digit number doesn't exactly match
    (handles single-digit STT garbling like '4901-2223-4477' vs '4901-2233-4477').
    """
    canonical = _normalize_account_no(account_no)
    if canonical is None:
        return None
    if len(canonical) == 4:
        full = _LAST4_INDEX.get(canonical)
        if full is None:
            return None
        return _ACCOUNTS.get(full)
    # Exact match first.
    record = _ACCOUNTS.get(canonical)
    if record is not None:
        return record
    # Fallback: try last-4 digits of the given number — handles STT garbling
    # a middle digit while the last 4 (which the agent reads back for
    # confirmation) are correct.
    last4 = re.sub(r"\D", "", canonical)[-4:]
    if len(last4) == 4:
        full = _LAST4_INDEX.get(last4)
        if full:
            return _ACCOUNTS.get(full)
    return None


def verify_identity(
    account_no: str,
    nic: str,
    dob: str,
    mothers_maiden_name: str,
) -> dict:
    """Lenient match of the three identity fields against the account record.

    Returns:
        {"verified": bool, "mismatched_fields": list[str]}
        Mismatch entries are any of: "nic", "dob", "mothers_maiden_name".
        If the account itself isn't found, all three fields are reported as
        mismatched.
    """
    record = lookup_account(account_no)
    if record is None:
        return {
            "verified": False,
            "mismatched_fields": ["nic", "dob", "mothers_maiden_name"],
        }
    expected = record.get("verification") or {}
    mismatched: list[str] = []

    # NIC — skip if not provided (empty string means field was not asked this call).
    if nic:
        if _norm_nic(nic) != _norm_nic(expected.get("nic", "")):
            mismatched.append("nic")

    # DOB — skip if not provided.
    if dob:
        given_dob = _parse_dob(dob)
        expected_dob = _parse_dob(expected.get("dob", ""))
        if given_dob is None or expected_dob is None or given_dob != expected_dob:
            mismatched.append("dob")

    # Mother's maiden name — skip if not provided.
    # Match against the canonical stored value AND any registered pronunciation
    # variants (see _MAIDEN_NAME_VARIANTS above). For each candidate we try:
    # (a) exact equality, (b) Levenshtein <=2 on the strings,
    # (c) Double Metaphone phonetic match — set intersection, Lev <=2 on code
    #     pairs, or prefix match between codes.
    # Any one candidate accepting is enough.
    if mothers_maiden_name:
        given_m = _norm_text(mothers_maiden_name)
        canonical = expected.get("mothers_maiden_name", "")
        expected_pool = _expected_maiden_name_pool(canonical)
        if not given_m or not expected_pool or expected_pool == {""}:
            if given_m != _norm_text(canonical):
                mismatched.append("mothers_maiden_name")
        else:
            given_codes = _phonetic_tokens(given_m)
            matched = False
            for candidate in expected_pool:
                if not candidate:
                    continue
                if given_m == candidate or _levenshtein(given_m, candidate) <= 2:
                    matched = True
                    break
                candidate_codes = _phonetic_tokens(candidate)
                if (given_codes & candidate_codes) or any(
                    _levenshtein(gc, cc) <= 2
                    or gc.startswith(cc) or cc.startswith(gc)
                    for gc in given_codes
                    for cc in candidate_codes
                ):
                    matched = True
                    break
            if not matched:
                mismatched.append("mothers_maiden_name")

    return {"verified": not mismatched, "mismatched_fields": mismatched}


def get_multi_account_holder(account_holder_name: str) -> list[str]:
    """Return the list of account_nos held by this person, or [] if not multi-holder."""
    if not account_holder_name:
        return []
    key = re.sub(r"\s+", " ", account_holder_name.lower().strip())
    return list(_MULTI_HOLDERS_LOWER.get(key, []))


# ---------------------------------------------------------------------------
# Self-tests
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    nimal_canonical = "1042-8837-9201"

    # lookup_account variants — all should resolve to Nimal Perera's personal acc.
    rec = lookup_account("9201")
    assert rec is not None and rec["account_no"] == nimal_canonical, "last-4 lookup failed"
    assert lookup_account("1042-8837-9201")["account_no"] == nimal_canonical
    assert lookup_account("1042 8837 9201")["account_no"] == nimal_canonical
    assert lookup_account("104288379201")["account_no"] == nimal_canonical
    assert lookup_account("ending in 9201")["account_no"] == nimal_canonical
    assert lookup_account("My account is 1042-8837-9201")["account_no"] == nimal_canonical
    assert lookup_account("nonsense") is None
    assert lookup_account("") is None

    # verify_identity — happy path (all lenient).
    res = verify_identity(
        "1042-8837-9201", "901234567v", "12 april 1990", "jayasinghe"
    )
    assert res == {"verified": True, "mismatched_fields": []}, f"happy path: {res}"

    # verify_identity — wrong NIC only.
    res = verify_identity(
        "1042-8837-9201", "wrong", "12 April 1990", "Jayasinghe"
    )
    assert res == {"verified": False, "mismatched_fields": ["nic"]}, f"wrong nic: {res}"

    # verify_identity — ISO dob + 1-char typo on maiden name.
    res = verify_identity(
        "1042-8837-9201", "901234567V", "1990-04-12", "Jayasingh"
    )
    assert res == {"verified": True, "mismatched_fields": []}, f"lenient: {res}"

    # Multi-holder lookups.
    assert get_multi_account_holder("Nimal Perera") == [
        "1042-8837-9201", "1098-5541-3367",
    ], "Nimal Perera multi-holder lookup failed"
    assert get_multi_account_holder("nimal perera") == [
        "1042-8837-9201", "1098-5541-3367",
    ], "case-insensitive multi-holder lookup failed"
    assert get_multi_account_holder("Dilani Wijesinghe") == []
    assert get_multi_account_holder("") == []

    # Phonetic maiden-name path: STT mangles Sinhala names beyond Levenshtein <=2.
    res = verify_identity(
        "1042-8837-9201", "901234567V", "12 April 1990", "giant singer"
    )
    assert res == {"verified": True, "mismatched_fields": []}, f"phonetic 'giant singer': {res}"
    res = verify_identity(
        "1042-8837-9201", "901234567V", "12 April 1990", "Jinger"
    )
    assert res == {"verified": True, "mismatched_fields": []}, f"phonetic 'Jinger': {res}"
    res = verify_identity(
        "1042-8837-9201", "901234567V", "12 April 1990", "Smith"
    )
    assert res == {
        "verified": False, "mismatched_fields": ["mothers_maiden_name"],
    }, f"phonetic should reject 'Smith': {res}"

    # Phonetic — truncated STT ("Ja" should match "Jayasinghe" via prefix on code 'J').
    res = verify_identity(
        "1042-8837-9201", "901234567V", "12 April 1990", "Ja"
    )
    assert res == {"verified": True, "mismatched_fields": []}, f"phonetic 'Ja': {res}"

    # 8-digit account fallback: STT often drops one 4-digit group. Last 4 should still resolve.
    assert _normalize_account_no("88379201") == "9201", "8-digit fallback failed"
    assert lookup_account("88379201")["account_no"] == nimal_canonical, "8-digit lookup failed"
    # 11-digit hybrid (LLM splices corrected last-4 onto wrong base). Trailing 4 still resolves.
    assert _normalize_account_no("10428379201") == "9201", "11-digit fallback failed"
    assert lookup_account("10428379201")["account_no"] == nimal_canonical, "11-digit lookup failed"

    # Pronunciation variants for each stored maiden name should all verify.
    # Includes both registered single-word variants AND space-separated caller
    # dictation (handled by the phonetic Lev distance, not the variant list).
    variant_cases = [
        # (account, valid_dictation_of_maiden_name, label)
        ("1042-8837-9201", "Jayasingha",   "Jayasinghe → Jayasingha"),
        ("1042-8837-9201", "Jayasinha",    "Jayasinghe → Jayasinha"),
        ("1042-8837-9201", "Jaya Singha",  "Jayasinghe → 'Jaya Singha' (space split)"),
        ("2087-4412-6654", "Fernandus",    "Fernando → Fernandus"),
        ("3054-9960-1138", "Senevirathne", "Seneviratne → Senevirathne"),
        ("3054-9960-1138", "Seneviratna",  "Seneviratne → Seneviratna"),
        ("4901-2233-4477", "Rajapakse",    "Rajapaksa → Rajapakse"),
        ("4901-2233-4477", "Rajapaksha",   "Rajapaksa → Rajapaksha"),
        ("5673-8800-2295", "Gunawardhana", "Gunawardena → Gunawardhana"),
    ]
    for acc, given_name, label in variant_cases:
        rec = lookup_account(acc)
        assert rec is not None, f"{label}: account {acc} not found"
        v = rec["verification"]
        res = verify_identity(acc, v["nic"], v["dob"], given_name)
        assert res == {"verified": True, "mismatched_fields": []}, f"{label}: {res}"

    # Extra DOB sanity (not required by spec — fast-fails on regressions).
    assert _parse_dob("12 Apr 1990") == (1990, 4, 12)
    assert _parse_dob("12/04/1990") == (1990, 4, 12)
    assert _parse_dob("12th April 1990") == (1990, 4, 12)
    assert _parse_dob("April 12 1990") == (1990, 4, 12)

    print("All mock_db self-tests passed")
