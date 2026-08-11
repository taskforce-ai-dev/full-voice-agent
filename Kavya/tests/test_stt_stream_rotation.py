"""Google STT stream rotation, swap buffering, and telephony recognition config.

Google terminates one ``streaming_recognize`` at 305 seconds of AUDIO. Live on
2026-08-11 a call discovered that ceiling the hard way::

    11:02:03 Google STT stream started (lang=en)
    11:07:09 STT stream ended (400 Exceeded maximum allowed stream duration
             of 305 seconds.) — restart 1/8 in 0.2s
    11:10:37 Google STT stream started (lang=en)     <- a NEW CALL, 3.5 min later

The agent was deaf for three and a half minutes. The announced 0.2 s restart did
open a new stream, but the DEAD stream's request generator was still alive on
gRPC's writer thread — ``_audio_generator`` looped on ``self._running``, which is
still true after one stream ends — so it kept pulling the caller's frames off the
single shared queue and posting them into a closed RPC. Whoever won the race got
half the audio.

These tests pin the three fixes at the seams: the stream is rotated BEFORE the
ceiling, the old generator dies the moment its stream does, and audio spoken
across the swap is replayed into the replacement.
"""

from __future__ import annotations

import itertools
import queue
import types

import pytest

import server


class FakeClock:
    """Monotonic clock under test control — rotation is a four-minute behaviour."""

    def __init__(self, start: float = 1_000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _stream(lang: str = "en", clock: FakeClock | None = None):
    stt = server.GoogleSTTStream(on_final_result=lambda *_: None, lang=lang)
    stt._running = True
    stt._clock = clock or FakeClock()
    return stt


def _fake_google_speech(response_batches=None, *, fail_on_model=False, drain=0):
    """Stand-in for the google-cloud-speech surface the stream code touches.

    ``drain`` pulls that many frames off the request generator inside the call,
    which is what gRPC's writer thread does in production.
    """
    recorded: dict[str, list] = {"configs": [], "request_iters": [], "sent": []}

    class _AudioEncoding:
        MULAW = "MULAW"

    class RecognitionConfig:
        AudioEncoding = _AudioEncoding

        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class StreamingRecognitionConfig:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class StreamingRecognizeRequest:
        def __init__(self, **kwargs):
            self.audio_content = kwargs.get("audio_content")

    class SpeechContext:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class SpeechClient:
        def streaming_recognize(self, *, config, requests):
            recorded["configs"].append(config.kwargs["config"].kwargs)
            if fail_on_model and "model" in config.kwargs["config"].kwargs:
                raise ValueError("model 'telephony' is not supported for this language")
            recorded["request_iters"].append(requests)
            if drain:
                recorded["sent"].extend(
                    request.audio_content
                    for request in itertools.islice(requests, drain)
                )
            batch = (response_batches or [[]]).pop(0) if response_batches else []
            return iter(batch)

        def close(self):
            pass

    fake = types.SimpleNamespace(
        RecognitionConfig=RecognitionConfig,
        StreamingRecognitionConfig=StreamingRecognitionConfig,
        StreamingRecognizeRequest=StreamingRecognizeRequest,
        SpeechContext=SpeechContext,
        SpeechClient=SpeechClient,
    )
    return fake, recorded


# --- proactive rotation ---------------------------------------------------

def test_rotation_is_due_before_googles_305_second_ceiling():
    clock = FakeClock()
    stt = _stream(clock=clock)
    started = clock.now
    stt._last_voice_activity = clock.now

    assert server.STT_STREAM_ROTATE_SECONDS < 305.0
    assert server.STT_STREAM_ROTATE_DEADLINE_SECONDS < 305.0

    clock.advance(server.STT_STREAM_ROTATE_SECONDS - 1)
    assert stt._rotation_due(started) is False

    clock.advance(2)  # past the rotation age, and the caller has been quiet
    assert stt._rotation_due(started) is True


def test_rotation_waits_for_a_gap_in_the_callers_speech():
    clock = FakeClock()
    stt = _stream(clock=clock)
    started = clock.now
    clock.advance(server.STT_STREAM_ROTATE_SECONDS + 5)

    # The caller is mid-word: an interim landed just now.
    stt._last_voice_activity = clock.now
    assert stt._rotation_due(started) is False, "a swap mid-word clips a syllable"

    clock.advance(server.STT_ROTATE_QUIET_SECONDS + 0.05)
    assert stt._rotation_due(started) is True


def test_a_caller_who_never_pauses_still_rotates_before_the_ceiling():
    clock = FakeClock()
    stt = _stream(clock=clock)
    started = clock.now
    clock.advance(server.STT_STREAM_ROTATE_DEADLINE_SECONDS + 0.1)
    stt._last_voice_activity = clock.now  # still talking, no gap ever

    assert stt._rotation_due(started) is True, (
        "the hard deadline must win: a clipped syllable beats a 400 and a deaf call"
    )


def test_the_generator_ends_the_stream_when_rotation_is_due():
    clock = FakeClock()
    stt = _stream(clock=clock)
    started = clock.now
    stt._last_voice_activity = clock.now
    for _ in range(5):
        stt.feed(b"\xff" * 160)

    generator = stt._audio_generator(epoch=stt._stream_epoch, started_at=started)
    assert next(generator) == b"\xff" * 160

    clock.advance(server.STT_STREAM_ROTATE_DEADLINE_SECONDS + 1)
    with pytest.raises(StopIteration):
        next(generator)

    assert stt._rotations == 1
    assert stt._running is True, "a rotation is not a failure — the worker keeps going"
    assert stt._audio_q.qsize() > 0, "unsent audio stays queued for the next stream"


# --- the epoch fence (the actual deafness bug) ----------------------------

def test_a_superseded_generator_stops_stealing_audio():
    stt = _stream()
    stale = stt._audio_generator(epoch=stt._stream_epoch)
    stt.feed(b"first")
    assert next(stale) == b"first"

    # The stream this generator belonged to has ended and a new one opened.
    stt._stream_epoch += 1
    stt.feed(b"second")

    with pytest.raises(StopIteration):
        next(stale)
    assert stt._audio_q.get_nowait() == b"second", (
        "frames fed after the swap must be left for the live stream, not consumed "
        "by the dead one"
    )


def test_each_stream_bumps_the_epoch_so_the_previous_generator_dies(monkeypatch):
    fake, _recorded = _fake_google_speech()
    monkeypatch.setattr(server, "google_speech", fake)
    stt = _stream()

    before = stt._stream_epoch
    stt._run_one_stream()
    assert stt._stream_epoch == before + 1


def test_the_epoch_is_bumped_even_when_the_stream_fails(monkeypatch):
    def explode(*_args, **_kwargs):
        raise RuntimeError("400 Exceeded maximum allowed stream duration of 305 seconds")

    fake, _recorded = _fake_google_speech()
    fake.SpeechClient.streaming_recognize = explode
    monkeypatch.setattr(server, "google_speech", fake)
    stt = _stream()

    before = stt._stream_epoch
    with pytest.raises(RuntimeError):
        stt._run_one_stream()

    assert stt._stream_epoch == before + 1, (
        "after the 305s 400 the dead generator MUST be fenced — leaving it alive "
        "is what made the call deaf for 3.5 minutes"
    )


def test_restart_after_a_simulated_400_is_immediate(monkeypatch):
    """A clean rotation or a 400 must both put a live stream back within ~1s."""
    stt = _stream()
    opens: list[int] = []
    sleeps: list[float] = []

    def one_stream():
        opens.append(1)
        if len(opens) == 1:
            raise RuntimeError(
                "400 Exceeded maximum allowed stream duration of 305 seconds."
            )
        if len(opens) >= 3:
            stt._running = False

    monkeypatch.setattr(stt, "_run_one_stream", one_stream)
    monkeypatch.setattr(server.time, "sleep", lambda seconds: sleeps.append(seconds))

    stt._loop()

    assert len(opens) >= 2, "the stream must be reopened after the 400"
    assert sum(sleeps) <= 1.0, f"reopen must not take a second: slept {sleeps}"


# --- audio buffered across the swap --------------------------------------

def test_audio_spoken_during_the_swap_is_delivered_to_the_new_stream(monkeypatch):
    fake, recorded = _fake_google_speech(drain=5)
    monkeypatch.setattr(server, "google_speech", fake)
    stt = _stream()

    # The caller keeps talking while no stream is accepting.
    for index in range(5):
        stt.feed(bytes([index]) * 160)

    stt._run_one_stream()

    assert recorded["sent"] == [bytes([index]) * 160 for index in range(5)], (
        "audio queued across the swap must be replayed into the replacement stream"
    )


def test_a_deep_backlog_is_trimmed_oldest_first():
    stt = _stream()
    for index in range(server.STT_SWAP_BUFFER_CHUNKS + 120):
        stt.feed(index.to_bytes(4, "big"))

    dropped = stt._trim_stale_backlog()

    assert dropped == 120
    assert stt._audio_q.qsize() == server.STT_SWAP_BUFFER_CHUNKS
    # The OLDEST went, so the next frame out is the 120th one fed.
    assert stt._audio_q.get_nowait() == (120).to_bytes(4, "big")


def test_trimming_never_swallows_the_stop_sentinel():
    stt = _stream()
    for index in range(server.STT_SWAP_BUFFER_CHUNKS + 50):
        stt.feed(index.to_bytes(4, "big"))
    stt.stop()  # queues the None sentinel

    stt._trim_stale_backlog()

    frames = []
    while True:
        try:
            frames.append(stt._audio_q.get_nowait())
        except queue.Empty:
            break
    assert None in frames, "dropping the sentinel would hang stop() for 5s"


def test_a_short_backlog_is_left_alone():
    stt = _stream()
    for _ in range(10):
        stt.feed(b"\x00" * 160)

    assert stt._trim_stale_backlog() == 0
    assert stt._audio_q.qsize() == 10


# --- recognition config: telephony model + adaptation --------------------

def test_english_uses_the_telephony_model_and_the_booking_phrase_list(monkeypatch):
    fake, recorded = _fake_google_speech()
    monkeypatch.setattr(server, "google_speech", fake)
    stt = _stream(lang="en")

    stt._run_one_stream()

    config = recorded["configs"][0]
    assert config["model"] == "telephony"
    assert config["language_code"] == "en-US"
    assert config["enable_automatic_punctuation"] is True
    # English must never name en-US as an alternative to itself.
    assert config["alternative_language_codes"] == []
    contexts = config["speech_contexts"]
    assert len(contexts) == 1
    assert contexts[0].kwargs["phrases"] == list(server.EN_STT_PHRASE_LIST)
    assert contexts[0].kwargs["boost"] == server.STT_ADAPTATION_BOOST


@pytest.mark.parametrize("lang", ["si", "ta", "ar"])
def test_the_retained_languages_keep_the_default_model(monkeypatch, lang):
    """Telephony-model coverage for si-LK/ta-IN is unverified — do not risk it."""
    fake, recorded = _fake_google_speech()
    monkeypatch.setattr(server, "google_speech", fake)
    stt = _stream(lang=lang)

    stt._run_one_stream()

    config = recorded["configs"][0]
    assert "model" not in config
    assert "speech_contexts" not in config
    assert config["language_code"] == server.STT_PRIMARY[lang]
    assert config["alternative_language_codes"] == server.STT_ALTERNATIVES[lang]


def test_a_rejected_telephony_model_degrades_instead_of_failing_the_call(monkeypatch):
    fake, recorded = _fake_google_speech(fail_on_model=True)
    monkeypatch.setattr(server, "google_speech", fake)
    stt = _stream(lang="en")

    stt._run_one_stream()  # must not raise

    assert len(recorded["configs"]) == 2
    assert "model" in recorded["configs"][0]
    assert "model" not in recorded["configs"][1]
    assert stt._enhanced_config_rejected is True

    # And it is not retried on every subsequent stream.
    recorded["configs"].clear()
    stt._run_one_stream()
    assert len(recorded["configs"]) == 1
    assert "model" not in recorded["configs"][0]


def test_an_older_sdk_without_speech_context_still_streams(monkeypatch):
    fake, recorded = _fake_google_speech()
    del fake.SpeechContext
    monkeypatch.setattr(server, "google_speech", fake)
    stt = _stream(lang="en")

    stt._run_one_stream()

    config = recorded["configs"][0]
    assert config["model"] == "telephony"
    assert "speech_contexts" not in config


def test_a_provider_response_refreshes_the_voice_activity_marker(monkeypatch):
    clock = FakeClock()
    alternative = types.SimpleNamespace(transcript=" checking in tomorrow ")
    result = types.SimpleNamespace(alternatives=[alternative], is_final=False)
    response = types.SimpleNamespace(results=[result])
    fake, _recorded = _fake_google_speech([[response]])
    monkeypatch.setattr(server, "google_speech", fake)

    interims: list[str] = []
    stt = server.GoogleSTTStream(
        on_final_result=lambda *_: None,
        on_interim_result=interims.append,
        lang="en",
    )
    stt._running = True
    stt._clock = clock

    clock.advance(30)
    stt._run_one_stream()

    assert interims == ["checking in tomorrow"]
    assert stt._last_voice_activity == clock.now, (
        "rotation must know when the caller was last speaking"
    )


# --- ElevenLabs streaming latency knob -----------------------------------

def test_the_elevenlabs_url_asks_for_low_latency_streaming():
    url = server._elevenlabs_stream_url("voice-1")

    assert url.startswith(
        "https://api.elevenlabs.io/v1/text-to-speech/voice-1/stream?output_format=ulaw_8000"
    )
    assert "optimize_streaming_latency=3" in url


def test_the_latency_knob_can_be_switched_off(monkeypatch):
    monkeypatch.setattr(server, "ELEVENLABS_OPTIMIZE_STREAMING_LATENCY", 0)

    assert "optimize_streaming_latency" not in server._elevenlabs_stream_url("voice-1")


def test_the_latency_knob_is_clamped_to_the_supported_range():
    parse = server._parse_clamped_int
    assert parse({"K": "3"}, "K", 3, 0, 4) == 3
    assert parse({"K": "9"}, "K", 3, 0, 4) == 4
    assert parse({"K": "-2"}, "K", 3, 0, 4) == 0
    assert parse({"K": "nope"}, "K", 3, 0, 4) == 3
    assert parse({}, "K", 3, 0, 4) == 3
    assert 0 <= server.ELEVENLABS_OPTIMIZE_STREAMING_LATENCY <= 4
