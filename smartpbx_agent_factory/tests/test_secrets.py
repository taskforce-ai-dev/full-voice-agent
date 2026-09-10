from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from smartpbx_agent_factory.secrets import (
    CredentialSourcePolicy,
    OperationsPrerequisiteError,
    OperationsPrerequisites,
    RepositoryVisibilityVerifier,
    SopsAgeSecretProvider,
)


class FakeRunner:
    def __init__(self, remote: str = "git@github.com:example/private-operations.git") -> None:
        self.remote = remote
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        if argv[:5] == ["git", "-C", argv[2], "config", "--get"]:
            return type("Completed", (), {"returncode": 0, "stdout": self.remote.encode(), "stderr": b""})()
        return type("Completed", (), {"returncode": 0, "stdout": b"sops:\n  age: encrypted", "stderr": b""})()


class FakeVisibilityVerifier:
    def __init__(self, private: bool = True) -> None:
        self.private = private
        self.calls = []

    def is_private(self, *, repository: Path, canonical_remote: str) -> bool:
        self.calls.append((repository, canonical_remote))
        return self.private


def valid_prerequisites(tmp_path: Path, *, policy: dict[str, CredentialSourcePolicy] | None = None) -> OperationsPrerequisites:
    repository = tmp_path / "operations"
    repository.mkdir()
    (repository / ".git").write_text("gitdir: /must-not-be-followed\n", encoding="utf-8")
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
        credential_source_policy=policy if policy is not None else {
            "acme-inquiry/wss_token": CredentialSourcePolicy(
                provider="generated", path="agents/acme-inquiry/wss_token", rotation_owner="platform-security"
            )
        },
        sops_binary=Path("/bin/true"),
        age_binary=Path("/bin/true"),
    )


def test_generated_token_is_reused_for_resume_without_printing_value(tmp_path: Path):
    state = {}
    provider = SopsAgeSecretProvider(
        valid_prerequisites(tmp_path),
        generation_state=state,
        runner=FakeRunner(),
        visibility_verifier=FakeVisibilityVerifier(),
    )
    first = provider.generate("acme-inquiry/wss_token", length=32)
    second = provider.generate("acme-inquiry/wss_token", length=32)
    assert first == second
    report = provider.audit_report()
    assert first not in json.dumps(report)
    assert report.generated_names == ("acme-inquiry/wss_token",)


@pytest.mark.parametrize(
    ("replacement", "reason"),
    (
        ({"repository_path": Path("relative")}, "repository_path"),
        ({"owner": ""}, "owner"),
        ({"recipient_file": None}, "recipient_file"),
        ({"recipient_review_source": ""}, "recipient_review_source"),
        ({"credential_source_policy": {}}, "credential_source_policy"),
        ({"sops_binary": Path("relative/sops")}, "sops_binary"),
        ({"age_binary": Path("relative/age")}, "age_binary"),
    ),
)
def test_missing_operations_prerequisite_fails_closed_before_any_command(tmp_path: Path, replacement, reason: str):
    prerequisites = replace(valid_prerequisites(tmp_path), **replacement)
    runner = FakeRunner()
    provider = SopsAgeSecretProvider(prerequisites, runner=runner, visibility_verifier=FakeVisibilityVerifier())
    with pytest.raises(OperationsPrerequisiteError, match=reason) as raised:
        provider.validate()
    assert raised.value.code == "OPS_PREREQUISITES_UNAVAILABLE"
    assert runner.calls == []


def test_linked_worktree_remote_uses_git_argv_without_following_gitdir(tmp_path: Path):
    prerequisites = valid_prerequisites(tmp_path)
    runner = FakeRunner()
    visibility = FakeVisibilityVerifier()
    provider = SopsAgeSecretProvider(prerequisites, runner=runner, visibility_verifier=visibility)
    provider.validate()
    git_argv, git_kwargs = runner.calls[0]
    assert git_argv == ["git", "-C", str(prerequisites.repository_path), "config", "--get", "remote.origin.url"]
    assert git_kwargs["shell"] is False
    assert visibility.calls == [(prerequisites.repository_path, prerequisites.canonical_remote)]


def test_missing_or_non_private_visibility_verifier_fails_closed(tmp_path: Path):
    prerequisites = valid_prerequisites(tmp_path)
    with pytest.raises(OperationsPrerequisiteError, match="private_repository"):
        SopsAgeSecretProvider(prerequisites, runner=FakeRunner()).validate()
    with pytest.raises(OperationsPrerequisiteError, match="private_repository"):
        SopsAgeSecretProvider(
            prerequisites, runner=FakeRunner(), visibility_verifier=FakeVisibilityVerifier(private=False)
        ).validate()


def test_prerequisite_policy_and_recipients_are_frozen_after_validation(tmp_path: Path):
    source_policy = {
        "acme-inquiry/wss_token": CredentialSourcePolicy(
            provider="generated", path="agents/acme-inquiry/wss_token", rotation_owner="platform-security"
        )
    }
    recipient_file = tmp_path / "recipients.txt"
    recipient_file.write_text(
        "age1qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqe3m5r\n", encoding="utf-8"
    )
    prerequisites = replace(valid_prerequisites(tmp_path, policy=source_policy), recipient_file=recipient_file)
    provider = SopsAgeSecretProvider(prerequisites, runner=FakeRunner(), visibility_verifier=FakeVisibilityVerifier())
    provider.validate()
    source_policy.clear()
    recipient_file.write_text("unapproved-recipient\n", encoding="utf-8")
    assert provider.generate("acme-inquiry/wss_token")
    with pytest.raises(TypeError):
        prerequisites.credential_source_policy["other/token"] = CredentialSourcePolicy("x", "x", "x")


def test_encrypt_uses_argument_vector_and_removes_plaintext_tempfile(tmp_path: Path):
    runner = FakeRunner()
    provider = SopsAgeSecretProvider(
        valid_prerequisites(tmp_path), runner=runner, visibility_verifier=FakeVisibilityVerifier()
    )
    encrypted = provider.encrypt_yaml(b"wss_token: fixture-only", path=tmp_path / "secrets.sops.yaml")
    assert encrypted.startswith(b"sops:")
    git_call, sops_version, age_version, sops_call = runner.calls
    assert git_call[0][0] == "git"
    assert sops_version[0] == ["/bin/true", "--version"]
    assert age_version[0] == ["/bin/true", "--version"]
    assert sops_call[0][0] == "/bin/true" and "--encrypt" in sops_call[0]
    assert all(kwargs["shell"] is False for _, kwargs in runner.calls)
    assert b"fixture-only" not in b" ".join(" ".join(argv).encode() for argv, _ in runner.calls)
    assert not list(tmp_path.glob("*.plaintext"))
