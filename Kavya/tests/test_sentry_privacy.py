"""Sentry privacy hardening for the Kavya server.

Verifies the fix for the audit finding "Sentry ships frame local variables on
unhandled exceptions": `sentry_sdk.init` must disable local-variable capture
and request bodies, and `_sentry_before_send` must scrub `extra`/`contexts`
and any breadcrumb carrying a digit run of 5+ (phone numbers, booking
references) before an event ever leaves the process.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("sentry_sdk")

import sentry_sdk  # noqa: E402
from sentry_sdk.transport import Transport  # noqa: E402

import server  # noqa: E402


class _CapturingTransport(Transport):
    """A Sentry transport that records envelopes instead of sending them."""

    def __init__(self):
        super().__init__({"dsn": "https://public@sentry.example/1"})
        self.captured: list[dict] = []

    def capture_envelope(self, envelope):
        for item in envelope.items:
            if item.data_category == "error":
                self.captured.append(item.payload.json)


async def _one_event_stream(event):
    yield event


class _RecordingClaudeStream:
    async def __aenter__(self):
        return _one_event_stream(
            SimpleNamespace(
                type="content_block_delta",
                delta=SimpleNamespace(type="text_delta", text="Acknowledged."),
            )
        )

    async def __aexit__(self, *_args):
        return False


class _RecordingClaudeMessages:
    def stream(self, **_kwargs):
        return _RecordingClaudeStream()


class _RecordingClaude:
    def __init__(self):
        self.messages = _RecordingClaudeMessages()


@pytest.fixture
def sentry_capture():
    """Init Sentry exactly like server.py's real block, against a fake transport."""
    transport = _CapturingTransport()
    sentry_sdk.init(
        dsn="https://public@sentry.example/1",
        transport=transport,
        traces_sample_rate=0.0,
        environment="test",
        send_default_pii=False,
        enable_logs=False,
        include_local_variables=False,
        max_request_body_size="never",
        before_send=server._sentry_before_send,
    )
    try:
        yield transport
    finally:
        # Do not leak a live Sentry client into other test modules.
        sentry_sdk.init(dsn=None)


def _make_session():
    session = server.MediaStreamSession(
        websocket=None,
        lang="en",
        anthropic_client=_RecordingClaude(),
        media_transport=None,
        llm_provider="claude",
        model="claude-privacy-test",
    )
    session.tools = []

    async def _no_speak(*_args, **_kwargs):
        return None

    session._invoke_speak = _no_speak
    return session


@pytest.mark.asyncio
async def test_kb_retrieval_exception_is_reported_without_locals_or_digit_breadcrumbs(
    monkeypatch, sentry_capture
):
    session = _make_session()
    assert session._is_smartpbx_session() is False  # exercises the logger.exception branch

    phone_like = "0771234567"

    def _boom(_text):
        # A realistic local variable an unpatched Sentry init would have
        # captured verbatim (caller phone number).
        caller_phone = phone_like  # noqa: F841
        raise RuntimeError(f"kb lookup failed for {phone_like}")

    monkeypatch.setattr(server, "retrieve_context", _boom)
    # Simulate a digit-bearing breadcrumb reaching Sentry from some other
    # code path (e.g. a third-party HTTP client integration) immediately
    # before the failure.
    sentry_sdk.add_breadcrumb(message=f"dialing {phone_like}", category="test")
    sentry_sdk.add_breadcrumb(message="ordinary breadcrumb, no digits", category="test")

    # Deliberately not a rate-intent phrase: those short-circuit KB retrieval
    # via `_current_rate_context()` and would never reach `retrieve_context`.
    await session._process_utterance("Tell me about the forest walking trails")
    sentry_sdk.flush()

    assert sentry_capture.captured, "the KB retrieval failure must reach Sentry"
    event = sentry_capture.captured[-1]

    # include_local_variables=False: no frame in the captured traceback may
    # carry a `vars` mapping.
    frames = (
        event.get("exception", {}).get("values", [{}])[0].get("stacktrace", {}).get("frames", [])
    )
    assert frames, "expected a captured stacktrace"
    for frame in frames:
        assert "vars" not in frame

    # before_send drops extra/contexts wholesale.
    assert "extra" not in event
    assert "contexts" not in event

    # before_send drops any breadcrumb whose message contains a digit run of 5+.
    breadcrumb_messages = [
        crumb.get("message", "")
        for crumb in event.get("breadcrumbs", {}).get("values", [])
    ]
    assert "ordinary breadcrumb, no digits" in breadcrumb_messages
    assert not any(server._SENTRY_LOGGABLE_DIGITS.search(msg) for msg in breadcrumb_messages)
    assert not any(phone_like in msg for msg in breadcrumb_messages)


def test_sentry_before_send_drops_extra_contexts_and_digit_breadcrumbs_directly():
    event = {
        "extra": {"caller_phone": "0771234567"},
        "contexts": {"trace": {"trace_id": "abc123"}},
        "breadcrumbs": {
            "values": [
                {"message": "booking ref 887711", "category": "x"},
                {"message": "clean breadcrumb", "category": "x"},
                {"category": "x"},  # no "message" key at all
                "not-a-dict",  # malformed entries must not raise
            ]
        },
    }

    result = server._sentry_before_send(event, {})

    assert "extra" not in result
    assert "contexts" not in result
    messages = [c.get("message") for c in result["breadcrumbs"]["values"] if isinstance(c, dict)]
    assert "clean breadcrumb" in messages
    assert "booking ref 887711" not in messages


def test_sentry_before_send_never_drops_the_event_itself():
    event = {"message": "no extra, no contexts, no breadcrumbs"}
    assert server._sentry_before_send(event, {}) is event


@pytest.mark.parametrize("digits", ["1234", "12345", "999999999"])
def test_sentry_loggable_digits_pattern_matches_five_or_more_digits(digits):
    matched = bool(server._SENTRY_LOGGABLE_DIGITS.search(f"call {digits} now"))
    assert matched == (len(digits) >= 5)


def test_server_sentry_init_call_disables_locals_and_request_bodies_and_wires_scrubber():
    import inspect

    source = inspect.getsource(server)
    init_block = source.split("if os.getenv(\"SENTRY_DSN\"):", 1)[1]
    # Bounded by the next statement (set_tag), not a naive first ")" split —
    # traces_sample_rate=float(os.getenv(...)) has a nested paren.
    init_call = init_block.split("sentry_sdk.init(", 1)[1].split("sentry_sdk.set_tag(", 1)[0]

    assert "include_local_variables=False" in init_call
    assert 'max_request_body_size="never"' in init_call
    assert "before_send=_sentry_before_send" in init_call
