"""Azure-owned final identity and audio-coverage reconciliation contracts."""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest

import server


class _Scheduled:
    def __init__(self, delay, callback):
        self.delay = delay
        self.callback = callback
        self.cancelled = False

    def cancel(self):
        self.cancelled = True


class _Loop:
    def __init__(self):
        self.scheduled = []

    def call_later(self, delay, callback):
        handle = _Scheduled(delay, callback)
        self.scheduled.append(handle)
        return handle


def _session(*, lang="si", direct=True):
    pipeline = server.MediaStreamSession(
        websocket=None,
        lang=lang,
        media_transport=object() if direct else None,
        llm_provider="gemini" if lang == "si" else "claude",
    )
    if direct:
        pipeline._smartpbx_transfer_context = object()
    pipeline._smartpbx_azure_final_endpointing = lang == "si" and direct
    pipeline._event_loop = _Loop()
    return pipeline


def _metadata(result_id, offset, duration, confidence=0.9):
    return server.AzureFinalMetadata(
        result_id=result_id,
        offset=offset,
        duration=duration,
        confidence=confidence,
    )


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (
            SimpleNamespace(
                result_id="result-1",
                offset=120,
                duration=80,
                json='{"NBest":[{"Confidence":0.42}]}',
            ),
            _metadata("result-1", 120, 80, 0.42),
        ),
        (
            SimpleNamespace(
                result_id="x" * 257,
                offset=-1,
                duration=1.5,
                json="not-json",
            ),
            _metadata(None, None, None, None),
        ),
    ],
)
def test_azure_final_metadata_is_bounded_and_validated(result, expected):
    assert server._azure_final_metadata(result) == expected


def test_direct_azure_callback_forwards_metadata_without_using_legacy_seams(
    monkeypatch,
):
    calls = []
    stream = server.AzureSTTStream(
        on_final_result=lambda _text: calls.append("legacy"),
        on_final_result_with_confidence=lambda *_args: calls.append("confidence"),
        on_final_result_with_metadata=lambda text, metadata: calls.append(
            (text, metadata)
        ),
        lang="si",
        privacy_safe=True,
        direct_smartpbx_sinhala=True,
    )
    stream._running = True
    monkeypatch.setattr(
        server,
        "azure_speech",
        SimpleNamespace(ResultReason=SimpleNamespace(RecognizedSpeech="recognized")),
    )
    event = SimpleNamespace(
        result=SimpleNamespace(
            reason="recognized",
            text="අමුත්තාගේ නම",
            result_id="result-1",
            offset=120,
            duration=80,
            json='{"NBest":[{"Confidence":0.42}]}',
        )
    )

    stream._on_recognized(event)

    assert calls == [("අමුත්තාගේ නම", _metadata("result-1", 120, 80, 0.42))]


@pytest.mark.asyncio
async def test_azure_sdk_thread_does_not_mutate_identity_state_before_loop_admission():
    pipeline = _session()
    submitted = []
    metadata = _metadata("result-1", 0, 100)

    def capture_submission(callback, *args):
        submitted.append((callback, args))
        return True

    pipeline._submit_stt_callback = capture_submission

    pipeline._on_stt_result_with_metadata("caller speech", metadata)

    assert list(pipeline._azure_final_result_ids) == []
    assert pipeline._azure_final_segments == []
    callback, args = submitted.pop()
    await callback(*args)
    assert list(pipeline._azure_final_result_ids) == ["result-1"]
    assert [(item.start, item.end) for item in pipeline._azure_final_segments] == [
        (0, 100)
    ]


@pytest.mark.asyncio
async def test_same_id_late_final_cannot_create_a_deferred_next_turn(caplog):
    pipeline = _session()
    release = asyncio.Event()

    async def block_processing(_text):
        await release.wait()

    pipeline._process_utterance = block_processing
    metadata = _metadata("result-1", 0, 100)
    await pipeline._accumulate_transcript("first final", metadata=metadata)
    turn = asyncio.create_task(pipeline._flush_transcript())
    await asyncio.sleep(0)
    assert pipeline._utterance_dispatched is True

    with caplog.at_level(logging.INFO):
        await pipeline._accumulate_transcript(
            "different transcript for the same audio", metadata=metadata
        )

    assert pipeline._pending_transcript == ""
    assert pipeline._deferred_flush_pending is False
    assert "basis=result_id" in caplog.text
    assert "result-1" not in caplog.text
    assert "different transcript" not in caplog.text
    release.set()
    await turn


@pytest.mark.asyncio
async def test_same_id_final_is_fenced_before_speaking_barge_in(caplog):
    pipeline = _session()
    release = asyncio.Event()
    metadata = _metadata("result-1", 0, 100)
    submitted = []
    barge_ins = 0

    async def block_processing(_text):
        await release.wait()

    async def record_barge_in():
        nonlocal barge_ins
        barge_ins += 1

    def capture_submission(callback, *args):
        submitted.append((callback, args))
        return True

    pipeline._process_utterance = block_processing
    await pipeline._accumulate_transcript("first final", metadata=metadata)
    turn = asyncio.create_task(pipeline._flush_transcript())
    await asyncio.sleep(0)
    assert pipeline._utterance_dispatched is True

    pipeline._is_speaking = True
    pipeline._speaking_since = 0.0
    pipeline._handle_bargein = record_barge_in
    pipeline._submit_stt_callback = capture_submission
    generation = pipeline._speak_generation

    with caplog.at_level(logging.INFO):
        pipeline._on_stt_result_with_metadata(
            "late duplicate final with enough words to interrupt", metadata,
        )
        callback, args = submitted.pop()
        await callback(*args)

    assert barge_ins == 0
    assert pipeline._speak_generation == generation
    assert pipeline._pending_transcript == ""
    assert pipeline._endpointing_handle is None
    assert pipeline._deferred_flush_pending is False
    assert pipeline._utterance_dispatched is True
    assert "basis=result_id" in caplog.text
    assert "result-1" not in caplog.text
    assert "late duplicate final" not in caplog.text
    release.set()
    await turn


@pytest.mark.asyncio
async def test_different_id_fully_covered_late_final_is_also_suppressed():
    pipeline = _session()
    release = asyncio.Event()

    async def block_processing(_text):
        await release.wait()

    pipeline._process_utterance = block_processing
    await pipeline._accumulate_transcript(
        "first final", metadata=_metadata("result-1", 0, 100)
    )
    turn = asyncio.create_task(pipeline._flush_transcript())
    await asyncio.sleep(0)

    await pipeline._accumulate_transcript(
        "same audio new identity", metadata=_metadata("result-2", 0, 100)
    )

    assert pipeline._pending_transcript == ""
    assert pipeline._deferred_flush_pending is False
    release.set()
    await turn


@pytest.mark.asyncio
async def test_cumulative_extension_replaces_pending_text_despite_punctuation_change():
    pipeline = _session()

    await pipeline._accumulate_transcript(
        "පරණ වාක්‍යය", metadata=_metadata("result-1", 20, 80, 0.8)
    )
    await pipeline._accumulate_transcript(
        "අලුත්, සම්පූර්ණ වාක්‍යය.",
        metadata=_metadata("result-2", 20, 140, 0.7),
    )

    assert pipeline._committed_transcript == "අලුත්, සම්පූර්ණ වාක්‍යය."
    assert pipeline._committed_transcript_confidence == 0.7


@pytest.mark.asyncio
async def test_cumulative_extension_replaces_superseded_final_confidence():
    pipeline = _session()

    await pipeline._accumulate_transcript(
        "partial final", metadata=_metadata("result-1", 20, 80, 0.2)
    )
    await pipeline._accumulate_transcript(
        "replacement final", metadata=_metadata("result-2", 20, 140, 0.9)
    )

    assert pipeline._committed_transcript == "replacement final"
    assert pipeline._committed_transcript_confidence == 0.9


@pytest.mark.asyncio
async def test_non_overlapping_and_partial_overlap_finals_are_conservatively_appended():
    pipeline = _session()

    await pipeline._accumulate_transcript(
        "first", metadata=_metadata("result-1", 0, 100)
    )
    await pipeline._accumulate_transcript(
        "partial", metadata=_metadata("result-2", 50, 100)
    )
    await pipeline._accumulate_transcript(
        "sequential", metadata=_metadata("result-3", 150, 50)
    )

    assert pipeline._committed_transcript == "first partial sequential"


@pytest.mark.asyncio
async def test_pre_audio_final_keeps_metadata_until_loop_side_accumulation():
    pipeline = _session()
    pipeline._tts_synthesis_in_flight = True
    pipeline._tts_synthesis_generation = pipeline._speak_generation
    metadata = _metadata("result-1", 0, 100)

    await pipeline._handle_pre_audio_stt("final", "caller tail", metadata)

    assert list(pipeline._azure_final_result_ids) == []
    pipeline._tts_synthesis_in_flight = False
    await pipeline._flush_pre_audio_stt()
    assert list(pipeline._azure_final_result_ids) == ["result-1"]
    assert pipeline._committed_transcript == "caller tail"


@pytest.mark.asyncio
@pytest.mark.parametrize(("lang", "direct"), [("en", True), ("si", False)])
async def test_metadata_reconciliation_never_changes_english_or_twilio(lang, direct):
    pipeline = _session(lang=lang, direct=direct)

    await pipeline._accumulate_transcript("first")
    await pipeline._accumulate_transcript("first second")

    assert pipeline._committed_transcript == "first first second"
    assert list(pipeline._azure_final_result_ids) == []
