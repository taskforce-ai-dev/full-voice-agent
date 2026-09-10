import json
from pathlib import Path

import pytest

from smartpbx_agent_factory.provenance import (
    ProvenanceError,
    render_template_text,
    validate_image_digest,
    validate_source_revision,
    verify_deployed_image_source,
    verify_template_files,
)


def test_branch_name_is_not_accepted_as_template_revision():
    with pytest.raises(ProvenanceError, match="full 40-character revision"):
        validate_source_revision("main")


def test_mutable_image_tag_and_missing_oci_revision_are_rejected():
    with pytest.raises(ProvenanceError, match="immutable digest"):
        verify_deployed_image_source("ghcr.io/example/agent:latest", "a" * 40, "sha256:" + "b" * 64)
    with pytest.raises(ProvenanceError, match="OCI revision"):
        verify_deployed_image_source("ghcr.io/example/agent@sha256:" + "b" * 64, "a" * 40, "sha256:" + "b" * 64)


def test_deployed_image_source_requires_matching_revision_and_digest():
    expected_revision = "a" * 40
    expected_digest = "sha256:" + "b" * 64
    evidence = verify_deployed_image_source(
        {
            "image_ref": "ghcr.io/example/agent@" + expected_digest,
            "oci_revision": expected_revision,
        },
        expected_revision,
        expected_digest,
    )
    assert evidence.source_revision == expected_revision
    assert evidence.image_digest == expected_digest


def test_template_substitution_rejects_unknown_variable():
    with pytest.raises(ProvenanceError, match="unknown variable"):
        render_template_text("Hello {{company_name}}", {"agent_name": "A"})


def test_checked_in_blocked_allowlist_cannot_verify_templates(tmp_path):
    allowlist = json.loads(
        (Path(__file__).parents[1] / "template_v1" / "file_allowlist.json").read_text(encoding="utf-8")
    )
    with pytest.raises(ProvenanceError, match="approved|blocked"):
        verify_template_files(tmp_path, allowlist)


def test_template_substitution_rejects_unresolved_braces_and_nul():
    with pytest.raises(ProvenanceError, match="unresolved"):
        render_template_text("Hello {{agent_name}} {{", {"agent_name": "A"})
    with pytest.raises(ProvenanceError, match="NUL"):
        render_template_text("Hello\x00", {})


def test_digest_validation_is_strict():
    assert validate_source_revision("a" * 40) == "a" * 40
    assert validate_image_digest("sha256:" + "b" * 64).startswith("sha256:")
    with pytest.raises(ProvenanceError):
        validate_image_digest("sha256:" + "B" * 64)
