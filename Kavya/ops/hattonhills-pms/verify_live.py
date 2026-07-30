#!/usr/bin/env python3
"""Verify the live PMS matches Kavya's Hatton Hills vocabulary.

Runs through Kavya's OWN code path (yanolja_service), not raw SQL, so it proves
what the agent will actually see on a call — including the name matching and the
`_property_of` filter that silently drops unrecognised room types.

    /home/dev/full-voice-agent/.venv/bin/python ops/hattonhills-pms/verify_live.py

Read-only: lists rooms and derives availability. Creates nothing.
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
    print(f"Kavya vocabulary ({len(ys.ALL_ROOM_TYPES)} room types):")
    for n in ys.ALL_ROOM_TYPES:
        print(f"    {n}  —  USD {ys.DEMO_NIGHTLY_RATE_USD.get(n)}")
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
    check("every Kavya room type exists in the PMS", not missing, f"missing: {missing}")

    unrecognised = [n for n in pms_names if n and not ys._property_of({"name": n})]
    check(
        "no PMS room type is invisible to Kavya",
        not unrecognised,
        f"unrecognised (will be filtered out of availability): {unrecognised}",
    )

    stale = [n for n in pms_names if any(
        w in n for w in ("Deluxe", "Founders", "Beach Villa", "Family Villa")
    )]
    check("no Mosvold room names remain", not stale, f"found: {stale}")

    stale_rooms = [
        r.get("roomNumber") for r in rooms
        if str(r.get("roomNumber", "")).startswith(("MV-", "SU-"))
    ]
    check("no MV-/SU- room numbers remain", not stale_rooms, f"found: {stale_rooms}")

    # base_price in the PMS must equal the rate Kavya quotes, or the folio and the
    # call disagree.
    mismatched = []
    for r in rooms:
        rt = r.get("roomType") or {}
        name = rt.get("name", "")
        want = ys.DEMO_NIGHTLY_RATE_USD.get(name)
        if want is None:
            continue
        try:
            got = int(float(rt.get("basePrice") or 0))
        except (TypeError, ValueError):
            got = -1
        if got != want:
            mismatched.append(f"{name}: PMS {got} != Kavya {want}")
    check("PMS base_price matches Kavya's quoted rate", not mismatched,
          "; ".join(sorted(set(mismatched))))

    ci = (date.today() + timedelta(days=30)).isoformat()
    co = (date.today() + timedelta(days=32)).isoformat()
    print(f"\nAvailability {ci} -> {co} (2 nights, 2 adults):")
    avail = await ys.derive_availability(ci, co, num_adults=2, num_children=0)
    if avail.get("error"):
        check("availability succeeded", False, str(avail["error"]))
    else:
        # derive_availability returns EVERY scoped room type, each with an
        # `available` flag — not just the free ones.
        entries = avail.get("rooms") or []
        for e in entries:
            flag = "free" if e.get("available") else "SOLD OUT"
            rate = e.get("rate_per_night_usd")
            print(f"    {e.get('room_type_name'):32} {flag:9} USD {rate}/night")
        check("availability returned room types", bool(entries), f"{len(entries)} types")

        names = {e.get("room_type_name", "") for e in entries}
        check(
            "availability lists exactly the five Hatton Hills room types",
            names == set(ys.ALL_ROOM_TYPES),
            f"missing: {sorted(set(ys.ALL_ROOM_TYPES) - names)}, "
            f"unexpected: {sorted(names - set(ys.ALL_ROOM_TYPES))}",
        )
        check(
            "at least one room type is free 30 days out",
            any(e.get("available") for e in entries),
            f"available_room_types={avail.get('available_room_types')}",
        )
        rated = [e for e in entries if e.get("rate_per_night_usd")]
        check("every room type carries a nightly rate",
              bool(entries) and len(rated) == len(entries),
              f"{len(rated)}/{len(entries)} rated — is DEMO_RATES_ENABLED set?")

        blob = repr(avail).lower()
        leaks = [w for w in ("mosvold", "sundara", "balapitiya", "ahangama", "deluxe")
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
