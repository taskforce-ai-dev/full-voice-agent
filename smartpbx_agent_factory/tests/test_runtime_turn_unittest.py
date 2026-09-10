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
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        root = Path(self._directory.name)
        self._module_names = ("product_profile", "provider_adapters", "turn_engine")
        self._previous_modules = {name: sys.modules.pop(name, None) for name in self._module_names}
        for module_name in self._module_names:
            (root / f"{module_name}.py").write_text(
                (_RUNTIME / f"{module_name}.py.tmpl").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
        sys.path.insert(0, str(root))
        profile_module = importlib.import_module("product_profile")
        self._engine_module = importlib.import_module("turn_engine")
        self._profile = profile_module.test_product_profile()
        self._language = self._profile.language("en")

    def tearDown(self) -> None:
        sys.path.remove(self._directory.name)
        for module_name in self._module_names:
            sys.modules.pop(module_name, None)
            previous = self._previous_modules[module_name]
            if previous is not None:
                sys.modules[module_name] = previous
        self._directory.cleanup()

    def _engine(self) -> object:
        engine = self._engine_module.ConversationTurnEngine(
            _Adapter(), object(), self._profile,
            endpointing_silence_seconds=0.0,
            final_grace_seconds=0.0,
            silence_reprompt_seconds=5.0,
        )
        engine._language = self._language
        return engine

    async def test_drain_does_not_wait_for_the_silence_reprompt_timer(self) -> None:
        engine = self._engine()
        await engine._arm_reprompt()
        self.assertIsNotNone(engine._reprompt_task)
        await asyncio.wait_for(engine.drain(), timeout=0.1)
        await engine.close()

    async def test_close_cancels_and_awaits_the_current_reprompt(self) -> None:
        engine = self._engine()
        entered = asyncio.Event()
        cancelled = asyncio.Event()

        async def waiting_reprompt() -> None:
            entered.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                cancelled.set()
                raise

        engine._reprompt_after_silence = waiting_reprompt
        await engine._arm_reprompt()
        task = engine._reprompt_task
        self.assertIsNotNone(task)
        await entered.wait()

        await engine.close()

        self.assertTrue(cancelled.is_set())
        self.assertTrue(task.cancelled())
        self.assertIsNone(engine._reprompt_task)

    async def test_reprompt_exception_is_observed_and_clears_current_ownership(self) -> None:
        engine = self._engine()
        reported: list[dict[str, object]] = []
        loop = asyncio.get_running_loop()
        prior_handler = loop.get_exception_handler()

        async def failing_reprompt() -> None:
            raise RuntimeError("reprompt failure")

        engine._reprompt_after_silence = failing_reprompt
        loop.set_exception_handler(lambda _loop, context: reported.append(context))
        try:
            await engine._arm_reprompt()
            task = engine._reprompt_task
            self.assertIsNotNone(task)
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            self.assertTrue(task.done())
            self.assertIsNone(engine._reprompt_task)
            self.assertEqual(reported, [])
        finally:
            loop.set_exception_handler(prior_handler)

    async def test_rearm_cannot_overwrite_a_newer_reprompt_while_awaiting_an_older_one(self) -> None:
        engine = self._engine()
        first_started = asyncio.Event()
        first_cancelled = asyncio.Event()
        release_first = asyncio.Event()
        runs = 0

        async def controlled_reprompt() -> None:
            nonlocal runs
            runs += 1
            if runs == 1:
                first_started.set()
                try:
                    await asyncio.Future()
                except asyncio.CancelledError:
                    first_cancelled.set()
                    await release_first.wait()
                    return
            await asyncio.Future()

        engine._reprompt_after_silence = controlled_reprompt
        await engine._arm_reprompt()
        await first_started.wait()

        older_rearm = asyncio.create_task(engine._arm_reprompt())
        await first_cancelled.wait()
        await engine._arm_reprompt()
        newer_task = engine._reprompt_task
        self.assertIsNotNone(newer_task)

        release_first.set()
        await older_rearm
        self.assertIs(engine._reprompt_task, newer_task)
        await engine.close()


if __name__ == "__main__":
    unittest.main()
