"""PDF extraction after the PyPDF2 -> pypdf migration (PYSEC-2026-1835).

PyPDF2 3.0.1 was PyPDF2's final release; no fixed PyPDF2 version exists for
this advisory. knowledge_base.py now imports its actively maintained
successor, pypdf, through the same PdfReader/.pages/.extract_text() surface.
These tests exercise `_read_file` on a real (hand-built, dependency-free)
single-page PDF end to end, and confirm the availability flag was renamed
rather than duplicated.
"""

from __future__ import annotations

import knowledge_base


def _minimal_pdf_bytes(text: str) -> bytes:
    """Build the smallest valid single-page PDF whose content stream shows `text`."""
    content = f"BT /F1 24 Tf 10 100 Td ({text}) Tj ET".encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] "
        b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]

    parts = [b"%PDF-1.4\n"]
    offsets = [0]
    for index, obj in enumerate(objects, start=1):
        offsets.append(sum(len(part) for part in parts))
        parts.append(f"{index} 0 obj\n".encode() + obj + b"\nendobj\n")

    xref_offset = sum(len(part) for part in parts)
    xref = [b"xref\n", f"0 {len(objects) + 1}\n".encode(), b"0000000000 65535 f \n"]
    for offset in offsets[1:]:
        xref.append(f"{offset:010d} 00000 n \n".encode())
    parts.extend(xref)
    parts.append(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_offset}\n%%EOF".encode()
    )
    return b"".join(parts)


def test_pypdf_is_available_and_the_flag_was_renamed_not_duplicated():
    assert knowledge_base.PYPDF_AVAILABLE is True
    assert not hasattr(knowledge_base, "PYPDF2_AVAILABLE")
    assert not hasattr(knowledge_base, "PyPDF2")
    assert knowledge_base.pypdf is not None


def test_read_file_extracts_text_from_a_pdf_via_pypdf(tmp_path):
    pdf_path = tmp_path / "note.pdf"
    pdf_path.write_bytes(_minimal_pdf_bytes("Hello pypdf"))

    text = knowledge_base._read_file(str(pdf_path))

    assert text is not None
    assert "Hello pypdf" in text


def test_read_file_returns_none_for_a_corrupt_pdf_without_raising(tmp_path):
    pdf_path = tmp_path / "corrupt.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\nnot a real pdf body")

    assert knowledge_base._read_file(str(pdf_path)) is None


def test_read_file_reports_pdf_unavailable_when_pypdf_is_absent(tmp_path, monkeypatch):
    pdf_path = tmp_path / "note.pdf"
    pdf_path.write_bytes(_minimal_pdf_bytes("Hello pypdf"))

    monkeypatch.setattr(knowledge_base, "PYPDF_AVAILABLE", False)

    assert knowledge_base._read_file(str(pdf_path)) is None
