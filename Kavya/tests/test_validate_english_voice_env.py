from pathlib import Path
import subprocess

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
VALIDATOR = PROJECT_ROOT / "scripts" / "validate_english_voice_env.sh"
KEY = "KAVYA_EN_ELEVENLABS_VOICE_ID"


def write_environment(path: Path, value: str | None) -> None:
    if value is None:
        path.write_text("UNRELATED=value\n", encoding="utf-8")
    else:
        path.write_text(f"{KEY}={value}\n", encoding="utf-8")


def run_validator(first: Path, second: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sh", str(VALIDATOR), str(first), str(second)],
        text=True,
        capture_output=True,
        check=False,
    )


def test_equal_nonblank_values_pass_with_only_fixed_marker(tmp_path):
    first = tmp_path / "first.env"
    second = tmp_path / "second.env"
    write_environment(first, "equal-placeholder")
    write_environment(second, "equal-placeholder")

    result = run_validator(first, second)

    assert result.returncode == 0
    assert result.stdout == "canonical_voice_match=ok\n"
    assert result.stderr == ""


def test_mismatch_fails_without_echoing_either_value(tmp_path):
    first = tmp_path / "first.env"
    second = tmp_path / "second.env"
    write_environment(first, "first-placeholder")
    write_environment(second, "second-placeholder")

    result = run_validator(first, second)

    assert result.returncode != 0
    assert "first-placeholder" not in result.stdout + result.stderr
    assert "second-placeholder" not in result.stdout + result.stderr


@pytest.mark.parametrize("value", ["", None])
def test_blank_or_missing_value_fails_without_success_marker(tmp_path, value):
    first = tmp_path / "first.env"
    second = tmp_path / "second.env"
    write_environment(first, "present-placeholder")
    write_environment(second, value)

    result = run_validator(first, second)

    assert result.returncode != 0
    assert "canonical_voice_match=ok" not in result.stdout + result.stderr
    assert "present-placeholder" not in result.stdout + result.stderr


def test_duplicate_assignment_fails_without_echoing_either_value(tmp_path):
    first = tmp_path / "first.env"
    second = tmp_path / "second.env"
    first.write_text(
        f"{KEY}=duplicate-first-sentinel\n{KEY}=duplicate-second-sentinel\n",
        encoding="utf-8",
    )
    write_environment(second, "duplicate-first-sentinel")

    result = run_validator(first, second)

    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "duplicate-first-sentinel" not in combined
    assert "duplicate-second-sentinel" not in combined
