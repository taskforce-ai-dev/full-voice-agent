#!/usr/bin/env python3
"""Smoke-test the Treehouse Post-Call Processor n8n workflow end-to-end.

Fires the post-call webhook with a booking-confirmed payload whose transcript
contains a LOCAL-format Sri Lankan phone number (0711 754 668) — the exact
shape that caused the July 2026 WhatsApp JID 422 regression — then polls the
n8n API and asserts:

  1. the execution succeeded,
  2. the OpenAI node copied the phone digits verbatim (no reformatting),
  3. the "wa customer confirmation" node sent successfully to the
     normalized JID 94711754668@s.whatsapp.net.

Run after ANY edit to the workflow:

    python3 n8n-workflows/smoke_test_postcall.py

NOTE: a successful run delivers one real WhatsApp message to the test number
(94711754668) and one summary to the manager number. Exit code 0 = pass.

Reads N8N_API_KEY from .env.secrets at the repo root.
"""
import json
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

N8N_BASE = "https://automation.taskforceai.tech"
WORKFLOW_ID = "lGCsV0DYRtPNXfsd"
WEBHOOK_PATH = "/webhook/treehouse-call-webhook"
TEST_NUMBER_LOCAL_SPOKEN = "0711 754 668"   # as the guest says it
TEST_NUMBER_DIGITS = "0711754668"           # what extraction must return
EXPECTED_JID = "94711754668@s.whatsapp.net"  # what the node must send to


def api_key() -> str:
    secrets = Path(__file__).resolve().parent.parent / ".env.secrets"
    for line in secrets.read_text().splitlines():
        if line.startswith("N8N_API_KEY="):
            return line.split("=", 1)[1].strip()
    sys.exit("N8N_API_KEY not found in .env.secrets")


def api_get(key: str, path: str) -> dict:
    req = urllib.request.Request(
        f"{N8N_BASE}{path}", headers={"X-N8N-API-KEY": key})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def main() -> None:
    key = api_key()
    call_sid = f"CA_SMOKE_{datetime.now(timezone.utc):%Y%m%d%H%M%S}"
    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "call_end_time": datetime.now(timezone.utc).isoformat(),
        "call_sid": call_sid,
        "language": "English",
        "caller_phone": "+94776697566",
        "transcript": (
            "Guest: Smoke test booking please. My WhatsApp number is "
            f"{TEST_NUMBER_LOCAL_SPOKEN}.\n"
            "Kavya: Confirmed. Reference number SMOKE one two three.\n"
            "Guest: Thank you."
        ),
        "guest_name": "Smoke Test",
        "num_guests": "2 adults",
        "check_in": "2026-08-10",
        "check_out": "2026-08-12",
        "room_preference": "Eco Harmony",
        "availability_result": "Eco Harmony available at 52,580 rupees",
        "call_outcome": "booking_confirmed",
        "follow_up_needed": "No",
        "summary": "Automated smoke test of the post-call processor.",
    }
    req = urllib.request.Request(
        f"{N8N_BASE}{WEBHOOK_PATH}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        resp.read()
    print(f"webhook fired ({call_sid}); waiting for execution ...")

    execution = None
    for _ in range(12):
        time.sleep(5)
        latest = api_get(
            key, f"/api/v1/executions?workflowId={WORKFLOW_ID}&limit=3")
        for entry in latest["data"]:
            detail = api_get(
                key, f"/api/v1/executions/{entry['id']}?includeData=true")
            run = detail["data"]["resultData"]["runData"]
            hook = run.get("Webhook (from Voice Agent)")
            if not hook:
                continue
            body = hook[0]["data"]["main"][0][0]["json"]["body"]
            if body.get("call_sid") == call_sid and entry["status"] != "running":
                execution = detail
                break
        if execution:
            break
    if not execution:
        sys.exit("FAIL: smoke-test execution never completed")

    run = execution["data"]["resultData"]["runData"]
    failures = []

    if execution.get("status") != "success":
        failures.append(f"execution status = {execution.get('status')}")

    parsed = run.get("Parse AI JSON")
    phone = (parsed[0]["data"]["main"][0][0]["json"].get("customer_phone")
             if parsed else None)
    if phone != TEST_NUMBER_DIGITS:
        failures.append(
            f"extraction reformatted the phone: {phone!r} "
            f"(expected {TEST_NUMBER_DIGITS!r})")

    wa = run.get("wa customer confirmation")
    wa_out = wa[0]["data"]["main"][0][0]["json"] if wa else {}
    if not wa_out.get("success"):
        failures.append(f"wa customer confirmation failed: "
                        f"{json.dumps(wa_out)[:300]}")
    elif wa_out["data"].get("jid") != EXPECTED_JID:
        failures.append(f"sent to wrong JID: {wa_out['data'].get('jid')!r}")

    if failures:
        print("FAIL (execution", execution["id"], ")")
        for f in failures:
            print("  -", f)
        sys.exit(1)
    print(f"PASS (execution {execution['id']}): phone extracted verbatim, "
          f"WhatsApp sent to {EXPECTED_JID}")


if __name__ == "__main__":
    main()
