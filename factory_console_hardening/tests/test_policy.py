"""Contract tests for the non-deployable Factory Console hardening bundle."""

from __future__ import annotations

import json
from pathlib import Path
import unittest

from factory_console_hardening.policy import validate_policy


ROOT = Path(__file__).parents[1]
POLICY_PATH = ROOT / "policy.json"
TEMPLATE_ROOT = ROOT / "templates"


def load_policy() -> dict[str, object]:
    return json.loads(POLICY_PATH.read_text(encoding="utf-8"))


class FactoryConsolePolicyTests(unittest.TestCase):
    def test_review_only_policy_is_accepted(self) -> None:
        self.assertEqual(validate_policy(load_policy()), ())

    def test_non_loopback_origin_is_rejected(self) -> None:
        policy = load_policy()
        policy["origin"]["host"] = "0.0.0.0"

        self.assertIn("origin.host must be a loopback address", validate_policy(policy))

    def test_pr_creation_requires_a_bound_explicit_approval(self) -> None:
        policy = load_policy()
        policy["operations"]["approval_required_actions"].remove("open-pr")

        self.assertIn(
            "operations.approval_required_actions must include open-pr",
            validate_policy(policy),
        )

    def test_policy_permits_the_two_digest_review_actions_exposed_by_the_facade(self) -> None:
        policy = load_policy()

        self.assertIn("approve-knowledge", policy["operations"]["allowed_actions"])
        self.assertIn("approve-plan", policy["operations"]["allowed_actions"])
        self.assertEqual(validate_policy(policy), ())

    def test_deploy_and_provision_can_never_be_allowed(self) -> None:
        policy = load_policy()
        policy["operations"]["allowed_actions"].append("deploy")

        self.assertIn("operations.allowed_actions contains a forbidden action", validate_policy(policy))

    def test_content_or_token_auditing_is_rejected(self) -> None:
        policy = load_policy()
        policy["audit"]["include_request_content"] = True
        policy["audit"]["include_token_claims"] = True

        errors = validate_policy(policy)

        self.assertIn("audit.include_request_content must be false", errors)
        self.assertIn("audit.include_token_claims must be false", errors)

    def test_cloudflared_template_has_access_gate_and_deny_catch_all(self) -> None:
        rendered = (TEMPLATE_ROOT / "cloudflared" / "factory-console.yml").read_text(encoding="utf-8")

        self.assertIn("service: http://127.0.0.1:8400", rendered)
        self.assertIn("required: true", rendered)
        self.assertIn("audTag:", rendered)
        self.assertIn("service: http_status:404", rendered)

    def test_nginx_template_only_listens_on_loopback_and_denies_deploy_routes(self) -> None:
        rendered = (TEMPLATE_ROOT / "nginx" / "factory-console.conf").read_text(encoding="utf-8")

        self.assertIn("listen 127.0.0.1:8400", rendered)
        self.assertIn("client_max_body_size 16k", rendered)
        self.assertIn("Content-Security-Policy", rendered)
        self.assertIn("deploy|provision", rendered)

    def test_nginx_template_serves_the_standalone_ui_and_proxies_only_v1_to_the_facade(self) -> None:
        rendered = (TEMPLATE_ROOT / "nginx" / "factory-console.conf").read_text(encoding="utf-8")

        self.assertIn("root /var/lib/factory-console/ui", rendered)
        self.assertIn("location ^~ /v1/", rendered)
        self.assertIn("proxy_pass http://127.0.0.1:8401", rendered)
        self.assertIn("try_files $uri $uri/ /index.html", rendered)
        self.assertIn('proxy_set_header Cookie "factory_csrf=$cookie_factory_csrf"', rendered)

    def test_console_service_requires_access_verification_before_starting(self) -> None:
        console = (TEMPLATE_ROOT / "systemd" / "factory-console.service").read_text(encoding="utf-8")

        self.assertIn(
            "ExecStartPre=/opt/factory-console/venv/bin/python -m factory_console verify-access-config --config /etc/factory-console/runtime.json",
            console,
        )
        self.assertIn("ExecStart=/opt/factory-console/venv/bin/gunicorn", console)
        self.assertIn("--bind 127.0.0.1:8401", console)
        self.assertIn("factory_console.entrypoint:application", console)
        self.assertNotIn("wsgiref", console)
        self.assertNotIn("EnvironmentFile=", console)

    def test_policy_declares_the_runtime_bounds_used_by_the_wsgi_entrypoint(self) -> None:
        policy = load_policy()

        self.assertEqual(policy["runtime"]["csrf_ttl_seconds"], 300)
        self.assertEqual(policy["runtime"]["jwks_cache_seconds"], 300)
        self.assertEqual(policy["runtime"]["max_cached_jwks"], 16)
        self.assertEqual(validate_policy(policy), ())

    def test_runtime_template_names_only_root_owned_host_local_inputs(self) -> None:
        runtime = json.loads((TEMPLATE_ROOT / "runtime" / "factory-console.json").read_text(encoding="utf-8"))

        self.assertEqual(set(runtime), {"version", "policy_path", "factory_config_path", "csrf_secret_file", "manifests"})
        self.assertEqual(runtime["version"], 1)
        self.assertEqual(runtime["policy_path"], "/etc/factory-console/policy.json")
        self.assertNotIn("secret", json.dumps(runtime["manifests"]).lower())

    def test_systemd_templates_use_dedicated_users_and_restrictive_sandboxes(self) -> None:
        console = (TEMPLATE_ROOT / "systemd" / "factory-console.service").read_text(encoding="utf-8")
        tunnel = (TEMPLATE_ROOT / "systemd" / "cloudflared-factory-console.service").read_text(encoding="utf-8")

        self.assertIn("User=factory-console", console)
        self.assertIn("Group=factory-console", console)
        self.assertIn("root:factory-console 0750", console)
        self.assertIn("root:factory-console 0640", console)
        self.assertIn("UMask=0077", console)
        self.assertIn("ProtectSystem=strict", console)
        self.assertIn("NoNewPrivileges=yes", console)
        self.assertIn("User=cloudflared", tunnel)
        self.assertIn("ProtectSystem=strict", tunnel)
        self.assertIn("NoNewPrivileges=yes", tunnel)

    def test_templates_contain_only_credential_placeholders(self) -> None:
        rendered = "\n".join(path.read_text(encoding="utf-8") for path in TEMPLATE_ROOT.rglob("*") if path.is_file())

        self.assertNotRegex(
            rendered,
            r"(?i)(?:api[_-]?key|password|authorization|token)\s*[:=]\s*[^\s<#][^\s]{7,}",
        )
        self.assertIn("REPLACE_WITH_ACCESS_APPLICATION_AUDIENCE", rendered)


if __name__ == "__main__":
    unittest.main()
