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
