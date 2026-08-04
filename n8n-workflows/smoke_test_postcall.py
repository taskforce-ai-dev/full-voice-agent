#!/usr/bin/env python3
"""Smoke-test the Treehouse Post-Call Processor n8n workflow end-to-end.

Fires the post-call webhook once per CASE and, for each, polls the n8n API and
asserts the "wa customer confirmation" node normalised the phone correctly and
sent to the expected WhatsApp JID.

The `to` expression in that node mirrors `Kavya/handover.py::normalize_whatsapp`
(expand spoken "double"/"triple" -> strip to digits -> "00" intl access code ->
"0"-trunk -> "94" -> 9-digit NSN -> international). These cases exercise the
three paths that used to break, and every one is arranged to resolve to the SAME
real test WhatsApp number (94711754668) so the sends actually deliver instead of
422-ing on a non-existent number:

  1. local, dictated in the transcript ("0711 754 668")      -> extraction path
  2. spoken "double" shorthand, via guest_phone
     ("0711 754 double 6 8")                                  -> to-expr expands
  3. international 00 access code, via guest_phone
     ("0094711754668")                                        -> to-expr slices 00

Case 1 also asserts the OpenAI node copied the digits verbatim (no reformatting).
Cases 2 and 3 pass the number in `guest_phone`, which the `to` expression reads
before `customer_phone`, so they test the normalisation deterministically without
depending on what the extraction model does with the words.

Run after ANY edit to the workflow:

    python3 n8n-workflows/smoke_test_postcall.py

NOTE: a successful run delivers THREE real WhatsApp messages to the test number
(94711754668) and three manager summaries (one per case). Exit code 0 = all pass.

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

EXPECTED_JID = "94711754668@s.whatsapp.net"  # every case resolves here

# label, guest_phone (None => force the extraction path), spoken number in the
# transcript, expected verbatim extraction (None => skip, guest_phone is used).
CASES = [
    {
        "label": "local number dictated in transcript",
        "guest_phone": None,
        "spoken": "0711 754 668",
        "expect_extracted": "0711754668",
    },
    {
        "label": "spoken 'double' shorthand (via guest_phone)",
        "guest_phone": "0711 754 double 6 8",
        "spoken": "0711 754 668",
        "expect_extracted": None,
    },
    {
        "label": "international 00 access code (via guest_phone)",
        "guest_phone": "0094711754668",
        "spoken": "0711 754 668",
        "expect_extracted": None,
    },
]


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


def fire(call_sid: str, case: dict) -> None:
    now = datetime.now(timezone.utc).isoformat()
    payload = {
        "timestamp": now,
        "call_end_time": now,
        "call_sid": call_sid,
        "language": "English",
        "caller_phone": "+94776697566",
        "transcript": (
            "Guest: Smoke test booking please. My WhatsApp number is "
            f"{case['spoken']}.\n"
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
    if case["guest_phone"] is not None:
        payload["guest_phone"] = case["guest_phone"]
    req = urllib.request.Request(
        f"{N8N_BASE}{WEBHOOK_PATH}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        resp.read()


def wait_for_execution(key: str, call_sid: str) -> dict | None:
    for _ in range(12):
        time.sleep(5)
        latest = api_get(
            key, f"/api/v1/executions?workflowId={WORKFLOW_ID}&limit=5")
        for entry in latest["data"]:
            detail = api_get(
                key, f"/api/v1/executions/{entry['id']}?includeData=true")
            run = detail["data"]["resultData"]["runData"]
            hook = run.get("Webhook (from Voice Agent)")
            if not hook:
                continue
            body = hook[0]["data"]["main"][0][0]["json"]["body"]
            if body.get("call_sid") == call_sid and entry["status"] != "running":
                return detail
    return None


def check(execution: dict, case: dict) -> list[str]:
    run = execution["data"]["resultData"]["runData"]
    failures = []

    if execution.get("status") != "success":
        failures.append(f"execution status = {execution.get('status')}")

    if case["expect_extracted"] is not None:
        parsed = run.get("Parse AI JSON")
        phone = (parsed[0]["data"]["main"][0][0]["json"].get("customer_phone")
                 if parsed else None)
        if phone != case["expect_extracted"]:
            failures.append(
                f"extraction reformatted the phone: {phone!r} "
                f"(expected {case['expect_extracted']!r})")

    wa = run.get("wa customer confirmation")
    wa_out = wa[0]["data"]["main"][0][0]["json"] if wa else {}
    if not wa_out.get("success"):
        failures.append(
            f"wa customer confirmation failed: {json.dumps(wa_out)[:300]}")
    elif wa_out["data"].get("jid") != EXPECTED_JID:
        failures.append(f"sent to wrong JID: {wa_out['data'].get('jid')!r}")

    return failures


def main() -> None:
    key = api_key()
    stamp = f"{datetime.now(timezone.utc):%Y%m%d%H%M%S}"
    any_failed = False

    for i, case in enumerate(CASES):
        call_sid = f"CA_SMOKE_{stamp}_{i}"
        print(f"\n[{i + 1}/{len(CASES)}] {case['label']}")
        fire(call_sid, case)
        print(f"  webhook fired ({call_sid}); waiting for execution ...")
        execution = wait_for_execution(key, call_sid)
        if not execution:
            print("  FAIL: execution never completed")
            any_failed = True
            continue
        failures = check(execution, case)
        if failures:
            any_failed = True
            print(f"  FAIL (execution {execution['id']}):")
            for f in failures:
                print("    -", f)
        else:
            print(f"  PASS (execution {execution['id']}): "
                  f"sent to {EXPECTED_JID}")

    if any_failed:
        sys.exit(1)
    print(f"\nAll {len(CASES)} cases passed.")


if __name__ == "__main__":
    main()
