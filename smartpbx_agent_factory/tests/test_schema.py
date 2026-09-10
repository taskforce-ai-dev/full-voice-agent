import json
import re
from pathlib import Path

import pytest

from smartpbx_agent_factory.schema import ManifestError, manifest_digest, parse_manifest
from smartpbx_agent_factory.catalogue import CapabilityCatalogue


FIXTURE = Path(__file__).parent / "fixtures" / "acme-minimal.json"
CATALOGUE = Path(__file__).parent / "fixtures" / "approved-provider-catalogue.json"


def load_raw():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def parse(raw):
    return parse_manifest(
        raw,
        approved_source_roots=(Path.cwd(),),
        catalogue=CapabilityCatalogue.load(CATALOGUE),
    )


def test_parse_manifest_rejects_unknown_keys():
    raw = load_raw()
    raw["unexpected"] = True
    with pytest.raises(ManifestError, match="unknown key: unexpected"):
        parse(raw)


def test_manifest_digest_is_stable_and_covers_all_fields():
    raw = load_raw()
    first = manifest_digest(parse(raw))
    second = manifest_digest(parse(json.loads(json.dumps(raw))))
    assert first == second
    assert re.fullmatch(r"[0-9a-f]{64}", first)

    changed = load_raw()
    changed["purpose"] = "A changed inquiry purpose"
    assert manifest_digest(parse(changed)) != first


def test_booking_requires_explicit_destination_and_pii_policy():
    raw = load_raw()
    raw["capabilities"]["booking"] = {"enabled": True}
    with pytest.raises(ManifestError, match="booking.destination"):
        parse(raw)


def test_parser_returns_frozen_typed_manifest():
    manifest = parse(load_raw())
    assert manifest.slug == "acme-inquiry"
    assert manifest.languages[0].code == "en"
    assert manifest.capabilities.any_enabled is False
    with pytest.raises(AttributeError):
        manifest.slug = "other"


def test_parser_rejects_path_traversal_in_knowledge_sources():
    raw = load_raw()
    raw["knowledge_sources"][0]["path"] = "../../outside.txt"
    with pytest.raises(ManifestError, match="approved root|path"):
        parse(raw)


def test_parser_requires_explicit_trusted_source_roots():
    with pytest.raises(ManifestError, match="approved source roots"):
        parse_manifest(load_raw(), catalogue=CapabilityCatalogue.load(CATALOGUE))


def test_parser_requires_a_reviewed_catalogue_and_rejects_blocked_catalogue():
    with pytest.raises(ManifestError, match="reviewed capability catalogue"):
        parse_manifest(load_raw(), approved_source_roots=(Path.cwd(),))


def test_parser_rejects_unknown_provider_mapping_and_capability_detail_keys():
    raw = load_raw()
    raw["languages"][0]["stt"] = {"provider": "deepgram", "unreviewed": True}
    with pytest.raises(ManifestError, match="unknown key.*stt"):
        parse(raw)

    raw = load_raw()
    raw["languages"][0]["code"] = "en"
    raw["languages"][0]["language"] = "en"
    with pytest.raises(ManifestError, match="code.*language|alias"):
        parse(raw)

    raw = load_raw()
    raw["languages"][0]["stt"] = {"provider": "deepgram", "name": "other"}
    with pytest.raises(ManifestError, match="provider.*name|alias"):
        parse(raw)

    raw = load_raw()
    raw["capabilities"] = {"booking": {"enabled": False, "details": {"unreviewed": True}}}
    with pytest.raises(ManifestError, match="unsupported capability detail"):
        parse(raw)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("locale", "en-GB"),
        ("stt", {"provider": "deepgram", "model": "unapproved"}),
        ("llm", {"provider": "claude", "model": "unapproved"}),
        ("tts", {"provider": "elevenlabs", "model": "unapproved"}),
    ),
)
def test_parser_rejects_unapproved_language_locale_or_provider_model_pair(field, value):
    raw = load_raw()
    raw["languages"][0][field] = value
    with pytest.raises(ManifestError, match="not verified"):
        parse(raw)


def test_manifest_is_deeply_immutable_and_uses_v07_defaults():
    raw = load_raw()
    raw["smartpbx"].pop("protocol_profile")
    raw["smartpbx"].pop("status_authentication")
    manifest = parse(raw)
    assert manifest.smartpbx.protocol_profile == "smartpbx-ai-provider-v07"
    assert manifest.smartpbx.capacity <= 4
    assert manifest.smartpbx.status_authentication is True
    assert manifest.website_demo.visibility == "pending"
    with pytest.raises(TypeError):
        manifest.operating_hours["sat"] = "closed"
    with pytest.raises(TypeError):
        manifest.capabilities.booking.details["x"] = "y"
    assert manifest_digest(manifest) == manifest_digest(parse(raw))


def test_parser_rejects_non_v07_or_over_capacity_smartpbx_input():
    raw = load_raw()
    raw["smartpbx"]["protocol_profile"] = "smartpbx-ai-provider-v06"
    with pytest.raises(ManifestError, match="v07"):
        parse(raw)
    raw = load_raw()
    raw["smartpbx"]["capacity"] = 5
    with pytest.raises(ManifestError, match="at most 4"):
        parse(raw)


def test_parser_rejects_non_pending_website_visibility():
    raw = load_raw()
    raw["website_demo"]["visibility"] = "active"
    with pytest.raises(ManifestError, match="pending"):
        parse(raw)


@pytest.mark.parametrize("field", ["collect_name", "collect_phone"])
def test_pii_collection_requires_explicit_consent_without_capabilities(field):
    raw = load_raw()
    raw["pii_policy"][field] = True
    with pytest.raises(ManifestError, match="explicit_consent"):
        parse(raw)


def test_collect_other_pii_requires_explicit_consent_without_capabilities():
    raw = load_raw()
    raw["pii_policy"]["collect_other"] = ["email"]
    with pytest.raises(ManifestError, match="explicit_consent"):
        parse(raw)
