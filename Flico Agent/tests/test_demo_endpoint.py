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
    # The real client's inbound path (English-only ConversationRelay, no IVR
    # menu -- that's pre-existing, unrelated to this task) must keep behaving
    # exactly as before and must never carry the demo's branding.
    r = client.post("/voice/incoming")
    assert r.status_code == 200
    assert "<ConversationRelay" in r.text
    assert "Start Property" not in r.text
    assert "Amaya" not in r.text


def test_relay_twiml_defaults_to_rodrigo(client):
    # Existing callers pass three positional args; they must keep Rodrigo.
    out = server._build_conversation_relay_twiml(
        "example.test", "en", server.LANGUAGE_CONFIGS["en"])
    assert "Rodrigo Realtors" in out
    assert "Start Property" not in out
    assert "brand=rodrigo" in out
