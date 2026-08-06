"""Fail-closed contracts for Kavya's Dialog MCP handover boundary."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
import logging

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
        if isinstance(self.outcome, Exception):
            raise self.outcome

    async def call_tool(self, name, arguments):
        self.events.append(("call_tool", name, arguments))
        if isinstance(self.outcome, AfterDispatch):
            raise self.outcome.error
        if isinstance(self.outcome, Exception):
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
        ("call_tool", "transfer_call", {"destination_number": "tel:+94110000000"}),
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
async def test_iserror_tool_result_is_a_non_retryable_tool_failure():
    fake_mcp = FakeSessionFactory(FakeResult(True, [object()]))

    result = await DialogMCPCallControl(settings(), context(), fake_mcp).transfer_call("human_support")

    assert result == smartpbx_mcp.TransferResult(False, "tool_error")
    assert len(fake_mcp.calls) == 1


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
