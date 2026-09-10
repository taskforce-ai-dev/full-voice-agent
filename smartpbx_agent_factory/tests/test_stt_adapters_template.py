"""Static source contract for the pending client-neutral STT template lane."""

from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).parents[1] / "template_v1"
TEMPLATE = ROOT / "runtime" / "stt_adapters.py.tmpl"


def test_stt_template_preserves_the_pinned_streaming_provider_boundary() -> None:
    text = TEMPLATE.read_text(encoding="utf-8")

    assert "class GoogleStreamingSTT" in text
    assert "class AzurePushStreamSTT" in text
    assert "AudioEncoding.MULAW" in text
    assert "samples_per_second=8000" in text
    assert "bits_per_sample=16" in text
    assert "channels=1" in text
    assert "asyncio.run_coroutine_threadsafe" in text
    assert "def feed(self, mulaw_audio: bytes)" in text
    assert "def start(self)" in text
    assert "def stop(self)" in text


def test_stt_template_has_startup_injection_and_no_client_identity() -> None:
    text = TEMPLATE.read_text(encoding="utf-8")

    assert "GoogleSTTConfig" in text
    assert "AzureSTTConfig" in text
    assert "os.getenv" not in text
    assert "import os" not in text
    assert "tenant" not in text.lower()


def test_candidate_provenance_records_exact_kavya_stt_ranges_without_approval() -> None:
    candidate = json.loads((ROOT / "candidate_runtime_provenance.json").read_text(encoding="utf-8"))

    assert candidate["status"] == "partial-candidate-not-approved-for-rendering"
    component = next(item for item in candidate["components"] if item["template_path"] == "runtime/stt_adapters.py.tmpl")
    assert component["source_path"] == "Kavya/server.py"
    assert component["source_ranges"] == ["575-582", "1855-1863", "6549-6881", "6884-6980", "6983-7252", "9359-9443"]
