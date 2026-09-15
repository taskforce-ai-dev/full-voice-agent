"""Minimal preflight CLI for the production WSGI entrypoint."""

from __future__ import annotations

import argparse

from .runtime import RuntimeConfigurationError, verify_runtime


def main() -> int:
    parser = argparse.ArgumentParser(prog="factory-console")
    subcommands = parser.add_subparsers(dest="command", required=True)
    verify = subcommands.add_parser("verify-access-config")
    verify.add_argument("--config", required=True)
    args = parser.parse_args()
    try:
        verify_runtime(args.config)
    except RuntimeConfigurationError:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
