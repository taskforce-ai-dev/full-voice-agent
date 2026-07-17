"""End-to-end answer accuracy eval — QA, NOT proof.

The KB test suite proves what Fiona is HANDED: a correct, complete, self-labelling
context. It cannot prove what she SAYS. An LLM can still misread a price or ignore
a NOTE. This measures that, and it is monitoring, not a guarantee. Never quote its
score as if it were the exhaustive proof.

Replicates the production call shape exactly (server.py ~2483):
    system  = _build_system_prompt(lang)
    user    = "[Reference context: {retrieve_context(text, sticky)}]\n\nGuest: {text}"

Each scenario is graded twice:
  * mechanically  — invented ref codes, invented zones, type/bedroom claims that
                    contradict the retrieved set. Deterministic, no judge needed.
  * by a judge    — an independent Opus pass that only sees the context and the
                    reply and hunts for contradictions.

Run inside the flico container (it has the API key and the model):
    docker exec flico-voice-agent python evals/answer_eval.py
"""
import json
import os
import random
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import anthropic  # noqa: E402

import knowledge_base  # noqa: E402
from knowledge_base import retrieve_context  # noqa: E402

# Judge preference order. A stronger, independent judge is better, but the API
# returns 529 under load -- an eval that silently drops scenarios on a transient
# error reports a fake score, so retry hard and fall back rather than skip.
JUDGE_MODELS = ["claude-opus-4-8", "claude-sonnet-4-6"]
AGENT_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")


def _call(client, model, **kw):
    last = None
    for attempt in range(6):
        try:
            return client.messages.create(model=model, **kw)
        except Exception as exc:  # 429/500/529 are all transient here
            last = exc
            time.sleep(min(2 ** attempt + random.random(), 30))
    raise last


def _judge_call(client, **kw):
    last = None
    for model in JUDGE_MODELS:
        try:
            return _call(client, model, **kw)
        except Exception as exc:
            last = exc
    raise last

# (id, turns, what a correct answer must respect)
SCENARIOS = [
    ("1br_apartment", ["I'm looking for a one bedroom apartment"],
     "Must offer only 1-bedroom apartments (P51, P52 or P53). Must not offer a "
     "2-bedroom or a house as if it were a 1-bedroom apartment."),
    ("2br_house_c7", ["Do you have a two bedroom house in Colombo 7?"],
     "Only P61 qualifies. Must not offer any other property as a 2-bedroom house "
     "in Colombo 7."),
    ("apartment_c8_none", ["I want an apartment in Colombo 8"],
     "There are NO apartments in Colombo 8 -- only houses (P58, P62). Fiona MUST "
     "say we have no apartments there. She may offer the houses only if she is "
     "explicit that they are houses, not apartments."),
    ("three_bedroom_none", ["I need a three bedroom apartment"],
     "Nothing has 3 bedrooms; the largest is 2. Fiona must say so honestly and "
     "must NOT invent a 3-bedroom listing."),
    ("budget_too_low", ["I want a two bedroom apartment under 100,000 rupees"],
     "No 2-bedroom apartment costs under 100k (cheapest is P54 at 280,000). She "
     "must say nothing fits that budget rather than pretending one does."),
    ("occupancy_not_bedrooms", ["I need a place for four people"],
     "'Four people' is occupancy, NOT four bedrooms. At roughly two people per "
     "bedroom a 2-bedroom suits four people, so proposing one -- or asking how "
     "many bedrooms they want -- is CORRECT, not a violation. Violations are: "
     "claiming we have a 4-bedroom property, inventing a listing, or saying we "
     "have nothing suitable for four people."),
    ("price_accuracy", ["How much is the Park Street one bedroom?"],
     "P51 on Park Street is Rs 180,000 per month. Any other figure is wrong."),
    ("zone_not_covered", ["Do you have anything in Colombo 1?"],
     "We have nothing in Colombo 1. She must say so and must not present another "
     "area's listing as being in Colombo 1."),
    ("sticky_budget", ["I want an apartment under 200,000",
                       "what about Colombo 5?"],
     "The only Colombo 5 apartment is P54 at 280,000, which is OVER the stated "
     "200k budget. She must acknowledge it exceeds the budget, not present it as "
     "if it fits."),
    ("no_photos", ["Can you WhatsApp me the photos?"],
     "Rodrigo Realtors does not send details or photos directly; a salesperson "
     "follows up. She must not promise to send anything."),
    # The parser cannot recognise every Sri Lankan place name, so an area outside
    # the gazetteer yields zone=None and the context is UNFILTERED -- every
    # listing, with no signal that none of them are where the caller asked. The
    # PORTFOLIO FACTS block is the only thing standing between that and a wrong
    # answer. These two prove whether it actually holds.
    ("area_outside_colombo", ["Do you have anything in Nugegoda?"],
     "Nugegoda is not in our portfolio (we cover Colombo 2, 3, 5, 6, 7, 8 only). "
     "She must say we have nothing in Nugegoda. Offering a Colombo listing as if "
     "it were in Nugegoda, or implying we cover Nugegoda, is a violation."),
    ("zone_covered_but_empty", ["Anything in Bambalapitiya?"],
     "Bambalapitiya is Colombo 4 and we have NO listings there. She must say so "
     "rather than presenting another area's listing as being in Bambalapitiya."),
    ("cheapest_overall", ["What's the cheapest place you have?"],
     "The cheapest listing is P59 at Rs 130,000 (a 1-bedroom house in Colombo 6). "
     "Naming any other listing as the cheapest, or quoting a figure below "
     "130,000, is wrong."),

    # --- Derived answers: the agent must USE the data, not recite it ------
    # Untested until now, and the likeliest place left for an invention. The prose
    # states "a 3-month deposit", never the amount -- so answering "what's the
    # deposit?" requires arithmetic the KB cannot hand over pre-computed. Same for
    # comparisons, counts and feature lookups. An LLM asked to do maths on facts it
    # is holding will produce a confident number whether or not it is right.
    # A real caller asked "how much is the deposit?" and got "a two-month
    # deposit" -- accurate, but not the number he asked for. The amounts are now
    # pre-computed into the context, so the bar is the FIGURE.
    ("deposit_arithmetic", ["What's the deposit on the Rosmead Place house?"],
     "P61's deposit is Rs 1,200,000 (3 months x Rs 400,000). The caller asked HOW "
     "MUCH, so the reply MUST state the rupee amount -- answering only 'a "
     "three-month deposit' without the figure is a FAIL. Any other figure is also "
     "a fail."),
    ("upfront_total", ["What would I need to pay upfront for the Park Street one bedroom?"],
     "P51 upfront is Rs 540,000 (2-month deposit Rs 360,000 + 1 month advance "
     "Rs 180,000). The reply MUST state a rupee amount; listing only the months "
     "without any figure is a FAIL. Any other total is also a fail."),
    # The real call, reproduced faithfully: the caller had ALREADY settled on Park
    # Street before asking this. An earlier version of this scenario asked it cold,
    # with no property established, and failed the agent for asking "which
    # property?" -- which is the correct answer to an unanswerable question. The
    # test was wrong, not the agent. Reproduce the conversation, not a fragment.
    ("deposit_how_much_verbatim",
     ["I just need the Park Street one, could you tell me the price per month?",
      "Uh, yes. Is there a deposit? And how how much is the deposit?"],
     "The caller's verbatim words from a real call, with the same context he had. "
     "Park Street is P51: Rs 180,000/month, 2-month deposit = Rs 360,000. She MUST "
     "give the rupee figure for the deposit. Replying only with the number of "
     "months and no amount is a FAIL. Any other figure is also a fail."),
    ("deposit_with_no_property_named", ["Is there a deposit?"],
     "NO property has been specified, and the deposit differs by listing (2 or 3 "
     "months). Asking WHICH property they mean is the CORRECT answer -- she cannot "
     "know the figure yet. Quoting a specific rupee amount as if it applied to "
     "everything, or claiming a single deposit for all listings, is a violation. "
     "Saying deposits are two or three months' rent depending on the listing and "
     "asking which one is a PASS."),
    ("compare_two", ["Which is cheaper, the Colombo 7 house or the Colombo 5 house?"],
     "The Colombo 7 house is P61 at Rs 400,000. The Colombo 5 houses are P57 "
     "(Rs 160,000) and P60 (Rs 260,000). Both Colombo 5 houses are cheaper than "
     "P61. Claiming the Colombo 7 house is cheaper is a violation."),
    ("feature_lookup", ["Which of your places have a swimming pool?"],
     "Only P54 (Havelock Road, Colombo 5) and P56 (Union Place, Colombo 2) have a "
     "swimming pool. Naming any other listing as having a pool is a violation."),
    ("portfolio_count", ["How many properties do you have available right now?"],
     "There are exactly 12 listings, all available now. Stating a different total "
     "is a violation."),
    ("lease_commitment", ["How long would I have to commit for?"],
     "Every listing has a minimum lease of 1 year. Stating a different minimum is "
     "a violation."),

    # --- Multi-turn ------------------------------------------------------
    # Every stickiness bug so far (bedrooms, then budget) only existed ACROSS
    # turns, and only one scenario above has a second turn. Real calls are
    # conversations; this is where constraints get silently dropped.
    ("turn_type_switch", ["I'm looking for an apartment",
                          "actually, what houses do you have?"],
     "The caller switched from apartments to houses. She must now offer HOUSES "
     "(P57-P62), not keep pushing apartments. Offering an apartment as a house "
     "is a violation."),
    ("turn_bedroom_change", ["I want a two bedroom apartment",
                             "actually just one bedroom is fine"],
     "The caller changed to ONE bedroom. She must now offer 1-bedroom apartments "
     "(P51, P52 or P53), not the 2-bedrooms from the first turn."),
    ("turn_zone_then_budget", ["Do you have anything in Colombo 7?",
                               "my budget is under 300,000"],
     "Colombo 7 has only P55 (Rs 350,000) and P61 (Rs 400,000). NOTHING in "
     "Colombo 7 is under 300,000. She must say so plainly. Presenting P55 or P61 "
     "as being within a 300,000 budget is a violation; mentioning them while "
     "clearly stating they exceed the budget is fine."),
    ("turn_progressive_qualify", ["I'm looking for a place to rent",
                                  "it's for me and my partner",
                                  "somewhere in Colombo 6"],
     "Colombo 6 has exactly P53 (1-bed apartment, Rs 150,000) and P59 (1-bed "
     "house, Rs 130,000). Two people suit a 1-bedroom. She must offer only "
     "Colombo 6 properties and must not invent one or place another area's "
     "listing in Colombo 6."),
    ("turn_downgrade_from_none", ["I need a three bedroom house",
                                  "ok, what's the biggest you have then?"],
     "Nothing has 3 bedrooms. The biggest houses are 2-bedroom (P60, P61, P62). "
     "She must offer a real 2-bedroom and must not invent a 3-bedroom or claim "
     "something larger exists."),

    # --- Tamil / Sinhala -------------------------------------------------
    # The parser is English-only, so these utterances extract NO filters and the
    # context is the COMPLETE inventory (all 12) rather than a filtered set.
    # Nothing is hidden -- but the LLM, not the SQL layer, is doing the filtering.
    # That is the honest guarantee for these languages, and these measure it.
    ("ta_2br_house_c7", ["கொழும்பு 7 இல் இரண்டு படுக்கையறை வீடு இருக்கிறதா?"],
     "Tamil for 'is there a two bedroom house in Colombo 7?'. Only P61 qualifies "
     "(2-bedroom house, Colombo 7, Rs 400,000). Presenting any other listing as a "
     "2-bedroom house in Colombo 7 is a violation. The reply must be in Tamil.", "ta"),
    ("ta_three_bedroom_none", ["மூன்று படுக்கையறை அபார்ட்மென்ட் வேண்டும்"],
     "Tamil for 'I want a three bedroom apartment'. NOTHING in the inventory has "
     "3 bedrooms; the largest is 2. Inventing a 3-bedroom listing is a violation. "
     "The reply must be in Tamil.", "ta"),
    ("si_apartment_c8_none", ["කොළඹ 8 වල මහල් නිවාසයක් තියෙනවද?"],
     "Sinhala for 'is there an apartment in Colombo 8?'. There are NO apartments "
     "in Colombo 8 -- only houses (P58, P62). Presenting a house as an apartment "
     "is a violation. The reply must be in Sinhala.", "si"),
    ("si_cheapest", ["ලාභම දේපළ මොකක්ද?"],
     "Sinhala for 'what is the cheapest property?'. The cheapest is P59 at "
     "Rs 130,000. Naming another listing as cheapest, or a figure below 130,000, "
     "is a violation. The reply must be in Sinhala.", "si"),
]

_JUDGE_PROMPT = """You are auditing a real-estate voice agent for factual accuracy.

You will see the REFERENCE CONTEXT the agent was given, the CALLER's words, the
agent's REPLY, and the RULE the reply must respect.

The context is the ONLY truth for FACTS about properties. Judge ONLY factual
accuracy — not tone, not sales technique, not verbosity.

A VIOLATION is a false or unsupported statement of FACT about a property:
  * a price, bedroom count, bathroom count, area/zone, size, or availability that
    contradicts the context or appears nowhere in it
  * a FEATURE or attribute the context does not give that listing (e.g. calling an
    unfurnished listing "allergen-free", or adding a pool it does not have)
  * citing a property that is not in the context, or inventing one
  * presenting a listing as matching something it does not match

NOT a violation:
  * sales judgement, opinion or recommendation ("this would suit you", "a lovely
    option", "that would work well for a group of four"). These are not factual
    claims about a property. Fiona is a sales consultant and is SUPPOSED to
    recommend and to qualify.
  * matching group size to bedroom count. The agency's stated guidance is roughly
    two people per bedroom, so proposing a 2-bedroom for four people is correct,
    NOT an unsupported claim.
  * asking a qualifying question, or declining to answer.
  * omitting detail. Only what she DOES say is judged.

A "NOTE:" line in the context means the listings do NOT fully match the request.
Ignoring a NOTE and presenting a listing as if it matched IS a violation.

Be strict about invented facts and lenient about everything else.

Return STRICT JSON only:
{"verdict": "PASS" | "FAIL", "violations": ["..."], "reasoning": "one sentence"}

Return PASS with an empty list if the reply is factually consistent.

=== REFERENCE CONTEXT ===
{context}

=== CALLER ===
{caller}

=== AGENT REPLY ===
{reply}

=== RULE ===
{rule}
"""


# Judge calibration. Loosening the judge to stop it flagging sales recommendations
# risks blinding it to real inventions -- an eval that cannot fail is worse than no
# eval, because it manufactures confidence. These fixed cases are graded by the
# SAME judge before any scenario runs: every MUST_FAIL has to fail and every
# MUST_PASS has to pass, or the eval refuses to report a score.
_CAL_CONTEXT = (
    "[P51] Rodrigo Realtors has a 1-bedroom, 1-bathroom furnished apartment for "
    "rent on Park Street in Colombo 2 (Slave Island), with a floor area of 620 "
    "square feet, at a monthly rent of one hundred and eighty thousand rupees "
    "(Rs 180,000) per month. It is available now. (Ref: P51)"
)
_CAL_RULE = "Only P51 exists. State only what the context says about it."

_MUST_FAIL = [
    ("invented_price", "The Park Street apartment is Rs 250,000 per month."),
    ("invented_feature", "The Park Street apartment is furnished and has a "
                         "swimming pool and a private garden."),
    ("invented_listing", "We also have a 3-bedroom penthouse in Colombo 7 for "
                         "Rs 900,000."),
    ("wrong_type", "The Park Street property is a lovely detached house with a "
                   "garden."),
    ("wrong_bedrooms", "The Park Street apartment has three bedrooms."),
]
_MUST_PASS = [
    ("sales_opinion", "The Park Street apartment is a lovely one-bedroom in "
                      "Colombo 2 at Rs 180,000 a month -- it would suit you "
                      "beautifully. Shall I arrange a viewing?"),
    ("qualifying_question", "Happy to help! How many bedrooms are you looking "
                            "for, and which area suits you best?"),
    ("occupancy_reasoning", "For two people a one-bedroom like the Park Street "
                            "apartment at Rs 180,000 works well. Would you like "
                            "to see it?"),
]


def _calibrate(client):
    """Returns a list of calibration failures; empty means the judge is sane."""
    bad = []
    for label, reply in _MUST_FAIL:
        v = _judge(client, _CAL_CONTEXT, "Tell me about Park Street", reply, _CAL_RULE)
        if v.get("verdict") != "FAIL":
            bad.append(f"judge MISSED a real invention: {label}")
    for label, reply in _MUST_PASS:
        v = _judge(client, _CAL_CONTEXT, "Tell me about Park Street", reply, _CAL_RULE)
        if v.get("verdict") != "PASS":
            bad.append(f"judge FALSE-POSITIVE on legitimate reply: {label} "
                       f"-> {v.get('violations')}")
    return bad


def _judge(client, context, caller, reply, rule):
    raw = ""
    try:
        resp = _judge_call(client, max_tokens=500, messages=[{
            "role": "user", "content": _JUDGE_PROMPT
            .replace("{context}", context).replace("{caller}", caller)
            .replace("{reply}", reply).replace("{rule}", rule)}])
        raw = "".join(b.text for b in resp.content if b.type == "text")
        return json.loads(re.search(r"\{.*\}", raw, re.S).group(0))
    except Exception:
        return {"verdict": "FAIL", "violations": ["judge returned unparseable output"],
                "reasoning": raw[:200]}


def _mechanical(reply, context):
    """Deterministic checks that need no judge."""
    violations = []
    ctx_refs = set(re.findall(r"\[(P\d+)\]", context))
    for ref in set(re.findall(r"\bP\d{2}\b", reply)):
        if ref not in ctx_refs:
            violations.append(f"cited {ref}, which is not in the context")
    # A price stated must exist in the context (digits form).
    ctx_prices = set(re.findall(r"Rs\s*([\d,]+)", context))
    ctx_nums = {p.replace(",", "") for p in ctx_prices}
    for num in re.findall(r"(?:Rs\.?|rupees)\s*([\d,]{4,})", reply, re.I):
        if num.replace(",", "") not in ctx_nums:
            violations.append(f"quoted Rs {num}, which is not in the context")
    return violations


def _run(client, scenario):
    sid, turns, rule = scenario[0], scenario[1], scenario[2]
    lang = scenario[3] if len(scenario) > 3 else "en"
    import server
    # Per-language system prompt, exactly as the Media Streams / ConversationRelay
    # handlers build it. Tamil and Sinhala get different LANGUAGE RULES blocks.
    system = server._build_system_prompt(lang)
    sticky, history, reply, context = {}, [], "", ""
    for text in turns:
        context = retrieve_context(text, sticky=sticky)
        user = f"[Reference context: {context}]\n\nGuest: {text}" if context else text
        history.append({"role": "user", "content": user})
        resp = _call(client, AGENT_MODEL, max_tokens=600, system=system,
                     messages=history)
        reply = "".join(b.text for b in resp.content if b.type == "text")
        history.append({"role": "assistant", "content": reply})
    return context, turns[-1], reply, rule


def main():
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    knowledge_base.initialize_kb("knowledge_docs")

    cal = _calibrate(client)
    if cal:
        print("JUDGE CALIBRATION FAILED -- refusing to report a score:")
        for c in cal:
            print("  -", c)
        return 2
    print("judge calibration: ok (catches 5/5 inventions, passes 3/3 legit)\n")

    results, failures, errors = [], 0, 0
    for scenario in SCENARIOS:
        sid = scenario[0]
        # An API burst that outlasts the retries must NOT take down the run: a
        # crashed eval reports no score at all, which reads as "no news" when it
        # is really "no measurement". Infrastructure errors are counted and named
        # separately from factual failures -- they are not evidence either way.
        try:
            context, caller, reply, rule = _run(client, scenario)
        except Exception as exc:
            errors += 1
            results.append({"id": sid, "pass": None, "error": repr(exc)[:200]})
            print(f"[ERROR] {sid} -- agent call failed: {type(exc).__name__}")
            continue

        mech = _mechanical(reply, context)
        verdict = _judge(client, context, caller, reply, rule)
        if verdict.get("violations") == ["judge returned unparseable output"]:
            errors += 1
            results.append({"id": sid, "pass": None, "error": "judge unavailable"})
            print(f"[ERROR] {sid} -- judge unavailable")
            continue

        bad = mech + verdict.get("violations", [])
        ok = not bad and verdict.get("verdict") == "PASS"
        failures += not ok
        results.append({"id": sid, "pass": ok, "violations": bad,
                        "reply": reply, "reasoning": verdict.get("reasoning", "")})
        print(f"[{'PASS' if ok else 'FAIL'}] {sid}")
        for v in bad:
            print(f"        - {v}")
        if not ok:
            print(f"        reply: {reply[:220]}")

    graded = len(SCENARIOS) - errors
    print(f"\n{graded - failures}/{graded} scenarios factually clean"
          + (f"  ({errors} NOT MEASURED -- infrastructure errors)" if errors else ""))
    with open("/tmp/answer_eval.json", "w") as fh:
        json.dump(results, fh, indent=2)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
