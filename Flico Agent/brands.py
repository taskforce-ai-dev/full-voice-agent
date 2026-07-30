"""Entry-point registry — one agency, two ways in.

There is exactly ONE brand now: **Star Properties**, agent **Amaya**. The
earlier two-brand setup (Rodrigo Realtors on the phone, Star Properties on the
website demo) was collapsed on the operator's instruction: "forget the
branding, the only brand is star properties."

What is still genuinely different is the **entry point**, and only in one
respect -- whether a human can be reached:

  starproperties        the live inbound phone line. HUMAN_AGENT_PHONE is
                        configured, so transfer_to_human is a real capability.
  starproperties_demo   the website Book-a-Demo call. Nobody is standing behind
                        it, so the agent must never offer a handoff it cannot
                        perform.

Both greet as Star Properties and both speak as Amaya. `transfer` is the ONLY
field that differs, and it is a property of the entry point, not of the brand.

DEFAULT_BRAND is load-bearing: every call site that passes only `lang` resolves
to the phone entry point, which is the one real callers use.

`starproperties` here is also the agent `id` in the website's BookDemo.tsx and
the key in Hatton's DEMO_AGENT_HOSTS. Those must agree -- a mismatch between
this key and the live site's id sent every demo call to the wrong agent on
2026-07-30. Fetch the website repo and read the live id before changing it.

This module imports NOTHING. The deploy-gating CI job installs only
pytest/numpy/pydantic and cannot import server, so keeping the registry
dependency-free is what puts it under the gate.
"""

_AGENCY = "Star Properties"
_AGENT = "Amaya"
_GREETING_EN = (
    "Thank you for calling Star Properties — this is Amaya. "
    "How can I help you today?"
)

BRANDS: dict[str, dict] = {
    "starproperties": {
        "agency": _AGENCY,
        "agent": _AGENT,
        # Live phone line: HUMAN_AGENT_PHONE is set, so the handoff is real.
        "transfer": True,
        "greeting": {
            # Must stay identical to LANGUAGE_CONFIGS["en"]["welcome_greeting"]
            # in server.py -- test_phone_greeting_matches_language_config pins it.
            "en": _GREETING_EN,
        },
    },
    "starproperties_demo": {
        "agency": _AGENCY,
        "agent": _AGENT,
        # Website demo: no human behind it. Never offer a transfer.
        "transfer": False,
        "greeting": {
            "en": _GREETING_EN,
        },
    },
}

DEFAULT_BRAND: str = "starproperties"


def resolve_brand(brand: str | None) -> dict:
    """Return the entry-point config, falling back to the default for anything unknown."""
    return BRANDS.get((brand or "").strip().lower(), BRANDS[DEFAULT_BRAND])
