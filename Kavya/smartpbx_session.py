"""Dialog SmartPBX lifecycle adapter for Kavya's existing media pipeline."""

from __future__ import annotations

import asyncio
import copy
import inspect
import logging
import os
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Literal

from smartpbx_diagnostics import SmartPBXDiagnosticSink
from smartpbx_protocol import CallContext
from smartpbx_transport import SmartPBXMediaTransport


PostCallProcessor = Callable[..., Awaitable[None]]
logger = logging.getLogger(__name__)
_SESSION_MAX_MS = 600_000


class _LanguageProfileSTTUnavailable(Exception):
    """A selected profile's recognizer could not be constructed pre-commit."""


@dataclass(frozen=True)
class SmartPBXLanguageProfile:
    """The immutable provider/STT choices for one selected IVR language."""

    lang: Literal["en", "si"]
    stt_provider: Literal["google", "azure"]
    stt_language: Literal["en", "si"]
    stt_fail_closed: bool
    llm_provider: Literal["claude", "gemini", "openai"]
    model: str
    gemini_thinking_level: str | None = None
    gemini_max_tokens: int | None = None


def _without_transfer_tool(
    tools: list[dict[str, Any]], provider: str,
) -> list[dict[str, Any]]:
    """Remove Dialog-owned transfer without corrupting provider-native schemas."""
    if provider != "gemini":
        return [tool for tool in tools if tool.get("name") != "transfer_to_human"]
    filtered: list[dict[str, Any]] = []
    for tool in tools:
        declarations = [
            declaration
            for declaration in tool.get("function_declarations", [])
            if declaration.get("name") != "transfer_to_human"
        ]
        if declarations:
            filtered.append({**tool, "function_declarations": declarations})
    return filtered


def _emit_smartpbx_session_summary(**fields: object) -> None:
    """Emit only the fixed aggregate SmartPBX session contract."""
    logger.info("smartpbx_media %s", " ".join(f"{key}={value}" for key, value in fields.items()))


def _resolve_llm_provider(provider: str | None, default_provider: str) -> str:
    resolved_provider = default_provider if provider is None else provider
    if resolved_provider not in {"claude", "gemini", "openai"}:
        raise ValueError("invalid LLM provider")
    return resolved_provider


class KavyaSmartPBXSession:
    """Own one Dialog call while reusing Kavya's media-session behavior."""

    def __init__(
        self,
        context: CallContext,
        transport: SmartPBXMediaTransport,
        *,
        pipeline: Any | None = None,
        stt_factory: Callable[..., Any] | None = None,
        post_call_processor: PostCallProcessor | None = None,
        welcome_text: str | None = None,
        llm_provider: str | None = None,
        model: str | None = None,
        diagnostic_sink: SmartPBXDiagnosticSink | None = None,
        echo_rejection_callback: Callable[[int, float], None] | None = None,
    ) -> None:
        self._context = context
        self._transport = transport
        self._pipeline = pipeline
        self._stt_factory = stt_factory
        self._post_call_processor = post_call_processor
        self._welcome_text = welcome_text
        self._welcome_uses_language_default = welcome_text is None
        self._llm_provider = llm_provider
        self._model = model
        self._diagnostic_sink = diagnostic_sink if diagnostic_sink is not None else _noop_diagnostic
        self._record_echo_rejection = echo_rejection_callback

        loop = asyncio.get_running_loop()
        self._terminal_future: asyncio.Future[None] = loop.create_future()
        self._start_lock = asyncio.Lock()
        self._finish_lock = asyncio.Lock()
        self._language_activation_lock = asyncio.Lock()
        self._start_task: asyncio.Task[None] | None = None
        self._finish_task: asyncio.Task[None] | None = None
        self._welcome_task: asyncio.Task[None] | None = None
        self._selected_language: str | None = None
        self._language_menu_task: asyncio.Task[None] | None = None
        self._language_timeout_handle: asyncio.TimerHandle | None = None
        self._invalid_language_selections = 0
        self._post_call_task: asyncio.Task[None] | None = None
        self._call_start_time = ""
        self._smartpbx_transfer_context: Any | None = None
        self._session_trace_id: str | None = None
        self._session_started_ns = time.monotonic_ns()
        self._session_summary_emitted = False
        self._prepared_stt_cleanup_ids: set[int] = set()

    @property
    def terminal_future(self) -> asyncio.Future[None]:
        return self._terminal_future

    @property
    def transfer_pending(self) -> bool:
        """Expose the inner pipeline state to the provider-neutral gateway."""
        return bool(getattr(self._pipeline, "transfer_pending", False))

    async def start(self) -> None:
        async with self._start_lock:
            if self._start_task is None:
                self._start_task = asyncio.create_task(self._start_once())
            task = self._start_task
        await asyncio.shield(task)

    async def feed_audio(self, payload: bytes) -> None:
        if self._finish_task is not None:
            return
        if getattr(self._pipeline, "transfer_pending", False):
            return
        if self._start_task is None or not self._start_task.done():
            raise RuntimeError("SmartPBX session is not started")
        pipeline = self._require_pipeline()
        if getattr(pipeline, "_stt", None) is not None:
            pipeline._stt.feed(bytes(payload))

    async def feed_dtmf(self, digit: str) -> bool:
        """Handle the session-owned language menu before normal DTMF collection."""
        if self._selected_language is None:
            if digit == "1":
                await self._activate_language("en", "digit")
                return True
            if digit == "2":
                await self._activate_language("si", "digit")
                return True
            self._invalid_language_selections += 1
            if self._invalid_language_selections == 1:
                await self._restart_language_menu()
            else:
                await self._activate_language("en", "invalid")
            return True
        return await self._require_pipeline().feed_dtmf(digit)

    async def finish(self, schedule_post_call: bool = False) -> None:
        async with self._finish_lock:
            if self._finish_task is None:
                self._finish_task = asyncio.create_task(
                    self._finish_once(schedule_post_call)
                )
            task = self._finish_task
        await asyncio.shield(task)

    async def _start_once(self) -> None:
        import server

        self._ensure_session_trace_id()
        self._load_runtime_defaults()
        pipeline = self._require_pipeline()
        self._bind_smartpbx_tool_context(pipeline)
        # Bind the trace before telemetry can exist, so the very first turn
        # event already carries it.
        pipeline._smartpbx_session_trace_id = self._ensure_session_trace_id()
        ensure_telemetry = getattr(pipeline, "_ensure_smartpbx_turn_telemetry", None)
        if callable(ensure_telemetry):
            ensure_telemetry()
        set_transport_callback = getattr(self._transport, "set_transport_event_callback", None)
        if callable(set_transport_callback):
            set_transport_callback(getattr(pipeline, "_on_smartpbx_transport_event", None))
        pipeline._record_echo_rejection = self._record_echo_rejection
        pipeline.lang = "en"
        pipeline.call_sid = self._context.other_leg_call_id
        pipeline.caller_phone = self._context.caller_number
        self._call_start_time = datetime.now(timezone.utc).isoformat()
        pipeline.call_start_time = self._call_start_time
        pipeline._event_loop = asyncio.get_running_loop()
        pipeline._smartpbx_diagnostic_sink = self._diagnostic_sink
        self._language_menu_task = asyncio.create_task(self._speak_language_menu())
        loop = asyncio.get_running_loop()
        self._language_timeout_handle = loop.call_later(
            server.SMARTPBX_LANGUAGE_SELECTION_TIMEOUT_SECONDS,
            lambda: asyncio.create_task(self._activate_language("en", "timeout")),
        )

    async def _restart_language_menu(self) -> None:
        """Replay the menu once without turning an invalid key into barge-in."""
        menu_task = self._language_menu_task
        if menu_task is not None and not menu_task.done():
            menu_task.cancel()
            await asyncio.gather(menu_task, return_exceptions=True)
        if self._selected_language is None:
            self._language_menu_task = asyncio.create_task(self._speak_language_menu())

    async def _speak_language_menu(self) -> None:
        """Speak the pre-STT menu only while this session still awaits selection."""
        pipeline = self._require_pipeline()
        segments = (
            ("en", "For English, press 1."),
            ("si", "සිංහල සඳහා, 2 ඔබන්න."),
        )
        for lang, text in segments:
            if self._selected_language is not None:
                return
            pipeline.lang = lang
            await pipeline._speak(text)
            if self._selected_language is not None:
                return

    async def _activate_language(
        self,
        lang: Literal["en", "si"],
        source: Literal["digit", "timeout", "invalid"],
    ) -> None:
        """Prepare provider/STT first, then make exactly one committed selection."""
        del source  # The source is audit context; selection semantics are identical.
        async with self._language_activation_lock:
            if self._selected_language is not None or self._finish_task is not None:
                return
            pipeline = self._require_pipeline()
            requested_profile = self._resolve_language_profile(lang)
            prepared = await self._preflight_language_profile(pipeline, requested_profile)
            if prepared is None:
                return
            profile, prepared_client, prepared_tools, prepared_stt = prepared
            if self._selected_language is not None or self._finish_task is not None:
                await self._cleanup_unstarted_prepared_stt(prepared_stt)
                return

            # The first mutation claims this call's language. Everything before
            # it is local preflight, so a failed candidate cannot cross-mutate a
            # concurrent call or leave a half-selected menu behind.
            self._selected_language = profile.lang
            timeout_handle = self._language_timeout_handle
            self._language_timeout_handle = None
            if timeout_handle is not None:
                timeout_handle.cancel()
            menu_task = self._language_menu_task
            if menu_task is not None and not menu_task.done():
                menu_task.cancel()
            await self._transport.clear_audio()
            if self._finish_task is not None:
                await self._cleanup_unstarted_prepared_stt(prepared_stt)
                return
            if menu_task is not None:
                await asyncio.gather(menu_task, return_exceptions=True)
            if self._finish_task is not None:
                await self._cleanup_unstarted_prepared_stt(prepared_stt)
                return

            pipeline._is_speaking = False
            pipeline._speak_generation = getattr(pipeline, "_speak_generation", 0) + 1
            profile = self._apply_prepared_language_profile(
                pipeline, profile, prepared_client, prepared_tools,
            )
            import server
            pipeline.lang = profile.lang
            pipeline.system_prompt = server._build_system_prompt(profile.lang)
            if self._finish_task is not None:
                await self._cleanup_unstarted_prepared_stt(prepared_stt)
                return
            pipeline._stt = prepared_stt
            self._wire_stt_fatal_signal(prepared_stt)
            if self._finish_task is not None:
                await self._cleanup_unstarted_prepared_stt(prepared_stt)
                if pipeline._stt is prepared_stt:
                    pipeline._stt = None
                return
            try:
                prepared_stt.start()
            except asyncio.CancelledError:
                raise
            except Exception:
                await self._cleanup_started_prepared_stt(prepared_stt)
                if pipeline._stt is prepared_stt:
                    pipeline._stt = None
                self._emit_language_profile_unavailable(profile)
                self._end_call_without_language_profile(profile)
                return
            if self._finish_task is not None:
                await self._cleanup_started_prepared_stt(prepared_stt)
                if pipeline._stt is prepared_stt:
                    pipeline._stt = None
                return
            welcome_text = (
                server.LANGUAGE_CONFIGS[profile.lang]["welcome_greeting"]
                if self._welcome_uses_language_default
                else self._welcome_text
            )
            if welcome_text:
                pipeline._smartpbx_welcome_audio_pending = welcome_text
                self._welcome_task = asyncio.create_task(pipeline._speak(welcome_text))

    def _resolve_language_profile(
        self, lang: Literal["en", "si"],
    ) -> SmartPBXLanguageProfile:
        import server

        if lang == "en":
            return SmartPBXLanguageProfile(
                lang="en",
                stt_provider="azure" if server.STT_PROVIDER == "azure" else "google",
                stt_language="en",
                stt_fail_closed=False,
                llm_provider=self._llm_provider or server.LLM_PROVIDER,
                model=self._model or server.MODEL,
            )
        provider = server.SMARTPBX_SINHALA_LLM_PROVIDER
        return SmartPBXLanguageProfile(
            lang="si",
            stt_provider="azure",
            stt_language="si",
            stt_fail_closed=True,
            llm_provider=provider,
            model=(server.SMARTPBX_SINHALA_GEMINI_LLM_MODEL
                   if provider == "gemini" else server.CLAUDE_MODEL),
            gemini_thinking_level=server.SMARTPBX_SINHALA_GEMINI_THINKING_LEVEL,
            gemini_max_tokens=server.SMARTPBX_SINHALA_GEMINI_MAX_TOKENS,
        )

    async def _preflight_language_profile(
        self, pipeline: Any, requested: SmartPBXLanguageProfile,
    ) -> tuple[SmartPBXLanguageProfile, Any, list[dict[str, Any]], Any] | None:
        """Acquire one complete profile and recognizer without mutating a call."""
        try:
            return self._prepare_language_profile(pipeline, requested)
        except asyncio.CancelledError:
            raise
        except _LanguageProfileSTTUnavailable:
            self._emit_language_profile_unavailable(requested)
            self._end_call_without_language_profile(requested)
            return None
        except Exception:
            if requested.lang != "si" or requested.llm_provider != "gemini":
                self._emit_language_profile_unavailable(requested)
                self._end_call_without_language_profile(requested)
                return None
        # A Gemini client/setup failure has one bounded Sinhala Claude rollback.
        import server
        fallback = SmartPBXLanguageProfile(
            lang="si", stt_provider="azure", stt_language="si", stt_fail_closed=True,
            llm_provider="claude", model=server.CLAUDE_MODEL,
        )
        try:
            prepared = self._prepare_language_profile(pipeline, fallback)
        except asyncio.CancelledError:
            raise
        except _LanguageProfileSTTUnavailable:
            self._emit_language_profile_unavailable(fallback)
            self._end_call_without_language_profile(fallback)
            return None
        except Exception:
            self._emit_language_profile_unavailable(fallback, provider="none")
            self._end_call_without_language_profile(fallback)
            return None
        logger.warning(
            "smartpbx_media event=language_profile_fallback lang=si "
            "from=gemini to=claude reason=client_unavailable"
        )
        return prepared

    def _prepare_language_profile(
        self, pipeline: Any, profile: SmartPBXLanguageProfile,
    ) -> tuple[SmartPBXLanguageProfile, Any, list[dict[str, Any]], Any]:
        """Synchronous technical half of preflight; deliberately no assignments."""
        import server

        if profile.lang == "en":
            # The English adapter is already fully resolved by
            # _load_runtime_defaults; IVR selection must not rebuild tools or
            # touch unrelated TTS/LLM configuration.
            client = None
            tools = getattr(pipeline, "tools", [])
        elif profile.llm_provider == "gemini":
            client = getattr(pipeline, "gemini_client", None) or server._get_gemini_client()
            tools = _without_transfer_tool(server.get_tools_gemini(), "gemini")
        elif profile.llm_provider == "claude":
            client = getattr(pipeline, "anthropic_client", None) or server._get_anthropic_client()
            # An injected Claude pipeline already owns its provider-native
            # schema; preserve that contract while copying it below. A provider
            # switch instead rebuilds the native Claude definition.
            tools = (
                getattr(pipeline, "tools", [])
                if getattr(pipeline, "llm_provider", None) == "claude"
                else server.get_tools()
            )
            tools = _without_transfer_tool(tools, "claude")
        elif profile.llm_provider == "openai":
            client = getattr(pipeline, "client", None) or server._get_client()
            tools = _without_transfer_tool(server.get_tools_openai(), "openai")
        else:
            raise RuntimeError("unsupported language profile provider")
        try:
            stt = self._stt_factory(
                on_final_result=pipeline._on_stt_result,
                on_interim_result=pipeline._on_stt_interim,
                lang=profile.stt_language,
                privacy_safe=True,
                provider=profile.stt_provider,
                fail_closed=profile.stt_fail_closed,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise _LanguageProfileSTTUnavailable from exc
        return profile, client, copy.deepcopy(tools), stt

    def _apply_language_profile(
        self, pipeline: Any, profile: SmartPBXLanguageProfile,
    ) -> SmartPBXLanguageProfile | None:
        """Compatibility seam for callers that already hold a validated profile."""
        try:
            import server
            if profile.lang == "en":
                client = None
                tools = copy.deepcopy(getattr(pipeline, "tools", []))
            elif profile.llm_provider == "gemini":
                client = getattr(pipeline, "gemini_client", None) or server._get_gemini_client()
                tools = _without_transfer_tool(server.get_tools_gemini(), "gemini")
            elif profile.llm_provider == "claude":
                client = getattr(pipeline, "anthropic_client", None) or server._get_anthropic_client()
                tools = _without_transfer_tool(
                    getattr(pipeline, "tools", [])
                    if getattr(pipeline, "llm_provider", None) == "claude"
                    else server.get_tools(),
                    "claude",
                )
            else:
                client = getattr(pipeline, "client", None) or server._get_client()
                tools = _without_transfer_tool(server.get_tools_openai(), "openai")
        except Exception:
            return None
        return self._apply_prepared_language_profile(pipeline, profile, client, tools)

    def _apply_prepared_language_profile(
        self, pipeline: Any, profile: SmartPBXLanguageProfile,
        prepared_client: Any, prepared_tools: list[dict[str, Any]],
    ) -> SmartPBXLanguageProfile:
        # English keeps the adapter's already-resolved provider/model/client,
        # while still breaking every shared mutable tools reference.
        pipeline.tools = copy.deepcopy(prepared_tools)
        if profile.lang == "en":
            return profile
        if profile.llm_provider == "gemini":
            pipeline.gemini_client = prepared_client
        elif profile.llm_provider == "claude":
            pipeline.anthropic_client = prepared_client
        else:
            pipeline.client = prepared_client
        pipeline.llm_provider = profile.llm_provider
        pipeline.model = profile.model
        self._llm_provider = profile.llm_provider
        self._model = profile.model
        pipeline._gemini_thinking_level = profile.gemini_thinking_level
        pipeline._smartpbx_gemini_max_tokens = profile.gemini_max_tokens
        return profile

    async def _cleanup_unstarted_prepared_stt(self, stt: Any) -> None:
        await self._cleanup_prepared_stt(stt)

    async def _cleanup_started_prepared_stt(self, stt: Any) -> None:
        await self._cleanup_prepared_stt(stt)

    async def _cleanup_prepared_stt(self, stt: Any) -> None:
        if stt is None or id(stt) in self._prepared_stt_cleanup_ids:
            return
        self._prepared_stt_cleanup_ids.add(id(stt))
        stop = getattr(stt, "stop", None)
        if callable(stop):
            result = stop()
            if inspect.isawaitable(result):
                await result

    def _emit_language_profile_unavailable(
        self, profile: SmartPBXLanguageProfile, *, provider: str | None = None,
    ) -> None:
        logger.warning(
            "smartpbx_media event=language_profile_unavailable lang=%s provider=%s",
            profile.lang, provider or profile.stt_provider,
        )

    def _end_call_without_language_profile(self, _profile: SmartPBXLanguageProfile) -> None:
        """Terminal profile failure is not a false generic STT-unavailable event."""
        if not self._terminal_future.done():
            self._terminal_future.set_result(None)

    async def _finish_once(self, schedule_post_call: bool) -> None:
        """Publish finish before acquiring the activation/teardown handoff lock."""
        await self._language_activation_lock.acquire()
        try:
            await self._finish_once_locked(schedule_post_call)
        finally:
            self._language_activation_lock.release()

    async def _finish_once_locked(self, schedule_post_call: bool) -> None:
        try:
            pipeline = self._pipeline
            if pipeline is None:
                return
            timeout_handle = self._language_timeout_handle
            self._language_timeout_handle = None
            if timeout_handle is not None:
                timeout_handle.cancel()
            menu_task = self._language_menu_task
            if menu_task is not None and not menu_task.done():
                menu_task.cancel()
                await asyncio.gather(menu_task, return_exceptions=True)
            close_dispatch = getattr(pipeline, "_close_teardown_dispatch", None)
            if callable(close_dispatch):
                close_dispatch()
            # A specialized PMS filler has the same terminal ownership as the
            # initial filler below. It may hold the shared TTS lock while a
            # tool awaits, so cancellation alone is insufficient: terminal
            # summary/future completion must wait for it to release the lock.
            finish_tool_fillers = getattr(pipeline, "_cancel_smartpbx_tool_fillers", None)
            if callable(finish_tool_fillers):
                pending = finish_tool_fillers()
                if inspect.isawaitable(pending):
                    await pending
            # Model sentences deferred behind a later transfer/PMS tool are
            # likewise session-owned. Await their release before terminal
            # completion so an old runner cannot keep the shared TTS lock live.
            finish_deferred_tts = getattr(pipeline, "_cancel_smartpbx_deferred_tts", None)
            if callable(finish_deferred_tts):
                pending = finish_deferred_tts()
                if inspect.isawaitable(pending):
                    await pending
            # Cancel-and-await the initial-round filler BEFORE any other
            # teardown await below. Its delay (default 1.5s) runs on its own
            # timer, independent of the provider stream -- if it is still
            # pending when teardown starts, an `await asyncio.to_thread(stt.
            # stop)` or similar below could let that timer fire mid-teardown
            # and speak (and queue audio for) a session that is already going
            # away. `_finish_initial_smartpbx_filler` no-ops for `None` and is
            # idempotent (safe even if a runner's own cleanup already fired
            # it), so calling it unconditionally here is safe.
            finish_filler = getattr(pipeline, "_finish_initial_smartpbx_filler", None)
            if callable(finish_filler):
                await finish_filler(getattr(pipeline, "_smartpbx_initial_filler", None))
            pipeline._cancel_reprompt()
            # Resolve any in-flight keypad collection so its awaiting turn unwinds
            # instead of hanging on the collector future.
            cancel_dtmf = getattr(pipeline, "_cancel_dtmf_collection", None)
            if cancel_dtmf is not None:
                cancel_dtmf()
            endpointing_handle = getattr(pipeline, "_endpointing_handle", None)
            if endpointing_handle is not None:
                endpointing_handle.cancel()
                pipeline._endpointing_handle = None
            stt = getattr(pipeline, "_stt", None)
            if stt is not None:
                # stop() joins the STT worker thread for up to 5s. This loop is
                # shared by every concurrent call, so it must not block here.
                await asyncio.to_thread(stt.stop)
            if self._welcome_task is not None:
                if not self._welcome_task.done():
                    self._welcome_task.cancel()
                await asyncio.gather(self._welcome_task, return_exceptions=True)
            # No-op for Dialog calls: only MediaStreamSession.run() appends to
            # _audio_dump, and the SmartPBX path never runs it. STT_DEBUG_DUMP
            # produces no wavs here -- do not debug STT waiting for them.
            pipeline._write_audio_dump()
            close_stt_callbacks = getattr(pipeline, "_close_stt_callbacks", None)
            if callable(close_stt_callbacks):
                pending = close_stt_callbacks()
                if inspect.isawaitable(pending):
                    await pending
            # Dialog hangup/stop ownership: RETAINED. Anything still buffered —
            # a half-finished number, or ordinary speech admitted while a turn
            # held the dispatch guard — goes into the transcript before the
            # snapshot below reads it for post-call extraction. It must not
            # vanish with the socket.
            retain_pending_speech = getattr(pipeline, "_retain_pending_speech", None)
            if callable(retain_pending_speech):
                pending = retain_pending_speech("hangup")
                if inspect.isawaitable(pending):
                    await pending
            if self._smartpbx_transfer_context is not None and self._smartpbx_transfer_context.coordinator is not None:
                await self._smartpbx_transfer_context.coordinator.finalize_notification_retry()

            transcript = list(getattr(pipeline, "full_transcript", []))
            if schedule_post_call and transcript:
                self._post_call_task = asyncio.create_task(
                    self._post_call_processor(
                        call_sid=self._context.other_leg_call_id,
                        lang=self._selected_language or "en",
                        caller_phone=self._context.caller_number,
                        full_transcript=transcript,
                        call_start_time=self._call_start_time,
                        call_end_time=datetime.now(timezone.utc).isoformat(),
                        llm_provider=self._llm_provider,
                        anthropic_client=getattr(pipeline, "anthropic_client", None),
                        openai_client=getattr(pipeline, "client", None),
                        gemini_client=getattr(pipeline, "gemini_client", None),
                        model=self._model,
                        privacy_safe=True,
                    )
                )
        finally:
            # Unfinished turns must reach exactly one terminal summary before
            # the session aggregate that counts them.
            #
            # Fix-4 (pre-merge review, in-flight tool vs. teardown race):
            # the in-flight utterance-runner task (the `asyncio.ensure_future`
            # spawned by `_arm_endpointing`'s endpointing timer -> `_flush_transcript`
            # -> `_process_utterance*` -> the LLM runner -> `execute_tool`) is
            # deliberately NOT cancelled here. A cancel could land mid-`await
            # execute_tool(...)`, and asyncio cancellation gives no guarantee the
            # outbound HTTP request (e.g. create_booking) had not already been
            # sent when CancelledError is raised -- a half-committed side effect
            # on the wire is worse than letting the call finish. Ownership is
            # instead revalidated on the pipeline side after every awaited
            # provider/tool boundary (`_current_smartpbx_runner_owns_shared_state`),
            # which already discards a result when a newer turn/generation has
            # taken over (ordinary barge-in); `_finalize_smartpbx_turns()` below
            # clears `_active_smartpbx_turn_id`, so a runner that resumes after
            # this point loses ownership the same way and its result is
            # discarded rather than committed to history. The one gap that fix
            # closes: when a tool already executed this turn, losing ownership
            # now emits one bounded `turn_stage stage="late_tool_completion"`
            # event first, so a late-completing tool becomes an observable
            # event instead of a silent success with no trace.
            finalize_turns = getattr(self._pipeline, "_finalize_smartpbx_turns", None)
            if callable(finalize_turns):
                finalize_turns()
            self._emit_session_summary()
            if not self._terminal_future.done():
                self._terminal_future.set_result(None)

    def _ensure_session_trace_id(self) -> str:
        if self._session_trace_id is None:
            self._session_trace_id = secrets.token_urlsafe(16)
        return self._session_trace_id

    def _emit_session_summary(self) -> None:
        if self._session_summary_emitted:
            return
        self._session_summary_emitted = True
        pipeline = self._pipeline
        telemetry = getattr(pipeline, "_turn_telemetry", None)
        duration_ms = min(max((time.monotonic_ns() - self._session_started_ns) // 1_000_000, 0), _SESSION_MAX_MS)
        _emit_smartpbx_session_summary(
            event="session_summary",
            session_trace_id=self._ensure_session_trace_id(),
            outcome="finished",
            turns_started=min(max(int(getattr(telemetry, "turns_started", 0)), 0), 100_000),
            turns_summarized=min(max(int(getattr(telemetry, "turns_summarized", 0)), 0), 100_000),
            duration_ms=duration_ms,
            frames_dropped_total=min(max(int(getattr(self._transport, "frames_dropped_total", 0)), 0), 100_000),
            barge_ins=min(max(int(getattr(pipeline, "_smartpbx_barge_ins", 0)), 0), 100_000),
        )

    def _wire_stt_fatal_signal(self, stt: Any) -> None:
        """Let a terminally failed STT stream end the call in seconds.

        Without this the STT thread gives up quietly and the guest hears nothing
        until the gateway's idle timeout, 90 seconds later.
        """
        loop = asyncio.get_running_loop()

        def on_fatal() -> None:
            # Invoked from the STT worker thread, so hop back onto the loop.
            loop.call_soon_threadsafe(self._end_call_without_stt)

        try:
            stt.on_fatal = on_fatal
        except (AttributeError, TypeError):
            return

    def _end_call_without_stt(self) -> None:
        from smartpbx_diagnostics import (
            DiagnosticFailureClass, DiagnosticOutcome, DiagnosticStage,
        )

        try:
            self._diagnostic_sink(
                DiagnosticStage.AUDIO_INGESTION,
                DiagnosticOutcome.FAILED,
                DiagnosticFailureClass.STT_UNAVAILABLE,
            )
        except Exception:
            pass
        if not self._terminal_future.done():
            self._terminal_future.set_result(None)

    def _load_runtime_defaults(self) -> None:
        import server

        self._llm_provider = _resolve_llm_provider(
            self._llm_provider, server.LLM_PROVIDER
        )
        if all(
            value is not None
            for value in (
                self._pipeline,
                self._stt_factory,
                self._post_call_processor,
                self._welcome_text,
                self._llm_provider,
                self._model,
            )
        ):
            return

        if self._model is None:
            self._model = server.MODEL

        if self._pipeline is None:
            anthropic_client = openai_client = gemini_client = None
            if self._llm_provider == "claude":
                anthropic_client = server._get_anthropic_client()
            elif self._llm_provider == "gemini":
                gemini_client = server._get_gemini_client()
            else:
                openai_client = server._get_client()
            self._pipeline = server.MediaStreamSession(
                websocket=None,
                lang="en",
                anthropic_client=anthropic_client,
                openai_client=openai_client,
                gemini_client=gemini_client,
                media_transport=self._transport,
                llm_provider=self._llm_provider,
                model=self._model,
            )
        if self._stt_factory is None:
            self._stt_factory = server._make_stt
        if self._post_call_processor is None:
            self._post_call_processor = server.process_post_call_data
        if self._welcome_text is None:
            self._welcome_text = server.LANGUAGE_CONFIGS["en"]["welcome_greeting"]

    def _require_pipeline(self) -> Any:
        if self._pipeline is None:
            raise RuntimeError("Kavya media pipeline is unavailable")
        return self._pipeline

    def _bind_smartpbx_tool_context(self, pipeline: Any) -> None:
        """Bind transfer and booking state to this validated Dialog call only."""
        from smartpbx_mcp import DialogMCPCallControl, DialogMCPSettings
        from smartpbx_handover import SmartPBXHandoverCoordinator
        from tools import SmartPBXTransferContext
        import handover
        try:
            import dashboard_client
            dashboard_sender = dashboard_client.send_call_transferred
        except Exception:
            dashboard_sender = None

        settings = DialogMCPSettings.from_env(os.environ)
        control = DialogMCPCallControl(settings, self._context)
        coordinator = SmartPBXHandoverCoordinator(
            call_control=control, pipeline=pipeline, call_sid=self._context.other_leg_call_id,
            caller_phone=self._context.caller_number,
            transcript=lambda: getattr(pipeline, "full_transcript", []),
            dashboard_sender=dashboard_sender, notification_sender=handover.send_handover_notification,
            human_agent_whatsapp=os.getenv("SMARTPBX_HUMAN_AGENT_WHATSAPP", ""),
        )
        self._smartpbx_transfer_context = SmartPBXTransferContext(
            control,
            coordinator=coordinator,
            pipeline=pipeline,
        )
        pipeline._smartpbx_transfer_context = self._smartpbx_transfer_context
        pipeline._smartpbx_caller_context = {
            "caller_phone": self._context.caller_number,
        }


def _noop_diagnostic(*_args: object) -> None:
    return None
