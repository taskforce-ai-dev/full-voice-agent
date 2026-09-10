from pathlib import Path

import pytest

from smartpbx_agent_factory.provenance import ProvenanceError, verify_template_files


def test_file_allowlist_rejects_unlisted_kavya_identity_file(tmp_path):
    (tmp_path / "hotel_info.txt").write_text("Hatton Hills", encoding="utf-8")
    allowlist = {"server.py": "sha256:" + ("0" * 64)}
    with pytest.raises(ProvenanceError, match="not allowlisted"):
        verify_template_files(tmp_path, allowlist)


def test_file_allowlist_rejects_hash_drift(tmp_path):
    source = tmp_path / "server.py"
    source.write_text("fixture", encoding="utf-8")
    with pytest.raises(ProvenanceError, match="hash drift"):
        verify_template_files(tmp_path, {"server.py": "sha256:" + ("0" * 64)})


def test_file_allowlist_rejects_symlink(tmp_path):
    target = tmp_path / "target.py"
    target.write_text("fixture", encoding="utf-8")
    link = tmp_path / "server.py"
    link.symlink_to(target)
    with pytest.raises(ProvenanceError, match="symlink"):
        verify_template_files(tmp_path, {"server.py": "sha256:" + ("0" * 64)})


def test_file_allowlist_accepts_metadata_wrapper(tmp_path):
    source = tmp_path / "server.py"
    source.write_text("fixture", encoding="utf-8")
    digest = "sha256:" + __import__("hashlib").sha256(b"fixture").hexdigest()
    result = verify_template_files(tmp_path, {"template_version": "v1", "files": {"server.py": digest}})
    assert result["server.py"] == digest
