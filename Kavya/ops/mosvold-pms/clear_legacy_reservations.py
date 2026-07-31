"""Cancel the leftover Treehouse-era reservations that still hold Mosvold rooms.

WHY: the room-type rename (rename_to_mosvold.sql) re-pointed the PMS's existing
Treehouse bookings at Mosvold room types. `reservations.room_type_id` is a
foreign key, so those old rows now occupy Mosvold rooms and block availability.
The visible symptom: Mosvold Villa reports 0/4 available on 2026-08-01..02
because #40/#45/#46/#47 hold all four rooms, and Beach Villa disappears from
Sundara's list because of #48.

WHAT IT DOES: cancels only reservations that are
  - created BEFORE the rename (2026-07-23 09:39 UTC), and
  - not already cancelled, and
  - still in the future (check-out on/after TODAY) — past stays block nothing,
    so they are deliberately left alone.

This is a STATUS CHANGE, not a delete: the rows survive with status
'cancelled', and a snapshot of the prior state is written to
/home/dev/backups/kavya-kb/ before anything is touched.

SAFETY GATES (the script aborts rather than guess):
  - reservations 61 and 62 (management's real Mosvold bookings) are asserted
    NOT to be in the target set
  - the target count must equal EXPECTED_COUNT; if the data has shifted since
    the audit, it stops so a human can re-check

Run:
  /home/dev/full-voice-agent/.venv/bin/python \
    /home/dev/full-voice-agent/Kavya/ops/mosvold-pms/clear_legacy_reservations.py

Rollback: the PMS exposes no un-cancel endpoint, so restore from the snapshot
by setting status back to 'confirmed' for those ids in MySQL, e.g.
  UPDATE reservations SET status='confirmed' WHERE id IN (...);
"""
import sys, asyncio, json, os
from datetime import datetime, date

sys.path.insert(0, "/home/dev/full-voice-agent/Kavya")
import yanolja_client, yanolja_service

CUTOFF = datetime.fromisoformat("2026-07-23T09:39:00+00:00")   # the rename
TODAY = date(2026, 7, 24)
PROTECT = {61, 62}          # management's real Mosvold bookings — never touch
EXPECTED_COUNT = 14         # from the audit; mismatch => stop and re-check
SNAPSHOT = "/home/dev/backups/kavya-kb/legacy-reservations-pre-cancel-20260724.json"


def d(s):
    return datetime.fromisoformat(str(s).replace("Z", "+00:00")).date()


async def main():
    rooms = await yanolja_client.list_rooms()
    rby = {r["id"]: r for r in rooms}
    res = await yanolja_client.list_reservations()

    targets = [
        x for x in res
        if datetime.fromisoformat(str(x["createdAt"]).replace("Z", "+00:00")) < CUTOFF
        and x.get("status") != "cancelled"
        and d(x["checkOut"]) >= TODAY
    ]
    ids = sorted(x["id"] for x in targets)
    print(f"Targets ({len(ids)}): {ids}")

    caught = PROTECT & set(ids)
    if caught:
        print(f"ABORT: real booking(s) {sorted(caught)} matched the legacy filter. Nothing cancelled.")
        return
    if len(ids) != EXPECTED_COUNT:
        print(f"ABORT: expected {EXPECTED_COUNT} targets, found {len(ids)}. "
              "Data has changed since the audit — re-check before cancelling.")
        return
    print("Safety gates passed (61/62 excluded, count matches audit).\n")

    os.makedirs(os.path.dirname(SNAPSHOT), exist_ok=True)
    json.dump(
        [{k: str(x.get(k)) for k in
          ("id", "status", "roomId", "roomTypeId", "checkIn", "checkOut", "guestId", "createdAt")}
         for x in targets],
        open(SNAPSHOT, "w"), indent=2)
    print(f"Snapshot written: {SNAPSHOT}\n")

    ok, fail = [], []
    for x in targets:
        rid = x["id"]
        rm = rby.get(x.get("roomId")) or {}
        try:
            r = await yanolja_service.cancel(str(rid))
            (ok if r.get("success") else fail).append(rid)
            state = "cancelled" if r.get("success") else str(r)[:80]
            print(f"  #{rid:<3} {str(x['checkIn'])[:10]}->{str(x['checkOut'])[:10]} "
                  f"{rm.get('roomNumber', '?'):<7} -> {state}")
        except Exception as exc:
            fail.append(rid)
            print(f"  #{rid:<3} FAILED: {exc}")

    print(f"\nCancelled {len(ok)}, failed {len(fail)} {fail or ''}")
    await yanolja_client.close_session()


asyncio.run(main())
