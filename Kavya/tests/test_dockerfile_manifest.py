"""The Dockerfile COPY manifest must contain every module the app imports.

server.py imports the whole runtime tree, so a module added to the code but not
to the Dockerfile's explicit COPY list ships a broken image that crashes on
`ModuleNotFoundError` at container start (handover.py took prod down this way on
2026-07-31; smartpbx_dtmf.py on the DTMF publish). This asserts the manifest
covers server's real import closure, restricted to Kavya's own modules, so the
same mistake fails the test job before any build.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = PROJECT_ROOT / "Dockerfile"


def _copy_manifest() -> set[str]:
    """The set of .py basenames on the Dockerfile's `COPY server.py ... ./` line."""
    text = DOCKERFILE.read_text(encoding="utf-8")
    # Robust to spacing/module count: the module COPY line starts at server.py
    # and ends with the `./` destination.
    match = re.search(r"^COPY\s+(server\.py\s.*?)\s+\./\s*$", text, flags=re.MULTILINE)
    assert match, "could not find the `COPY server.py ... ./` manifest line in the Dockerfile"
    return set(re.findall(r"\b[\w-]+\.py\b", match.group(1)))


def _server_import_closure() -> list[str]:
    """Kavya-owned .py modules pulled in by importing server (fresh subprocess)."""
    code = (
        "import server, sys, json, pathlib\n"
        "root = pathlib.Path(server.__file__).parent\n"
        "mods = sorted({\n"
        "    pathlib.Path(m.__file__).name\n"
        "    for m in list(sys.modules.values())\n"
        "    if getattr(m, '__file__', None)\n"
        "    and m.__file__.endswith('.py')\n"
        "    and pathlib.Path(m.__file__).parent == root\n"
        "})\n"
        "print(json.dumps(mods))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(PROJECT_ROOT),
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, f"`import server` failed:\n{result.stderr}"
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_dockerfile_copy_manifest_covers_every_imported_module():
    manifest = _copy_manifest()
    required = _server_import_closure()

    assert "server.py" in required, "sanity: server itself must appear in its own closure"
    missing = sorted(m for m in required if m not in manifest)
    assert not missing, (
        "modules imported by server.py but absent from the Dockerfile COPY manifest "
        f"(they would crash the container on import): {missing}"
    )


def test_dockerfile_has_a_build_time_import_guard():
    text = DOCKERFILE.read_text(encoding="utf-8")
    # A build must fail if a copied-but-missing module breaks the import tree.
    assert re.search(r"^RUN\s+python\s+-c\s+[\"']import server[\"']", text, flags=re.MULTILINE), (
        "the Dockerfile must import server at build time so a missing COPY fails the build"
    )
