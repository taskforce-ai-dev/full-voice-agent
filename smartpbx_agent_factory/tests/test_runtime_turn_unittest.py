"""Stdlib regression coverage for generated conversation-turn lifecycle."""

from __future__ import annotations

import asyncio
import importlib
import sys
import tempfile
import unittest
from pathlib import Path


_RUNTIME = Path(__file__).parents[1] / "template_v1" / "runtime"


class _Adapter:
    active = True


class TurnDrainTests(unittest.IsolatedAsyncioTestCase):
    async def test_drain_does_not_wait_for_the_silence_reprompt_timer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for module_name in ("product_profile", "provider_adapters", "turn_engine"):
                (root / f"{module_name}.py").write_text(
                    (_RUNTIME / f"{module_name}.py.tmpl").read_text(encoding="utf-8"),
                    encoding="utf-8",
                )
            sys.path.insert(0, str(root))
            for module_name in ("product_profile", "provider_adapters", "turn_engine"):
                sys.modules.pop(module_name, None)
            try:
                profile_module = importlib.import_module("product_profile")
                engine_module = importlib.import_module("turn_engine")
                profile = profile_module.test_product_profile()
                language = profile.language("en")
                engine = engine_module.ConversationTurnEngine(
                    _Adapter(), object(), profile,
                    endpointing_silence_seconds=0.0,
                    final_grace_seconds=0.0,
                    silence_reprompt_seconds=5.0,
                )
                engine._language = language
                engine._arm_reprompt()
                self.assertIsNotNone(engine._reprompt_task)
                reprompt_task = engine._reprompt_task
                await asyncio.wait_for(engine.drain(), timeout=0.1)
                engine._cancel_reprompt()
                await asyncio.gather(reprompt_task, return_exceptions=True)
            finally:
                sys.path.remove(str(root))
                for module_name in ("product_profile", "provider_adapters", "turn_engine"):
                    sys.modules.pop(module_name, None)


if __name__ == "__main__":
    unittest.main()
