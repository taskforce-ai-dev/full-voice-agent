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


def test_demo_incoming_brands_as_starproperties(client):
    r = client.post("/voice/demo-incoming", data={"lang": "en"})
    assert "Star Properties" in r.text
    assert "Amaya" in r.text
    assert "Rodrigo Realtors" not in r.text


def test_demo_incoming_passes_brand_on_the_websocket_url(client):
    # ws_conversation needs the brand, and Twilio re-sends connect params on
    # neither a redirected webhook nor the WebSocket handshake — so it must
    # ride the query string.
    r = client.post("/voice/demo-incoming", data={"lang": "en"})
    assert "brand=starproperties" in r.text


def test_demo_incoming_escapes_the_query_ampersand(client):
    # A bare & is invalid XML and Twilio rejects the TwiML outright.
    r = client.post("/voice/demo-incoming", data={"lang": "en"})
    assert "&amp;brand=" in r.text
    assert "?lang=en&brand=" not in r.text


def test_demo_incoming_is_always_english_regardless_of_input(client):
    # voice_demo_incoming hardcodes "en" and never reads a lang field -- there
    # is no branching here to collapse. This pins that guarantee so a future
    # change that *added* lang branching to this endpoint would be caught.
    for lang in ("", "ta", "si", "zz"):
        r = client.post("/voice/demo-incoming", data={"lang": lang})
        assert r.status_code == 200, lang
        assert 'language="en-US"' in r.text, lang
        assert "<Stream" not in r.text, lang


def test_demo_incoming_accepts_get_query_params(client):
    r = client.get("/voice/demo-incoming?lang=en")
    assert r.status_code == 200
    assert "Star Properties" in r.text


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
    # The real client's inbound path (English-only ConversationRelay, no IVR
    # menu -- that's pre-existing, unrelated to this task) must keep behaving
    # exactly as before and must never carry the demo's branding.
    r = client.post("/voice/incoming")
    assert r.status_code == 200
    assert "<ConversationRelay" in r.text
    assert "Star Properties" not in r.text
    assert "Amaya" not in r.text


def test_relay_twiml_defaults_to_rodrigo(client):
    # Existing callers pass three positional args; they must keep Rodrigo.
    out = server._build_conversation_relay_twiml(
        "example.test", "en", server.LANGUAGE_CONFIGS["en"])
    assert "Rodrigo Realtors" in out
    assert "Star Properties" not in out
    assert "brand=rodrigo" in out


# ---------------------------------------------------------------------------
# _tools_for -- the safety property of the whole feature: Amaya has no human
# consultant behind her, so she must never be handed the transfer tool. This
# is the load-bearing test; it does not go through websocket_connect because
# ws_conversation blocks on messages after accept() and nothing else in this
# suite drives it.
# ---------------------------------------------------------------------------

def test_tools_for_rodrigo_includes_transfer(client):
    assert server._tools_for("en", "rodrigo") == [server.TRANSFER_TOOL]


def test_tools_for_no_brand_arg_defaults_to_rodrigo_with_transfer(client):
    # A phone call with no brand query param must still resolve to Rodrigo
    # with the transfer tool enabled.
    assert server._tools_for("en") == [server.TRANSFER_TOOL]


def test_tools_for_starproperties_has_no_transfer(client):
    assert server._tools_for("en", "starproperties") == []


def test_tools_for_non_english_has_no_transfer_regardless_of_brand(client):
    for brand in ("rodrigo", "starproperties"):
        assert server._tools_for("ta", brand) == []
        assert server._tools_for("si", brand) == []


# ---------------------------------------------------------------------------
# A mixed-case brand must resolve identically everywhere it is used. Before
# this fix, `_build_conversation_relay_twiml` picked the brand's greeting via
# `resolve_brand()` (which normalizes with .strip().lower()) but embedded the
# WebSocket `brand=` value via a separate exact-match check (`brand in
# BRANDS`), so "StartProperty" would render Amaya's greeting while the URL
# still said "brand=rodrigo" -- a session briefed as Amaya that would then
# resolve as Rodrigo, transfer tool included.
# ---------------------------------------------------------------------------

def test_mixed_case_brand_greeting_and_url_agree(client):
    out = server._build_conversation_relay_twiml(
        "example.test", "en", server.LANGUAGE_CONFIGS["en"],
        brand="StarProperties",
    )
    assert "Amaya" in out
    assert "Star Properties" in out
    assert "brand=starproperties" in out
    assert "brand=rodrigo" not in out
