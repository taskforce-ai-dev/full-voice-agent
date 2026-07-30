"""Brand registry — one agent process serves more than one storefront.

"rodrigo" is the real client on the real phone number. "starproperties" is the
website Book-a-Demo persona; it shares this server, the retrieval engine and the
synthetic demo portfolio, but presents a different agency and agent name and has
NO human consultant to transfer to.

The brand key MUST equal the `id` of the matching agent in the website's
BookDemo.tsx — that id is what Twilio forwards and what Hatton's DEMO_AGENT_HOSTS
routes on. It was briefly "startproperty" here while the live site already said
"starproperties", and the mismatch sent every demo call to the hotel agent.

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
    "starproperties": {
        "agency": "Star Properties",
        "agent": "Amaya",
        "transfer": False,
        "greeting": {
            "en": (
                "Thank you for calling Star Properties — this is Amaya. "
                "How can I help you today?"
            ),
        },
    },
}

DEFAULT_BRAND: str = "rodrigo"


def resolve_brand(brand: str | None) -> dict:
    """Return the brand config, falling back to the default for anything unknown."""
    return BRANDS.get((brand or "").strip().lower(), BRANDS[DEFAULT_BRAND])
