"""Stdlib regression coverage for redacted CI lifecycle Docker diagnostics."""

from __future__ import annotations

import hashlib
import importlib.util
import subprocess
import sys
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest.mock import patch


_RUNNER = Path(__file__).resolve().parents[2] / "scripts" / "run_smartpbx_ci_lifecycle.py"


def _load_runner():
    spec = importlib.util.spec_from_loader(
        "ci_lifecycle_diagnostics_contract",
        SourceFileLoader("ci_lifecycle_diagnostics_contract", str(_RUNNER)),
    )
    if spec is None or spec.loader is None:
        raise AssertionError("CI lifecycle runner is unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)
    return module


class LifecycleDockerDiagnosticsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runner = _load_runner()

    def test_failed_image_build_reports_only_bounded_metadata_and_no_secret_or_argv(self) -> None:
        secret = "super-secret-build-token"
        stdout = f"build output {secret}".encode("utf-8")
        stderr = b"#17 [website-import] RUN python -c \"import website_demo\""
        result = subprocess.CompletedProcess(["docker"], 17, stdout=stdout, stderr=stderr)

        with patch.object(self.runner.subprocess, "run", return_value=result):
            with self.assertRaises(self.runner.LifecycleError) as raised:
                self.runner.command("image-build", ["docker", "build", "--build-arg", secret])

        message = str(raised.exception)
        self.assertIn("operation=image-build", message)
        self.assertIn("exit_code=17", message)
        self.assertIn(f"stdout_bytes={len(stdout)}", message)
        self.assertIn(f"stderr_bytes={len(stderr)}", message)
        self.assertIn(f"stdout_sha256={hashlib.sha256(stdout).hexdigest()}", message)
        self.assertIn(f"stderr_sha256={hashlib.sha256(stderr).hexdigest()}", message)
        self.assertIn("build_subphase=website-import", message)
        self.assertNotIn(secret, message)
        self.assertNotIn("--build-arg", message)
        self.assertLess(len(message), 512)

    def test_invalid_operation_is_rejected_before_any_subprocess(self) -> None:
        with patch.object(self.runner.subprocess, "run") as execute:
            with self.assertRaisesRegex(self.runner.LifecycleError, "invalid Docker operation label"):
                self.runner.command("unbounded-operation", ["docker", "version"])
        execute.assert_not_called()

    def test_image_build_subphase_classifier_uses_only_the_fixed_vocabulary(self) -> None:
        cases = (
            (b"RUN pip install --no-cache-dir -r requirements-prod.lock.txt", "dependency-install"),
            (b'RUN python -c "import startup"', "smartpbx-import"),
            (b'RUN python -c "import website_demo"', "website-import"),
            (b"untrusted arbitrary build output", "unknown"),
        )
        for output, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(self.runner.classify_image_build_subphase(output, b""), expected)
        self.assertEqual(
            self.runner.IMAGE_BUILD_SUBPHASES,
            frozenset({"dependency-install", "smartpbx-import", "website-import", "unknown"}),
        )

    def test_cleanup_failure_remains_allowed_and_never_returns_raw_output(self) -> None:
        result = subprocess.CompletedProcess(
            ["docker"], 1, stdout=b"cleanup " + b"x" * 1024, stderr=b"cleanup failure"
        )
        with patch.object(self.runner.subprocess, "run", return_value=result):
            self.assertEqual(
                self.runner.command("container-cleanup", ["docker", "rm", "unowned-value"], allow_failure=True),
                "",
            )


if __name__ == "__main__":
    unittest.main()
