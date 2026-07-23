"""LIVE end-to-end: real tools.execute_tool -> booking_api -> yanolja_service -> LIVE PMS.
Creates one real reservation per property, then CANCELS both to leave no test data."""
import sys, asyncio, json
sys.path.insert(0, "/home/dev/full-voice-agent/Kavya")
import yanolja_service, yanolja_client, tools

VILLA = {"Deluxe Double Room","Deluxe Twin Room","Family Suite","Founders Suite"}
SUND  = {"Deluxe Double Room with Garden View","Deluxe Double Room with Sea View",
         "Deluxe Twin Room with Sea View","Beach Villa","Family Villa with Pool"}
CI, CO = "2026-09-14", "2026-09-16"
results, created = [], []

async def call(tool, **inp):
    return json.loads(await tools.execute_tool(tool, inp))

def show(label, ok):
    ok = bool(ok); results.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")

def names_of(r):
    return {x.get("room_type_name") or x.get("name")
            for x in (r.get("rooms") or r.get("available_rooms") or [])} - {None}

async def main():
    # 1/2. Per-property availability, live
    r = await call("check_availability", property="Mosvold Villa", check_in=CI, check_out=CO, num_adults=2)
    n = names_of(r); show(f"Villa availability -> {sorted(n)}", n and n <= VILLA)

    r = await call("check_availability", property="Sundara by Mosvold", check_in=CI, check_out=CO, num_adults=2)
    n = names_of(r); show(f"Sundara availability -> {sorted(n)}", n and n <= SUND)

    # 3. Cross-property substitution must NOT happen
    r = await call("check_availability", property="Mosvold Villa",
                   room_type="Deluxe Double Room with Sea View", check_in=CI, check_out=CO, num_adults=2)
    n = names_of(r)
    show(f"cross-property 'Sea View'@Villa not substituted (got {sorted(n) or 'error/none'})",
         "Deluxe Double Room" not in n)

    # 4. Abbreviation within a property
    r = await call("check_availability", property="Mosvold Villa",
                   room_type="deluxe double", check_in=CI, check_out=CO, num_adults=2)
    n = names_of(r); show(f"'deluxe double'@Villa -> {sorted(n) or r.get('error','?')}",
                          "Deluxe Double Room" in n)

    # 5/6. REAL bookings, one per property
    for prop, rt in (("Sundara by Mosvold", "Beach Villa"), ("Mosvold Villa", "Family Suite")):
        r = await call("create_booking", property=prop, room_type=rt, check_in=CI, check_out=CO,
                       guest_name="Verification Test", guest_email="verify@example.com",
                       guest_phone="+94770000009", num_adults=2)
        rid = r.get("booking_reference")
        if rid: created.append(rid)
        show(f"BOOK {rt!r} @ {prop} -> ref={rid} property={r.get('property')!r} "
             f"room={r.get('room_type')!r} {r.get('error') or ''}".strip(),
             r.get("success") and rid and r.get("property") == prop and r.get("room_type") == rt)

    # 7. Wrong-property room refused
    r = await call("create_booking", property="Sundara by Mosvold", room_type="Founders Suite",
                   check_in=CI, check_out=CO, guest_name="Verification Test")
    if r.get("booking_reference"): created.append(r["booking_reference"])
    show(f"Villa-only 'Founders Suite'@Sundara refused", bool(r.get("error")))

    # Cleanup: cancel everything this run created
    print("\n  -- cleanup --")
    for rid in created:
        try:
            await yanolja_service.cancel(str(rid))
            print(f"  cancelled reservation {rid}")
        except Exception as e:
            print(f"  WARN could not cancel {rid}: {e}")

    await yanolja_client.close_session()
    print(f"\n{'ALL PASS' if all(results) else 'FAILURES PRESENT'}  ({sum(results)}/{len(results)})")

asyncio.run(main())
