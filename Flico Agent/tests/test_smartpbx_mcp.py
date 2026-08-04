"""Contract tests for allowlisted Dialog MCP call control."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass

import httpx
import pytest

from smartpbx_mcp import DialogMCPCallControl, DialogMCPSettings
from smartpbx_protocol import CallContext, MediaFormat


BASE_ENV = {
    "SMARTPBX_MCP_URL": "https://dialog.example:9443/ucp/v2/mcp",
    "SMARTPBX_API_KEY": "test-secret-never-log",
    "SMARTPBX_ACCOUNT_ID": "account-1",
    "SMARTPBX_MCP_ACCOUNT_HEADER": "account_id",
    "SMARTPBX_TRANSFER_DESTINATIONS_JSON": (
        '{"live_agent":"tel:+94110000000","support":"sip:200@example.com"}'
    ),
}


def settings(**overrides):
    environment = dict(BASE_ENV)
    environment.update(overrides)
    return DialogMCPSettings.from_env(environment)


def context(**overrides):
    values = {
        "call_id": "call-main-sensitive",
        "other_leg_call_id": "call-other-leg-sensitive",
        "caller_id_number": "+94112223333",
        "callee_id_number": "+94114698850",
        "account_id": "account-1",
        "media_format": MediaFormat(encoding="g711_ulaw", sample_rate=8000),
    }
    values.update(overrides)
    return CallContext(**values)


@dataclass
class FakeResult:
    isError: bool
    content: list[object]


_DEFAULT_OUTCOME = object()


class FakeSession:
    def __init__(self, outcome=_DEFAULT_OUTCOME):
        self.outcome = (
            FakeResult(False, [object()]) if outcome is _DEFAULT_OUTCOME else outcome
        )
        self.events = []

    async def initialize(self):
        self.events.append(("initialize",))

    async def call_tool(self, name, arguments):
        assert self.events == [("initialize",)]
        self.events.append(("call_tool", name, arguments))
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


class FakeSessionFactory:
    def __init__(self, *outcomes):
        self.outcomes = list(outcomes) or [FakeResult(False, [object()])]
        self.calls = []
        self.sessions = []

    def __call__(self, *, endpoint, headers, connect_timeout, read_timeout):
        self.calls.append({
            "endpoint": endpoint,
            "headers": dict(headers),
            "connect_timeout": connect_timeout,
            "read_timeout": read_timeout,
        })
        outcome = self.outcomes.pop(0)
        session = FakeSession(outcome)
        self.sessions.append(session)

        @asynccontextmanager
        async def open_session():
            yield session

        return open_session()


def http_error(status_code):
    request = httpx.Request("POST", "https://dialog.example/ucp/v2/mcp")
    response = httpx.Response(status_code, request=request)
    return httpx.HTTPStatusError("request failed", request=request, response=response)


def test_empty_environment_is_disabled_and_unconfigured():
    value = DialogMCPSettings.from_env({})

    assert value.enabled is False
    assert value.configured is False
    assert value.transfer_destinations == {}


@pytest.mark.parametrize(
    "override",
    [
        {"SMARTPBX_MCP_URL": "https://dialog.example/mcp"},
        {"SMARTPBX_API_KEY": "secret"},
        {"SMARTPBX_MCP_ACCOUNT_HEADER": "account_id"},
        {"SMARTPBX_TRANSFER_DESTINATIONS_JSON": '{"live_agent":"tel:+94110000000"}'},
    ],
)
def test_partial_mcp_configuration_fails_closed(override):
    with pytest.raises(ValueError, match="configuration"):
        DialogMCPSettings.from_env(override)


@pytest.mark.parametrize(
    "url",
    [
        "http://dialog.example/mcp",
        "https:///mcp",
        "https://user:password@dialog.example/mcp",
        "https://dialog.example/mcp#fragment",
    ],
)
def test_endpoint_must_be_an_uncredentialed_https_url(url):
    with pytest.raises(ValueError, match="configuration"):
        settings(SMARTPBX_MCP_URL=url)


def test_secret_and_destinations_do_not_appear_in_settings_repr():
    value = settings()

    rendered = repr(value)
    assert "test-secret-never-log" not in rendered
    assert "tel:+94110000000" not in rendered
    assert "sip:200@example.com" not in rendered


@pytest.mark.parametrize("header", ["", "Account-ID", "account_id,X-Account-ID"])
def test_account_header_must_be_explicitly_one_documented_value(header):
    with pytest.raises(ValueError, match="configuration"):
        settings(SMARTPBX_MCP_ACCOUNT_HEADER=header)


@pytest.mark.parametrize("header", ["account_id", "X-Account-ID"])
def test_each_documented_account_header_can_be_selected(header):
    assert settings(SMARTPBX_MCP_ACCOUNT_HEADER=header).account_header == header


@pytest.mark.parametrize(
    "destinations",
    [
        "[]",
        "not-json",
        '{"UPPER":"tel:+94110000000"}',
        '{"live agent":"tel:+94110000000"}',
        '{"live_agent":"+94110000000"}',
        '{"live_agent":"https://attacker.example"}',
        '{"live_agent":"tel:0110000000"}',
        '{"live_agent":"sip:user name@example.com"}',
        '{"live_agent":"sip:200@example.com:99999"}',
        '{"live_agent":"tel:+94110000000","live_agent":"tel:+94770000000"}',
    ],
)
def test_destination_map_rejects_unbounded_or_unsafe_values(destinations):
    with pytest.raises(ValueError, match="configuration"):
        settings(SMARTPBX_TRANSFER_DESTINATIONS_JSON=destinations)


def test_controller_rejects_account_mismatch_and_blank_other_leg_call_id():
    with pytest.raises(ValueError, match="context"):
        DialogMCPCallControl(settings(), context(account_id="wrong"))
    with pytest.raises(ValueError, match="context"):
        DialogMCPCallControl(settings(), context(other_leg_call_id=" "))
    with pytest.raises(ValueError, match="context"):
        DialogMCPCallControl(settings(), context(other_leg_call_id="call\nheader"))


def test_header_values_and_endpoint_are_bounded_and_injection_safe():
    unsafe = {
        "SMARTPBX_API_KEY": "secret\nheader",
        "oversized_api_key": "x" * 513,
        "SMARTPBX_ACCOUNT_ID": "account\rheader",
        "oversized_account_id": "x" * 257,
        "query_url": "https://dialog.example/mcp?credential=secret",
        "oversized_url": "https://" + "x" * 2048,
    }
    for case, value in unsafe.items():
        name = {
            "oversized_api_key": "SMARTPBX_API_KEY",
            "oversized_account_id": "SMARTPBX_ACCOUNT_ID",
            "query_url": "SMARTPBX_MCP_URL",
            "oversized_url": "SMARTPBX_MCP_URL",
        }.get(case, case)
        with pytest.raises(ValueError, match="configuration"):
            settings(**{name: value})


def test_only_stable_call_control_methods_are_exposed():
    control = DialogMCPCallControl(settings(), context())

    assert hasattr(control, "transfer_call")
    assert hasattr(control, "hangup_call")
    assert not hasattr(control, "create_lead")
    assert not hasattr(control, "send_dtmf")
    assert not hasattr(control, "make_call")


@pytest.mark.asyncio
async def test_transfer_initializes_then_calls_exact_tool_with_allowlisted_destination():
    factory = FakeSessionFactory(FakeResult(False, [object()]))
    control = DialogMCPCallControl(settings(), context(), session_factory=factory)

    assert await control.transfer_call("live_agent") is True

    assert factory.sessions[0].events == [
        ("initialize",),
        (
            "call_tool",
            "transfer_call",
            {"destination_number": "tel:+94110000000"},
        ),
    ]


@pytest.mark.asyncio
async def test_hangup_calls_exact_stable_tool_with_empty_arguments():
    factory = FakeSessionFactory(FakeResult(False, [object()]))
    control = DialogMCPCallControl(settings(), context(), session_factory=factory)

    assert await control.hangup_call() is True
    assert factory.sessions[0].events[-1] == ("call_tool", "hangup_call", {})


@pytest.mark.asyncio
@pytest.mark.parametrize("account_header", ["account_id", "X-Account-ID"])
async def test_outbound_headers_use_other_leg_call_id_and_exactly_one_account_header(
    account_header,
):
    factory = FakeSessionFactory(FakeResult(False, [object()]))
    control = DialogMCPCallControl(
        settings(SMARTPBX_MCP_ACCOUNT_HEADER=account_header),
        context(),
        session_factory=factory,
    )

    assert await control.hangup_call() is True

    headers = factory.calls[0]["headers"]
    alternate = "X-Account-ID" if account_header == "account_id" else "account_id"
    assert headers == {
        "X-API-Key": "test-secret-never-log",
        account_header: "account-1",
        "call_id": "call-other-leg-sensitive",
    }
    assert alternate not in headers
    assert "callId" not in headers


@pytest.mark.asyncio
async def test_arbitrary_runtime_destination_is_rejected_without_opening_a_session():
    factory = FakeSessionFactory(FakeResult(False, [object()]))
    control = DialogMCPCallControl(settings(), context(), session_factory=factory)

    assert await control.transfer_call("tel:+94770000000") is False
    assert await control.transfer_call("unknown_key") is False
    assert factory.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "result",
    [FakeResult(True, [object()]), FakeResult(False, []), object(), None],
)
async def test_mcp_error_and_malformed_results_fail_closed_without_retry(result):
    factory = FakeSessionFactory(result)
    control = DialogMCPCallControl(
        settings(SMARTPBX_MCP_RETRY_COUNT="2"),
        context(),
        session_factory=factory,
    )

    assert await control.hangup_call() is False
    assert len(factory.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [400, 401, 403, 404, 422])
async def test_auth_and_validation_http_failures_are_not_retried(status_code):
    factory = FakeSessionFactory(http_error(status_code))
    control = DialogMCPCallControl(
        settings(SMARTPBX_MCP_RETRY_COUNT="2"),
        context(),
        session_factory=factory,
    )

    assert await control.hangup_call() is False
    assert len(factory.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "retryable",
    [
        httpx.ConnectError("connection failed"),
        httpx.ReadTimeout("read timed out"),
        http_error(503),
        asyncio.TimeoutError(),
    ],
)
async def test_transport_timeout_and_5xx_failures_retry_once_then_succeed(retryable):
    factory = FakeSessionFactory(retryable, FakeResult(False, [object()]))
    control = DialogMCPCallControl(
        settings(SMARTPBX_MCP_RETRY_COUNT="1"),
        context(),
        session_factory=factory,
    )

    assert await control.hangup_call() is True
    assert len(factory.calls) == 2


@pytest.mark.asyncio
async def test_failure_logs_are_structured_and_redacted(caplog):
    caplog.set_level("INFO")
    response_body = "provider-response-sensitive"
    error = httpx.HTTPStatusError(
        response_body,
        request=httpx.Request("POST", "https://dialog.example/mcp"),
        response=httpx.Response(401, text=response_body),
    )
    factory = FakeSessionFactory(error)
    control = DialogMCPCallControl(settings(), context(), session_factory=factory)

    assert await control.transfer_call("live_agent") is False

    logged = caplog.text
    assert "smartpbx_mcp_result" in logged
    for sensitive in (
        "test-secret-never-log",
        "tel:+94110000000",
        "call-main-sensitive",
        "call-other-leg-sensitive",
        response_body,
    ):
        assert sensitive not in logged


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("SMARTPBX_MCP_CONNECT_TIMEOUT_SECONDS", "0"),
        ("SMARTPBX_MCP_CONNECT_TIMEOUT_SECONDS", "31"),
        ("SMARTPBX_MCP_READ_TIMEOUT_SECONDS", "0"),
        ("SMARTPBX_MCP_READ_TIMEOUT_SECONDS", "61"),
        ("SMARTPBX_MCP_RETRY_COUNT", "-1"),
        ("SMARTPBX_MCP_RETRY_COUNT", "3"),
    ],
)
def test_timeouts_and_retry_count_are_bounded(name, value):
    with pytest.raises(ValueError, match="configuration"):
        settings(**{name: value})
