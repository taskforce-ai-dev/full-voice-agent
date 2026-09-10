import json
import re
from pathlib import Path

import pytest

from smartpbx_agent_factory.schema import ManifestError, manifest_digest, parse_manifest


FIXTURE = Path(__file__).parent / "fixtures" / "acme-minimal.json"


def load_raw():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_parse_manifest_rejects_unknown_keys():
    raw = load_raw()
    raw["unexpected"] = True
    with pytest.raises(ManifestError, match="unknown key: unexpected"):
        parse_manifest(raw)


def test_manifest_digest_is_stable_and_covers_all_fields():
    raw = load_raw()
    first = manifest_digest(parse_manifest(raw))
    second = manifest_digest(parse_manifest(json.loads(json.dumps(raw))))
    assert first == second
    assert re.fullmatch(r"[0-9a-f]{64}", first)

    changed = load_raw()
    changed["purpose"] = "A changed inquiry purpose"
    assert manifest_digest(parse_manifest(changed)) != first


def test_booking_requires_explicit_destination_and_pii_policy():
    raw = load_raw()
    raw["capabilities"]["booking"] = {"enabled": True}
    with pytest.raises(ManifestError, match="booking.destination"):
        parse_manifest(raw)


def test_parser_returns_frozen_typed_manifest():
    manifest = parse_manifest(load_raw())
    assert manifest.slug == "acme-inquiry"
    assert manifest.languages[0].code == "en-US"
    assert manifest.capabilities.any_enabled is False
    with pytest.raises(AttributeError):
        manifest.slug = "other"


def test_parser_rejects_path_traversal_in_knowledge_sources():
    raw = load_raw()
    raw["knowledge_sources"][0]["path"] = "../../outside.txt"
    with pytest.raises(ManifestError, match="approved root|path"):
        parse_manifest(raw)
