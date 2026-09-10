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
    for name in ("product_profile", "provider_adapters", "turn_engine"):
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
        self.clears = 0
        self.mark_release = asyncio.Event()
        self.hold_mark = False

    async def send_audio(self, audio: bytes) -> None:
        self.events.append(("audio", audio))

    async def send_mark(self, name: str) -> None:
        self.events.append(("mark", name))
        if self.hold_mark:
            await self.mark_release.wait()

    async def clear_audio(self) -> None:
        self.clears += 1


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
        self.echo_texts: set[str] = set()
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


    def is_echo(self, text: str, _language: str) -> bool:
        return text in self.echo_texts


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
        for _ in range(4):
            await asyncio.sleep(0)
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
        recognizer.emit(result("this is material but only one callback", is_final=False))
        for _ in range(4):
            await asyncio.sleep(0)
        assert transport.clears == 0

        now[0] = 0.25
        recognizer.emit(result("this is the second sustained callback", is_final=False))
        for _ in range(4):
            await asyncio.sleep(0)
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
        adapter.echo_texts.add("this is echoed assistant audio")
        transport.hold_mark = True
        engine, _language, recognizer = await _new_engine(
            runtime_module, adapter, transport, barge_in_debounce_seconds=0.0
        )
        result = runtime_module.RecognizerResult

        recognizer.emit(result("first reply", is_final=True, result_id="first"))
        for _ in range(4):
            await asyncio.sleep(0)
        recognizer.emit(result("this is echoed assistant audio", is_final=False))
        for _ in range(4):
            await asyncio.sleep(0)

        assert transport.clears == 0
        transport.mark_release.set()
        await engine.drain()
        assert adapter.generated == ["first reply"]

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


def test_provisional_llm_sentences_are_not_spoken_until_terminal_commit(runtime_module):
    async def exercise():
        adapter, transport = RecordingAdapter(), RecordingTransport()
        commit = asyncio.Event()

        async def stream():
            yield runtime_module.ProvisionalSentence("not committed")
            await commit.wait()
            yield runtime_module.TerminalCommit()

        adapter.response_factory = lambda _transcript: stream()
        engine, _language, recognizer = await _new_engine(runtime_module, adapter, transport)
        result = runtime_module.RecognizerResult

        recognizer.emit(result("question", is_final=True))
        for _ in range(4):
            await asyncio.sleep(0)
        assert transport.events == []

        commit.set()
        await engine.drain()
        assert transport.events == [("audio", b"not committed"), ("mark", "conversation-turn")]

    asyncio.run(exercise())


def test_cancelled_generation_cannot_speak_after_a_late_terminal_commit(runtime_module):
    async def exercise():
        adapter, transport = RecordingAdapter(), RecordingTransport()
        old_commit = asyncio.Event()

        async def old_stream():
            yield runtime_module.ProvisionalSentence("stale sentence")
            await old_commit.wait()
            yield runtime_module.TerminalCommit()

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
        for _ in range(4):
            await asyncio.sleep(0)
        recognizer.emit(result("this is a material interruption", is_final=False))
        for _ in range(4):
            await asyncio.sleep(0)
        assert transport.clears == 1
        old_commit.set()
        await engine.drain()

        assert ("audio", b"stale sentence") not in transport.events
        assert ("audio", b"this is a material interruption") in transport.events
        assert adapter.generated == ["first", "this is a material interruption"]

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
        for _ in range(4):
            await asyncio.sleep(0)
        assert transport.events == [("audio", b"delivered"), ("mark", "conversation-turn")]
        assert engine.turns_completed == 0

        transport.mark_release.set()
        await engine.drain()
        assert engine.turns_completed == 1

    asyncio.run(exercise())
