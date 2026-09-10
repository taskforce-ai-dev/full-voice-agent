"""Strict, non-secret bootstrap for the review-only factory CLI.

The configuration deliberately identifies *where* approved credentials and
repositories live, never the credential values themselves.  Network-facing
adapters are command-injected so contract tests can inspect argv without
calling GitHub.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence

from .gitops import WorktreeManager
from .orchestrator import (
    GenerationBinding,
    GenerationBlockedError,
    GenerationOrchestrator,
    LaneBinding,
    RepositoryOwnedCIVerificationCoordinator,
)
from .provenance import validate_allowlist_metadata
from .prs import (
    GenerationOwnershipEvidence,
    GenerationWorktree,
    PRProvider,
    WorktreeInspection,
    WorktreeInspector,
    open_linked_prs,
)
from .readiness import ReadinessAuthority, ReadinessEvidence
from .secrets import (
    CredentialSourcePolicy,
    OperationsPrerequisites,
    RepositoryVisibilityVerifier,
    SopsAgeSecretProvider,
)
from .verify import VerificationReport, load_lifecycle_attestation


class FactoryConfigError(ValueError):
    """Raised when a config is incomplete, mutable, or contains secret material."""


_SHA = re.compile(r"^[0-9a-f]{40}$")
_REMOTE = re.compile(r"^[A-Za-z][A-Za-z0-9._-]*$")
_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_AGE = re.compile(r"^age1[ac-hj-np-z02-9]{20,}$")
_ENV = re.compile(r"^[A-Z][A-Z0-9_]{2,127}$")
_POLICY_ID = re.compile(r"^[a-z][a-z0-9-]*(?:/[a-z][a-z0-9_-]*)+$")
_CREDENTIAL_LIKE = re.compile(r"(?:gh[pousr]_[A-Za-z0-9_-]{8,}|sk-[A-Za-z0-9_-]{8,}|-----BEGIN)", re.I)
_ROLES = ("backend", "operations", "website")


def _object(value: object, label: str, keys: set[str]) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != keys:
        raise FactoryConfigError(f"{label} has an invalid schema")
    return value


def _text(value: object, label: str, *, pattern: re.Pattern[str] | None = None) -> str:
    if not isinstance(value, str) or not value or "\x00" in value or _CREDENTIAL_LIKE.search(value):
        raise FactoryConfigError(f"{label} must be non-secret text")
    if pattern is not None and not pattern.fullmatch(value):
        raise FactoryConfigError(f"{label} is invalid")
    return value


def _absolute_path(value: object, label: str) -> Path:
    text = _text(value, label)
    path = Path(text)
    if not path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise FactoryConfigError(f"{label} must be an absolute path")
    return path


@dataclass(frozen=True)
class LaneConfig:
    primary: Path
    remote: str
    canonical_remote: str
    revision: str
    target_root: Path
    repository: str
    base_branch: str
    ci_check: str
    ci_policy: str


@dataclass(frozen=True)
class CIConfig:
    repository: str
    workflow: str
    gh_binary: Path


@dataclass(frozen=True)
class AgeConfig:
    recipient_file: Path
    approved_recipient_fingerprints: tuple[str, ...]
    recipient_review_source: str
    credential_source_policy: Mapping[str, CredentialSourcePolicy]
    sops_binary: Path
    age_binary: Path


@dataclass(frozen=True)
class FactoryConfig:
    """Versioned, strict, immutable input for one factory installation."""

    path: Path
    state_root: Path
    catalogue: Path
    lanes: Mapping[str, LaneConfig]
    age: AgeConfig
    ci: CIConfig

    @classmethod
    def load(cls, path: Path) -> "FactoryConfig":
        path = _absolute_path(str(path), "config path")
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise FactoryConfigError("config must be readable strict JSON") from error
        root = _object(raw, "config", {"version", "state_root", "catalogue", "lanes", "age", "ci"})
        if root["version"] != 1:
            raise FactoryConfigError("config version must be 1")
        lanes_raw = _object(root["lanes"], "lanes", set(_ROLES))
        lanes: dict[str, LaneConfig] = {}
        for role in _ROLES:
            item = _object(
                lanes_raw[role], role,
                {"primary", "remote", "canonical_remote", "revision", "target_root", "repository", "base_branch", "ci_check", "ci_policy"},
            )
            revision = _text(item["revision"], f"{role}.revision", pattern=_SHA)
            if revision.lower() != revision or revision == "0" * 40:
                raise FactoryConfigError(f"{role}.revision must be an immutable full SHA")
            canonical_remote = _text(item["canonical_remote"], f"{role}.canonical_remote")
            if not (canonical_remote.startswith("https://") or canonical_remote.startswith("ssh://") or canonical_remote.startswith("git@")):
                raise FactoryConfigError(f"{role}.canonical_remote must be an exact Git remote URL")
            lanes[role] = LaneConfig(
                _absolute_path(item["primary"], f"{role}.primary"),
                _text(item["remote"], f"{role}.remote", pattern=_REMOTE),
                canonical_remote,
                revision,
                _absolute_path(item["target_root"], f"{role}.target_root"),
                _text(item["repository"], f"{role}.repository", pattern=_REPOSITORY),
                _text(item["base_branch"], f"{role}.base_branch", pattern=_REMOTE),
                _text(item["ci_check"], f"{role}.ci_check"),
                _text(item["ci_policy"], f"{role}.ci_policy"),
            )
            if lanes[role].ci_policy not in {"lifecycle-attestation", "secret-static", "website-build"} or (role == "backend") != (lanes[role].ci_policy == "lifecycle-attestation"):
                raise FactoryConfigError(f"{role}.ci_policy is not approved")
        roots = [lane.target_root for lane in lanes.values()]
        if len(set(roots)) != len(roots):
            raise FactoryConfigError("lane target roots must be distinct")
        age_raw = _object(
            root["age"], "age",
            {"recipient_file", "approved_recipient_fingerprints", "recipient_review_source", "credential_source_policy", "sops_binary", "age_binary"},
        )
        fingerprints_raw = age_raw["approved_recipient_fingerprints"]
        if not isinstance(fingerprints_raw, list) or not fingerprints_raw:
            raise FactoryConfigError("age approved recipients are required")
        fingerprints = tuple(_text(item, "age recipient", pattern=_AGE) for item in fingerprints_raw)
        if len(set(fingerprints)) != len(fingerprints):
            raise FactoryConfigError("age approved recipients must be distinct")
        policy_raw = age_raw["credential_source_policy"]
        if not isinstance(policy_raw, dict) or not policy_raw:
            raise FactoryConfigError("credential source policy is required")
        policy: dict[str, CredentialSourcePolicy] = {}
        for record_id, item in policy_raw.items():
            name = _text(record_id, "credential policy record", pattern=_POLICY_ID)
            policy_item = _object(item, f"credential policy {name}", {"provider", "path", "rotation_owner"})
            provider = _text(policy_item["provider"], f"credential policy {name}.provider")
            if provider != "environment":
                raise FactoryConfigError("only the approved environment credential-source adapter is configured")
            policy[name] = CredentialSourcePolicy(
                provider=provider,
                path=_text(policy_item["path"], f"credential policy {name}.path", pattern=_ENV),
                rotation_owner=_text(policy_item["rotation_owner"], f"credential policy {name}.rotation_owner"),
            )
        ci_raw = _object(root["ci"], "ci", {"repository", "workflow", "gh_binary"})
        return cls(
            path=path,
            state_root=_absolute_path(root["state_root"], "state_root"),
            catalogue=_absolute_path(root["catalogue"], "catalogue"),
            lanes=lanes,
            age=AgeConfig(
                recipient_file=_absolute_path(age_raw["recipient_file"], "age.recipient_file"),
                approved_recipient_fingerprints=fingerprints,
                recipient_review_source=_text(age_raw["recipient_review_source"], "age.recipient_review_source"),
                credential_source_policy=policy,
                sops_binary=_absolute_path(age_raw["sops_binary"], "age.sops_binary"),
                age_binary=_absolute_path(age_raw["age_binary"], "age.age_binary"),
            ),
            ci=CIConfig(
                repository=_text(ci_raw["repository"], "ci.repository", pattern=_REPOSITORY),
                workflow=_text(ci_raw["workflow"], "ci.workflow"),
                gh_binary=_absolute_path(ci_raw["gh_binary"], "ci.gh_binary"),
            ),
        )


def _run(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(argv), capture_output=True, text=True, check=False, shell=False)


def inspect_config(config: FactoryConfig, *, runner: Callable[[Sequence[str]], object] = _run) -> Mapping[str, str]:
    """Read-only repository and collision validation; it never fetches or checks out."""
    checks: dict[str, str] = {}
    for role, lane in config.lanes.items():
        checks[f"{role}.primary"] = "ready" if lane.primary.is_dir() and (lane.primary / ".git").exists() else "blocked: primary unavailable"
        if checks[f"{role}.primary"] != "ready":
            continue
        commands = {
            "remote": ("git", "-C", str(lane.primary), "config", "--get", f"remote.{lane.remote}.url"),
            "revision": ("git", "-C", str(lane.primary), "rev-parse", "--verify", f"{lane.revision}^{{commit}}"),
            "clean": ("git", "-C", str(lane.primary), "status", "--porcelain"),
            "worktrees": ("git", "-C", str(lane.primary), "worktree", "list", "--porcelain"),
        }
        results: dict[str, object] = {}
        try:
            results = {name: runner(argv) for name, argv in commands.items()}
        except Exception:
            checks[f"{role}.repository"] = "blocked: Git inspection unavailable"
            continue
        remote = getattr(results["remote"], "stdout", "").strip()
        revision = getattr(results["revision"], "stdout", "").strip()
        dirty = getattr(results["clean"], "stdout", "")
        listed = getattr(results["worktrees"], "stdout", "")
        if any(getattr(results[name], "returncode", 1) != 0 for name in commands):
            checks[f"{role}.repository"] = "blocked: Git inspection failed"
        elif remote.rstrip("/") != lane.canonical_remote.rstrip("/"):
            checks[f"{role}.repository"] = "blocked: configured remote differs"
        elif revision != lane.revision:
            checks[f"{role}.repository"] = "blocked: configured immutable revision unavailable"
        elif dirty.strip():
            checks[f"{role}.repository"] = "blocked: primary worktree is dirty"
        elif not lane.target_root.is_dir() or lane.target_root.is_symlink():
            checks[f"{role}.repository"] = "blocked: target root unavailable"
        elif f"worktree {lane.target_root}\n" in listed:
            checks[f"{role}.repository"] = "blocked: target root collision"
        else:
            checks[f"{role}.repository"] = "ready"
    checks["catalogue"] = "ready" if config.catalogue.is_file() else "blocked: catalogue unavailable"
    checks["age_recipients"] = "ready" if config.age.recipient_file.is_file() else "blocked: age recipient file unavailable"
    return checks


class GitHubCommandAdapter(RepositoryVisibilityVerifier, PRProvider, WorktreeInspector):
    """Concrete ``gh`` seam.  Nothing runs until a CLI operation reaches its gate."""

    def __init__(self, config: FactoryConfig, *, runner: Callable[[Sequence[str]], object] = _run) -> None:
        self._config, self._runner = config, runner

    def _call(self, argv: Sequence[str]) -> str:
        try:
            result = self._runner(argv)
        except OSError as error:
            raise GenerationBlockedError("GitHub command unavailable") from error
        if getattr(result, "returncode", 1) != 0:
            raise GenerationBlockedError("GitHub command could not be authoritatively checked")
        return str(getattr(result, "stdout", "")).strip()

    def is_private(self, *, repository: Path, canonical_remote: str) -> bool:
        lane = self._config.lanes["operations"]
        if repository.resolve() != lane.primary.resolve() or canonical_remote.rstrip("/") != lane.canonical_remote.rstrip("/"):
            return False
        return self._call((str(self._config.ci.gh_binary), "repo", "view", lane.repository, "--json", "isPrivate", "--jq", ".isPrivate")) == "true"

    def inspect_worktree(self, *, path: Path, ownership_handle: str) -> WorktreeInspection:
        role, lane = _lane_for_path(self._config, path)
        branch = self._call(("git", "-C", str(path), "branch", "--show-current"))
        head = self._call(("git", "-C", str(path), "rev-parse", "HEAD"))
        clean = not self._call(("git", "-C", str(path), "status", "--porcelain"))
        return WorktreeInspection(path, lane.repository, branch, head, clean, ownership_handle)

    def push_generated_branch(self, *, role: str, path: Path, remote: str, branch: str) -> None:
        if role not in _ROLES or not re.fullmatch(r"smartpbx-agent-factory/[a-z0-9][a-z0-9-]{0,63}", branch):
            raise GenerationBlockedError("only a generated review branch may be pushed")
        _, lane = _lane_for_path(self._config, path)
        if remote != lane.remote:
            raise GenerationBlockedError("configured remote is required for generated branch push")
        self._call(("git", "-C", str(path), "push", remote, f"refs/heads/{branch}:refs/heads/{branch}"))

    def open_pull_request(self, *, repository: str, branch: str, title: str, body: str) -> str:
        if repository not in {lane.repository for lane in self._config.lanes.values()}:
            raise GenerationBlockedError("PR repository is not configured")
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as handle:
            handle.write(body)
            body_path = Path(handle.name)
        try:
            lane = next(lane for lane in self._config.lanes.values() if lane.repository == repository)
            return self._call((str(self._config.ci.gh_binary), "pr", "create", "--repo", repository, "--head", branch, "--base", lane.base_branch, "--title", title, "--body-file", str(body_path)))
        finally:
            body_path.unlink(missing_ok=True)

    def comment_pull_request(self, *, pull_request_url: str, body: str) -> None:
        self._call((str(self._config.ci.gh_binary), "pr", "comment", pull_request_url, "--body", body))

    def update_pull_request_body(self, *, pull_request_url: str, body: str) -> None:
        self._call((str(self._config.ci.gh_binary), "pr", "edit", pull_request_url, "--body", body))


def _lane_for_path(config: FactoryConfig, path: Path) -> tuple[str, LaneConfig]:
    resolved = path.resolve()
    for role, lane in config.lanes.items():
        try:
            resolved.relative_to(lane.target_root)
            return role, lane
        except ValueError:
            continue
    raise GenerationBlockedError("worktree is outside configured target roots")


class GitHubCIResultAdapter:
    """Checks repository-owned CI output for exact lane SHA and artifact digest."""

    def __init__(self, config: FactoryConfig, *, runner: Callable[[Sequence[str]], object] = _run) -> None:
        self._config, self._runner = config, runner

    def preflight(self) -> None:
        try:
            validate_allowlist_metadata(json.loads((Path(__file__).parent / "template_v1" / "file_allowlist.json").read_text(encoding="utf-8")))
        except Exception as error:
            raise GenerationBlockedError("approved template provenance is unavailable") from error

    def publish_for_ci(self, *, generation_id: str, inventory: object) -> None:
        """Publish only exact, clean factory branches so repository CI can attest them."""
        handles = getattr(inventory, "worktrees", ())
        publisher = GitHubCommandAdapter(self._config, runner=self._runner)
        for role, lane in self._config.lanes.items():
            matches = [item for item in handles if getattr(item, "target", None) == lane.target_root / generation_id]
            if len(matches) != 1:
                raise GenerationBlockedError("CI publication requires exact generated worktrees")
            handle = WorktreeManager(lane.target_root).reuse_recorded(matches[0])
            if handle.branch is None:
                raise GenerationBlockedError("CI publication requires generated review branches")
            publisher.push_generated_branch(role=role, path=handle.target, remote=lane.remote, branch=handle.branch)

    def verify(self, *, generation_id: str, resources: object, lane_records: Mapping[str, Mapping[str, str]]) -> tuple[ReadinessEvidence, Mapping[str, VerificationReport]]:
        self.preflight()
        allowlist = validate_allowlist_metadata(json.loads((Path(__file__).parent / "template_v1" / "file_allowlist.json").read_text(encoding="utf-8")))
        if set(lane_records) != set(_ROLES):
            raise GenerationBlockedError("CI requires exact committed records for all lanes")
        for role in _ROLES:
            record = lane_records[role]
            sha, artifact = record.get("head_sha"), record.get("artifact_digest")
            if not isinstance(sha, str) or not _SHA.fullmatch(sha) or not isinstance(artifact, str) or not re.fullmatch(r"[0-9a-f]{64}", artifact):
                raise GenerationBlockedError("CI requires immutable lane SHA and artifact digest")
            lane = self._config.lanes[role]
            result = self._runner((str(self._config.ci.gh_binary), "api", f"repos/{lane.repository}/commits/{sha}/check-runs"))
            if getattr(result, "returncode", 1) != 0:
                raise GenerationBlockedError("external CI result is pending or cannot be authoritatively checked")
            try:
                checks = json.loads(str(getattr(result, "stdout", ""))).get("check_runs", [])
            except (TypeError, ValueError) as error:
                raise GenerationBlockedError("external CI result is malformed") from error
            matches = [check for check in checks if isinstance(check, dict) and check.get("name") == lane.ci_check and check.get("conclusion") == "success" and check.get("head_sha", sha) == sha]
            required_markers = (sha, artifact)
            if lane.ci_policy == "lifecycle-attestation":
                required_markers += ("smartpbx-ci-lifecycle-attestations",)
            if not matches or not any(all(marker in json.dumps(check, sort_keys=True) for marker in required_markers) for check in matches):
                raise GenerationBlockedError("external CI result is pending or lacks exact lane provenance")
            if lane.ci_policy == "lifecycle-attestation":
                details = str(matches[0].get("details_url", ""))
                run_match = re.search(r"/actions/runs/([1-9][0-9]*)", details)
                if run_match is None:
                    raise GenerationBlockedError("repository-owned lifecycle attestation is unavailable")
                with tempfile.TemporaryDirectory(prefix=".smartpbx-ci-", dir=str(lane.target_root)) as directory:
                    os.chmod(directory, 0o700)
                    downloaded = self._runner((str(self._config.ci.gh_binary), "run", "download", run_match.group(1), "--repo", lane.repository, "-n", "smartpbx-ci-lifecycle-attestations", "-D", directory))
                    if getattr(downloaded, "returncode", 1) != 0:
                        raise GenerationBlockedError("repository-owned lifecycle attestation is unavailable")
                    files = tuple(Path(directory).glob("*.json"))
                    if len(files) != 1:
                        raise GenerationBlockedError("repository-owned lifecycle attestation is unavailable")
                    attestation = load_lifecycle_attestation(files[0], agent_dir=lane.target_root / generation_id / "SmartPBX Agents" / getattr(resources, "slug"), lane=self._config.ci.workflow, source_sha=sha)
                    if attestation.artifact_digest != artifact:
                        raise GenerationBlockedError("lifecycle attestation artifact digest differs from lane record")
        artifact_digests = {role: lane_records[role]["artifact_digest"] for role in _ROLES}
        reports = {
            role: VerificationReport(
                agent_slug=getattr(resources, "slug"), artifact_digest=artifact_digests[role],
                template_version=allowlist.template_version, source_revision=allowlist.source_revision,
                ci_identifier=getattr(resources, "ci_identifier"), protocol_events=("connected", "start", "media", "stop", "hangup"),
                static_contracts_passed=True, runtime_lifecycle_verified=True, ready_for_pr=True,
                runtime_status="CI_LIFECYCLE_VERIFIED",
                evidence=("Dockerfile", "server.py", "smartpbx_gateway.py", "smartpbx_protocol.py", "smartpbx_transport.py", "smartpbx_diagnostics.py", "docker-compose.yml", ".github-workflow-fragment.yml", ".smartpbx-factory-provenance.json"),
            )
            for role in _ROLES
        }
        digest = lambda value: hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        ownership = digest({role: lane_records[role]["head_sha"] for role in _ROLES})
        readiness = ReadinessEvidence(
            readiness_report_path=Path(".smartpbx-generations") / generation_id / "readiness.json",
            readiness_verified=True, secret_scan_passed=True, ci_registered=True,
            provenance_source_revision=allowlist.source_revision, template_revision=allowlist.source_revision,
            artifact_digests=artifact_digests, review_label=f"SmartPBX {getattr(resources, 'slug')}",
            wss_url=getattr(resources, "wss_url"), expected_wss_hostname=getattr(resources, "smartpbx_hostname"),
            allowed_wss_paths=("/smartpbx",), readiness_digest=digest(lane_records),
            secret_scan_digest=digest({"artifacts": artifact_digests}), ci_registration_digest=digest({"repository": self._config.ci.repository, "workflow": self._config.ci.workflow}),
            provenance_digest=digest({"source_revision": allowlist.source_revision}), worktree_ownership_digest=ownership,
        )
        return readiness, reports

    def worktrees_for(self, *, generation_id: str, inventory: object, readiness: ReadinessEvidence) -> tuple[GenerationWorktree, ...]:
        """Bind the verified CI records to exact persisted generation handles."""
        handles = getattr(inventory, "worktrees", ())
        result: list[GenerationWorktree] = []
        for role, lane in self._config.lanes.items():
            matches = [item for item in handles if getattr(item, "target", None) == lane.target_root / generation_id]
            if len(matches) != 1:
                raise GenerationBlockedError("CI requires an exact generation-owned worktree for every lane")
            handle = matches[0]
            branch = getattr(handle, "branch", None)
            if not isinstance(branch, str) or not re.fullmatch(r"smartpbx-agent-factory/[a-z0-9][a-z0-9-]{0,63}", branch):
                raise GenerationBlockedError("CI requires a generated review branch for every lane")
            result.append(GenerationWorktree(
                role, lane.repository, branch, handle.revision, handle.target, True,
                GenerationOwnershipEvidence(generation_id, lane.target_root, handle.ownership_token, readiness.worktree_ownership_digest),
            ))
        return tuple(result)


class ConfiguredPRCoordinator:
    """Rebuilds only configured managers, pushes generated branches, then opens linked PRs."""

    def __init__(self, config: FactoryConfig, provider: GitHubCommandAdapter) -> None:
        self._config, self._provider = config, provider

    def open(self, *, generation_id: str, state: object, inventory: object) -> object:
        readiness = ReadinessAuthority(self._config.state_root).load(state)
        handles = getattr(inventory, "worktrees", ())
        by_target = {handle.target: handle for handle in handles}
        worktrees: list[GenerationWorktree] = []
        for role, lane in self._config.lanes.items():
            candidates = [handle for target, handle in by_target.items() if target.parent == lane.target_root and target.name == generation_id]
            if len(candidates) != 1:
                raise GenerationBlockedError("exact generation-owned worktree is unavailable")
            handle = WorktreeManager(lane.target_root).reuse_recorded(candidates[0])
            if handle.branch is None:
                raise GenerationBlockedError("only generated review branches may open PRs")
            evidence = GenerationOwnershipEvidence(generation_id, lane.target_root, handle.ownership_token, readiness.worktree_ownership_digest)
            worktrees.append(GenerationWorktree(role, lane.repository, handle.branch, handle.revision, handle.target, True, evidence))
        ReadinessAuthority(self._config.state_root).load(state, worktrees=worktrees)
        for worktree in worktrees:
            role, lane = _lane_for_path(self._config, worktree.path)
            self._provider.push_generated_branch(role=role, path=worktree.path, remote=lane.remote, branch=worktree.branch)
        return open_linked_prs(self._provider, state=state, readiness_authority=ReadinessAuthority(self._config.state_root), worktrees=worktrees, inspector=self._provider)


@dataclass(frozen=True)
class FactoryBootstrap:
    config: FactoryConfig

    def binding_for(self, generation_id: str) -> GenerationBinding:
        if not re.fullmatch(r"gen-[a-f0-9]{32}", generation_id):
            raise FactoryConfigError("generation id is invalid")
        lanes = {
            role: LaneBinding(WorktreeManager(lane.target_root), lane.primary, lane.remote, lane.revision, lane.target_root / generation_id)
            for role, lane in self.config.lanes.items()
        }
        return GenerationBinding(lanes["backend"], lanes["operations"], lanes["website"])

    def secret_provider(self) -> SopsAgeSecretProvider:
        operations = self.config.lanes["operations"]
        policy = self.config.age.credential_source_policy
        prerequisites = OperationsPrerequisites(
            repository_path=operations.primary, canonical_remote=operations.canonical_remote, owner=self.config.ci.repository.split("/", 1)[0],
            recipient_file=self.config.age.recipient_file, approved_recipient_fingerprints=self.config.age.approved_recipient_fingerprints,
            recipient_review_source=self.config.age.recipient_review_source, credential_source_policy=policy,
            sops_binary=self.config.age.sops_binary, age_binary=self.config.age.age_binary,
        )
        def read(policy_item: CredentialSourcePolicy) -> str:
            value = os.environ.get(policy_item.path)
            if not value:
                raise GenerationBlockedError("approved credential source is unavailable")
            return value
        return SopsAgeSecretProvider(prerequisites, generation_state={}, credential_reader=read, visibility_verifier=GitHubCommandAdapter(self.config))

    def orchestrator(self) -> GenerationOrchestrator:
        provider = GitHubCommandAdapter(self.config)
        return GenerationOrchestrator(
            self.config.state_root, catalogue_path=self.config.catalogue,
            verification_coordinator=RepositoryOwnedCIVerificationCoordinator(GitHubCIResultAdapter(self.config)),
            pr_coordinator=ConfiguredPRCoordinator(self.config, provider),
        )
