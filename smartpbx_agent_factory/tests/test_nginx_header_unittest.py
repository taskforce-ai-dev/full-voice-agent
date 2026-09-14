"""Stdlib regression coverage for generated SmartPBX Nginx auth forwarding."""

from __future__ import annotations

from pathlib import Path
import unittest

from smartpbx_agent_factory.provenance import render_template_text
import smartpbx_agent_factory.render as renderer


_TEMPLATE = (
    Path(__file__).parents[1] / "template_v1" / "infrastructure" / "nginx-smartpbx.conf.tmpl"
)


def _render_nginx(header: str, variable: str) -> str:
    return render_template_text(
        _TEMPLATE.read_text(encoding="utf-8"),
        {
            "smartpbx_hostname": "smartpbx-test.invalid",
            "smartpbx_port": 19999,
            "tls_certificate_path": "/run/secrets/test-cert.pem",
            "tls_certificate_key_path": "/run/secrets/test-key.pem",
            "wss_header": header,
            "wss_header_variable": variable,
        },
    )


class NginxHeaderRenderingTests(unittest.TestCase):
    def test_canonical_header_is_forwarded_to_both_authenticated_routes(self) -> None:
        header = "X-SmartPBX-Canonical-CI-Token"
        expected = f"proxy_set_header {header} $http_x_smartpbx_canonical_ci_token;"

        rendered = _render_nginx(header, "$http_x_smartpbx_canonical_ci_token")

        self.assertEqual(rendered.count(expected), 2)

    def test_custom_agent_header_is_forwarded_to_both_authenticated_routes(self) -> None:
        header = "X-Acme-Guide-SmartPBX-Token"
        expected = f"proxy_set_header {header} $http_x_acme_guide_smartpbx_token;"

        rendered = _render_nginx(header, "$http_x_acme_guide_smartpbx_token")

        self.assertEqual(rendered.count(expected), 2)

    def test_renderer_derives_nginx_variable_from_trusted_header(self) -> None:
        derive = getattr(renderer, "_nginx_request_header_variable", None)

        self.assertIsNotNone(derive)
        self.assertEqual(derive("X-Acme-Guide-SmartPBX-Token"), "$http_x_acme_guide_smartpbx_token")
        self.assertEqual(derive("X-SmartPBX-Canonical-CI-Token"), "$http_x_smartpbx_canonical_ci_token")


if __name__ == "__main__":
    unittest.main()
