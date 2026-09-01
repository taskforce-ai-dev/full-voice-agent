# Kavya SmartPBX Sinhala Gemini LLM Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make SmartPBX IVR option 2 use Gemini 3.7 Flash for normal Sinhala turns, retain Claude as an automatic call-local technical fallback, and leave IVR option 1 behavior unchanged.

**Architecture:** Resolve one immutable language profile at IVR activation and apply it to that call's existing `MediaStreamSession` before STT starts. The profile records executable STT, LLM, tool, and generation-control values only; the existing immutable `lang` remains the execution key for both the current STT factory and TTS router. Reuse the existing Gemini streaming/tool/failover runner, but first close its Gemini 3.7 function-call-ID, deadline, truncation, and per-session thinking-compatibility contracts. Do not add another runner, provider registry, transport, or descriptive TTS fields to the profile. Keep Azure `si-LK` as the only live Sinhala STT because neither Chirp 2 nor Gemini Transcribe currently documents Sinhala live-streaming support.

**Tech Stack:** Python 3.11, FastAPI/asyncio, `google-genai==2.16.0`, `anthropic==0.120.2`, Azure Speech `1.51.1`, pytest in GitHub Actions, Docker Compose SmartPBX profile.

## Global Constraints

- SmartPBX only: do not modify Twilio, ConversationRelay, Flico, audio framing, or dashboard routing.
- IVR option 1, timeout, and second-invalid-digit fallback must preserve the current English STT, Claude model/client/tools, ElevenLabs TTS, prompt, latency, fillers, capture, and handover behavior.
- IVR option 2 defaults to Gemini `gemini-3.7-flash`, thinking level `low`, and output ceiling `600`; it retains Gemini `gemini-3.1-flash-tts-preview` voice `Vindemiatrix` for TTS.
- `SMARTPBX_SINHALA_LLM_PROVIDER=claude` is the operational rollback. Runtime Gemini failures reuse the existing call-local Gemini-to-Claude failover and sticky-degradation state.
- Provider/model/tool/thinking/token state and thinking-compatibility decisions are session-owned. Shared lazy SDK client singletons may remain, but a call must not mutate process-wide request behavior read by another call.
- Keep Azure `si-LK` as live Sinhala STT and fail option 2 closed if its SDK, `audioop`, or a blank/whitespace-only `AZURE_SPEECH_KEY` is unavailable; never silently route Sinhala to Google. `AzureSTTStream` cancellation must call its session-wired `on_fatal` callback exactly once, never for normal `stop()`. Do not add `google_chirp2` or Gemini Transcribe to `_make_stt()` in this change.
- Do not add deprecated Gemini 3.x sampling parameters (`temperature`, `top_p`, `top_k`) or migrate the existing Generate Content runner to the Interactions API in this change.
- New diagnostics use fixed event/enumerated fields only; never log prompts, transcripts, caller data, tool arguments/results, response bodies, exception bodies, audio, API keys, or credentials.
- Do not run pytest on the development or production host. Use `python3 -m py_compile` locally for syntax only and the existing GitHub Actions Kavya test job for behavioral RED/GREEN evidence.
- Preserve existing dirty Graphify artifacts and `docs/superpowers/plans/2026-08-31-kavya-smartpbx-sinhala-gemini-tts.md`; do not stage or modify them.

## File Map

- Modify `Kavya/server.py`: closed Sinhala LLM settings, Gemini 3.7 call-ID/history compliance, direct-SmartPBX deadlines and incomplete-round fencing, session-owned Gemini thinking/token controls, sticky fallback readiness, request wiring, and conversational Sinhala prompt rules.
- Modify `Kavya/smartpbx_session.py`: immutable language profile resolution and call-local profile activation before STT starts.
- Modify `Kavya/tests/test_smartpbx_sinhala_ivr.py`: provider/model/client/tool isolation and concurrent English/Sinhala session contracts.
- Modify `Kavya/tests/test_smartpbx_server.py`: explicit Azure fail-closed versus preserved English/configured STT fallback behavior.
- Modify `Kavya/tests/test_gemini_streaming.py`: Gemini 3.7 function-call IDs, truncation/deadline behavior, session isolation, Sinhala-specific request shape, and Claude fallback preservation.
- Modify `Kavya/tests/test_smartpbx_gemini_tts.py`: option-1 ElevenLabs, option-2 Gemini, and Gemini-TTS preservation during Claude LLM fallback.
- Modify `Kavya/tests/test_prompt_policy.py`: conversational Sinhala prompt contract without weakening English policy.
- Modify `Kavya/tests/test_smartpbx_deployment.py`: Compose, dotenv, and runbook setting contracts.
- Modify `Kavya/docker-compose.yml`: explicit SmartPBX-only environment allowlist entries.
- Modify `Kavya/.env.example`: non-secret Sinhala LLM defaults and rollback comments.
- Modify `Kavya/SMARTPBX_RUNBOOK.md`: runtime behavior, canary checks, and one-line rollback.
- Modify `Kavya/AGENTS.md` and `Kavya/CLAUDE.md` together only in the final documentation task so the required mirror remains exact.

---

### Task 1: Add closed Sinhala LLM settings

**Files:**
- Modify: `Kavya/server.py:556-676`
- Test: `Kavya/tests/test_smartpbx_sinhala_ivr.py`

**Interfaces:**
- Consumes: process environment only at module initialization.
- Produces: `_resolve_smartpbx_sinhala_llm_provider(raw: object) -> str`, `_resolve_smartpbx_sinhala_gemini_thinking_level(raw: object) -> str`, `_resolve_smartpbx_sinhala_gemini_max_tokens(raw: object) -> int`, and constants `SMARTPBX_SINHALA_LLM_PROVIDER`, `SMARTPBX_SINHALA_GEMINI_LLM_MODEL`, `SMARTPBX_SINHALA_GEMINI_THINKING_LEVEL`, `SMARTPBX_SINHALA_GEMINI_MAX_TOKENS`.

- [ ] **Step 1: Write the failing resolver tests**

Add these tests to `Kavya/tests/test_smartpbx_sinhala_ivr.py`:

```python
def test_sinhala_llm_provider_defaults_to_gemini_and_invalid_values_fail_to_claude():
    assert server._resolve_smartpbx_sinhala_llm_provider(None) == "gemini"
    assert server._resolve_smartpbx_sinhala_llm_provider("") == "gemini"
    assert server._resolve_smartpbx_sinhala_llm_provider(" GEMINI ") == "gemini"
    assert server._resolve_smartpbx_sinhala_llm_provider("claude") == "claude"
    assert server._resolve_smartpbx_sinhala_llm_provider("openai") == "claude"


def test_sinhala_gemini_thinking_level_is_closed_and_latency_safe():
    assert server._resolve_smartpbx_sinhala_gemini_thinking_level(None) == "low"
    assert server._resolve_smartpbx_sinhala_gemini_thinking_level("") == "low"
    assert server._resolve_smartpbx_sinhala_gemini_thinking_level("medium") == "medium"
    assert server._resolve_smartpbx_sinhala_gemini_thinking_level("HIGH") == "high"
    assert server._resolve_smartpbx_sinhala_gemini_thinking_level("minimal") == "low"


def test_sinhala_gemini_output_budget_defaults_and_clamps():
    assert server._resolve_smartpbx_sinhala_gemini_max_tokens(None) == 600
    assert server._resolve_smartpbx_sinhala_gemini_max_tokens("") == 600
    assert server._resolve_smartpbx_sinhala_gemini_max_tokens("invalid") == 600
    assert server._resolve_smartpbx_sinhala_gemini_max_tokens("199") == 200
    assert server._resolve_smartpbx_sinhala_gemini_max_tokens("600") == 600
    assert server._resolve_smartpbx_sinhala_gemini_max_tokens("1025") == 1024
```

- [ ] **Step 2: Push the RED test commit to the task branch and capture CI evidence**

```bash
git add Kavya/tests/test_smartpbx_sinhala_ivr.py
git commit -m "test(kavya): specify Sinhala Gemini LLM settings"
git push origin Rakesh
```

Expected GitHub Actions result: the Kavya `test` matrix fails only because the three resolver functions do not exist. Do not run local pytest.

- [ ] **Step 3: Implement the minimal closed resolvers**

Add beside the existing SmartPBX token resolvers in `Kavya/server.py`:

```python
_SMARTPBX_SINHALA_LLM_PROVIDERS = frozenset({"gemini", "claude"})
_SMARTPBX_SINHALA_GEMINI_THINKING_LEVELS = frozenset({"low", "medium", "high"})


def _resolve_smartpbx_sinhala_llm_provider(raw: object) -> str:
    value = "" if raw is None else str(raw).strip().lower()
    if not value:
        return "gemini"
    return value if value in _SMARTPBX_SINHALA_LLM_PROVIDERS else "claude"


def _resolve_smartpbx_sinhala_gemini_thinking_level(raw: object) -> str:
    value = "" if raw is None else str(raw).strip().lower()
    return value if value in _SMARTPBX_SINHALA_GEMINI_THINKING_LEVELS else "low"


def _resolve_smartpbx_sinhala_gemini_max_tokens(raw: object) -> int:
    try:
        value = int(raw) if raw not in (None, "") else 600
    except (TypeError, ValueError):
        value = 600
    return min(max(value, 200), 1024)


SMARTPBX_SINHALA_LLM_PROVIDER = _resolve_smartpbx_sinhala_llm_provider(
    os.getenv("SMARTPBX_SINHALA_LLM_PROVIDER")
)
SMARTPBX_SINHALA_GEMINI_LLM_MODEL = (
    os.getenv("SMARTPBX_SINHALA_GEMINI_LLM_MODEL", "gemini-3.7-flash").strip()
    or "gemini-3.7-flash"
)
SMARTPBX_SINHALA_GEMINI_THINKING_LEVEL = (
    _resolve_smartpbx_sinhala_gemini_thinking_level(
        os.getenv("SMARTPBX_SINHALA_GEMINI_THINKING_LEVEL")
    )
)
SMARTPBX_SINHALA_GEMINI_MAX_TOKENS = _resolve_smartpbx_sinhala_gemini_max_tokens(
    os.getenv("SMARTPBX_SINHALA_GEMINI_MAX_TOKENS")
)
```

Do not alter `LLM_PROVIDER`, `MODEL`, `GEMINI_MODEL`, `SMARTPBX_MAX_TOKENS`, or `SMARTPBX_CLAUDE_MAX_TOKENS`.

- [ ] **Step 4: Run syntax validation and push GREEN**

```bash
python3 -m py_compile Kavya/server.py Kavya/tests/test_smartpbx_sinhala_ivr.py
git add Kavya/server.py Kavya/tests/test_smartpbx_sinhala_ivr.py
git commit -m "feat(kavya): add closed Sinhala Gemini LLM settings"
git push origin Rakesh
```

Expected GitHub Actions result: the new resolver tests pass and every pre-existing test remains green.

---

### Task 2: Resolve and apply an immutable per-language profile

**Files:**
- Modify: `Kavya/smartpbx_session.py:1-235,432-477`
- Modify: `Kavya/server.py:4737-4898` (`AzureSTTStream._on_canceled()` and `_make_stt()`)
- Test: `Kavya/tests/test_smartpbx_sinhala_ivr.py`
- Test: `Kavya/tests/test_smartpbx_server.py`

**Interfaces:**
- Consumes: the four constants from Task 1 and the existing English `_llm_provider`/`_model` resolved by `_load_runtime_defaults()`.
- Produces: frozen `SmartPBXLanguageProfile`, `KavyaSmartPBXSession._resolve_language_profile(lang)`, `KavyaSmartPBXSession._apply_language_profile(pipeline, profile)`, and `_without_transfer_tool(tools, provider)`.

- [ ] **Step 1: Make the recording pipeline expose provider-owned state**

At the test module imports, add `import copy`; the English preservation test
below compares a deep-copied expected tool value separately from list identity.

Extend `RecordingPipeline.__init__()` in `Kavya/tests/test_smartpbx_sinhala_ivr.py`:

```python
self.llm_provider = "claude"
self.model = "test-model"
self._gemini_thinking_level = "global-low"
self._smartpbx_gemini_max_tokens = 120
self.anthropic_client = object()
self.gemini_client = None
```

Give each `RecordingStt` a `snapshot_factory` callback and a
`profile_at_start` field. Have `make_session()` pass a closure over its pipeline,
then make `start()` call that closure so the test observes the exact state at
the recognition boundary:

```python
self.profile_at_start = self.snapshot_factory()
```

The closure returns `lang`, `llm_provider`, `model`, a deep copy of `tools`,
`_gemini_thinking_level`, and `_smartpbx_gemini_max_tokens` from `pipeline`.

Use distinct English Anthropic and Sinhala Gemini client sentinels. Do not
pre-populate `gemini_client` in the default fake: the option-2 test must prove
lazy initialization rather than accidentally bypass it.

Extend the existing
`test_selected_sinhala_uses_sinhala_welcome_and_post_call_language()` test so
the captured post-call metadata also proves the selected profile reaches the
adapter-owned fields used by `_finish_once()`:

```python
assert post_calls[0]["llm_provider"] == "gemini"
assert post_calls[0]["model"] == "gemini-3.7-flash"
```

- [ ] **Step 2: Write the failing language-profile tests**

Add:

```python
@pytest.mark.asyncio
async def test_digit_two_applies_gemini_profile_before_sinhala_stt(monkeypatch):
    gemini_tools = [{
        "function_declarations": [
            {"name": "transfer_to_human"},
            {"name": "check_availability"},
            {"name": "create_booking"},
        ]
    }]
    gemini_client = object()
    monkeypatch.setattr(server, "SMARTPBX_SINHALA_LLM_PROVIDER", "gemini")
    monkeypatch.setattr(server, "SMARTPBX_SINHALA_GEMINI_LLM_MODEL", "gemini-3.7-flash")
    monkeypatch.setattr(server, "SMARTPBX_SINHALA_GEMINI_THINKING_LEVEL", "low")
    monkeypatch.setattr(server, "SMARTPBX_SINHALA_GEMINI_MAX_TOKENS", 600)
    monkeypatch.setattr(server, "get_tools_gemini", lambda: gemini_tools)
    monkeypatch.setattr(server, "_get_gemini_client", lambda: gemini_client)

    session, pipeline, stt = make_session()
    await session.start()
    await session.feed_dtmf("2")

    assert stt.starts == 1
    assert pipeline.llm_provider == "gemini"
    assert pipeline.gemini_client is gemini_client
    assert pipeline.model == "gemini-3.7-flash"
    assert pipeline._gemini_thinking_level == "low"
    assert pipeline._smartpbx_gemini_max_tokens == 600
    assert pipeline.tools == [{
        "function_declarations": [
            {"name": "check_availability"},
            {"name": "create_booking"},
        ]
    }]
    assert stt.profile_at_start == {
        "lang": "si",
        "llm_provider": "gemini",
        "model": "gemini-3.7-flash",
        "tools": pipeline.tools,
        "thinking_level": "low",
        "max_tokens": 600,
    }


@pytest.mark.asyncio
async def test_english_selection_keeps_existing_provider_model_tools_and_clients(monkeypatch):
    session, pipeline, _stt = make_session()
    monkeypatch.setattr(
        server,
        "load_kavya_english_voice_profile",
        lambda: (_ for _ in ()).throw(AssertionError("IVR must not load TTS secrets")),
    )
    original_tools = pipeline.tools
    expected_tools = copy.deepcopy(original_tools)
    expected = (
        pipeline.llm_provider,
        pipeline.model,
        pipeline.anthropic_client,
        pipeline.gemini_client,
    )
    monkeypatch.setattr(
        server,
        "get_tools_gemini",
        lambda: (_ for _ in ()).throw(AssertionError("English must not rebuild Gemini tools")),
    )

    await session.start()
    await session.feed_dtmf("1")

    assert (
        pipeline.llm_provider,
        pipeline.model,
        pipeline.anthropic_client,
        pipeline.gemini_client,
    ) == expected
    assert pipeline.tools == expected_tools
    assert pipeline.tools is original_tools
    # The profile contains no descriptive TTS fields. `lang` remains the
    # executable router key, so selecting English cannot load TTS secrets.
    assert session._resolve_language_profile("en").lang == "en"


@pytest.mark.asyncio
async def test_concurrent_english_and_sinhala_profiles_never_cross_mutate(monkeypatch):
    monkeypatch.setattr(server, "SMARTPBX_SINHALA_LLM_PROVIDER", "gemini")
    monkeypatch.setattr(server, "SMARTPBX_SINHALA_GEMINI_LLM_MODEL", "gemini-3.7-flash")
    monkeypatch.setattr(server, "SMARTPBX_SINHALA_GEMINI_THINKING_LEVEL", "low")
    monkeypatch.setattr(server, "SMARTPBX_SINHALA_GEMINI_MAX_TOKENS", 600)
    monkeypatch.setattr(
        server,
        "get_tools_gemini",
        lambda: [{"function_declarations": [{"name": "check_availability"}]}],
    )
    english, english_pipeline, _ = make_session()
    sinhala, sinhala_pipeline, _ = make_session()

    await asyncio.gather(english.start(), sinhala.start())
    await asyncio.gather(english.feed_dtmf("1"), sinhala.feed_dtmf("2"))

    assert (english_pipeline.lang, english_pipeline.llm_provider, english_pipeline.model) == (
        "en", "claude", "test-model"
    )
    assert (sinhala_pipeline.lang, sinhala_pipeline.llm_provider, sinhala_pipeline.model) == (
        "si", "gemini", "gemini-3.7-flash"
    )
```

Add a separate behavioral routing test, using a no-welcome test session so
selection is isolated from greeting synthesis. Patch
`load_kavya_english_voice_profile` to raise during `feed_dtmf("1")` and assert
selection still starts English STT. Then replace the pipeline's
`_tts_elevenlabs` and `_tts_gemini_sinhala` with distinct async recorders,
restore a canonical English voice-profile stub for the actual speak, and await
`pipeline._speak("English route")`: assert only `_tts_elevenlabs` received the
call and that it used the canonical-profile path. In the paired Sinhala session
await `_speak("සිංහල")` and assert only `_tts_gemini_sinhala` received the call.
The test must assert the concrete `_speak` dispatches, not merely profile field
values, because `lang` is the actual TTS execution key.

Also extend `Kavya/tests/test_smartpbx_gemini_tts.py`: prove option 1 speaks
with ElevenLabs, option 2 speaks with Gemini, and a Sinhala Gemini-to-Claude
LLM fallback still speaks through Gemini TTS. Preserve and test the current
bilingual pre-selection menu separately: `_speak_language_menu()` temporarily
sets `pipeline.lang="si"` for the Sinhala menu line and therefore uses Gemini
TTS before selection. This LLM-only plan must not silently change that menu
behavior or mistake it for selected-profile routing.

Strengthen that test beyond the abbreviated tuples: use distinct client
sentinels, synchronize both activations at a barrier, and assert both STT start
snapshots, complete provider-native tool shapes, thinking level, token ceiling,
client identity, adapter `_llm_provider`/`_model`, profile STT fields, and the
separate `lang`-owned TTS routing assertions above.
Neither session may hold the other session's mutable tool list. Repeat the same
English assertions for timeout and second-invalid selection so those paths
cannot silently inherit Sinhala state.

Add the rollback-profile test explicitly:

```python
@pytest.mark.asyncio
async def test_sinhala_claude_rollback_profile_stays_transfer_free(monkeypatch):
    monkeypatch.setattr(server, "SMARTPBX_SINHALA_LLM_PROVIDER", "claude")
    claude_client = object()
    monkeypatch.setattr(server, "_get_anthropic_client", lambda: claude_client)
    session, pipeline, stt = make_session()
    pipeline.anthropic_client = None

    await session.start()
    await session.feed_dtmf("2")

    assert stt.starts == 1
    assert pipeline.llm_provider == "claude"
    assert pipeline.model == server.CLAUDE_MODEL
    assert pipeline.anthropic_client is claude_client
    assert all("function_declarations" not in tool for tool in pipeline.tools)
    assert "transfer_to_human" not in {tool["name"] for tool in pipeline.tools}
```

Add a focused provider-shape test for `_without_transfer_tool()` with a Gemini
tool wrapper whose only declaration is `transfer_to_human`; assert the returned
list is empty. This prevents an invalid empty
`{"function_declarations": []}` wrapper from reaching Gemini. Retain a second
wrapper containing `transfer_to_human` plus `check_availability` and assert it
keeps only the latter declaration.

Add two activation-failure tests with exact outcomes:

```python
def _raise_runtime_error():
    raise RuntimeError("synthetic client failure")


def _capture_profile_events(monkeypatch):
    events: list[str] = []

    def capture(message, *args, **_kwargs):
        rendered = message % args if args else message
        if "event=language_profile_" in rendered:
            events.append(rendered)

    monkeypatch.setattr(smartpbx_session.logger, "warning", capture)
    return events


@pytest.mark.asyncio
async def test_sinhala_gemini_client_init_failure_falls_back_before_stt(monkeypatch):
    events = _capture_profile_events(monkeypatch)
    session, pipeline, stt = make_session()
    claude_client = pipeline.anthropic_client
    monkeypatch.setattr(server, "_get_gemini_client", _raise_runtime_error)

    await session.start()
    await session.feed_dtmf("2")

    assert stt.starts == 1
    assert pipeline.anthropic_client is claude_client
    assert stt.profile_at_start["llm_provider"] == "claude"
    assert stt.profile_at_start["model"] == server.CLAUDE_MODEL
    assert all(
        "function_declarations" not in tool
        for tool in stt.profile_at_start["tools"]
    )
    assert "transfer_to_human" not in {
        tool["name"] for tool in stt.profile_at_start["tools"]
    }
    assert events == [
        "smartpbx_media event=language_profile_fallback lang=si "
        "from=gemini to=claude reason=client_unavailable"
    ]


@pytest.mark.asyncio
async def test_sinhala_activation_fails_closed_when_no_llm_client_is_usable(monkeypatch):
    events = _capture_profile_events(monkeypatch)
    session, pipeline, stt = make_session()
    pipeline.anthropic_client = None
    monkeypatch.setattr(server, "_get_gemini_client", _raise_runtime_error)
    monkeypatch.setattr(server, "_get_anthropic_client", _raise_runtime_error)

    await session.start()
    await session.feed_dtmf("2")

    assert stt.starts == 0
    assert session.terminal_future.done()
    assert session._welcome_task is None
    assert events == [
        "smartpbx_media event=language_profile_unavailable lang=si provider=none"
    ]
```

Import the `smartpbx_session` module in the test file so the fixed-field logger
can be patched directly. The capture helper must not inspect exception text.

Add executable STT-factory tests in `test_smartpbx_server.py`:

```python
def test_explicit_sinhala_azure_is_fail_closed_when_sdk_is_unavailable(monkeypatch):
    monkeypatch.setattr(server, "AZURE_STT_AVAILABLE", False)
    with pytest.raises(RuntimeError, match="requested STT provider unavailable"):
        server._make_stt(
            lambda _text: None,
            lambda _text: None,
            "si",
            provider="azure",
            fail_closed=True,
        )


@pytest.mark.parametrize(
    ("attribute", "value"),
    (("AZURE_STT_AVAILABLE", False), ("audioop", None), ("AZURE_SPEECH_KEY", "  ")),
)
def test_explicit_sinhala_azure_is_fail_closed_when_a_required_dependency_is_missing(
    monkeypatch, attribute, value,
):
    monkeypatch.setattr(server, attribute, value)
    with pytest.raises(RuntimeError, match="requested STT provider unavailable"):
        server._make_stt(
            lambda _text: None,
            lambda _text: None,
            "si",
            provider="azure",
            fail_closed=True,
        )


def test_configured_english_azure_keeps_existing_google_fallback(monkeypatch):
    monkeypatch.setattr(server, "AZURE_STT_AVAILABLE", False)
    stream = server._make_stt(
        lambda _text: None,
        lambda _text: None,
        "en",
        provider="azure",
        fail_closed=False,
    )
    assert isinstance(stream, server.GoogleSTTStream)


@pytest.mark.asyncio
async def test_invalid_global_stt_provider_preserves_english_google_behavior(monkeypatch):
    monkeypatch.setattr(server, "STT_PROVIDER", "unexpected")
    session, _pipeline, stt = make_session()

    await session.start()
    await session.feed_dtmf("1")

    assert stt.starts == 1
    assert session._resolve_language_profile("en").stt_provider == "google"
```

Add an IVR-level variant whose STT factory raises for the concrete Azure
profile. Assert no STT start, no welcome, resolved terminal future, and the
single `provider=azure` unavailable event. Add parallel variants for
`AZURE_STT_AVAILABLE=False`, `audioop=None`, and a blank `AZURE_SPEECH_KEY`;
all three must reject Sinhala before welcome or STT start while the configured
English fallback remains unchanged. Add an `AzureSTTStream` unit test that
sets `stream.on_fatal` to a recorder, fires `_on_canceled()` twice, and asserts
one fatal callback; then call normal `stop()` and assert it does not signal a
fatal condition. Implementation must add `on_fatal` and an idempotence fence to
Azure's instance state, invoke it only for runtime cancellation, and never
falsely treat Google-only fatal signaling as Azure behavior.

The first diagnostic contract is
`smartpbx_media event=language_profile_fallback lang=si from=gemini to=claude reason=client_unavailable`.
The terminal contract is
`smartpbx_media event=language_profile_unavailable lang=si provider=none`.
Never include exception text, credentials, prompt text, caller data, or response
bodies in either line.

- [ ] **Step 3: Push RED and capture the provider-isolation failure**

```bash
git add Kavya/tests/test_smartpbx_sinhala_ivr.py
git commit -m "test(kavya): specify per-language SmartPBX LLM isolation"
git push origin Rakesh
```

Expected GitHub Actions result: option 2 still reports Claude/test-model and the concurrent profile assertion fails; existing English tests pass.

- [ ] **Step 4: Add the immutable profile and provider-shaped transfer filter**

In `Kavya/smartpbx_session.py`, import `dataclass` and add:

```python
from dataclasses import dataclass


@dataclass(frozen=True)
class SmartPBXLanguageProfile:
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
```

The `if declarations` guard is required: a Gemini declaration wrapper that
becomes empty after removing `transfer_to_human` must be dropped, not retained
as `{"function_declarations": []}`.

Add these methods to `KavyaSmartPBXSession`:

```python
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
        model=(
            server.SMARTPBX_SINHALA_GEMINI_LLM_MODEL
            if provider == "gemini"
            else server.CLAUDE_MODEL
        ),
        gemini_thinking_level=server.SMARTPBX_SINHALA_GEMINI_THINKING_LEVEL,
        gemini_max_tokens=server.SMARTPBX_SINHALA_GEMINI_MAX_TOKENS,
    )


def _apply_language_profile(
    self, pipeline: Any, profile: SmartPBXLanguageProfile,
) -> SmartPBXLanguageProfile | None:
    if profile.lang == "en":
        return profile
    import server

    if profile.llm_provider == "gemini":
        if getattr(pipeline, "gemini_client", None) is None:
            pipeline.gemini_client = server._get_gemini_client()
        tools = server.get_tools_gemini()
    else:
        if getattr(pipeline, "anthropic_client", None) is None:
            pipeline.anthropic_client = server._get_anthropic_client()
        tools = server.get_tools()
    pipeline.llm_provider = profile.llm_provider
    pipeline.model = profile.model
    self._llm_provider = profile.llm_provider
    self._model = profile.model
    pipeline.tools = copy.deepcopy(
        _without_transfer_tool(tools, profile.llm_provider)
    )
    pipeline._gemini_thinking_level = profile.gemini_thinking_level
    pipeline._smartpbx_gemini_max_tokens = profile.gemini_max_tokens
    return profile
```

The concrete code must not blindly call the client getters as the abbreviated
snippet above suggests. Wrap Gemini initialization before changing any pipeline
field. On `RuntimeError`, resolve a Claude copy of the same Sinhala STT/TTS
profile, initialize or reuse Anthropic, emit the bounded fallback event, and
apply that profile atomically. If Anthropic is also unavailable, emit the
bounded unavailable event, call a new LLM/profile-specific
`_end_call_without_language_profile()` helper, and return `None`. That helper
resolves `terminal_future` and does not report the existing false
`STT_UNAVAILABLE` diagnostic. `_activate_language()` must use the returned
profile, not the original requested profile.

In `_activate_language()`, after the menu task is canceled/awaited and before `pipeline.lang = lang`, add:

```python
requested_profile = self._resolve_language_profile(lang)
profile = self._apply_language_profile(pipeline, requested_profile)
if profile is None:
    return
```

Extend `_make_stt()` with optional keyword-only `provider` and `fail_closed`
arguments. Omitted arguments must preserve every existing caller. For an
explicit Azure provider, treat missing SDK, `audioop`, **or a
whitespace-stripped-empty `AZURE_SPEECH_KEY`** as unavailable. When unavailable and `fail_closed=True`,
raise a bounded `RuntimeError` instead of constructing Google; with
`fail_closed=False`, retain the existing Google fallback. Reject unknown
explicit providers.

Construct STT with `lang=profile.stt_language`,
`provider=profile.stt_provider`, and
`fail_closed=profile.stt_fail_closed`. Catch factory/start failure before the
welcome task, emit
`smartpbx_media event=language_profile_unavailable lang=si provider=azure`,
and terminate via the same profile-specific helper. Assert the fixed profile
mapping before start: English normalizes exact `azure` to Azure and every other
configured value to Google, preserving today's behavior. IVR selection must not
load or validate ElevenLabs secrets; after selection, an English `_speak` test
must prove the canonical `lang="en"` ElevenLabs route while a Sinhala `_speak`
test proves the existing `lang="si"` Gemini-TTS route is preserved. Do not add
a new provider registry, profile TTS fields, or change the current `lang`-based
TTS path.

Import `copy` in `smartpbx_session.py`. `get_tools()` and `get_tools_gemini()`
may return shared nested definitions, so every assigned session tool set must
be deep-copied. Add a two-session test that asserts distinct list and nested
declaration identities, mutates one session's tool declaration after profile
activation, and proves the other session and future `get_tools*()` output are
unchanged.

Do not rebuild the `MediaStreamSession`, STT callbacks, transport, history, or tool context.

- [ ] **Step 5: Syntax-check, push GREEN, and verify the exact changed boundary**

```bash
python3 -m py_compile Kavya/server.py Kavya/smartpbx_session.py Kavya/tests/test_smartpbx_sinhala_ivr.py Kavya/tests/test_smartpbx_server.py Kavya/tests/test_smartpbx_gemini_tts.py
git diff --check -- Kavya/server.py Kavya/smartpbx_session.py Kavya/tests/test_smartpbx_sinhala_ivr.py Kavya/tests/test_smartpbx_server.py Kavya/tests/test_smartpbx_gemini_tts.py
git add Kavya/server.py Kavya/smartpbx_session.py Kavya/tests/test_smartpbx_sinhala_ivr.py Kavya/tests/test_smartpbx_server.py Kavya/tests/test_smartpbx_gemini_tts.py
git commit -m "feat(kavya): bind SmartPBX LLMs to IVR language"
git push origin Rakesh
```

Expected GitHub Actions result: provider-isolation tests pass; all existing SmartPBX IVR, teardown, DTMF, handover, and English tests remain green.

---

### Task 3: Make the Gemini 3.7 SmartPBX runner provider-correct and session-owned

**Files:**
- Modify: `Kavya/server.py:3274-3473,4946-4983,5180-5194,8291-8444`
- Test: `Kavya/tests/test_gemini_streaming.py:425-466,665-960`
- Test: `Kavya/tests/test_smartpbx_server.py:3318-3356`

**Interfaces:**
- Consumes: `pipeline._gemini_thinking_level` and `pipeline._smartpbx_gemini_max_tokens` assigned by Task 2, Gemini 3.7 `FunctionCall.id`, the existing direct-SmartPBX timeout helpers, and the existing atomic timeout recovery path.
- Produces: matching Gemini `FunctionResponse.id` values, fail-closed incomplete-round classification, direct-SmartPBX Sinhala deadlines, per-session thinking-compatibility state, `_gemini_thinking_config(model, thinking_level, unsupported_models)`, `_build_gemini_config(..., thinking_level, unsupported_models)`, and provider-aware `_provider_max_tokens("gemini")` behavior.

- [ ] **Step 1: Write the failing provider-contract, deadline, and request-isolation tests**

Add to `Kavya/tests/test_gemini_streaming.py`:

```python
def test_direct_sinhala_gemini_uses_its_session_owned_thinking_and_budget():
    session, _spoken = _session(
        [[_text_chunk("හරි."), _terminal_chunk()]],
        lang="si",
        smartpbx=True,
        model="gemini-3.7-flash",
    )
    session._gemini_thinking_level = "low"
    session._smartpbx_gemini_max_tokens = 600

    asyncio.run(session._run_llm_gemini())

    config = session.gemini_client.configs[0]
    assert config["thinking_config"] == {"thinking_level": "low"}
    assert config["max_output_tokens"] == 600


def test_direct_english_gemini_keeps_shared_controls():
    session, _spoken = _session(
        [[_text_chunk("All right."), _terminal_chunk()]],
        lang="en",
        smartpbx=True,
        model="gemini-3.7-flash",
    )
    session._gemini_thinking_level = server.GEMINI_THINKING_LEVEL
    session._smartpbx_gemini_max_tokens = 600

    asyncio.run(session._run_llm_gemini())

    config = session.gemini_client.configs[0]
    assert config["thinking_config"] == {
        "thinking_level": server.GEMINI_THINKING_LEVEL
    }
    assert config["max_output_tokens"] == server.SMARTPBX_MAX_TOKENS
```

First repair the Task-3 test harness before adding any Task-4 fallback test:
add `import copy` at the module imports, add `_terminal_chunk("STOP")`, add an
optional provider `id` to `_tool_chunk()`, record the complete request contents,
and make `_session()` stub `_tts_gemini_sinhala` as well as the existing TTS methods. Add
terminal metadata to every healthy direct-SmartPBX Gemini fake round across
both `test_gemini_streaming.py` and `test_smartpbx_server.py`, not only the
nearby line range. Non-direct fixtures keep their existing contract.

Then implement these complete production-shaped tests through
`_run_llm_gemini()`; placeholder/comment-only bodies are not acceptable:

| Test | Setup | Required assertions |
| --- | --- | --- |
| `test_gemini_37_tool_followup_preserves_provider_call_id` | First round emits `FunctionCall(id="call-17", name="check_availability")` plus `STOP`; mocked tool returns a fixed dict; second round emits healthy text plus `STOP`. | Tool executes once; request 2 contains model `function_call.id == "call-17"`, user `function_response.id == "call-17"`, matching name, response, and thought signature; no synthesized `gemini_tc_*` ID appears. |
| `test_gemini_max_tokens_tool_round_discards_all_and_executes_nothing` | Two attempts each emit a preamble, a complete-looking tool with an ID, then `MAX_TOKENS`; hold sentence TTS at the generation fence. | `execute_tool` count is zero; requests equal two; no assistant/tool history for either attempt; held TTS and filler are canceled/awaited; one recovery sentence is delivered; turn summary occurs once; `asyncio.all_tasks()` contains no task owned by the session. |
| `test_gemini_stream_without_terminal_metadata_is_aborted_not_completed` | Same as above but both attempts end at EOF without a finish chunk. | Same no-side-effect, no-partial-history, fencing, one-recovery, one-summary, and no-task assertions; bounded outcome is `stream_aborted`. |
| `test_direct_sinhala_gemini_uses_atomic_smartpbx_deadline_recovery` | Parameterize `acquire`, `initial`, and `mid_stream`; use the existing timeout clock/helpers and blocked TTS lock, but seed/patch a test `SmartPBXInitialFillerController` rather than relying on the production filler gate. | Filler and TTS cancellation complete; recovery does not hang; exactly one recovery sentence and summary; no stale undelivered audio; no tasks remain. A paired capture-mode assertion proves the timeout wrapper is not installed. Do not broaden the English-only production filler predicate. |
| `test_thinking_rejection_is_call_local_and_model_local` | Run two sessions concurrently. Session A's first request rejects thinking for `gemini-3.7-flash`; session B accepts it. | A retries once without thinking and records only that model locally; B's first request still contains its requested thinking level; module legacy latch is unchanged by either MediaStream session. |
| `test_gemini_session_constructor_owns_default_generation_controls` | Construct a session without assigning either new attribute. | Values equal `GEMINI_THINKING_LEVEL`, `SMARTPBX_MAX_TOKENS`, and an empty per-session unsupported-model set. |
| `test_non_smartpbx_sinhala_keeps_global_max_tokens` | Construct non-SmartPBX Sinhala Gemini session. | `_provider_max_tokens("gemini") == MAX_TOKENS`. |
| `test_direct_sinhala_gemini_exhausted_empty_uses_direct_recovery` | Two empty Gemini attempts in a direct Sinhala SmartPBX session. | The language-appropriate direct-SmartPBX recovery helper runs once, terminates/fences the turn, and Sinhala recovery speech uses the existing Sinhala TTS route. |
| `test_direct_sinhala_gemini_tool_executed_exception_uses_direct_recovery` | A Sinhala tool completes, then the Gemini runner raises on the follow-up. | No replay occurs; the direct-SmartPBX recovery helper runs once with `tool_executed=True`, with no duplicate tool, stale TTS, or generic English recovery. |

Update the existing direct-provider budget assertions in
`test_smartpbx_server.py:3318-3356` at the same time. Replace literal `120` and
`600` request assertions with `server.SMARTPBX_MAX_TOKENS` and
`server.SMARTPBX_CLAUDE_MAX_TOKENS`, respectively. Replace the environment-
dependent module-constant/default assertions with pure resolver cases (bounds,
invalid input, and explicit raw values) and one request-shape assertion against
the already-resolved constants. The tests must remain valid when deployment
environment variables override `SMARTPBX_MAX_TOKENS`; they must not encode
import-time defaults.

The incomplete-round tests assert bounded outcome metadata only; never log text,
tool arguments/results, exception bodies, caller data, or response bodies.

- [ ] **Step 2: Push RED and prove each pre-existing contract gap**

```bash
git add Kavya/tests/test_gemini_streaming.py
git commit -m "test(kavya): expose Sinhala Gemini request isolation"
git push origin Rakesh
```

Expected GitHub Actions result: failures are limited to the missing call-ID
round trip, unsafe max-token/aborted-round acceptance, missing Sinhala deadline,
process-wide thinking latch, constructor/session defaults, and the Sinhala
request still using `SMARTPBX_MAX_TOKENS` instead of its session-owned `600`
ceiling. Record the exact failing test names; do not assert or describe an
environment-dependent literal/default for `SMARTPBX_MAX_TOKENS`.

- [ ] **Step 3: Preserve Gemini 3.7 call IDs and reject incomplete rounds**

In `_iter_gemini_stream()`, defensively extract the provider's
`function_call.id`, name, and arguments: tolerate a missing name/ID and a
non-mapping arguments value by recording malformed payload state rather than
raising before the round classifier runs. Carry a valid ID as `payload["id"]`
alongside name, arguments, and thought signature. In the direct SmartPBX Gemini
path, store that ID in the internal
assistant tool-call entry instead of synthesizing `gemini_tc_*`. Extend the
SmartPBX call to `_history_to_gemini()` so the model function-call part contains
that ID and the matching SDK `FunctionResponse` dict contains `id`, `name`, and
`response`. Keep ConversationRelay on its current behavior in this SmartPBX-only
change.

Add a closed `SmartPBXGeminiRoundOutcome` classifier. Normalize finish reasons
to a fixed vocabulary and apply precedence in this order:

1. `MAX_TOKENS` -> truncated, even when visible text or a complete-looking tool
   part was received;
2. missing/invalid provider call ID, name, or argument mapping -> malformed tool;
3. no terminal finish metadata -> stream aborted;
4. otherwise a round with valid text or tools -> completed;
5. otherwise -> true empty.

Add a malformed-provider-payload test with missing name/ID and non-mapping
arguments. It must reach the `malformed tool` outcome, execute no tool, commit
no partial history, and never leak a parser exception from `_iter_gemini_stream()`.

Emit only fixed outcome metadata for every classified direct-SmartPBX Gemini
round: `smartpbx_media event=llm_round_outcome provider=gemini
outcome=<completed|truncated|malformed_tool|stream_aborted|empty>`. Add this
five-value allowlist to the deployment/runbook diagnostics test and document it
as bounded metadata only; never append text, arguments, exception details, or
provider response content.

Preserve the existing incremental-speech latency contract: completed sentences
may still enter the generation-fenced TTS pipeline before terminal metadata.
Only `completed` may execute tools or commit provider text/tool history. For a
non-completed outcome, cancel and await the filler and every still-undelivered
sentence task through the existing atomic recovery machinery before one
retry/recovery decision. Audio already confirmed delivered cannot be retracted;
retain it only through the existing delivered-sentence/transcript ownership
path, never as a fabricated completed model/tool round. Never execute a complete
sibling tool from a discarded batch. Do not duplicate the Claude enum or handler
mechanically: add only the Gemini-specific classifier and route into the
existing fencing helpers.

At the existing Gemini exhausted-empty recovery branch near `server.py:8570`
and the tool-executed exception branch near `server.py:8822`, replace the
English-only `_is_direct_smartpbx_english_non_capture()` check with the
language-appropriate `_is_direct_smartpbx_non_capture()` predicate. Keep
English-only filler, transfer, and wording gates unchanged. The two Sinhala
tests above must exercise these exact branches, rather than asserting a generic
fallback after the runner returns.

- [ ] **Step 4: Make thinking compatibility session-owned**

Stop consulting the process-wide `_gemini_thinking_unsupported` latch from the
MediaStream runner. Give each `MediaStreamSession` a model-keyed set and thread
it through the existing helpers. Preserve the current legacy latch only as the
default for callers outside `MediaStreamSession`, so Twilio behavior is not
changed by this SmartPBX task:

```python
def _gemini_thinking_config(
    model: str,
    thinking_level: str | None = None,
    unsupported_models: frozenset[str] | set[str] | None = None,
) -> dict[str, Any] | None:
    if (
        (_gemini_thinking_unsupported if unsupported_models is None else False)
        or (unsupported_models is not None and model in unsupported_models)
    ):
        return None
    match = _GEMINI_MAJOR_VERSION.search((model or "").lower())
    major = int(match.group(1)) if match else 2
    if major >= 3:
        level = GEMINI_THINKING_LEVEL if thinking_level is None else thinking_level
        return {"thinking_level": level or "low"}
    return {"thinking_budget": GEMINI_THINKING_BUDGET}
```

Add the optional keyword to `_build_gemini_config()` and use it:

```python
def _build_gemini_config(
    *,
    system: str,
    tools: list[dict] | None,
    model: str,
    nudge: str | None = None,
    max_output_tokens: int = MAX_TOKENS,
    thinking_level: str | None = None,
    unsupported_models: frozenset[str] | set[str] | None = None,
) -> dict[str, Any]:
    system_instruction = f"{system}\n\n{nudge}" if nudge else system
    config: dict[str, Any] = {
        "system_instruction": system_instruction,
        "max_output_tokens": max_output_tokens,
    }
    thinking = _gemini_thinking_config(model, thinking_level, unsupported_models)
    if thinking is not None:
        config["thinking_config"] = thinking
    if tools:
        config["tools"] = tools
    return config
```

Make `_open_gemini_stream()` accept that call's mutable set. On a bounded,
recognized thinking-config rejection, add only the current model to that set
and retry once without `thinking_config`. Do not log exception text. Existing
callers that are outside `MediaStreamSession` retain their request shape; do not
make a ConversationRelay/Twilio refactor part of this task.

- [ ] **Step 5: Initialize controls, budgets, deadlines, and sticky fallback**

In `MediaStreamSession.__init__()` add:

```python
self._gemini_thinking_level = GEMINI_THINKING_LEVEL
self._smartpbx_gemini_max_tokens = SMARTPBX_MAX_TOKENS
self._gemini_thinking_unsupported_models: set[str] = set()
```

Use the complete `_provider_max_tokens()` body so the non-direct guard cannot be
lost:

```python
if not self._is_direct_smartpbx():
    return MAX_TOKENS
if provider == "claude":
    return SMARTPBX_CLAUDE_MAX_TOKENS
if provider == "gemini" and self.lang == "si":
    return self._smartpbx_gemini_max_tokens
return SMARTPBX_MAX_TOKENS
```

In `_run_llm_gemini()` change only the config call:

```python
config=_build_gemini_config(
    system=self._active_system_prompt(),
    tools=self.tools,
    model=self.model,
    nudge=nudge,
    max_output_tokens=self._provider_max_tokens("gemini"),
    thinking_level=self._gemini_thinking_level,
    unsupported_models=self._gemini_thinking_unsupported_models,
),
```

Pass the same session-owned set to `_open_gemini_stream()`. Change only the
Gemini runner's timeout-policy predicate from
`_is_direct_smartpbx_english_non_capture()` to
`_is_direct_smartpbx_non_capture()` so Sinhala receives the existing initial
and stall deadlines while capture flows remain excluded. Keep English-specific
filler, transfer, and recovery wording predicates unchanged.

Finally, replace the sticky branch's `and ANTHROPIC_API_KEY` check with
`self._gemini_failover_ready()`. This honors an already injected Anthropic
client even when the process key string is blank. Add a degraded-state test that
sets `anthropic_client` directly, leaves the key blank, and asserts Gemini is
not called and provider/model/tools are restored after Claude.

Replace the full stale `_resolve_smartpbx_claude_max_tokens()` docstring, not
only a nearby comment: Gemini 3.7 may consume thinking tokens, while the
English Gemini ceiling remains `SMARTPBX_MAX_TOKENS` solely to preserve its
existing request contract. Correct the matching stale `.env.example` comment
in Task 5; neither may say Gemini has no thinking tokens or simply “stays on
SMARTPBX_MAX_TOKENS” without that preservation rationale.

- [ ] **Step 6: Verify syntax and GREEN CI**

```bash
python3 -m py_compile Kavya/server.py Kavya/tests/test_gemini_streaming.py
git diff --check -- Kavya/server.py Kavya/tests/test_gemini_streaming.py
git add Kavya/server.py Kavya/tests/test_gemini_streaming.py
git commit -m "fix(kavya): harden Sinhala Gemini provider rounds"
git push origin Rakesh
```

Expected GitHub Actions result: every new call-ID, truncation, aborted-stream,
deadline, constructor, sticky-fallback, and concurrency test passes, including
the English shared-budget and non-SmartPBX guards. Every existing Gemini retry,
tool-side-effect, thought-filtering, timeout, and failover test remains green.

---

### Task 4: Pin conversational Sinhala and Claude fallback behavior

**Dependency:** Complete Task 3 first. Its repaired Gemini harness (including
the Sinhala `_tts_gemini_sinhala` stub, terminal chunks, and `copy` import) is
required for the fallback tests below; do not duplicate an incomplete fixture
in this task.

**Files:**
- Modify: `Kavya/server.py:2322-2343`
- Test: `Kavya/tests/test_prompt_policy.py`
- Test: `Kavya/tests/test_gemini_streaming.py`

**Interfaces:**
- Consumes: `_build_system_prompt("si")`, existing `_run_claude_failover_turn()`, existing Gemini tools, and Gemini Sinhala TTS routing by `lang == "si"`.
- Produces: a non-contradictory spoken-Sinhala prompt contract and regression proof that Gemini failure temporarily invokes Claude without changing the session's normal provider/model/tools.

- [ ] **Step 1: Write the failing Sinhala prompt policy test**

Add to `Kavya/tests/test_prompt_policy.py`:

```python
def test_sinhala_prompt_requires_conversational_sri_lankan_speech_and_safe_code_switching():
    prompt = server._build_system_prompt("si")

    for rule in (
        "contemporary conversational Sri Lankan Sinhala",
        "one short sentence",
        "at most one question",
        "official room names, Hatton Hills, WhatsApp",
        "Preserve dates, prices, room names, guest counts, phone digits",
    ):
        assert rule in prompt
    assert "MUST respond entirely in Sinhala" not in prompt
```

- [ ] **Step 2: Write the failing call-local fallback restoration test**

Add to `Kavya/tests/test_gemini_streaming.py`:

```python
def test_direct_sinhala_gemini_failure_uses_claude_then_restores_profile(monkeypatch):
    session, spoken = _session(
        [[_empty_chunk()]],
        lang="si",
        smartpbx=True,
        model="gemini-3.7-flash",
    )
    session.gemini_client = FakeFlakyGemini([_QuotaError()])
    session.anthropic_client = object()
    original_tools = [{"function_declarations": [{"name": "check_availability"}]}]
    session.tools = original_tools
    seen: list[tuple[str, str, bool, list[dict]]] = []

    async def run_claude() -> str:
        seen.append((
            session.llm_provider,
            session.model,
            "caller selected Sinhala" in session._active_system_prompt(),
            copy.deepcopy(session.tools),
        ))
        return "මට ඔබට උදව් කරන්න පුළුවන්."

    monkeypatch.setattr(session, "_run_llm_claude", run_claude)

    result = asyncio.run(session._run_llm_gemini())

    assert result == "මට ඔබට උදව් කරන්න පුළුවන්."
    assert seen[0][:3] == ("claude", server.CLAUDE_MODEL, True)
    claude_tools = seen[0][3]
    assert all("function_declarations" not in tool for tool in claude_tools)
    assert "transfer_to_human" not in {tool["name"] for tool in claude_tools}
    assert "check_availability" in {tool["name"] for tool in claude_tools}
    assert session.llm_provider == "gemini"
    assert session.model == "gemini-3.7-flash"
    assert session.tools is original_tools
    assert spoken == []
```

The assertions inside `run_claude()` are load-bearing: checking only the final
restored object can pass while Claude was actually invoked with invalid Gemini
`function_declarations`. Also keep a degraded-state variant from Task 3 so the
sticky fast path proves the same temporary conversion and restoration.

- [ ] **Step 3: Push RED and confirm only the prompt contract fails**

```bash
git add Kavya/tests/test_prompt_policy.py Kavya/tests/test_gemini_streaming.py
git commit -m "test(kavya): specify conversational Sinhala and fallback"
git push origin Rakesh
```

Expected GitHub Actions result: the new prompt test fails on missing conversational wording. The fallback restoration test should already pass through the reused runner; if it fails, fix only the proven restoration defect before proceeding.

- [ ] **Step 4: Replace only the contradictory Sinhala language-rule block**

Use this `lang == "si"` content in `_build_system_prompt()`:

```python
language_rules = (
    "LANGUAGE RULES:\n"
    "- The caller selected Sinhala. Use Sinhala as the main language, in native "
    "Unicode script. Speak in contemporary conversational Sri Lankan Sinhala, "
    "not formal written or ceremonial Sinhala.\n"
    "- Keep a routine reply to one short sentence and ask at most one question. "
    "Use a second short sentence only when it carries necessary booking information.\n"
    "- Natural English code-switching is allowed for official room names, Hatton "
    "Hills, WhatsApp, and familiar hotel terms. Never romanize Sinhala words.\n"
    "- Never switch the whole response to English unless the guest explicitly "
    "switches to English.\n"
    "- Preserve dates, prices, room names, guest counts, phone digits, and tool "
    "results exactly while phrasing the surrounding response naturally.\n"
    "- Never expose English-only internal recovery, keypad, validation, or tool "
    "wording to the caller.\n\n"
)
```

Do not translate the full shared system prompt or add scripted sample answers. The fluent-human canary review, not unit tests, decides whether the resulting speech is natural enough.

- [ ] **Step 5: Verify syntax and GREEN CI**

```bash
python3 -m py_compile Kavya/server.py Kavya/tests/test_prompt_policy.py Kavya/tests/test_gemini_streaming.py
git diff --check -- Kavya/server.py Kavya/tests/test_prompt_policy.py Kavya/tests/test_gemini_streaming.py
git add Kavya/server.py Kavya/tests/test_prompt_policy.py Kavya/tests/test_gemini_streaming.py
git commit -m "feat(kavya): tune conversational Sinhala model policy"
git push origin Rakesh
```

Expected GitHub Actions result: prompt and fallback tests pass; existing English prompt-policy, booking, number-capture, recovery, and Gemini TTS tests stay green.

---

### Task 5: Expose only the approved SmartPBX settings and rollback

**Files:**
- Modify: `Kavya/docker-compose.yml:99-168`
- Modify: `Kavya/.env.example:265-285`
- Modify: `Kavya/SMARTPBX_RUNBOOK.md:143-185,304-335`
- Modify: `Kavya/tests/test_smartpbx_deployment.py:3184-3213`

**Interfaces:**
- Consumes: the four environment variable names from Task 1.
- Produces: an explicit SmartPBX Compose allowlist, documented defaults, a Claude rollback command, and deployment tests proving the settings are not explicitly mapped on the Twilio service. The Twilio service still consumes its existing `.env` through `env_file`; these settings are behaviorally inert outside SmartPBX, not process-environment-isolated.

- [ ] **Step 1: Write the failing environment-contract test**

Replace, rather than supplement, the existing section-scoped deployment
assertions in `test_sinhala_smartpbx_settings_are_explicit_and_secret_safe()`
and `test_sinhala_smartpbx_runbook_documents_the_closed_gemini_tts_contract()`
around lines 3184-3213. The replacement retains the Gemini-key safety check but
removes assertions that Sinhala retains Claude, Gemini is TTS-only, or that no
LLM fallback exists. Rename the latter test to describe the per-language LLM
contract and keep its `section = runbook.split(...)` extraction so it proves
the existing `## SmartPBX Sinhala menu and Gemini TTS` section itself changed.
Do not add a separate runbook test/section while leaving the stale one intact.

Use these additional assertions in the replaced tests:

```python
expected_llm = {
    "SMARTPBX_SINHALA_LLM_PROVIDER": "${SMARTPBX_SINHALA_LLM_PROVIDER:-gemini}",
    "SMARTPBX_SINHALA_GEMINI_LLM_MODEL": "${SMARTPBX_SINHALA_GEMINI_LLM_MODEL:-gemini-3.7-flash}",
    "SMARTPBX_SINHALA_GEMINI_THINKING_LEVEL": "${SMARTPBX_SINHALA_GEMINI_THINKING_LEVEL:-low}",
    "SMARTPBX_SINHALA_GEMINI_MAX_TOKENS": "${SMARTPBX_SINHALA_GEMINI_MAX_TOKENS:-600}",
}
for name, value in expected_llm.items():
    assert environment[name] == value

twilio_explicit_environment = compose["services"]["kavya"].get("environment", {})
for name in expected_llm:
    assert name not in twilio_explicit_environment

for name, default in (
    ("SMARTPBX_SINHALA_LLM_PROVIDER", "gemini"),
    ("SMARTPBX_SINHALA_GEMINI_LLM_MODEL", "gemini-3.7-flash"),
    ("SMARTPBX_SINHALA_GEMINI_THINKING_LEVEL", "low"),
    ("SMARTPBX_SINHALA_GEMINI_MAX_TOKENS", "600"),
):
    assert re.search(rf"^{name}={re.escape(default)}$", example, re.MULTILINE)
```

Within that replaced, section-scoped runbook test assert:

```python
def test_sinhala_gemini_llm_runbook_has_bounded_canary_and_claude_rollback():
    runbook = read_text("SMARTPBX_RUNBOOK.md")
    assert "gemini-3.7-flash" in runbook
    assert "thinking level `low`" in runbook
    assert "output ceiling `600`" in runbook
    assert "SMARTPBX_SINHALA_LLM_PROVIDER=claude" in runbook
    assert "Azure `si-LK` remains the live Sinhala STT" in runbook
    assert "Gemini Transcribe" in runbook and "offline-only" in runbook
    assert "Chirp 2" in runbook and "StreamingRecognize" in runbook
```

Also assert that the extracted section contains neither `Only its TTS uses
Gemini` nor `Sinhala retains the existing Claude LLM`; require the updated
Press-2 canary to name Gemini 3.7 Flash and Azure `si-LK`. Preserve the
existing key-presence command and its no-secret-printing contract.

- [ ] **Step 2: Push RED and capture missing allowlist evidence**

```bash
git add Kavya/tests/test_smartpbx_deployment.py
git commit -m "test(kavya): specify Sinhala Gemini deployment contract"
git push origin Rakesh
```

Expected GitHub Actions result: the Kavya deployment tests fail because the four settings and rollback documentation are absent.

- [ ] **Step 3: Add SmartPBX-only Compose values**

Under the existing Sinhala Gemini TTS values in `kavya-smartpbx.environment`, add:

```yaml
SMARTPBX_SINHALA_LLM_PROVIDER: "${SMARTPBX_SINHALA_LLM_PROVIDER:-gemini}"
SMARTPBX_SINHALA_GEMINI_LLM_MODEL: "${SMARTPBX_SINHALA_GEMINI_LLM_MODEL:-gemini-3.7-flash}"
SMARTPBX_SINHALA_GEMINI_THINKING_LEVEL: "${SMARTPBX_SINHALA_GEMINI_THINKING_LEVEL:-low}"
SMARTPBX_SINHALA_GEMINI_MAX_TOKENS: "${SMARTPBX_SINHALA_GEMINI_MAX_TOKENS:-600}"
```

Do not explicitly add them to the `kavya` Twilio service and do not add
`env_file` to `kavya-smartpbx`. Do not claim the values are absent from the
Twilio process: its existing service-wide `.env` import makes that untrue. The
SmartPBX guards in code are the behavioral isolation boundary.

- [ ] **Step 4: Add non-secret dotenv defaults and accurate comments**

In `Kavya/.env.example`, replace the complete existing Sinhala menu/TTS dotenv
block (including its Gemini-TTS-only comment) with exactly this single block;
also replace the stale Gemini API comment at current lines 22-32 so it says the
key is required for the SmartPBX Sinhala Gemini LLM **and** TTS path, not
TTS-only. Do not add a second set of assignments elsewhere:

```dotenv
# SmartPBX callers choose 1 for the unchanged English pipeline or 2 for the
# Sinhala Gemini LLM + Gemini TTS pipeline. Azure si-LK remains the live STT.
SMARTPBX_LANGUAGE_SELECTION_TIMEOUT_SECONDS=8.0
SMARTPBX_SINHALA_LLM_PROVIDER=gemini
SMARTPBX_SINHALA_GEMINI_LLM_MODEL=gemini-3.7-flash
SMARTPBX_SINHALA_GEMINI_THINKING_LEVEL=low
SMARTPBX_SINHALA_GEMINI_MAX_TOKENS=600
SMARTPBX_SINHALA_GEMINI_TTS_MODEL=gemini-3.1-flash-tts-preview
SMARTPBX_SINHALA_GEMINI_TTS_VOICE=Vindemiatrix
SMARTPBX_SINHALA_GEMINI_TTS_TIMEOUT_SECONDS=15.0
```

Keep `GEMINI_API_KEY=` blank and unique.

Extend the existing dotenv deployment helper tests to require exactly one active
assignment for each of the four `SMARTPBX_SINHALA_LLM_*` names as well as the
single blank `GEMINI_API_KEY`; a duplicate or later nonblank active assignment
must fail. Do not merely search for a matching line.

In the preceding `SMARTPBX_CLAUDE_MAX_TOKENS` comment block, replace the stale
`OpenAI and Gemini stay on SMARTPBX_MAX_TOKENS` sentence. State that Gemini 3.7
can consume thinking tokens, but English Gemini continues to use
`SMARTPBX_MAX_TOKENS` to preserve the existing English request contract; Sinhala
uses its separate session-owned ceiling. This mirrors the full server docstring
replacement from Task 3.

- [ ] **Step 5: Document behavior, evidence, and one-setting rollback**

Replace the existing `## SmartPBX Sinhala menu and Gemini TTS` section (not a
new section elsewhere) with concise text containing all of these operational
facts. Replace both stale claims at current lines 149-151 and 176-178: Press 2
must no longer be described as Claude plus Gemini TTS, and its canary must no
longer verify that obsolete path.

In the protected `.env.smartpbx` template already in this runbook, add the four
non-secret LLM settings with their documented defaults alongside the existing
Sinhala TTS settings. The section-scoped deployment test must assert those
template names occur once; this is separate from, and must retain, the blank
secret `GEMINI_API_KEY` assignment contract.

````markdown
## Sinhala per-language pipeline

Press 1 keeps the existing English STT -> Claude -> ElevenLabs path. Press 2
uses Azure `si-LK` STT -> Gemini `gemini-3.7-flash` at thinking level `low`
with output ceiling `600` -> Gemini `gemini-3.1-flash-tts-preview` voice
`Vindemiatrix`. Gemini technical failures reuse the existing call-local Claude
fallback; TTS remains Gemini and no other active call changes provider.

Rollback only the Sinhala LLM by setting:

```dotenv
SMARTPBX_SINHALA_LLM_PROVIDER=claude
```

Then recreate only `kavya-smartpbx` through the guarded image deployment
procedure. Do not edit `LLM_PROVIDER`; it is the English/global baseline.

Azure `si-LK` remains the live Sinhala STT. Chirp 2's general V2 table lists
Sinhala, but its method-specific `StreamingRecognize` list excludes Sinhala;
Gemini Transcribe Live also omits Sinhala. Both are offline-only evaluations.
````

- [ ] **Step 6: Syntax/config check, push GREEN, and inspect the rendered Compose service**

```bash
python3 -m py_compile Kavya/tests/test_smartpbx_deployment.py
docker compose -f Kavya/docker-compose.yml config --services
git diff --check -- Kavya/docker-compose.yml Kavya/.env.example Kavya/SMARTPBX_RUNBOOK.md Kavya/tests/test_smartpbx_deployment.py
git add Kavya/docker-compose.yml Kavya/.env.example Kavya/SMARTPBX_RUNBOOK.md Kavya/tests/test_smartpbx_deployment.py
git commit -m "docs(kavya): expose Sinhala Gemini LLM canary controls"
git push origin Rakesh
```

Expected: Compose parses and lists `kavya-smartpbx`; GitHub Actions returns the full Kavya suite green without exposing a secret value.

---

### Task 6: Reconcile project guidance and obtain final review evidence

**Files:**
- Modify: `Kavya/CLAUDE.md`
- Modify: `Kavya/AGENTS.md`
- Verify: all files from Tasks 1-5

**Interfaces:**
- Consumes: the completed behavior and runbook contract.
- Produces: exact `CLAUDE.md`/`AGENTS.md` parity, one reviewable PR head, full CI evidence, and no deployment.

- [ ] **Step 1: Update the canonical Kavya guidance with the final facts**

Add a short version entry to `Kavya/CLAUDE.md` stating:

```markdown
- SmartPBX IVR option 1 retains the existing English provider pipeline.
- SmartPBX IVR option 2 resolves a call-local Gemini 3.7 Flash LLM profile
  before STT starts; Azure si-LK STT and Gemini 3.1 Flash TTS remain in place.
- Gemini-to-Claude failover is technical-failure-only and call-local. The
  `SMARTPBX_SINHALA_LLM_PROVIDER=claude` setting is the Sinhala-only rollback.
- Chirp 2 and Gemini Transcribe are not live Sinhala STT options because their
  method-specific live-streaming support tables omit Sinhala.
```

Do not rewrite historical entries.

- [ ] **Step 2: Mirror the canonical guidance exactly**

```bash
cp Kavya/CLAUDE.md Kavya/AGENTS.md
cmp -s Kavya/CLAUDE.md Kavya/AGENTS.md
```

Expected: `cmp` exits `0`.

- [ ] **Step 3: Run all permitted local verification**

```bash
python3 -m py_compile Kavya/server.py Kavya/smartpbx_session.py Kavya/tests/test_smartpbx_sinhala_ivr.py Kavya/tests/test_smartpbx_server.py Kavya/tests/test_gemini_streaming.py Kavya/tests/test_prompt_policy.py Kavya/tests/test_smartpbx_deployment.py
git diff --check
git status --short
```

Expected: compilation and diff checks exit `0`; status shows only intended task files plus the pre-existing Graphify and older untracked-plan artifacts.

- [ ] **Step 4: Commit documentation parity and push the review head**

```bash
git add Kavya/CLAUDE.md Kavya/AGENTS.md
git commit -m "docs(kavya): record per-language SmartPBX LLM routing"
git push origin Rakesh
```

- [ ] **Step 5: Require full CI and senior review before merge**

Use the PR's GitHub Actions run as the behavioral verification. Acceptance requires:

```text
CI: every repository-required branch-protection check green
Kavya test job: green
Kavya image build/import: green
Review: no unresolved correctness, concurrency, privacy, or English-regression finding
Deployment: not started
```

Review the final diff against the approved design and reject any line that cannot be traced to provider isolation, session-owned Gemini controls, conversational Sinhala, fallback safety, or deployment documentation.

---

## Guarded Canary After Merge

This section is an operations handoff, not authorization to deploy during implementation.

1. Build and probe the immutable Kavya image for the reviewed merge SHA using the existing `probe-kavya-image.yml`, `build-kavya-image.yml`, and `scripts/deploy_smartpbx_image.sh` gates.
2. Confirm the live SmartPBX environment still selects Azure STT and contains the Gemini API key without printing the value.
3. Deploy only `kavya-smartpbx`; do not recreate the Twilio `kavya` service.
4. Verify `/health` returns SmartPBX mode and authenticated `/smartpbx/status` is healthy.
5. Place one Press-1 preservation call: greeting, general KB question, rate question, phone capture, and hangup. Compare first-media latency and errors with the pre-deploy stable baseline.
6. Place one Press-2 fluent-speaker call: general question, rate, availability tool, name, repeated-digit phone number, booking, barge-in, and hangup. Confirm natural Sinhala, correct slots, no English recovery leakage, no duplicate tool side effects, and Gemini TTS throughout.
7. Place simultaneous Press-1 and Press-2 calls. Confirm telemetry shows independent sessions and no provider/model/tool crossing.
8. Force a replay-safe Gemini failure in a controlled canary. Confirm one call falls back to Claude in Sinhala, Gemini TTS remains active, and the other call stays on its selected provider.
9. If Sinhala quality or reliability regresses, set `SMARTPBX_SINHALA_LLM_PROVIDER=claude` and recreate only `kavya-smartpbx`. If the service itself is unhealthy, use the existing immutable-image rollback.

## Official Implementation Sources

- Gemini 3.7 Flash model and supported thinking levels: https://ai.google.dev/gemini-api/docs/models/gemini-3.7-flash
- Gemini 3.7 migration constraints and deprecated sampling parameters: https://ai.google.dev/gemini-api/docs/latest-model
- Gemini 3.7 Generate Content migration checklist, including function-response call IDs: https://ai.google.dev/gemini-api/docs/generate-content/latest-model
- Gemini thinking-level request shape: https://ai.google.dev/gemini-api/docs/generate-content/thinking
- Gemini function calling: https://ai.google.dev/gemini-api/docs/function-calling
- Google Gen AI Python SDK `FunctionCall.id` and `FunctionResponse.id`: https://googleapis.github.io/python-genai/
- Gemini Transcribe Live supported languages: https://ai.google.dev/gemini-api/docs/live-api/live-transcribe#supported-languages
- Chirp 2 method-specific streaming language support: https://docs.cloud.google.com/speech-to-text/docs/models/chirp-2#language-availability
- General Speech-to-Text V2 `si-LK` model table: https://docs.cloud.google.com/speech-to-text/docs/speech-to-text-supported-languages
- Azure Speech `si-LK` support: https://learn.microsoft.com/en-us/azure/ai-services/speech-service/language-support
