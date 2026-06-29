"""Quick end-to-end smoke test for the Yanolja booking flow.

Runs inside the container against the live PMS — no Twilio, no LLM.
Exercises the same booking_api.* surface the LLM tools call.
"""
import asyncio
import json
from datetime import date, timedelta

from booking_api import (
    check_availability,
    create_booking,
    retrieve_booking,
    cancel_booking,
    close_session,
    is_configured,
)


def dump(label, obj):
    print(f"\n=== {label} ===")
    print(json.dumps(obj, indent=2, default=str))


async def main():
    print(f"is_configured(): {is_configured()}")

    # Use a 1-night stay 3 weeks out so we don't collide with real demos.
    ci = (date.today() + timedelta(days=21)).isoformat()
    co = (date.today() + timedelta(days=22)).isoformat()

    # 1) Broad availability scan — no filter
    avail = await check_availability(
        check_in=ci,
        check_out=co,
        num_adults=2,
        num_children=0,
    )
    dump(f"AVAIL (any room, {ci} → {co})", avail)

    # 2) Filtered availability — Mount Luxe with 3 adults + 1 child
    avail_luxe = await check_availability(
        check_in=ci,
        check_out=co,
        room_type="Mount Luxe",
        num_adults=3,
        num_children=1,
    )
    dump("AVAIL (Mount Luxe, 3A+1C — was failing before the cap fix)", avail_luxe)

    # Pick a room type that came back available
    target_type = None
    for r in avail.get("rooms", []):
        if r.get("available"):
            target_type = r["room_type_name"]
            break
    if not target_type:
        print("\nNo available room type — aborting before booking.")
        await close_session()
        return

    # 3) Create booking
    booking = await create_booking(
        check_in=ci,
        check_out=co,
        room_type=target_type,
        guest_name="Kavya Smoketest",
        guest_email="smoketest@example.com",
        guest_phone="+94770000000",
        num_adults=2,
        num_children=0,
    )
    dump(f"BOOK ({target_type})", booking)

    ref = booking.get("booking_reference")
    if not ref:
        print("\nBooking failed — skipping retrieve/cancel.")
        await close_session()
        return

    # 4) Retrieve
    got = await retrieve_booking(reservation_no=str(ref))
    dump(f"RETRIEVE ({ref})", got)

    # 5) Cancel — clean up so we don't pollute the PMS
    cancelled = await cancel_booking(str(ref))
    dump(f"CANCEL ({ref})", cancelled)

    await close_session()


if __name__ == "__main__":
    asyncio.run(main())
