"""Direct Sinhala/Azure endpointing waits for provider-final ownership."""

from __future__ import annotations

import asyncio

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
        self.scheduled: list[_Scheduled] = []

    def call_later(self, delay, callback):
        handle = _Scheduled(delay, callback)
        self.scheduled.append(handle)
        return handle


def _session(*, lang="si", direct=True, final_only=True):
    session = server.MediaStreamSession(
        websocket=None,
        lang=lang,
        media_transport=None,
        llm_provider="gemini" if lang == "si" else "claude",
    )
    if direct:
        session._smartpbx_transfer_context = object()
    session._smartpbx_azure_final_endpointing = final_only
    loop = _Loop()
    session._event_loop = loop
    processed: list[str] = []

    async def record(text):
        processed.append(text)

    session._process_utterance = record
    return session, loop, processed


@pytest.mark.asyncio
async def test_direct_sinhala_azure_interim_never_dispatches_early():
    session, loop, processed = _session()

    await session._set_transcript_interim("කාමර දෙකක් වෙන් කරගන්න ඕනි")

    assert session._pending_transcript == "කාමර දෙකක් වෙන් කරගන්න ඕනි"
    assert session._endpointing_handle is None
    assert loop.scheduled == []
    assert processed == []


@pytest.mark.asyncio
async def test_matching_azure_final_replaces_interim_and_dispatches_exactly_once():
    session, loop, processed = _session()

    await session._set_transcript_interim("කාමර දෙකක් වෙන් කරගන්න ඕනි")
    await session._accumulate_transcript(
        "කාමර දෙකක් වෙන් කරගන්න ඕනි සැප්තැම්බර් දහය සිට",
        confidence=0.91,
    )

    assert len(loop.scheduled) == 1
    assert loop.scheduled[0].delay == server.STT_FINAL_GRACE_SECONDS
    loop.scheduled[0].callback()
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert processed == [
        "කාමර දෙකක් වෙන් කරගන්න ඕනි සැප්තැම්බර් දහය සිට"
    ]


@pytest.mark.asyncio
async def test_new_interim_cancels_prior_final_grace_until_next_azure_final():
    session, loop, processed = _session()

    await session._accumulate_transcript("සැප්තැම්බර් දහය සිට")
    first_final_timer = loop.scheduled[-1]

    await session._set_transcript_interim("සැප්තැම්බර් දහතුන දක්වා")

    assert first_final_timer.cancelled is True
    assert session._endpointing_handle is None
    await session._accumulate_transcript(
        "සැප්තැම්බර් දහය සිට සැප්තැම්බර් දහතුන දක්වා"
    )
    loop.scheduled[-1].callback()
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert processed == ["සැප්තැම්බර් දහය සිට සැප්තැම්බර් දහතුන දක්වා"]


@pytest.mark.asyncio
async def test_segmented_azure_finals_still_combine_without_data_loss():
    session, loop, processed = _session()

    await session._accumulate_transcript("සැප්තැම්බර් දහය සිට")
    await session._accumulate_transcript("සැප්තැම්බර් දහතුන දක්වා")
    loop.scheduled[-1].callback()
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert processed == ["සැප්තැම්බර් දහය සිට සැප්තැම්බර් දහතුන දක්වා"]


@pytest.mark.asyncio
async def test_low_confidence_phone_turn_carries_confirmation_instruction():
    session, loop, processed = _session()
    session._enter_capture_mode(kind="phone")

    await session._accumulate_transcript("0771234567", confidence=0.42)
    loop.scheduled[-1].callback()
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert processed == ["0771234567"]
    assert session._last_guest_utterance_capture_kind == "phone"
    assert session._last_guest_utterance_confirmation_required is True
    note = session._stt_confirmation_note()
    assert "phone" in note
    assert "yes/no" in note
    assert "create_booking" in note


@pytest.mark.asyncio
async def test_high_confidence_phone_turn_needs_no_extra_confirmation_note():
    session, loop, _processed = _session()
    session._enter_capture_mode(kind="phone")

    await session._accumulate_transcript("0771234567", confidence=0.95)
    loop.scheduled[-1].callback()
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert session._last_guest_utterance_confirmation_required is False
    assert session._stt_confirmation_note() == ""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("lang", "direct"),
    [("en", True), ("si", False)],
)
async def test_final_only_endpointing_never_changes_english_or_twilio(lang, direct):
    session, loop, _processed = _session(lang=lang, direct=direct, final_only=True)

    await session._set_transcript_interim("caller speech")

    assert len(loop.scheduled) == 1
    assert loop.scheduled[0].delay == server.ENDPOINTING_SILENCE
