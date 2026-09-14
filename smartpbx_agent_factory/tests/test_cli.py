import json
from pathlib import Path
import subprocess

from smartpbx_agent_factory.cli import invoke_cli


FIXTURE = Path(__file__).parent / "fixtures" / "acme-minimal.json"
CATALOGUE = Path(__file__).parent / "fixtures" / "approved-provider-catalogue.json"


def factory_config(tmp_path: Path) -> Path:
    """Write the non-secret bootstrap input required by every stateful command."""
    repository_root = Path(__file__).parents[2]
    config = {
        "version": 1,
        "state_root": str(tmp_path / "state"),
        "catalogue": str(CATALOGUE.resolve()),
        "approved_source_roots": [str(repository_root)],
        "age": {
            "recipient_file": str(tmp_path / "recipients.txt"),
            "approved_recipient_fingerprints": ["age1qqqqqqqqqqqqqqqqqqqqqqqq"],
            "recipient_review_source": "test-security-review",
            "credential_source_policy": {
                "providers/google_application_credentials": {
                    "provider": "environment",
                    "path": "GOOGLE_APPLICATION_CREDENTIALS_JSON",
                    "rotation_owner": "security",
                }
            },
            "sops_binary": "/usr/bin/sops",
            "age_binary": "/usr/bin/age",
        },
        "ci": {
            "repository": "acme/factory-ci",
            "workflow": "smartpbx-generated-ci",
            "gh_binary": "/usr/bin/gh",
        },
        "lanes": {
            role: {
                "primary": str(tmp_path / f"{role}-primary"),
                "remote": "origin",
                "canonical_remote": f"https://github.com/acme/{role}.git",
                "revision": "a" * 40,
                "target_root": str(tmp_path / "worktrees" / role),
                "repository": f"acme/{role}",
                "base_branch": "main",
                "ci_check": f"{role}-gate",
                "ci_policy": "lifecycle-attestation" if role == "backend" else (
                    "secret-static" if role == "operations" else "website-build"
                ),
            }
            for role in ("backend", "operations", "website")
        },
    }
    path = tmp_path / "factory.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    return path


def test_cli_has_no_deploy_or_provision_command():
    result = subprocess.run(
        ["python3", "-m", "smartpbx_agent_factory", "--help"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert "inspect" in result.stdout
    assert "open-pr" in result.stdout
    assert "deploy" not in result.stdout
    assert "provision" not in result.stdout


def test_plan_does_not_write_target_repositories(tmp_path):
    config = factory_config(tmp_path)
    result = invoke_cli(
        ["plan", "--manifest", str(FIXTURE), "--config", str(config)]
    )
    assert result.exit_code == 0
    assert "wss_token" not in result.stdout
    assert len(list((tmp_path / "state").glob("*.json"))) == 1


def test_resume_refuses_changed_manifest_digest(tmp_path):
    config = factory_config(tmp_path)
    manifest = tmp_path / "manifest.json"
    manifest.write_bytes(FIXTURE.read_bytes())
    plan = invoke_cli(
        ["plan", "--manifest", str(manifest), "--config", str(config)]
    )
    generation_id = plan.stdout.split("generation_id=", 1)[1].splitlines()[0]
    manifest.write_text(manifest.read_text(encoding="utf-8").replace("Acme Inquiry", "Changed Inquiry", 1), encoding="utf-8")
    result = invoke_cli(["resume", generation_id, "--config", str(config)])
    assert result.exit_code == 3
    assert "manifest digest changed" in result.stderr
