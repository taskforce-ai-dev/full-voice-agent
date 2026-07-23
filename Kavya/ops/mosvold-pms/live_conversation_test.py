"""Drive the LIVE deployed Kavya over ConversationRelay WS, as Twilio would."""
import asyncio, json, sys, websockets

URL = "wss://treehouse.taskforceai.tech/ws/conversation?lang=en"

async def ask(ws, text, label):
    await ws.send(json.dumps({"type": "prompt", "voicePrompt": text}))
    out = []
    try:
        while True:
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=75))
            if msg.get("type") == "text":
                out.append(msg.get("token", ""))
                if msg.get("last"):
                    reply = "".join(out).strip()
                    if reply:
                        print(f"\nGUEST: {text}\nKAVYA: {reply}\n" + "-" * 70)
                        return reply
                    out = []
    except asyncio.TimeoutError:
        print(f"\nGUEST: {text}\nKAVYA: <timeout>\n" + "-" * 70)
        return ""

async def main():
    async with websockets.connect(URL, open_timeout=30) as ws:
        await ws.send(json.dumps({"type": "setup", "callSid": "VERIFY-LIVE-001",
                                  "from": "+94770000000", "to": "+10000000000"}))
        replies = {}
        replies["hotel"] = await ask(ws, "Hi, which hotel am I calling?", "hotel")
        replies["props"] = await ask(ws, "What properties do you have and where are they?", "props")
        replies["rooms"] = await ask(ws, "What room types do you have at Sundara by Mosvold?", "rooms")
        replies["avail"] = await ask(ws,
            "Do you have a Beach Villa free at Sundara by Mosvold from the 14th of September to the 16th, for two adults?", "avail")
        replies["price"] = await ask(ws, "How much does it cost per night?", "price")

    blob = " ".join(replies.values()).lower()
    print("\n=== CHECKS ===")
    checks = [
        ("mentions Mosvold",            "mosvold" in blob),
        ("no Treehouse leakage",        "treehouse" not in blob and "belihuloya" not in blob),
        ("knows both properties",       "sundara" in blob and ("ahangama" in blob or "villa" in blob)),
        ("names Sundara room types",    "beach villa" in blob or "sea view" in blob or "garden view" in blob),
        ("no invented rates",           not any(t in blob for t in ("lkr", "usd", "$", "rupees", "per night is"))),
    ]
    for name, ok in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    print(f"\n{sum(1 for _, o in checks if o)}/{len(checks)} checks passed")

asyncio.run(main())
