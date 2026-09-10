"""Strict manifest parsing and canonical digesting."""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import asdict
from pathlib import Path
from typing import Any, Mapping, Sequence

from .model import (
    AgentManifest,
    Capability,
    CapabilitySelection,
    KnowledgeSource,
    LanguageProfile,
    OperationsInput,
    PiiPolicy,
    SmartPBXInput,
    WebsiteDemoInput,
)


class ManifestError(ValueError):
    """Raised when an onboarding manifest is invalid or unsafe."""


TOP_LEVEL_KEYS = frozenset(
    {
        "schema_version",
        "display_name",
        "public_name",
        "slug",
        "agent_name",
        "industry",
        "purpose",
        "audience",
        "profile",
        "timezone",
        "operating_hours",
        "technical_owner",
        "languages",
        "allowed_topics",
        "refused_topics",
        "pii_policy",
        "capabilities",
        "knowledge_sources",
        "smartpbx",
        "operations",
        "website_demo",
    }
)
_CAPABILITIES = (
    "booking",
    "handover",
    "whatsapp",
    "crm",
    "payment",
    "post_call_reporting",
    "recording",
    "transcript_retention",
)
_CAPABILITY_KEYS = frozenset({"enabled", "destination", "fallback", "identifier", "details"})
_SLUG_RE = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ManifestError(f"{label} must be an object")
    return value


def _strict_keys(value: Mapping[str, object], allowed: set[str] | frozenset[str], label: str) -> None:
    unknown = sorted(set(value) - set(allowed))
    if unknown:
        raise ManifestError(f"unknown key: {label}.{unknown[0]}")


def _text(value: object, label: str, *, required: bool = True) -> str:
    if value is None and not required:
        return ""
    if not isinstance(value, str) or not value.strip():
        raise ManifestError(f"{label} must be a non-empty string")
    if "\x00" in value:
        raise ManifestError(f"{label} contains NUL")
    return value.strip()


def _optional_text(value: object, label: str) -> str | None:
    if value is None or value == "":
        return None
    return _text(value, label)


def _bool(value: object, label: str, *, default: bool | None = None) -> bool:
    if value is None and default is not None:
        return default
    if not isinstance(value, bool):
        raise ManifestError(f"{label} must be a boolean")
    return value


def _int(value: object, label: str, *, minimum: int | None = None, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ManifestError(f"{label} must be an integer")
    if minimum is not None and value < minimum:
        raise ManifestError(f"{label} must be at least {minimum}")
    if maximum is not None and value > maximum:
        raise ManifestError(f"{label} must be at most {maximum}")
    return value


def _text_tuple(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ManifestError(f"{label} must be an array")
    return tuple(_text(item, f"{label}[{index}]") for index, item in enumerate(value))


def validate_slug(slug: str) -> str:
    if not isinstance(slug, str) or not _SLUG_RE.fullmatch(slug) or len(slug) > 48:
        raise ManifestError("slug must be lowercase kebab-case, start with a letter, and be at most 48 characters")
    return slug


def _parse_language(raw: object, index: int) -> LanguageProfile:
    value = _mapping(raw, f"languages[{index}]")
    _strict_keys(value, {"code", "language", "locale", "stt", "llm", "tts", "fallback", "greeting", "voice"}, f"languages[{index}]")
    code = _text(value.get("code", value.get("language")), f"languages[{index}].code")
    locale = _text(value.get("locale", code), f"languages[{index}].locale")
    providers = {}
    for name in ("stt", "llm", "tts"):
        provider = value.get(name)
        if isinstance(provider, Mapping):
            provider = provider.get("provider", provider.get("name"))
        providers[name] = _text(provider, f"languages[{index}].{name}")
    fallback = _optional_text(value.get("fallback"), f"languages[{index}].fallback")
    greeting = value.get("greeting", "")
    voice = value.get("voice", "")
    if greeting and not isinstance(greeting, str):
        raise ManifestError(f"languages[{index}].greeting must be a string")
    if voice and not isinstance(voice, str):
        raise ManifestError(f"languages[{index}].voice must be a string")
    return LanguageProfile(code, locale, providers["stt"], providers["llm"], providers["tts"], fallback, greeting, voice)


def _parse_capability(name: str, raw: object) -> Capability:
    if raw is None:
        return Capability()
    if isinstance(raw, bool):
        return Capability(enabled=raw)
    value = _mapping(raw, f"capabilities.{name}")
    _strict_keys(value, _CAPABILITY_KEYS, f"capabilities.{name}")
    enabled = _bool(value.get("enabled", False), f"capabilities.{name}.enabled")
    details = value.get("details", {})
    if not isinstance(details, Mapping):
        raise ManifestError(f"capabilities.{name}.details must be an object")
    capability = Capability(
        enabled=enabled,
        destination=_optional_text(value.get("destination"), f"capabilities.{name}.destination"),
        fallback=_optional_text(value.get("fallback"), f"capabilities.{name}.fallback"),
        identifier=_optional_text(value.get("identifier"), f"capabilities.{name}.identifier"),
        details=dict(details),
    )
    if enabled and name == "booking" and not capability.destination:
        raise ManifestError("booking.destination is required when booking is enabled")
    if enabled and name == "handover" and not capability.destination:
        raise ManifestError("handover.destination is required when handover is enabled")
    return capability


def _parse_source(raw: object, index: int) -> KnowledgeSource:
    value = _mapping(raw, f"knowledge_sources[{index}]")
    allowed = {"kind", "path", "url", "owner", "effective_date", "classification", "approved_origins", "path_prefixes"}
    _strict_keys(value, allowed, f"knowledge_sources[{index}]")
    kind = _text(value.get("kind"), f"knowledge_sources[{index}].kind")
    if kind not in {"local", "url"}:
        raise ManifestError("knowledge source kind must be local or url")
    path = _optional_text(value.get("path"), f"knowledge_sources[{index}].path")
    url = _optional_text(value.get("url"), f"knowledge_sources[{index}].url")
    if kind == "local":
        if not path or url:
            raise ManifestError(f"knowledge_sources[{index}] local source requires path only")
        _validate_local_path(path)
    elif not url or path:
        raise ManifestError(f"knowledge_sources[{index}] URL source requires url only")
    owner = _text(value.get("owner"), f"knowledge_sources[{index}].owner")
    effective_date = _text(value.get("effective_date"), f"knowledge_sources[{index}].effective_date")
    classification = _text(value.get("classification", "public"), f"knowledge_sources[{index}].classification")
    origins = _text_tuple(value.get("approved_origins", ()), f"knowledge_sources[{index}].approved_origins")
    prefixes = _text_tuple(value.get("path_prefixes", ()), f"knowledge_sources[{index}].path_prefixes")
    if kind == "url" and not origins:
        raise ManifestError(f"knowledge_sources[{index}].approved_origins is required for URL sources")
    return KnowledgeSource(kind, path, url, owner, effective_date, classification, origins, prefixes)


def _validate_local_path(path: str) -> None:
    candidate = Path(path)
    if "\x00" in path or any(part == ".." for part in candidate.parts):
        raise ManifestError("knowledge source path is outside the approved root")
    roots_raw = os.environ.get("SMARTPBX_APPROVED_SOURCE_ROOTS", str(Path.cwd()))
    roots = [Path(item).expanduser().resolve() for item in roots_raw.split(os.pathsep) if item]
    resolved = candidate.expanduser().resolve() if candidate.is_absolute() else (Path.cwd() / candidate).resolve()
    if not any(resolved == root or root in resolved.parents for root in roots):
        raise ManifestError("knowledge source path is outside the approved root")


def parse_manifest(raw: Mapping[str, object]) -> AgentManifest:
    if not isinstance(raw, Mapping):
        raise ManifestError("manifest must be an object")
    unknown = sorted(set(raw) - TOP_LEVEL_KEYS)
    if unknown:
        raise ManifestError(f"unknown key: {unknown[0]}")
    required = TOP_LEVEL_KEYS
    missing = sorted(key for key in required if key not in raw)
    if missing:
        raise ManifestError(f"missing required key: {missing[0]}")
    schema_version = _int(raw["schema_version"], "schema_version", minimum=1, maximum=1)
    text_fields = ["display_name", "public_name", "agent_name", "industry", "purpose", "audience", "timezone", "technical_owner"]
    values = {field: _text(raw[field], field) for field in text_fields}
    slug = validate_slug(_text(raw["slug"], "slug"))
    profile = _text(raw["profile"], "profile")
    if profile not in {"demo", "production-intent"}:
        raise ManifestError("profile must be demo or production-intent")
    hours = _mapping(raw["operating_hours"], "operating_hours")
    operating_hours = {str(day): _text(period, f"operating_hours.{day}") for day, period in hours.items()}
    languages_raw = raw["languages"]
    if not isinstance(languages_raw, Sequence) or isinstance(languages_raw, (str, bytes)) or not languages_raw:
        raise ManifestError("languages must contain at least one language")
    languages = tuple(_parse_language(value, index) for index, value in enumerate(languages_raw))
    if len({language.code for language in languages}) != len(languages):
        raise ManifestError("languages must not contain duplicate codes")
    pii_raw = _mapping(raw["pii_policy"], "pii_policy")
    _strict_keys(pii_raw, {"explicit_consent", "collect_name", "collect_phone", "collect_other", "confirmation_policy"}, "pii_policy")
    pii = PiiPolicy(
        explicit_consent=_bool(pii_raw.get("explicit_consent"), "pii_policy.explicit_consent"),
        collect_name=_bool(pii_raw.get("collect_name", False), "pii_policy.collect_name"),
        collect_phone=_bool(pii_raw.get("collect_phone", False), "pii_policy.collect_phone"),
        collect_other=_text_tuple(pii_raw.get("collect_other", ()), "pii_policy.collect_other"),
        confirmation_policy=_text(pii_raw.get("confirmation_policy", "confirm-uncertain"), "pii_policy.confirmation_policy"),
    )
    capabilities_raw = _mapping(raw["capabilities"], "capabilities")
    _strict_keys(capabilities_raw, set(_CAPABILITIES), "capabilities")
    capabilities = CapabilitySelection(**{name: _parse_capability(name, capabilities_raw.get(name)) for name in _CAPABILITIES})
    if capabilities.any_enabled and not pii.explicit_consent:
        raise ManifestError("capabilities require pii_policy.explicit_consent")
    sources_raw = raw["knowledge_sources"]
    if not isinstance(sources_raw, Sequence) or isinstance(sources_raw, (str, bytes)):
        raise ManifestError("knowledge_sources must be an array")
    sources = tuple(_parse_source(value, index) for index, value in enumerate(sources_raw))
    smart_raw = _mapping(raw["smartpbx"], "smartpbx")
    _strict_keys(smart_raw, {"account_id", "capacity", "protocol_profile", "status_authentication"}, "smartpbx")
    smartpbx = SmartPBXInput(
        account_id=_text(smart_raw.get("account_id"), "smartpbx.account_id"),
        capacity=_int(smart_raw.get("capacity"), "smartpbx.capacity", minimum=1, maximum=100),
        protocol_profile=_text(smart_raw.get("protocol_profile", "smartpbx-ai-provider-v06"), "smartpbx.protocol_profile"),
        status_authentication=_bool(smart_raw.get("status_authentication", False), "smartpbx.status_authentication"),
    )
    operations_raw = _mapping(raw["operations"], "operations")
    _strict_keys(operations_raw, {"alert_owner", "support_contact", "rotation_due"}, "operations")
    operations = OperationsInput(
        alert_owner=_text(operations_raw.get("alert_owner"), "operations.alert_owner"),
        support_contact=_text(operations_raw.get("support_contact"), "operations.support_contact"),
        rotation_due=_text(operations_raw.get("rotation_due", "not-set"), "operations.rotation_due"),
    )
    website_raw = _mapping(raw["website_demo"], "website_demo")
    _strict_keys(website_raw, {"enabled", "visibility", "supported_languages"}, "website_demo")
    website = WebsiteDemoInput(
        enabled=_bool(website_raw.get("enabled"), "website_demo.enabled"),
        visibility=_text(website_raw.get("visibility", "pending"), "website_demo.visibility"),
        supported_languages=_text_tuple(website_raw.get("supported_languages", tuple(language.code for language in languages)), "website_demo.supported_languages"),
    )
    unknown_languages = set(website.supported_languages) - {language.code for language in languages}
    if unknown_languages:
        raise ManifestError(f"website_demo.supported_languages contains unknown language: {sorted(unknown_languages)[0]}")
    return AgentManifest(
        schema_version=schema_version,
        **values,
        slug=slug,
        profile=profile,
        operating_hours=operating_hours,
        languages=languages,
        allowed_topics=_text_tuple(raw["allowed_topics"], "allowed_topics"),
        refused_topics=_text_tuple(raw["refused_topics"], "refused_topics"),
        pii_policy=pii,
        capabilities=capabilities,
        knowledge_sources=sources,
        smartpbx=smartpbx,
        operations=operations,
        website_demo=website,
    )


def manifest_digest(manifest: AgentManifest) -> str:
    if not isinstance(manifest, AgentManifest):
        raise ManifestError("manifest_digest requires AgentManifest")
    payload = json.dumps(asdict(manifest), sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
