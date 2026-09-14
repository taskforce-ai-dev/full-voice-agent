"""Dependency-free regression coverage for lifecycle status observations."""

from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path

from scripts import run_smartpbx_ci_lifecycle as lifecycle


ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = ROOT / "smartpbx_agent_factory" / "template_v1" / "runtime" / "server.py.tmpl"
CANONICAL = ROOT / "SmartPBX Agents" / ".ci-lifecycle-canonical" / "server.py"
CANDIDATE_PROVENANCE = ROOT / "smartpbx_agent_factory" / "template_v1" / "candidate_runtime_provenance.json"


class StatusContractTests(unittest.TestCase):
    def test_status_response_accepts_protocol_version_and_canonical_matches_template(self) -> None:
        template = TEMPLATE.read_text(encoding="utf-8")
        canonical = CANONICAL.read_text(encoding="utf-8")

        self.assertIn("def status(request: Request) -> dict[str, bool | int | str]:", template)
        self.assertEqual(canonical, template)

    def test_status_template_provenance_digest_matches_the_exact_template(self) -> None:
        document = json.loads(CANDIDATE_PROVENANCE.read_text(encoding="utf-8"))
        component = next(
            item for item in document["components"]
            if item["template_path"] == "runtime/server.py.tmpl"
        )

        digest = "sha256:" + hashlib.sha256(TEMPLATE.read_bytes()).hexdigest()
        self.assertEqual(component["template_sha256"], digest)

    def test_status_snapshot_exposes_only_the_numeric_http_status_on_failure(self) -> None:
        token = "test-token-must-not-appear"
        body = b'{"detail":"Bearer sensitive-body-must-not-appear"}'
        original_http_request = lifecycle.http_request
        lifecycle.http_request = lambda _url, _headers: (500, body)
        try:
            with self.assertRaises(lifecycle.LifecycleError) as raised:
                lifecycle.status_snapshot("http://127.0.0.1:19999", "X-Test-Token", token)
        finally:
            lifecycle.http_request = original_http_request

        message = str(raised.exception)
        self.assertEqual(message, "authenticated status returned HTTP 500")
        self.assertNotIn(token, message)
        self.assertNotIn(body.decode("utf-8"), message)


if __name__ == "__main__":
    unittest.main()
