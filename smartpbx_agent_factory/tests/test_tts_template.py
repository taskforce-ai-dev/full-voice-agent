"""Static contracts for the pending client-neutral SmartPBX TTS lane."""

import asyncio
import importlib.util
import json
import sys
from hashlib import sha256
from importlib.machinery import SourceFileLoader
from pathlib import Path


TEMPLATE_ROOT = Path(__file__).parents[1] / "template_v1"
TEMPLATE_PATH = TEMPLATE_ROOT / "runtime" / "tts_adapters.py.tmpl"


def test_tts_template_is_pending_and_pinned_to_kavya_source_ranges():
    candidate = json.loads((TEMPLATE_ROOT / "candidate_runtime_provenance.json").read_text())
    assert candidate["status"] == "partial-candidate-not-approved-for-rendering"
    component = next(
        item for item in candidate["components"]
        if item["template_path"] == "runtime/tts_adapters.py.tmpl"
    )
    assert component["source_path"] == "Kavya/server.py"
    assert component["source_ranges"] == [
        "3075-3090", "2092-2310", "13112-13152", "13407-13848"
    ]
    assert component["template_sha256"] == "sha256:" + sha256(TEMPLATE_PATH.read_bytes()).hexdigest()


def test_tts_template_keeps_smartpbx_audio_and_fencing_contracts_explicit():
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    assert "output_format=ulaw_8000" in template
    assert 'model_id: str = "eleven_flash_v2_5"' in template
    assert '"audio/PCMU"' in template
    assert '"audio/l16"' in template
    assert "audioop.ratecv" in template
    assert "audioop.lin2ulaw" in template
    assert "FRAME_BYTES = 160" in template
    assert "generation_is_current" in template
    assert "async for chunk" in template
    assert "transport.send_audio" not in template


def test_tts_template_requires_exact_startup_language_routes_and_bounded_http_audio():
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    assert "class TTSLanguageRoute" in template
    assert "language_routes: Mapping[str, TTSLanguageRoute]" in template
    assert "self._config.language_routes[language]" in template
    assert 'if language == "en"' not in template
    assert 'if language == "si"' not in template
    assert "max_response_bytes" in template
    assert "response_too_large" in template
    assert "_bounded_provider_audio" in template


def test_tts_template_accepts_only_explicit_catalogue_language_codes():
    loader = SourceFileLoader("pending_tts_adapter", str(TEMPLATE_PATH))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = module
    loader.exec_module(module)
    try:
        config = module.TTSStartupConfig(
            english=module.ElevenLabsSettings("key", "voice"),
            sinhala_gemini=module.SinhalaGeminiSettings("key"),
            language_routes={
                "en-US": module.TTSLanguageRoute("elevenlabs"),
                "si-LK": module.TTSLanguageRoute("sinhala"),
            },
        )
        adapter = module.SmartPBXTTSAdapter(config, module.TTSProviderClients(http=None, gemini=None))
        assert asyncio.run(
            adapter.synthesize_for_generation(
                "hello", "en-US", generation=1, generation_is_current=lambda _: True
            )
        ) is not None
        try:
            asyncio.run(
                adapter.synthesize_for_generation(
                    "hello", "en-GB", generation=1, generation_is_current=lambda _: True
                )
            )
        except module.TTSConfigurationError:
            pass
        else:
            raise AssertionError("unmapped locale must be rejected without normalization")
    finally:
        sys.modules.pop(loader.name, None)


def test_tts_template_uses_startup_injection_and_contains_no_tenant_copy():
    template = TEMPLATE_PATH.read_text(encoding="utf-8").lower()
    assert "class ttsstartupconfig" in template
    assert "os.getenv" not in template
    assert "hatton hills" not in template
    assert "treehouse" not in template
    assert "kavya" not in template
