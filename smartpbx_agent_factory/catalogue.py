"""Versioned, explicit provider/language capability catalogue."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


class CatalogueError(ValueError):
    """Raised when a provider pipeline is not explicitly verified."""


@dataclass(frozen=True)
class CapabilityCatalogue:
    version: int
    languages: Mapping[str, Mapping[str, tuple[str, ...]]]
    status: str = ""

    @classmethod
    def load(cls, path: Path) -> "CapabilityCatalogue":
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CatalogueError(f"cannot load catalogue: {exc}") from exc
        if not isinstance(raw, Mapping):
            raise CatalogueError("catalogue must be an object")
        if raw.get("version") != 1:
            raise CatalogueError("catalogue version must be 1")
        languages = raw.get("languages")
        if not isinstance(languages, Mapping) or not languages:
            raise CatalogueError("catalogue languages must be a non-empty object")
        parsed: dict[str, dict[str, tuple[str, ...]]] = {}
        for language, pipeline in languages.items():
            if not isinstance(language, str) or not language:
                raise CatalogueError("catalogue language names must be non-empty strings")
            if not isinstance(pipeline, Mapping):
                raise CatalogueError(f"catalogue language {language} must be an object")
            parsed_pipeline: dict[str, tuple[str, ...]] = {}
            for component in ("stt", "llm", "tts", "fallback"):
                providers = pipeline.get(component, ())
                if not isinstance(providers, (list, tuple)):
                    raise CatalogueError(f"catalogue {language}.{component} must be an array")
                if any(not isinstance(provider, str) or not provider for provider in providers):
                    raise CatalogueError(f"catalogue {language}.{component} has an invalid provider")
                parsed_pipeline[component] = tuple(providers)
            if any(not parsed_pipeline[name] for name in ("stt", "llm", "tts")):
                raise CatalogueError(f"catalogue {language} must list stt, llm and tts providers")
            parsed[language] = parsed_pipeline
        status = raw.get("catalogue_status", "")
        if not isinstance(status, str):
            raise CatalogueError("catalogue_status must be a string")
        return cls(1, parsed, status)

    def validate_pipeline(self, language: str, pipeline: Mapping[str, object]) -> None:
        if language not in self.languages:
            raise CatalogueError(f"language/provider pipeline is not verified: {language}")
        if not isinstance(pipeline, Mapping):
            raise CatalogueError("provider pipeline must be an object")
        approved = self.languages[language]
        for component in ("stt", "llm", "tts"):
            provider = pipeline.get(component)
            if not isinstance(provider, str) or provider not in approved[component]:
                raise CatalogueError(f"provider pipeline is not verified for {language}: {component}")
        fallback = pipeline.get("fallback")
        if fallback is not None and (not isinstance(fallback, str) or fallback not in approved["fallback"]):
            raise CatalogueError(f"provider pipeline is not verified for {language}: fallback")
