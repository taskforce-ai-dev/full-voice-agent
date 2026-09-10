from pathlib import Path
import subprocess

from smartpbx_agent_factory.cli import invoke_cli


FIXTURE = Path(__file__).parent / "fixtures" / "acme-minimal.json"
CATALOGUE = Path(__file__).parent / "fixtures" / "approved-provider-catalogue.json"


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
    result = invoke_cli(
        ["plan", "--manifest", str(FIXTURE), "--state-root", str(tmp_path), "--catalogue", str(CATALOGUE)]
    )
    assert result.exit_code == 0
    assert "wss_token" not in result.stdout
    assert len(list(tmp_path.glob("*.json"))) == 1


def test_resume_refuses_changed_manifest_digest(tmp_path):
    manifest = tmp_path / "manifest.json"
    manifest.write_bytes(FIXTURE.read_bytes())
    plan = invoke_cli(
        ["plan", "--manifest", str(manifest), "--state-root", str(tmp_path), "--catalogue", str(CATALOGUE)]
    )
    generation_id = plan.stdout.split("generation_id=", 1)[1].splitlines()[0]
    manifest.write_text(manifest.read_text(encoding="utf-8").replace("Acme Inquiry", "Changed Inquiry", 1), encoding="utf-8")
    result = invoke_cli(["resume", generation_id, "--state-root", str(tmp_path), "--catalogue", str(CATALOGUE)])
    assert result.exit_code == 3
    assert "manifest digest changed" in result.stderr
