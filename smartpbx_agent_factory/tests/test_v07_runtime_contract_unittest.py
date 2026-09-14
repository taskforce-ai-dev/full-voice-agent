"""Static V07 parity checks for the review-only generated runtime candidate."""

from __future__ import annotations

import json
import unittest
from pathlib import Path


_ROOT = Path(__file__).parents[1]
_TEMPLATE_ROOT = _ROOT / "template_v1"
_V07_SOURCE_REVISION = "0f83ac662a34657f06d490a46dc1b773bfe021e7"


class V07RuntimeContractTests(unittest.TestCase):
    def test_gateway_status_exports_the_v07_compatibility_marker(self) -> None:
        gateway = (_TEMPLATE_ROOT / "runtime" / "smartpbx_gateway.py.tmpl").read_text(encoding="utf-8")

        self.assertIn('SMARTPBX_PROTOCOL_VERSION = "smartpbx-ai-provider-v07"', gateway)
        self.assertIn('"protocol_version": SMARTPBX_PROTOCOL_VERSION', gateway)

    def test_candidate_provenance_records_v07_source_but_stays_partial(self) -> None:
        candidate = json.loads((_TEMPLATE_ROOT / "candidate_runtime_provenance.json").read_text(encoding="utf-8"))

        self.assertEqual(candidate["status"], "partial-candidate-not-approved-for-rendering")
        self.assertEqual(candidate["source_revision"], _V07_SOURCE_REVISION)
        self.assertEqual(candidate["protocol_version"], "smartpbx-ai-provider-v07")

    def test_generated_review_docs_preserve_the_v07_carrier_contract(self) -> None:
        runbook = (_TEMPLATE_ROOT / "infrastructure" / "SMARTPBX_RUNBOOK.md.tmpl").read_text(encoding="utf-8")
        client_connect = (_TEMPLATE_ROOT / "infrastructure" / "CLIENT_CONNECT.md.tmpl").read_text(encoding="utf-8")
        protocol = (_TEMPLATE_ROOT / "runtime" / "smartpbx_protocol.py.tmpl").read_text(encoding="utf-8")

        self.assertIn("smartpbx-ai-provider-v07", runbook)
        self.assertIn("destination number", runbook)
        self.assertIn("tier=BYPASS", runbook)
        self.assertIn("g711_ulaw", runbook)
        self.assertIn("8000", runbook)
        self.assertIn("/ws/v1/smartpbx/media", client_connect)
        self.assertIn("{{wss_header}}", client_connect)
        self.assertIn('encoding != "g711_ulaw" or sample_rate != 8000', protocol)


if __name__ == "__main__":
    unittest.main()
