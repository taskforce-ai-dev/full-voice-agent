from pathlib import Path

import pytest

from english_voice_profile import (
    ELEVEN_FLASH_V2_5,
    KAVYA_EN_ELEVENLABS_VOICE_ID,
    ULAW_8000,
    load_kavya_english_voice_profile,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = PROJECT_ROOT.parent


def test_profile_selects_exact_semantics_and_redacts_voice_id():
    profile = load_kavya_english_voice_profile(
        {KAVYA_EN_ELEVENLABS_VOICE_ID: "unit-test-canonical-voice"}
    )

    assert profile.twilio_composite_voice == "unit-test-canonical-voice-flash_v2_5"
    assert profile.model_id == ELEVEN_FLASH_V2_5
    assert profile.output_format == ULAW_8000
    assert profile.request_voice_settings == {
        "stability": 0.5,
        "similarity_boost": 0.75,
        "style": 0.0,
        "use_speaker_boost": True,
    }
    assert KAVYA_EN_ELEVENLABS_VOICE_ID in repr(profile)
    assert "unit-test-canonical-voice" not in repr(profile)
    assert "unit-test-canonical-voice" not in str(profile)


@pytest.mark.parametrize("value", [None, "", "   "])
def test_profile_fails_closed_when_protected_value_is_absent_or_blank(value):
    environment = {}
    if value is not None:
        environment[KAVYA_EN_ELEVENLABS_VOICE_ID] = value

    with pytest.raises(ValueError, match=KAVYA_EN_ELEVENLABS_VOICE_ID):
        load_kavya_english_voice_profile(environment)


def test_dockerfile_explicit_allowlist_includes_profile_runtime_module():
    dockerfile = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "english_voice_profile.py" in dockerfile


def test_ci_runs_a_non_deploying_kavya_image_build_and_import_gate():
    ci_workflow = (REPOSITORY_ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )

    assert "if: matrix.agent == 'Kavya'" in ci_workflow
    assert "working-directory: Kavya" in ci_workflow
    assert "docker build -t \"$image\" ." in ci_workflow
    assert "docker run --rm --entrypoint python \"$image\" -c \"import english_voice_profile; import server\"" in ci_workflow
