"""Small, dependency-free contract tests for the website-demo transport.

These deliberately load the generated runtime template directly: the local
factory checkout does not install runtime-only FastAPI/Twilio dependencies.
"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path


_TEMPLATE = (
    Path(__file__).resolve().parents[1]
    / "template_v1"
    / "runtime"
    / "website_demo_core.py.tmpl"
)


def _load_core():
    spec = importlib.util.spec_from_loader(
        "website_demo_core_contract", SourceFileLoader("website_demo_core_contract", str(_TEMPLATE))
    )
    if spec is None or spec.loader is None:
        raise AssertionError("website demo core template is unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)
    return module


class WebsiteDemoTransportContractTests(unittest.TestCase):
    def test_ticket_is_one_time_agent_and_language_bound(self) -> None:
        core = _load_core()
        tickets = core.SessionTickets(capacity=2, ttl_seconds=30.0, clock=lambda: 100.0)
        ticket = tickets.issue(agent="acme-inquiry", language="si")
        self.assertEqual(tickets.consume(ticket, agent="acme-inquiry", language="si"), "si")
        self.assertIsNone(tickets.consume(ticket, agent="acme-inquiry", language="si"))

    def test_ticket_rejects_cross_agent_or_expired_replay(self) -> None:
        core = _load_core()
        now = [100.0]
        tickets = core.SessionTickets(capacity=1, ttl_seconds=5.0, clock=lambda: now[0])
        ticket = tickets.issue(agent="acme-inquiry", language="en")
        self.assertIsNone(tickets.consume(ticket, agent="other", language="en"))
        now[0] = 106.0
        self.assertIsNone(tickets.consume(ticket, agent="acme-inquiry", language="en"))

    def test_quota_is_per_client_and_bounded(self) -> None:
        core = _load_core()
        now = [50.0]
        quota = core.TokenQuota(limit=2, window_seconds=60.0, capacity=2, clock=lambda: now[0])
        self.assertTrue(quota.admit("203.0.113.1"))
        self.assertTrue(quota.admit("203.0.113.1"))
        self.assertFalse(quota.admit("203.0.113.1"))
        self.assertTrue(quota.admit("203.0.113.2"))
        self.assertLessEqual(quota.client_count, 2)

    def test_twiml_is_text_relay_only_and_escapes_query_values(self) -> None:
        core = _load_core()
        twiml = core.conversation_relay_twiml(
            public_base_url="https://demo.example.test",
            agent="acme-inquiry",
            language="si",
            locale="si-LK",
            ticket="ticket&not-a-param",
            greeting="Hello & welcome",
        )
        self.assertIn('url="wss://demo.example.test/ws/v1/website-demo/conversation?', twiml)
        self.assertIn("agent=acme-inquiry", twiml)
        self.assertIn("ticket=ticket%26not-a-param", twiml)
        self.assertIn('language="si-LK"', twiml)
        self.assertIn("welcomeGreeting=\"Hello &amp; welcome\"", twiml)
        self.assertNotIn("<Dial", twiml)


if __name__ == "__main__":
    unittest.main()
