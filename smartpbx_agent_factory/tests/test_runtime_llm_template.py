"""Static contract for the candidate-only client-neutral LLM runtime lane."""

from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path


ROOT = Path(__file__).parents[1] / "template_v1"
TEMPLATE = ROOT / "runtime" / "llm_adapters.py.tmpl"


def test_candidate_llm_template_keeps_streaming_and_safety_boundaries_explicit() -> None:
    source = TEMPLATE.read_text(encoding="utf-8")

    assert "class InquiryOnlyLLMAdapter" in source
    assert "class ClaudeStreamClient(Protocol)" in source
    assert "class GeminiStreamClient(Protocol)" in source
    assert "asyncio.wait_for" in source
    assert "terminal_metadata" in source
    assert "saw_terminal_stop" in source
    assert "saw_terminal_metadata" in source
    assert "retry_used" in source
    assert "_validate_inquiry_messages" in source
    assert "provider emitted a tool event in inquiry-only mode" in source
    assert "buffer_until_terminal" in source
    assert "provisional" in source


def test_candidate_llm_template_has_pinned_server_ranges_and_remains_partial() -> None:
    candidate = json.loads((ROOT / "candidate_runtime_provenance.json").read_text(encoding="utf-8"))
    component = next(item for item in candidate["components"] if item["template_path"] == "runtime/llm_adapters.py.tmpl")

    assert candidate["status"] == "partial-candidate-not-approved-for-rendering"
    assert component["source_path"] == "Kavya/server.py"
    assert component["source_ranges"] == ["1567-1708", "8437-8522", "11702-12051", "12488-12845"]
    assert component["template_sha256"] == "sha256:" + sha256(TEMPLATE.read_bytes()).hexdigest()


def test_candidate_llm_template_contains_no_known_kavya_tenant_identity_or_secret_assignment() -> None:
    source = TEMPLATE.read_text(encoding="utf-8").lower()

    for forbidden in ("hatton hills", "treehouse", "mosvold", "yanolja", "kavya", "api_key="):
        assert forbidden not in source
