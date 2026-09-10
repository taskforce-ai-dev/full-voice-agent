from __future__ import annotations

import json
from pathlib import Path

import pytest

from smartpbx_agent_factory.secrets import (
    CredentialSourcePolicy,
    OperationsPrerequisiteError,
    OperationsPrerequisites,
    SopsAgeSecretProvider,
)


class FakeRunner:
    def __call__(self, argv, **kwargs):
        self.argv = argv
        self.kwargs = kwargs
        return type("Completed", (), {"returncode": 0, "stdout": b"sops:\n  age: encrypted", "stderr": b""})()


def valid_prerequisites(tmp_path: Path) -> OperationsPrerequisites:
    repository = tmp_path / "operations"
    (repository / ".git").mkdir(parents=True)
    (repository / ".git" / "config").write_text(
        '[remote "origin"]\n\turl = git@github.com:example/private-operations.git\n', encoding="utf-8"
    )
    recipients = Path(__file__).parent / "fixtures" / "age-recipients.txt"
    return OperationsPrerequisites(
        repository_path=repository,
        canonical_remote="git@github.com:example/private-operations.git",
        owner="platform-security",
        recipient_file=recipients,
        approved_recipient_fingerprints=(
            "age1qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqe3m5r",
        ),
        recipient_review_source="security-review-2026-09-10",
        credential_source_policy={
            "acme-inquiry/wss_token": CredentialSourcePolicy(
                provider="generated", path="agents/acme-inquiry/wss_token", rotation_owner="platform-security"
            )
        },
        sops_binary=Path("/bin/true"),
        age_binary=Path("/bin/true"),
        repository_is_private=True,
    )


def test_generated_token_is_reused_for_resume_without_printing_value(tmp_path: Path):
    state = {}
    provider = SopsAgeSecretProvider(valid_prerequisites(tmp_path), generation_state=state, runner=FakeRunner())
    first = provider.generate("acme-inquiry/wss_token", length=32)
    second = provider.generate("acme-inquiry/wss_token", length=32)
    assert first == second
    report = provider.audit_report()
    assert first not in json.dumps(report)
    assert report.generated_names == ("acme-inquiry/wss_token",)


@pytest.mark.parametrize(
    ("mutate", "reason"),
    (
        (lambda prereqs: setattr(prereqs, "repository_path", Path("relative")), "repository_path"),
        (lambda prereqs: setattr(prereqs, "canonical_remote", "git@github.com:example/wrong.git"), "canonical_remote"),
        (lambda prereqs: setattr(prereqs, "repository_is_private", False), "private_repository"),
        (lambda prereqs: setattr(prereqs, "owner", ""), "owner"),
        (lambda prereqs: setattr(prereqs, "recipient_file", None), "recipient_file"),
        (lambda prereqs: setattr(prereqs, "recipient_review_source", ""), "recipient_review_source"),
        (lambda prereqs: setattr(prereqs, "credential_source_policy", {}), "credential_source_policy"),
        (lambda prereqs: setattr(prereqs, "sops_binary", Path("relative/sops")), "sops_binary"),
        (lambda prereqs: setattr(prereqs, "age_binary", Path("relative/age")), "age_binary"),
    ),
)
def test_missing_operations_prerequisite_fails_closed_before_any_command(tmp_path: Path, mutate, reason: str):
    prerequisites = valid_prerequisites(tmp_path)
    mutate(prerequisites)
    runner = FakeRunner()
    provider = SopsAgeSecretProvider(prerequisites, runner=runner)
    with pytest.raises(OperationsPrerequisiteError, match=reason) as raised:
        provider.validate()
    assert raised.value.code == "OPS_PREREQUISITES_UNAVAILABLE"
    assert not hasattr(runner, "argv")


def test_encrypt_uses_argument_vector_and_removes_plaintext_tempfile(tmp_path: Path):
    runner = FakeRunner()
    provider = SopsAgeSecretProvider(valid_prerequisites(tmp_path), runner=runner)
    encrypted = provider.encrypt_yaml(b"wss_token: fixture-only", path=tmp_path / "secrets.sops.yaml")
    assert encrypted.startswith(b"sops:")
    assert isinstance(runner.argv, list)
    assert runner.kwargs["shell"] is False
    assert not list(tmp_path.glob("*.plaintext"))
