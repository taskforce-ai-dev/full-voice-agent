"""Fail-closed contracts for Kavya's Dialog MCP handover boundary."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
import logging
from types import SimpleNamespace

import httpx
import pytest

import smartpbx_mcp
from smartpbx_mcp import (
    DialogMCPCallControl,
    DialogMCPSettings,
    TransferDisabled,
)
from smartpbx_protocol import CallContext, MediaFormat


BASE_ENV = {
    "SMARTPBX_MCP_URL": "https://dialog.example:9443/ucp/v2/mcp",
    "SMARTPBX_API_KEY": "api-key-marker",
    "SMARTPBX_ACCOUNT_ID": "account-1",
    "SMARTPBX_MCP_ACCOUNT_HEADER": "account_id",
    "SMARTPBX_TRANSFER_DESTINATIONS_JSON": '{"human_support":"tel:+94110000000"}',
}


def settings(**overrides):
    environment = dict(BASE_ENV)
    environment.update(overrides)
    return DialogMCPSettings.from_env(environment)


@dataclass
class FakeResult:
    isError: bool
    content: list[object]


@dataclass
class AfterDispatch:
    error: Exception


class FakeSession:
    def __init__(self, outcome):
        self.outcome = outcome
        self.events = []

    async def initialize(self):
        self.events.append(("initialize",))
        if isinstance(self.outcome, BaseException):
            raise self.outcome

    async def call_tool(self, name, arguments):
        self.events.append(("call_tool", name, arguments))
        if isinstance(self.outcome, AfterDispatch):
            raise self.outcome.error
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


class FakeSessionFactory:
    def __init__(self, *outcomes):
        self.outcomes = list(outcomes) or [FakeResult(False, [object()])]
        self.calls = []
        self.sessions = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        session = FakeSession(self.outcomes.pop(0))
        self.sessions.append(session)

        @asynccontextmanager
        async def open_session():
            yield session

        return open_session()


def context(**overrides):
    values = {
        "call_id": "media-leg-must-not-be-used",
        "other_leg_call_id": "other-leg-only",
        "caller_id_number": "+94000000000",
        "callee_id_number": "+94110000000",
        "account_id": "account-1",
        "media_format": MediaFormat("g711_ulaw", 8000),
    }
    values.update(overrides)
    return CallContext(**values)


def test_empty_and_partial_configuration_disable_transfer():
    assert DialogMCPSettings.from_env({}).enabled is False
    assert DialogMCPSettings.from_env({"SMARTPBX_API_KEY": "marker"}).enabled is False
    assert DialogMCPSettings.from_env({
        "SMARTPBX_TRANSFER_DESTINATIONS_JSON": '{"human_support":"tel:+94110000000"}'
    }).enabled is False


def test_endpoint_alone_cannot_enable_transfer():
    configuration = DialogMCPSettings.from_env({
        "SMARTPBX_MCP_URL": "https://endpoint-only-mcp.example.invalid/ucp/v2/mcp",
        "SMARTPBX_API_KEY": "",
        "SMARTPBX_ACCOUNT_ID": "",
        "SMARTPBX_MCP_ACCOUNT_HEADER": "",
        "SMARTPBX_TRANSFER_DESTINATIONS_JSON": "{}",
    })

    assert configuration.enabled is False
    assert configuration.transfer_destinations == {}


def test_secret_and_destinations_do_not_appear_in_settings_repr():
    rendered = repr(settings())

    assert "api-key-marker" not in rendered
    assert "tel:+94110000000" not in rendered


@pytest.mark.asyncio
async def test_transfer_rejects_destination_not_in_operator_allowlist():
    control = DialogMCPCallControl(settings(), context(), FakeSessionFactory())

    with pytest.raises(TransferDisabled):
        await control.transfer_call("attacker_uri")


@pytest.mark.asyncio
async def test_missing_account_header_disables_transfer_without_network_call():
    fake_mcp = FakeSessionFactory()
    control = DialogMCPCallControl(
        settings(SMARTPBX_MCP_ACCOUNT_HEADER=""), context(), fake_mcp
    )

    with pytest.raises(TransferDisabled):
        await control.transfer_call("human_support")

    assert fake_mcp.calls == []


@pytest.mark.asyncio
async def test_transfer_uses_other_leg_call_id_and_exactly_one_configured_account_header():
    fake_mcp = FakeSessionFactory()
    control = DialogMCPCallControl(settings(), context(), fake_mcp)

    result = await control.transfer_call("human_support")

    assert result.transferred is True
    assert fake_mcp.calls[0]["headers"] == {
        "X-API-Key": "api-key-marker",
        "account_id": "account-1",
        "call_id": "other-leg-only",
    }
    assert fake_mcp.calls[0]["headers"].get("X-Account-ID") is None
    session = fake_mcp.sessions[0]
    assert session.events == [
        ("initialize",),
        ("call_tool", "transfer_call", {"number": "+94110000000"}),
    ]


@pytest.mark.asyncio
async def test_retryable_initial_connection_failure_retries_once_only():
    fake_mcp = FakeSessionFactory(
        httpx.ConnectError("unavailable"), FakeResult(False, [object()])
    )
    control = DialogMCPCallControl(settings(), context(), fake_mcp)

    result = await control.transfer_call("human_support")

    assert result.transferred is True
    assert len(fake_mcp.calls) == 2


@pytest.mark.asyncio
async def test_nested_lifecycle_connect_failure_retries_before_dispatch_only():
    fake_mcp = FakeSessionFactory(
        ExceptionGroup("anyio lifecycle", [ExceptionGroup("wrapped", [httpx.ConnectError("down")])]),
        FakeResult(False, [object()]),
    )
    control = DialogMCPCallControl(settings(), context(), fake_mcp)

    result = await control.transfer_call("human_support")

    assert result.transferred is True
    assert len(fake_mcp.calls) == 2


@pytest.mark.asyncio
async def test_nested_lifecycle_read_timeout_after_dispatch_is_not_retried():
    fake_mcp = FakeSessionFactory(
        AfterDispatch(ExceptionGroup("anyio lifecycle", [httpx.ReadTimeout("ambiguous")]))
    )
    control = DialogMCPCallControl(settings(), context(), fake_mcp)

    result = await control.transfer_call("human_support")

    assert result == smartpbx_mcp.TransferResult(False, "timeout")
    assert len(fake_mcp.calls) == 1


def status_error(status):
    request = httpx.Request("POST", "https://dialog.example/ucp/v2/mcp")
    return httpx.HTTPStatusError(
        "status", request=request, response=httpx.Response(status, request=request)
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("leaves", "expected_failure", "expected_calls"),
    [
        ([httpx.ConnectError("down"), httpx.ReadTimeout("never retry")], "timeout", 1),
        ([httpx.ConnectError("down"), status_error(400)], "client", 1),
        ([status_error(503), status_error(400)], "client", 1),
        ([httpx.ConnectError("down"), ValueError("nonretryable")], "transport", 1),
        ([httpx.ConnectError("down"), status_error(503)], None, 2),
    ],
)
async def test_nested_predispatch_retry_requires_every_leaf_to_be_retryable(
    leaves, expected_failure, expected_calls
):
    fake_mcp = FakeSessionFactory(
        ExceptionGroup("mixed lifecycle", leaves), FakeResult(False, [object()])
    )
    control = DialogMCPCallControl(settings(), context(), fake_mcp)

    result = await control.transfer_call("human_support")

    assert len(fake_mcp.calls) == expected_calls
    if expected_failure is None:
        assert result.transferred is True
    else:
        assert result == smartpbx_mcp.TransferResult(False, expected_failure)


@pytest.mark.asyncio
async def test_nested_cancellation_propagates_and_never_retries():
    cancellation = asyncio.CancelledError("cancelled")
    lifecycle_error = BaseExceptionGroup(
        "mixed lifecycle", [httpx.ConnectError("down"), cancellation]
    )
    fake_mcp = FakeSessionFactory(lifecycle_error, FakeResult(False, [object()]))
    control = DialogMCPCallControl(settings(), context(), fake_mcp)

    with pytest.raises(BaseExceptionGroup) as raised:
        await control.transfer_call("human_support")

    assert cancellation in tuple(smartpbx_mcp._exception_leaves(raised.value))
    assert len(fake_mcp.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "calls", "failure"),
    [(503, 2, "server"), (429, 1, "client")],
)
async def test_http_status_retry_is_limited_to_predispatch_5xx(status, calls, failure):
    request = httpx.Request("POST", "https://dialog.example/ucp/v2/mcp")
    error = httpx.HTTPStatusError("status", request=request, response=httpx.Response(status, request=request))
    fake_mcp = FakeSessionFactory(*([error] * calls))
    control = DialogMCPCallControl(settings(), context(), fake_mcp)

    result = await control.transfer_call("human_support")

    assert result == smartpbx_mcp.TransferResult(False, failure)
    assert len(fake_mcp.calls) == calls


@pytest.mark.asyncio
async def test_malformed_tool_result_is_bounded_and_never_retried():
    fake_mcp = FakeSessionFactory(FakeResult(False, []))
    control = DialogMCPCallControl(settings(), context(), fake_mcp)

    result = await control.transfer_call("human_support")

    assert result == smartpbx_mcp.TransferResult(False, "malformed_result")
    assert len(fake_mcp.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "result",
    [
        SimpleNamespace(isError="false", content=[object()]),
        SimpleNamespace(isError=False, content=(object(),)),
        SimpleNamespace(isError=False, content=[]),
        {"isError": False, "content": [object()]},
    ],
)
async def test_malformed_result_types_are_nonretryable(result):
    fake_mcp = FakeSessionFactory(result)

    outcome = await DialogMCPCallControl(settings(), context(), fake_mcp).transfer_call(
        "human_support"
    )

    assert outcome == smartpbx_mcp.TransferResult(False, "malformed_result")
    assert len(fake_mcp.calls) == 1


@pytest.mark.asyncio
async def test_iserror_tool_result_is_a_non_retryable_tool_failure():
    fake_mcp = FakeSessionFactory(FakeResult(True, [object()]))

    result = await DialogMCPCallControl(settings(), context(), fake_mcp).transfer_call("human_support")

    assert result == smartpbx_mcp.TransferResult(False, "tool_error")
    assert len(fake_mcp.calls) == 1


# --------------------------------------------------------------------------
# Live-observed Dialog UCP contract (dialog.cybergate.lk:9443/ucp/v2/mcp,
# captured 2026-08-11 with the production key). Every literal below is a
# recorded response body, not an assumption:
#
#   tools/list      transfer_call inputSchema -> {"number": <string>} required
#   tools/call OK   {"content":[{"type":"text","text":"..."}]}      (no isError)
#   tools/call err  {"content":[...],"isError":true}
#   JSON-RPC error  {"error":{"code":-32603,"message":"required argument
#                    \"destination number\" not found"}}  over HTTP 200
#
# The 11:10 UTC outage was the last of these: we sent `destination_number`,
# Dialog rejected it as a JSON-RPC error, and the SDK's McpError was filed as
# `transport`. These tests exist so neither half can silently regress.
# --------------------------------------------------------------------------


class FakeErrorData:
    """Shape-compatible stand-in for mcp.types.ErrorData."""

    def __init__(self, code, message):
        self.code = code
        self.message = message


class FakeMcpError(Exception):
    """Shape-compatible stand-in for mcp.shared.exceptions.McpError.

    The SDK is not installed in the test environment, and production classifies
    this by duck-typing (`.error.code`) precisely so it need not be.
    """

    __module__ = "mcp.shared.exceptions"

    def __init__(self, code, message):
        self.error = FakeErrorData(code, message)
        super().__init__(message)


class FakeTextBlock:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class AcknowledgedResult:
    """Dialog's real non-error reply: content present, `isError` absent."""

    def __init__(self, text="Transfer initiated"):
        self.content = [FakeTextBlock(text)]


@pytest.mark.asyncio
async def test_transfer_sends_the_bare_number_argument_dialog_actually_requires():
    """Regression for the 2026-08-11 outage: the argument name and value shape.

    `tel:` is an allowlist notation of ours; Dialog wants `number` holding the
    dialable value. Sending `destination_number` transferred nobody.
    """
    fake_mcp = FakeSessionFactory(AcknowledgedResult())

    result = await DialogMCPCallControl(settings(), context(), fake_mcp).transfer_call(
        "human_support"
    )

    assert result.transferred is True
    name, arguments = fake_mcp.sessions[0].events[1][1], fake_mcp.sessions[0].events[1][2]
    assert name == "transfer_call"
    assert arguments == {"number": "+94110000000"}
    assert "destination_number" not in arguments
    assert not arguments["number"].startswith("tel:")


@pytest.mark.asyncio
async def test_sip_destination_reaches_the_provider_as_the_whole_uri():
    fake_mcp = FakeSessionFactory(AcknowledgedResult())
    configured = settings(
        SMARTPBX_TRANSFER_DESTINATIONS_JSON='{"human_support":"sip:support@pbx.example"}'
    )

    result = await DialogMCPCallControl(configured, context(), fake_mcp).transfer_call(
        "human_support"
    )

    assert result.transferred is True
    assert fake_mcp.sessions[0].events[1][2] == {"number": "sip:support@pbx.example"}


@pytest.mark.asyncio
async def test_acknowledged_result_without_iserror_is_a_success_not_a_failure():
    """Dialog omits `isError` when it accepts the call. That is an acknowledgement.

    Treating the absent flag as malformed would fail closed on a transfer that
    really happened -- guest handed to a human AND the manager WhatsApped.
    """
    fake_mcp = FakeSessionFactory(AcknowledgedResult())

    result = await DialogMCPCallControl(settings(), context(), fake_mcp).transfer_call(
        "human_support"
    )

    assert result == smartpbx_mcp.TransferResult(True, None)
    assert len(fake_mcp.calls) == 1


@pytest.mark.asyncio
async def test_jsonrpc_error_reply_is_a_protocol_failure_never_transport():
    """The exact 11:10 UTC failure must no longer masquerade as a network fault."""
    fake_mcp = FakeSessionFactory(
        AfterDispatch(FakeMcpError(-32603, 'required argument "destination number" not found'))
    )

    result = await DialogMCPCallControl(settings(), context(), fake_mcp).transfer_call(
        "human_support"
    )

    assert result == smartpbx_mcp.TransferResult(False, "protocol")
    assert result.failure != "transport"
    assert len(fake_mcp.calls) == 1


@pytest.mark.asyncio
async def test_protocol_failure_is_logged_with_the_cause_and_no_dialable_digits(caplog):
    """An operator must be able to read *why* off one line, without leaking a number."""
    fake_mcp = FakeSessionFactory(
        AfterDispatch(FakeMcpError(-32602, "tool 'transfer_call' rejected +94711754668"))
    )

    with caplog.at_level(logging.INFO, logger="smartpbx_mcp"):
        await DialogMCPCallControl(settings(), context(), fake_mcp).transfer_call(
            "human_support"
        )

    line = "\n".join(record.getMessage() for record in caplog.records)
    assert "outcome=protocol" in line
    assert "code=-32602" in line
    assert "rejected" in line
    assert "94711754668" not in line


@pytest.mark.asyncio
async def test_provider_refusal_reports_its_reason_as_a_bounded_tool_error(caplog):
    """`routing key not found` is the real reply when the call leg is unknown."""
    fake_mcp = FakeSessionFactory(
        SimpleNamespace(isError=True, content=[FakeTextBlock("routing key not found")])
    )

    with caplog.at_level(logging.INFO, logger="smartpbx_mcp"):
        result = await DialogMCPCallControl(settings(), context(), fake_mcp).transfer_call(
            "human_support"
        )

    assert result == smartpbx_mcp.TransferResult(False, "tool_error")
    assert "outcome=tool_error" in caplog.text
    assert "routing key not found" in caplog.text


@pytest.mark.asyncio
async def test_protocol_failure_does_not_retry_and_stays_fail_closed():
    """A rejected request is not worth repeating, and must still reach the fallback."""
    fake_mcp = FakeSessionFactory(
        AfterDispatch(FakeMcpError(-32603, "bad argument")),
        AcknowledgedResult(),
    )

    result = await DialogMCPCallControl(settings(), context(), fake_mcp).transfer_call(
        "human_support"
    )

    assert result.transferred is False
    assert len(fake_mcp.calls) == 1


@pytest.mark.asyncio
async def test_genuine_network_failure_is_still_classified_as_transport():
    """The protocol class must not swallow the case it was carved out of."""
    fake_mcp = FakeSessionFactory(AfterDispatch(httpx.ReadError("connection reset")))

    result = await DialogMCPCallControl(settings(), context(), fake_mcp).transfer_call(
        "human_support"
    )

    assert result == smartpbx_mcp.TransferResult(False, "transport")


def test_configured_production_destination_passes_the_operator_allowlist():
    """The number swapped in on 2026-08-11 is valid config -- it was never the cause."""
    for number in ('{"human_support":"tel:+94711754668"}', '{"human_support":"tel:+94765603946"}'):
        configured = settings(SMARTPBX_TRANSFER_DESTINATIONS_JSON=number)
        assert configured.failure is None
        assert configured.enabled is True


class ChunkedResponse(httpx.AsyncByteStream):
    def __init__(self, *chunks):
        self.chunks = chunks
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk

    async def aclose(self):
        self.closed = True


class StaticResponseTransport(httpx.AsyncBaseTransport):
    def __init__(self, response):
        self.response = response

    async def handle_async_request(self, _request):
        return self.response


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("headers", "chunks", "expected"),
    [
        ({"Content-Length": "9"}, (b"small",), smartpbx_mcp.MCPResponseTooLarge),
        ({"Content-Encoding": "gzip"}, (b"small",), smartpbx_mcp.MCPUnsupportedContentEncoding),
    ],
)
async def test_bounded_transport_rejects_unsafe_declared_or_compressed_responses(headers, chunks, expected):
    stream = ChunkedResponse(*chunks)
    source = StaticResponseTransport(httpx.Response(200, headers=headers, stream=stream))
    transport = smartpbx_mcp._BoundedHTTPTransport(source, maximum_response_bytes=8)

    with pytest.raises(expected):
        await transport.handle_async_request(httpx.Request("POST", "https://dialog.example/mcp"))

    assert stream.closed is True


@pytest.mark.asyncio
async def test_bounded_transport_rejects_chunked_response_only_after_it_exceeds_the_limit():
    stream = ChunkedResponse(b"four", b"five!")
    source = StaticResponseTransport(httpx.Response(200, stream=stream))
    transport = smartpbx_mcp._BoundedHTTPTransport(source, maximum_response_bytes=8)
    response = await transport.handle_async_request(httpx.Request("POST", "https://dialog.example/mcp"))

    with pytest.raises(smartpbx_mcp.MCPResponseTooLarge):
        async for _chunk in response.stream:
            pass

    assert stream.closed is True


@pytest.mark.asyncio
async def test_redirects_remain_visible_to_the_caller_and_are_not_followed():
    seen = []

    def respond(request):
        seen.append(request.url.path)
        return httpx.Response(307, headers={"Location": "https://dialog.example/elsewhere"})

    transport = smartpbx_mcp._BoundedHTTPTransport(httpx.MockTransport(respond), 8)
    async with httpx.AsyncClient(transport=transport, follow_redirects=False) as client:
        response = await client.post("https://dialog.example/mcp")

    assert response.status_code == 307
    assert seen == ["/mcp"]


@pytest.mark.asyncio
async def test_tool_failure_is_not_retried_after_dispatch():
    fake_mcp = FakeSessionFactory(AfterDispatch(httpx.ReadTimeout("ambiguous")))
    control = DialogMCPCallControl(settings(), context(), fake_mcp)

    result = await control.transfer_call("human_support")

    assert result.transferred is False
    assert result.failure == "timeout"
    assert len(fake_mcp.calls) == 1


@pytest.mark.asyncio
async def test_context_account_mismatch_disables_transfer_without_network_call():
    fake_mcp = FakeSessionFactory()
    control = DialogMCPCallControl(settings(), context(account_id="other-account"), fake_mcp)

    with pytest.raises(TransferDisabled):
        await control.transfer_call("human_support")

    assert fake_mcp.calls == []


def test_invalid_configuration_and_unallowlisted_destination_have_stable_safe_codes():
    invalid = settings(SMARTPBX_MCP_URL="http://not-https.example")
    control = DialogMCPCallControl(invalid, context(), FakeSessionFactory())

    assert invalid.failure == "invalid_endpoint"
    with pytest.raises(TransferDisabled, match="invalid_configuration"):
        asyncio.run(control.transfer_call("human_support"))

    with pytest.raises(TransferDisabled, match="destination_not_allowed"):
        asyncio.run(DialogMCPCallControl(settings(), context(), FakeSessionFactory()).transfer_call("unknown"))


@pytest.mark.parametrize(
    "raw_destinations",
    [
        '{"human_support":"tel:+94110000000","human_support":"tel:+94110000001"}',
        "{" + ",".join(f'\"d{i}\":\"tel:+94110000000\"' for i in range(17)) + "}",
        '{"human_support":"not-a-destination"}',
    ],
)
def test_invalid_destinations_keep_a_safe_configuration_classification(raw_destinations):
    configured = settings(SMARTPBX_TRANSFER_DESTINATIONS_JSON=raw_destinations)

    assert configured.enabled is False
    assert configured.failure == "invalid_destinations"


@pytest.mark.asyncio
async def test_alternate_configured_account_header_is_the_only_account_header():
    fake_mcp = FakeSessionFactory()
    control = DialogMCPCallControl(
        settings(SMARTPBX_MCP_ACCOUNT_HEADER="X-Account-ID"), context(), fake_mcp
    )

    await control.transfer_call("human_support")

    assert fake_mcp.calls[0]["headers"] == {
        "X-API-Key": "api-key-marker",
        "X-Account-ID": "account-1",
        "call_id": "other-leg-only",
    }


def test_sdk_log_filter_is_scoped_to_mcp_loggers_only():
    global_client_logger = logging.getLogger("client")
    before = list(global_client_logger.filters)

    smartpbx_mcp._apply_sdk_logger_policy()

    assert global_client_logger.filters == before
    assert smartpbx_mcp._SDK_LOG_FILTER in logging.getLogger("mcp.client.streamable_http").filters


@pytest.mark.asyncio
async def test_smartpbx_disabled_transfer_never_reports_legacy_transferring(caplog):
    from tools import SmartPBXTransferContext, execute_tool, smartpbx_transfer_context

    token = smartpbx_transfer_context.set(SmartPBXTransferContext(call_control=None))
    try:
        result = await execute_tool("transfer_to_human", {"reason": "sensitive reason"})
    finally:
        smartpbx_transfer_context.reset(token)

    assert result == '{"status": "unavailable"}'
    assert "sensitive reason" not in caplog.text


@pytest.mark.asyncio
async def test_smartpbx_concurrent_transfer_tool_calls_dispatch_only_once():
    from tools import SmartPBXTransferContext, execute_tool, smartpbx_transfer_context

    fake_mcp = FakeSessionFactory()
    control = DialogMCPCallControl(settings(), context(), fake_mcp)
    transfer_context = SmartPBXTransferContext(call_control=control)
    token = smartpbx_transfer_context.set(transfer_context)
    try:
        results = await asyncio.gather(
            execute_tool("transfer_to_human", {"reason": "first"}),
            execute_tool("transfer_to_human", {"reason": "second"}),
        )
    finally:
        smartpbx_transfer_context.reset(token)

    assert results == ['{"status": "transferred"}', '{"status": "unavailable"}']
    assert len(fake_mcp.calls) == 1


@pytest.mark.asyncio
async def test_legacy_transfer_tool_result_is_byte_for_byte_equivalent():
    from tools import execute_tool, smartpbx_transfer_context

    assert smartpbx_transfer_context.get() is None
    result = await execute_tool(
        "transfer_to_human", {"reason": "Caller asked for the manager."}
    )

    assert result == '{"status": "transferring", "reason": "Caller asked for the manager."}'
