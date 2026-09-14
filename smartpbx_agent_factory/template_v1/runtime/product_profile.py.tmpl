"""Reviewed product configuration loaded once before a call is admitted.

The renderer writes ``product_profile.json`` from an approved manifest and
knowledge review. This module reads that generated file only during service
construction; calls receive the already-frozen ProductProfile instance.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping


@dataclass(frozen=True)
class LanguageProfile:
    code: str
    locale: str
    stt: str
    stt_model: str | None
    llm: str
    llm_model: str | None
    tts: str
    tts_model: str | None
    fallback: str | None
    fallback_model: str | None
    prompt_block: str
    greeting: str = ""
    menu_prompt: str = ""
    recovery_line: str = ""
    reprompt: str = ""
    filler_phrases: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProductProfile:
    """Tenant-owned reviewed data, isolated from conversational mechanics."""

    display_name: str
    public_name: str
    agent_name: str
    industry: str
    purpose: str
    audience: str
    language_profiles: Mapping[str, LanguageProfile]
    default_language: str
    allowed_topics: tuple[str, ...]
    refused_topics: tuple[str, ...]
    knowledge_paths: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "language_profiles", MappingProxyType(dict(self.language_profiles)))

    def language(self, code: str) -> LanguageProfile:
        try:
            return self.language_profiles[code]
        except KeyError as exc:
            raise ValueError("requested language is not configured") from exc


def load_product_profile(path: Path) -> ProductProfile:
    """Load generated reviewed configuration once during service construction."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping):
        raise ValueError("product profile must be an object")
    identity = _identity(raw.get("identity"))
    languages = raw.get("languages")
    if not isinstance(languages, Mapping) or not languages:
        raise ValueError("product profile requires language profiles")
    parsed_languages: dict[str, LanguageProfile] = {}
    for code, value in languages.items():
        if not isinstance(code, str) or not isinstance(value, Mapping):
            raise ValueError("invalid language profile")
        strings = {name: value.get(name) for name in ("locale", "stt", "llm", "tts", "prompt_block", "greeting", "menu_prompt", "recovery_line", "reprompt")}
        if not all(isinstance(item, str) for item in strings.values()):
            raise ValueError("invalid language profile")
        parsed_languages[code] = LanguageProfile(
            code=code,
            locale=strings["locale"],
            stt=strings["stt"],
            stt_model=_model(value.get("stt_model")),
            llm=strings["llm"],
            llm_model=_model(value.get("llm_model")),
            tts=strings["tts"],
            tts_model=_model(value.get("tts_model")),
            fallback=_provider(value.get("fallback")),
            fallback_model=_model(value.get("fallback_model")),
            prompt_block=strings["prompt_block"],
            greeting=strings["greeting"],
            menu_prompt=strings["menu_prompt"],
            recovery_line=strings["recovery_line"],
            reprompt=strings["reprompt"],
            filler_phrases=_strings(value.get("filler_phrases"), "filler_phrases"),
        )
    default_language = raw.get("default_language")
    if not isinstance(default_language, str) or default_language not in parsed_languages:
        raise ValueError("product profile default language is invalid")
    return ProductProfile(
        language_profiles=parsed_languages,
        default_language=default_language,
        allowed_topics=_strings(raw.get("allowed_topics"), "allowed_topics"),
        refused_topics=_strings(raw.get("refused_topics"), "refused_topics"),
        knowledge_paths=_strings(raw.get("knowledge_paths"), "knowledge_paths"),
        **identity,
    )


def test_product_profile() -> ProductProfile:
    """A content-free profile used only by the disposable lifecycle contract."""
    language = LanguageProfile("en", "en-US", "synthetic", None, "synthetic", None, "synthetic", None, None, None, "Answer approved inquiries.", "Hello.", "", "I am sorry, please try again.", "Are you still there?", ("One moment, please.",))
    return ProductProfile("Inquiry Agent", "Inquiry Agent", "Inquiry Agent", "general", "Answer inquiries", "test callers", {"en": language}, "en", (), (), ())


def _identity(value: object) -> dict[str, str]:
    if not isinstance(value, Mapping) or set(value) != {"display_name", "public_name", "agent_name", "industry", "purpose", "audience"}:
        raise ValueError("product profile identity is invalid")
    if not all(isinstance(item, str) and item for item in value.values()):
        raise ValueError("product profile identity is invalid")
    return {key: value for key, value in value.items() if isinstance(value, str)}


def _model(value: object) -> str | None:
    if value is not None and (not isinstance(value, str) or not value):
        raise ValueError("product profile provider model is invalid")
    return value


def _provider(value: object) -> str | None:
    if value is not None and (not isinstance(value, str) or not value):
        raise ValueError("product profile fallback provider is invalid")
    return value


def _strings(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise ValueError(f"product profile {label} is invalid")
    return tuple(value)
