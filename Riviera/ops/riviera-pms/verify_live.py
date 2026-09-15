#!/usr/bin/env python3
"""Verify the live PMS matches Riya's Riviera Resort vocabulary.

Runs through Riya's OWN code path (yanolja_service), not raw SQL, so it proves
what the agent will actually see on a call — including the name matching and the
`_property_of` filter that silently drops unrecognised room types.

    cd Riviera && python ops/riviera-pms/verify_live.py

Needs YANOLJA_BASE_URL / YANOLJA_USERNAME / YANOLJA_PASSWORD in the environment
(or a .env in the Riviera folder). Read-only: lists rooms and derives
availability. Creates nothing.
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import yanolja_service as ys  # noqa: E402
from yanolja_client import list_rooms  # noqa: E402

FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILURES.append(label)


async def main() -> int:
    print(f"Riya vocabulary ({len(ys.ALL_ROOM_TYPES)} room types):")
    for n in ys.ALL_ROOM_TYPES:
        rates = ys.NIGHTLY_RATE_LKR.get(n, {})
        print(f"    {n:32}  —  LKR mid {rates.get('mid')} / high {rates.get('high')}")
    print()

    print("PMS /rooms:")
    rooms = await list_rooms()
    pms_names = sorted({(r.get("roomType") or {}).get("name", "") for r in rooms})
    for n in pms_names:
        owner = ys._property_of({"name": n})
        print(f"    {n!r:40} -> property {owner!r}")
    print()

    print("Checks:")
    check("PMS returned rooms", bool(rooms), f"{len(rooms)} rooms")

    missing = [n for n in ys.ALL_ROOM_TYPES if n not in pms_names]
    check("every Riviera room type exists in the PMS", not missing, f"missing: {missing}")

    unrecognised = [n for n in pms_names if n and not ys._property_of({"name": n})]
    check(
        "no PMS room type is invisible to Riya",
        not unrecognised,
        f"unrecognised (will be filtered out of availability): {unrecognised}",
    )

    # A Riviera PMS instance must never carry another property's inventory.
    foreign = [n for n in pms_names if any(
        w in n for w in ("Forest Escape", "Eco Harmony", "Sunrise Vista", "Mount Luxe",
                         "Mount Monarch", "Deluxe", "Founders", "Beach Villa", "Family Villa")
    )]
    check("no Hatton Hills / Mosvold room names present (wrong PMS instance?)",
          not foreign, f"found: {foreign}")

    foreign_rooms = [
        r.get("roomNumber") for r in rooms
        if str(r.get("roomNumber", "")).startswith(("HH-", "MV-", "SU-"))
    ]
    check("no HH-/MV-/SU- room numbers present", not foreign_rooms, f"found: {foreign_rooms}")

    # base_price in the PMS must equal the MID-season rate Riya quotes, or the
    # folio and the call disagree.
    mismatched = []
    for r in rooms:
        rt = r.get("roomType") or {}
        name = rt.get("name", "")
        want = ys.NIGHTLY_RATE_LKR.get(name, {}).get("mid")
        if want is None:
            continue
        try:
            got = int(float(rt.get("basePrice") or 0))
        except (TypeError, ValueError):
            got = -1
        if got != want:
            mismatched.append(f"{name}: PMS {got} != Riya mid-season {want}")
    check("PMS base_price matches Riya's mid-season rate", not mismatched,
          "; ".join(sorted(set(mismatched))))

    ci = (date.today() + timedelta(days=30)).isoformat()
    co = (date.today() + timedelta(days=32)).isoformat()
    print(f"\nAvailability {ci} -> {co} (2 nights, 2 adults):")
    avail = await ys.derive_availability(ci, co, num_adults=2, num_children=0)
    if avail.get("error"):
        check("availability succeeded", False, str(avail["error"]))
    else:
        entries = avail.get("rooms") or []
        for e in entries:
            flag = "free" if e.get("available") else "SOLD OUT"
            rate = e.get("rate_per_night_lkr")
            print(f"    {e.get('room_type_name'):32} {flag:9} LKR {rate}/night")
        check("availability returned room types", bool(entries), f"{len(entries)} types")

        names = {e.get("room_type_name", "") for e in entries}
        check(
            "availability lists exactly the seven Riviera room types",
            names == set(ys.ALL_ROOM_TYPES),
            f"missing: {sorted(set(ys.ALL_ROOM_TYPES) - names)}, "
            f"unexpected: {sorted(names - set(ys.ALL_ROOM_TYPES))}",
        )
        check(
            "at least one room type is free 30 days out",
            any(e.get("available") for e in entries),
            f"available_room_types={avail.get('available_room_types')}",
        )
        season = ys.season_for_stay(date.fromisoformat(ci), date.fromisoformat(co))
        if season in ("mid", "high"):
            rated = [e for e in entries if e.get("rate_per_night_lkr")]
            check("every room type carries a nightly rate",
                  bool(entries) and len(rated) == len(entries),
                  f"{len(rated)}/{len(entries)} rated — is RATES_ENABLED set?")
        else:
            print(f"    (stay is {season!r}: no nightly rate expected — reservations confirms)")

        blob = repr(avail).lower()
        leaks = [w for w in ("hatton", "mosvold", "sundara", "balapitiya", "ahangama", "deluxe")
                 if w in blob]
        check("no stale-brand string in the payload", not leaks, f"leaked: {leaks}")

    print()
    if FAILURES:
        print(f"FAILED ({len(FAILURES)}): " + "; ".join(FAILURES))
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
