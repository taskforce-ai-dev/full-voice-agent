"""Narrow, fail-closed secret handling for generated operations artifacts.

This module deliberately has no environment-variable, shell, or repository
creation fallback.  Real operations material can be used only after an
operator has supplied and approved the complete SOPS/age prerequisite record.
"""

from __future__ import annotations

import os
import re
import secrets as secure_secrets
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Callable, Mapping, MutableMapping, Protocol, Sequence

from .model import AgentManifest
from .resources import DerivedResources


class SecretError(RuntimeError):
    """Raised when secret material cannot be safely resolved or encrypted."""


class OperationsPrerequisiteError(SecretError):
    """A typed, fail-closed refusal to use an unapproved operations setup."""

    code = "OPS_PREREQUISITES_UNAVAILABLE"

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"{self.code}: {reason}")


@dataclass(frozen=True)
class CredentialSourcePolicy:
    """Approved provenance for one named secret, never its value."""

    provider: str
    path: str
    rotation_owner: str


@dataclass(frozen=True)
class SecretRequirement:
    """One internal record and its distinct runtime environment name.

    The record id is the only identifier passed to a credential reader.  The
    environment name is rendered only into the private operations ciphertext;
    neither is a secret value.
    """

    record_id: str
    runtime_env: str
    generated: bool = False

    def __post_init__(self) -> None:
        if not _SECRET_NAME_RE.fullmatch(self.record_id):
            raise SecretError("invalid secret record id")
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{2,127}", self.runtime_env):
            raise SecretError("invalid secret runtime environment name")
        if not isinstance(self.generated, bool):
            raise SecretError("secret generation flag is invalid")


@dataclass(frozen=True)
class SecretPlan:
    """Exact secret surface selected by immutable manifest language pipelines."""

    requirements: tuple[SecretRequirement, ...]

    def __post_init__(self) -> None:
        if not self.requirements:
            raise SecretError("secret plan must not be empty")
        if len({item.record_id for item in self.requirements}) != len(self.requirements):
            raise SecretError("secret plan record ids must be distinct")
        if len({item.runtime_env for item in self.requirements}) != len(self.requirements):
            raise SecretError("secret plan runtime environment names must be distinct")

    @property
    def operations_env_names(self) -> tuple[str, ...]:
        return tuple(item.runtime_env for item in self.requirements)

    @property
    def website_env_names(self) -> tuple[str, ...]:
        return ()

    def requirement_for_env(self, runtime_env: str) -> SecretRequirement:
        for item in self.requirements:
            if item.runtime_env == runtime_env:
                return item
        raise SecretError("secret plan does not contain runtime environment name")

    def digest_payload(self) -> tuple[dict[str, object], ...]:
        return tuple(
            {"record_id": item.record_id, "runtime_env": item.runtime_env, "generated": item.generated}
            for item in self.requirements
        )


_PROVIDER_ENV = {
    "anthropic": "ANTHROPIC_API_KEY",
    "azure": "AZURE_SPEECH_KEY",
    "deepgram": "DEEPGRAM_API_KEY",
    "elevenlabs": "ELEVENLABS_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "google": "GOOGLE_API_KEY",
    "openai": "OPENAI_API_KEY",
}


def derive_secret_plan(manifest: AgentManifest, resources: DerivedResources) -> SecretPlan:
    """Select shared provider records once plus the per-agent WSS credential.

    This function is deterministic and deliberately has no secret-provider
    dependency.  A provider occurring in multiple language roles therefore
    maps to one shared record and one runtime environment variable.
    """
    if not isinstance(manifest, AgentManifest) or not isinstance(resources, DerivedResources):
        raise SecretError("secret plan requires manifest and derived resources")
    if manifest.slug != resources.slug:
        raise SecretError("secret plan resources do not match manifest")
    providers = sorted({provider.lower() for language in manifest.languages for provider in (language.stt, language.llm, language.tts)})
    requirements: list[SecretRequirement] = []
    for provider in providers:
        env = _PROVIDER_ENV.get(provider)
        if env is None:
            normalized = re.sub(r"[^a-z0-9]+", "_", provider).strip("_").upper()
            if not normalized:
                raise SecretError("provider cannot form a secret environment name")
            env = f"SMARTPBX_PROVIDER_{normalized}_CREDENTIAL"
        requirements.append(SecretRequirement(f"providers/{provider}", env))
    requirements.append(SecretRequirement(f"agents/{resources.slug}/wss_token", "SMARTPBX_WS_TOKEN", generated=True))
    return SecretPlan(tuple(requirements))


@dataclass(frozen=True)
class OperationsPrerequisites:
    """Operator-approved inputs required before SOPS/age may be invoked."""

    repository_path: Path | None
    canonical_remote: str
    owner: str
    recipient_file: Path | None
    approved_recipient_fingerprints: tuple[str, ...]
    recipient_review_source: str
    credential_source_policy: Mapping[str, CredentialSourcePolicy]
    sops_binary: Path | None
    age_binary: Path | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "repository_path", Path(self.repository_path) if self.repository_path else None)
        object.__setattr__(self, "recipient_file", Path(self.recipient_file) if self.recipient_file else None)
        object.__setattr__(self, "sops_binary", Path(self.sops_binary) if self.sops_binary else None)
        object.__setattr__(self, "age_binary", Path(self.age_binary) if self.age_binary else None)
        object.__setattr__(self, "approved_recipient_fingerprints", tuple(self.approved_recipient_fingerprints))
        object.__setattr__(self, "credential_source_policy", MappingProxyType(dict(self.credential_source_policy)))


@dataclass(frozen=True)
class SecretBundle:
    """A short-lived, immutable collection of named secret values."""

    values: Mapping[str, str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "values", MappingProxyType(dict(self.values)))


class SecretAudit(dict[str, object]):
    """JSON-safe audit metadata containing names and paths, never values."""

    def __init__(
        self,
        *,
        fetched_names: Sequence[str] = (),
        generated_names: Sequence[str] = (),
        ciphertext_paths: Sequence[Path | str] = (),
        plaintext_paths: Sequence[Path | str] = (),
    ) -> None:
        self.fetched_names = tuple(sorted(set(fetched_names)))
        self.generated_names = tuple(sorted(set(generated_names)))
        self.ciphertext_paths = tuple(str(path) for path in ciphertext_paths)
        self.plaintext_paths = tuple(str(path) for path in plaintext_paths)
        super().__init__(
            fetched_names=self.fetched_names,
            generated_names=self.generated_names,
            ciphertext_paths=self.ciphertext_paths,
            plaintext_paths=self.plaintext_paths,
        )


class SecretProvider(Protocol):
    """The only secret capability exposed to factory rendering code."""

    def fetch(self, name: str) -> str: ...

    def generate(self, name: str, *, length: int = 32) -> str: ...

    def encrypt_yaml(self, plaintext: bytes, *, path: Path) -> bytes: ...

    def validate(self) -> None: ...

    def audit_report(self) -> SecretAudit: ...


class RepositoryVisibilityVerifier(Protocol):
    """Authoritatively determines whether an existing operations repository is private."""

    def is_private(self, *, repository: Path, canonical_remote: str) -> bool: ...


_SECRET_NAME_RE = re.compile(r"^[a-z][a-z0-9-]*(?:/[a-z][a-z0-9_-]*)+$")
_AGE_RECIPIENT_RE = re.compile(r"^age1[ac-hj-np-z02-9]{20,}$")
_CANONICAL_REMOTE_RE = re.compile(r"^(?:https://[^/\s]+/.+|ssh://[^\s]+|git@[^:\s]+:.+)$")


class SopsAgeSecretProvider:
    """SOPS/age adapter which accepts only reviewed inputs and argv commands."""

    def __init__(
        self,
        prerequisites: OperationsPrerequisites | None = None,
        *,
        generation_state: MutableMapping[str, str] | None = None,
        credential_reader: Callable[[CredentialSourcePolicy], str] | None = None,
        visibility_verifier: RepositoryVisibilityVerifier | None = None,
        runner: Callable[..., object] = subprocess.run,
    ) -> None:
        self._prerequisites = prerequisites
        self._generation_state = generation_state if generation_state is not None else {}
        self._credential_reader = credential_reader
        self._visibility_verifier = visibility_verifier
        self._runner = runner
        self._fetched_names: list[str] = []
        self._generated_names: list[str] = []
        self._validated = False
        self._recipients: tuple[str, ...] = ()

    @staticmethod
    def _require_text(value: str, reason: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise OperationsPrerequisiteError(reason)
        return value.strip()

    @staticmethod
    def _require_absolute_file(path: Path | None, reason: str, *, directory: bool = False) -> Path:
        if path is None or not isinstance(path, Path) or not path.is_absolute():
            raise OperationsPrerequisiteError(reason)
        if directory:
            if not path.is_dir():
                raise OperationsPrerequisiteError(reason)
        elif not path.is_file():
            raise OperationsPrerequisiteError(reason)
        return path.resolve()

    @staticmethod
    def _require_binary(path: Path | None, reason: str) -> Path:
        binary = SopsAgeSecretProvider._require_absolute_file(path, reason)
        if not os.access(binary, os.X_OK):
            raise OperationsPrerequisiteError(reason)
        return binary

    @staticmethod
    def _read_recipients(path: Path) -> tuple[str, ...]:
        recipients = tuple(
            line.strip()
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        )
        if not recipients or any(not _AGE_RECIPIENT_RE.fullmatch(recipient) for recipient in recipients):
            raise OperationsPrerequisiteError("approved_recipients")
        if len(set(recipients)) != len(recipients):
            raise OperationsPrerequisiteError("approved_recipients")
        return recipients

    def _verify_origin_remote(self, repository: Path, canonical_remote: str) -> None:
        """Ask Git for origin, supporting both normal and linked worktrees."""
        if not _CANONICAL_REMOTE_RE.fullmatch(canonical_remote):
            raise OperationsPrerequisiteError("canonical_remote")
        argv = ["git", "-C", str(repository), "config", "--get", "remote.origin.url"]
        try:
            result = self._runner(argv, capture_output=True, check=False, shell=False, text=False)
        except OSError as error:
            raise OperationsPrerequisiteError("canonical_remote")
        if getattr(result, "returncode", 1) != 0:
            raise OperationsPrerequisiteError("canonical_remote")
        raw_remote = getattr(result, "stdout", b"")
        if isinstance(raw_remote, bytes):
            try:
                actual_remote = raw_remote.decode("utf-8", errors="strict").strip()
            except UnicodeDecodeError as error:
                raise OperationsPrerequisiteError("canonical_remote") from error
        elif isinstance(raw_remote, str):
            actual_remote = raw_remote.strip()
        else:
            actual_remote = ""
        if not actual_remote or actual_remote.rstrip("/") != canonical_remote.rstrip("/"):
            raise OperationsPrerequisiteError("canonical_remote")

    def _run_version(self, binary: Path, reason: str) -> None:
        try:
            result = self._runner(
                [str(binary), "--version"], capture_output=True, check=False, shell=False, text=False
            )
        except OSError as error:
            raise OperationsPrerequisiteError(reason) from error
        if getattr(result, "returncode", 1) != 0:
            raise OperationsPrerequisiteError(reason)

    def validate(self) -> None:
        """Validate all external gates before generating an operations artifact."""
        if self._validated:
            return
        prerequisites = self._prerequisites
        if prerequisites is None:
            raise OperationsPrerequisiteError("operations_prerequisites")
        repository = self._require_absolute_file(prerequisites.repository_path, "repository_path", directory=True)
        if not (repository / ".git").exists():
            raise OperationsPrerequisiteError("repository_path")
        canonical_remote = self._require_text(prerequisites.canonical_remote, "canonical_remote")
        if self._visibility_verifier is None:
            raise OperationsPrerequisiteError("private_repository")
        self._verify_origin_remote(repository, canonical_remote)
        try:
            private = self._visibility_verifier.is_private(
                repository=repository, canonical_remote=canonical_remote
            )
        except Exception as error:
            raise OperationsPrerequisiteError("private_repository") from error
        if private is not True:
            raise OperationsPrerequisiteError("private_repository")
        self._require_text(prerequisites.owner, "owner")
        recipient_file = self._require_absolute_file(prerequisites.recipient_file, "recipient_file")
        self._require_text(prerequisites.recipient_review_source, "recipient_review_source")
        recipients = self._read_recipients(recipient_file)
        approved = tuple(sorted(set(prerequisites.approved_recipient_fingerprints)))
        if not approved or tuple(sorted(recipients)) != approved:
            raise OperationsPrerequisiteError("approved_recipients")
        if not prerequisites.credential_source_policy:
            raise OperationsPrerequisiteError("credential_source_policy")
        for name, policy in prerequisites.credential_source_policy.items():
            self._validate_secret_name(name)
            if not isinstance(policy, CredentialSourcePolicy):
                raise OperationsPrerequisiteError("credential_source_policy")
            self._require_text(policy.provider, "credential_source_policy")
            self._require_text(policy.path, "credential_source_policy")
            self._require_text(policy.rotation_owner, "credential_source_policy")
        sops = self._require_binary(prerequisites.sops_binary, "sops_binary")
        age = self._require_binary(prerequisites.age_binary, "age_binary")
        self._run_version(sops, "sops_binary")
        self._run_version(age, "age_binary")
        self._recipients = recipients
        self._validated = True

    @staticmethod
    def _validate_secret_name(name: str) -> None:
        if not isinstance(name, str) or not _SECRET_NAME_RE.fullmatch(name):
            raise SecretError("invalid secret name")

    def _policy_for(self, name: str) -> CredentialSourcePolicy:
        self.validate()
        assert self._prerequisites is not None
        policy = self._prerequisites.credential_source_policy.get(name)
        if policy is None:
            raise OperationsPrerequisiteError("credential_source_policy")
        return policy

    def fetch(self, name: str) -> str:
        """Retrieve one reviewed secret via the injected credential adapter only."""
        self._validate_secret_name(name)
        policy = self._policy_for(name)
        if self._credential_reader is None:
            raise SecretError("credential retrieval is unavailable")
        value = self._credential_reader(policy)
        if not isinstance(value, str) or not value:
            raise SecretError("credential retrieval returned no value")
        self._fetched_names.append(name)
        return value

    def generate(self, name: str, *, length: int = 32) -> str:
        """Generate once per stable name and retain it only in generation state."""
        self._validate_secret_name(name)
        self._policy_for(name)
        if not isinstance(length, int) or length < 16 or length > 256:
            raise SecretError("secret length must be between 16 and 256")
        existing = self._generation_state.get(name)
        if existing is not None:
            if not isinstance(existing, str) or not existing:
                raise SecretError("generation state contains an invalid secret")
            self._generated_names.append(name)
            return existing
        value = secure_secrets.token_urlsafe(length)
        self._generation_state[name] = value
        self._generated_names.append(name)
        return value

    def encrypt_yaml(self, plaintext: bytes, *, path: Path) -> bytes:
        """Encrypt YAML with SOPS using only reviewed age recipients.

        Plaintext is held in a mode-0600 temporary file and removed even if
        SOPS rejects it or the subprocess adapter raises.
        """
        self.validate()
        if not isinstance(plaintext, bytes) or not plaintext:
            raise SecretError("plaintext must be non-empty bytes")
        if not isinstance(path, Path) or not path.parent.is_dir():
            raise SecretError("ciphertext path parent must exist")
        assert self._prerequisites is not None
        temporary_name: str | None = None
        try:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=".smartpbx-secrets-", suffix=".plaintext", dir=str(path.parent)
            )
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as temporary:
                temporary.write(plaintext)
                temporary.flush()
                os.fsync(temporary.fileno())
            argv = [
                str(self._prerequisites.sops_binary),
                "--encrypt",
                "--input-type",
                "yaml",
                "--output-type",
                "yaml",
                "--age",
                ",".join(self._recipients),
                temporary_name,
            ]
            try:
                result = self._runner(argv, capture_output=True, check=False, shell=False, text=False)
            except OSError as error:
                raise SecretError("SOPS encryption failed") from error
            if getattr(result, "returncode", 1) != 0:
                raise SecretError("SOPS encryption failed")
            ciphertext = getattr(result, "stdout", b"")
            if not isinstance(ciphertext, bytes) or not ciphertext:
                raise SecretError("SOPS encryption returned no ciphertext")
            return ciphertext
        finally:
            if temporary_name is not None:
                try:
                    os.unlink(temporary_name)
                except FileNotFoundError:
                    pass

    def audit_report(self) -> SecretAudit:
        return SecretAudit(fetched_names=self._fetched_names, generated_names=self._generated_names)
