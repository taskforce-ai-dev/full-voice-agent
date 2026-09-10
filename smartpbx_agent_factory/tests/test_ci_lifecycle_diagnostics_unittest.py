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

    def test_port_discovery_operations_are_closed_to_structured_inspection(self) -> None:
        self.assertIn("container-state-inspect", self.runner.DOCKER_OPERATION_LABELS)
        self.assertIn("port-inspect", self.runner.DOCKER_OPERATION_LABELS)
        self.assertNotIn("port-discover", self.runner.DOCKER_OPERATION_LABELS)

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

    def test_image_build_subphase_classifier_uses_the_latest_known_marker(self) -> None:
        cumulative = (
            b"RUN pip install --no-cache-dir -r requirements-prod.lock.txt\n"
            b'RUN python -c "import startup"\n'
            b'RUN python -c "import website_demo"'
        )
        self.assertEqual(self.runner.classify_image_build_subphase(cumulative, b""), "website-import")
        repeated = b'RUN python -c "import website_demo"\nRUN python -c "import startup"'
        self.assertEqual(self.runner.classify_image_build_subphase(repeated, b""), "smartpbx-import")

    def test_cleanup_failure_remains_allowed_and_never_returns_raw_output(self) -> None:
        result = subprocess.CompletedProcess(
            ["docker"], 1, stdout=b"cleanup " + b"x" * 1024, stderr=b"cleanup failure"
        )
        with patch.object(self.runner.subprocess, "run", return_value=result):
            self.assertEqual(
                self.runner.command("container-cleanup", ["docker", "rm", "unowned-value"], allow_failure=True),
                "",
            )

    def test_container_state_accepts_only_running_with_a_bounded_exit_code(self) -> None:
        self.assertEqual(self.runner._container_state_from_inspect("running 0\n"), ("running", 0))
        for raw in ("exited 17\n", "created 0\n", "paused 0\n", "restarting 1\n", "removing 0\n", "dead 137\n"):
            with self.subTest(raw=raw):
                with self.assertRaisesRegex(self.runner.LifecycleError, r"container is not running state=.* exit_code=[0-9]+") as raised:
                    self.runner._container_state_from_inspect(raw)
                self.assertNotIn(raw.strip(), str(raised.exception))

    def test_container_state_rejects_unknown_or_unbounded_shapes_without_echoing_them(self) -> None:
        for raw in ("unknown 0\n", "running nope\n", "running -1\n", "running 9223372036854775808\n", "running 0 extra\n", "\n"):
            with self.subTest(raw=raw):
                with self.assertRaisesRegex(self.runner.LifecycleError, "container state inspection was invalid") as raised:
                    self.runner._container_state_from_inspect(raw)
                if raw.strip():
                    self.assertNotIn(raw.strip(), str(raised.exception))

    def test_port_binding_accepts_only_exact_single_loopback_mapping(self) -> None:
        self.assertEqual(
            self.runner._mapped_port_from_inspect('{"8000/tcp":[{"HostIp":"127.0.0.1","HostPort":"49152"}]}'),
            49152,
        )
        self.assertEqual(
            self.runner._mapped_port_from_inspect('{"8000/tcp":[{"HostIp":"::1","HostPort":"65535"}]}'),
            65535,
        )
        self.assertEqual(
            self.runner._mapped_port_from_inspect('{"8000/tcp":[{"HostIp":"127.0.0.1","HostPort":"49152"}],"8081/tcp":null}'),
            49152,
        )

    def test_port_binding_rejects_every_unsafe_or_malformed_shape_without_echoing_it(self) -> None:
        cases = (
            "not-json",
            "[]",
            "{}",
            '{"8000/tcp":null}',
            '{"8000/tcp":[]}',
            '{"8000/tcp":[{"HostIp":"127.0.0.1","HostPort":"49152"},{"HostIp":"127.0.0.1","HostPort":"49153"}]}',
            '{"8000/udp":[{"HostIp":"127.0.0.1","HostPort":"49152"}]}',
            '{"8000/tcp":[null]}',
            '{"8000/tcp":[{"HostIp":127,"HostPort":"49152"}]}',
            '{"8000/tcp":[{"HostPort":"49152"}]}',
            '{"8000/tcp":[{"HostIp":"0.0.0.0","HostPort":"49152"}]}',
            '{"8000/tcp":[{"HostIp":"localhost","HostPort":"49152"}]}',
            '{"8000/tcp":[{"HostIp":"127.0.0.1","HostPort":49152}]}',
            '{"8000/tcp":[{"HostIp":"127.0.0.1","HostPort":"nope"}]}',
            '{"8000/tcp":[{"HostIp":"127.0.0.1","HostPort":"0"}]}',
            '{"8000/tcp":[{"HostIp":"127.0.0.1","HostPort":"65536"}]}',
        )
        for raw in cases:
            with self.subTest(raw=raw):
                with self.assertRaisesRegex(self.runner.LifecycleError, "container port binding was invalid") as raised:
                    self.runner._mapped_port_from_inspect(raw)
                self.assertNotIn(raw, str(raised.exception))

    def test_mapped_port_uses_only_state_and_structured_port_inspection(self) -> None:
        outputs = iter(("running 0\n", '{"8000/tcp":[{"HostIp":"127.0.0.1","HostPort":"49152"}]}'))
        with patch.object(self.runner, "command", side_effect=lambda operation, argv, **_kwargs: next(outputs)) as command:
            self.assertEqual(self.runner.mapped_port("owned-container"), 49152)
        self.assertEqual(
            [call.args[0] for call in command.call_args_list],
            ["container-state-inspect", "port-inspect"],
        )
        self.assertEqual(command.call_args_list[0].args[1], ["docker", "container", "inspect", "--format", "{{.State.Status}} {{.State.ExitCode}}", "owned-container"])
        self.assertEqual(command.call_args_list[1].args[1], ["docker", "container", "inspect", "--format", "{{json .NetworkSettings.Ports}}", "owned-container"])


if __name__ == "__main__":
    unittest.main()
