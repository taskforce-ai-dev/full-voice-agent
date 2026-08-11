import logging

import pytest

from smartpbx_gateway import SmartPBXGateway, SmartPBXSessionRegistry
from tests.test_smartpbx_gateway import Factory, START, _DtmfFactory, FakeWebSocket, fixed_diagnostics, settings


async def run(
    messages,
    *,
    configuration=None,
    registry=None,
    factory=None,
    token="test-token",
    header="X-Kavya-SmartPBX-Token",
):
    configuration = configuration or settings()
    registry = registry or SmartPBXSessionRegistry(configuration.max_calls)
    gateway = SmartPBXGateway(configuration, registry)
    socket = FakeWebSocket(messages, token=token, header=header)
    factory = factory or Factory()
    await gateway.handle(socket, factory)
    return gateway, registry, socket, factory


@pytest.mark.asyncio
async def test_dtmf_mid_session_routes_to_collector_and_does_not_teardown_session():
    dtmf = {"event": "dtmf", "dtmf": {"callId": "call-1", "otherLegCallId": "other-1", "digit": "7"}}
    factory = _DtmfFactory()
    _, registry, socket, _ = await run([START, dtmf, {"event": "stop"}], factory=factory)

    assert registry.snapshot()["active_sessions"] == 0
    assert socket.close_calls == [(1000, "call ended")]
    assert factory.sessions[0].dtmf_digits == ["7"]


@pytest.mark.asyncio
async def test_unknown_mid_session_message_logs_diagnostic_and_continues(caplog):
    with caplog.at_level(logging.INFO):
        _, _, socket, _ = await run([START, {"event": "future-event"}, {"event": "stop"}])

    tuples = [(row["stage"], row["outcome"], row["failure_class"]) for row in fixed_diagnostics(caplog)]
    assert ("context_validation", "observed", "unsupported_event") in tuples
    assert socket.close_calls == [(1000, "call ended")]


@pytest.mark.asyncio
async def test_start_stage_validation_still_rejects_unknown_prestart_event():
    _, _, socket, factory = await run([{"event": "future-event"}, START, {"event": "stop"}])

    assert socket.close_calls == [(1008, "unsupported event")]
    assert factory.sessions == []
