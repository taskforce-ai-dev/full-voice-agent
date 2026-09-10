"""Versioned, source-pinned provider and language capability catalogue."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping


class CatalogueError(ValueError):
    """Raised when a provider pipeline is not explicitly verified."""


@dataclass(frozen=True)
class ProviderModel:
    """One source-proven provider selection, with identifiers but never values."""

    provider: str
    model: str | None
    required_secret_identifiers: tuple[str, ...] = ()


@dataclass(frozen=True)
class CapabilityCatalogue:
    version: int
    source_revision: str
    source_hashes: Mapping[str, str]
    runtime_required_secret_identifiers: tuple[str, ...]
    languages: Mapping[str, Mapping[str, Mapping[str, tuple[ProviderModel, ...]]]]
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
        object.__setattr__(self, "source_hashes", MappingProxyType(dict(self.source_hashes)))
        object.__setattr__(self, "runtime_required_secret_identifiers", tuple(self.runtime_required_secret_identifiers))
        object.__setattr__(self, "languages", MappingProxyType(frozen))

    @classmethod
    def load(cls, path: Path) -> "CapabilityCatalogue":
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CatalogueError(f"cannot load catalogue: {exc}") from exc
        if not isinstance(raw, Mapping):
            raise CatalogueError("catalogue must be an object")
        unknown = set(raw) - {
            "version",
            "catalogue_status",
            "source_revision",
            "source_hashes",
            "runtime_required_secret_identifiers",
            "languages",
        }
        if unknown:
            raise CatalogueError(f"catalogue contains an unknown key: {sorted(unknown)[0]}")
        if raw.get("version") != 1:
            raise CatalogueError("catalogue version must be 1")
        status = raw.get("catalogue_status", "")
        if status != "approved":
            raise CatalogueError("catalogue is not approved for operational use")
        source_revision = raw.get("source_revision")
        if not isinstance(source_revision, str) or not re.fullmatch(r"[0-9a-f]{40}", source_revision):
            raise CatalogueError("catalogue source_revision must be a full 40-character revision")
        source_hashes = raw.get("source_hashes")
        if not isinstance(source_hashes, Mapping) or not source_hashes:
            raise CatalogueError("catalogue source_hashes must be a non-empty object")
        parsed_hashes: dict[str, str] = {}
        for source_path, digest in source_hashes.items():
            if not isinstance(source_path, str) or not source_path or not isinstance(digest, str):
                raise CatalogueError("catalogue source_hashes contains an invalid entry")
            if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
                raise CatalogueError("catalogue source_hashes contains an invalid digest")
            parsed_hashes[source_path] = digest
        runtime_secret_identifiers = _parse_secret_identifiers(
            raw.get("runtime_required_secret_identifiers"), "catalogue runtime"
        )
        languages = raw.get("languages")
        if not isinstance(languages, Mapping) or not languages:
            raise CatalogueError("catalogue languages must be a non-empty object")
        parsed: dict[str, dict[str, dict[str, tuple[ProviderModel, ...]]]] = {}
        for language, raw_locales in languages.items():
            if not isinstance(language, str) or not language:
                raise CatalogueError("catalogue language names must be non-empty strings")
            if not isinstance(raw_locales, Mapping) or set(raw_locales) != {"locales"}:
                raise CatalogueError(f"catalogue language {language} must contain locales only")
            locales = raw_locales["locales"]
            if not isinstance(locales, Mapping) or not locales:
                raise CatalogueError(f"catalogue language {language}.locales must be a non-empty object")
            parsed_locales: dict[str, dict[str, tuple[ProviderModel, ...]]] = {}
            for locale, pipeline in locales.items():
                if not isinstance(locale, str) or not locale or not isinstance(pipeline, Mapping):
                    raise CatalogueError(f"catalogue {language} contains an invalid locale pipeline")
                if set(pipeline) - {"stt", "llm", "tts", "fallback"}:
                    raise CatalogueError(f"catalogue {language}.{locale} has an invalid pipeline")
                parsed_pipeline: dict[str, tuple[ProviderModel, ...]] = {}
                for component in ("stt", "llm", "tts"):
                    combinations = pipeline.get(component)
                    if not isinstance(combinations, list) or not combinations:
                        raise CatalogueError(f"catalogue {language}.{locale}.{component} must be a non-empty array")
                    entries = tuple(_parse_provider_model(combination, language, locale, component) for combination in combinations)
                    if len(set(entries)) != len(entries):
                        raise CatalogueError(f"catalogue {language}.{locale}.{component} has duplicate provider/model pairs")
                    parsed_pipeline[component] = entries
                fallback = pipeline.get("fallback", [])
                if not isinstance(fallback, list):
                    raise CatalogueError(f"catalogue {language}.{locale}.fallback must be an array")
                parsed_pipeline["fallback"] = tuple(
                    _parse_provider_model(combination, language, locale, "fallback") for combination in fallback
                )
                parsed_locales[locale] = parsed_pipeline
            parsed[language] = parsed_locales
        return cls(1, source_revision, parsed_hashes, runtime_secret_identifiers, parsed, status)

    def validate_pipeline(self, language: str, pipeline: Mapping[str, object]) -> None:
        self._pipeline_entries(language, pipeline)

    def required_secret_identifiers_for_pipeline(self, language: str, pipeline: Mapping[str, object]) -> frozenset[str]:
        """Return only configured secret names after validating the full selection."""
        entries = self._pipeline_entries(language, pipeline)
        return frozenset(self.runtime_required_secret_identifiers).union(
            identifier for entry in entries for identifier in entry.required_secret_identifiers
        )

    def _pipeline_entries(self, language: str, pipeline: Mapping[str, object]) -> tuple[ProviderModel, ...]:
        if self.status != "approved":
            raise CatalogueError("catalogue is not approved for operational use")
        if language not in self.languages or not isinstance(pipeline, Mapping):
            raise CatalogueError(f"language/provider pipeline is not verified: {language}")
        locale = pipeline.get("locale")
        if not isinstance(locale, str) or locale not in self.languages[language]:
            raise CatalogueError(f"language/provider pipeline is not verified: {language}")
        approved = self.languages[language][locale]
        selected: list[ProviderModel] = []
        for component in ("stt", "llm", "tts"):
            provider = pipeline.get(component)
            model = pipeline.get(f"{component}_model")
            if not isinstance(provider, str):
                raise CatalogueError(f"provider pipeline is not verified for {language}: {component}")
            match = next(
                (
                    entry
                    for entry in approved[component]
                    if entry.provider == provider and entry.model == (model if isinstance(model, str) else None)
                ),
                None,
            )
            if match is None:
                raise CatalogueError(f"provider pipeline is not verified for {language}: {component}")
            selected.append(match)
        fallback = pipeline.get("fallback")
        if fallback is not None and (not isinstance(fallback, str) or fallback not in {item.provider for item in approved["fallback"]}):
            raise CatalogueError(f"provider pipeline is not verified for {language}: fallback")
        return tuple(selected)


def _parse_provider_model(raw: object, language: str, locale: str, component: str) -> ProviderModel:
    if not isinstance(raw, Mapping) or set(raw) != {"provider", "model", "required_secret_identifiers"}:
        raise CatalogueError(f"catalogue {language}.{locale}.{component} has an invalid provider/model pair")
    provider = raw["provider"]
    model = raw["model"]
    identifiers = raw["required_secret_identifiers"]
    if not isinstance(provider, str) or not provider or not (isinstance(model, str) and model or model is None):
        raise CatalogueError(f"catalogue {language}.{locale}.{component} has an invalid provider/model pair")
    return ProviderModel(provider, model, _parse_secret_identifiers(identifiers, f"catalogue {language}.{locale}.{component}"))


def _parse_secret_identifiers(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(
        not isinstance(identifier, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]*", identifier)
        for identifier in value
    ):
        raise CatalogueError(f"{label} has invalid secret identifiers")
    if len(set(value)) != len(value):
        raise CatalogueError(f"{label} has duplicate secret identifiers")
    return tuple(value)
