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
    builders = source("provider_builders.py.tmpl")
    assert "RuntimeConfiguration.from_environ(os.environ)" in startup
    assert "bind_provider_adapter(provider_profile, os.environ)" in startup
    assert "start_recognizer" in runtime
    assert "stream_response" in runtime
    assert "build_tts_adapter" in runtime
    assert "GOOGLE_APPLICATION_CREDENTIALS" in builders
    assert "AsyncAnthropic" in builders
    assert "genai.Client" in builders
    assert "httpx.AsyncClient" in builders
    assert "SMARTPBX_ALLOW_SYNTHETIC_FOR_CI" in builders
    assert "model_dump" in builders
    assert "to_dict" in builders
    assert "event_type != \"step.delta\"" in builders
    assert "getattr(delta, \"type\", None) != \"audio\"" in builders
    assert "getattr(event, \"audio\"" not in builders
    assert "base64.b64decode(data, validate=True)" in builders
    assert "missing = [] if synthetic" in runtime
    assert "if \"gemini\" in selected" in builders
    assert "if selected & {\"elevenlabs\", \"rime\"}" in builders
    assert "sinhala_provider" not in source("tts_adapters.py.tmpl")
    assert "getattr(part, \"thought\", False)" in builders
    assert "prompt_feedback" in builders
    assert "usage_metadata" in builders
    assert "TTSProviderError(_classify_gemini_tts_provider_error(error))" in builders
    assert "async def aclose" in builders
    assert 'raise TTSProviderError("empty_audio")' in source("tts_adapters.py.tmpl")
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
    assert "active_tasks" in gateway
    assert "active_resources" in gateway
    for counter in ("admitted_total", "released_total", "connected_total", "started_total", "media_frames_total", "stopped_total", "hung_up_total"):
        assert counter in gateway
    assert "shutdown_runtime" in server


def test_profile_driven_conversation_contract_preserves_delivery_and_fencing_boundaries():
    session = source("smartpbx_session.py.tmpl")
    engine = source("turn_engine.py.tmpl")
    profile = source("product_profile.py.tmpl")
    assert 'str(index): code for index, code in enumerate(self._product_profile.language_profiles, start=1)' in session
    assert "language-menu" in session and "initial-greeting" in session
    assert "self._language_timeout" in session
    assert "_history: deque[tuple[str, str]] = deque(maxlen=12)" in engine
    assert 'self._history.append(("user", transcript))' in engine
    assert 'self._history.append(("assistant", committed_response))' in engine
    assert "RecoveryBoundary" in engine and "language.recovery_line" in engine
    assert "_reconcile_recognizer_text" in engine
    assert "_latest_interim" in engine and "_committed_finals" in engine
    assert "_reprompt_after_silence" in engine and "_delayed_filler" in engine
    assert "filler_phrases" in profile and "recovery_line" in profile


def test_source_shaped_history_recognition_and_selection_contracts_prevent_known_p0_regressions():
    builders = source("provider_builders.py.tmpl")
    events = source("provider_adapters.py.tmpl")
    stt = source("stt_adapters.py.tmpl")
    engine = source("turn_engine.py.tmpl")
    session = source("smartpbx_session.py.tmpl")
    renderer = (Path(__file__).parents[1] / "render.py").read_text(encoding="utf-8")
    assert 'messages=messages' in builders
    assert '(("user", transcript),)' in builders
    assert 'messages=(*messages, InquiryMessage("user", transcript))' not in builders
    assert 'self._history.append(("user", transcript))' in engine
    assert 'self._history.append(("assistant", committed_response))' in engine
    assert "audio_offset" in events and "audio_duration" in events and "audio_interval" in events
    assert "audio_offset=event.metadata.offset" in stt
    assert "_intervals_cover" in engine and "_pending_audio_coverage" in engine
    assert "text in self._committed_finals" not in engine
    assert 'exact_prefix = f"{committed} "' in engine
    assert "_normalized_tokens" in engine and "SequenceMatcher" in engine
    assert "len(transcript_tokens) < 5" in engine
    assert session.index("set_recognizer_result_admission(False)") < session.index("await self._turn_engine.start")
    assert "set_recognizer_result_admission(True)" in session
    assert "_reviewed_language_ux" in renderer and "no reviewed caller UX catalogue entry" in renderer
    assert "කරුණාකර රැඳෙන්න." in renderer


def test_renderer_uses_the_verified_gateway_template_and_keeps_web_ingress_review_only():
    root = Path(__file__).parents[1]
    renderer = (root / "render.py").read_text(encoding="utf-8")
    candidate = (root / "template_v1" / "runtime_infrastructure_candidate.json").read_text(encoding="utf-8")
    allowlist = (root / "template_v1" / "file_allowlist.json").read_text(encoding="utf-8")
    assert '"smartpbx_gateway.py": runtime_template("smartpbx_gateway.py.tmpl")' in renderer
    assert "def _python_gateway" not in renderer
    assert '"website_demo.py": runtime_template("website_demo.py.tmpl")' in renderer
    assert '"website_demo_core.py": runtime_template("website_demo_core.py.tmpl")' in renderer
    assert "synthetic=synthetic" in renderer and "review-only-exact-template" in renderer
    assert "signed-webhook" in candidate
    assert "source-extracted website-demo profile" in allowlist
    compose = (root / "template_v1" / "infrastructure" / "docker-compose.yml.tmpl").read_text(encoding="utf-8")
    website_proxy = (root / "template_v1" / "infrastructure" / "nginx-website-demo.conf.tmpl").read_text(encoding="utf-8")
    website = source("website_demo.py.tmpl")
    assert 'profiles: ["website-demo"]' in compose
    assert "SMARTPBX_WS_TOKEN" not in website
    assert "TWILIO_AUTH_TOKEN" in website
    assert '"/ws/v1/website-demo/conversation"' in website
    assert "server_name {{website_hostname}};" in website_proxy
    assert "location = /api/voice-token" in website_proxy
    assert "location = /voice/demo-incoming" in website_proxy
    assert "location = /ws/v1/website-demo/conversation" in website_proxy


def test_deepgram_is_not_an_approved_generated_runtime_provider():
    root = Path(__file__).parents[1]
    assert "deepgram" not in (root / "template_v1" / "provider_catalogue.json").read_text(encoding="utf-8").lower()
    assert '"deepgram"' not in (root / "render.py").read_text(encoding="utf-8")
