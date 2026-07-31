"""Manual smoke test for dashboard_client.

Run from the Kavya/ directory:

    python test_dashboard_client.py

With env vars unset, prints "skipped" and exits 0 without any network call.
With env vars set, POSTs a fake call.started + call.completed to the live
dashboard for call_sid="local-test-001".
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
)

import dashboard_client


FAKE_CALL_SID = "local-test-001"
FAKE_PHONE = "+94770000000"
FAKE_LANG = "en"
FAKE_STARTED = "2026-05-23T05:00:00Z"
FAKE_ENDED = "2026-05-23T05:00:42Z"
FAKE_DURATION = 42

FAKE_TRANSCRIPT = [
    {"role": "assistant", "text": "Hello, this is Kavya from Mosvold Boutique Hotels."},
    {"role": "user", "text": "Hi, I want to check availability for next weekend."},
    {"role": "assistant", "text": "Sure, let me check that for you."},
]

FAKE_EXTRACTED = {
    "guest_name": "Test Guest",
    "num_guests": "2",
    "check_in": "2026-05-30",
    "check_out": "2026-06-01",
    "room_preference": "Founders Suite",
    "availability_result": "Available",
    "call_outcome": "booking_inquiry",
    "follow_up_needed": "No",
    "summary": "Guest inquired about availability at Mosvold Villa for next weekend.",
}


async def main() -> int:
    if not dashboard_client._ENABLED:
        print("skipped — no env set (DASHBOARD_API_URL / DASHBOARD_API_KEY / DASHBOARD_AGENT_ID)")
        return 0

    print(f"Sending call.started for {FAKE_CALL_SID} → {os.getenv('DASHBOARD_API_URL')}")
    await dashboard_client.send_call_started(
        call_sid=FAKE_CALL_SID,
        caller_phone=FAKE_PHONE,
        lang=FAKE_LANG,
        started_at_iso=FAKE_STARTED,
    )

    await asyncio.sleep(1)

    print(f"Sending call.completed for {FAKE_CALL_SID}")
    await dashboard_client.send_call_completed(
        call_sid=FAKE_CALL_SID,
        caller_phone=FAKE_PHONE,
        lang=FAKE_LANG,
        started_at_iso=FAKE_STARTED,
        ended_at_iso=FAKE_ENDED,
        duration_sec=FAKE_DURATION,
        full_transcript=FAKE_TRANSCRIPT,
        extracted=FAKE_EXTRACTED,
    )

    await dashboard_client.send_call_transferred(
        call_sid="local-test-001",
        caller_phone="+94711754668",
        reason="caller wants to discuss special booking",
        human_phone="+94711754668",
    )
    print("send_call_transferred returned")

    print("done")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
