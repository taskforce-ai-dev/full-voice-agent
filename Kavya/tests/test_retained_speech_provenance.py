"""Retained speech is evidence, not testimony — and must be labelled as such.

`_retain_pending_speech` is the RETAINED ownership: buffered caller speech that
never became a turn is written into `full_transcript` so it reaches the call log
and post-call extraction instead of vanishing at a lifecycle boundary. That is
the right call. What was wrong is the SHAPE it was written in:

    self.full_transcript.append({"role": "user", "text": buffered})

which is byte-identical to the entry `_flush_transcript` writes for an utterance
the agent actually heard, answered, and read back. Downstream cannot tell them
apart, and downstream is an LLM extractor whose whole job is turning transcript
lines into booking fields. Two distinct defects follow:

  1. An INTERIM-only hypothesis — a partial recognition the provider never
     committed as final — was promoted into an ordinary confirmed guest
     statement. "zero seven seven one two" is a plausible phone number and a
     wrong one.
  2. Even a genuine final retained here was UNANSWERED: the guest said it, the
     agent never responded to it, and the guest never heard it read back. It is
     the weakest evidence in the transcript and it was presented as the
     strongest.

So a retained record now carries, explicitly:

  * `role`      — a distinct role, never the confirmed `"user"`;
  * `provenance`— `"final"` or `"interim"`, a closed enum;
  * `answered`  — `"unanswered"` (structurally always so on this path: retention
                  is by definition the not-dispatched path);
  * `text`      — hard-clamped, independent of the operator-tunable capture bound;
  * `retention_reason` — which boundary retained it.

…and every downstream renderer is taught the new role. That last part is the
load-bearing half: `post_call._format_transcript` maps unknown roles to
`"Kavya"`, so a new role added without touching it would attribute the guest's
unconfirmed words to the AGENT — strictly worse than the bug being fixed.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest

import booking_api
import post_call
import server


DEFERRED = "and could we have a late checkout as well"
CONFIRMED = "i would like a room for two nights"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class _Scheduled:
    def __init__(self, delay, callback):
        self.delay = delay
        self.callback = callback
        self.cancelled = False

    def cancel(self):
        self.cancelled = True


class FakeLoop:
    def __init__(self):
        self.scheduled: list[_Scheduled] = []

    def call_later(self, delay, callback):
        handle = _Scheduled(delay, callback)
        self.scheduled.append(handle)
        return handle

    @property
    def last(self) -> _Scheduled:
        return self.scheduled[-1]


class _FakeTransport:
    async def send_audio(self, _audio):
        return None

    async def clear_audio(self):
        return None

    async def send_mark(self, _name: str) -> None:
        return None

    async def close(self):
        return None


async def _noop(*_args, **_kwargs):
    return None


def make_session(*, smartpbx=True):
    session = server.MediaStreamSession(websocket=None, lang="en", media_transport=None)
    if smartpbx:
        session._smartpbx_transfer_context = object()
        session._media_transport = _FakeTransport()
    loop = FakeLoop()
    session._event_loop = loop
    processed: list[str] = []

    async def record(text):
        processed.append(text)

    session._process_utterance = record
    session._clear_media_audio = _noop
    return session, loop, processed


def _retained(transcript) -> list[dict]:
    return [
        entry
        for entry in transcript
        if entry.get("role") == server.RETAINED_SPEECH_ROLE
    ]


# ---------------------------------------------------------------------------
# 1. Provenance: an interim hypothesis is never promoted to a confirmed final
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_interim_only_speech_is_retained_as_interim_not_as_a_user_turn():
    """The reviewer's headline: no interim-only text may look like a confirmed turn."""
    session, _loop, _processed = make_session()
    await session._set_transcript_interim(DEFERRED)
    assert session._committed_transcript == "", (
        "precondition: the provider never committed a final for this"
    )

    await session._retain_pending_speech("session_end")

    assert [entry.get("role") for entry in session.full_transcript] == [
        server.RETAINED_SPEECH_ROLE
    ], "an interim hypothesis must not be written as an ordinary role=user turn"
    record = session.full_transcript[0]
    assert record["text"] == DEFERRED
    assert record["provenance"] == "interim"
    assert record["answered"] == "unanswered"
    assert record["retention_reason"] == "session_end"


@pytest.mark.asyncio
async def test_committed_final_speech_is_retained_with_final_provenance():
    """A provider-committed final is still unanswered, but it is not a guess."""
    session, _loop, _processed = make_session()
    await session._accumulate_transcript(DEFERRED)
    assert session._committed_transcript == DEFERRED

    await session._retain_pending_speech("hangup")

    record = _retained(session.full_transcript)[0]
    assert record["provenance"] == "final"
    assert record["answered"] == "unanswered"
    assert record["retention_reason"] == "hangup"


@pytest.mark.asyncio
async def test_an_interim_extending_committed_finals_is_retained_as_interim():
    """Mixed buffers resolve DOWN to interim: the tail was never confirmed."""
    session, _loop, _processed = make_session()
    await session._accumulate_transcript("my number is zero seven seven")
    await session._set_transcript_interim("one two three four five")
    assert session._pending_transcript != session._committed_transcript, (
        "precondition: an unconfirmed interim extends the committed finals"
    )

    await session._retain_pending_speech("barge_in")

    record = _retained(session.full_transcript)[0]
    assert record["provenance"] == "interim", (
        "part of this text was never committed, so the whole record is unconfirmed"
    )


@pytest.mark.asyncio
async def test_dispatched_speech_is_still_an_ordinary_confirmed_user_turn():
    """Regression guard: the ANSWERED path must keep its existing shape exactly."""
    session, loop, processed = make_session()
    await session._accumulate_transcript(CONFIRMED)
    loop.last.callback()
    await asyncio.sleep(0)

    assert processed == [CONFIRMED]
    assert session.full_transcript == [{"role": "user", "text": CONFIRMED}], (
        "speech that became a turn is confirmed AND answered — unchanged shape"
    )


# ---------------------------------------------------------------------------
# 2. Bound
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retained_text_is_hard_clamped():
    session, _loop, _processed = make_session()
    session._pending_transcript = "a" * (server.RETAINED_SPEECH_MAX_CHARS * 3)

    await session._retain_pending_speech("session_end")

    record = _retained(session.full_transcript)[0]
    assert len(record["text"]) <= server.RETAINED_SPEECH_MAX_CHARS, (
        "a retained record is a transcript entry shipped to an LLM and to n8n; "
        "it needs a fixed ceiling, not an operator-tunable one"
    )


def test_retained_bound_is_independent_of_the_operator_tunable_capture_bound():
    """CAPTURE_BUFFER_MAX_CHARS is env-tunable to 4000. This bound is not."""
    assert isinstance(server.RETAINED_SPEECH_MAX_CHARS, int)
    assert 0 < server.RETAINED_SPEECH_MAX_CHARS <= 1000


# ---------------------------------------------------------------------------
# 3. Telemetry: bounded fields, closed enums, never the text
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retention_telemetry_is_bounded_and_carries_no_transcript_text(caplog):
    session, _loop, _processed = make_session()
    secret = "SECRET-GUEST-WORDS " * 80
    session._pending_transcript = secret

    with caplog.at_level(logging.INFO):
        await session._retain_pending_speech("session_end")

    line = next(
        record.getMessage()
        for record in caplog.records
        if "event=capture_forced_dispatch" in record.getMessage()
    )
    assert "SECRET-GUEST-WORDS" not in line, "telemetry must never carry the text"
    assert "reason=session_end" in line
    assert "provenance=interim" in line
    assert "answered=unanswered" in line
    assert f"chars={server.RETAINED_SPEECH_MAX_CHARS}" in line, (
        "the reported length must describe the CLAMPED record, not the raw buffer"
    )


# ---------------------------------------------------------------------------
# 4. Downstream: post-call extraction must not read it as a guest statement
# ---------------------------------------------------------------------------


def test_unconfirmed_role_is_not_rendered_as_a_kavya_line():
    """The trap: `_format_transcript` maps every unknown role to "Kavya"."""
    rendered = post_call._format_transcript(
        [
            {"role": "user", "text": "confirmed guest line"},
            {
                "role": post_call.UNCONFIRMED_TRANSCRIPT_ROLE,
                "text": "half heard fragment",
                "provenance": "interim",
                "answered": "unanswered",
                "retention_reason": "hangup",
            },
        ]
    )
    assert "Kavya: half heard fragment" not in rendered, (
        "attributing the guest's unconfirmed words to the AGENT is worse than "
        "the bug being fixed"
    )
    assert "Guest: confirmed guest line" in rendered
    assert post_call.UNCONFIRMED_TRANSCRIPT_LABEL in rendered
    assert "half heard fragment" in rendered


def test_unconfirmed_line_states_its_provenance_and_answered_state():
    rendered = post_call._format_transcript(
        [
            {
                "role": post_call.UNCONFIRMED_TRANSCRIPT_ROLE,
                "text": "zero seven seven one two",
                "provenance": "interim",
                "answered": "unanswered",
                "retention_reason": "barge_in",
            }
        ]
    )
    assert "interim" in rendered
    assert "unanswered" in rendered


def test_format_transcript_tolerates_a_retained_entry_missing_its_annotations():
    """Fail-safe: an un-annotated unconfirmed entry stays unconfirmed."""
    rendered = post_call._format_transcript(
        [{"role": post_call.UNCONFIRMED_TRANSCRIPT_ROLE, "text": "bare fragment"}]
    )
    assert post_call.UNCONFIRMED_TRANSCRIPT_LABEL in rendered
    assert "Kavya:" not in rendered


def test_extraction_prompt_forbids_reading_unconfirmed_fragments_as_booking_data():
    prompt = post_call.build_extraction_system_prompt("2026-08-18")
    assert post_call.UNCONFIRMED_TRANSCRIPT_LABEL in prompt, (
        "the extractor must be told what the label means, or it will guess"
    )
    lowered = prompt.lower()
    assert "not confirmed" in lowered or "never confirmed" in lowered
    for field in ("guest_name", "check_in", "check_out", "room_preference"):
        assert field in prompt
    assert "unconfirmed" in lowered


def test_retry_extraction_prompt_carries_the_same_instruction():
    retry = post_call.build_retry_prompt("2026-08-18")
    assert post_call.UNCONFIRMED_TRANSCRIPT_LABEL in retry, (
        "the retry path extracts the same fields from the same transcript"
    )


def test_server_and_post_call_agree_on_the_unconfirmed_role():
    assert server.RETAINED_SPEECH_ROLE == post_call.UNCONFIRMED_TRANSCRIPT_ROLE, (
        "one vocabulary, one definition — a silent divergence here re-opens the "
        "exact hole this change closes"
    )


# ---------------------------------------------------------------------------
# 5. Downstream: the n8n / Google Sheet payload carries the marking
# ---------------------------------------------------------------------------


class _FakeResponse:
    status = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def text(self):
        return "ok"


class _FakeHTTPSession:
    def __init__(self):
        self.payloads: list[dict] = []

    def post(self, _url, *, json, **_kwargs):
        self.payloads.append(json)
        return _FakeResponse()


class _StubOpenAI:
    def __init__(self):
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    async def create(self, **_kwargs):
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content='{"call_outcome": "dropped", "follow_up_needed": "Yes"}'
                    )
                )
            ]
        )


@pytest.mark.asyncio
async def test_n8n_transcript_payload_marks_unconfirmed_fragments(monkeypatch):
    """n8n receives the FLATTENED transcript string — the marking must survive it."""
    http = _FakeHTTPSession()

    async def get_session():
        return http

    monkeypatch.setattr(booking_api, "get_session", get_session)
    monkeypatch.setattr(post_call, "dashboard_client", None)

    await post_call.process_post_call_data(
        call_sid="call-id",
        lang="en",
        caller_phone="+94770000000",
        full_transcript=[
            {"role": "user", "text": "confirmed guest line"},
            {
                "role": post_call.UNCONFIRMED_TRANSCRIPT_ROLE,
                "text": "zero seven seven one two",
                "provenance": "interim",
                "answered": "unanswered",
                "retention_reason": "hangup",
            },
        ],
        call_start_time="2026-08-18T00:00:00+00:00",
        call_end_time="2026-08-18T00:01:00+00:00",
        llm_provider="openai",
        openai_client=_StubOpenAI(),
    )

    assert http.payloads, "the n8n POST must still happen"
    transcript = http.payloads[0]["transcript"]
    assert "Guest: confirmed guest line" in transcript
    assert post_call.UNCONFIRMED_TRANSCRIPT_LABEL in transcript, (
        "the Google Sheet row must not read an unanswered fragment as a plain "
        "guest statement"
    )
    assert "Kavya: zero seven seven one two" not in transcript


# ---------------------------------------------------------------------------
# 6. Downstream: the handover / failsafe renderer
# ---------------------------------------------------------------------------


def test_handoff_transcript_does_not_attribute_unconfirmed_speech_to_kavya():
    rendered = server._format_handoff_transcript(
        [
            {"role": "user", "text": "confirmed guest line"},
            {
                "role": server.RETAINED_SPEECH_ROLE,
                "text": "half heard fragment",
                "provenance": "interim",
                "answered": "unanswered",
            },
        ]
    )
    assert "Kavya: half heard fragment" not in rendered, (
        "`who = 'Guest' if role == 'user' else 'Kavya'` is a binary test — a new "
        "role silently lands on the wrong side of it"
    )
    assert "Guest: confirmed guest line" in rendered
    assert "interim" in rendered
    assert "unanswered" in rendered


def test_handoff_retained_final_states_provenance_and_answered_state():
    rendered = server._format_handoff_transcript(
        [
            {
                "role": server.RETAINED_SPEECH_ROLE,
                "text": "unanswered final",
                "provenance": "final",
                "answered": "unanswered",
            }
        ]
    )
    assert "final" in rendered
    assert "unanswered" in rendered


def test_extraction_renderer_replaces_unconfirmed_text_with_a_provenance_marker():
    sentinel = "SENTINEL NAME ON 2047-09-17"
    rendered = post_call._format_extraction_transcript(
        [
            {"role": "user", "text": "confirmed guest line"},
            {
                "role": post_call.UNCONFIRMED_TRANSCRIPT_ROLE,
                "text": sentinel,
                "provenance": "interim",
                "answered": "unanswered",
                "retention_reason": "hangup",
            },
        ]
    )
    assert "confirmed guest line" in rendered
    assert sentinel not in rendered
    assert "unconfirmed" in rendered.lower()
    assert "interim" in rendered
    assert "unanswered" in rendered


class _RetryRecordingOpenAI:
    def __init__(self):
        self.calls: list[dict] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        content = (
            "not json"
            if len(self.calls) == 1
            else '{"guest_name": null, "check_in": null, "check_out": null, '
            '"room_preference": null, "call_outcome": "dropped", '
            '"follow_up_needed": "Yes"}'
        )
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
        )


@pytest.mark.asyncio
async def test_extraction_and_retry_never_receive_raw_unconfirmed_retained_text(monkeypatch):
    sentinel = "SENTINEL NAME ON 2047-09-17"
    http = _FakeHTTPSession()

    async def get_session():
        return http

    client = _RetryRecordingOpenAI()
    monkeypatch.setattr(booking_api, "get_session", get_session)
    monkeypatch.setattr(post_call, "dashboard_client", None)
    await post_call.process_post_call_data(
        call_sid="call-id", lang="en", caller_phone="+94770000000",
        full_transcript=[
            {
                "role": post_call.UNCONFIRMED_TRANSCRIPT_ROLE,
                "text": sentinel,
                "provenance": "interim",
                "answered": "unanswered",
                "retention_reason": "hangup",
            }
        ],
        call_start_time="2026-08-18T00:00:00+00:00",
        call_end_time="2026-08-18T00:01:00+00:00",
        llm_provider="openai", openai_client=client,
    )

    assert len(client.calls) == 2, "invalid first response must exercise retry"
    assert all(sentinel not in repr(call) for call in client.calls)
    assert http.payloads[0]["guest_name"] is None
    assert http.payloads[0]["check_in"] is None
    assert http.payloads[0]["transcript"].endswith(sentinel), (
        "the explicitly approved n8n transcript representation keeps its raw, "
        "bounded, labelled retained evidence"
    )
