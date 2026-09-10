from pathlib import Path


ROOT = Path(__file__).parents[2]


def test_factory_state_directory_is_root_anchored_and_only_named_path():
    lines = [line.strip() for line in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()]
    assert lines.count("/.smartpbx-generations/") == 1


def test_factory_ci_installs_pytest_timeout_for_timeout_flag():
    workflow = (ROOT / ".github" / "workflows" / "smartpbx-agent-factory.yml").read_text(encoding="utf-8")
    assert "pytest-timeout" in workflow
    assert "--timeout=300" in workflow
