"""Tests for Flico post_call payload mapping + transcript formatting.

Run in the container (has httpx):  docker exec -i flico-voice-agent python - < test_postcall_payload.py
Exit 0 = all pass, 1 = failures.
"""
import sys

import post_call as pc

PASS = 0
FAIL = 0
FAILS = []


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        FAILS.append(f"{name} :: {detail}")
        print(f"  FAIL  {name} -- {detail}")


# --- exact webhook schema keys ---------------------------------------------
EXPECTED_KEYS = {
    "timestamp", "call_end_time", "call_id", "property_id", "caller_name",
    "caller_phone", "intent", "property_type", "preferred_area", "bedrooms",
    "budget_lkr", "lead_captured", "summary", "next_step", "transcript",
}

print("[_build_payload schema]")
extracted = {
    "caller_name": "Chris Fernando",
    "caller_phone": "0765603946",
    "intent": "Rent",
    "property_type": "apartment",
    "preferred_area": "Colombo 2",
    "bedrooms": 3,
    "budget_lkr": "Rs 200,000",
    "lead_captured": "Yes",
    "next_step": "Consultant to WhatsApp details and arrange viewing.",
    "summary": "Caller wanted a Colombo 2 apartment.",
    "property_id": "p26",
}
p = pc._build_payload("CA123", "94776697566", extracted, "Guest: hi\nAgent: hello",
                      "2026-06-24T10:28:00.000000", "2026-06-24T10:32:00.000000")
check("payload has exactly the expected keys", set(p.keys()) == EXPECTED_KEYS,
      f"got {sorted(p.keys())}")
check("call_id mapped", p["call_id"] == "CA123", p["call_id"])
check("transcript included", p["transcript"] == "Guest: hi\nAgent: hello", repr(p["transcript"]))
check("timestamp passed through", p["timestamp"] == "2026-06-24T10:28:00.000000", repr(p["timestamp"]))
check("call_end_time passed through", p["call_end_time"] == "2026-06-24T10:32:00.000000", repr(p["call_end_time"]))
check("budget_lkr is STRING '200000'", p["budget_lkr"] == "200000", repr(p["budget_lkr"]))
check("bedrooms coerced to str '3'", p["bedrooms"] == "3", repr(p["bedrooms"]))
check("caller_phone normalized to +94", p["caller_phone"] == "+94765603946", p["caller_phone"])
check("intent lowercased", p["intent"] == "rent", repr(p["intent"]))
check("lead_captured -> 'true'", p["lead_captured"] == "true", repr(p["lead_captured"]))
check("property_type lowercase", p["property_type"] == "apartment", repr(p["property_type"]))
check("property_id normalized to uppercase P-code", p["property_id"] == "P26", repr(p["property_id"]))

# fallback phone + defaults when fields missing
p2 = pc._build_payload("CA999", "0770000000", {})
check("caller_phone fallback normalized", p2["caller_phone"] == "+94770000000", p2["caller_phone"])
check("lead_captured defaults to 'false'", p2["lead_captured"] == "false", p2["lead_captured"])
check("missing budget -> None", p2["budget_lkr"] is None, repr(p2["budget_lkr"]))
check("missing bedrooms -> None", p2["bedrooms"] is None, repr(p2["bedrooms"]))
check("missing intent -> None", p2["intent"] is None, repr(p2["intent"]))
check("missing property_id -> None", p2["property_id"] is None, repr(p2["property_id"]))
check("empty extracted still has all keys", set(p2.keys()) == EXPECTED_KEYS, sorted(p2.keys()))

# property_id validation
check("invalid property_id rejected", pc._build_payload("C", "0", {"property_id": "banana"})["property_id"] is None)
check("hallucinated non-code rejected", pc._build_payload("C", "0", {"property_id": "the colombo 3 one"})["property_id"] is None)

# catalog loads from KB (Ref tags must be present)
cat = pc._load_catalog()
check("catalog loaded with P-codes", ("P15" in cat and "P01" in cat), f"len={len(cat)}")

# phone normalization variants
check("phone +already kept", pc._normalize_phone("+94771234567") == "+94771234567")
check("phone 94-prefixed", pc._normalize_phone("94771234567") == "+94771234567")
check("phone with spaces", pc._normalize_phone("071 651 7638") == "+94716517638")

# budget coercion
check("int budget preserved", pc._coerce_budget(150000) == 150000)
check("lakhs-as-words handled via digits", pc._coerce_budget("200,000") == 200000)

# --- transcript formatting --------------------------------------------------
print("\n[_format_transcript]")
# ConversationRelay shape (text key)
cr = [
    {"role": "user", "text": "Have you got any apartments?"},
    {"role": "assistant", "text": "Yes, in Colombo 2 and 3."},
]
out = pc._format_transcript(cr)
check("CR formats Guest/Agent", out == "Guest: Have you got any apartments?\nAgent: Yes, in Colombo 2 and 3.", repr(out))

# Media Streams shape (content key) with KB prefix on user turn
ms = [
    {"role": "user", "content": "[Reference context: some listing]\n\nGuest: anything in Colombo 3?"},
    {"role": "assistant", "content": "We have a 3-bedroom apartment in Colombo 3."},
]
out2 = pc._format_transcript(ms)
check("MS strips KB prefix", "Guest: anything in Colombo 3?" in out2, repr(out2))
check("MS no leaked reference context", "[Reference context" not in out2, repr(out2))
check("MS agent line present", "Agent: We have a 3-bedroom apartment in Colombo 3." in out2, repr(out2))

print(f"\n==== {PASS} passed, {FAIL} failed ====")
if FAIL:
    for f in FAILS:
        print("  -", f)
sys.exit(1 if FAIL else 0)
