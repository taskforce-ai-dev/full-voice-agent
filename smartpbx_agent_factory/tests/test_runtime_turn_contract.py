"""Executable contract for the candidate client-neutral conversation lane.

These tests intentionally exercise the templates directly.  The checked-in
candidate remains blocked from rendering until the integration reviewer accepts
the whole runtime provenance set.
"""

from __future__ import annotations

import asyncio
import importlib
import sys
from pathlib import Path

import pytest


@pytest.fixture
def runtime_module(tmp_path, monkeypatch):
    """Load the three dependency-free runtime templates as disposable modules."""
    runtime = Path(__file__).parents[1] / "template_v1" / "runtime"
    for name in ("product_profile", "provider_adapters", "turn_engine", "smartpbx_session"):
        (tmp_path / f"{name}.py").write_text(
            (runtime / f"{name}.py.tmpl").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.syspath_prepend(str(tmp_path))
    return importlib.import_module("turn_engine")


class RecordingTransport:
    def __init__(self) -> None:
        self.events: list[tuple[str, bytes | str]] = []
        self.pending_audio: list[bytes] = []
        self.clears = 0
        self.audio_sent = asyncio.Event()
        self.mark_sent = asyncio.Event()
        self.audio_cleared = asyncio.Event()
        self.mark_release = asyncio.Event()
        self.hold_mark = False

    async def send_audio(self, audio: bytes) -> None:
        self.events.append(("audio", audio))
        self.pending_audio.append(audio)
        self.audio_sent.set()

    async def send_mark(self, name: str) -> None:
        self.events.append(("mark", name))
        self.mark_sent.set()
        if self.hold_mark:
            await self.mark_release.wait()
        self.pending_audio.clear()

    async def clear_audio(self) -> None:
        self.clears += 1
        self.audio_cleared.set()


class RecordingRecognizer:
    def __init__(self, callback) -> None:
        self._callback = callback
        self.frames: list[bytes] = []
        self.closed = False

    async def feed_audio(self, audio: bytes) -> None:
        self.frames.append(bytes(audio))

    async def close(self) -> None:
        self.closed = True

    def emit(self, result) -> None:
        self._callback(result)


class RecordingAdapter:
    active = True

    def __init__(self) -> None:
        self.recognizer: RecordingRecognizer | None = None
        self.started_languages: list[str] = []
        self.generated: list[str] = []
        self.block_first_response = False
        self.first_response_started = asyncio.Event()
        self.response_factory = None

    async def start_recognizer(self, language: str, on_result) -> RecordingRecognizer:
        self.started_languages.append(language)
        self.recognizer = RecordingRecognizer(on_result)
        return self.recognizer

    async def generate_response(self, transcript: str, _language: str, _prompt: str):
        self.generated.append(transcript)
        if self.response_factory is not None:
            return self.response_factory(transcript)
        if self.block_first_response and transcript == "first":
            self.first_response_started.set()
            await asyncio.Event().wait()
        return transcript

    async def synthesize_audio(self, response: str, _language: str) -> bytes:
        return response.encode("ascii")

async def _new_engine(runtime_module, adapter: RecordingAdapter, transport: RecordingTransport, **options):
    profile_module = importlib.import_module("product_profile")
    language = profile_module.test_product_profile().language("en")
    engine = runtime_module.ConversationTurnEngine(
        adapter,
        transport,
        profile_module.test_product_profile(),
        endpointing_silence_seconds=0.0,
        final_grace_seconds=0.0,
        **options,
    )
    await engine.start(language)
    assert adapter.recognizer is not None
    return engine, language, adapter.recognizer


async def _await_event(event: asyncio.Event) -> None:
    """Synchronize with the event-loop bridge; never count scheduler ticks."""
    await asyncio.wait_for(event.wait(), timeout=0.5)


def test_continuous_recognizer_owns_audio_and_final_endpoint(runtime_module):
    async def exercise():
        adapter, transport = RecordingAdapter(), RecordingTransport()
        engine, language, recognizer = await _new_engine(runtime_module, adapter, transport)
        result = runtime_module.RecognizerResult

        await engine.accept_audio(b"one", language)
        await engine.accept_audio(b"two", language)
        recognizer.emit(result("partial", is_final=False, result_id=1))
        recognizer.emit(result("complete inquiry", is_final=True, result_id=2))
        await engine.drain()

        assert adapter.started_languages == ["en"]
        assert recognizer.frames == [b"one", b"two"]
        assert adapter.generated == ["complete inquiry"]
        assert transport.events == [("audio", b"complete inquiry"), ("mark", "conversation-turn")]
        assert engine.turns_completed == 1

    asyncio.run(exercise())


def test_exact_duplicate_final_never_dispatches_a_second_turn(runtime_module):
    async def exercise():
        adapter, transport = RecordingAdapter(), RecordingTransport()
        engine, _language, recognizer = await _new_engine(runtime_module, adapter, transport)
        result = runtime_module.RecognizerResult

        recognizer.emit(result("new final", is_final=True, result_id=4))
        recognizer.emit(result("new final", is_final=True, result_id=4))
        await engine.drain()

        assert adapter.generated == ["new final"]
        assert engine.turns_completed == 1

    asyncio.run(exercise())


def test_barge_in_cancels_old_generation_and_fences_late_tts(runtime_module):
    async def exercise():
        adapter, transport = RecordingAdapter(), RecordingTransport()
        transport.hold_mark = True
        engine, _language, recognizer = await _new_engine(
            runtime_module, adapter, transport, barge_in_debounce_seconds=0.0
        )
        result = runtime_module.RecognizerResult

        recognizer.emit(result("first", is_final=True, result_id=1))
        await _await_event(transport.audio_sent)
        recognizer.emit(result("this is a material interruption", is_final=False, result_id=2))
        recognizer.emit(result("second", is_final=True, result_id=3))
        transport.mark_release.set()
        await engine.drain()

        assert transport.clears == 1
        assert adapter.generated == ["first", "second"]
        assert ("audio", b"second") in transport.events
        assert ("audio", b"late") not in transport.events
        assert engine.turns_completed == 1

    asyncio.run(exercise())


def test_pre_audio_requires_sustained_material_speech_before_barge_in(runtime_module):
    async def exercise():
        adapter, transport = RecordingAdapter(), RecordingTransport()
        adapter.block_first_response = True
        now = [0.0]
        engine, _language, recognizer = await _new_engine(
            runtime_module,
            adapter,
            transport,
            clock=lambda: now[0],
            pre_audio_min_seconds=0.25,
        )
        result = runtime_module.RecognizerResult

        recognizer.emit(result("first", is_final=True, result_id="first"))
        await asyncio.wait_for(adapter.first_response_started.wait(), timeout=0.2)
        await engine._handle_recognizer_result(
            engine._recognizer_epoch,
            result("this is material but only one callback", is_final=False),
        )
        assert transport.clears == 0

        now[0] = 0.25
        await engine._handle_recognizer_result(
            engine._recognizer_epoch,
            result("this is the second sustained callback", is_final=False),
        )
        await _await_event(transport.audio_cleared)
        assert transport.clears == 1

        await engine.close()

    asyncio.run(exercise())


def test_short_interim_during_speech_is_buffered_without_barge_in(runtime_module):
    async def exercise():
        adapter, transport = RecordingAdapter(), RecordingTransport()
        transport.hold_mark = True
        engine, _language, recognizer = await _new_engine(runtime_module, adapter, transport)
        result = runtime_module.RecognizerResult

        recognizer.emit(result("first reply", is_final=True, result_id="first"))
        for _ in range(4):
            await asyncio.sleep(0)
        recognizer.emit(result("mm", is_final=False))
        for _ in range(4):
            await asyncio.sleep(0)

        assert transport.clears == 0
        assert engine.turns_completed == 0
        transport.mark_release.set()
        await engine.drain()
        assert adapter.generated == ["first reply", "mm"]

    asyncio.run(exercise())


def test_immediate_material_speech_after_audio_obeys_debounce(runtime_module):
    async def exercise():
        adapter, transport = RecordingAdapter(), RecordingTransport()
        transport.hold_mark = True
        engine, _language, recognizer = await _new_engine(
            runtime_module, adapter, transport, barge_in_debounce_seconds=0.6
        )
        result = runtime_module.RecognizerResult

        recognizer.emit(result("first reply", is_final=True, result_id="first"))
        for _ in range(4):
            await asyncio.sleep(0)
        recognizer.emit(result("this is long enough but arrived immediately", is_final=False))
        for _ in range(4):
            await asyncio.sleep(0)

        assert transport.clears == 0
        transport.mark_release.set()
        await engine.drain()

    asyncio.run(exercise())


def test_provider_echo_is_not_admitted_as_a_barge_in(runtime_module):
    async def exercise():
        adapter, transport = RecordingAdapter(), RecordingTransport()
        transport.hold_mark = True
        engine, _language, recognizer = await _new_engine(
            runtime_module, adapter, transport, barge_in_debounce_seconds=0.0
        )
        result = runtime_module.RecognizerResult

        recognizer.emit(result("this is echoed assistant audio", is_final=True, result_id="first"))
        await _await_event(transport.audio_sent)
        await engine._handle_recognizer_result(
            engine._recognizer_epoch,
            result("this is echoed assistant audio", is_final=False),
        )

        assert transport.clears == 0
        transport.mark_release.set()
        await engine.drain()
        assert adapter.generated == ["this is echoed assistant audio"]

    asyncio.run(exercise())


def test_reset_or_reordered_provider_ids_do_not_reject_fresh_speech(runtime_module):
    async def exercise():
        adapter, transport = RecordingAdapter(), RecordingTransport()
        engine, _language, recognizer = await _new_engine(runtime_module, adapter, transport)
        result = runtime_module.RecognizerResult

        recognizer.emit(result("first final", is_final=True, result_id=9))
        await engine.drain()
        recognizer.emit(result("second final", is_final=True, result_id=1))
        await engine.drain()

        assert adapter.generated == ["first final", "second final"]
        assert engine.turns_completed == 2

    asyncio.run(exercise())


def test_late_epoch_callback_cannot_start_a_task_or_emit_stale_audio(runtime_module):
    async def exercise():
        adapter, transport = RecordingAdapter(), RecordingTransport()
        engine, _language, recognizer = await _new_engine(runtime_module, adapter, transport)
        result = runtime_module.RecognizerResult

        await engine.close()
        recognizer.emit(result("late final", is_final=True, result_id="late"))
        await engine.drain()

        assert adapter.generated == []
        assert transport.events == []
        assert engine.turns_completed == 0

    asyncio.run(exercise())


def test_recognizer_fatal_fences_turn_media_and_completes_once(runtime_module):
    async def exercise():
        adapter, transport = RecordingAdapter(), RecordingTransport()
        adapter.block_first_response = True
        engine, _language, recognizer = await _new_engine(runtime_module, adapter, transport)
        result = runtime_module.RecognizerResult
        fatal = runtime_module.RecognizerFatal

        recognizer.emit(result("question", is_final=True))
        await asyncio.wait_for(adapter.first_response_started.wait(), timeout=0.2)
        recognizer.emit(fatal("provider_unavailable"))
        await engine.drain()

        assert engine.terminal_failure == fatal("provider_unavailable")
        assert transport.clears == 1
        assert recognizer.closed is True
        assert engine.turns_completed == 0
        assert adapter.generated == ["question"]

        recognizer.emit(fatal("provider_unavailable"))
        await engine.drain()
        assert transport.clears == 1

    asyncio.run(exercise())


def test_expected_recognizer_close_never_reports_a_fatal(runtime_module):
    async def exercise():
        adapter, transport = RecordingAdapter(), RecordingTransport()
        engine, _language, recognizer = await _new_engine(runtime_module, adapter, transport)

        await engine.close()

        assert recognizer.closed is True
        assert engine.terminal_failure is None
        assert transport.clears == 1

    asyncio.run(exercise())


def test_session_completes_one_terminal_failure_signal_for_recognizer_fatal(runtime_module):
    async def exercise():
        session_module = importlib.import_module("smartpbx_session")
        profile_module = importlib.import_module("product_profile")
        adapter, transport = RecordingAdapter(), RecordingTransport()
        session = session_module.InquirySmartPBXSession(
            None,
            transport,
            None,
            provider_adapter=adapter,
            product_profile=profile_module.test_product_profile(),
        )
        await session.start()
        assert adapter.recognizer is not None

        adapter.recognizer.emit(runtime_module.RecognizerFatal("provider_timeout"))
        await session._turn_engine.drain()

        assert session.terminal_future.done()
        assert session.terminal_future.result() is None
        assert session.close_reason == "stt_fatal"
        assert transport.clears == 1
        assert adapter.recognizer.closed is True

        adapter.recognizer.emit(runtime_module.RecognizerFatal("provider_timeout"))
        await session._turn_engine.drain()
        assert transport.clears == 1

    asyncio.run(exercise())


def test_provisional_sentence_starts_tts_before_terminal_commit(runtime_module):
    async def exercise():
        adapter, transport = RecordingAdapter(), RecordingTransport()
        commit = asyncio.Event()

        async def stream():
            yield runtime_module.ProvisionalSentence(1, "speak promptly")
            await commit.wait()
            yield runtime_module.TerminalCommit(1)

        adapter.response_factory = lambda _transcript: stream()
        engine, _language, recognizer = await _new_engine(runtime_module, adapter, transport)
        result = runtime_module.RecognizerResult

        recognizer.emit(result("question", is_final=True))
        await _await_event(transport.audio_sent)
        assert transport.events == [("audio", b"speak promptly")]
        assert engine.turns_completed == 0
        assert engine.committed_responses == []

        commit.set()
        await engine.drain()
        assert transport.events == [("audio", b"speak promptly"), ("mark", "conversation-turn")]
        assert engine.committed_responses == ["speak promptly"]

    asyncio.run(exercise())


def test_cancelled_generation_cannot_speak_after_a_late_terminal_commit(runtime_module):
    async def exercise():
        adapter, transport = RecordingAdapter(), RecordingTransport()
        old_commit = asyncio.Event()

        async def old_stream():
            yield runtime_module.ProvisionalSentence(1, "stale sentence")
            await old_commit.wait()
            yield runtime_module.TerminalCommit(1)

        adapter.response_factory = lambda transcript: old_stream() if transcript == "first" else transcript
        engine, _language, recognizer = await _new_engine(
            runtime_module,
            adapter,
            transport,
            pre_audio_min_events=1,
            pre_audio_min_seconds=0.0,
        )
        result = runtime_module.RecognizerResult

        recognizer.emit(result("first", is_final=True))
        await _await_event(transport.audio_sent)
        recognizer.emit(result("this is a material interruption", is_final=False))
        await _await_event(transport.audio_cleared)
        assert transport.clears == 1
        old_commit.set()
        await engine.drain()

        assert transport.pending_audio == []
        assert engine.committed_responses == ["this is a material interruption"]
        assert ("audio", b"this is a material interruption") in transport.events
        assert adapter.generated == ["first", "this is a material interruption"]

    asyncio.run(exercise())


def test_truncated_preamble_fences_media_then_allows_one_adapter_retry(runtime_module):
    async def exercise():
        adapter, transport = RecordingAdapter(), RecordingTransport()
        release_retry = asyncio.Event()
        retry_started = asyncio.Event()

        async def stream():
            yield runtime_module.ProvisionalSentence(1, "unsafe preamble")
            yield runtime_module.GenerationFence(
                1, runtime_module.RoundOutcome.MAX_TOKENS_TRUNCATED, retrying=True
            )
            retry_started.set()
            await release_retry.wait()
            yield runtime_module.ProvisionalSentence(2, "recovered answer")
            yield runtime_module.TerminalCommit(2)

        adapter.response_factory = lambda _transcript: stream()
        engine, _language, recognizer = await _new_engine(runtime_module, adapter, transport)
        result = runtime_module.RecognizerResult

        recognizer.emit(result("question", is_final=True))
        await asyncio.wait_for(retry_started.wait(), timeout=0.2)

        assert transport.clears == 1
        assert transport.pending_audio == []
        assert engine.committed_responses == []
        assert engine.turns_completed == 0
        assert adapter.generated == ["question"]

        release_retry.set()
        await engine.drain()

        assert transport.events == [
            ("audio", b"unsafe preamble"),
            ("audio", b"recovered answer"),
            ("mark", "conversation-turn"),
        ]
        assert transport.pending_audio == []
        assert transport.clears == 1
        assert adapter.generated == ["question"]
        assert engine.committed_responses == ["recovered answer"]
        assert engine.turns_completed == 1

    asyncio.run(exercise())


def test_aborted_preamble_clears_media_without_history_or_stale_task(runtime_module):
    async def exercise():
        adapter, transport = RecordingAdapter(), RecordingTransport()

        async def stream():
            yield runtime_module.ProvisionalSentence(1, "truncated preamble")
            raise RuntimeError("provider stream aborted")

        adapter.response_factory = lambda _transcript: stream()
        engine, _language, recognizer = await _new_engine(runtime_module, adapter, transport)
        result = runtime_module.RecognizerResult

        recognizer.emit(result("question", is_final=True))
        await engine.drain()

        assert transport.events == [("audio", b"truncated preamble")]
        assert transport.pending_audio == []
        assert transport.clears == 1
        assert engine.committed_responses == []
        assert engine.turns_completed == 0
        assert engine._turn_task is None

    asyncio.run(exercise())


def test_teardown_closes_recognizer_and_rejects_late_callback(runtime_module):
    async def exercise():
        adapter, transport = RecordingAdapter(), RecordingTransport()
        engine, _language, recognizer = await _new_engine(runtime_module, adapter, transport)
        result = runtime_module.RecognizerResult

        recognizer.emit(result("late", is_final=True, result_id=1))
        await engine.close()
        await engine.drain()

        assert recognizer.closed is True
        assert adapter.generated == []
        assert transport.clears == 1

    asyncio.run(exercise())


def test_turn_counts_only_after_transport_mark_barrier(runtime_module):
    async def exercise():
        adapter, transport = RecordingAdapter(), RecordingTransport()
        transport.hold_mark = True
        engine, _language, recognizer = await _new_engine(runtime_module, adapter, transport)
        result = runtime_module.RecognizerResult

        recognizer.emit(result("delivered", is_final=True, result_id=1))
        await _await_event(transport.mark_sent)
        assert transport.events == [("audio", b"delivered"), ("mark", "conversation-turn")]
        assert engine.turns_completed == 0

        transport.mark_release.set()
        await engine.drain()
        assert engine.turns_completed == 1

    asyncio.run(exercise())
