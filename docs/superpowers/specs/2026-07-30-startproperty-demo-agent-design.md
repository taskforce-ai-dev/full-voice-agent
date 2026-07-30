# Start Property — real-estate demo voice agent

**Date:** 2026-07-30
**Status:** approved, ready for implementation

## Problem

The website's Book a Demo page (`Taskforce_AI_Website/components/pages/BookDemo.tsx`)
ships a fourth agent card — **Start Property**, agent **Amaya**, "Real Estate Agent",
Colombo, English only. The card is live. The backend behind it is not.

Because Hatton's demo router has no route for the `startproperty` agent id, the call
falls through to the default branch and connects the visitor to **Tanya at Hatton
Hills**, who greets them as a hotel. Verified live:

```
$ curl -sX POST -d "agent=startproperty&lang=en" \
    https://hattonhills.taskforceai.tech/voice/demo-incoming
... welcomeGreeting="Welcome to Hatton Hills! I'm Tanya, how can I help you today?"
```

That is worse than a visible error — it is a working call to the wrong business.

## Existing assets

The directory named `Flico Agent` is, in substance, the **Rodrigo Realtors**
real-estate agent. The "Flico electronics retailer" naming is legacy and was never
cleaned up: the module docstring and FastAPI title still say Flico, but the persona is
*"Fiona, a warm, confident, top-performing SALES consultant for Rodrigo Realtors."*
Live and healthy at `flico.taskforceai.tech`.

It carries a purpose-built hybrid retrieval engine in `kb/` that we get for free:

- **Structured rows are the source of truth** (`knowledge_docs/listings.json`);
  `kb/prose.py` *generates* prose from fields. Nothing parses prose back into fields —
  that inversion is deliberate (see the Jul 17 2026 note in `Flico Agent/CLAUDE.md`).
- **Sticky filters** — `property_type`/`zone` persist across turns, so a later "I'd love
  Colombo 5" does not lose the earlier "apartment".
- **A relaxation ladder** (`kb/engine.py::_RELAXATIONS`) relaxes rent → bedrooms →
  property_type and prepends a `NOTE:` naming what was given up, so the LLM cannot
  present a house as the apartment the caller asked for. Zone is never dropped.
- Rent-period awareness (per-day vs per-month), inclusive/exclusive budget parsing,
  exact-vs-floor bedroom matching.

Three datasets exist. This design uses the first and touches none of them:

| Source | Rows | What it is |
|---|---|---|
| `knowledge_docs/listings.json` | 12 | **Synthetic** demo rentals P51–P62, all `rent`, 1BR/2BR, 6 apartments + 6 houses, Colombo 2/3/5/6/7/8 — current source of truth |
| `data/rodrigo_kb.db` | 49 | The **real** Rodrigo portfolio, with embeddings |
| `_rodrigo_listings.json` | 44 | Scraped lankapropertyweb listings — all `sale`, not rent |

The synthetic set is the right one: the card promises rentals, and fictional inventory
behind a fictional brand misrepresents nobody.

**The gap:** the Rodrigo server has no `/voice/demo-incoming` (404). It only has the
phone IVR `/voice/incoming`, so it cannot receive a browser demo call.

## Decisions

1. **Reuse the Rodrigo agent** rather than cloning an isolated Start Property service.
   No new container, subdomain, nginx vhost, cert or DNS record; the retrieval engine
   and KB come for free. Accepted risk: the demo shares a container with a real client's
   live phone line — mitigated by decision 2 and by deploying without an image rebuild.
2. **Brand as Start Property / Amaya**, keeping the shipped card, by parameterizing the
   persona rather than editing it. Rodrigo's phone path must stay byte-identical.

## Design

### 1. Brand registry (`Flico Agent/server.py`)

```python
BRANDS = {
  "rodrigo":       {"agency": "Rodrigo Realtors", "agent": "Fiona", "transfer": True,  ...},
  "startproperty": {"agency": "Start Property",   "agent": "Amaya", "transfer": False, ...},
}
DEFAULT_BRAND = "rodrigo"
```

Each entry carries agency name, agent name, per-language welcome greeting, and a
`transfer` flag. `_build_system_prompt(lang, brand=DEFAULT_BRAND)` substitutes the names
into the **9 substitution sites** inside the returned prompt — `server.py:416` (`Fiona`)
and `417, 438, 445, 526, 580, 584, 587, 594` (`Rodrigo Realtors`).

Two other occurrences are deliberately **out of scope**: line 372 is a Sinhala
transliteration glossary entry reached only on the `si` branch (this demo is English
only), and line 431 is a code comment.

**The greeting echo at line 437–439 must be derived from the brand's greeting, not
duplicated.** That block tells the agent *"The opening greeting is already spoken
automatically: '…'. Do NOT repeat it."* If it keeps naming Rodrigo while Twilio actually
speaks the Start Property greeting, Amaya is briefed on words the caller never heard.
Build it by interpolating `BRANDS[brand]` so the two can never drift.

**The default argument is the safety property.** Every existing call site keeps passing
only `lang`, so the phone IVR produces a byte-identical prompt for Rodrigo. A real
client's number sees no behaviour change.

### 2. `/voice/demo-incoming` on the Rodrigo server

Modelled on `Kitchened/server.py`'s working implementation, with two deliberate
differences:

- **English only.** The card declares `langs: ['en']`, so unknown/missing `lang`
  collapses to `en` and no Media Streams branch is needed.
- Stamps `brand=startproperty` into both the `welcomeGreeting` and the ConversationRelay
  WebSocket URL.

### 3. `ws_conversation(websocket, lang="en", brand=DEFAULT_BRAND)`

Validates `brand` against `BRANDS` (unknown → `DEFAULT_BRAND`), threads it into
`_build_system_prompt`, and gates the transfer tool on `BRANDS[brand]["transfer"]`:

```python
tools = [TRANSFER_TOOL] if (lang == "en" and BRANDS[brand]["transfer"]) else []
```

Amaya has no human consultant behind her. She must never offer a transfer she cannot
perform.

### 4. Hatton demo router

The router exists in production but **in no git branch** — it was hot-patched on the VPS
and never committed. Captured 2026-07-30 by diffing `/opt/hatton-hills/server.py`
against both `origin/main` and `kavya-mosvold-kb`; both diffs are identical and consist
of exactly two hunks, purely the router. No other uncommitted changes exist in that file.

Recovery commits those two hunks verbatim, then adds the new route:

```python
DEMO_AGENT_HOSTS: dict[str, str] = {
    "kitchened": os.getenv("DEMO_HOST_KITCHENED", "kitchened.taskforceai.tech"),
    "worldofrefrigerators": os.getenv("DEMO_HOST_WOR", "worldofrefrigerators.taskforceai.tech"),
    "startproperty": os.getenv("DEMO_HOST_STARTPROPERTY", "flico.taskforceai.tech"),
}
```

**This must land before any other Hatton change ships.** Pushing to `main` auto-deploys
via rsync + hot-swap of `server.py`; a deploy from current `main` would overwrite prod
and take the Kitchen & Co. and World of Refrigerators demos down with it.

### 5. A distinct voice for Amaya

New env var `CR_VOICE_STARTPROPERTY` (ElevenLabs voice id), falling back to Fiona's
`ELEVENLABS_VOICE_ID` when unset so a missing variable degrades instead of breaking. Two
demos on the same page should not sound like the same person.

### 6. Card correction (`BookDemo.tsx`)

The shipped `trainedOn` list has exactly one false claim: *"Houses, apartments &
commercial units"*. The 12 demo rows are residential-only — 6 apartments, 6 houses, no
commercial. Change that bullet to "Houses & apartments".

The other bullets check out against the data and stay as-is. In particular *"Furnished /
unfurnished options"* is accurate: furnishing splits `furnished: 6, semi: 4,
unfurnished: 2`. Likewise `deposit_months`, `min_lease_months`, `advance_months`,
`floor_area_sqft`, `key_features` and `available` all exist as fields, so the rates,
lease-terms, size/amenities and availability bullets are all backed.

`Flico Agent/CLAUDE.md` warns about exactly this class of mismatch: when prompt and KB
disagree about what the portfolio contains, the agent denies listings that retrieval
correctly handed it. Note the prompt side of this is already safe — `kb/facts.py::portfolio_facts()`
*generates* the PORTFOLIO FACTS block from the live inventory, and
`tests/test_portfolio_facts.py` proves it tracks the data and stays silent when it
cannot know. Only the hand-written website card can drift.

## Non-goals

- No new Twilio phone number. The browser demo rides the existing shared TwiML app, so
  the token endpoint already in production covers it.
- No KB authoring, migration, or schema change.
- No cleanup of the legacy "Flico" naming (module docstring, FastAPI title, directory
  name). Real but out of scope; it would touch deploy paths and container names.
- No Tamil/Sinhala/Arabic support for this demo.

## Global constraints

- **Prod runs Python 3.11; this dev box runs 3.12.3.** No PEP 701 f-strings — never nest
  same-type quotes inside an f-string (`f"{d["k"]}"` compiles here and is a SyntaxError
  there). Use `f'{d["k"]}'` as the existing code does. `server.py` imports `kb/prose.py`,
  so a SyntaxError is a hard outage, and local `py_compile` will not catch it. The
  `kb-verify` CI job pins 3.11 and is what caught this last time.
- **`kb-verify` gates deploy.** `.github/workflows/deploy-on-push.yml` runs
  `python -m pytest tests/ -q` (Python 3.11, `pytest numpy pydantic`) from the
  `Flico Agent` working directory on any change under `Flico Agent/`, and the `deploy`
  job requires it to pass. Every task touching that directory must leave the suite green.
- **Run pytest as `python -m pytest`**, not bare `pytest`, so CWD is importable and the
  `tests/` oracle package resolves.
- Never fix a KB filter bug by adding one more example test — add the dimension to the
  grammar/oracle and let the sweep prove the space. (Not expected in this plan, which
  touches no filter logic.)

## Testing

No-phone smoke tests, all runnable with `curl`:

1. `POST lang=en` → `flico.../voice/demo-incoming` returns ConversationRelay TwiML with
   the Start Property greeting and `brand=startproperty` in the WebSocket URL.
2. `POST agent=startproperty` → `hattonhills.../voice/demo-incoming` returns a
   `<Redirect>` to `flico.taskforceai.tech`.
3. **Regression:** `POST` to `flico.../voice/incoming` renders the unchanged Rodrigo IVR;
   `agent=kitchened` and `agent=worldofrefrigerators` still redirect to their own hosts;
   `agent=hatton` and no-agent still serve Tanya locally.
4. A unit-level assertion that `_build_system_prompt("en")` — called with no brand — is
   byte-identical to its pre-change output. This is the guard on the live client line.
5. Live browser call through the site: select Start Property, call, confirm Amaya greets
   as Start Property, ask for a 2-bedroom apartment in Colombo 5, confirm she answers
   from the demo portfolio and offers a viewing rather than a human transfer.

## Risks

| Risk | Mitigation |
|---|---|
| Deploy from current `main` wipes prod's uncommitted router, breaking two live demos | Commit the recovered router first; verify the two hunks are present before any push |
| Demo shares a container with Rodrigo's live phone line | `DEFAULT_BRAND` keeps the phone path byte-identical; test 4 asserts it |
| VPS disk ~89% full, `docker compose build` fails with "No space left on device" | Reuse path needs no image build — Flico's compose volume-mounts `server.py`, so deploy is rsync + force-recreate |
| Token endpoint rate-limits 5 tokens/min/IP | Acceptable for demo traffic; documented, not changed |
