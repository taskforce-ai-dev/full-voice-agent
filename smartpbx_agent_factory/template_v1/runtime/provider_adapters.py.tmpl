"""Provider-neutral STT, LLM, and TTS boundaries for the conversation core."""

from __future__ import annotations

from collections.abc import AsyncIterable, Callable
from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from product_profile import LanguageProfile, ProductProfile


@dataclass(frozen=True)
class RecognizerResult:
    """One ordered recognizer observation delivered to a call-local callback."""

    text: str
    is_final: bool
    result_id: str | int | None = None
    audio_offset: int | None = None
    audio_duration: int | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.text, str)
            or not isinstance(self.is_final, bool)
            or (
                self.result_id is not None
                and (
                    not isinstance(self.result_id, str | int)
                    or isinstance(self.result_id, bool)
                    or isinstance(self.result_id, str) and len(self.result_id) > 256
                )
            )
            or self.audio_offset is not None and (not isinstance(self.audio_offset, int) or isinstance(self.audio_offset, bool) or self.audio_offset < 0)
            or self.audio_duration is not None and (not isinstance(self.audio_duration, int) or isinstance(self.audio_duration, bool) or self.audio_duration < 0)
        ):
            raise ValueError("invalid recognizer result")

    def duplicate_identity(self) -> tuple[str | int, bool] | None:
        """Return provider identity only; text is never a final dedupe basis."""
        if self.result_id is None:
            return None
        return (self.result_id, self.is_final)

    @property
    def audio_interval(self) -> tuple[int, int] | None:
        if self.audio_offset is None or self.audio_duration is None or self.audio_duration <= 0:
            return None
        return self.audio_offset, self.audio_offset + self.audio_duration


@dataclass(frozen=True)
class RecognizerFatal:
    """Bounded provider failure class; never carries provider payload or text."""

    reason: str

    def __post_init__(self) -> None:
        if self.reason not in {
            "provider_unavailable",
            "provider_timeout",
            "provider_stream_aborted",
            "provider_protocol_error",
        }:
            raise ValueError("invalid recognizer fatal class")


RecognizerEvent = RecognizerResult | RecognizerFatal
RecognizerCallback = Callable[[RecognizerEvent], None]


class ContinuousRecognizer(Protocol):
    """Provider-owned streaming recognizer for exactly one session/language."""

    async def feed_audio(self, audio: bytes) -> None:
        """Accept one audio frame without creating a new recognition request."""

    async def close(self) -> None:
        """Stop callbacks and release provider resources exactly once."""


class STTAdapter(Protocol):
    active: bool

    async def start_recognizer(
        self, language: str, on_result: RecognizerCallback
    ) -> ContinuousRecognizer:
        """Start one continuous recognizer; callbacks may arrive off-loop."""


class LLMAdapter(Protocol):
    active: bool

    async def generate_response(
        self, transcript: str, language: str, prompt: str
    ) -> str | AsyncIterable["LLMStreamEvent"]:
        """Return one atomic response or a generation-scoped streaming response."""


@dataclass(frozen=True)
class ProvisionalSentence:
    """A complete sentence that may be spoken before terminal commit."""

    generation: int
    text: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.generation, int)
            or isinstance(self.generation, bool)
            or self.generation < 0
            or not isinstance(self.text, str)
        ):
            raise ValueError("provisional sentence is invalid")


@dataclass(frozen=True)
class ThinkingProgress:
    """Non-spoken provider progress for a generation."""

    generation: int


class RoundOutcome(str, Enum):
    """The normalized terminal state for an uncommitted provider attempt."""

    COMPLETED = "completed"
    TRUE_EMPTY = "true_empty"
    MAX_TOKENS_TRUNCATED = "max_tokens_truncated"
    STREAM_ABORTED = "stream_aborted"
    INCOMPLETE_TOOL_BLOCK = "incomplete_tool_block"
    MALFORMED_TOOL_JSON = "malformed_tool_json"
    TIMEOUT = "timeout"
    ABORTED = "aborted"


@dataclass(frozen=True)
class TerminalCommit:
    """The provider completed one generation; prior speech may enter history."""

    generation: int
    metadata: object | None = None


@dataclass(frozen=True)
class GenerationFence:
    """Discard one unsafe generation before an adapter-owned single retry."""

    generation: int
    reason: RoundOutcome
    retrying: bool


@dataclass(frozen=True)
class RecoveryBoundary:
    """A failed generation has no further response stream to commit."""

    generation: int
    reason: RoundOutcome
    retrying: bool = False


LLMStreamEvent = (
    ProvisionalSentence
    | ThinkingProgress
    | TerminalCommit
    | GenerationFence
    | RecoveryBoundary
)


class TTSAdapter(Protocol):
    active: bool

    async def synthesize_audio(
        self, response: str, language: str
    ) -> bytes | AsyncIterable[bytes]:
        """Return encoded SmartPBX audio or a streaming sequence of chunks."""


class ConversationProviderAdapter(STTAdapter, LLMAdapter, TTSAdapter, Protocol):
    """One injected provider bundle; no key, model, or environment lookup lives here."""


class Retriever(Protocol):
    async def retrieve(self, transcript: str, profile: ProductProfile) -> str:
        """Return reviewed knowledge context for an inquiry, without side effects."""


class Toolset(Protocol):
    """Reserved explicit seam; inquiry-only generated profiles pass no toolset."""


class HandoffHook(Protocol):
    async def handoff(self, reason: str) -> None:
        """Reserved explicit seam; inquiry-only generated profiles pass no hook."""


class AfterCallHook(Protocol):
    async def complete(self, *, language: LanguageProfile, turns: int) -> None:
        """Optional metadata-only hook; it receives no transcript or audio payload."""
