"""Contract tests for bounded SmartPBX configuration and admission."""

import asyncio
import pytest

from smartpbx_gateway import (
    SMARTPBX_PROTOCOL_VERSION,
    SmartPBXSessionRegistry,
    SmartPBXSettings,
    smartpbx_status,
)


def enabled_environment(**overrides):
    environment = {
        "ENABLE_SMARTPBX_WSS": "true",
        "SMARTPBX_WS_TOKEN": "shared-secret",
        "SMARTPBX_ACCOUNT_ID": "account-1",
    }
    environment.update(overrides)
    return environment


def test_smartpbx_is_disabled_and_unconfigured_by_default():
    settings = SmartPBXSettings.from_env({})

    assert settings.enabled is False
    assert settings.configured is False
    assert settings.token_matches("shared-secret") is False
    assert "shared-secret" not in repr(settings)


def test_disabled_smartpbx_can_still_report_valid_configuration():
    settings = SmartPBXSettings.from_env({
        "SMARTPBX_WS_TOKEN": "shared-secret",
        "SMARTPBX_ACCOUNT_ID": "account-1",
    })

    assert settings.enabled is False
    assert settings.configured is True


@pytest.mark.parametrize(
    "environment",
    [
        {"ENABLE_SMARTPBX_WSS": "true", "SMARTPBX_ACCOUNT_ID": "account-1"},
        {"ENABLE_SMARTPBX_WSS": "true", "SMARTPBX_WS_TOKEN": "shared-secret"},
    ],
)
def test_enabled_smartpbx_requires_token_and_account(environment):
    with pytest.raises(ValueError, match="configuration"):
        SmartPBXSettings.from_env(environment)


def test_settings_enforces_documented_integer_bounds_and_authenticates_tokens():
    settings = SmartPBXSettings.from_env(enabled_environment(
        SMARTPBX_MAX_CALLS="4",
        SMARTPBX_MAX_MESSAGE_CHARS="65536",
        SMARTPBX_MAX_AUDIO_BYTES="32768",
        SMARTPBX_MAX_OUTBOUND_FRAMES="128",
        SMARTPBX_START_TIMEOUT_SECONDS="30",
        SMARTPBX_IDLE_TIMEOUT_SECONDS="300",
    ))

    assert settings.max_calls == 4
    assert settings.max_message_chars == 65536
    assert settings.max_audio_bytes == 32768
    assert settings.max_outbound_frames == 128
    assert settings.start_timeout_seconds == 30
    assert settings.idle_timeout_seconds == 300
    assert settings.token_matches("shared-secret") is True
    assert settings.token_matches("wrong-token") is False


@pytest.mark.parametrize(
    "name,value",
    [
        ("SMARTPBX_MAX_CALLS", "0"),
        ("SMARTPBX_MAX_CALLS", "5"),
        ("SMARTPBX_MAX_MESSAGE_CHARS", "1023"),
        ("SMARTPBX_MAX_MESSAGE_CHARS", "65537"),
        ("SMARTPBX_MAX_AUDIO_BYTES", "159"),
        ("SMARTPBX_MAX_AUDIO_BYTES", "32769"),
        ("SMARTPBX_MAX_OUTBOUND_FRAMES", "0"),
        ("SMARTPBX_MAX_OUTBOUND_FRAMES", "129"),
        ("SMARTPBX_START_TIMEOUT_SECONDS", "0"),
        ("SMARTPBX_START_TIMEOUT_SECONDS", "31"),
        ("SMARTPBX_IDLE_TIMEOUT_SECONDS", "9"),
        ("SMARTPBX_IDLE_TIMEOUT_SECONDS", "301"),
    ],
)
def test_settings_rejects_out_of_range_integers(name, value):
    with pytest.raises(ValueError, match="configuration"):
        SmartPBXSettings.from_env(enabled_environment(**{name: value}))


@pytest.mark.parametrize("value", ["", "one", "1.0", "-1"])
def test_settings_rejects_malformed_integers(value):
    with pytest.raises(ValueError, match="configuration"):
        SmartPBXSettings.from_env(enabled_environment(SMARTPBX_MAX_CALLS=value))


@pytest.mark.asyncio
async def test_registry_enforces_four_active_session_leases():
    registry = SmartPBXSessionRegistry(max_sessions=4)

    leases = [await registry.try_acquire() for _ in range(4)]
    rejected = await registry.try_acquire()

    assert all(lease is not None for lease in leases)
    assert rejected is None
    assert registry.snapshot() == {
        "active_sessions": 4,
        "max_sessions": 4,
        "admitted_total": 4,
        "rejected_capacity_total": 1,
        "released_total": 0,
    }


@pytest.mark.asyncio
async def test_releasing_a_lease_admits_another_without_underflow_on_double_release():
    registry = SmartPBXSessionRegistry(max_sessions=1)
    lease = await registry.try_acquire()
    assert lease is not None

    await lease.release()
    replacement = await registry.try_acquire()
    await lease.release()

    assert replacement is not None
    assert registry.snapshot() == {
        "active_sessions": 1,
        "max_sessions": 1,
        "admitted_total": 2,
        "rejected_capacity_total": 0,
        "released_total": 1,
    }


@pytest.mark.asyncio
async def test_cancelled_release_waiting_for_registry_lock_can_be_retried():
    registry = SmartPBXSessionRegistry(max_sessions=1)
    lease = await registry.try_acquire()
    assert lease is not None
    await registry._lock.acquire()
    release_task = asyncio.create_task(lease.release())
    await asyncio.sleep(0)

    release_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await release_task
    registry._lock.release()

    await lease.release()
    replacement = await registry.try_acquire()

    assert replacement is not None
    await replacement.release()
    assert registry.snapshot() == {
        "active_sessions": 0,
        "max_sessions": 1,
        "admitted_total": 2,
        "rejected_capacity_total": 0,
        "released_total": 2,
    }


@pytest.mark.asyncio
async def test_registry_counters_saturate_at_signed_64_bit_maximum():
    registry = SmartPBXSessionRegistry(max_sessions=1)
    lease = await registry.try_acquire()
    assert lease is not None
    maximum = (1 << 63) - 1
    registry._admitted_total = maximum
    registry._rejected_capacity_total = maximum
    registry._released_total = maximum

    assert await registry.try_acquire() is None
    await lease.release()

    assert registry.snapshot() == {
        "active_sessions": 0,
        "max_sessions": 1,
        "admitted_total": maximum,
        "rejected_capacity_total": maximum,
        "released_total": maximum,
    }


@pytest.mark.asyncio
async def test_status_exposes_only_safe_bounded_operational_values():
    settings = SmartPBXSettings.from_env(enabled_environment())
    registry = SmartPBXSessionRegistry(max_sessions=settings.max_calls)
    for _ in range(4):
        assert await registry.try_acquire() is not None
    assert await registry.try_acquire() is None

    status = smartpbx_status(settings, registry)

    assert status == {
        "enabled": True,
        "configured": True,
        "active_sessions": 4,
        "max_sessions": 4,
        "admitted_total": 4,
        "rejected_capacity_total": 1,
        "released_total": 0,
        "protocol_version": SMARTPBX_PROTOCOL_VERSION,
    }
    assert set(status) == {
        "enabled",
        "configured",
        "active_sessions",
        "max_sessions",
        "admitted_total",
        "rejected_capacity_total",
        "released_total",
        "protocol_version",
    }
    assert "shared-secret" not in repr(status)
