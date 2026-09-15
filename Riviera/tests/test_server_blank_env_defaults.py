"""Blank-string trap contract for server.py module-level env resolvers (P1-4).

A key present but blank -- e.g. an unset compose ``${VAR:-default}``
passthrough -- must resolve exactly like a missing key. Each case here
imports ``server`` fresh in a subprocess (module-level constants, so a
same-process reload would re-run every import-time side effect for every
other test in the session) and asserts the resolved value.
"""
from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _import_server_attr(env_overrides: dict[str, str], attr: str) -> str:
    env = os.environ | env_overrides
    code = f"import server; print(repr(server.{attr}))"
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=PROJECT_ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    # Subprocess prints repr(str) of a trusted, controlled attribute -- parse
    # it as a Python literal rather than evaluating it as code.
    return ast.literal_eval(result.stdout.strip().splitlines()[-1])


def test_blank_stt_provider_falls_back_to_google_like_absent():
    assert _import_server_attr({"STT_PROVIDER": ""}, "STT_PROVIDER") == "google"


def test_blank_llm_provider_falls_back_to_claude_like_absent():
    assert _import_server_attr({"LLM_PROVIDER": ""}, "LLM_PROVIDER") == "claude"


def test_explicit_llm_provider_still_selects_openai():
    assert _import_server_attr({"LLM_PROVIDER": "openai"}, "LLM_PROVIDER") == "openai"


def test_blank_sentry_traces_sample_rate_does_not_crash_when_dsn_is_set():
    """Before the fix, float("") raised at import whenever SENTRY_DSN was set
    and SENTRY_TRACES_SAMPLE_RATE arrived blank -- crash-looping the
    container on every restart."""
    env = os.environ | {
        "SENTRY_DSN": "https://public@example.invalid/0",
        "SENTRY_TRACES_SAMPLE_RATE": "",
    }
    result = subprocess.run(
        [sys.executable, "-c", "import server; print('IMPORT_OK')"],
        cwd=PROJECT_ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert "IMPORT_OK" in result.stdout
