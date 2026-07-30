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
