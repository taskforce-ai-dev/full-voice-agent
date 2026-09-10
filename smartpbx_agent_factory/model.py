"""Immutable data contracts shared by the factory foundation."""

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Mapping, Optional


def _freeze(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, set):
        return frozenset(_freeze(item) for item in value)
    return value


@dataclass(frozen=True)
class LanguageProfile:
    code: str
    locale: str
    stt: str
    llm: str
    tts: str
    fallback: Optional[str] = None
    fallback_model: str = ""
    greeting: str = ""
    voice: str = ""
    stt_model: str = ""
    llm_model: str = ""
    tts_model: str = ""

    @property
    def pipeline(self) -> Mapping[str, str]:
        result = {
            "locale": self.locale,
            "stt": self.stt,
            "llm": self.llm,
            "tts": self.tts,
        }
        if self.fallback:
            result["fallback"] = self.fallback
        if self.fallback_model:
            result["fallback_model"] = self.fallback_model
        if self.stt_model:
            result["stt_model"] = self.stt_model
        if self.llm_model:
            result["llm_model"] = self.llm_model
        if self.tts_model:
            result["tts_model"] = self.tts_model
        return result


@dataclass(frozen=True)
class PiiPolicy:
    explicit_consent: bool
    collect_name: bool = False
    collect_phone: bool = False
    collect_other: tuple[str, ...] = ()
    confirmation_policy: str = "confirm-uncertain"


@dataclass(frozen=True)
class Capability:
    enabled: bool = False
    destination: Optional[str] = None
    fallback: Optional[str] = None
    identifier: Optional[str] = None
    details: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "details", _freeze(self.details))


@dataclass(frozen=True)
class CapabilitySelection:
    booking: Capability = field(default_factory=Capability)
    handover: Capability = field(default_factory=Capability)
    whatsapp: Capability = field(default_factory=Capability)
    crm: Capability = field(default_factory=Capability)
    payment: Capability = field(default_factory=Capability)
    post_call_reporting: Capability = field(default_factory=Capability)
    recording: Capability = field(default_factory=Capability)
    transcript_retention: Capability = field(default_factory=Capability)

    @property
    def any_enabled(self) -> bool:
        return any(value.enabled for value in self.as_mapping().values())

    @property
    def enabled_names(self) -> tuple[str, ...]:
        return tuple(name for name, value in self.as_mapping().items() if value.enabled)

    def as_mapping(self) -> Mapping[str, Capability]:
        return {
            "booking": self.booking,
            "handover": self.handover,
            "whatsapp": self.whatsapp,
            "crm": self.crm,
            "payment": self.payment,
            "post_call_reporting": self.post_call_reporting,
            "recording": self.recording,
            "transcript_retention": self.transcript_retention,
        }


@dataclass(frozen=True)
class KnowledgeSource:
    kind: str
    path: Optional[str] = None
    url: Optional[str] = None
    owner: str = ""
    effective_date: str = ""
    classification: str = "public"
    approved_origins: tuple[str, ...] = ()
    path_prefixes: tuple[str, ...] = ()


@dataclass(frozen=True)
class SmartPBXInput:
    account_id: str
    capacity: int
    protocol_profile: str = "smartpbx-ai-provider-v07"
    status_authentication: bool = True


@dataclass(frozen=True)
class OperationsInput:
    alert_owner: str
    support_contact: str
    rotation_due: str = ""


@dataclass(frozen=True)
class WebsiteDemoInput:
    enabled: bool
    visibility: str = "pending"
    supported_languages: tuple[str, ...] = ()


@dataclass(frozen=True)
class AgentManifest:
    schema_version: int
    display_name: str
    public_name: str
    slug: str
    agent_name: str
    industry: str
    purpose: str
    audience: str
    profile: str
    timezone: str
    operating_hours: Mapping[str, str]
    technical_owner: str
    languages: tuple[LanguageProfile, ...]
    allowed_topics: tuple[str, ...]
    refused_topics: tuple[str, ...]
    pii_policy: PiiPolicy
    capabilities: CapabilitySelection
    knowledge_sources: tuple[KnowledgeSource, ...]
    smartpbx: SmartPBXInput
    operations: OperationsInput
    website_demo: WebsiteDemoInput

    def __post_init__(self) -> None:
        object.__setattr__(self, "operating_hours", _freeze(self.operating_hours))
