"""Static contract for the generated SmartPBX runtime candidate.

These checks deliberately inspect templates: provider SDKs and carrier media are
not available in this factory checkout.  They prevent a generated tree from
silently regressing to split event types, per-request configuration reads, or
unwired provider lanes.
"""

from pathlib import Path


RUNTIME = Path(__file__).parents[1] / "template_v1" / "runtime"


def source(name: str) -> str:
    return (RUNTIME / name).read_text(encoding="utf-8")


def test_shared_provider_events_are_the_only_runtime_event_definitions():
    events = source("provider_adapters.py.tmpl")
    assert "class RecognizerResult" in events
    assert "class RecognizerFatal" in events
    assert "class ProvisionalSentence" in events
    assert "class TerminalCommit" in events
    assert "class GenerationFence" in events
    for name in ("stt_adapters.py.tmpl", "llm_adapters.py.tmpl", "turn_engine.py.tmpl"):
        text = source(name)
        assert "from provider_adapters import" in text
        assert "class RecognizerResult" not in text
        assert "class RecognizerFatal" not in text
        assert "class ProvisionalSentence" not in text
        assert "class TerminalCommit" not in text
        assert "class GenerationFence" not in text


def test_startup_wires_concrete_provider_methods_once_and_hot_path_reads_no_environment():
    startup = source("startup.py.tmpl")
    runtime = source("provider_runtime.py.tmpl")
    assert "RuntimeConfiguration.from_environ(os.environ)" in startup
    assert "bind_provider_adapter(provider_profile, os.environ)" in startup
    assert "start_recognizer" in runtime
    assert "stream_response" in runtime
    assert "build_tts_adapter" in runtime
    for name in ("provider_adapters.py.tmpl", "stt_adapters.py.tmpl", "llm_adapters.py.tmpl", "tts_adapters.py.tmpl", "turn_engine.py.tmpl"):
        assert "os.environ" not in source(name)


def test_runtime_entrypoint_exposes_health_authenticated_status_and_full_carrier_lifecycle():
    server = source("server.py.tmpl")
    gateway = source("smartpbx_gateway.py.tmpl")
    protocol = source("smartpbx_protocol.py.tmpl")
    assert '@app.get("/health")' in server
    assert '@app.get("/smartpbx/status")' in server
    assert '@app.websocket("/ws/v1/smartpbx/media")' in server
    for event in ("connected", "start", "media", "stop", "hangup"):
        assert event in protocol
    assert "await lease.release()" in gateway
    assert "rejected_capacity_total" in gateway
    assert "active_sessions" in gateway


def test_deepgram_is_not_an_approved_generated_runtime_provider():
    root = Path(__file__).parents[1]
    assert "deepgram" not in (root / "template_v1" / "provider_catalogue.json").read_text(encoding="utf-8").lower()
    assert '"deepgram"' not in (root / "render.py").read_text(encoding="utf-8")
