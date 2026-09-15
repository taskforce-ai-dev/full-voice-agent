"""Local contract tests for non-secret first-company readiness."""

from __future__ import annotations

import unittest
from os import environ
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from smartpbx_agent_factory.orchestrator import GenerationOrchestrator


ROOT = Path(__file__).parent
FIXTURE = ROOT / "fixtures" / "acme-minimal.json"
CATALOGUE = ROOT / "fixtures" / "approved-provider-catalogue.json"


class FactoryReadinessContractTests(unittest.TestCase):
    def test_inspect_uses_config_bound_operations_status_without_local_docker(self) -> None:
        with TemporaryDirectory() as temporary:
            orchestrator = GenerationOrchestrator(
                Path(temporary),
                catalogue_path=CATALOGUE,
                operations_prerequisites_checker=lambda: "ready",
            )

            with patch.dict(environ, {}, clear=True):
                checks = orchestrator.inspect(FIXTURE)["checks"]

        self.assertEqual(checks["operations_prerequisites"], "ready")
        self.assertNotIn("docker", checks)

    def test_operations_preflight_rejects_duplicate_age_recipients(self) -> None:
        from smartpbx_agent_factory.bootstrap import FactoryBootstrap

        recipient = "age1qqqqqqqqqqqqqqqqqqqqqqqq"
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            recipient_file = root / "recipients.txt"
            recipient_file.write_text(f"{recipient}\n{recipient}\n", encoding="utf-8")
            config = SimpleNamespace(
                lanes={
                    "operations": SimpleNamespace(
                        primary=root,
                        remote="origin",
                        canonical_remote="https://github.com/acme/operations.git",
                    )
                },
                age=SimpleNamespace(
                    recipient_file=recipient_file,
                    approved_recipient_fingerprints=(recipient,),
                    credential_source_policy={"providers/example": object()},
                    sops_binary=Path("/bin/sh"),
                    age_binary=Path("/bin/sh"),
                ),
            )
            (root / ".git").mkdir()
            with patch(
                "smartpbx_agent_factory.bootstrap._run",
                return_value=SimpleNamespace(returncode=0, stdout="https://github.com/acme/operations.git\n"),
            ):
                status = FactoryBootstrap(config).operations_prerequisites_status()

        self.assertEqual(status, "blocked: approved age recipients are invalid")


if __name__ == "__main__":
    unittest.main()
