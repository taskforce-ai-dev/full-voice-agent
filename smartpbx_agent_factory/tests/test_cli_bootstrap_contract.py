"""Executable contracts for the non-secret CLI bootstrap boundary."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


class FactoryBootstrapContractTests(unittest.TestCase):
    def _config(self, root: Path) -> Path:
        approved_root = root / "approved-knowledge"
        approved_root.mkdir()
        payload = {
            "version": 1,
            "state_root": str(root / "state"),
            "catalogue": str(root / "catalogue.json"),
            "approved_source_roots": [str(approved_root)],
            "age": {
                "recipient_file": str(root / "age-recipients.txt"),
                "approved_recipient_fingerprints": ["age1qqqqqqqqqqqqqqqqqqqqqqqq"],
                "recipient_review_source": "security-review-2026-09-10",
                "credential_source_policy": {
                    "providers/google_application_credentials": {
                        "provider": "environment",
                        "path": "GOOGLE_APPLICATION_CREDENTIALS_JSON",
                        "rotation_owner": "security",
                    }
                },
                "sops_binary": "/usr/bin/sops",
                "age_binary": "/usr/bin/age",
            },
            "ci": {"repository": "acme/factory-ci", "workflow": "smartpbx-generated-ci", "gh_binary": "/usr/bin/gh"},
            "lanes": {
                role: {
                    "primary": str(root / f"{role}-primary"),
                    "remote": "origin",
                    "canonical_remote": f"https://github.com/acme/{role}.git",
                    "revision": "a" * 40,
                    "target_root": str(root / "worktrees" / role),
                    "repository": f"acme/{role}",
                    "base_branch": "main",
                    "ci_check": f"{role}-gate",
                    "ci_policy": "lifecycle-attestation" if role == "backend" else ("secret-static" if role == "operations" else "website-build"),
                }
                for role in ("backend", "operations", "website")
            },
        }
        path = root / "factory.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_config_requires_exact_non_secret_three_lane_inputs(self) -> None:
        from smartpbx_agent_factory.bootstrap import FactoryConfig, FactoryConfigError

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = FactoryConfig.load(self._config(root))
            self.assertEqual(config.lanes["backend"].revision, "a" * 40)
            self.assertEqual(config.lanes["operations"].repository, "acme/operations")
            payload = json.loads((root / "factory.json").read_text(encoding="utf-8"))
            payload["lanes"]["website"]["revision"] = "HEAD"
            (root / "factory.json").write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(FactoryConfigError):
                FactoryConfig.load(root / "factory.json")

    def test_config_requires_and_persists_explicit_approved_source_roots(self) -> None:
        from smartpbx_agent_factory.bootstrap import FactoryConfig, FactoryConfigError

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_path = self._config(root)
            payload = json.loads(config_path.read_text(encoding="utf-8"))
            payload.pop("approved_source_roots")
            config_path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(FactoryConfigError):
                FactoryConfig.load(config_path)

            approved_root = root / "approved-knowledge"
            payload["approved_source_roots"] = [str(approved_root)]
            config_path.write_text(json.dumps(payload), encoding="utf-8")
            config = FactoryConfig.load(config_path)
            self.assertEqual(config.approved_source_roots, (approved_root.resolve(),))

    def test_new_uses_the_same_config_handoff_as_later_operations(self) -> None:
        from smartpbx_agent_factory.cli import _parser

        parser = _parser()
        arguments = parser.parse_args([
            "new", "--output", "/tmp/manifest.json", "--config", "/tmp/factory.json",
        ])
        self.assertEqual(arguments.config, Path("/tmp/factory.json"))

    def test_config_rejects_secret_keys_and_unknown_fields(self) -> None:
        from smartpbx_agent_factory.bootstrap import FactoryConfig, FactoryConfigError

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            payload = json.loads(self._config(root).read_text(encoding="utf-8"))
            payload["github_token"] = "must-not-be-configured"
            (root / "factory.json").write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(FactoryConfigError):
                FactoryConfig.load(root / "factory.json")

    def test_cli_requires_bootstrap_config_for_stateful_commands(self) -> None:
        from smartpbx_agent_factory.cli import invoke_cli

        result = invoke_cli(["generate", "gen-example"])
        self.assertEqual(result.exit_code, 2)
        self.assertIn("--config", result.stderr)

    def test_bootstrap_constructs_exact_three_lane_binding(self) -> None:
        from smartpbx_agent_factory.bootstrap import FactoryBootstrap, FactoryConfig

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            binding = FactoryBootstrap(FactoryConfig.load(self._config(root))).binding_for("gen-" + "a" * 32)
            self.assertEqual(binding.backend.revision, "a" * 40)
            self.assertEqual(binding.operations.target, root / "worktrees" / "operations" / ("gen-" + "a" * 32))
            self.assertEqual(binding.website.remote, "origin")

    def test_inspect_is_read_only_and_rejects_target_collision(self) -> None:
        from smartpbx_agent_factory.bootstrap import FactoryConfig, inspect_config

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = FactoryConfig.load(self._config(root))
            for lane in config.lanes.values():
                lane.primary.mkdir()
                (lane.primary / ".git").mkdir()
                lane.target_root.mkdir(parents=True)
            seen: list[tuple[str, ...]] = []
            def runner(argv):
                seen.append(tuple(argv))
                command = tuple(argv[-2:])
                stdout = ""
                if "config" in argv:
                    role = next(role for role, lane in config.lanes.items() if str(lane.primary) in argv)
                    stdout = config.lanes[role].canonical_remote + "\n"
                elif "rev-parse" in argv:
                    stdout = "a" * 40 + "\n"
                return SimpleNamespace(returncode=0, stdout=stdout)
            checks = inspect_config(config, runner=runner)
            self.assertTrue(all(value == "ready" for key, value in checks.items() if key.endswith(".repository")))
            self.assertFalse(any(any(word in command for word in ("fetch", "reset", "clean", "checkout")) for command in seen))
            def collision_runner(argv):
                result = runner(argv)
                if "worktree" in argv and str(config.lanes["backend"].primary) in argv:
                    return SimpleNamespace(returncode=0, stdout=f"worktree {config.lanes['backend'].target_root}\n")
                return result
            self.assertEqual(inspect_config(config, runner=collision_runner)["backend.repository"], "blocked: target root collision")

    def test_generated_branch_push_rejects_non_factory_branch_before_runner(self) -> None:
        from smartpbx_agent_factory.bootstrap import FactoryConfig, GitHubCommandAdapter
        from smartpbx_agent_factory.orchestrator import GenerationBlockedError

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = FactoryConfig.load(self._config(root))
            adapter = GitHubCommandAdapter(config, runner=lambda argv: self.fail("runner must not be called"))
            with self.assertRaises(GenerationBlockedError):
                adapter.push_generated_branch(role="backend", path=config.lanes["backend"].target_root / "gen-test", remote="origin", branch="main")

    def test_pr_recovery_adapter_reads_exact_remote_head_and_all_pr_states_without_network(self) -> None:
        from smartpbx_agent_factory.bootstrap import FactoryConfig, GitHubCommandAdapter

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = FactoryConfig.load(self._config(root))
            calls: list[tuple[str, ...]] = []

            def runner(argv):
                calls.append(tuple(argv))
                if "api" in argv:
                    return SimpleNamespace(returncode=0, stdout="b" * 40 + "\n")
                if "list" in argv:
                    return SimpleNamespace(returncode=0, stdout=json.dumps([{
                        "url": "https://github.com/acme/backend/pull/42",
                        "state": "OPEN",
                        "headRefName": "smartpbx-agent-factory/gen-001",
                        "headRefOid": "b" * 40,
                        "baseRefName": "main",
                    }]))
                self.fail(f"unexpected command: {argv}")

            adapter = GitHubCommandAdapter(config, runner=runner)
            branch = "smartpbx-agent-factory/gen-001"
            self.assertEqual(adapter.remote_branch_head(repository="acme/backend", branch=branch), "b" * 40)
            pulls = adapter.find_pull_requests(repository="acme/backend", branch=branch)

            self.assertEqual(len(pulls), 1)
            self.assertEqual(pulls[0].head_sha, "b" * 40)
            self.assertTrue(any("--state" in call and "all" in call for call in calls))
            self.assertTrue(any("smartpbx-agent-factory%2Fgen-001" in argument for call in calls for argument in call))

    def test_ci_adapter_stays_blocked_without_approved_provenance_or_result(self) -> None:
        from smartpbx_agent_factory.bootstrap import FactoryConfig, GitHubCIResultAdapter
        from smartpbx_agent_factory.orchestrator import GenerationBlockedError

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = FactoryConfig.load(self._config(root))
            adapter = GitHubCIResultAdapter(config, runner=lambda argv: self.fail("CI must not be queried before provenance is approved"))
            resources = SimpleNamespace(slug="acme-inquiry", ci_identifier="acme-inquiry", wss_url="wss://smartpbx-acme-inquiry.taskforceai.tech/smartpbx", smartpbx_hostname="smartpbx-acme-inquiry.taskforceai.tech")
            records = {role: {"head_sha": "a" * 40, "artifact_digest": "b" * 64} for role in ("backend", "operations", "website")}
            with self.assertRaises(GenerationBlockedError):
                adapter.verify(generation_id="gen-" + "a" * 32, resources=resources, lane_records=records)

    def test_wizard_writes_private_non_secret_review_manifest(self) -> None:
        from smartpbx_agent_factory.cli import create_manifest_wizard
        from smartpbx_agent_factory.tests.test_wizard_unittest import ManifestWizardTests

        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "manifest.json"
            answers = ManifestWizardTests()._answers(output.parent)
            create_manifest_wizard(output, input_fn=lambda prompt: next(answers), approved_source_roots=(output.parent,))
            self.assertEqual(oct(output.stat().st_mode & 0o777), "0o600")
            self.assertEqual(json.loads(output.read_text())["slug"], "acme-inquiry")


if __name__ == "__main__":
    unittest.main()
