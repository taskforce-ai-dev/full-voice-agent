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
_WEBSITE_TEMPLATE = _TEMPLATE.with_name("website_demo.py.tmpl")
_PROVIDER_TEMPLATE = _TEMPLATE.with_name("provider_builders.py.tmpl")
_WEBSITE_PROXY_TEMPLATE = _TEMPLATE.parents[1] / "infrastructure" / "nginx-website-demo.conf.tmpl"
_WEBSITE_COMPOSE_TEMPLATE = _TEMPLATE.parents[1] / "infrastructure" / "docker-compose.yml.tmpl"
_RUNBOOK_TEMPLATE = _TEMPLATE.parents[1] / "infrastructure" / "SMARTPBX_RUNBOOK.md.tmpl"


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

    def test_issued_browser_identity_is_exact_one_time_and_bounded_by_ttl(self) -> None:
        core = _load_core()
        now = [100.0]
        identities = core.IssuedBrowserIdentities(capacity=2, ttl_seconds=5.0, clock=lambda: now[0])
        identities.issue("demo-0123456789abcdef")
        self.assertTrue(identities.consume("demo-0123456789abcdef"))
        self.assertFalse(identities.consume("demo-0123456789abcdef"))
        self.assertFalse(identities.consume("demo-unknown"))
        identities.issue("demo-expired")
        now[0] = 106.0
        self.assertFalse(identities.consume("demo-expired"))

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

    def test_signed_webhook_consumes_issued_client_identity_before_ticket_issue(self) -> None:
        website = _WEBSITE_TEMPLATE.read_text(encoding="utf-8")
        self.assertIn("identities.issue(identity)", website)
        self.assertIn('caller = form.get("From", "")', website)
        self.assertIn('caller.startswith("client:")', website)
        self.assertIn("identities.consume(caller.removeprefix(\"client:\"))", website)
        self.assertLess(
            website.index("identities.consume(caller.removeprefix(\"client:\"))"),
            website.index("tickets.issue(agent=settings.agent_id, language=language_code)"),
        )

    def test_current_user_is_supplied_once_to_provider_history(self) -> None:
        website = _WEBSITE_TEMPLATE.read_text(encoding="utf-8")
        provider = _PROVIDER_TEMPLATE.read_text(encoding="utf-8")
        self.assertEqual(website.count('self._history.append(("user", text))'), 1)
        self.assertIn(
            "stream_with_history(transcript, self._language.code, prompt, tuple(self._history))",
            website,
        )
        history_method = provider.split("async def stream_response_with_history(", 1)[1].split(
            "async def close", 1
        )[0]
        self.assertIn("for role, content in history", history_method)
        self.assertNotIn('(("user", transcript),)', history_method)

    def test_relay_ticket_is_not_written_to_nginx_access_logs(self) -> None:
        proxy = _WEBSITE_PROXY_TEMPLATE.read_text(encoding="utf-8")
        compose = _WEBSITE_COMPOSE_TEMPLATE.read_text(encoding="utf-8")
        runbook = _RUNBOOK_TEMPLATE.read_text(encoding="utf-8")
        location = proxy.split("location = /ws/v1/website-demo/conversation {", 1)[1].split("\n    }", 1)[0]
        service = compose.split("  {{website_service}}:", 1)[1].split("\nnetworks:", 1)[0]
        self.assertIn("access_log off;", location)
        self.assertIn('"--no-access-log"', service)
        self.assertIn("http://127.0.0.1:8081/health", service)
        self.assertIn("relay ticket", runbook)
        self.assertIn("access_log off", runbook)
        self.assertIn("--no-access-log", runbook)

    def test_smartpbx_and_website_profiles_remain_independently_opt_in(self) -> None:
        compose = _WEBSITE_COMPOSE_TEMPLATE.read_text(encoding="utf-8")
        smartpbx = compose.split("  {{smartpbx_service}}:", 1)[1].split("  {{website_service}}:", 1)[0]
        website = compose.split("  {{website_service}}:", 1)[1].split("\nnetworks:", 1)[0]

        self.assertIn('profiles: ["smartpbx"]', smartpbx)
        self.assertIn('profiles: ["website-demo"]', website)


if __name__ == "__main__":
    unittest.main()
