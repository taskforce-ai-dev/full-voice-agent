"""IAAC production must use a reviewed immutable image and manual first deploy."""

from __future__ import annotations

from pathlib import Path

import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = PROJECT_ROOT.parent


def test_compose_pins_the_smartpbx_service_to_a_reviewed_image():
    compose = yaml.load(
        (PROJECT_ROOT / "docker-compose.yml").read_text(encoding="utf-8"),
        Loader=yaml.BaseLoader,
    )
    service = compose["services"]["iaac-smartpbx"]

    assert "build" not in service
    assert service["image"] == (
        "ghcr.io/taskforce-ai-dev/iaac:${SMARTPBX_IMAGE_TAG:-disabled}"
    )
    assert service["ports"] == ["127.0.0.1:8042:8000"]
    assert service["environment"]["SMARTPBX_AUTH_HEADER_NAME"] == (
        "X-IAAC-SmartPBX-Token"
    )


def test_iaac_deploy_is_manual_image_only_and_never_builds_on_production():
    workflow = (REPO_ROOT / ".github/workflows/deploy.yml").read_text(
        encoding="utf-8"
    )

    assert "iaac" in workflow
    assert 'iaac)      SRC="IAAC Agent";          OPT="/opt/iaac";' in workflow
    assert 'if [[ "$AGENT" == "iaac" && "$MODE" != "image" ]]' in workflow
    assert "SMARTPBX_IMAGE_TAG='$IMG_SHA'" in workflow
    assert "--env-file .env.smartpbx --profile smartpbx pull iaac-smartpbx" in workflow
    assert "--env-file .env.smartpbx --profile smartpbx up -d --force-recreate iaac-smartpbx" in workflow
    assert not (REPO_ROOT / ".github/workflows/deploy-iaac.yml").exists()


def test_language_menu_is_iaac_specific_and_wire_valid():
    audio = (PROJECT_ROOT / "smartpbx_language_menu.ulaw").read_bytes()
    kavya_audio = (REPO_ROOT / "Kavya/smartpbx_language_menu.ulaw").read_bytes()

    assert audio != kavya_audio
    assert len(audio) % 160 == 0
    assert len(audio) <= 512 * 160
    assert audio[:2400] == b"\xff" * 2400
    assert audio[2400:2560] != b"\xff" * 160


def test_docker_build_context_excludes_runtime_secrets():
    patterns = {
        line.strip()
        for line in (PROJECT_ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }

    assert ".env" in patterns
    assert ".env.*" in patterns
    assert "full-voice-agent-*.json" in patterns
