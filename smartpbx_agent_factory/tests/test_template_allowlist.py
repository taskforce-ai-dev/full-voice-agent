from hashlib import sha256

import pytest

from smartpbx_agent_factory.provenance import ProvenanceError, verify_template_files


def test_file_allowlist_rejects_unlisted_kavya_identity_file(tmp_path):
    (tmp_path / "hotel_info.txt").write_text("Hatton Hills", encoding="utf-8")
    allowlist = metadata({"Kavya/server.py": entry("runtime/server.py", "sha256:" + ("0" * 64))})
    with pytest.raises(ProvenanceError, match="not allowlisted"):
        verify_template_files(tmp_path, allowlist)


def test_file_allowlist_rejects_hash_drift(tmp_path):
    source = tmp_path / "runtime" / "server.py"
    source.parent.mkdir()
    source.write_text("fixture", encoding="utf-8")
    with pytest.raises(ProvenanceError, match="hash drift"):
        verify_template_files(tmp_path, metadata({"Kavya/server.py": entry("runtime/server.py", "sha256:" + ("0" * 64))}))


def test_file_allowlist_rejects_symlink(tmp_path):
    target = tmp_path / "target.py"
    target.write_text("fixture", encoding="utf-8")
    link = tmp_path / "runtime" / "server.py"
    link.parent.mkdir()
    link.symlink_to(target)
    with pytest.raises(ProvenanceError, match="symlink"):
        verify_template_files(tmp_path, metadata({"Kavya/server.py": entry("runtime/server.py", "sha256:" + ("0" * 64))}))


def test_file_allowlist_accepts_metadata_wrapper(tmp_path):
    source = tmp_path / "runtime" / "server.py"
    source.parent.mkdir()
    source.write_text("fixture", encoding="utf-8")
    digest = "sha256:" + sha256(b"fixture").hexdigest()
    result = verify_template_files(tmp_path, metadata({"Kavya/server.py": entry("runtime/server.py", digest)}))
    assert result["Kavya/server.py"] == digest


def test_frozen_template_allowlist_records_approved_v06_source_and_immutable_image():
    import json

    allowlist = json.loads((__import__("pathlib").Path("smartpbx_agent_factory/template_v1/file_allowlist.json")).read_text())
    assert allowlist["status"] == "approved"
    assert allowlist["source_revision"] == "6f6c2a3ae6f50e3ea84d293a24c37ef74808ec0e"
    assert allowlist["oci_revision"] == allowlist["source_revision"]
    assert allowlist["image_digest"] == "sha256:3d1cfce67574efd1c8bde484d26345c027713c4d169f37804114fecec5b81350"
    assert allowlist["protocol_version"] == "smartpbx-ai-provider-v06"
    assert set(allowlist["files"]) == {
        "Kavya/smartpbx_protocol.py",
        "Kavya/smartpbx_diagnostics.py",
        "Kavya/smartpbx_transport.py",
    }


@pytest.mark.parametrize("unsafe", ("../server.py", "/server.py", "runtime/../server.py", "runtime\\server.py"))
def test_file_allowlist_rejects_unsafe_source_or_template_paths(unsafe):
    with pytest.raises(ProvenanceError, match="unsafe path"):
        verify_template_files(__import__("pathlib").Path("."), metadata({unsafe: entry("runtime/server.py", "sha256:" + ("0" * 64))}))
    with pytest.raises(ProvenanceError, match="unsafe path"):
        verify_template_files(__import__("pathlib").Path("."), metadata({"Kavya/server.py": entry(unsafe, "sha256:" + ("0" * 64))}))


def test_file_allowlist_rejects_duplicate_template_targets():
    files = {
        "Kavya/server.py": entry("runtime/server.py", "sha256:" + ("0" * 64)),
        "Kavya/config.py": entry("runtime/server.py", "sha256:" + ("1" * 64)),
    }
    with pytest.raises(ProvenanceError, match="duplicate template path"):
        verify_template_files(__import__("pathlib").Path("."), metadata(files))


def test_file_allowlist_rejects_incomplete_entry_metadata():
    with pytest.raises(ProvenanceError, match="entry"):
        verify_template_files(__import__("pathlib").Path("."), metadata({"Kavya/server.py": {"template_path": "runtime/server.py"}}))


def metadata(files):
    return {
        "template_version": "v1",
        "status": "approved",
        "source_revision": "a" * 40,
        "oci_revision": "a" * 40,
        "image_digest": "sha256:" + "b" * 64,
        "protocol_version": "smartpbx-ai-provider-v07",
        "environment_schema_version": "v1",
        "files": files,
    }


def entry(template_path, digest):
    return {"template_path": template_path, "sha256": digest}
