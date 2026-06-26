#!/usr/bin/env python3
"""WSL-safe `graphify update .` for this repo.

Why this exists
---------------
The repo is edited over the `\\\\wsl.localhost\\...` UNC mount on Windows.
Stock `graphify update .` breaks here for two reasons:

  1. graphify.cache._normalize_path() calls os.path.normcase(), which lowercases
     the path. The WSL 9P mount is CASE-SENSITIVE, so the lowercased path no
     longer exists -> `file_hash requires a file` -> rebuild aborts.
  2. AST extraction uses a ProcessPoolExecutor (spawn). Spawned workers can't
     re-exec from the UNC cwd and crash. Forcing sequential extraction avoids it.

This wrapper monkeypatches both, then runs the normal AST update. It is AST-only
(no LLM / no API cost), same as `graphify update .`.

Build-artifact exclusion (minified admin SPA `asset_*.js`, __MACOSX) is handled
natively by the repo's `.graphifyignore` — do NOT rely on this script for it.

Usage (from repo root, with the Windows store Python that has graphify):
    python ops/graphify-update-wsl.py            # incremental
    python ops/graphify-update-wsl.py --force    # allow node count to drop
"""
import sys
from pathlib import Path

import graphify.cache as gc
import graphify.extract as ge


def _install_patches() -> None:
    # Fix 1: preserve case on WSL UNC mounts (they are case-sensitive).
    _orig_norm = gc._normalize_path

    def _patched_norm(path):
        s = str(path)
        if sys.platform == "win32" and (
            s.lower().startswith("\\\\wsl") or s.lower().startswith("//wsl")
        ):
            if s.startswith("\\\\?\\"):
                s = s[4:]
            return Path(s)
        return _orig_norm(path)

    gc._normalize_path = _patched_norm

    # Fix 2: force sequential extraction (no ProcessPool spawn over UNC cwd).
    _orig_extract = ge.extract

    def _seq_extract(*args, **kwargs):
        kwargs["parallel"] = False
        return _orig_extract(*args, **kwargs)

    ge.extract = _seq_extract


def main() -> int:
    force = "--force" in sys.argv[1:]
    _install_patches()
    from graphify.watch import _rebuild_code

    print("Re-extracting code files in . (WSL-safe, sequential, no LLM)...")
    ok = _rebuild_code(Path("."), force=force)
    print("REBUILD_OK", ok)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
