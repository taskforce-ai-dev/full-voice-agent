"""Focused contract tests for the non-secret manifest wizard.

These stay ``unittest``-native so they can run without the broader pytest suite.
"""

from __future__ import annotations

import json
import stat
import tempfile
import unittest
from pathlib import Path

from smartpbx_agent_factory.catalogue import CapabilityCatalogue
from smartpbx_agent_factory.cli import create_manifest_wizard, invoke_cli
from smartpbx_agent_factory.schema import parse_manifest


ROOT = Path(__file__).parents[1]
CATALOGUE = ROOT / "template_v1" / "provider_catalogue.json"


class ManifestWizardTests(unittest.TestCase):
    def _answers(self, knowledge_root: Path) -> iter[str]:
        return iter(
            (
                "Acme Inquiry",
                "Acme Inquiry",
                "acme-inquiry",
                "Acme Guide",
                "general information",
                "Answer approved company questions",
                "prospective customers",
                "demo",
                "UTC",
                "mon-fri=09:00-17:00",
                "owner@example.invalid",
                "en,si",
                # English: Azure + Claude + ElevenLabs; no fallback.
                "1", "1", "1", "1", "",
                # Sinhala: Azure + Gemini + Rime + required Claude fallback.
                "1", "1", "1", "2", "1", "",
                "company information",
                "account changes",
                "no", "no", "no", "",
                # Every optional capability is explicitly disabled.
                "no", "no", "no", "no", "no", "no", "no", "no",
                str(knowledge_root / "faq.txt"),
                "Acme Inquiry",
                "2026-09-10",
                "public",
                "account-fixture",
                "1",
                "owner@example.invalid",
                "support@example.invalid",
                "yes",
            )
        )

    def test_wizard_selects_catalogue_pipelines_writes_flat_parseable_manifest_atomically(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "manifest.json"
            answers = self._answers(root)

            path = create_manifest_wizard(
                output,
                input_fn=lambda _label: next(answers),
                approved_source_roots=(root,),
            )

            self.assertEqual(path, output)
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
            raw = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(raw["languages"][0]["stt"], "azure")
            self.assertEqual(raw["languages"][0]["llm"], "claude")
            self.assertEqual(raw["languages"][0]["tts"], "elevenlabs")
            self.assertIsInstance(raw["languages"][0]["stt"], str)
            self.assertEqual(raw["languages"][1]["llm"], "gemini")
            self.assertEqual(raw["languages"][1]["tts"], "rime")
            self.assertEqual(raw["languages"][1]["fallback"], "claude")
            self.assertIn("ආයුබෝවන්", raw["languages"][1]["greeting"])
            manifest = parse_manifest(
                raw,
                approved_source_roots=(root,),
                catalogue=CapabilityCatalogue.load(CATALOGUE),
            )
            self.assertEqual(tuple(language.code for language in manifest.languages), ("en", "si"))

    def test_wizard_rejects_invalid_catalogue_choice_without_leaving_a_manifest(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "manifest.json"
            answers = self._answers(root)
            values = list(answers)
            # The first English STT selection is immediately after the locale.
            values[13] = "99"
            invalid = iter(values)

            with self.assertRaisesRegex(ValueError, "invalid selection"):
                create_manifest_wizard(
                    output,
                    input_fn=lambda _label: next(invalid),
                    approved_source_roots=(root,),
                )

            self.assertFalse(output.exists())
            self.assertEqual(list(root.glob(".manifest.json.*.tmp")), [])

    def test_new_command_requires_an_explicit_approved_source_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = invoke_cli(["new", "--output", str(Path(temporary) / "manifest.json")])

            self.assertEqual(result.exit_code, 2)

    def test_wizard_parses_serialized_manifest_before_creating_the_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "manifest.json"
            values = list(self._answers(root))
            # Name collection requires explicit consent; keep consent at "no".
            values[27] = "yes"
            invalid = iter(values)

            with self.assertRaisesRegex(ValueError, "PII collection requires"):
                create_manifest_wizard(
                    output,
                    input_fn=lambda _label: next(invalid),
                    approved_source_roots=(root,),
                )

            self.assertFalse(output.exists())
            self.assertEqual(list(root.glob(".manifest.json.*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
