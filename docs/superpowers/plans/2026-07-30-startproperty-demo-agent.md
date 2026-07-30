# Start Property (Amaya) Demo Agent Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the website's existing "Start Property / Amaya" demo card connect to a real real-estate voice agent instead of falling through to Tanya at Hatton Hills.

**Architecture:** Reuse the existing Rodrigo Realtors agent (the directory confusingly named `Flico Agent`) rather than cloning a service. Put the brand registry in a new dependency-free `brands.py`, add a `/voice/demo-incoming` endpoint that stamps `brand=startproperty`, and register the agent id in Hatton's demo router. Rodrigo's live phone line must stay byte-identical throughout, which the `DEFAULT_BRAND` default argument guarantees.

**Tech Stack:** Python 3.11 (prod) / 3.12.3 (dev), FastAPI, Twilio ConversationRelay, pytest + numpy + pydantic, React 19 + Vite + TypeScript (website).

**Spec:** `docs/superpowers/specs/2026-07-30-startproperty-demo-agent-design.md`

## Global Constraints

- **Prod runs Python 3.11; dev runs 3.12.3. No PEP 701 f-strings.** Never nest same-type quotes inside an f-string: `f"{d["k"]}"` compiles on 3.12 and is a **SyntaxError on 3.11**. Write `f'{d["k"]}'` as the existing code does. `server.py` imports `kb/prose.py`, so this is a hard outage, and local `py_compile` will not catch it.
- **`kb-verify` gates deploy, and it installs only `pytest numpy pydantic`.** Any change under `Flico Agent/` triggers `python -m pytest tests/ -q` on Python 3.11 from the `Flico Agent` working directory, and the `deploy` job requires it green. **`import server` fails in that environment** (no `httpx`, `fastapi`, `anthropic`), so any test importing `server` MUST be guarded with `pytest.importorskip` or it will error and block the deploy.
- **Locally, `server` only imports under the venv:** `/home/dev/full-voice-agent/.venv/bin/python` (symlink to `incoming/taskforce-ai/.venv`). Plain `python3` lacks `httpx`.
- **Pin `KB_BACKEND=sqlite` for any prompt snapshot.** The prompt embeds an inventory-derived facts block, so it differs by backend: 13,370 chars on sqlite (prod's setting) vs 12,595 on chroma (the local default, which omits the block entirely). An unpinned snapshot is not reproducible.
- **Always `python -m pytest`**, never bare `pytest` — CWD must be importable for the `tests/` oracle package to resolve.
- **Rodrigo's live phone line is a real client's number.** No task may change what `_build_system_prompt(lang)` returns when called without a `brand` argument.
- Branch: `feature/startproperty-demo-agent` (already created off `origin/main`).
- Do **not** push to `main` until Task 5. Pushing `main` auto-deploys.
- Agency display name is exactly `Start Property`; agent display name is exactly `Amaya`. Brand id / agent id is exactly `startproperty` (matches `BookDemo.tsx`).

## File Structure

| File | Responsibility |
|---|---|
| `Flico Agent/brands.py` | **Create.** Dependency-free brand registry: `BRANDS`, `DEFAULT_BRAND`, `resolve_brand()`. Stdlib only, so its tests run in CI. |
| `Flico Agent/tests/test_brands.py` | **Create.** Registry unit tests. Imports `brands` only — runs in CI, gates deploy. |
| `Flico Agent/server.py` | Modify: import `brands`, parameterize `_build_system_prompt`, add `/voice/demo-incoming`, thread `brand` through `_build_conversation_relay_twiml` and `ws_conversation`. |
| `Flico Agent/tests/test_prompt_branding.py` | **Create.** Prompt invariance guard. Imports `server` — `importorskip`-guarded, skips in CI. |
| `Flico Agent/tests/test_demo_endpoint.py` | **Create.** Endpoint TwiML tests. Imports `server` + `TestClient` — `importorskip`-guarded. |
| `HattonHills/server.py` | Modify: recover prod's `DEMO_AGENT_HOSTS` + router hunks, add `startproperty`. |
| `Taskforce_AI_Website/components/pages/BookDemo.tsx` | Modify: one false `trainedOn` bullet. |
| `Flico Agent/CLAUDE.md`, `HattonHills/CLAUDE.md` | Modify: document the registry and the recovered router. |

**Why `brands.py` is a separate module:** the deploy gate cannot import `server`. Keeping the registry dependency-free means the brand data and its fallback semantics are covered by the suite that actually blocks a bad deploy, instead of being skipped.

---

### Task 1: Recover Hatton's production demo router into git

This task ships no new feature. It captures code that exists **only** in production so the next deploy cannot destroy it. It must land before any other change to `HattonHills/`.

**Files:**
- Modify: `HattonHills/server.py` (insert after line 266; modify `voice_demo_incoming` at ~line 1197)

**Interfaces:**
- Consumes: nothing.
- Produces: `DEMO_AGENT_HOSTS: dict[str, str]` — module-level, maps a `BookDemo.tsx` agent id to that agent's public host.

- [ ] **Step 1: Confirm prod still matches the captured diff**

The capture was taken 2026-07-30 11:41 UTC. Re-verify nothing changed since, because this task commits prod's version verbatim.

```bash
cd /home/dev/full-voice-agent
scp root@67.207.90.109:/opt/hatton-hills/server.py /tmp/hatton-prod.py
diff -u <(git show origin/main:HattonHills/server.py) /tmp/hatton-prod.py > /tmp/hatton.diff
grep -c '^@@' /tmp/hatton.diff
```

Expected: exactly `2`. If it is not 2, **stop** — prod drifted and the new hunks must be reviewed before committing.

- [ ] **Step 2: Add `DEMO_AGENT_HOSTS` after `DIGIT_TO_LANG`**

Insert immediately after the `DIGIT_TO_LANG: dict[str, str] = {"1": "en", "2": "ar"}` line in `HattonHills/server.py`. This is prod's block verbatim, plus the new `startproperty` entry:

```python
# Website demo routing: BookDemo.tsx agent ids → that agent's public host.
# All demos mint tokens for one shared TwiML app whose voiceUrl is THIS
# server's /voice/demo-incoming; non-Hatton agents are <Redirect>ed to their
# own /voice/demo-incoming (see voice_demo_incoming). 'hatton' stays local.
DEMO_AGENT_HOSTS: dict[str, str] = {
    "kitchened": os.getenv("DEMO_HOST_KITCHENED", "kitchened.taskforceai.tech"),
    "worldofrefrigerators": os.getenv(
        "DEMO_HOST_WOR", "worldofrefrigerators.taskforceai.tech"),
    # Start Property (Amaya) is served by the Rodrigo Realtors agent, whose
    # deployment is still named "flico" for historical reasons.
    "startproperty": os.getenv(
        "DEMO_HOST_STARTPROPERTY", "flico.taskforceai.tech"),
}
```

- [ ] **Step 3: Replace the `lang` parsing in `voice_demo_incoming` to also read `agent`**

Find this block inside `voice_demo_incoming`:

```python
    # lang can arrive as a query param (GET) or form field (POST). Default en.
    lang = (request.query_params.get("lang") or "").strip().lower()
    if not lang and request.method == "POST":
        try:
            form = await request.form()
            lang = str(form.get("lang", "")).strip().lower()
        except Exception:
            lang = ""
```

Replace it with prod's version:

```python
    # lang/agent can arrive as query params (GET) or form fields (POST).
    lang = (request.query_params.get("lang") or "").strip().lower()
    agent = (request.query_params.get("agent") or "").strip().lower()
    if request.method == "POST" and not (lang and agent):
        try:
            form = await request.form()
            lang = lang or str(form.get("lang", "")).strip().lower()
            agent = agent or str(form.get("agent", "")).strip().lower()
        except Exception:
            pass
```

- [ ] **Step 4: Add the redirect branch**

Insert immediately after the `if lang not in ("en", "ar", "ru", "si"): lang = "en"` block and **before** the `if lang in ("ar", "si"):` branch:

```python
    # All website demo agents share ONE TwiML app whose voiceUrl points here,
    # and the site passes the chosen agent via Device.connect params (Twilio
    # forwards them as POST fields on this FIRST webhook only). For a
    # non-Hatton agent, hand the call to that agent's own demo endpoint with a
    # <Redirect> — lang goes in the query string because Twilio does NOT
    # re-send custom connect params on redirected webhooks.
    other = DEMO_AGENT_HOSTS.get(agent)
    if other and other != host:
        twiml = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            "<Response>\n"
            f'  <Redirect method="POST">https://{other}/voice/demo-incoming?lang={lang}</Redirect>\n'
            "</Response>"
        )
        logger.info("Demo incoming call from %s — redirecting to agent %r (%s)",
                    request.headers.get("x-forwarded-for", "unknown"), agent, other)
        return Response(content=twiml, media_type="application/xml")
```

- [ ] **Step 5: Verify the file compiles and the only diff vs prod is the new route**

```bash
cd /home/dev/full-voice-agent
python3 -m py_compile HattonHills/server.py && echo COMPILE_OK
diff -u /tmp/hatton-prod.py HattonHills/server.py
```

Expected: `COMPILE_OK`, and the diff shows **only** the added `startproperty` entry and its two comment lines. Any other difference means the recovery is not faithful.

- [ ] **Step 6: Commit**

```bash
cd /home/dev/full-voice-agent
git add HattonHills/server.py
git commit -m "fix(hatton): recover demo agent router from prod, add startproperty

The /voice/demo-incoming agent router existed only on the VPS — it was
hot-patched and never committed, so a deploy from main would have wiped it
and broken the Kitchen & Co. and World of Refrigerators demos.

Captured verbatim by diffing /opt/hatton-hills/server.py against origin/main
(two hunks, router only, no other drift), then adds the startproperty route
pointing at the Rodrigo Realtors agent."
```

---

### Task 2: Dependency-free brand registry

**Files:**
- Create: `Flico Agent/brands.py`
- Test: `Flico Agent/tests/test_brands.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `BRANDS: dict[str, dict]` — keys `"rodrigo"`, `"startproperty"`. Each value has `agency: str`, `agent: str`, `transfer: bool`, `greeting: dict[str, str]` keyed by lang code.
  - `DEFAULT_BRAND: str = "rodrigo"`.
  - `resolve_brand(brand: str | None) -> dict` — returns the config, falling back to `BRANDS[DEFAULT_BRAND]` for anything unknown, empty, or `None`.

- [ ] **Step 1: Write the failing test**

Create `Flico Agent/tests/test_brands.py`. Note it imports `brands`, **not** `server` — this is what lets it run in CI and gate the deploy:

```python
"""The brand registry: one agent process, more than one storefront.

Imports `brands` only — never `server`. The deploy gate installs just
pytest/numpy/pydantic, so a test that imports server is skipped there and
cannot protect anything.
"""
import pytest

import brands


def test_default_brand_is_rodrigo():
    # Rodrigo is the real client on the real phone number. If the default ever
    # changes, that number silently rebrands.
    assert brands.DEFAULT_BRAND == "rodrigo"


def test_rodrigo_identity():
    b = brands.BRANDS["rodrigo"]
    assert b["agency"] == "Rodrigo Realtors"
    assert b["agent"] == "Fiona"


def test_startproperty_identity():
    b = brands.BRANDS["startproperty"]
    assert b["agency"] == "Start Property"
    assert b["agent"] == "Amaya"


@pytest.mark.parametrize("brand,expected", [("rodrigo", True), ("startproperty", False)])
def test_transfer_flag(brand, expected):
    # Amaya has no human consultant behind her; she must not offer a transfer.
    assert brands.BRANDS[brand]["transfer"] is expected


def test_every_brand_has_an_english_greeting():
    # _build_system_prompt falls back to ["en"], so its absence is a KeyError
    # at call time on a live call.
    for name, b in brands.BRANDS.items():
        assert b["greeting"]["en"].strip(), name


def test_every_brand_greeting_names_its_own_agency():
    # A greeting naming the wrong agency is the exact bug this registry exists
    # to prevent.
    for name, b in brands.BRANDS.items():
        assert b["agency"] in b["greeting"]["en"], name


def test_every_brand_declares_all_keys():
    for name, b in brands.BRANDS.items():
        assert set(b) == {"agency", "agent", "transfer", "greeting"}, name


@pytest.mark.parametrize("value", ["nope", "", "   ", None, "RODRIGO"])
def test_resolve_brand_falls_back_or_normalizes(value):
    got = brands.resolve_brand(value)
    if value and value.strip().lower() in brands.BRANDS:
        assert got is brands.BRANDS[value.strip().lower()]
    else:
        assert got is brands.BRANDS[brands.DEFAULT_BRAND]


def test_resolve_brand_returns_the_startproperty_config():
    assert brands.resolve_brand("startproperty") is brands.BRANDS["startproperty"]
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
cd "/home/dev/full-voice-agent/Flico Agent" && python -m pytest tests/test_brands.py -q
```

Expected: FAIL — `ModuleNotFoundError: No module named 'brands'`.

- [ ] **Step 3: Create `brands.py`**

Create `Flico Agent/brands.py`. Stdlib only — add no imports:

```python
"""Brand registry — one agent process serves more than one storefront.

"rodrigo" is the real client on the real phone number. "startproperty" is the
website Book-a-Demo persona; it shares this server, the retrieval engine and the
synthetic demo portfolio, but presents a different agency and agent name and has
NO human consultant to transfer to.

DEFAULT_BRAND is load-bearing: every pre-existing call site passes only `lang`,
so the default is what keeps the live phone line byte-identical.

This module imports NOTHING. The deploy-gating CI job installs only
pytest/numpy/pydantic and cannot import server, so keeping the registry
dependency-free is what puts it under the gate.
"""

BRANDS: dict[str, dict] = {
    "rodrigo": {
        "agency": "Rodrigo Realtors",
        "agent": "Fiona",
        "transfer": True,
        "greeting": {
            # Must stay identical to LANGUAGE_CONFIGS["en"]["welcome_greeting"]
            # in server.py — test_rodrigo_greeting_matches_language_config pins it.
            "en": (
                "You have reached Rodrigo Realtors — you are speaking with "
                "our virtual property consultant. How can I help you today?"
            ),
        },
    },
    "startproperty": {
        "agency": "Start Property",
        "agent": "Amaya",
        "transfer": False,
        "greeting": {
            "en": (
                "Thank you for calling Start Property — this is Amaya. "
                "How can I help you today?"
            ),
        },
    },
}

DEFAULT_BRAND: str = "rodrigo"


def resolve_brand(brand: str | None) -> dict:
    """Return the brand config, falling back to the default for anything unknown."""
    return BRANDS.get((brand or "").strip().lower(), BRANDS[DEFAULT_BRAND])
```

- [ ] **Step 4: Run the tests**

```bash
cd "/home/dev/full-voice-agent/Flico Agent" && python -m pytest tests/test_brands.py -q
```

Expected: PASS — 14 test cases (7 plain + 2 from `test_transfer_flag` + 5 from `test_resolve_brand_falls_back_or_normalizes`).

- [ ] **Step 5: Run the full deploy-gating suite**

```bash
cd "/home/dev/full-voice-agent/Flico Agent" && python -m pytest tests/ -q
```

Expected: PASS with no new failures (`test_demo_portfolio_lookups.py` skips without sentence-transformers, as before).

- [ ] **Step 6: Commit**

```bash
cd /home/dev/full-voice-agent
git add "Flico Agent/brands.py" "Flico Agent/tests/test_brands.py"
git commit -m "feat(rodrigo): dependency-free brand registry

Adds BRANDS/DEFAULT_BRAND/resolve_brand in its own module with no imports, so
the registry is covered by the deploy-gating CI job — which installs only
pytest/numpy/pydantic and cannot import server.

Rodrigo is the real client on the real phone number; startproperty is the
website demo persona and sets transfer=False, having no consultant to hand
off to."
```

---

### Task 3: Parameterize the persona

**Files:**
- Modify: `Flico Agent/server.py` (import `brands` near the top; modify `_build_system_prompt` at line 332; condition at line 402)
- Test: `Flico Agent/tests/test_prompt_branding.py`

**Interfaces:**
- Consumes: `brands.BRANDS`, `brands.DEFAULT_BRAND`, `brands.resolve_brand` from Task 2.
- Produces: `_build_system_prompt(lang: str = "en", brand: str = DEFAULT_BRAND) -> str`.

- [ ] **Step 1: Write the failing test**

Create `Flico Agent/tests/test_prompt_branding.py`. The `importorskip` calls are mandatory — without them this file errors in CI and blocks the deploy:

```python
"""Branding must not change what the live phone line says.

Guarded: `server` needs httpx/fastapi/anthropic, which the deploy-gating CI job
does not install. Run locally with the venv:
    KB_BACKEND=sqlite /home/dev/full-voice-agent/.venv/bin/python -m pytest \
        tests/test_prompt_branding.py -q
"""
import pytest

pytest.importorskip("httpx")
pytest.importorskip("fastapi")
pytest.importorskip("anthropic")

import brands  # noqa: E402
import server  # noqa: E402

# The persona opener, verbatim from the pre-change implementation. This is the
# sentence a real client's callers hear the agent behave like.
RODRIGO_OPENER = (
    "You are Fiona, a warm, confident, top-performing SALES consultant for "
    "Rodrigo Realtors, a trusted Sri Lankan real estate agency that helps people rent "
)


def test_default_arg_matches_explicit_rodrigo():
    # Environment-independent: both sides are generated in this same process,
    # so this holds on either KB backend.
    assert server._build_system_prompt("en") == server._build_system_prompt("en", "rodrigo")


def test_rodrigo_opener_is_verbatim_unchanged():
    assert RODRIGO_OPENER in server._build_system_prompt("en")


def test_rodrigo_prompt_still_names_the_agency_and_agent():
    out = server._build_system_prompt("en")
    assert "Rodrigo Realtors" in out
    assert "Fiona" in out


def test_startproperty_replaces_the_agency_and_agent_name():
    out = server._build_system_prompt("en", "startproperty")
    assert "Start Property" in out
    assert "Amaya" in out
    assert "Rodrigo Realtors" not in out
    assert "Fiona" not in out


def test_startproperty_greeting_echo_matches_the_spoken_greeting():
    # The prompt says the greeting was "already spoken". If it names different
    # words than Twilio speaks, the agent is briefed on what the caller never heard.
    out = server._build_system_prompt("en", "startproperty")
    assert brands.BRANDS["startproperty"]["greeting"]["en"] in out


def test_rodrigo_greeting_echo_matches_the_spoken_greeting():
    out = server._build_system_prompt("en")
    assert brands.BRANDS["rodrigo"]["greeting"]["en"] in out


def test_rodrigo_greeting_matches_language_config():
    # The registry duplicates this string; if they drift, the prompt tells the
    # agent one greeting while Twilio speaks another.
    assert (brands.BRANDS["rodrigo"]["greeting"]["en"]
            == server.LANGUAGE_CONFIGS["en"]["welcome_greeting"])


def test_unknown_brand_falls_back_to_default():
    assert server._build_system_prompt("en", "nope") == server._build_system_prompt("en")


def test_startproperty_prompt_omits_the_handoff_offer():
    # transfer_to_human appears only inside the handoff block (server.py:406,411),
    # which must not be emitted for a brand with transfer=False.
    assert "transfer_to_human" not in server._build_system_prompt("en", "startproperty")


def test_rodrigo_prompt_keeps_the_handoff_offer():
    assert "transfer_to_human" in server._build_system_prompt("en")


def test_only_brand_tokens_differ():
    # The strongest guard: past the greeting block, swapping the brand must
    # change ONLY the agency name — not a character of actual guidance.
    #
    # Anchoring after this phrase is what makes the comparison clean. The
    # prompt is assembled persona -> language_rules -> handoff_rules ->
    # portfolio_facts -> GREETING -> anchor -> shared guidance, so both the
    # handoff block (absent for startproperty) and the greeting (worded
    # differently per brand, not just name-swapped) fall BEFORE the anchor and
    # need no special handling.
    anchor = "NEVER ask for the caller's name or phone number at the start"
    rod = server._build_system_prompt("en")
    sp = server._build_system_prompt("en", "startproperty")
    normalized = sp.replace("Start Property", "Rodrigo Realtors").replace("Amaya", "Fiona")
    assert anchor in rod and anchor in normalized
    assert normalized.split(anchor, 1)[1] == rod.split(anchor, 1)[1]
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
cd "/home/dev/full-voice-agent/Flico Agent"
KB_BACKEND=sqlite /home/dev/full-voice-agent/.venv/bin/python -m pytest tests/test_prompt_branding.py -q
```

Expected: FAIL — `TypeError: _build_system_prompt() takes 1 positional argument but 2 were given` on the brand tests. The two `test_rodrigo_*` verbatim tests should already PASS.

- [ ] **Step 3: Confirm the same file SKIPS cleanly under the CI environment**

This is the check that protects the deploy gate.

```bash
cd "/home/dev/full-voice-agent/Flico Agent" && python -m pytest tests/test_prompt_branding.py -q
```

Expected: `s` / "skipped" — **never** an error or a collection failure. If it errors, the `importorskip` guards are wrong and merging would block deploys.

- [ ] **Step 4: Import the registry in `server.py`**

Add beside the other local imports near the top of `Flico Agent/server.py`:

```python
from brands import BRANDS, DEFAULT_BRAND, resolve_brand
```

- [ ] **Step 5: Parameterize `_build_system_prompt`**

Change the signature at line 332 and add the brand locals:

```python
def _build_system_prompt(lang: str = "en", brand: str = DEFAULT_BRAND) -> str:
    today = date.today().isoformat()
    _brand = resolve_brand(brand)
    agency = _brand["agency"]
    agent_name = _brand["agent"]
    greeting = _brand["greeting"].get(lang) or _brand["greeting"]["en"]
```

Gate the handoff block — change **only the condition on line 402**:

```python
    handoff_rules = ""
    if lang == "en":                      # <-- before
```

becomes:

```python
    handoff_rules = ""
    if lang == "en" and _brand["transfer"]:
```

Leave the assignment body (lines 403–413) untouched.

Replace the persona opener (lines 416–417):

```python
        f"You are {agent_name}, a warm, confident, top-performing SALES consultant for "
        f"{agency}, a trusted Sri Lankan real estate agency that helps people rent "
```

Replace the greeting echo (lines 437–440) so it *derives* from the brand and cannot drift:

```python
        "GREETING & CALLER DETAILS:\n"
        f"- The opening greeting is already spoken automatically: '{greeting}' "
        "Do NOT repeat it.\n"
```

For the six remaining sites (lines 445, 526, 580, 584, 587, 594) replace the literal `Rodrigo Realtors` with `{agency}` and make each string an f-string. Example, line 445:

```python
        f"anything. Instead, a {agency} salesperson personally contacts the "
```

**Leave line 372 alone** — a Sinhala transliteration glossary entry inside the `si` branch; this demo is English-only. **Leave the line-431 comment alone.**

- [ ] **Step 6: Verify the 3.11 f-string constraint**

```bash
cd "/home/dev/full-voice-agent/Flico Agent"
grep -nE 'f"[^"]*"[a-zA-Z_]+"' server.py | head
```

Expected: no new hits. Existing `f'...["key"]...'` single-quoted forms are fine.

- [ ] **Step 7: Run the branding tests**

```bash
cd "/home/dev/full-voice-agent/Flico Agent"
KB_BACKEND=sqlite /home/dev/full-voice-agent/.venv/bin/python -m pytest tests/test_prompt_branding.py -q
```

Expected: PASS, 11 tests. `test_only_brand_tokens_differ` is the important one — it proves the swap changed nothing but the brand names and the handoff block.

- [ ] **Step 8: Confirm the CI-visible suite is still green and still skips cleanly**

```bash
cd "/home/dev/full-voice-agent/Flico Agent" && python -m pytest tests/ -q
```

Expected: PASS with `test_prompt_branding.py` skipped, no errors.

- [ ] **Step 9: Commit**

```bash
cd /home/dev/full-voice-agent
git add "Flico Agent/server.py" "Flico Agent/tests/test_prompt_branding.py"
git commit -m "feat(rodrigo): thread brand through the system prompt

_build_system_prompt(lang, brand=DEFAULT_BRAND) substitutes the agency and
agent name at its 9 hardcoded sites, and the greeting echo now derives from
the brand's greeting so the two cannot drift. The handoff block is gated on
the brand's transfer flag.

The default argument is what keeps Rodrigo's live phone line byte-identical:
every pre-existing call site passes only lang. test_only_brand_tokens_differ
proves a brand swap changes nothing but the names and the handoff block."
```

---

### Task 4: `/voice/demo-incoming` on the Rodrigo agent

**Files:**
- Modify: `Flico Agent/server.py` (env var near line 91; `_build_conversation_relay_twiml` at line 1037; new endpoint after `/voice/language-selected` at ~line 989; `ws_conversation` at line 2438)
- Test: `Flico Agent/tests/test_demo_endpoint.py`

**Interfaces:**
- Consumes: `brands` (Task 2), `_build_system_prompt(lang, brand)` (Task 3).
- Produces: `GET|POST /voice/demo-incoming` returning ConversationRelay TwiML; `_build_conversation_relay_twiml(host, lang, config, brand=DEFAULT_BRAND, voice=None)`; `ws_conversation(websocket, lang="en", brand=DEFAULT_BRAND)`.

- [ ] **Step 1: Write the failing test**

Create `Flico Agent/tests/test_demo_endpoint.py`:

```python
"""The website demo entry point must brand itself and never disturb the IVR.

Guarded — see test_prompt_branding.py. TestClient additionally needs httpx.
"""
import pytest

pytest.importorskip("httpx")
pytest.importorskip("fastapi")
pytest.importorskip("anthropic")

from fastapi.testclient import TestClient  # noqa: E402

import server  # noqa: E402


@pytest.fixture
def client():
    return TestClient(server.app, raise_server_exceptions=True)


def test_demo_incoming_returns_conversation_relay(client):
    r = client.post("/voice/demo-incoming", data={"lang": "en"})
    assert r.status_code == 200
    assert "<ConversationRelay" in r.text


def test_demo_incoming_brands_as_startproperty(client):
    r = client.post("/voice/demo-incoming", data={"lang": "en"})
    assert "Start Property" in r.text
    assert "Amaya" in r.text
    assert "Rodrigo Realtors" not in r.text


def test_demo_incoming_passes_brand_on_the_websocket_url(client):
    # ws_conversation needs the brand, and Twilio re-sends connect params on
    # neither a redirected webhook nor the WebSocket handshake — so it must
    # ride the query string.
    r = client.post("/voice/demo-incoming", data={"lang": "en"})
    assert "brand=startproperty" in r.text


def test_demo_incoming_escapes_the_query_ampersand(client):
    # A bare & is invalid XML and Twilio rejects the TwiML outright.
    r = client.post("/voice/demo-incoming", data={"lang": "en"})
    assert "&amp;brand=" in r.text
    assert "?lang=en&brand=" not in r.text


def test_demo_incoming_collapses_unknown_lang_to_english(client):
    # The demo card declares langs: ['en'] only.
    for lang in ("", "ta", "si", "zz"):
        r = client.post("/voice/demo-incoming", data={"lang": lang})
        assert r.status_code == 200, lang
        assert 'language="en-US"' in r.text, lang
        assert "<Stream" not in r.text, lang


def test_demo_incoming_accepts_get_query_params(client):
    r = client.get("/voice/demo-incoming?lang=en")
    assert r.status_code == 200
    assert "Start Property" in r.text


def test_demo_incoming_twiml_is_well_formed_xml(client):
    # Catches the bare-& class of bug, which Twilio rejects outright.
    #
    # stdlib ElementTree is the right tool here despite its XXE/billion-laughs
    # exposure: the input is a string this same process just built from literals
    # in server.py — no external input, no DTD, no entities. Using defusedxml
    # would add a dependency the deploy-gating CI job does not install.
    import xml.etree.ElementTree as ET
    r = client.post("/voice/demo-incoming", data={"lang": "en"})
    ET.fromstring(r.text)  # raises on malformed XML


def test_phone_ivr_is_untouched(client):
    # The real client's inbound path must still render the DTMF menu.
    r = client.post("/voice/incoming")
    assert r.status_code == 200
    assert "<Gather" in r.text
    assert "Start Property" not in r.text
    assert "Amaya" not in r.text


def test_relay_twiml_defaults_to_rodrigo(client):
    # Existing callers pass three positional args; they must keep Rodrigo.
    out = server._build_conversation_relay_twiml(
        "example.test", "en", server.LANGUAGE_CONFIGS["en"])
    assert "Rodrigo Realtors" in out
    assert "Start Property" not in out
    assert "brand=rodrigo" in out
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
cd "/home/dev/full-voice-agent/Flico Agent"
KB_BACKEND=sqlite /home/dev/full-voice-agent/.venv/bin/python -m pytest tests/test_demo_endpoint.py -q
```

Expected: FAIL — demo tests get 404. `test_phone_ivr_is_untouched` should already PASS.

- [ ] **Step 3: Add the voice override env var**

Add beside `ELEVENLABS_VOICE_ID` (~line 91) in `Flico Agent/server.py`:

```python
# Distinct ConversationRelay voice for the Start Property demo persona, so the
# two demos on the website do not sound like the same person. Falls back to the
# language default when unset.
CR_VOICE_STARTPROPERTY: str = os.getenv("CR_VOICE_STARTPROPERTY", "").strip()
```

- [ ] **Step 4: Make `_build_conversation_relay_twiml` brand-aware**

Replace the function at line 1037:

```python
def _build_conversation_relay_twiml(
    host: str, lang: str, config: dict[str, str],
    brand: str = DEFAULT_BRAND, voice: str | None = None,
) -> str:
    """Build the <ConversationRelay> XML tag for the given language config.

    `brand` rides the WebSocket query string because Twilio re-sends
    Device.connect params on neither a redirected webhook nor the WebSocket
    handshake. `voice` overrides the language default so a second brand can
    sound like a different person.
    """
    extra = config["extra_attrs"]
    _brand = resolve_brand(brand)
    greeting_text = _brand["greeting"].get(lang) or config["welcome_greeting"]
    # XML-escape in case the greeting contains special characters
    greeting = xml.sax.saxutils.escape(greeting_text)
    ws_voice = voice or config["voice"]
    brand_key = brand if brand in BRANDS else DEFAULT_BRAND

    return (
        f'<ConversationRelay url="wss://{host}/ws/conversation?lang={lang}&amp;brand={brand_key}"\n'
        f'        ttsProvider="{config["tts_provider"]}"\n'
        f'        voice="{ws_voice}"\n'
        f'{extra}'
        f'        language="{config["language"]}"\n'
        f'        transcriptionProvider="google"\n'
        f'        speechModel="telephony"\n'
        f'        welcomeGreeting="{greeting}"\n'
        '        interruptible="true"\n'
        '        dtmfDetection="true">\n'
        "    </ConversationRelay>"
    )
```

`&amp;` is required — this is XML, and a bare `&` makes Twilio reject the document. Existing three-arg callers get `brand=DEFAULT_BRAND`, whose `rodrigo` greeting equals `LANGUAGE_CONFIGS["en"]["welcome_greeting"]`, so the phone path is unchanged.

- [ ] **Step 5: Add the endpoint**

Insert after the `/voice/language-selected` handler:

```python
@app.api_route("/voice/demo-incoming", methods=["GET", "POST"])
async def voice_demo_incoming(request: Request) -> Response:
    """Website Book-a-Demo entry — Start Property (Amaya), English, no IVR.

    Hatton's shared TwiML app <Redirect>s here for agent id 'startproperty'.
    Unlike the phone IVR there is no <Gather>: the demo card declares
    langs: ['en'], so everything collapses to English ConversationRelay.
    """
    host = request.headers.get("host", request.url.hostname or "localhost")

    config = LANGUAGE_CONFIGS["en"]
    cr = _build_conversation_relay_twiml(
        host, "en", config,
        brand="startproperty",
        voice=CR_VOICE_STARTPROPERTY or None,
    )
    twiml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<Response>\n"
        "  <Connect>\n"
        f"    {cr}\n"
        "  </Connect>\n"
        "</Response>"
    )
    logger.info("Demo incoming call from %s — Start Property (en)",
                request.headers.get("x-forwarded-for", "unknown"))
    return Response(content=twiml, media_type="application/xml")
```

- [ ] **Step 6: Thread `brand` through `ws_conversation`**

Change the signature at lines 2438–2439:

```python
@app.websocket("/ws/conversation")
async def ws_conversation(websocket: WebSocket, lang: str = "en", brand: str = DEFAULT_BRAND):
```

Then replace:

```python
    system_prompt: str = _build_system_prompt(lang)
    tools: list[dict] = [TRANSFER_TOOL] if lang == "en" else []
```

with:

```python
    _brand = resolve_brand(brand)
    system_prompt: str = _build_system_prompt(lang, brand)
    # Amaya has no human consultant behind her — never offer a transfer that
    # cannot be performed.
    tools: list[dict] = (
        [TRANSFER_TOOL] if (lang == "en" and _brand["transfer"]) else []
    )
```

And extend the existing acceptance log so calls are attributable:

```python
    logger.info("WebSocket connection accepted -- language: %s, brand: %s",
                lang, _brand["agency"])
```

- [ ] **Step 7: Run the endpoint and branding tests**

```bash
cd "/home/dev/full-voice-agent/Flico Agent"
KB_BACKEND=sqlite /home/dev/full-voice-agent/.venv/bin/python -m pytest \
  tests/test_demo_endpoint.py tests/test_prompt_branding.py tests/test_brands.py -q
```

Expected: PASS — 34 test cases (9 endpoint + 11 branding + 14 registry).

- [ ] **Step 8: Confirm the CI-visible suite is green with clean skips**

```bash
cd "/home/dev/full-voice-agent/Flico Agent" && python -m pytest tests/ -q
```

Expected: PASS. `test_demo_endpoint.py` and `test_prompt_branding.py` skip; `test_brands.py` runs and passes. No errors.

- [ ] **Step 9: Commit**

```bash
cd /home/dev/full-voice-agent
git add "Flico Agent/server.py" "Flico Agent/tests/test_demo_endpoint.py"
git commit -m "feat(rodrigo): add /voice/demo-incoming for the Start Property demo

English-only browser-demo entry point branded as Start Property (Amaya). The
brand rides the ConversationRelay WebSocket query string (XML-escaped &amp;)
because Twilio re-sends Device.connect params on neither a redirected webhook
nor the WebSocket handshake.

CR_VOICE_STARTPROPERTY optionally gives Amaya a distinct ElevenLabs voice,
falling back to the language default so a missing var degrades rather than
breaks. The phone IVR path is untouched."
```

---

### Task 5: Correct the website card's false claim

**Files:**
- Modify: `Taskforce_AI_Website/components/pages/BookDemo.tsx:225`

This is a separate git repository with its own remote. Pushing its `main` deploys the public site.

**Interfaces:** consumes nothing, produces nothing.

- [ ] **Step 1: Verify the claim is still false**

```bash
cd "/home/dev/full-voice-agent/Flico Agent"
python3 -c "
import json, collections
d = json.load(open('knowledge_docs/listings.json'))
rows = d if isinstance(d, list) else list(d.values())
print(collections.Counter(r.get('property_type') for r in rows))
print('furnishing:', collections.Counter(r.get('furnishing') for r in rows))
"
```

Expected: `Counter({'apartment': 6, 'house': 6})` — no `commercial`, confirming the bullet is wrong. Furnishing shows `furnished: 6, semi: 4, unfurnished: 2`, confirming the *furnished/unfurnished* bullet is accurate and must be left alone.

- [ ] **Step 2: Fix the bullet**

In `Taskforce_AI_Website/components/pages/BookDemo.tsx`, inside the `startproperty` agent's `trainedOn` array, change:

```tsx
      'Houses, apartments & commercial units',
```

to:

```tsx
      'Houses & apartments',
```

Leave every other bullet — all are backed by fields in the portfolio (`deposit_months`, `min_lease_months`, `advance_months`, `floor_area_sqft`, `key_features`, `furnishing`, `available`).

- [ ] **Step 3: Verify the build**

```bash
cd /home/dev/full-voice-agent/Taskforce_AI_Website && npx tsc --noEmit
```

Expected: no errors. If `tsc` reports pre-existing unrelated errors, confirm they also occur on a clean checkout before proceeding.

- [ ] **Step 4: Commit — do not push**

```bash
cd /home/dev/full-voice-agent/Taskforce_AI_Website
git add components/pages/BookDemo.tsx
git commit -m "Fix Start Property demo card: no commercial units in the portfolio

The card advertised 'Houses, apartments & commercial units' but the demo
portfolio is residential only (6 apartments, 6 houses). An agent that denies
a category the page promises reads as broken."
```

---

### Task 6: Deploy and verify end to end

Nothing here is reversible by a code edit alone, so verify at each gate.

**Files:** none modified until Step 11.

- [ ] **Step 1: Confirm both suites and both files are clean**

```bash
cd "/home/dev/full-voice-agent/Flico Agent"
python -m pytest tests/ -q                                    # CI-equivalent
KB_BACKEND=sqlite /home/dev/full-voice-agent/.venv/bin/python -m pytest tests/ -q   # full
cd /home/dev/full-voice-agent && python3 -m py_compile HattonHills/server.py \
  "Flico Agent/server.py" "Flico Agent/brands.py" && echo COMPILE_OK
```

Expected: both suites PASS, `COMPILE_OK`.

- [ ] **Step 2: Verify the 3.11 syntax gate the way CI will**

This box is 3.12; `py_compile` here cannot catch a PEP 701 f-string.

```bash
cd "/home/dev/full-voice-agent/Flico Agent"
(python3.11 -m py_compile server.py brands.py && echo OK_311) \
  || echo "no local 3.11 — the kb-verify CI job in Step 4 is the gate"
```

If no local 3.11 exists, **do not skip** — watch CI in Step 4 before touching prod.

- [ ] **Step 3: Merge to `main` and push**

```bash
cd /home/dev/full-voice-agent
git checkout main && git merge --no-ff feature/startproperty-demo-agent
git push origin main
```

This auto-deploys both changed agents in "fast" mode (rsync + hot-swap + restart), gated on `kb-verify`.

- [ ] **Step 4: Watch the deploy and the CI gate**

```bash
cd /home/dev/full-voice-agent && gh run list --limit 3 && gh run watch
```

Expected: `kb-verify` succeeds and `deploy` runs for both `hatton` and `flico`. If `kb-verify` fails, the deploy is correctly blocked — fix forward, never bypass.

**Note:** verify `brands.py` actually reached the container. The fast path rsyncs changed `.py` files, but `/opt/flico` volume-mounts only `server.py` and `knowledge_base.py`. If `brands.py` did not land, `server.py` will fail to import and the container will crash-loop:

```bash
ssh root@67.207.90.109 "ls -l /opt/flico/brands.py && docker compose -f /opt/flico/docker-compose.yml logs --tail=30 flico"
curl -s https://flico.taskforceai.tech/health
```

Expected: the file exists, no `ModuleNotFoundError: No module named 'brands'` in the logs, and `/health` returns 200. If it is missing, `scp` it to `/opt/flico/` and add it to the compose volume list, then force-recreate.

- [ ] **Step 5: Verify the router and the new endpoint live**

```bash
echo "--- hatton routes startproperty to flico ---"
curl -s -X POST -d "agent=startproperty&lang=en" \
  https://hattonhills.taskforceai.tech/voice/demo-incoming

echo "--- flico serves Start Property ---"
curl -s -X POST -d "lang=en" https://flico.taskforceai.tech/voice/demo-incoming
```

Expected: the first returns a `<Redirect>` to `flico.taskforceai.tech`; the second returns ConversationRelay TwiML naming Start Property and Amaya, with `&amp;brand=startproperty` in the WebSocket URL.

- [ ] **Step 6: Verify no regressions on the other demos or the real phone line**

```bash
for a in hatton kitchened worldofrefrigerators; do
  echo "--- $a ---"
  curl -s -X POST -d "agent=$a&lang=en" \
    https://hattonhills.taskforceai.tech/voice/demo-incoming | head -5
done

echo "--- Rodrigo's phone IVR must be unchanged ---"
curl -s -X POST https://flico.taskforceai.tech/voice/incoming | head -10
```

Expected: `kitchened` and `worldofrefrigerators` still `<Redirect>` to their own hosts; `hatton` still serves Tanya locally; the IVR still renders its `<Gather>` menu naming Rodrigo Realtors, with no mention of Start Property.

- [ ] **Step 7: Set Amaya's distinct voice (optional — needs a chosen ElevenLabs voice id)**

```bash
ssh root@67.207.90.109 "cd /opt/flico && cp .env .env.bak.\$(date +%s) && \
  echo 'CR_VOICE_STARTPROPERTY=<voice_id>-flash_v2_5' >> .env && \
  docker compose up -d --force-recreate flico"
```

Skip if no distinct voice has been chosen — the fallback keeps Amaya on the default English voice. `/opt/flico` volume-mounts `server.py`, so this force-recreate needs no image build (the VPS disk is ~89% full and `docker compose build` fails there).

- [ ] **Step 8: Deploy the website**

```bash
cd /home/dev/full-voice-agent/Taskforce_AI_Website && git push origin main
```

This ships the public site. If the build fails fetching `wp.taskforceai.tech`, that host intermittently times out from GitHub's US runners — re-run the workflow, it is not a code fault.

- [ ] **Step 9: Live call check**

On `https://taskforceai.tech/book-demo/`, select **Start Property**, click call, and confirm:
1. Amaya greets as Start Property — not Hatton Hills, not Rodrigo Realtors.
2. "A two-bedroom apartment in Colombo 5" → she answers from the demo portfolio.
3. "Can I speak to a human?" → she must **not** offer a transfer; she should say a salesperson follows up.
4. "Do you have a commercial unit?" → she declines rather than inventing one.

Then call the **Hatton Hills** and **Kitchen & Co.** cards once each to confirm the shared router still routes them correctly.

- [ ] **Step 10: Run the answer eval more than once**

This was a prompt change, and the eval has previously caught an invented feature only on the third pass.

```bash
ssh root@67.207.90.109 "docker exec flico-voice-agent python evals/answer_eval.py"
```

Run at least twice. It costs money and is nondeterministic, which is why it is not in CI. It is QA, not proof — do not quote it as a guarantee.

- [ ] **Step 11: Update the docs**

Add to `Flico Agent/CLAUDE.md`: the `brands.py` registry and why it is dependency-free (the CI gate cannot import `server`); that `DEFAULT_BRAND` keeps the phone line identical; the new `/voice/demo-incoming`; `CR_VOICE_STARTPROPERTY`; and that `brands.py` must be present in `/opt/flico` or `server.py` fails to import.

Add to `HattonHills/CLAUDE.md`: `DEMO_AGENT_HOSTS`, that it was recovered from an uncommitted prod hot-patch on 2026-07-30, and that adding a website demo agent means adding an entry there.

Refresh the knowledge graph (this box is native Linux, so plain `update` is correct — not the WSL wrapper):

```bash
cd /home/dev/full-voice-agent && graphify update .
GRAPHIFY_VIZ_NODE_LIMIT=9000 graphify cluster-only .
```

The second command is required because the graph exceeds the 5,000-node viz limit and a plain update silently deletes `graph.html`.

- [ ] **Step 12: Commit the docs**

```bash
cd /home/dev/full-voice-agent
git add "Flico Agent/CLAUDE.md" HattonHills/CLAUDE.md graphify-out
git commit -m "docs: record the brand registry and the recovered Hatton demo router"
git push origin main
```

---

## Rollback

- **Website card:** `git revert` in `Taskforce_AI_Website` and push.
- **Agent code:** `git revert` the commit on `main` and push — auto-deploy ships the revert in fast mode.
- **Voice override:** restore `/opt/flico/.env.bak.<timestamp>` and force-recreate.
- **Emergency, no code change:** set `DEMO_HOST_STARTPROPERTY` to Hatton's host, or remove the `startproperty` key from `DEMO_AGENT_HOSTS`. The demo returns to today's fall-through behaviour without touching the Rodrigo agent at all.
