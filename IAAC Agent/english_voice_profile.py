from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field


KAVYA_EN_ELEVENLABS_VOICE_ID = "KAVYA_EN_ELEVENLABS_VOICE_ID"
ELEVEN_FLASH_V2_5 = "eleven_flash_v2_5"
ULAW_8000 = "ulaw_8000"
_TWILIO_FLASH_SUFFIX = "flash_v2_5"


@dataclass(frozen=True)
class ElevenLabsVoiceSettings:
    stability: float = 0.5
    similarity_boost: float = 0.75
    style: float = 0.0
    use_speaker_boost: bool = True

    def as_request_payload(self) -> dict[str, float | bool]:
        return {
            "stability": self.stability,
            "similarity_boost": self.similarity_boost,
            "style": self.style,
            "use_speaker_boost": self.use_speaker_boost,
        }


@dataclass(frozen=True)
class KavyaEnglishVoiceProfile:
    voice_id: str = field(repr=False)
    model_id: str = ELEVEN_FLASH_V2_5
    output_format: str = ULAW_8000
    settings: ElevenLabsVoiceSettings = field(default_factory=ElevenLabsVoiceSettings)

    @property
    def twilio_composite_voice(self) -> str:
        return f"{self.voice_id}-{_TWILIO_FLASH_SUFFIX}"

    @property
    def request_voice_settings(self) -> dict[str, float | bool]:
        return self.settings.as_request_payload()

    def __repr__(self) -> str:
        return (
            "KavyaEnglishVoiceProfile("
            f"env_key={KAVYA_EN_ELEVENLABS_VOICE_ID!r}, "
            f"model_id={self.model_id!r}, output_format={self.output_format!r}, "
            f"settings={self.settings!r})"
        )

    __str__ = __repr__


def load_kavya_english_voice_profile(
    environment: Mapping[str, str] | None = None,
) -> KavyaEnglishVoiceProfile:
    source = os.environ if environment is None else environment
    value = source.get(KAVYA_EN_ELEVENLABS_VOICE_ID, "")
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{KAVYA_EN_ELEVENLABS_VOICE_ID} must be configured")
    return KavyaEnglishVoiceProfile(voice_id=value.strip())
