"""Per-WebSocket-session state for BSL agent — stub.

Replaces SLIC's active_session.py. Much simpler: BSL has no cross-call
continuation, so this is just an in-memory dict keyed by Call SID.

Filled in Phase 4.
"""
from typing import Any

_state: dict[str, dict[str, Any]] = {}


def get(call_sid: str) -> dict[str, Any]:
    return _state.setdefault(call_sid, {})


def clear(call_sid: str) -> None:
    _state.pop(call_sid, None)
