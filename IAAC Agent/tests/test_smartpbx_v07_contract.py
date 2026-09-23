"""External SmartPBX status must advertise the current V07 contract."""

from __future__ import annotations

from smartpbx_gateway import (
    SMARTPBX_PROTOCOL_VERSION,
    SmartPBXSessionRegistry,
    SmartPBXSettings,
    smartpbx_status,
)


def test_status_exports_v07_protocol_marker():
    settings = SmartPBXSettings.from_env(
        {
            "ENABLE_SMARTPBX_WSS": "true",
            "SMARTPBX_WS_TOKEN": "test-token",
            "SMARTPBX_ACCOUNT_ID": "test-account",
            "SMARTPBX_AUTH_HEADER_NAME": "X-IAAC-SmartPBX-Token",
        }
    )

    assert SMARTPBX_PROTOCOL_VERSION == "smartpbx-ai-provider-v07"
    assert smartpbx_status(settings, SmartPBXSessionRegistry(4))["protocol_version"] == "smartpbx-ai-provider-v07"

