"""Direct SmartPBX Tamil IVR: digit ``3`` activates the Tamil profile.

Senior review gate for PR #329: start a Dialog SmartPBX session, press ``3``
on the static language menu, and prove the Tamil profile -- Google ``ta`` STT
(not the Sinhala-tuned Azure stream, regardless of ``STT_PROVIDER``), the
primary LLM provider/model, transfer tool withheld like Sinhala -- is what the
call actually runs on. Fixtures are shared with the Sinhala IVR suite so the
three menu digits are exercised through one and the same session seam.
"""
from __future__ import annotations

import asyncio

import pytest

import server
from smartpbx_session import RivieraSmartPBXSession
from tests.test_smartpbx_sinhala_ivr import (
    RecordingPipeline,
    RecordingStt,
    RecordingTransport,
    _context,
    make_session,
)


class RecordingSttFactory:
    """Records the kwargs the session hands to the STT factory."""

    def __init__(self, stt: RecordingStt) -> None:
        self.stt = stt
        self.calls: list[dict] = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return self.stt


def _tamil_session(post_call=None):
    pipeline = RecordingPipeline()
    stt = RecordingStt(lambda: {
        "lang": pipeline.lang,
        "llm_provider": pipeline.llm_provider,
        "model": pipeline.model,
        "tools": [dict(t) for t in pipeline.tools],
    })
    factory = RecordingSttFactory(stt)

    async def _noop_post_call(**_metadata):
        return None

    session = RivieraSmartPBXSession(
        _context(),
        RecordingTransport(),
        pipeline=pipeline,
        stt_factory=factory,
        post_call_processor=post_call or _noop_post_call,
        welcome_text="",
        llm_provider="claude",
        model="test-model",
    )
    return session, pipeline, stt, factory


@pytest.mark.asyncio
async def test_tamil_profile_resolves_to_google_ta_stt_and_the_primary_llm(monkeypatch):
    # Azure is the configured English/Sinhala STT here; Tamil must still force Google.
    # (The session binds to the running loop in __init__, hence the async test.)
    monkeypatch.setattr(server, "STT_PROVIDER", "azure")
    session, _pipeline, _stt, _factory = _tamil_session()

    profile = session._resolve_language_profile("ta")

    assert profile.lang == "ta"
    assert profile.stt_provider == "google"
    assert profile.stt_language == "ta"
    assert profile.stt_fail_closed is False
    assert profile.llm_provider == "claude"
    assert profile.model == "test-model"


@pytest.mark.asyncio
async def test_digit_three_activates_the_tamil_stt_and_llm_profile(monkeypatch):
    monkeypatch.setattr(server, "STT_PROVIDER", "azure")
    session, pipeline, stt, factory = _tamil_session()

    await session.start()
    assert await session.feed_dtmf("3") is True

    # Exactly one STT stream was built and started, on the Tamil/Google profile.
    assert len(factory.calls) == 1
    kwargs = factory.calls[0]
    assert kwargs["lang"] == "ta"
    assert kwargs["provider"] == "google"
    assert kwargs["fail_closed"] is False
    assert kwargs["direct_smartpbx_sinhala"] is False
    # The Azure/Sinhala-only confidence + metadata callbacks are not wired for Tamil.
    assert kwargs["on_final_result_with_confidence"] is None
    assert kwargs["on_final_result_with_metadata"] is None
    assert stt.starts == 1

    # The pipeline now runs Tamil on the primary (English) LLM, transfer tool withheld.
    assert pipeline.lang == "ta"
    assert pipeline.llm_provider == "claude"
    assert pipeline.model == "test-model"
    assert pipeline._smartpbx_azure_final_endpointing is False
    assert [t["name"] for t in pipeline.tools] == ["check_availability"]
    assert stt.profile_at_start == {
        "lang": "ta",
        "llm_provider": "claude",
        "model": "test-model",
        "tools": [{"name": "check_availability"}],
    }

    # Only the menu digit was consumed; later DTMF is the call's, not the menu's.
    assert await session.feed_dtmf("7") is False
    assert stt.starts == 1
    assert len(factory.calls) == 1


@pytest.mark.asyncio
async def test_selected_tamil_uses_tamil_welcome_and_post_call_language():
    post_calls: list[dict[str, object]] = []

    async def post_call(**metadata):
        post_calls.append(metadata)

    pipeline = RecordingPipeline()
    stt = RecordingStt()
    session = RivieraSmartPBXSession(
        _context(), RecordingTransport(), pipeline=pipeline,
        stt_factory=lambda **_kwargs: stt, post_call_processor=post_call,
        welcome_text=None, llm_provider="claude", model="test-model",
    )
    await session.start()
    await session.feed_dtmf("3")
    await asyncio.sleep(0)
    await session.finish(True)
    await asyncio.sleep(0)

    assert ("ta", server.LANGUAGE_CONFIGS["ta"]["welcome_greeting"]) in pipeline.spoken
    assert post_calls[0]["lang"] == "ta"
    assert post_calls[0]["llm_provider"] == "claude"
    assert post_calls[0]["model"] == "test-model"


@pytest.mark.asyncio
async def test_tamil_and_sinhala_menu_digits_never_cross_mutate(monkeypatch):
    """Digits 2 and 3 on concurrent calls each get their own profile."""
    monkeypatch.setattr(server, "SMARTPBX_SINHALA_LLM_PROVIDER", "gemini")
    monkeypatch.setattr(server, "SMARTPBX_SINHALA_GEMINI_LLM_MODEL", "gemini-3.7-flash")
    monkeypatch.setattr(server, "get_tools_gemini", lambda: [{"function_declarations": [
        {"name": "transfer_to_human"}, {"name": "check_availability"},
    ]}])
    monkeypatch.setattr(server, "_get_gemini_client", lambda: object())

    tamil, tamil_pipeline, tamil_stt, _factory = _tamil_session()
    sinhala, sinhala_pipeline, sinhala_stt = make_session()
    await asyncio.gather(tamil.start(), sinhala.start())
    await asyncio.gather(tamil.feed_dtmf("3"), sinhala.feed_dtmf("2"))

    assert (tamil_pipeline.lang, tamil_pipeline.llm_provider, tamil_pipeline.model) == (
        "ta", "claude", "test-model",
    )
    assert (sinhala_pipeline.lang, sinhala_pipeline.llm_provider, sinhala_pipeline.model) == (
        "si", "gemini", "gemini-3.7-flash",
    )
    assert tamil_stt.profile_at_start["lang"] == "ta"
    assert sinhala_stt.profile_at_start["lang"] == "si"
    assert tamil_pipeline._smartpbx_azure_final_endpointing is False
    assert sinhala_pipeline._smartpbx_azure_final_endpointing is True
