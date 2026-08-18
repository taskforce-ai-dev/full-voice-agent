"""Booking slots must survive history trimming (mid-call slot amnesia).

Every turn assembles ``[system_prompt] + history`` and ``_trim_history`` keeps
only the last MAX_HISTORY_MESSAGES, dropping the oldest turns. On a real call a
long phone-number retry loop pushed the early date/guest turns out of that
window, so Kavya re-asked for check-in/check-out and the guest count. Slots
captured from tool calls are held on the session and re-injected into the system
context every turn, so they survive trimming. Shared MediaStreamSession code —
covers both the Twilio and SmartPBX paths.
"""

from __future__ import annotations

import inspect

import pytest

import server


def make_session(lang="en"):
    return server.MediaStreamSession(websocket=None, lang=lang, media_transport=None)


def test_captured_slots_appear_in_the_active_system_prompt():
    session = make_session()
    session._capture_booking_slots(
        "check_availability", {"check_in": "2026-09-27", "check_out": "2026-09-30"}
    )
    session._capture_booking_slots(
        "create_booking",
        {"room_type": "Mount Monarch Chalet", "guest_name": "Jane Doe",
         "num_adults": 2, "num_children": 1, "guest_phone": "+94771234567"},
    )

    prompt = session._active_system_prompt()
    assert session.system_prompt in prompt, "the base prompt must still be present"
    for token in ("2026-09-27", "2026-09-30", "Mount Monarch Chalet", "Jane Doe"):
        assert token in prompt, token
    assert "2" in prompt and "adult" in prompt.lower()


def test_no_slots_note_when_nothing_captured():
    session = make_session()
    assert session._booking_slots_note() == ""
    assert session._active_system_prompt() == session.system_prompt


def test_slots_survive_a_long_number_retry_loop_that_evicts_early_turns():
    session = make_session()

    # Guest gives dates + guest count early; the availability check tool carries
    # the dates, and the booking tool the guest count.
    session._capture_booking_slots(
        "check_availability", {"check_in": "2026-09-27", "check_out": "2026-09-30"}
    )
    session._capture_booking_slots("create_booking", {"num_adults": 2})
    session.history.append({"role": "user", "content": "dates are 27th to 30th September"})
    session.history.append({"role": "assistant", "content": "Lovely, let me check availability."})

    # A long phone-number retry sub-flow bloats the history well past the window.
    for attempt in range(80):
        session.history.append({"role": "user", "content": f"no, my number is 077 {attempt}"})
        session.history.append({"role": "assistant", "content": "Let me read that back."})
    session.history = server._trim_history(session.history)

    # The early date turn has genuinely fallen out of the window...
    assert len(session.history) <= server.MAX_HISTORY_MESSAGES
    assert not any("September" in str(m) for m in session.history), (
        "the test must actually reproduce eviction, or it proves nothing"
    )

    # ...but the slots are still in the context the model sees this turn.
    prompt = session._active_system_prompt()
    assert "2026-09-27" in prompt
    assert "2026-09-30" in prompt
    assert "adult" in prompt.lower()


def test_slots_capture_is_incremental_and_last_write_wins():
    session = make_session()
    session._capture_booking_slots("check_availability", {"check_in": "2026-09-27"})
    session._capture_booking_slots("create_booking", {"check_in": "2026-10-01", "check_out": "2026-10-03"})

    note = session._booking_slots_note()
    assert "2026-10-01" in note, "a later value must supersede the earlier one"
    assert "2026-09-27" not in note
    assert "2026-10-03" in note


def test_capture_ignores_unrelated_tools_and_blank_values():
    session = make_session()
    session._capture_booking_slots("retrieve_booking", {"booking_id": "XYZ"})
    session._capture_booking_slots("create_booking", {"check_in": "", "guest_name": None})
    assert session._booking_slots_note() == ""


def test_all_provider_paths_inject_the_slot_aware_prompt():
    # Guard against a single provider path regressing to the bare system prompt.
    import inspect

    for method in ("_run_llm", "_run_llm_gemini"):
        src = inspect.getsource(getattr(server.MediaStreamSession, method))
        assert "_active_system_prompt()" in src, (
            f"{method} must assemble the slot-aware system prompt, not self.system_prompt"
        )
    # The Claude path splits the slots note into a separate uncached system
    # block (prompt-cache layout) — still slot-aware, different assembly.
    claude_src = inspect.getsource(server.MediaStreamSession._run_llm_claude)
    assert "_build_claude_system_blocks(" in claude_src
    assert "_booking_slots_note()" in claude_src, (
        "_run_llm_claude must pass the slots note into the system blocks"
    )


@pytest.mark.parametrize("runner_name", ("_run_llm", "_run_llm_gemini", "_run_llm_claude"))
def test_captured_spoken_number_persists_as_a_slot_for_every_media_runner(runner_name):
    """A completed spoken capture must survive later model turns.

    Each media runner parses a tool result and calls the shared capture-completion
    hook.  This test pins the real cross-runner boundary: a normalized successful
    number becomes the durable `guest_phone` slot rendered into the next active
    system prompt, rather than existing only in a soon-to-be-trimmed tool result.
    """
    session = make_session()
    captured = {
        "status": "captured",
        "valid": True,
        "normalized": "+94771234567",
    }

    session._record_capture_tool_completion("capture_spoken_number", captured)

    assert session._booking_slots["guest_phone"] == "+94771234567"
    assert "+94771234567" in session._active_system_prompt()
    assert "_record_capture_tool_completion(" in inspect.getsource(
        getattr(server.MediaStreamSession, runner_name)
    ), f"{runner_name} must use the shared capture-result boundary"


@pytest.mark.parametrize("result", (
    {"status": "needs_more", "valid": False, "normalized": ""},
    {"status": "invalid", "valid": False, "normalized": ""},
))
def test_incomplete_or_invalid_spoken_number_never_persists_a_guest_phone_slot(result):
    session = make_session()

    session._record_capture_tool_completion("capture_spoken_number", result)

    assert "guest_phone" not in session._booking_slots
    assert "phone:" not in session._active_system_prompt().lower()
