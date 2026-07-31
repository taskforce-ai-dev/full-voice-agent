"""End-to-end answer accuracy eval — QA, NOT proof.

The KB test suite proves what Amaya is HANDED: a correct, complete, self-labelling
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

EXPECTATIONS ARE DERIVED, NOT HARDCODED. The 2026-07-30 portfolio swap (12 demo
rows -> 60 real rows) silently invalidated a rubric full of literals ("exactly 12
listings", "nothing has 3 bedrooms") and the eval failed the agent for being
right. The rules below are computed from tests/truth_table.py — the same
hand-audited oracle the exhaustive filter suite trusts — plus listings.json for
the fields the truth table does not carry (streets, deposits, key features).
The two sources are cross-checked at startup; a mismatch refuses to score.

Run inside the flico container (it has the API key and the model). The image
does not ship tests/, so copy the oracle in first:
    docker cp tests/truth_table.py flico-voice-agent:/app/tests/truth_table.py
    docker cp evals/answer_eval.py flico-voice-agent:/app/evals/answer_eval.py
    docker exec flico-voice-agent python evals/answer_eval.py
"""
import json
import os
import random
import re
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

import anthropic  # noqa: E402

import knowledge_base  # noqa: E402
from knowledge_base import retrieve_context  # noqa: E402

try:
    from tests.truth_table import TRUTH  # noqa: E402
except ImportError:
    sys.stderr.write(
        "FATAL: tests/truth_table.py not importable. The rubric is DERIVED from "
        "that hand-audited oracle; without it this eval would have to hardcode "
        "portfolio facts, which is exactly what went stale last time. Copy it "
        "into the container first:\n"
        "  docker cp tests/truth_table.py flico-voice-agent:/app/tests/truth_table.py\n")
    sys.exit(2)

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


# ---------------------------------------------------------------------------
# Derived portfolio facts. truth_table.py is the audited oracle for type/zone/
# beds/rent/period; listings.json (the KB's actual source of truth, mounted in
# the container) supplies the fields the truth table does not record.
# ---------------------------------------------------------------------------
with open(os.path.join(_ROOT, "knowledge_docs", "listings.json"),
          encoding="utf-8") as _fh:
    _LISTINGS = {r["id"]: r for r in json.load(_fh)["listings"]}


def crosscheck_sources():
    """Both sources must describe the same portfolio, or the rubric is stale."""
    bad = []
    if {r["id"] for r in TRUTH} != set(_LISTINGS):
        bad.append("truth_table ids != listings.json ids")
    for r in TRUTH:
        listed = _LISTINGS.get(r["id"], {}).get("rent_amount")
        if (listed or None) != (float(r["rent"]) if r["rent"] is not None else None):
            bad.append(f"{r['id']}: rent disagrees (truth={r['rent']} listings={listed})")
    return bad


def _rows(**kw):
    out = TRUTH
    for k, v in kw.items():
        out = [r for r in out if r[k] == v]
    return out


def _ids(**kw):
    return [r["id"] for r in _rows(**kw)]


def _rs(n):
    return f"Rs {int(n):,}"


def _join(items):
    return ", ".join(items)


def _street_of(pid):
    return _LISTINGS[pid]["street"]


def _deposit_amount(pid):
    l = _LISTINGS[pid]
    return int(l["deposit_months"] * l["rent_amount"])


def _upfront_amount(pid):
    l = _LISTINGS[pid]
    return int((l["deposit_months"] + l["advance_months"]) * l["rent_amount"])


_N_TOTAL = len(TRUTH)
_ZONES = sorted({r["zone"] for r in TRUTH})
_ZONE_LIST = ", ".join(str(z) for z in _ZONES)
_POR_IDS = sorted(r["id"] for r in TRUTH if r["rent"] is None)

_MONTHLY = [r for r in TRUTH if r["rent"] is not None and r["period"] == "month"]
_CHEAPEST = min(_MONTHLY, key=lambda r: r["rent"])                    # commercial
_RESIDENTIAL_MONTHLY = [r for r in _MONTHLY if r["type"] in ("apartment", "house")]
_CHEAPEST_HOME = min(_RESIDENTIAL_MONTHLY, key=lambda r: r["rent"])

_PER_DAY = [r for r in TRUTH if r["period"] == "day"]
assert len(_PER_DAY) == 1, "rubric assumes exactly one per-day listing"
_P_DAY = _PER_DAY[0]  # P03

_APT_1BR = _ids(type="apartment", beds=1)
_APT_2BR = _rows(type="apartment", beds=2)          # all priced, none POR
_CHEAPEST_2BR_APT = min(_APT_2BR, key=lambda r: r["rent"])
_APT_3BR_N = len(_ids(type="apartment", beds=3))
_APT_4BR = _ids(type="apartment", beds=4)
_APT_MAX_BEDS = max(r["beds"] for r in TRUTH
                    if r["type"] == "apartment" and r["beds"])
_HOUSES = _ids(type="house")
_H5 = _rows(type="house", beds=5)
_MAX_BEDS = max(r["beds"] for r in TRUTH if r["beds"])

_C7_2BR_HOUSE = _ids(type="house", beds=2, zone=7)
_C5_2BR_HOUSE = _rows(type="house", beds=2, zone=5)
_C8_APTS = _rows(type="apartment", zone=8)
_C8_HOUSES = _ids(type="house", zone=8)
_C10 = _rows(zone=10)
_C1 = _rows(zone=1)
_C1_PRICED = [r for r in _C1 if r["rent"] is not None]
_C6 = _rows(zone=6)
_C5_APTS = _rows(type="apartment", zone=5)
_C7 = _rows(zone=7)
_C7_UNDER_300K = [r for r in _C7 if r["rent"] is not None
                  and r["period"] == "month" and r["rent"] < 300000]
_C7_POR = [r["id"] for r in _C7 if r["rent"] is None]
_C7_OVER = [r for r in _C7 if r["rent"] is not None
            and r["period"] == "month" and r["rent"] >= 300000]

# Street-addressed listings the deposit scenarios reference by id.
_PARK_ST = next(i for i, l in _LISTINGS.items() if l.get("street") == "Park Street")
_ROSMEAD = next(i for i, l in _LISTINGS.items() if l.get("street") == "Rosmead Place")

_DEPOSIT_MONTHS_STATED = sorted({l["deposit_months"] for l in _LISTINGS.values()
                                 if l.get("deposit_months")})
_N_NO_DEPOSIT = sum(1 for l in _LISTINGS.values() if not l.get("deposit_months"))
_LEASE_STATED = sorted({l["min_lease_months"] for l in _LISTINGS.values()
                        if l.get("min_lease_months")})
_N_NO_LEASE = sum(1 for l in _LISTINGS.values() if not l.get("min_lease_months"))

_POOL_HOUSES = sorted(
    pid for pid, l in _LISTINGS.items()
    if any("pool" in f.lower() for f in l.get("key_features", []))
    and pid in set(_HOUSES))

_HAVELOCK_CITY = next(i for i, l in _LISTINGS.items()
                      if l.get("building") == "Havelock City")


def _fmt(rows):
    """Compact 'P61 (Rs 400,000)' / 'P02 (on request)' listing summaries."""
    out = []
    for r in rows:
        if r["rent"] is None:
            out.append(f"{r['id']} (rent on request)")
        elif r["period"] == "day":
            out.append(f"{r['id']} ({_rs(r['rent'])} per DAY)")
        else:
            out.append(f"{r['id']} ({_rs(r['rent'])})")
    return ", ".join(out)


# (id, turns, what a correct answer must respect[, lang])
# Every fact below is interpolated from the oracle at import time, so the next
# portfolio swap regrades automatically instead of failing the agent for the
# rubric's staleness. Each scenario exists to catch a specific failure class --
# invented listings/prices, denied stock, type substitution, POR-as-budget-fit,
# per-day-as-monthly, arithmetic, aggregation -- keep the class if you edit it.
SCENARIOS = [
    ("1br_apartment", ["I'm looking for a one bedroom apartment"],
     f"The only 1-bedroom apartments are {_join(_APT_1BR)}. Must offer only "
     "these. Must not offer a bigger unit or a house as if it were a 1-bedroom "
     "apartment."),
    ("2br_house_c7", ["Do you have a two bedroom house in Colombo 7?"],
     f"Only {_join(_C7_2BR_HOUSE)} qualifies. Must not offer any other property "
     "as a 2-bedroom house in Colombo 7."),

    # --- Stock that EXISTS must never be denied (new data: 3/4/5-bed, C1/4/8/10)
    ("apartment_c8_exists", ["I want an apartment in Colombo 8"],
     f"Colombo 8 HAS apartments: {_fmt(_C8_APTS)}. Denying them is a violation. "
     "Quoting any figure for a rent-on-request listing is a violation. The "
     f"Colombo 8 houses ({_join(_C8_HOUSES)}) must not be presented as "
     "apartments."),
    ("three_bedroom_exists", ["I need a three bedroom apartment"],
     f"There are {_APT_3BR_N} three-bedroom apartments -- denying that we have "
     "3-bedroom apartments is a violation. Many have rent on request: quoting "
     "or estimating a figure for one of those is a violation."),
    ("four_bedroom_exists", ["Do you have any four bedroom apartments?"],
     f"Yes: {_join(_APT_4BR)} are all 4-bedroom apartments. Denying them is a "
     "violation. Several are rent-on-request; quoting a figure for one of those "
     "is a violation."),
    ("five_bedroom_exists", ["I need a five bedroom house"],
     f"The 5-bedroom houses are {_fmt(_H5)}. Denying them, inventing another, "
     "or quoting a figure for the on-request one is a violation."),
    ("colombo_1_covered", ["Do you have anything in Colombo 1?"],
     f"Colombo 1 has {len(_C1)} listings: {_fmt(_C1)}. Saying we have nothing "
     "in Colombo 1 is a violation. Only the priced one(s) may be quoted a "
     "figure; the on-request ones must be offered as price-on-request."),

    # --- Type honesty: none of the asked type in a zone, commercial vs home --
    ("apartment_c10_none", ["I want an apartment in Colombo 10"],
     f"There are NO apartments in Colombo 10. The only Colombo 10 listing is "
     f"{_fmt(_C10)}, a COMMERCIAL building. Amaya MUST say we have no "
     "apartments there; she may mention it only while explicit that it is a "
     "commercial/office property, never as an apartment or a home."),
    ("commercial_not_home",
     ["I'm looking for somewhere for my family to live in Colombo 10"],
     f"The only Colombo 10 listing is {_fmt(_C10)} -- a COMMERCIAL building, "
     "not a home. Offering it as a place to live without saying it is "
     "commercial is a violation. Saying we have no residential property in "
     "Colombo 10 is CORRECT."),

    ("budget_too_low", ["I want a two bedroom apartment under 100,000 rupees"],
     f"No 2-bedroom apartment rents under 100k (they are {_fmt(_APT_2BR)}; the "
     f"cheapest is {_CHEAPEST_2BR_APT['id']} at {_rs(_CHEAPEST_2BR_APT['rent'])}). "
     "She must say nothing fits that budget rather than pretending one does; "
     "mentioning one while saying plainly it is over budget is fine."),
    ("occupancy_not_bedrooms", ["I need a place for four people"],
     "'Four people' is occupancy, NOT four bedrooms. At roughly two people per "
     "bedroom a 2-bedroom suits four people, so proposing one -- or asking how "
     "many bedrooms they want -- is CORRECT, not a violation. Violations are: "
     "inventing a listing, misstating a real listing's details, or saying we "
     "have nothing suitable for four people."),
    ("price_accuracy", ["How much is the Park Street one bedroom?"],
     f"{_PARK_ST} on {_street_of(_PARK_ST)} is "
     f"{_rs(_LISTINGS[_PARK_ST]['rent_amount'])} per month. Any other figure "
     "is wrong."),
    ("zone_not_covered", ["Do you have anything in Colombo 9?"],
     f"We have nothing in Colombo 9 (we cover Colombo {_ZONE_LIST} only). She "
     "must say so and must not present another area's listing as being in "
     "Colombo 9."),

    # --- Price on request: the price is UNKNOWN, full stop ------------------
    # A POR listing deliberately SURVIVES a budget ceiling (kb/database.py) so
    # the caller still hears about it -- but its price can never be claimed to
    # fit a budget, and never estimated. This failed for real on 2026-07-30.
    ("sticky_budget", ["I want an apartment under 200,000",
                       "what about Colombo 5?"],
     f"The Colombo 5 apartments are {_fmt(_C5_APTS)}. NONE has a quoted rent "
     "under 200,000. The rent-on-request one may be offered ONLY as "
     "price-on-request: presenting it as fitting, meeting or being under the "
     "200k budget, or estimating its price, is a violation. Presenting the "
     "priced one as within a 200k budget is a violation."),
    ("por_no_price", ["How much is the Havelock City apartment?"],
     f"{_HAVELOCK_CITY} at Havelock City has its rent ON REQUEST. Correct: say "
     "the rent is available on request and a consultant will confirm it. "
     f"Quoting ANY rupee figure as {_HAVELOCK_CITY}'s rent -- including another "
     "listing's rent -- or estimating one, is a violation."),

    # --- Per-day rent must never be quoted as monthly ------------------------
    ("per_day_not_monthly",
     [f"What's the rent for the {_P_DAY['id']} listing on "
      f"{_street_of(_P_DAY['id'])}?"],
     f"{_P_DAY['id']} rents at {_rs(_P_DAY['rent'])} per DAY, not per month. "
     "She must state the per-DAY period. Presenting the figure as monthly, or "
     "converting it into a monthly/annual estimate, is a violation."),
    ("bambalapitiya_per_day", ["Anything in Bambalapitiya?"],
     f"Bambalapitiya is Colombo 4, where the only listing is {_P_DAY['id']} at "
     f"{_rs(_P_DAY['rent'])} PER DAY. Denying we cover Bambalapitiya is a "
     "violation, and so is quoting the rent as a monthly figure."),

    # The parser cannot recognise every Sri Lankan place name, so an area outside
    # the gazetteer yields zone=None and the context is UNFILTERED -- every
    # listing, with no signal that none of them are where the caller asked. The
    # PORTFOLIO FACTS block is the only thing standing between that and a wrong
    # answer.
    ("area_outside_colombo", ["Do you have anything in Nugegoda?"],
     f"Nugegoda is not in our portfolio (we cover Colombo {_ZONE_LIST} only). "
     "She must say we have nothing in Nugegoda. Offering a Colombo listing as "
     "if it were in Nugegoda, or implying we cover Nugegoda, is a violation."),

    ("no_photos", ["Can you WhatsApp me the photos?"],
     "Star Properties does not send details or photos directly; a salesperson "
     "follows up. She must not promise to send anything."),
    ("cheapest_overall", ["What's the cheapest place you have?"],
     f"The cheapest listing overall is {_CHEAPEST['id']} at "
     f"{_rs(_CHEAPEST['rent'])} per month -- a COMMERCIAL property in Colombo "
     f"{_CHEAPEST['zone']}, not a home. The cheapest RESIDENTIAL listing is "
     f"{_CHEAPEST_HOME['id']} at {_rs(_CHEAPEST_HOME['rent'])} per month (a "
     f"{_CHEAPEST_HOME['beds']}-bedroom {_CHEAPEST_HOME['type']} in Colombo "
     f"{_CHEAPEST_HOME['zone']}). Either answer is acceptable ONLY with the "
     f"commercial one clearly identified as commercial. Naming "
     f"{_CHEAPEST['id']} as the cheapest without saying it is commercial "
     "property, naming any other listing as cheapest, quoting a figure below "
     f"{_rs(_CHEAPEST['rent'])}, or calling the per-day listing the cheapest "
     "monthly option, is a violation."),

    # --- Derived answers: the agent must USE the data, not recite it ------
    # The prose pre-computes every deposit/advance/upfront figure precisely so
    # the LLM never multiplies. The bar for a "how much" question is the FIGURE:
    # a real caller asked "how much is the deposit?" and got "a two-month
    # deposit" -- accurate, but not the number he asked for.
    ("deposit_arithmetic",
     [f"What's the deposit on the {_street_of(_ROSMEAD)} house?"],
     f"{_ROSMEAD}'s deposit is {_rs(_deposit_amount(_ROSMEAD))} "
     f"({_LISTINGS[_ROSMEAD]['deposit_months']} months x "
     f"{_rs(_LISTINGS[_ROSMEAD]['rent_amount'])}). The caller asked HOW MUCH, "
     "so the reply MUST state the rupee amount -- answering only with the "
     "number of months and no figure is a FAIL. Any other figure is also a "
     "fail."),
    ("upfront_total",
     [f"What would I need to pay upfront for the {_street_of(_PARK_ST)} "
      "one bedroom?"],
     f"{_PARK_ST} upfront is {_rs(_upfront_amount(_PARK_ST))} "
     f"({_LISTINGS[_PARK_ST]['deposit_months']}-month deposit "
     f"{_rs(_deposit_amount(_PARK_ST))} + "
     f"{_LISTINGS[_PARK_ST]['advance_months']} month advance "
     f"{_rs(_LISTINGS[_PARK_ST]['advance_months'] * _LISTINGS[_PARK_ST]['rent_amount'])}). "
     "The reply MUST state a rupee amount; listing only the months without any "
     "figure is a FAIL. Any other total is also a fail."),
    # The real call, reproduced faithfully: the caller had ALREADY settled on
    # Park Street before asking this. An earlier version asked it cold and
    # failed the agent for asking "which property?" -- which is the correct
    # answer to an unanswerable question. Reproduce the conversation.
    ("deposit_how_much_verbatim",
     ["I just need the Park Street one, could you tell me the price per month?",
      "Uh, yes. Is there a deposit? And how how much is the deposit?"],
     f"The caller's verbatim words from a real call. Park Street is {_PARK_ST}: "
     f"{_rs(_LISTINGS[_PARK_ST]['rent_amount'])}/month, "
     f"{_LISTINGS[_PARK_ST]['deposit_months']}-month deposit = "
     f"{_rs(_deposit_amount(_PARK_ST))}. She MUST give the rupee figure for "
     "the deposit. Replying only with the number of months and no amount is a "
     "FAIL. Any other figure is also a fail."),
    ("deposit_with_no_property_named", ["Is there a deposit?"],
     "NO property has been specified, and deposits DIFFER by listing (where "
     f"stated they range over {_DEPOSIT_MONTHS_STATED} months' rent, and "
     f"{_N_NO_DEPOSIT} of {_N_TOTAL} listings state no deposit at all). Asking "
     "WHICH property they mean is the CORRECT answer -- she cannot know the "
     "figure yet. Quoting a specific rupee amount as if it applied to "
     "everything, or claiming one deposit rule covers every listing, is a "
     "violation. Saying deposits depend on the listing and asking which one is "
     "a PASS."),
    ("compare_two", ["Which is cheaper, the two bedroom house in Colombo 7 or "
                     "the two bedroom house in Colombo 5?"],
     f"The Colombo 7 two-bedroom house is {_fmt(_rows(type='house', beds=2, zone=7))}; "
     f"the Colombo 5 two-bedroom house is {_fmt(_C5_2BR_HOUSE)}. The Colombo 5 "
     "one is cheaper. Claiming the Colombo 7 house is cheaper is a violation."),
    ("feature_lookup", ["Which of your houses have a swimming pool?"],
     f"Among the HOUSES, only {_join(_POOL_HOUSES)} have a swimming pool. "
     "Naming any other house as having a pool is a violation. (Apartments with "
     "pools exist but were not asked about; mentioning one is fine only if "
     "clearly called an apartment.)"),
    ("portfolio_count", ["How many properties do you have available right now?"],
     f"There are exactly {_N_TOTAL} listings. Stating a different total is a "
     "violation."),
    ("lease_commitment", ["How long would I have to commit for?"],
     f"No property has been named, and lease terms differ: {_N_TOTAL - _N_NO_LEASE} "
     f"listings state a minimum lease of {_LEASE_STATED[0] // 12} year, while "
     f"{_N_NO_LEASE} state no minimum at all. Saying the minimum is one year "
     "WHERE STATED, saying it depends on the listing, or asking which property, "
     "are all PASSES. Claiming one universal minimum binds every listing, or "
     "inventing a different figure, is a violation."),

    # --- Multi-turn ------------------------------------------------------
    # Every stickiness bug so far (bedrooms, then budget) only existed ACROSS
    # turns. Real calls are conversations; this is where constraints get
    # silently dropped.
    ("turn_type_switch", ["I'm looking for an apartment",
                          "actually, what houses do you have?"],
     f"The caller switched from apartments to houses. She must now offer "
     f"HOUSES ({_join(_HOUSES)}), not keep pushing apartments. Offering an "
     "apartment as a house is a violation."),
    ("turn_bedroom_change", ["I want a two bedroom apartment",
                             "actually just one bedroom is fine"],
     f"The caller changed to ONE bedroom. She must now offer 1-bedroom "
     f"apartments ({_join(_APT_1BR)}), not the 2-bedrooms from the first turn."),
    ("turn_zone_then_budget", ["Do you have anything in Colombo 7?",
                               "my budget is under 300,000"],
     f"The only Colombo 7 listing with a QUOTED rent under 300,000 is "
     f"{_fmt(_C7_UNDER_300K)}. The rent-on-request Colombo 7 listings "
     f"({_join(_C7_POR)}) may be mentioned only as price-on-request, never as "
     f"fitting the budget. The priced listings over 300k ({_fmt(_C7_OVER)}) "
     "must not be presented as within budget; mentioning them while clearly "
     "stating they exceed it is fine."),
    ("turn_progressive_qualify", ["I'm looking for a place to rent",
                                  "it's for me and my partner",
                                  "somewhere in Colombo 6"],
     f"Colombo 6 has exactly {_fmt(_C6)}. Two people suit a smaller unit, so "
     "recommending among these is correct. She must offer only Colombo 6 "
     "properties, must not invent one or place another area's listing in "
     "Colombo 6, and must not quote a figure for the rent-on-request one."),
    ("turn_downgrade_from_none", ["I need a six bedroom house",
                                  "ok, what's the biggest you have then?"],
     f"Nothing has 6 bedrooms; the largest are {_MAX_BEDS}-bedroom houses: "
     f"{_fmt(_H5)}. She must offer a real {_MAX_BEDS}-bedroom, must not invent "
     "a 6-bedroom or claim something larger exists, and must not quote a "
     "figure for the on-request one."),

    # --- Tamil / Sinhala -------------------------------------------------
    # The parser is English-only, so these utterances extract NO filters and the
    # context is the COMPLETE inventory (all 60) rather than a filtered set.
    # Nothing is hidden -- but the LLM, not the SQL layer, is doing the
    # filtering. That is the honest guarantee for these languages.
    ("ta_2br_house_c7", ["கொழும்பு 7 இல் இரண்டு படுக்கையறை வீடு இருக்கிறதா?"],
     f"Tamil for 'is there a two bedroom house in Colombo 7?'. Only "
     f"{_fmt(_rows(type='house', beds=2, zone=7))} qualifies. Presenting any "
     "other listing as a 2-bedroom house in Colombo 7 is a violation. The "
     "reply must be in Tamil.", "ta"),
    ("ta_5br_apartment_none", ["ஐந்து படுக்கையறை அபார்ட்மென்ட் இருக்கிறதா?"],
     f"Tamil for 'is there a five bedroom apartment?'. NO apartment has 5 "
     f"bedrooms (the largest apartments are {_APT_MAX_BEDS}-bedroom). The only "
     f"5-bedroom properties are HOUSES: {_fmt(_H5)}. Inventing a 5-bedroom "
     "apartment, or presenting a house as an apartment, is a violation. The "
     "reply must be in Tamil.", "ta"),
    ("si_apartment_c8_exists", ["කොළඹ 8 වල මහල් නිවාසයක් තියෙනවද?"],
     f"Sinhala for 'is there an apartment in Colombo 8?'. Colombo 8 HAS "
     f"apartments: {_fmt(_C8_APTS)}. Denying them is a violation; quoting a "
     "figure for the rent-on-request one is a violation; presenting a house as "
     "an apartment is a violation. The reply must be in Sinhala.", "si"),
    ("si_cheapest", ["ලාභම දේපළ මොකක්ද?"],
     f"Sinhala for 'what is the cheapest property?'. The cheapest is "
     f"{_CHEAPEST['id']} at {_rs(_CHEAPEST['rent'])} per month, which is "
     f"COMMERCIAL property; the cheapest home is {_CHEAPEST_HOME['id']} at "
     f"{_rs(_CHEAPEST_HOME['rent'])}. Either is acceptable with the commercial "
     "one identified as commercial. Naming another listing as cheapest, or a "
     f"figure below {_rs(_CHEAPEST['rent'])}, is a violation. The reply must "
     "be in Sinhala.", "si"),
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
  * quoting, estimating or implying a figure for a rent that the context says is
    "on request" — an on-request price is UNKNOWN and can never be said to fit a
    budget
  * quoting a per-day rent as if it were monthly, or converting it

NOT a violation:
  * sales judgement, opinion or recommendation ("this would suit you", "a lovely
    option", "that would work well for a group of four"). These are not factual
    claims about a property. Amaya is a sales consultant and is SUPPOSED to
    recommend and to qualify.
  * matching group size to bedroom count. The agency's stated guidance is roughly
    two people per bedroom, so proposing a 2-bedroom for four people is correct,
    NOT an unsupported claim.
  * asking a qualifying question, or declining to answer.
  * omitting detail. Only what she DOES say is judged.

A "NOTE:" line in the context means the listings do NOT fully match the request
or carry an unknown (on-request) rent under a stated budget. Ignoring a NOTE and
presenting a listing as if it matched IS a violation.

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
    "[P51] Star Properties has a 1-bedroom, 1-bathroom furnished apartment for "
    "rent on Park Street in Colombo 2 (Slave Island), with a floor area of 620 "
    "square feet, at a monthly rent of one hundred and eighty thousand rupees "
    "(Rs 180,000) per month. It is available now. (Ref: P51)\n\n"
    "[P90] Star Properties has a 3-bedroom furnished apartment for rent in "
    "Colombo 5. The monthly rent is available on request -- a Star Properties "
    "consultant will share it on follow-up. (Ref: P90)"
)
_CAL_RULE = ("Only P51 and P90 exist. State only what the context says. P90's "
             "rent is on request and may never be quoted, estimated, or claimed "
             "to fit any budget.")

_MUST_FAIL = [
    ("invented_price", "The Park Street apartment is Rs 250,000 per month."),
    ("invented_feature", "The Park Street apartment is furnished and has a "
                         "swimming pool and a private garden."),
    ("invented_listing", "We also have a 3-bedroom penthouse in Colombo 7 for "
                         "Rs 900,000."),
    ("wrong_type", "The Park Street property is a lovely detached house with a "
                   "garden."),
    ("wrong_bedrooms", "The Park Street apartment has three bedrooms."),
    ("por_priced", "The Colombo 5 three-bedroom is around Rs 300,000 a month "
                   "-- well within your budget."),
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
    ("por_honest", "We also have a three-bedroom in Colombo 5 -- its rent is "
                   "quoted on request, so a consultant will confirm the figure "
                   "on follow-up. Would you like me to arrange that?"),
]


def _calibrate(client):
    """Returns a list of calibration failures; empty means the judge is sane."""
    bad = []
    for label, reply in _MUST_FAIL:
        v = _judge(client, _CAL_CONTEXT, "Tell me about your listings", reply, _CAL_RULE)
        if v.get("verdict") != "FAIL":
            bad.append(f"judge MISSED a real invention: {label}")
    for label, reply in _MUST_PASS:
        v = _judge(client, _CAL_CONTEXT, "Tell me about your listings", reply, _CAL_RULE)
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

    stale = crosscheck_sources()
    if stale:
        print("RUBRIC SOURCES DISAGREE -- refusing to report a score. The truth "
              "table and listings.json must describe the same portfolio:")
        for s in stale:
            print("  -", s)
        return 2

    cal = _calibrate(client)
    if cal:
        print("JUDGE CALIBRATION FAILED -- refusing to report a score:")
        for c in cal:
            print("  -", c)
        return 2
    print(f"judge calibration: ok (catches {len(_MUST_FAIL)}/{len(_MUST_FAIL)} "
          f"inventions, passes {len(_MUST_PASS)}/{len(_MUST_PASS)} legit)\n")

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
