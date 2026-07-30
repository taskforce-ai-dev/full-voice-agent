"""The prompt must brand as Star Properties, and gate the handoff by entry point.

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

PHONE = "starproperties"
DEMO = "starproperties_demo"

# The persona opener, verbatim. This is the sentence the agent behaves as.
OPENER = (
    "You are Amaya, a warm, confident, top-performing SALES consultant for "
    "Star Properties, a trusted Sri Lankan real estate agency that helps people rent "
)


def test_default_arg_matches_the_explicit_phone_entry_point():
    # Environment-independent: both sides are generated in this same process,
    # so this holds on either KB backend.
    assert server._build_system_prompt("en") == server._build_system_prompt("en", PHONE)


def test_opener_is_verbatim():
    assert OPENER in server._build_system_prompt("en")


@pytest.mark.parametrize("brand", [None, PHONE, DEMO])
def test_every_entry_point_brands_as_star_properties(brand):
    out = (server._build_system_prompt("en") if brand is None
           else server._build_system_prompt("en", brand))
    assert "Star Properties" in out
    assert "Amaya" in out


@pytest.mark.parametrize("brand", [None, PHONE, DEMO])
def test_the_retired_brand_is_gone(brand):
    out = (server._build_system_prompt("en") if brand is None
           else server._build_system_prompt("en", brand))
    assert "Rodrigo" not in out
    assert "Fiona" not in out


@pytest.mark.parametrize("brand", [PHONE, DEMO])
def test_greeting_echo_matches_the_spoken_greeting(brand):
    # The prompt says the greeting was "already spoken". If it names different
    # words than Twilio speaks, the agent is briefed on what the caller never heard.
    out = server._build_system_prompt("en", brand)
    assert brands.BRANDS[brand]["greeting"]["en"] in out


def test_registry_greeting_matches_language_config():
    # The registry duplicates this string (it cannot import server -- that would
    # be circular). If they drift, the prompt describes one greeting while
    # Twilio speaks another.
    assert (brands.BRANDS[PHONE]["greeting"]["en"]
            == server.LANGUAGE_CONFIGS["en"]["welcome_greeting"])


def test_unknown_entry_point_falls_back_to_the_phone_default():
    assert server._build_system_prompt("en", "nope") == server._build_system_prompt("en")


def test_demo_prompt_omits_the_handoff_offer():
    # transfer_to_human appears only inside the handoff block, which must not be
    # emitted for an entry point with nobody behind it.
    assert "transfer_to_human" not in server._build_system_prompt("en", DEMO)


def test_phone_prompt_keeps_the_handoff_offer():
    # HUMAN_AGENT_PHONE is configured in production; the live line must keep it.
    assert "transfer_to_human" in server._build_system_prompt("en", PHONE)


def test_entry_points_differ_only_by_the_handoff_block():
    # Past the greeting, the two entry points must give byte-identical guidance.
    # The prompt is assembled persona -> language_rules -> handoff_rules ->
    # portfolio_facts -> GREETING -> anchor -> shared guidance, so the handoff
    # block (absent for the demo) falls BEFORE the anchor.
    anchor = "NEVER ask for the caller's name or phone number at the start"
    phone = server._build_system_prompt("en", PHONE)
    demo = server._build_system_prompt("en", DEMO)
    assert anchor in phone and anchor in demo
    assert phone.split(anchor, 1)[1] == demo.split(anchor, 1)[1]
