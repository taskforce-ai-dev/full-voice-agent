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
    required_metadata_identifiers: tuple[str, ...] = ()
    generated_runnable: bool = True


@dataclass(frozen=True)
class CapabilityCatalogue:
    """Reviewed capability data; account identifiers are non-secret metadata."""

    version: int
    source_revision: str
    source_hashes: Mapping[str, str]
    runtime_required_secret_identifiers: tuple[str, ...]
    runtime_required_metadata_identifiers: tuple[str, ...]
    manifest_selection_required: bool
    source_defaults: Mapping[str, Mapping[str, object]]
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
        object.__setattr__(self, "runtime_required_metadata_identifiers", tuple(self.runtime_required_metadata_identifiers))
        object.__setattr__(self, "source_defaults", MappingProxyType({
            language: MappingProxyType(dict(value)) for language, value in self.source_defaults.items()
        }))
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
            "runtime_required_metadata_identifiers",
            "manifest_selection_required",
            "source_defaults",
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
        runtime_metadata_identifiers = _parse_secret_identifiers(
            raw.get("runtime_required_metadata_identifiers"), "catalogue runtime metadata"
        )
        if set(runtime_secret_identifiers) & set(runtime_metadata_identifiers):
            raise CatalogueError("catalogue runtime identifiers cannot be both secret and metadata")
        manifest_selection_required = raw.get("manifest_selection_required")
        if manifest_selection_required is not True:
            raise CatalogueError("catalogue must require explicit manifest pipeline selection")
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
        defaults = raw.get("source_defaults")
        if not isinstance(defaults, Mapping) or set(defaults) != set(parsed):
            raise CatalogueError("catalogue source_defaults must cover exactly the supported languages")
        temporary = cls(
            1, source_revision, parsed_hashes, runtime_secret_identifiers,
            runtime_metadata_identifiers, manifest_selection_required, {}, parsed, status,
        )
        parsed_defaults: dict[str, Mapping[str, object]] = {}
        for language, default in defaults.items():
            if not isinstance(default, Mapping):
                raise CatalogueError(f"catalogue source default is invalid for {language}")
            if default.get("evidence") != "source-default":
                raise CatalogueError(f"catalogue source default must be source-default for {language}")
            temporary._pipeline_entries(language, default)
            parsed_defaults[language] = dict(default)
        return cls(
            1, source_revision, parsed_hashes, runtime_secret_identifiers,
            runtime_metadata_identifiers, manifest_selection_required, parsed_defaults, parsed, status,
        )

    def validate_pipeline(self, language: str, pipeline: Mapping[str, object]) -> None:
        self._pipeline_entries(language, pipeline)

    def validate_generated_pipeline(self, language: str, pipeline: Mapping[str, object]) -> None:
        entries = self._pipeline_entries(language, pipeline)
        if any(not entry.generated_runnable for entry in entries):
            raise CatalogueError(f"provider pipeline is source-observed but not generated-runnable for {language}")

    def required_secret_identifiers_for_pipeline(self, language: str, pipeline: Mapping[str, object]) -> frozenset[str]:
        """Return only configured secret names after validating the full selection."""
        entries = self._pipeline_entries(language, pipeline)
        return frozenset(self.runtime_required_secret_identifiers).union(
            identifier for entry in entries for identifier in entry.required_secret_identifiers
        )

    def required_metadata_identifiers_for_pipeline(self, language: str, pipeline: Mapping[str, object]) -> frozenset[str]:
        entries = self._pipeline_entries(language, pipeline)
        return frozenset(self.runtime_required_metadata_identifiers).union(
            identifier for entry in entries for identifier in entry.required_metadata_identifiers
        )

    def source_default(self, language: str) -> Mapping[str, object]:
        try:
            return self.source_defaults[language]
        except KeyError as exc:
            raise CatalogueError(f"source default is not verified: {language}") from exc

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
        fallback_model = pipeline.get("fallback_model")
        requires_fallback = selected[1].provider == "gemini" and bool(approved["fallback"])
        if fallback is None:
            if fallback_model is not None:
                raise CatalogueError(f"provider pipeline is not verified for {language}: fallback")
            if requires_fallback:
                raise CatalogueError(f"provider pipeline is not verified for {language}: fallback")
        else:
            match = next(
                (
                    entry
                    for entry in approved["fallback"]
                    if entry.provider == fallback and entry.model == (fallback_model if isinstance(fallback_model, str) else None)
                ),
                None,
            )
            if not isinstance(fallback, str) or match is None:
                raise CatalogueError(f"provider pipeline is not verified for {language}: fallback")
            selected.append(match)
        return tuple(selected)


def _parse_provider_model(raw: object, language: str, locale: str, component: str) -> ProviderModel:
    if not isinstance(raw, Mapping) or set(raw) != {
        "provider", "model", "required_secret_identifiers", "required_metadata_identifiers", "generated_runnable"
    }:
        raise CatalogueError(f"catalogue {language}.{locale}.{component} has an invalid provider/model pair")
    provider = raw["provider"]
    model = raw["model"]
    identifiers = raw["required_secret_identifiers"]
    metadata_identifiers = raw["required_metadata_identifiers"]
    generated_runnable = raw["generated_runnable"]
    if not isinstance(provider, str) or not provider or not (isinstance(model, str) and model or model is None):
        raise CatalogueError(f"catalogue {language}.{locale}.{component} has an invalid provider/model pair")
    secrets = _parse_secret_identifiers(identifiers, f"catalogue {language}.{locale}.{component}")
    metadata = _parse_secret_identifiers(metadata_identifiers, f"catalogue {language}.{locale}.{component} metadata")
    if set(secrets) & set(metadata):
        raise CatalogueError(f"catalogue {language}.{locale}.{component} identifiers cannot be both secret and metadata")
    if not isinstance(generated_runnable, bool):
        raise CatalogueError(f"catalogue {language}.{locale}.{component} generated_runnable must be boolean")
    return ProviderModel(provider, model, secrets, metadata, generated_runnable)


def _parse_secret_identifiers(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(
        not isinstance(identifier, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]*", identifier)
        for identifier in value
    ):
        raise CatalogueError(f"{label} has invalid secret identifiers")
    if len(set(value)) != len(value):
        raise CatalogueError(f"{label} has duplicate secret identifiers")
    return tuple(value)
