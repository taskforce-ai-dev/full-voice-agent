"""Fail-closed contracts for Kavya's Dialog MCP handover boundary."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass

import httpx
import pytest

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
