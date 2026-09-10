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

    async def start_recognizer(self, language: str, on_result) -> RecordingRecognizer:
        self.started_languages.append(language)
        self.recognizer = RecordingRecognizer(on_result)
        return self.recognizer

    async def generate_response(self, transcript: str, _language: str, _prompt: str):
        self.generated.append(transcript)
        if self.block_first_response and transcript == "first":
            self.first_response_started.set()
            await asyncio.Event().wait()
        return transcript

    async def synthesize_audio(self, response: str, _language: str) -> bytes:
        return response.encode("ascii")


async def _new_engine(runtime_module, adapter: RecordingAdapter, transport: RecordingTransport):
    profile_module = importlib.import_module("product_profile")
    language = profile_module.test_product_profile().language("en")
    engine = runtime_module.ConversationTurnEngine(
        adapter,
        transport,
        profile_module.test_product_profile(),
        endpointing_silence_seconds=0.0,
        final_grace_seconds=0.0,
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


def test_stale_or_duplicate_final_never_dispatches_a_second_turn(runtime_module):
    async def exercise():
        adapter, transport = RecordingAdapter(), RecordingTransport()
        engine, _language, recognizer = await _new_engine(runtime_module, adapter, transport)
        result = runtime_module.RecognizerResult

        recognizer.emit(result("new final", is_final=True, result_id=4))
        recognizer.emit(result("stale final", is_final=True, result_id=3))
        recognizer.emit(result("duplicate final", is_final=True, result_id=4))
        await engine.drain()

        assert adapter.generated == ["new final"]
        assert engine.turns_completed == 1

    asyncio.run(exercise())


def test_barge_in_cancels_old_generation_and_fences_late_tts(runtime_module):
    async def exercise():
        adapter, transport = RecordingAdapter(), RecordingTransport()
        adapter.block_first_response = True
        engine, _language, recognizer = await _new_engine(runtime_module, adapter, transport)
        result = runtime_module.RecognizerResult

        recognizer.emit(result("first", is_final=True, result_id=1))
        await asyncio.wait_for(adapter.first_response_started.wait(), timeout=0.2)
        recognizer.emit(result("interrupt", is_final=False, result_id=2))
        recognizer.emit(result("second", is_final=True, result_id=3))
        await engine.drain()

        assert transport.clears == 1
        assert adapter.generated == ["first", "second"]
        assert ("audio", b"second") in transport.events
        assert ("audio", b"late") not in transport.events
        assert engine.turns_completed == 1

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
