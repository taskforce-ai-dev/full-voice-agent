"""Direct-SmartPBX Sinhala filler latency contract.

The Sinhala path used to give the caller nothing at all while the model
thought, and then serialised its tool filler in front of the PMS call. Both
gaps are audible: production measured a 3.5 s typical first token and one 8 s
initial-response timeout on a single Sinhala call.

These tests pin the two fixes and the property that makes them safe to run at
all: a Sinhala filler never costs a Gemini TTS round trip, because its audio is
rendered once per process and replayed from bytes.
"""

from __future__ import annotations

import asyncio
import base64
from types import SimpleNamespace

import pytest

import server


class FakeTransport:
    def __init__(self) -> None:
        self.audio: list[bytes] = []
        self.marks: list[str] = []
        self.clears = 0

    async def send_audio(self, audio: bytes) -> None:
        self.audio.append(audio)

    async def clear_audio(self) -> int:
        self.clears += 1
        return 0

    async def send_mark(self, name: str) -> None:
        self.marks.append(name)


class _FakeAsyncStream:
    def __init__(self, events):
        self._events = iter(events)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._events)
        except StopIteration:
            raise StopAsyncIteration from None


def _audio_event(payload: bytes):
    return SimpleNamespace(
        event_type="step.delta",
        delta=SimpleNamespace(
            type="audio",
            data=base64.b64encode(payload).decode("ascii"),
            mime_type="audio/l16",
            channels=1,
            sample_rate=24000,
        ),
    )


class _FakeInteractions:
    def __init__(self, payloads):
        self.payloads = payloads
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return _FakeAsyncStream([_audio_event(p) for p in self.payloads])


class FakeTTSClient:
    def __init__(self, payloads=(b"\x01\x02" * 2400,)):
        self.interactions = _FakeInteractions(list(payloads))
        self.aio = SimpleNamespace(interactions=self.interactions)


@pytest.fixture(autouse=True)
def _isolated_phrase_cache(monkeypatch):
    """Never let one test's rendered clips leak into the next."""
    monkeypatch.setattr(server, "_SMARTPBX_SINHALA_PHRASE_AUDIO", {})
    monkeypatch.setattr(server, "_SMARTPBX_SINHALA_PHRASE_PREWARM", None)
    monkeypatch.setattr(server, "GEMINI_API_KEY", "test-gemini-key")


def _sinhala_pipeline():
    transport = FakeTransport()
    pipeline = server.MediaStreamSession(
        websocket=None, lang="si", media_transport=transport, llm_provider="gemini",
    )
    pipeline._smartpbx_transfer_context = object()
    return pipeline, transport


# --- the cache itself ------------------------------------------------------

def test_only_fixed_operator_phrases_may_ever_be_cached():
    """Caller transcript and model output must never enter the phrase cache."""
    assert server.SMARTPBX_SINHALA_INITIAL_FILLER_TEXT in server.SMARTPBX_SINHALA_CACHED_PHRASES
    for phrase in server.MEDIA_STREAM_FILLERS["si"].values():
        assert phrase in server.SMARTPBX_SINHALA_CACHED_PHRASES

    server._store_cached_smartpbx_sinhala_phrase_audio("guest said something", b"\xff" * 640)
    assert server._get_cached_smartpbx_sinhala_phrase_audio("guest said something") is None


def test_prewarm_renders_every_fixed_phrase_once_and_only_once(monkeypatch):
    client = FakeTTSClient()
    monkeypatch.setattr(server, "_get_gemini_tts_client", lambda: client)

    asyncio.run(server._prewarm_smartpbx_sinhala_phrase_audio())

    assert server._smartpbx_sinhala_phrase_audio_ready()
    assert len(client.interactions.calls) == len(server.SMARTPBX_SINHALA_CACHED_PHRASES)
    clip = server._get_cached_smartpbx_sinhala_phrase_audio(
        server.SMARTPBX_SINHALA_INITIAL_FILLER_TEXT
    )
    assert clip and len(clip) % 640 == 0

    asyncio.run(server._prewarm_smartpbx_sinhala_phrase_audio())
    assert len(client.interactions.calls) == len(server.SMARTPBX_SINHALA_CACHED_PHRASES)


def test_a_cached_sinhala_phrase_is_spoken_without_any_provider_request(monkeypatch):
    """The whole point: a filler must cost zero synthesis latency."""
    pipeline, transport = _sinhala_pipeline()
    text = server.SMARTPBX_SINHALA_INITIAL_FILLER_TEXT
    clip = b"\x7f" * 1280
    server._store_cached_smartpbx_sinhala_phrase_audio(text, clip)

    def _forbidden():
        raise AssertionError("a cached phrase must not open a Gemini TTS stream")

    monkeypatch.setattr(server, "_get_gemini_tts_client", _forbidden)

    asyncio.run(pipeline._speak(text, sentence=text))

    assert b"".join(transport.audio) == clip
    assert transport.marks == ["tts_done"]


def test_an_uncached_sinhala_reply_still_streams_from_the_provider(monkeypatch):
    """Model speech is not a fixed phrase and must keep its streaming path."""
    pipeline, transport = _sinhala_pipeline()
    client = FakeTTSClient()
    pipeline._gemini_tts_client = client

    asyncio.run(pipeline._speak("සිංහල පිළිතුර", sentence="සිංහල පිළිතුර"))

    assert len(client.interactions.calls) == 1
    assert transport.audio


# --- A1: the initial filler ------------------------------------------------

def test_direct_sinhala_arms_the_initial_filler_once_its_audio_is_rendered():
    pipeline, _transport = _sinhala_pipeline()
    server._store_cached_smartpbx_sinhala_phrase_audio(
        server.SMARTPBX_SINHALA_INITIAL_FILLER_TEXT, b"\xff" * 640,
    )

    async def scenario():
        controller = pipeline._start_initial_smartpbx_filler(
            round_idx=0, generation=pipeline._speak_generation,
        )
        assert controller is not None
        assert controller.text == server.SMARTPBX_SINHALA_INITIAL_FILLER_TEXT
        await controller.on_barge_in()

    asyncio.run(scenario())


def test_direct_sinhala_withholds_the_initial_filler_until_its_audio_exists():
    """An unrendered filler would hold the speak lock through a 2-5 s synthesis."""
    pipeline, _transport = _sinhala_pipeline()

    async def scenario():
        return pipeline._start_initial_smartpbx_filler(
            round_idx=0, generation=pipeline._speak_generation,
        )

    assert asyncio.run(scenario()) is None


def test_direct_sinhala_initial_filler_reuses_the_english_delay_and_semantics():
    """Same controller, same delay knob, same content/tool/barge-in lifecycle."""
    pipeline, _transport = _sinhala_pipeline()
    server._store_cached_smartpbx_sinhala_phrase_audio(
        server.SMARTPBX_SINHALA_INITIAL_FILLER_TEXT, b"\xff" * 640,
    )
    spoken: list[str] = []

    async def scenario():
        async def fake_speak(text, generation=-1, sentence=None):
            spoken.append(text)

        pipeline._invoke_speak = fake_speak
        controller = pipeline._start_initial_smartpbx_filler(
            round_idx=0, generation=pipeline._speak_generation,
        )
        assert controller.delay_seconds == server.SMARTPBX_INITIAL_FILLER_DELAY_SECONDS
        # Content arriving before the delay elapses cancels a filler that has
        # not spoken -- exactly the English rule.
        await controller.on_content_delta()
        assert controller.spoke is False
        assert spoken == []

    asyncio.run(scenario())


def test_twilio_sinhala_media_streams_never_arm_a_smartpbx_initial_filler():
    """The filler belongs to the direct Dialog path only."""
    server._store_cached_smartpbx_sinhala_phrase_audio(
        server.SMARTPBX_SINHALA_INITIAL_FILLER_TEXT, b"\xff" * 640,
    )
    pipeline = server.MediaStreamSession(
        websocket=None, lang="si", media_transport=None, llm_provider="gemini",
    )

    async def scenario():
        return pipeline._start_initial_smartpbx_filler(
            round_idx=0, generation=pipeline._speak_generation,
        )

    assert asyncio.run(scenario()) is None


# --- A2: the tool filler runs beside the tool -------------------------------

def _sinhala_tool_pipeline(provider, rounds):
    from tests.test_smartpbx_server import direct_tool_client, direct_tool_pipeline

    client = direct_tool_client(provider, rounds)
    pipeline = direct_tool_pipeline(server, provider, client, lang="si")
    pipeline.history = [{"role": "user", "content": "guest asks about rooms"}]
    return pipeline


def _run_sinhala_tool_turn(monkeypatch, provider, *, tool_name):
    """Drive one Sinhala tool round with a filler that will not finish itself.

    The filler's TTS blocks until the tool has actually started. Serialising the
    filler in front of ``execute_tool`` therefore deadlocks the turn, which is
    exactly the production behaviour this change removes.
    """
    from tests.test_smartpbx_server import (
        bind_direct_smartpbx_turn, direct_text_round, direct_tool_round,
    )

    pipeline = _sinhala_tool_pipeline(provider, [
        direct_tool_round(provider, {"nights": 2}, tool_name=tool_name),
        direct_text_round(provider, "පිළිතුර."),
    ])
    events: list[str] = []
    tool_started = asyncio.Event()

    async def fake_tts(text, **_kwargs):
        events.append("tts_start:" + text)
        if text in server.MEDIA_STREAM_FILLERS["si"].values():
            await asyncio.wait_for(tool_started.wait(), timeout=2.0)
        events.append("tts_done:" + text)

    async def fake_execute_tool(name, _arguments):
        events.append("tool:" + name)
        tool_started.set()
        return '{"status": "ok"}'

    monkeypatch.setattr(pipeline, "_tts_gemini_sinhala", fake_tts)
    monkeypatch.setattr(server, "execute_tool", fake_execute_tool)

    async def scenario():
        bind_direct_smartpbx_turn(server, pipeline)
        runner = (
            pipeline._run_llm_gemini if provider == "gemini"
            else pipeline._run_llm_claude
        )
        await asyncio.wait_for(runner(), timeout=5.0)

    asyncio.run(scenario())
    return events


@pytest.mark.parametrize("provider", ["gemini", "claude"])
def test_direct_sinhala_tool_filler_runs_concurrently_with_the_tool(
    monkeypatch, provider,
):
    events = _run_sinhala_tool_turn(
        monkeypatch, provider, tool_name="check_availability",
    )
    filler = server.MEDIA_STREAM_FILLERS["si"]["check_availability"]

    assert "tts_start:" + filler in events
    # The load-bearing assertion: the PMS call started while the filler was
    # still being delivered, not after it had drained.
    assert events.index("tool:check_availability") < events.index("tts_done:" + filler)


@pytest.mark.parametrize(
    "tool_name", ["transfer_to_human", "capture_spoken_number"],
)
def test_excluded_sinhala_tools_keep_their_serialised_filler(tool_name):
    """Transfer owns its own announcement; capture keeps its specialised flow."""
    pipeline, _transport = _sinhala_pipeline()

    assert not pipeline._is_direct_smartpbx_sinhala_tool_filler_round(
        tool_name, text_content="", filler_sent=False, initial_filler=None,
    )


def test_twilio_sinhala_media_streams_keep_their_serialised_filler():
    """Only the direct Dialog path gains the concurrent filler."""
    pipeline = server.MediaStreamSession(
        websocket=None, lang="si", media_transport=None, llm_provider="gemini",
    )

    assert not pipeline._is_direct_smartpbx_sinhala_tool_filler_round(
        "check_availability",
        text_content="", filler_sent=False, initial_filler=None,
    )


def test_a_spoken_initial_filler_suppresses_the_sinhala_tool_filler():
    """One filler per turn, exactly as on the English path."""
    pipeline, _transport = _sinhala_pipeline()
    spoke = SimpleNamespace(suppress_specialized_tool_filler=True)
    pending = SimpleNamespace(suppress_specialized_tool_filler=False)

    assert not pipeline._is_direct_smartpbx_sinhala_tool_filler_round(
        "check_availability",
        text_content="", filler_sent=False, initial_filler=spoke,
    )
    assert pipeline._is_direct_smartpbx_sinhala_tool_filler_round(
        "check_availability",
        text_content="", filler_sent=False, initial_filler=pending,
    )
    assert not pipeline._is_direct_smartpbx_sinhala_tool_filler_round(
        "check_availability",
        text_content="මම බලනවා.", filler_sent=False, initial_filler=None,
    )
    assert not pipeline._is_direct_smartpbx_sinhala_tool_filler_round(
        "check_availability",
        text_content="", filler_sent=True, initial_filler=None,
    )


# --- A1 x A7: the new Sinhala filler must not survive a discarded round -----

def test_a_cached_sinhala_filler_is_retired_by_a_max_tokens_discard(monkeypatch):
    """The Sinhala path now arms a filler, so the discard fence must retire it.

    A filler left running across a `max_tokens_truncated` discard would speak
    over the retry's answer with nothing left able to cancel it.
    """
    from tests.test_gemini_streaming import (
        _empty_chunk, _session, _terminal_chunk, _text_chunk,
    )
    from tests.test_smartpbx_server import bind_direct_smartpbx_turn

    server._store_cached_smartpbx_sinhala_phrase_audio(
        server.SMARTPBX_SINHALA_INITIAL_FILLER_TEXT, b"\xff" * 640,
    )
    monkeypatch.setattr(server, "SMARTPBX_INITIAL_FILLER_DELAY_SECONDS", 0.0)

    answer = "සම්පූර්ණ පිළිතුර."
    session, spoken = _session(
        [
            [_text_chunk("පළමු වාක්‍යය. "), _empty_chunk("MAX_TOKENS")],
            [_text_chunk(answer), _terminal_chunk()],
        ],
        lang="si",
        smartpbx=True,
        terminalize_direct_rounds=False,
    )

    controllers = []
    real_start = session._start_initial_smartpbx_filler

    def spy(**kwargs):
        controller = real_start(**kwargs)
        if controller is not None:
            controllers.append(controller)
        return controller

    session._start_initial_smartpbx_filler = spy

    async def scenario():
        bind_direct_smartpbx_turn(server, session)
        return await session._run_llm_gemini()

    result = asyncio.run(scenario())

    assert controllers, "a prewarmed process must arm the Sinhala initial filler"
    assert controllers[0]._task.done()
    assert session._smartpbx_initial_filler is None
    assert result == answer
    assert spoken[-1] == answer
    assert not any("සමාවෙන්න" in text for text in spoken)


# --- audible-state claim on the first frame (real paced transport) ----------
def test_a_cached_phrase_claims_speaking_state_on_its_first_frame_not_after_playback():
    """Regression: the cached clip used to go out as one un-framed blob, so the
    paced sender drained the whole phrase before ``_mark_tts_audible`` ran.
    ``_is_speaking`` stayed False for the entire audible filler, every STT
    result was routed through the pre-audio path, and two short back-channel
    interims (below BARGEIN_MIN_CHARS) barged in on the filler itself, bumping
    the speak generation and orphaning the real answer.  Uses the real
    ``SmartPBXMediaTransport`` because a fake instantaneous ``send_audio`` is
    exactly what hid this."""
    from smartpbx_protocol import CallContext, MediaFormat
    from smartpbx_transport import SmartPBXMediaTransport

    class _WS:
        def __init__(self) -> None:
            self.sent = 0

        async def send_text(self, _text: str) -> None:
            self.sent += 1

    async def scenario() -> tuple[int, int, int, bool, int]:
        ws = _WS()
        context = CallContext("c", "o", "1", "2", "a", MediaFormat("PCMU", 8000))
        transport = SmartPBXMediaTransport(ws, context)
        transport.start()
        await asyncio.sleep(0)
        pipeline = server.MediaStreamSession(
            websocket=None, lang="si", media_transport=transport, llm_provider="gemini",
        )
        pipeline._smartpbx_transfer_context = object()
        pipeline._event_loop = asyncio.get_running_loop()
        text = server.SMARTPBX_SINHALA_INITIAL_FILLER_TEXT
        server._store_cached_smartpbx_sinhala_phrase_audio(text, b"\xff" * 8000)  # 1.0 s
        gen0 = pipeline._speak_generation
        speaking_seen = False

        async def back_channel() -> None:
            nonlocal speaking_seen
            await asyncio.sleep(0.25)
            for interim in ("\u0dc4\u0dcf", "\u0dc4\u0dcf \u0dc4\u0dbb\u0dd2"):
                speaking_seen = speaking_seen or pipeline._is_speaking
                # Exactly the STT-thread routing in _on_stt_interim.
                if pipeline._pre_audio_synthesis_active():
                    await pipeline._handle_pre_audio_stt("interim", interim)
                elif pipeline._is_speaking:
                    if not pipeline._is_echo(interim) and pipeline._should_barge_in(interim):
                        await pipeline._handle_bargein()
                else:
                    await pipeline._set_transcript_interim(interim)
                await asyncio.sleep(0.2)

        noise = asyncio.create_task(back_channel())
        try:
            await pipeline._speak(text, sentence=text)
        finally:
            await noise
            await transport.close()
        return gen0, pipeline._speak_generation, pipeline._smartpbx_barge_ins, speaking_seen, ws.sent

    gen0, gen1, barge_ins, speaking_seen, sent = asyncio.run(scenario())

    assert speaking_seen, "audible speaking state must be claimed while the cached clip plays"
    assert gen1 == gen0, "a sub-threshold back-channel must not barge in on the filler"
    assert barge_ins == 0
    assert sent >= 8000 // 160, "every paced frame of the clip reached the wire"
