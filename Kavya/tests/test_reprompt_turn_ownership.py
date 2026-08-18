"""The silence re-prompt must be owned by the turn that owns dispatch.

`_send_tts_done` arms the no-speech nudge on every DELIVERED sentence, including
sentences delivered mid-turn — before a tool round, or before the next model
round of the same reply. In that gap `_is_speaking` is False while
`_utterance_dispatched` is still True, and `_reprompt_after_silence` checked
`transfer_pending`, `MAX_REPROMPTS`, `_is_speaking` and `_capture_dispatch_pending`
but never dispatch ownership. After `SILENCE_REPROMPT_DELAY` it therefore spoke
"Hello, are you still there?" straight over a live tool/LLM turn.

Before the STT endpoint-ownership gate landed, a late final or interim arriving
in that gap happened to cancel the timer — an accident, not a fix. The gate now
refuses those results before the cancel sites, so the hazard is exposed. It also
exists with no STT result at all: a tool round longer than the delay fires the
nudge on its own.

The fix is ownership, not cancellation: a refused result must still never touch
a timer (that is the whole point of the gate), so the deadline defers while a
turn holds the guard, exactly the way it already defers while the agent speaks.

Scope note (second #269 review round): the gate refuses only results with no
material characters — provider ownership is not provable without provider result
identity, which never reaches this code — so every injection below is such a
result. Caller speech in the same gap, INCLUDING a verbatim repeat of the
dispatched utterance, IS admitted and DOES cancel and reset the nudge; that is
`test_stt_post_dispatch_admission.py` and
`test_stt_post_dispatch_material_admission.py`. Not a contradiction: admitted
speech is a turn, refused speech is not.

Determinism: `SILENCE_REPROMPT_DELAY` is monkeypatched to 0.0 so the REAL
re-prompt task runs without a wall-clock wait; `Event` barriers hold the turn
open; `FakeLoop` captures endpointing timers instead of awaiting them.

Tests marked "preservation guard" hold in BOTH the pre-fix and post-fix trees on
purpose — they pin normal post-response nudging, the rejection gate's
timer-freedom, and the barge-in cancel that must all survive the fix.
"""

from __future__ import annotations

import asyncio
import time

import pytest

import server


# ---------------------------------------------------------------------------
# Fixtures — same shape as test_stt_late_result_isolation.py, plus a transport
# that lets the REAL _send_tts_done run and an _invoke_speak spy.
# ---------------------------------------------------------------------------


class _Scheduled:
    def __init__(self, delay, callback):
        self.delay = delay
        self.callback = callback
        self.cancelled = False

    def cancel(self):
        self.cancelled = True


class FakeLoop:
    """Captures call_later so timer arming is asserted, never awaited."""

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
    """Minimal media transport: enough for the real `_send_tts_done` to run."""

    def __init__(self):
        self.marks: list[str] = []

    async def send_mark(self, name: str) -> None:
        self.marks.append(name)


async def _noop(*_args, **_kwargs):
    return None


POST_DISPATCH_EVENT = "smartpbx_media event=stt_post_dispatch_result"
REPROMPT_EVENT = "smartpbx_media event=silence_reprompt"
NUDGE = server.REPROMPT_MESSAGES["en"][0]


def make_session(*, hold=None, telemetry=None, lang="en"):
    """A direct-SmartPBX-English session whose LLM turn is recorded, not run.

    `hold` (an `asyncio.Event`) blocks inside `_process_utterance`, holding the
    dispatched turn open so the re-prompt deadline expires in the exact tool/LLM
    gap the production defect occurs in. `speak` collects every nudge the
    re-prompt would have voiced.
    """
    session = server.MediaStreamSession(websocket=None, lang=lang, media_transport=None)
    session._smartpbx_transfer_context = object()
    session._media_transport = _FakeTransport()
    if telemetry is not None:
        session._turn_telemetry = telemetry
    loop = FakeLoop()
    session._event_loop = loop
    spoken: list[str] = []

    async def record_speech(text, generation=-1, sentence=None):
        spoken.append(text)

    session._invoke_speak = record_speech
    session._clear_media_audio = _noop

    async def run_turn(text):
        if hold is not None:
            await hold.wait()

    session._process_utterance = run_turn
    return session, loop, spoken


async def _dispatch_turn(session, loop, text="original utterance", *, in_flight=True):
    """Dispatch one turn.

    With a held runner the turn stays in flight; an unheld runner (`in_flight`
    False) completes inside the same loop step, which is the ordinary
    no-tool-gap shape.
    """
    await session._accumulate_transcript(text)
    loop.last.callback()
    await asyncio.sleep(0)
    assert session.full_transcript[-1] == {"role": "user", "text": text}
    if in_flight:
        assert session._utterance_dispatched is True, "turn must be in flight"
    else:
        assert session._utterance_dispatched is False, "turn must have completed"


async def _deliver_sentence(session, sentence="First sentence."):
    """Run the REAL delivered-sentence path, which arms the REAL re-prompt."""
    session._is_speaking = False
    delivered = await session._send_tts_done(sentence=sentence)
    assert delivered is True, "the harness must exercise the delivered branch"
    assert session._reprompt_task is not None, "a delivered sentence must arm the nudge"
    return session._reprompt_task


async def _drain(times: int = 12):
    """Let the 0-delay re-prompt task (and any re-armed successor) run."""
    for _ in range(times):
        await asyncio.sleep(0)


async def _drain_until_released(session, times: int = 40):
    for _ in range(times):
        await asyncio.sleep(0)
        if session._utterance_dispatched is False:
            return
    raise AssertionError("the held turn never released the dispatch guard")


def _reprompt_records(caplog):
    return [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith(REPROMPT_EVENT)
    ]


def _post_dispatch_records(caplog):
    return [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith(POST_DISPATCH_EVENT)
    ]


def _summaries(emitted):
    return [record for record in emitted if record.get("event") == "turn_summary"]


def _recording_telemetry(emitted, turn_id="opaque-turn-id"):
    return server.SmartPBXTurnTelemetry(
        emit=lambda _event, **fields: emitted.append(fields),
        new_id=lambda: turn_id,
    )


@pytest.fixture(autouse=True)
def _instant_reprompt_deadline(monkeypatch):
    """Drive the REAL re-prompt task; the delay is read at await time."""
    monkeypatch.setattr(server, "SILENCE_REPROMPT_DELAY", 0.0)


POST_DISPATCH_SUMMARY_FIELDS = (
    "ignored_post_dispatch_finals",
    "ignored_post_dispatch_interims",
    "ignored_post_dispatch_max_elapsed_ms",
)


# ---------------------------------------------------------------------------
# 1-3. RED — the nudge must never speak while a turn owns dispatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reprompt_deadline_in_tool_gap_never_speaks_over_the_active_turn(caplog):
    """The reviewer's production shape, end to end.

    A sentence is delivered mid-turn (arming the nudge), the turn then spends
    longer than the silence delay in a tool/model round, and late provider
    results land in that gap. The nudge must not be spoken, must not be
    cancelled by the rejected results, and must still fire once the turn ends.
    """
    hold = asyncio.Event()
    session, loop, spoken = make_session(hold=hold)
    await _dispatch_turn(session, loop)

    # (a) a delivered sentence mid-turn arms the REAL re-prompt
    armed = await _deliver_sentence(session)

    # (b) refused results leave the armed timer alone (admitted speech would
    #     instead cancel the nudge — see test_stt_post_dispatch_admission.py).
    #     Refusal now covers only results with no material characters.
    with caplog.at_level("INFO"):
        await session._accumulate_transcript("   ")
        await session._set_transcript_interim("...")
    assert session._committed_transcript == ""
    assert session._pending_transcript == ""
    assert len(_post_dispatch_records(caplog)) >= 1, "the gate must have refused both"
    assert session._reprompt_task is armed, "a rejected result must not re-arm the nudge"
    assert not armed.cancelled(), "a rejected result must not cancel the nudge"

    # (c) the deadline expires INSIDE the turn — nothing may be spoken
    caplog.clear()
    with caplog.at_level("INFO"):
        await _drain()
    assert session._utterance_dispatched is True, "the turn is still in flight"
    assert spoken == [], "the nudge talked over a live tool/LLM turn"
    assert session._reprompt_count == 0
    assert _reprompt_records(caplog) == []

    # (d) the turn finishes and releases the guard
    hold.set()
    await _drain_until_released(session)

    # (e) normal post-response nudging still happens, exactly once
    with caplog.at_level("INFO"):
        await _drain()
    assert spoken == [NUDGE]
    assert session._reprompt_count == 1
    assert len(_reprompt_records(caplog)) == 1

    # (f) the lifecycle sink still owns teardown; no task is left behind
    session._cancel_reprompt()
    assert session._reprompt_task is None
    await asyncio.sleep(0)
    leftover = asyncio.all_tasks() - {asyncio.current_task()}
    assert leftover == set(), leftover


@pytest.mark.asyncio
async def test_deferred_reprompt_does_not_leak_into_a_newer_turn():
    """A deadline deferred inside turn 1 must never fire during turn 2."""
    hold_one = asyncio.Event()
    session, loop, spoken = make_session(hold=hold_one)
    await _dispatch_turn(session, loop, "first utterance")
    await _deliver_sentence(session)

    await _drain()
    assert spoken == [], "the nudge talked over turn 1"

    # Turn 1 finishes.
    hold_one.set()
    await _drain_until_released(session)

    # The caller speaks: this final passes the gate (the guard is free) and hits
    # the cancel + counter reset, then dispatches turn 2.
    hold_two = asyncio.Event()

    async def hold_second(_text):
        await hold_two.wait()

    session._process_utterance = hold_second
    await session._accumulate_transcript("second utterance")
    assert session._reprompt_task is None, "caller speech must cancel the pending nudge"
    assert session._reprompt_count == 0
    loop.last.callback()
    await asyncio.sleep(0)
    assert session._utterance_dispatched is True, "turn 2 must be in flight"

    await _drain()
    assert spoken == [], "a deferred deadline leaked into the newer turn"
    assert session._reprompt_count == 0

    hold_two.set()
    await _drain_until_released(session)


@pytest.mark.asyncio
async def test_reprompt_defers_through_a_silent_tool_gap_without_any_late_stt_result():
    """The hazard predates the endpoint-ownership gate: no STT result required.

    A tool round longer than the silence delay is enough on its own.
    """
    hold = asyncio.Event()
    session, loop, spoken = make_session(hold=hold)
    await _dispatch_turn(session, loop)
    await _deliver_sentence(session)

    await _drain()
    assert session._utterance_dispatched is True
    assert spoken == [], "a silent tool gap alone fired the nudge over the turn"
    assert session._reprompt_count == 0

    hold.set()
    await _drain_until_released(session)
    await _drain()
    assert spoken == [NUDGE]
    assert session._reprompt_count == 1

    session._cancel_reprompt()


# ---------------------------------------------------------------------------
# 4-6. Preservation guards — green in BOTH trees
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reprompt_still_fires_after_a_normal_turn_with_no_tool_gap(caplog):
    """Preservation guard: ordinary post-response nudging is untouched."""
    session, loop, spoken = make_session()
    await _dispatch_turn(session, loop, in_flight=False)

    await _deliver_sentence(session, "Here is your answer.")
    with caplog.at_level("INFO"):
        await _drain()

    assert spoken == [NUDGE]
    assert session._reprompt_count == 1
    assert len(_reprompt_records(caplog)) == 1
    session._cancel_reprompt()


@pytest.mark.asyncio
async def test_rejected_results_still_do_not_cancel_or_reset_the_reprompt():
    """Preservation guard: the endpoint-ownership gate stays timer-free.

    A refused result is deliberately not admitted as a turn, so it must not
    cancel the nudge, re-arm it, or reset the attempt counter. Retargeted from
    repeats of the dispatched text (admitted now, and they DO cancel the nudge)
    to what is still refused: results with no material characters.
    """
    hold = asyncio.Event()
    session, loop, _spoken = make_session(hold=hold)
    await _dispatch_turn(session, loop)
    armed = await _deliver_sentence(session)
    session._reprompt_count = 1

    await session._accumulate_transcript("   ")
    await session._set_transcript_interim("  .  ")

    assert session._reprompt_task is armed
    assert not armed.cancelled()
    assert session._reprompt_count == 1

    hold.set()
    await _drain_until_released(session)
    session._cancel_reprompt()


@pytest.mark.asyncio
async def test_barge_in_still_cancels_the_reprompt_before_the_gate_matters():
    """Preservation guard: echo/barge-in ordering and its cancel sink."""
    hold = asyncio.Event()
    session, loop, spoken = make_session(hold=hold)
    await _dispatch_turn(session, loop)
    armed = await _deliver_sentence(session)

    session._is_speaking = True
    await session._handle_bargein()
    # Let the cancellation the barge-in requested actually land on the task.
    await asyncio.sleep(0)

    assert armed.cancelled()
    assert session._reprompt_task is None
    assert session._utterance_dispatched is False

    await _drain()
    assert spoken == [], "a barge-in must silence the nudge, not voice it"

    hold.set()
    await asyncio.sleep(0)


# ---------------------------------------------------------------------------
# 7-11. RED — bounded post-dispatch telemetry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_dispatch_info_line_capped_at_one_per_result_type_per_turn(caplog):
    """One INFO line per result_type per owning turn — at most two per turn."""
    hold = asyncio.Event()
    session, loop, _spoken = make_session(hold=hold)
    await _dispatch_turn(session, loop)

    # Five refusals, every one non-material: empty, whitespace, punctuation.
    with caplog.at_level("INFO"):
        await session._accumulate_transcript("")
        await session._accumulate_transcript("   ")
        await session._accumulate_transcript(" . ")
        await session._set_transcript_interim("...")
        await session._set_transcript_interim("  ")

    records = _post_dispatch_records(caplog)
    assert len(records) == 2, records
    result_types = sorted(
        line.split("result_type=", 1)[1].split(" ", 1)[0] for line in records
    )
    assert result_types == ["final", "interim"], records

    hold.set()
    await _drain_until_released(session)


@pytest.mark.asyncio
async def test_turn_summary_aggregates_ignored_post_dispatch_counts():
    """Per-turn totals ride the already-allowlisted privacy-safe summary."""
    emitted: list[dict[str, object]] = []
    hold = asyncio.Event()
    session, loop, _spoken = make_session(
        hold=hold, telemetry=_recording_telemetry(emitted)
    )
    await _dispatch_turn(session, loop)
    turn_id = session._active_smartpbx_turn_id
    assert turn_id is not None

    now = time.monotonic()
    # Age no longer decides refusal, but it is still what `elapsed_ms` reports,
    # so the peak below must track the oldest refusal.
    for age in (0.3, 1.2, 0.6):
        session._utterance_dispatched_at = now - age
        await session._accumulate_transcript("   ")
    for age in (0.2, 0.3):
        session._utterance_dispatched_at = now - age
        await session._set_transcript_interim("...")

    session._finalize_smartpbx_turns()

    summary = _summaries(emitted)[-1]
    assert summary["turn_id"] == turn_id
    assert summary["ignored_post_dispatch_finals"] == 3
    assert summary["ignored_post_dispatch_interims"] == 2
    peak = summary["ignored_post_dispatch_max_elapsed_ms"]
    assert 1200 <= peak <= 1900, peak
    assert peak <= server.POST_DISPATCH_ELAPSED_MS_MAX

    hold.set()
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_teardown_finalize_carries_post_dispatch_counts_exactly_once():
    """No loss at teardown, no double-emit from a delayed runner."""
    emitted: list[dict[str, object]] = []
    hold = asyncio.Event()
    session, loop, _spoken = make_session(
        hold=hold, telemetry=_recording_telemetry(emitted)
    )
    await _dispatch_turn(session, loop)
    turn_id = session._active_smartpbx_turn_id

    await session._accumulate_transcript("   ")
    await session._accumulate_transcript(" . ")
    await session._set_transcript_interim("...")

    telemetry = session._turn_telemetry
    session._finalize_smartpbx_turns()

    summaries = _summaries(emitted)
    assert len(summaries) == 1, summaries
    assert summaries[0]["ignored_post_dispatch_finals"] == 2
    assert summaries[0]["ignored_post_dispatch_interims"] == 1
    assert summaries[0]["ignored_post_dispatch_max_elapsed_ms"] >= 0
    assert session._smartpbx_post_dispatch_by_turn == {}

    # A delayed runner unwinding after teardown finds no open turn: the counts
    # travelled exactly once, on the teardown summary.
    telemetry.finish(
        turn_id, "completed", **session._post_dispatch_counts_for_turn(turn_id)
    )
    assert len(_summaries(emitted)) == 1

    hold.set()
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_clean_turn_summary_omits_post_dispatch_fields():
    """Absent, not zero — same convention as kb_ms / tool_ms."""
    emitted: list[dict[str, object]] = []
    hold = asyncio.Event()
    session, loop, _spoken = make_session(
        hold=hold, telemetry=_recording_telemetry(emitted)
    )
    await _dispatch_turn(session, loop)

    session._finalize_smartpbx_turns()

    summary = _summaries(emitted)[-1]
    for field in POST_DISPATCH_SUMMARY_FIELDS:
        assert field not in summary, field

    hold.set()
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_post_dispatch_turn_summary_fields_are_clamped_integers_only():
    """Adversarial: a corrupt counter cannot write an unbounded numeral."""
    emitted: list[dict[str, object]] = []
    hold = asyncio.Event()
    session, loop, _spoken = make_session(
        hold=hold, telemetry=_recording_telemetry(emitted)
    )
    await _dispatch_turn(session, loop)
    turn_id = session._active_smartpbx_turn_id

    await session._accumulate_transcript("   ")
    session._smartpbx_post_dispatch_by_turn[turn_id] = {
        "finals": 10 ** 9,
        "interims": 10 ** 9,
        "max_elapsed_ms": 10 ** 9,
    }

    session._finalize_smartpbx_turns()
    summary = _summaries(emitted)[-1]

    assert summary["ignored_post_dispatch_finals"] == 100_000
    assert summary["ignored_post_dispatch_interims"] == 100_000
    assert (
        summary["ignored_post_dispatch_max_elapsed_ms"]
        == server.POST_DISPATCH_ELAPSED_MS_MAX
    )
    for field in POST_DISPATCH_SUMMARY_FIELDS:
        value = summary[field]
        assert isinstance(value, int) and not isinstance(value, bool), field
    # No new free-text field rides along with the counts.
    assert {key for key, value in summary.items() if isinstance(value, str)} == {
        "event", "session_trace_id", "turn_id", "outcome", "endpoint_source",
    }

    hold.set()
    await asyncio.sleep(0)
