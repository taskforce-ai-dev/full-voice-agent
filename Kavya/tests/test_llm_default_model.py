"""Pin the default Claude model ID so a retired snapshot can never silently ship again.

`server.py`'s `CLAUDE_MODEL` env-default previously pinned `claude-sonnet-4-20250514`.
That snapshot is retired and returns 404 `{"type":"not_found_error"}` from the live
Anthropic API — any deployment without a `CLAUDE_MODEL` env override (e.g. a fresh
`kavya-smartpbx` container using `.env.smartpbx`) fails every LLM turn.

The fix pins the default to `claude-sonnet-4-5-20250929` — the verified drop-in
replacement with an identical request surface (sampling params still accepted,
thinking off by default). A Sonnet 5 jump is deliberately NOT the hotfix: it would
change thinking defaults and reject non-default sampling params, which matters for
voice latency.

Read the source text directly (matching the style of the other static-contract
tests in this suite, e.g. test_smartpbx_deployment.py / test_prompt_policy.py)
rather than importing+reloading `server` — reloading the module re-executes
process-wide setup (logging, optional client imports, KB init) that other test
modules in the same session depend on staying stable.
"""

from __future__ import annotations

from pathlib import Path

KAVYA = Path(__file__).resolve().parents[1]
SERVER_SOURCE = (KAVYA / "server.py").read_text(encoding="utf-8")

RETIRED_MODEL = "claude-sonnet-4-20250514"
CURRENT_DEFAULT_MODEL = "claude-sonnet-4-5-20250929"


def test_claude_model_default_is_the_verified_non_retired_snapshot():
    assert (
        f'CLAUDE_MODEL: str = os.getenv("CLAUDE_MODEL", "{CURRENT_DEFAULT_MODEL}")'
        in SERVER_SOURCE
    )


def test_claude_model_default_no_longer_pins_the_retired_snapshot():
    assert RETIRED_MODEL not in SERVER_SOURCE
