"""Patch the Treehouse Post-Call Processor n8n workflow.

Changes:
1. OpenAI prompt: reference original webhook body fields, use guest_phone/booking_reference from payload
2. wa customer confirmation: use guest_phone from payload (fallback to AI-extracted), enable continueOnFail
3. Management summary: always sends (from both IF branches, not chained after customer WA)
4. Remove the Wait node and sequential chain
"""
import json
import sys
import urllib.request

N8N_BASE = "https://automation.taskforceai.tech"
API_KEY = sys.argv[1]
WORKFLOW_ID = "lGCsV0DYRtPNXfsd"

with open("/tmp/treehouse_wf.json") as f:
    wf = json.load(f)

nodes = wf["nodes"]
connections = wf["connections"]

# --- Helper to find node by name ---
def find_node(name):
    for n in nodes:
        if n["name"] == name:
            return n
    return None

# ============================================================
# 1. Update OpenAI prompt to reference webhook body directly
#    and tell it about guest_phone/booking_reference fields
# ============================================================
openai_node = find_node("OpenAI — Build Summary + Email")
if openai_node:
    msgs = openai_node["parameters"]["messages"]["values"]
    # Update the user message to reference webhook body directly
    msgs[1]["content"] = "={{ JSON.stringify($('Webhook (from Voice Agent)').item.json.body) }}"

    # Update system prompt to mention new fields
    sys_prompt = msgs[0]["content"]
    old_phone_rule = "   - customer_phone: extract the phone number the customer GAVE during the call (from the transcript — Kavya typically confirms it as \"plus nine four ...\"). Return digits only, no plus sign, no spaces, no dashes. Include the country code. Example: \"94765603946\". If no phone was captured in the transcript, fall back to the caller_phone field (strip the + and any spaces)."
    new_phone_rule = "   - customer_phone: FIRST check if the input has a \"guest_phone\" field (this is the exact phone number from the booking system). If present, use it as-is (digits only, no plus sign). If not present, extract the phone number the customer GAVE during the call from the transcript. Return digits only, no plus sign, no spaces, no dashes. Include the country code. Example: \"94765603946\". If neither is available, fall back to the caller_phone field (strip the + and any spaces)."
    if old_phone_rule in sys_prompt:
        sys_prompt = sys_prompt.replace(old_phone_rule, new_phone_rule)
        print("Updated phone extraction rule in OpenAI prompt")

    # Add note about booking_reference field
    old_ref_note = "   - true ONLY if the call_outcome field is \"booking_confirmed\" AND a booking reference number is present in the transcript or summary."
    new_ref_note = "   - true ONLY if the call_outcome field is \"booking_confirmed\" AND a booking reference number is present in the input (check \"booking_reference\" field first, then transcript/summary)."
    if old_ref_note in sys_prompt:
        sys_prompt = sys_prompt.replace(old_ref_note, new_ref_note)
        print("Updated booking_reference rule in OpenAI prompt")

    msgs[0]["content"] = sys_prompt
    print("Updated OpenAI node to use webhook body directly")

# ============================================================
# 2. Enable "Continue on Fail" on wa customer confirmation
# ============================================================
wa_customer = find_node("wa customer confirmation")
if wa_customer:
    wa_customer["onError"] = "continueRegularOutput"
    print("Enabled continueOnFail on wa customer confirmation")

# ============================================================
# 3. Restructure connections so management summary always sends
#
# New flow:
#   IF Booking Confirmed [true]  -> wa customer confirmation
#                                -> wa summary message (in parallel)
#   IF Booking Confirmed [false] -> wa summary message1
#
# Remove: wa customer confirmation -> Wait -> wa summary message
# ============================================================

# Update IF Booking Confirmed true branch: add wa summary message in parallel
if_node_conns = connections.get("IF Booking Confirmed", {}).get("main", [])
if len(if_node_conns) >= 1:
    true_branch = if_node_conns[0]
    # Add wa summary message to the true branch (parallel with wa customer confirmation)
    already_has_summary = any(c["node"] == "wa summary message" for c in true_branch)
    if not already_has_summary:
        true_branch.append({
            "node": "wa summary message",
            "type": "main",
            "index": 0
        })
        print("Added wa summary message to IF true branch (parallel)")

# Remove wa customer confirmation -> Wait connection
if "wa customer confirmation" in connections:
    del connections["wa customer confirmation"]
    print("Removed wa customer confirmation -> Wait connection")

# Remove Wait -> wa summary message connection
if "Wait" in connections:
    del connections["Wait"]
    print("Removed Wait -> wa summary message connection")

# Remove the Wait node entirely
nodes[:] = [n for n in nodes if n["name"] != "Wait"]
print("Removed Wait node")

# Move wa summary message position to be next to wa customer confirmation
wa_summary = find_node("wa summary message")
if wa_summary:
    wa_summary["position"] = [2672, 1120]  # Above wa customer confirmation

# ============================================================
# 4. Update wa customer confirmation to prefer guest_phone from payload
# ============================================================
if wa_customer:
    body_params = wa_customer["parameters"]["bodyParameters"]["parameters"]
    for p in body_params:
        if p["name"] == "to":
            # Use guest_phone from webhook body if available, else AI-extracted
            p["value"] = "={{ $('Webhook (from Voice Agent)').item.json.body.guest_phone || $('Parse AI JSON').item.json.customer_phone }}"
            print("Updated wa customer confirmation to prefer guest_phone from payload")
            break

# ============================================================
# Save and push via API
# ============================================================

# Build the update payload (only nodes and connections)
update_payload = {
    "nodes": wf["nodes"],
    "connections": wf["connections"],
}

payload_json = json.dumps(update_payload).encode("utf-8")

req = urllib.request.Request(
    f"{N8N_BASE}/api/v1/workflows/{WORKFLOW_ID}",
    data=payload_json,
    headers={
        "X-N8N-API-KEY": API_KEY,
        "Content-Type": "application/json",
    },
    method="PATCH",
)

try:
    with urllib.request.urlopen(req, timeout=30) as resp:
        result = json.loads(resp.read())
        print(f"\nWorkflow updated successfully! (id={result.get('id')}, active={result.get('active')})")
except urllib.error.HTTPError as e:
    body = e.read().decode()
    print(f"ERROR: HTTP {e.code}: {body[:500]}")
    sys.exit(1)
