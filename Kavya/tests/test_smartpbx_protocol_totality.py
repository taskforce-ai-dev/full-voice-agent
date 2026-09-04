"""Hand-rolled parser-totality fuzz for `parse_smartpbx_event`.

`hypothesis` is not installed in this venv (audit-tests.md sec 1/6), so this
uses a fixed-seed `random.Random` generator instead: for any `str` input,
`parse_smartpbx_event` must either return a member of its closed event union
or raise `ProtocolViolation` -- never anything else (audit-tests.md gap #12,
sec 6 property 1).

2000 deterministic cases are generated across four categories: raw
adversarial unicode text ("random bytes" reaching the parser as `str`),
mutated/truncated fragments of otherwise-valid Dialog JSON, deep JSON
nesting (bomb for `json.loads`'s recursive descent), and text carrying a
lone UTF-16 surrogate code point (illegal in UTF-8, so `raw.encode("utf-8")`
at smartpbx_protocol.py:117 can raise on it directly, before any JSON
parsing is even attempted).
"""

from __future__ import annotations

import json
import random

import pytest

from smartpbx_protocol import (
    ConnectedEvent,
    DtmfEvent,
    HangupEvent,
    MediaEvent,
    ProtocolViolation,
    StartEvent,
    StopEvent,
    UnsupportedEvent,
    parse_smartpbx_event,
)


_EVENT_UNION = (
    ConnectedEvent, StartEvent, MediaEvent, DtmfEvent, HangupEvent,
    StopEvent, UnsupportedEvent,
)

_SEED = 20260830
_CASES_PER_CATEGORY = 500

_VALID_SAMPLES = [
    '{"event":"connected"}',
    (
        '{"event":"start","start":{"callId":"c1","otherLegCallId":"o1",'
        '"callerIdNumber":"cid","calleeIdNumber":"cee","accountId":"a1",'
        '"mediaFormat":{"encoding":"g711_ulaw","sampleRate":8000}}}'
    ),
    '{"event":"media","media":{"payload":"AAAA"}}',
    '{"event":"dtmf","dtmf":{"callId":"c1","otherLegCallId":"o1","digit":"5","durationMs":100}}',
    '{"event":"hangup","hangup":{"callId":"c1","otherLegCallId":"o1","reason":"normal"}}',
    '{"event":"stop"}',
]


def _random_unicode_char(rng: random.Random) -> str:
    """One code point from the full BMP+astral range, surrogates excluded."""
    while True:
        codepoint = rng.randint(0, 0x10FFFF)
        if 0xD800 <= codepoint <= 0xDFFF:
            continue
        return chr(codepoint)


def _random_bytes_cases(rng: random.Random, count: int) -> list[tuple[str, str]]:
    cases = []
    for _ in range(count):
        length = rng.randint(0, 300)
        text = "".join(_random_unicode_char(rng) for _ in range(length))
        cases.append(("random_bytes", text))
    return cases


def _json_fragment_cases(rng: random.Random, count: int) -> list[tuple[str, str]]:
    cases = []
    for _ in range(count):
        sample = rng.choice(_VALID_SAMPLES)
        mutation = rng.randint(0, 3)
        if mutation == 0 and sample:
            # Truncate at a random point.
            cut = rng.randint(0, len(sample))
            text = sample[:cut]
        elif mutation == 1:
            # Delete a random slice.
            if len(sample) < 2:
                text = sample
            else:
                start = rng.randint(0, len(sample) - 1)
                end = rng.randint(start, len(sample))
                text = sample[:start] + sample[end:]
        elif mutation == 2:
            # Insert random garbage at a random position.
            position = rng.randint(0, len(sample))
            garbage = "".join(_random_unicode_char(rng) for _ in range(rng.randint(1, 8)))
            text = sample[:position] + garbage + sample[position:]
        else:
            # Swap a structural character.
            if not sample:
                text = sample
            else:
                position = rng.randint(0, len(sample) - 1)
                replacement = rng.choice("{}[]:,\"0123456789")
                text = sample[:position] + replacement + sample[position + 1:]
        cases.append(("json_fragment", text))
    return cases


def _deep_nesting_cases(rng: random.Random, count: int) -> list[tuple[str, str]]:
    # CPython's C-accelerated json decoder recurses through
    # Py_EnterRecursiveCall (tied to sys.getrecursionlimit(), default 1000)
    # regardless of the pure-Python vs C scanner, but the effective nesting
    # depth before that trips is well above 1000 in practice -- measured on
    # this interpreter: array nesting `[[[...` needs depth >~ 8000-10000,
    # object nesting `{"a":{"a":...` needs depth >~ 10000. Both depth lists
    # below are chosen comfortably above their measured threshold while
    # staying under the parser's own max_message_chars=65536 bound used in
    # this test (so MESSAGE_TOO_BIG can never mask the recursion case).
    array_depths = [10000, 15000, 20000, 25000, 30000]
    object_depths = [10000, 10200, 10400, 10600, 10800]
    cases = []
    for _ in range(count):
        if rng.random() < 0.5:
            depth = rng.choice(array_depths)
            text = "[" * depth + "]" * depth
        else:
            depth = rng.choice(object_depths)
            text = '{"a":' * depth + "0" + "}" * depth
        cases.append(("deep_nesting", text))
    return cases


def _lone_surrogate_cases(rng: random.Random, count: int) -> list[tuple[str, str]]:
    cases = []
    for _ in range(count):
        surrogate = chr(rng.randint(0xD800, 0xDFFF))
        placement = rng.randint(0, 3)
        if placement == 0:
            text = surrogate
        elif placement == 1:
            text = surrogate + json.dumps({"event": "connected"})
        elif placement == 2:
            sample = rng.choice(_VALID_SAMPLES)
            position = rng.randint(0, len(sample))
            text = sample[:position] + surrogate + sample[position:]
        else:
            text = f'{{"event":"start","note":"{surrogate}"}}'
        cases.append(("lone_surrogate", text))
    return cases


def _generate_cases() -> list[tuple[str, str]]:
    rng = random.Random(_SEED)
    cases: list[tuple[str, str]] = []
    cases += _random_bytes_cases(rng, _CASES_PER_CATEGORY)
    cases += _json_fragment_cases(rng, _CASES_PER_CATEGORY)
    cases += _deep_nesting_cases(rng, _CASES_PER_CATEGORY)
    cases += _lone_surrogate_cases(rng, _CASES_PER_CATEGORY)
    return cases


def test_parse_smartpbx_event_returns_union_member_or_protocol_violation_over_2000_cases():
    """For any `str`, `parse_smartpbx_event` must return a union member or
    raise `ProtocolViolation` -- and nothing else. 2000 fixed-seed
    generated cases across 4 categories (random unicode text, mutated
    Dialog-shaped JSON fragments, deep nesting, lone surrogates)."""
    cases = _generate_cases()
    assert len(cases) == 2000

    for category, raw in cases:
        try:
            result = parse_smartpbx_event(
                raw, max_message_chars=65536, max_audio_bytes=32768
            )
        except ProtocolViolation:
            continue
        except Exception as leak:  # noqa: BLE001 -- deliberately broad: totality check
            raise AssertionError(
                f"parse_smartpbx_event leaked {type(leak).__name__} for a "
                f"{category!r} case instead of raising ProtocolViolation: "
                f"{raw!r}"
            ) from leak
        assert isinstance(result, _EVENT_UNION), (
            f"parse_smartpbx_event returned a non-union value for a "
            f"{category!r} case: {result!r}"
        )
