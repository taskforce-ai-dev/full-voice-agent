"""Patch the Treehouse Post-Call Processor n8n workflow via API."""
import json
import urllib.request
import sys

N8N_BASE = "https://automation.taskforceai.tech"
API_KEY = sys.argv[1]
WORKFLOW_ID = "lGCsV0DYRtPNXfsd"

with open("/tmp/treehouse_wf.json") as f:
    wf = json.load(f)

nodes = wf["nodes"]
connections = wf["connections"]

def find_node(name):
    for n in nodes:
        if n["name"] == name:
            return n
    return None

# --- 1. Update OpenAI node ---
openai_node = find_node("OpenAI — Build Summary + Email")
if openai_node:
    msgs = openai_node["parameters"]["messages"]["values"]
    # Use webhook body directly instead of Google Sheets output
    msgs[1]["content"] = "={{ JSON.stringify($" + "('Webhook (from Voice Agent)').item.json.body) }}"

    sys_prompt = msgs[0]["content"]
    sys_prompt = sys_prompt.replace(
        'customer_phone: extract the phone number the customer GAVE during the call (from the transcript',
        'customer_phone: FIRST check if the input has a "guest_phone" field (the exact phone from the booking system). If present, use it as-is (digits only, no plus). Otherwise extract the phone number the customer GAVE during the call (from the transcript'
    )
    sys_prompt = sys_prompt.replace(
        'true ONLY if the call_outcome field is "booking_confirmed" AND a booking reference number is present in the transcript or summary.',
        'true ONLY if the call_outcome field is "booking_confirmed" AND a booking reference number is present (check "booking_reference" field first, then transcript/summary).'
    )
    msgs[0]["content"] = sys_prompt
    print("1. Updated OpenAI node to use webhook body + new field hints")

# --- 2. Enable continueOnFail on wa customer confirmation ---
wa_customer = find_node("wa customer confirmation")
if wa_customer:
    wa_customer["onError"] = "continueRegularOutput"
    print("2. Enabled continueOnFail on wa customer confirmation")

# --- 3. Update wa customer confirmation phone source ---
if wa_customer:
    for p in wa_customer["parameters"]["bodyParameters"]["parameters"]:
        if p["name"] == "to":
            p["value"] = "={{ $" + "('Webhook (from Voice Agent)').item.json.body.guest_phone || $" + "('Parse AI JSON').item.json.customer_phone }}"
            print("3. Updated customer WA phone to prefer payload guest_phone")
            break

# --- 4. Restructure: management summary always sends ---
if_conns = connections.get("IF Booking Confirmed", {}).get("main", [])
if len(if_conns) >= 1:
    true_branch = if_conns[0]
    if not any(c["node"] == "wa summary message" for c in true_branch):
        true_branch.append({"node": "wa summary message", "type": "main", "index": 0})
        print("4a. Added wa summary message to IF true branch (parallel)")

if "wa customer confirmation" in connections:
    del connections["wa customer confirmation"]
    print("4b. Removed wa customer confirmation -> Wait chain")

if "Wait" in connections:
    del connections["Wait"]
    print("4c. Removed Wait -> wa summary message chain")

nodes[:] = [n for n in nodes if n["name"] != "Wait"]
print("4d. Removed Wait node")

wa_summary = find_node("wa summary message")
if wa_summary:
    wa_summary["position"] = [2672, 1120]

# --- Push via API ---
update = {
    "name": wf["name"],
    "nodes": nodes,
    "connections": connections,
    "settings": wf.get("settings", {}),
    "staticData": wf.get("staticData"),
}

payload_json = json.dumps(update).encode("utf-8")
req = urllib.request.Request(
    N8N_BASE + "/api/v1/workflows/" + WORKFLOW_ID,
    data=payload_json,
    headers={"X-N8N-API-KEY": API_KEY, "Content-Type": "application/json"},
    method="PUT",
)

try:
    with urllib.request.urlopen(req, timeout=30) as resp:
        result = json.loads(resp.read())
        print("\nWorkflow updated! id=" + str(result.get("id")) + " active=" + str(result.get("active")))
except urllib.error.HTTPError as e:
    body = e.read().decode()
    print("ERROR: HTTP " + str(e.code) + ": " + body[:500])
    sys.exit(1)
