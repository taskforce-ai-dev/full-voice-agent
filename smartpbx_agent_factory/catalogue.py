"""Versioned, explicit provider/language capability catalogue."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping


class CatalogueError(ValueError):
    """Raised when a provider pipeline is not explicitly verified."""


@dataclass(frozen=True)
class ProviderModel:
    provider: str
    model: str


@dataclass(frozen=True)
class CapabilityCatalogue:
    version: int
    languages: Mapping[str, Mapping[str, Mapping[str, tuple[ProviderModel, ...] | tuple[str, ...]]]]
    status: str = ""

    def __post_init__(self) -> None:
        frozen = {
            language: MappingProxyType(
                {
                    locale: MappingProxyType(
                        {component: tuple(providers) for component, providers in pipeline.items()}
                    )
                    for locale, pipeline in locales.items()
                }
            )
            for language, locales in self.languages.items()
        }
        object.__setattr__(self, "languages", MappingProxyType(frozen))

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
        status = raw.get("catalogue_status", "")
        if not isinstance(status, str):
            raise CatalogueError("catalogue_status must be a string")
        if status != "approved":
            raise CatalogueError("catalogue is not approved for operational use")
        languages = raw.get("languages")
        if not isinstance(languages, Mapping) or not languages:
            raise CatalogueError("catalogue languages must be a non-empty object")
        parsed: dict[str, dict[str, dict[str, tuple[ProviderModel, ...] | tuple[str, ...]]]] = {}
        for language, raw_locales in languages.items():
            if not isinstance(language, str) or not language:
                raise CatalogueError("catalogue language names must be non-empty strings")
            if not isinstance(raw_locales, Mapping):
                raise CatalogueError(f"catalogue language {language} must be an object")
            if set(raw_locales) != {"locales"}:
                raise CatalogueError(f"catalogue language {language} must contain locales only")
            locales = raw_locales["locales"]
            if not isinstance(locales, Mapping) or not locales:
                raise CatalogueError(f"catalogue language {language}.locales must be a non-empty object")
            parsed_locales: dict[str, dict[str, tuple[ProviderModel, ...] | tuple[str, ...]]] = {}
            for locale, pipeline in locales.items():
                if not isinstance(locale, str) or not locale:
                    raise CatalogueError(f"catalogue {language} locale names must be non-empty strings")
                if not isinstance(pipeline, Mapping) or set(pipeline) - {"stt", "llm", "tts", "fallback"}:
                    raise CatalogueError(f"catalogue {language}.{locale} has an invalid pipeline")
                parsed_pipeline: dict[str, tuple[ProviderModel, ...] | tuple[str, ...]] = {}
                for component in ("stt", "llm", "tts"):
                    combinations = pipeline.get(component)
                    if not isinstance(combinations, (list, tuple)) or not combinations:
                        raise CatalogueError(f"catalogue {language}.{locale}.{component} must be a non-empty array")
                    entries: list[ProviderModel] = []
                    for combination in combinations:
                        if not isinstance(combination, Mapping) or set(combination) != {"provider", "model"}:
                            raise CatalogueError(f"catalogue {language}.{locale}.{component} has an invalid provider/model pair")
                        provider = combination["provider"]
                        model = combination["model"]
                        if not isinstance(provider, str) or not provider or not isinstance(model, str) or not model:
                            raise CatalogueError(f"catalogue {language}.{locale}.{component} has an invalid provider/model pair")
                        entries.append(ProviderModel(provider, model))
                    if len(set(entries)) != len(entries):
                        raise CatalogueError(f"catalogue {language}.{locale}.{component} has duplicate provider/model pairs")
                    parsed_pipeline[component] = tuple(entries)
                fallback = pipeline.get("fallback", ())
                if not isinstance(fallback, (list, tuple)) or any(
                    not isinstance(provider, str) or not provider for provider in fallback
                ):
                    raise CatalogueError(f"catalogue {language}.{locale}.fallback has an invalid provider")
                parsed_pipeline["fallback"] = tuple(fallback)
                parsed_locales[locale] = parsed_pipeline
            parsed[language] = parsed_locales
        return cls(1, parsed, status)

    def validate_pipeline(self, language: str, pipeline: Mapping[str, object]) -> None:
        if self.status != "approved":
            raise CatalogueError("catalogue is not approved for operational use")
        if language not in self.languages:
            raise CatalogueError(f"language/provider pipeline is not verified: {language}")
        if not isinstance(pipeline, Mapping):
            raise CatalogueError("provider pipeline must be an object")
        locale = pipeline.get("locale")
        if not isinstance(locale, str) or locale not in self.languages[language]:
            raise CatalogueError(f"language/provider pipeline is not verified: {language}")
        approved = self.languages[language][locale]
        for component in ("stt", "llm", "tts"):
            provider = pipeline.get(component)
            model = pipeline.get(f"{component}_model")
            combinations = approved[component]
            if (
                not isinstance(provider, str)
                or not isinstance(model, str)
                or ProviderModel(provider, model) not in combinations
            ):
                raise CatalogueError(f"provider pipeline is not verified for {language}: {component}")
        fallback = pipeline.get("fallback")
        if fallback is not None and (not isinstance(fallback, str) or fallback not in approved["fallback"]):
            raise CatalogueError(f"provider pipeline is not verified for {language}: fallback")
