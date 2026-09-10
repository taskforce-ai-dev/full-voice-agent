"""Executable contracts for the non-secret CLI bootstrap boundary."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


class FactoryBootstrapContractTests(unittest.TestCase):
    def _config(self, root: Path) -> Path:
        payload = {
            "version": 1,
            "state_root": str(root / "state"),
            "catalogue": str(root / "catalogue.json"),
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


if __name__ == "__main__":
    unittest.main()
