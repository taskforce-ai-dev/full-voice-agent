"""Persistent, paced, classified Sinhala fixed-phrase prewarm.

Live evidence at an 11:00 UTC container start showed
``sinhala_phrase_prewarm rendered=13 total=19 ready=false`` after 19
back-to-back Gemini TTS requests within ~1 minute: Gemini TTS on this
project caps at ~10 requests/minute and 100 requests/day, so the burst both
tripped the per-minute limit (the 6 failures) and spent ~19% of the daily
budget on every container restart, with no per-phrase failure reason
logged. These tests pin the fix: a disk-backed cache that survives a
restart, pacing that keeps a cold start under the per-minute cap, and
classified quota/rate-limit handling that stops burning budget the moment
the daily cap is hit.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest

import server
from tests.test_smartpbx_gemini_tts import (
    FakeAsyncStream,
    audio_event,
)


@pytest.fixture(autouse=True)
def _isolated_prewarm_state(monkeypatch, tmp_path):
    """Never let one test's cache, quota state, or debounce timer leak."""
    monkeypatch.setattr(server, "_SMARTPBX_SINHALA_PHRASE_AUDIO", {})
    monkeypatch.setattr(server, "_SMARTPBX_SINHALA_PHRASE_PREWARM", None)
    monkeypatch.setattr(server, "_SMARTPBX_SINHALA_PHRASE_PREWARM_LAST_STARTED_AT", None)
    monkeypatch.setattr(server, "_SMARTPBX_SINHALA_PHRASE_PREWARM_RESET_TASK", None)
    monkeypatch.setattr(server, "GEMINI_API_KEY", "test-gemini-key")
    monkeypatch.setattr(
        server,
        "_smartpbx_sinhala_tts_quota_state",
        {"consecutive_failures": 0, "degraded": False, "degraded_logged": False},
    )
    monkeypatch.setattr(
        server,
        "_smartpbx_sinhala_tts_model_state",
        {"exhausted_until": {}, "active_model": None},
    )
    # A real cache directory per test, but disk persistence off by default --
    # individual tests opt in by pointing SMARTPBX_SINHALA_PHRASE_CACHE_DIR at
    # `tmp_path` themselves.
    monkeypatch.setattr(server, "SMARTPBX_SINHALA_PHRASE_CACHE_DIR", "")
    return tmp_path


class _FakeClock:
    """A deterministic virtual clock: sleep(n) advances time by exactly n."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def time(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        if seconds > 0:
            self.now += seconds


class _RecordingInteractions:
    """Always succeeds; records the wall-clock time of each `create()` call."""

    def __init__(self, clock: _FakeClock, payload: bytes = b"\x01\x02" * 2400) -> None:
        self._clock = clock
        self._payload = payload
        self.calls: list[tuple[float, dict]] = []

    async def create(self, **kwargs):
        self.calls.append((self._clock.time(), kwargs))
        return FakeAsyncStream([audio_event(self._payload)])


class _RecordingClient:
    def __init__(self, clock: _FakeClock) -> None:
        self.interactions = _RecordingInteractions(clock)
        self.aio = SimpleNamespace(interactions=self.interactions)


class _ScriptedInteractions:
    """Per-model scripted outcomes: a list of ``"ok"``/error-code strings.

    Each call to `create()` for a given model consumes the next scripted
    outcome for that model; exhausting a model's script re-raises its last
    entry (never IndexErrors a test that over-calls by one).
    """

    def __init__(self, scripts: dict[str, list[str]], payload: bytes = b"\x01\x02" * 2400) -> None:
        self._scripts = {model: list(outcomes) for model, outcomes in scripts.items()}
        self._payload = payload
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        model = kwargs.get("model")
        script = self._scripts.get(model, [])
        outcome = script.pop(0) if script else "ok"
        if outcome == "ok":
            return FakeAsyncStream([audio_event(self._payload)])
        raise server._GeminiTTSProviderError(outcome)


class _ScriptedClient:
    def __init__(self, scripts: dict[str, list[str]]) -> None:
        self.interactions = _ScriptedInteractions(scripts)
        self.aio = SimpleNamespace(interactions=self.interactions)


def _prime_all_but(texts_to_leave_missing: set[str]) -> None:
    """Pre-cache every allowlisted phrase except the given ones."""
    for text in server.SMARTPBX_SINHALA_CACHED_PHRASES:
        if text not in texts_to_leave_missing:
            server._store_cached_smartpbx_sinhala_phrase_audio(text, b"\xff" * 640)


# --- disk round trip ---------------------------------------------------------


def test_disk_round_trip_write_then_load(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "SMARTPBX_SINHALA_PHRASE_CACHE_DIR", str(tmp_path))
    model = server.SMARTPBX_SINHALA_GEMINI_TTS_MODEL
    text = server.SMARTPBX_SINHALA_INITIAL_FILLER_TEXT
    audio = b"\xff" * 640

    server._write_smartpbx_sinhala_phrase_audio_to_disk(model, text, audio)
    path = server._smartpbx_sinhala_phrase_cache_path(model, text)
    assert path is not None and path.exists()
    # The filename must never encode the phrase text.
    assert text not in path.name

    loaded = server._load_smartpbx_sinhala_phrase_audio_from_disk(model, text)
    assert loaded == audio


def test_disk_cache_ignores_corrupt_or_empty_files(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "SMARTPBX_SINHALA_PHRASE_CACHE_DIR", str(tmp_path))
    model = server.SMARTPBX_SINHALA_GEMINI_TTS_MODEL
    text = server.SMARTPBX_SINHALA_INITIAL_FILLER_TEXT
    path = server._smartpbx_sinhala_phrase_cache_path(model, text)
    path.parent.mkdir(parents=True, exist_ok=True)

    path.write_bytes(b"")  # empty
    assert server._load_smartpbx_sinhala_phrase_audio_from_disk(model, text) is None

    path.write_bytes(b"\xff" * 159)  # not a multiple of 160
    assert server._load_smartpbx_sinhala_phrase_audio_from_disk(model, text) is None

    path.write_bytes(b"\xff" * 160)  # exactly one frame -- valid
    assert server._load_smartpbx_sinhala_phrase_audio_from_disk(model, text) == b"\xff" * 160


def test_disk_cache_ignores_unreadable_path(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "SMARTPBX_SINHALA_PHRASE_CACHE_DIR", str(tmp_path / "does-not-exist"))
    model = server.SMARTPBX_SINHALA_GEMINI_TTS_MODEL
    text = server.SMARTPBX_SINHALA_INITIAL_FILLER_TEXT
    assert server._load_smartpbx_sinhala_phrase_audio_from_disk(model, text) is None


def test_blank_cache_dir_disables_disk_persistence(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "SMARTPBX_SINHALA_PHRASE_CACHE_DIR", "")
    model = server.SMARTPBX_SINHALA_GEMINI_TTS_MODEL
    text = server.SMARTPBX_SINHALA_INITIAL_FILLER_TEXT
    assert server._smartpbx_sinhala_phrase_cache_path(model, text) is None
    server._write_smartpbx_sinhala_phrase_audio_to_disk(model, text, b"\xff" * 640)
    assert list(tmp_path.iterdir()) == []
    assert server._load_smartpbx_sinhala_phrase_audio_from_disk(model, text) is None


# --- misses-only synthesis ---------------------------------------------------


@pytest.mark.asyncio
async def test_prewarm_loads_from_disk_first_and_synthesises_only_the_misses(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "SMARTPBX_SINHALA_PHRASE_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(server, "SMARTPBX_SINHALA_PREWARM_INTERVAL_SECONDS", 0.0)
    monkeypatch.setattr(server, "_smartpbx_sinhala_prewarm_sleep", lambda seconds: asyncio.sleep(0))

    phrases = server.SMARTPBX_SINHALA_CACHED_PHRASES
    on_disk = set(phrases[:5])
    misses = set(phrases[5:])
    model = server.SMARTPBX_SINHALA_GEMINI_TTS_MODEL
    for text in on_disk:
        server._write_smartpbx_sinhala_phrase_audio_to_disk(model, text, b"\xff" * 640)

    fake_client = _RecordingClient(_FakeClock())
    monkeypatch.setattr(server, "_get_gemini_tts_client", lambda: fake_client)

    await server._prewarm_smartpbx_sinhala_phrase_audio()

    requested_texts = {call[1]["input"] for call in fake_client.interactions.calls}
    assert requested_texts == misses
    assert server._smartpbx_sinhala_phrase_audio_ready()
    for text in on_disk:
        assert server._get_cached_smartpbx_sinhala_phrase_audio(text) == b"\xff" * 640


@pytest.mark.asyncio
async def test_prewarm_skips_phrases_already_cached_in_memory(monkeypatch):
    monkeypatch.setattr(server, "SMARTPBX_SINHALA_PREWARM_INTERVAL_SECONDS", 0.0)
    monkeypatch.setattr(server, "_smartpbx_sinhala_prewarm_sleep", lambda seconds: asyncio.sleep(0))

    phrases = server.SMARTPBX_SINHALA_CACHED_PHRASES
    missing = {phrases[0], phrases[-1]}
    _prime_all_but(missing)

    fake_client = _RecordingClient(_FakeClock())
    monkeypatch.setattr(server, "_get_gemini_tts_client", lambda: fake_client)

    await server._prewarm_smartpbx_sinhala_phrase_audio()

    requested_texts = {call[1]["input"] for call in fake_client.interactions.calls}
    assert requested_texts == missing
    assert server._smartpbx_sinhala_phrase_audio_ready()


# --- pacing -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pacing_keeps_at_most_nine_requests_in_any_sixty_second_window(monkeypatch):
    """19 phrases at the default 7 s spacing must never burst the ~10/min cap."""
    monkeypatch.setattr(server, "SMARTPBX_SINHALA_PHRASE_CACHE_DIR", "")
    clock = _FakeClock()
    monkeypatch.setattr(server, "_smartpbx_sinhala_prewarm_clock", clock.time)
    monkeypatch.setattr(server, "_smartpbx_sinhala_prewarm_sleep", clock.sleep)

    fake_client = _RecordingClient(clock)
    monkeypatch.setattr(server, "_get_gemini_tts_client", lambda: fake_client)

    await server._prewarm_smartpbx_sinhala_phrase_audio()

    timestamps = sorted(t for t, _ in fake_client.interactions.calls)
    assert len(timestamps) == len(server.SMARTPBX_SINHALA_CACHED_PHRASES)
    for start in timestamps:
        window_count = sum(1 for t in timestamps if start <= t < start + 60.0)
        assert window_count <= 9, f"{window_count} requests within 60s of t={start}"
    # And pacing actually ran (not degenerately instantaneous).
    assert timestamps[-1] - timestamps[0] > 0


@pytest.mark.asyncio
async def test_pacing_interval_zero_disables_spacing(monkeypatch):
    monkeypatch.setattr(server, "SMARTPBX_SINHALA_PHRASE_CACHE_DIR", "")
    monkeypatch.setattr(server, "SMARTPBX_SINHALA_PREWARM_INTERVAL_SECONDS", 0.0)
    clock = _FakeClock()
    monkeypatch.setattr(server, "_smartpbx_sinhala_prewarm_clock", clock.time)

    sleeps: list[float] = []

    async def _tracking_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(server, "_smartpbx_sinhala_prewarm_sleep", _tracking_sleep)

    fake_client = _RecordingClient(clock)
    monkeypatch.setattr(server, "_get_gemini_tts_client", lambda: fake_client)

    await server._prewarm_smartpbx_sinhala_phrase_audio()

    assert len(fake_client.interactions.calls) == len(server.SMARTPBX_SINHALA_CACHED_PHRASES)
    assert sleeps == []


# --- rate_limited backoff -----------------------------------------------------


@pytest.mark.asyncio
async def test_rate_limited_backs_off_doubling_up_to_three_retries_then_falls_back(monkeypatch):
    monkeypatch.setattr(server, "SMARTPBX_SINHALA_PHRASE_CACHE_DIR", "")
    monkeypatch.setattr(server, "SMARTPBX_SINHALA_PREWARM_INTERVAL_SECONDS", 7.0)
    clock = _FakeClock()
    monkeypatch.setattr(server, "_smartpbx_sinhala_prewarm_clock", clock.time)
    monkeypatch.setattr(server, "_smartpbx_sinhala_prewarm_sleep", clock.sleep)

    phrases = server.SMARTPBX_SINHALA_CACHED_PHRASES
    target = phrases[0]
    _prime_all_but({target})

    chain = server._smartpbx_sinhala_tts_model_chain()
    primary, fallback = chain[0], chain[1]
    fake_client = _ScriptedClient({
        primary: ["rate_limited", "rate_limited", "rate_limited", "rate_limited"],
        fallback: ["ok"],
    })
    monkeypatch.setattr(server, "_get_gemini_tts_client", lambda: fake_client)

    await server._prewarm_smartpbx_sinhala_phrase_audio()

    primary_calls = [c for c in fake_client.interactions.calls if c["model"] == primary]
    fallback_calls = [c for c in fake_client.interactions.calls if c["model"] == fallback]
    assert len(primary_calls) == 4  # 1 initial attempt + 3 retries
    assert len(fallback_calls) == 1
    assert server._get_cached_smartpbx_sinhala_phrase_audio(target) is not None
    assert server._smartpbx_sinhala_tts_model_is_exhausted(primary)

    # Backoff doubled each retry, capped at 60s: 14, 28, 56.
    backoff_sleeps = [s for s in clock.sleeps if s >= 10.0]
    assert backoff_sleeps == [14.0, 28.0, 56.0]


@pytest.mark.asyncio
async def test_rate_limited_exhausting_every_model_records_one_failure(monkeypatch):
    monkeypatch.setattr(server, "SMARTPBX_SINHALA_PHRASE_CACHE_DIR", "")
    monkeypatch.setattr(server, "SMARTPBX_SINHALA_PREWARM_INTERVAL_SECONDS", 0.0)
    monkeypatch.setattr(server, "_smartpbx_sinhala_prewarm_sleep", lambda seconds: asyncio.sleep(0))

    phrases = server.SMARTPBX_SINHALA_CACHED_PHRASES
    target = phrases[0]
    _prime_all_but({target})

    chain = server._smartpbx_sinhala_tts_model_chain()
    fake_client = _ScriptedClient({model: ["rate_limited"] * 4 for model in chain})
    monkeypatch.setattr(server, "_get_gemini_tts_client", lambda: fake_client)

    await server._prewarm_smartpbx_sinhala_phrase_audio()

    assert server._get_cached_smartpbx_sinhala_phrase_audio(target) is None
    for model in chain:
        assert server._smartpbx_sinhala_tts_model_is_exhausted(model)
    assert not server._smartpbx_sinhala_phrase_audio_ready()


# --- quota_exceeded stops the run ---------------------------------------------


@pytest.mark.asyncio
async def test_quota_exceeded_stops_the_run_immediately_and_marks_exhaustion(monkeypatch):
    monkeypatch.setattr(server, "SMARTPBX_SINHALA_PHRASE_CACHE_DIR", "")
    monkeypatch.setattr(server, "SMARTPBX_SINHALA_PREWARM_INTERVAL_SECONDS", 0.0)
    monkeypatch.setattr(server, "_smartpbx_sinhala_prewarm_sleep", lambda seconds: asyncio.sleep(0))

    phrases = server.SMARTPBX_SINHALA_CACHED_PHRASES
    failing_index = 2
    failing_text = phrases[failing_index]
    # Cache the prefix; leave failing_index and everything after it as a
    # genuine miss, so a run that fails to stop would keep going and cache
    # them too.
    for text in phrases[:failing_index]:
        server._store_cached_smartpbx_sinhala_phrase_audio(text, b"\xff" * 640)
    for text in phrases[failing_index:]:
        assert server._get_cached_smartpbx_sinhala_phrase_audio(text) is None

    primary = server._smartpbx_sinhala_tts_model_chain()[0]
    fake_client = _ScriptedClient({primary: ["quota_exceeded"]})
    monkeypatch.setattr(server, "_get_gemini_tts_client", lambda: fake_client)

    await server._prewarm_smartpbx_sinhala_phrase_audio()

    # Exactly one request was ever made -- the run stopped immediately.
    assert len(fake_client.interactions.calls) == 1
    assert fake_client.interactions.calls[0]["input"] == failing_text
    assert server._smartpbx_sinhala_tts_model_is_exhausted(primary)
    for text in phrases[failing_index:]:
        assert server._get_cached_smartpbx_sinhala_phrase_audio(text) is None


@pytest.mark.asyncio
async def test_quota_exceeded_when_every_model_already_exhausted_stops_without_a_request(monkeypatch):
    monkeypatch.setattr(server, "SMARTPBX_SINHALA_PHRASE_CACHE_DIR", "")
    for model in server._smartpbx_sinhala_tts_model_chain():
        server._mark_smartpbx_sinhala_tts_model_exhausted(model)

    fake_client = _RecordingClient(_FakeClock())
    monkeypatch.setattr(server, "_get_gemini_tts_client", lambda: fake_client)

    await server._prewarm_smartpbx_sinhala_phrase_audio()

    assert fake_client.interactions.calls == []


# --- fallback-model rendering is served ---------------------------------------


@pytest.mark.asyncio
async def test_fallback_model_rendering_is_cached_and_served_for_playback(monkeypatch):
    monkeypatch.setattr(server, "SMARTPBX_SINHALA_PHRASE_CACHE_DIR", "")
    monkeypatch.setattr(server, "SMARTPBX_SINHALA_PREWARM_INTERVAL_SECONDS", 0.0)
    monkeypatch.setattr(server, "_smartpbx_sinhala_prewarm_sleep", lambda seconds: asyncio.sleep(0))

    phrases = server.SMARTPBX_SINHALA_CACHED_PHRASES
    target = phrases[0]
    _prime_all_but({target})

    chain = server._smartpbx_sinhala_tts_model_chain()
    primary, fallback = chain[0], chain[1]
    fake_client = _ScriptedClient({
        primary: ["rate_limited", "rate_limited", "rate_limited", "rate_limited"],
        fallback: ["ok"],
    })
    monkeypatch.setattr(server, "_get_gemini_tts_client", lambda: fake_client)

    await server._prewarm_smartpbx_sinhala_phrase_audio()

    # Served for playback under the general lookup, exactly as a live call uses it.
    served = server._get_cached_smartpbx_sinhala_phrase_audio(target)
    assert served is not None
    with server._SMARTPBX_SINHALA_PHRASE_AUDIO_LOCK:
        assert server._SMARTPBX_SINHALA_PHRASE_AUDIO[
            server._smartpbx_sinhala_phrase_audio_key(target, fallback)
        ] == served
        assert server._smartpbx_sinhala_phrase_audio_key(target, primary) not in server._SMARTPBX_SINHALA_PHRASE_AUDIO


@pytest.mark.asyncio
async def test_fallback_model_render_is_logged(monkeypatch, caplog):
    monkeypatch.setattr(server, "SMARTPBX_SINHALA_PHRASE_CACHE_DIR", "")
    monkeypatch.setattr(server, "SMARTPBX_SINHALA_PREWARM_INTERVAL_SECONDS", 0.0)
    monkeypatch.setattr(server, "_smartpbx_sinhala_prewarm_sleep", lambda seconds: asyncio.sleep(0))

    phrases = server.SMARTPBX_SINHALA_CACHED_PHRASES
    target = phrases[0]
    _prime_all_but({target})

    chain = server._smartpbx_sinhala_tts_model_chain()
    primary, fallback = chain[0], chain[1]
    fake_client = _ScriptedClient({
        primary: ["rate_limited", "rate_limited", "rate_limited", "rate_limited"],
        fallback: ["ok"],
    })
    monkeypatch.setattr(server, "_get_gemini_tts_client", lambda: fake_client)

    with caplog.at_level(logging.INFO, logger="server"):
        await server._prewarm_smartpbx_sinhala_phrase_audio()

    fallback_lines = [
        r.message for r in caplog.records
        if "sinhala_phrase_prewarm_fallback_model" in r.message
    ]
    assert len(fallback_lines) == 1
    assert f"model={fallback}" in fallback_lines[0]
    assert "index=0" in fallback_lines[0]
    # The phrase text itself must never appear in the log line.
    assert target not in fallback_lines[0]


# --- status counts -------------------------------------------------------------


def _status_endpoint():
    app = server.build_service_app("smartpbx", {
        "ENABLE_SMARTPBX_WSS": "true",
        "SMARTPBX_WS_TOKEN": "status-token",
        "SMARTPBX_ACCOUNT_ID": "account-1",
    })
    endpoint = {route.path: route for route in app.routes}["/smartpbx/status"].endpoint
    request = SimpleNamespace(headers={"X-Kavya-SmartPBX-Token": "status-token"})
    return lambda: endpoint(request)


def test_status_reports_zero_ready_before_any_prewarm():
    status = _status_endpoint()
    body = status()
    assert body["sinhala_phrases_total"] == len(server.SMARTPBX_SINHALA_CACHED_PHRASES)
    assert body["sinhala_phrases_ready"] == 0


def test_status_reports_partial_and_full_readiness():
    status = _status_endpoint()
    phrases = server.SMARTPBX_SINHALA_CACHED_PHRASES

    server._store_cached_smartpbx_sinhala_phrase_audio(phrases[0], b"\xff" * 640)
    assert status()["sinhala_phrases_ready"] == 1

    for text in phrases:
        server._store_cached_smartpbx_sinhala_phrase_audio(text, b"\xff" * 640)
    body = status()
    assert body["sinhala_phrases_ready"] == len(phrases)
    assert body["sinhala_phrases_ready"] == body["sinhala_phrases_total"]


# --- summary line fields --------------------------------------------------------


@pytest.mark.asyncio
async def test_summary_line_carries_every_required_field(monkeypatch, caplog):
    monkeypatch.setattr(server, "SMARTPBX_SINHALA_PHRASE_CACHE_DIR", "")
    monkeypatch.setattr(server, "SMARTPBX_SINHALA_PREWARM_INTERVAL_SECONDS", 0.0)
    monkeypatch.setattr(server, "_smartpbx_sinhala_prewarm_sleep", lambda seconds: asyncio.sleep(0))

    phrases = server.SMARTPBX_SINHALA_CACHED_PHRASES
    quota_text = phrases[1]
    _prime_all_but({quota_text})

    primary = server._smartpbx_sinhala_tts_model_chain()[0]
    fake_client = _ScriptedClient({primary: ["quota_exceeded"]})
    monkeypatch.setattr(server, "_get_gemini_tts_client", lambda: fake_client)

    with caplog.at_level(logging.INFO, logger="server"):
        await server._prewarm_smartpbx_sinhala_phrase_audio()

    summary_lines = [
        r.message for r in caplog.records
        if r.message.startswith("smartpbx_media event=sinhala_phrase_prewarm ")
    ]
    assert len(summary_lines) == 1
    line = summary_lines[0]
    assert "loaded_from_disk=0" in line
    assert "synthesised=0" in line
    assert "failed=1" in line
    assert "failure_codes=quota_exceeded:1" in line
    assert "elapsed_ms=" in line
    assert "ready=false" in line
    assert quota_text not in line

    phrase_failed_lines = [
        r.message for r in caplog.records
        if "sinhala_phrase_prewarm_phrase_failed" in r.message
    ]
    assert len(phrase_failed_lines) == 1
    assert "index=1" in phrase_failed_lines[0]
    assert "code=quota_exceeded" in phrase_failed_lines[0]
    assert quota_text not in phrase_failed_lines[0]


# --- debounced re-prewarm trigger -----------------------------------------------


def test_schedule_prewarm_debounces_repeated_activation_retries(monkeypatch):
    async def _run():
        monkeypatch.setattr(server, "_smartpbx_sinhala_phrase_audio_ready", lambda: False)
        clock = _FakeClock()
        monkeypatch.setattr(server, "_smartpbx_sinhala_prewarm_clock", clock.time)

        started: list[int] = []

        async def _fake_prewarm():
            started.append(1)

        monkeypatch.setattr(server, "_prewarm_smartpbx_sinhala_phrase_audio", _fake_prewarm)

        server._schedule_smartpbx_sinhala_phrase_prewarm()
        await asyncio.sleep(0)
        assert len(started) == 1

        # A retry moments later (well under the 10-minute debounce) must not
        # start a second run, even though the first has already finished.
        clock.now += 5.0
        server._schedule_smartpbx_sinhala_phrase_prewarm()
        await asyncio.sleep(0)
        assert len(started) == 1

        # Past the debounce window, an activation retry may start a new run.
        clock.now += server._SMARTPBX_SINHALA_PHRASE_PREWARM_DEBOUNCE_SECONDS + 1.0
        server._schedule_smartpbx_sinhala_phrase_prewarm()
        await asyncio.sleep(0)
        assert len(started) == 2

    asyncio.run(_run())


def test_schedule_prewarm_force_bypasses_the_debounce(monkeypatch):
    async def _run():
        monkeypatch.setattr(server, "_smartpbx_sinhala_phrase_audio_ready", lambda: False)
        clock = _FakeClock()
        monkeypatch.setattr(server, "_smartpbx_sinhala_prewarm_clock", clock.time)

        started: list[int] = []

        async def _fake_prewarm():
            started.append(1)

        monkeypatch.setattr(server, "_prewarm_smartpbx_sinhala_phrase_audio", _fake_prewarm)

        server._schedule_smartpbx_sinhala_phrase_prewarm()
        await asyncio.sleep(0)
        assert len(started) == 1

        clock.now += 5.0  # well under the debounce window
        server._schedule_smartpbx_sinhala_phrase_prewarm(force=True)
        await asyncio.sleep(0)
        assert len(started) == 2

    asyncio.run(_run())


def test_schedule_prewarm_never_starts_a_second_run_while_one_is_in_flight(monkeypatch):
    async def _run():
        monkeypatch.setattr(server, "_smartpbx_sinhala_phrase_audio_ready", lambda: False)

        release = asyncio.Event()
        started = 0

        async def _fake_prewarm():
            nonlocal started
            started += 1
            await release.wait()

        monkeypatch.setattr(server, "_prewarm_smartpbx_sinhala_phrase_audio", _fake_prewarm)

        server._schedule_smartpbx_sinhala_phrase_prewarm()
        server._schedule_smartpbx_sinhala_phrase_prewarm(force=True)
        await asyncio.sleep(0)
        assert started == 1

        release.set()
        task = server._SMARTPBX_SINHALA_PHRASE_PREWARM[1]
        await task

    asyncio.run(_run())


def test_daily_reset_boundary_seconds_are_non_negative_and_bounded_by_a_day():
    now = server._smartpbx_utcnow()
    boundary = server._smartpbx_sinhala_tts_quota_reset_boundary(now)
    seconds = (boundary - now).total_seconds()
    assert 0 < seconds <= 24 * 3600
