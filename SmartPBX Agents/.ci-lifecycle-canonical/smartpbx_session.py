"""Client-neutral SmartPBX conversation session with no tenant business tools."""

from __future__ import annotations

import asyncio
from typing import Any

from product_profile import ProductProfile, test_product_profile
from provider_adapters import AfterCallHook, ConversationProviderAdapter, RecognizerFatal, Retriever
from turn_engine import ConversationTurnEngine


class InquirySmartPBXSession:
    """A call-local language session that delegates only to injected providers."""

    def __init__(
        self,
        _context: Any,
        transport: Any,
        _diagnostic_sink: Any,
        *,
        provider_adapter: ConversationProviderAdapter,
        product_profile: ProductProfile | None = None,
        retriever: Retriever | None = None,
        after_call_hook: AfterCallHook | None = None,
    ) -> None:
        self._transport = transport
        self._provider_adapter = provider_adapter
        self._product_profile = product_profile or test_product_profile()
        self._after_call_hook = after_call_hook
        self._turn_engine = ConversationTurnEngine(
            provider_adapter,
            transport,
            self._product_profile,
            retriever=retriever,
            on_terminal_failure=self._complete_recognizer_fatal,
        )
        self.terminal_future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self.close_reason: str | None = None
        self._selected_language: str | None = None
        self._dtmf_map = {
            str(index): code for index, code in enumerate(self._product_profile.language_profiles, start=1)
        }
        self._language_timeout: asyncio.TimerHandle | None = None
        self._started = False
        self._finished = False

    @property
    def supported_languages(self) -> tuple[str, ...]:
        return tuple(self._product_profile.language_profiles)

    async def start(self) -> None:
        if self._started:
            return
        if not self._turn_engine.active:
            raise RuntimeError("conversation provider adapter is inactive")
        self._started = True
        default = self._product_profile.default_language
        self._turn_engine.set_recognizer_result_admission(False)
        await self._turn_engine.start(self._product_profile.language(default))
        if len(self._product_profile.language_profiles) == 1:
            self._selected_language = default
            await self._turn_engine.speak_profile_message(
                self._product_profile.language(default).greeting, "initial-greeting"
            )
            self._turn_engine.set_recognizer_result_admission(True)
            return
        # Selection remains call-local.  The menu is delivered and marked
        # before its timeout begins, matching the carrier pacing boundary.
        await self._turn_engine.speak_profile_message(
            self._product_profile.language(default).menu_prompt, "language-menu"
        )
        self._language_timeout = asyncio.get_running_loop().call_later(
            8.0, lambda: asyncio.create_task(self._select_language(default))
        )

    async def feed_dtmf(self, digit: str) -> None:
        if not self._started or self._finished:
            return
        selected = self._dtmf_map.get(digit)
        if selected is not None:
            await self._select_language(selected)

    async def _select_language(self, selected: str) -> None:
        if self._finished or self._selected_language is not None:
            return
        timeout, self._language_timeout = self._language_timeout, None
        if timeout is not None:
            timeout.cancel()
        self._selected_language = selected
        language = self._product_profile.language(selected)
        await self._turn_engine.set_language(language)
        await self._turn_engine.speak_profile_message(language.greeting, "initial-greeting")
        self._turn_engine.set_recognizer_result_admission(True)

    async def feed_audio(self, audio: bytes) -> None:
        if not self._started:
            raise RuntimeError("SmartPBX session is not started")
        if self._finished:
            return
        if self._selected_language is None:
            # Speech before a digit selects the profile default; no static
            # en/si branch or process-global language choice exists.
            await self._select_language(self._product_profile.default_language)
        language = self._product_profile.language(self._selected_language or self._product_profile.default_language)
        await self._turn_engine.accept_audio(audio, language)

    async def _complete_recognizer_fatal(self, _failure: RecognizerFatal) -> None:
        """Complete the call terminal signal once without retaining provider detail."""
        if self._finished:
            return
        self._finished = True
        if self._language_timeout is not None:
            self._language_timeout.cancel()
            self._language_timeout = None
        self.close_reason = "stt_fatal"
        if not self.terminal_future.done():
            self.terminal_future.set_result(None)

    async def finish(self, **_kwargs: object) -> None:
        if self._finished:
            return
        self._finished = True
        if self._language_timeout is not None:
            self._language_timeout.cancel()
            self._language_timeout = None
        try:
            await self._turn_engine.close()
            if self._after_call_hook is not None and self._selected_language is not None:
                await self._after_call_hook.complete(
                    language=self._product_profile.language(self._selected_language),
                    turns=self._turn_engine.turns_completed,
                )
        finally:
            if not self.terminal_future.done():
                self.terminal_future.set_result(None)
