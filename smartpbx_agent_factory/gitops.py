"""Isolated worktree operations with no reset, clean, or shell execution."""

from __future__ import annotations

import re
import secrets
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Sequence


class GitOpsError(RuntimeError):
    """Raised when an isolated Git operation cannot be completed safely."""


class DirtyWorktreeError(GitOpsError):
    """Raised when a primary checkout is not pristine."""


class WorktreeConflictError(GitOpsError):
    """Raised when a target or revision cannot be safely used."""


@dataclass(frozen=True)
class WorktreeHandle:
    primary: Path
    target: Path
    revision: str
    temporary_root: Path = Path("/")
    ownership_token: str = ""
    branch: str | None = None


Runner = Callable[[Sequence[str]], str]
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_REMOTE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9._-]*$")
_OWNERSHIP_TOKEN_RE = re.compile(r"^[0-9a-f]{64}$")
_GENERATION_BRANCH_RE = re.compile(r"^smartpbx-agent-factory/[a-z0-9][a-z0-9-]{0,63}$")


def _subprocess_runner(args: Sequence[str]) -> str:
    result = subprocess.run(
        list(args), capture_output=True, check=False, shell=False, text=True
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "Git command failed"
        raise GitOpsError(detail)
    return result.stdout


class WorktreeManager:
    """Create generation-owned detached worktrees below one explicit root."""

    def __init__(self, temporary_root: Path, *, run: Runner = _subprocess_runner) -> None:
        if not isinstance(temporary_root, Path) or not temporary_root.is_absolute():
            raise WorktreeConflictError("temporary root must be an absolute path")
        self._temporary_root = temporary_root.resolve()
        self._run = run
        self._handles: dict[int, WorktreeHandle] = {}

    def create(
        self, *, primary: Path, remote: str, revision: str, target: Path, branch: str | None = None
    ) -> WorktreeHandle:
        remote = self._validate_remote(remote)
        primary = self._validate_primary(primary)
        target = self._validate_target(target)
        if not isinstance(revision, str) or not _SHA_RE.fullmatch(revision):
            raise WorktreeConflictError("revision must be a full 40-hex SHA")
        if branch is not None and (not isinstance(branch, str) or not _GENERATION_BRANCH_RE.fullmatch(branch)):
            raise WorktreeConflictError("generation branch name is invalid")

        status = self._run(("git", "-C", str(primary), "status", "--porcelain"))
        if status.strip():
            raise DirtyWorktreeError("primary worktree is dirty")
        self._run(("git", "-C", str(primary), "fetch", remote, "--prune"))
        resolved = self._run(("git", "-C", str(primary), "rev-parse", f"{remote}/main")).strip()
        if not _SHA_RE.fullmatch(resolved):
            raise WorktreeConflictError("remote main did not resolve to a full 40-hex SHA")
        if resolved != revision:
            raise WorktreeConflictError("requested revision does not match fetched remote main")
        if branch is None:
            self._run(("git", "-C", str(primary), "worktree", "add", "--detach", str(target), resolved))
        else:
            self._run(("git", "-C", str(primary), "worktree", "add", "-b", branch, str(target), resolved))
        handle = WorktreeHandle(
            primary=primary,
            target=target,
            revision=resolved,
            temporary_root=self._temporary_root,
            ownership_token=secrets.token_hex(32),
            branch=branch,
        )
        self._handles[id(handle)] = handle
        return handle

    def remove(self, handle: WorktreeHandle) -> None:
        if not isinstance(handle, WorktreeHandle):
            raise WorktreeConflictError("worktree handle is invalid")
        if self._handles.get(id(handle)) is not handle:
            raise WorktreeConflictError("worktree handle was not created by this manager")
        primary = self._validate_primary(handle.primary)
        target = self._validate_target(handle.target, must_not_exist=False)
        if primary != handle.primary or target != handle.target:
            raise WorktreeConflictError("worktree handle ownership changed")
        if not target.exists():
            del self._handles[id(handle)]
            return
        self._run(("git", "-C", str(primary), "worktree", "remove", str(target)))
        del self._handles[id(handle)]

    def owns(self, handle: WorktreeHandle) -> bool:
        """Expose the narrow ownership proof needed by transaction cleanup."""
        return isinstance(handle, WorktreeHandle) and self._handles.get(id(handle)) is handle

    def reuse_recorded(self, handle: WorktreeHandle) -> WorktreeHandle:
        """Re-adopt one persisted, exact owned worktree after Git revalidation.

        This is intentionally narrower than ``create``: it never fetches,
        recreates, resets, cleans, or follows a caller path.  It proves the
        target is still the recorded worktree at the immutable revision.
        """
        if not isinstance(handle, WorktreeHandle) or not _OWNERSHIP_TOKEN_RE.fullmatch(handle.ownership_token):
            raise WorktreeConflictError("recorded worktree ownership evidence is invalid")
        if handle.temporary_root != self._temporary_root:
            raise WorktreeConflictError("recorded worktree temporary root does not match manager")
        primary = self._validate_primary(handle.primary)
        target = self._validate_target(handle.target, must_not_exist=False)
        if primary != handle.primary or target != handle.target or not _SHA_RE.fullmatch(handle.revision):
            raise WorktreeConflictError("recorded worktree ownership changed")
        matching = self._worktree_entries(primary).get(target)
        expected_branch = f"refs/heads/{handle.branch}" if handle.branch else None
        if matching != (handle.revision, expected_branch):
            raise WorktreeConflictError("recorded worktree does not match immutable binding")
        self._handles[id(handle)] = handle
        return handle

    def remove_recorded(self, handle: WorktreeHandle) -> None:
        """Remove a state-root-protected handle after authoritative Git validation.

        This recovery path deliberately does not accept a normal constructed
        handle: the caller must supply the opaque token recorded in a 0600
        generation state file, and Git must independently confirm the exact
        primary, target, and detached revision before removal.
        """
        if not isinstance(handle, WorktreeHandle) or not _OWNERSHIP_TOKEN_RE.fullmatch(handle.ownership_token):
            raise WorktreeConflictError("recorded worktree ownership evidence is invalid")
        if handle.branch is not None and not _GENERATION_BRANCH_RE.fullmatch(handle.branch):
            raise WorktreeConflictError("recorded generation branch is invalid")
        if handle.temporary_root != self._temporary_root:
            raise WorktreeConflictError("recorded worktree temporary root does not match manager")
        primary = self._validate_primary(handle.primary)
        target = self._validate_target(handle.target, must_not_exist=False)
        if primary != handle.primary or target != handle.target or not _SHA_RE.fullmatch(handle.revision):
            raise WorktreeConflictError("recorded worktree ownership changed")
        entries = self._worktree_entries(primary)
        matching = entries.get(target)
        if matching is None:
            if target.exists():
                raise WorktreeConflictError("recorded target is not an authoritative Git worktree")
            return
        matching_revision, matching_branch = matching
        if matching_revision != handle.revision:
            raise WorktreeConflictError("recorded worktree revision does not match authoritative Git state")
        if handle.branch is not None and matching_branch != f"refs/heads/{handle.branch}":
            raise WorktreeConflictError("recorded worktree branch does not match authoritative Git state")
        self._run(("git", "-C", str(primary), "worktree", "remove", str(target)))

    def record_current_head(self, handle: WorktreeHandle) -> WorktreeHandle:
        """Persist an authoritative post-commit SHA for a safe generation branch."""
        if self._handles.get(id(handle)) is not handle:
            raise WorktreeConflictError("worktree handle was not created by this manager")
        if handle.branch is None:
            raise WorktreeConflictError("only an explicit generation branch may record a new head")
        if not _GENERATION_BRANCH_RE.fullmatch(handle.branch):
            raise WorktreeConflictError("generation branch name is invalid")
        primary = self._validate_primary(handle.primary)
        target = self._validate_target(handle.target, must_not_exist=False)
        if primary != handle.primary or target != handle.target:
            raise WorktreeConflictError("worktree handle ownership changed")
        entries = self._worktree_entries(primary)
        matching = entries.get(target)
        if matching is None or matching[1] != f"refs/heads/{handle.branch}":
            raise WorktreeConflictError("generation branch is not an authoritative Git worktree")
        head = self._run(("git", "-C", str(target), "rev-parse", "HEAD")).strip()
        if not _SHA_RE.fullmatch(head) or matching[0] != head:
            raise WorktreeConflictError("generation worktree HEAD is not authoritative")
        updated = replace(handle, revision=head)
        del self._handles[id(handle)]
        self._handles[id(updated)] = updated
        return updated

    def stage_and_commit(
        self, handle: WorktreeHandle, *, allowed_paths: Sequence[Path], message: str
    ) -> WorktreeHandle:
        """Commit only expected factory paths, then return an authoritative clean head.

        It is deliberately incapable of pushing.  Any pre-existing or
        renderer-unowned modification is a hard stop rather than collateral
        staging.  The commit identity is fixed to the factory review author.
        """
        if self._handles.get(id(handle)) is not handle or handle.branch is None:
            raise WorktreeConflictError("only a manager-owned generation branch may be committed")
        if not isinstance(message, str) or not re.fullmatch(r"factory\([a-z-]+\): review generation [a-z0-9-]+", message):
            raise WorktreeConflictError("factory review commit message is invalid")
        target = self._validate_target(handle.target, must_not_exist=False)
        safe_paths = tuple(self._relative_commit_path(path) for path in allowed_paths)
        if not safe_paths:
            raise WorktreeConflictError("factory review commit paths are required")
        changed = self._changed_paths(target)
        if not changed:
            raise WorktreeConflictError("factory review lane has no renderer output to commit")
        if any(not any(path == allowed or path.startswith(allowed + "/") for allowed in safe_paths) for path in changed):
            raise WorktreeConflictError("generation worktree has unexpected preexisting changes")
        self._run(("git", "-C", str(target), "add", "--", *safe_paths))
        staged = self._run(("git", "-C", str(target), "diff", "--cached", "--name-only")).splitlines()
        if not staged or set(staged) != set(changed):
            raise WorktreeConflictError("factory review staging does not exactly match renderer output")
        self._run((
            "git", "-C", str(target), "-c", "user.name=thiva2k",
            "-c", "user.email=178917250+thiva2k@users.noreply.github.com",
            "commit", "--author=thiva2k <178917250+thiva2k@users.noreply.github.com>", "-m", message,
        ))
        updated = self.record_current_head(handle)
        if self._run(("git", "-C", str(target), "status", "--porcelain")).strip():
            raise WorktreeConflictError("factory review branch is not clean after commit")
        return updated

    @staticmethod
    def _relative_commit_path(path: Path) -> str:
        if not isinstance(path, Path) or path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
            raise WorktreeConflictError("factory review path is invalid")
        return path.as_posix()

    def _changed_paths(self, target: Path) -> tuple[str, ...]:
        raw = self._run(("git", "-C", str(target), "status", "--porcelain", "--untracked-files=all"))
        paths: list[str] = []
        for line in raw.splitlines():
            if len(line) < 4 or line[2] != " " or line[0] == "?" and line[1] != "?":
                raise WorktreeConflictError("generation worktree status is invalid")
            path = line[3:]
            if not path or " -> " in path or path.startswith('"') or "\x00" in path:
                raise WorktreeConflictError("generation worktree status is invalid")
            paths.append(path)
        return tuple(paths)

    @staticmethod
    def _validate_remote(remote: str) -> str:
        if not isinstance(remote, str) or not _REMOTE_RE.fullmatch(remote):
            raise WorktreeConflictError("remote must be a safe remote name")
        return remote

    def _validate_primary(self, primary: Path) -> Path:
        if not isinstance(primary, Path) or not primary.is_absolute() or not primary.is_dir():
            raise WorktreeConflictError("primary must be an existing absolute Git checkout")
        resolved = primary.resolve()
        if not (resolved / ".git").exists():
            raise WorktreeConflictError("primary must be a Git checkout")
        return resolved

    def _validate_target(self, target: Path, *, must_not_exist: bool = True) -> Path:
        if not isinstance(target, Path) or not target.is_absolute():
            raise WorktreeConflictError("worktree target must be an absolute path")
        resolved = target.resolve()
        try:
            resolved.relative_to(self._temporary_root)
        except ValueError as error:
            raise WorktreeConflictError("worktree target must be inside the explicit temporary root") from error
        if must_not_exist and resolved.exists():
            raise WorktreeConflictError("worktree target already exists")
        return resolved

    def _worktree_entries(self, primary: Path) -> dict[Path, tuple[str, str | None]]:
        raw = self._run(("git", "-C", str(primary), "worktree", "list", "--porcelain"))
        entries: dict[Path, tuple[str, str | None]] = {}
        current_path: Path | None = None
        current_head: str | None = None
        current_branch: str | None = None
        for line in raw.splitlines() + [""]:
            if not line:
                if current_path is not None and current_head is not None:
                    entries[current_path.resolve()] = (current_head, current_branch)
                current_path = None
                current_head = None
                current_branch = None
                continue
            if line.startswith("worktree "):
                current_path = Path(line.removeprefix("worktree "))
            elif line.startswith("HEAD "):
                current_head = line.removeprefix("HEAD ")
            elif line.startswith("branch "):
                current_branch = line.removeprefix("branch ")
        return entries


def manager_owned_worktree_target(manager: WorktreeManager, handle: WorktreeHandle) -> Path:
    """Return one live manager-owned target after rejecting path indirection.

    Renderers use this narrow capability instead of accepting caller-controlled
    output paths.  ``owns`` is identity based, so a value-equal constructed
    handle cannot authorize writes.  The returned target is also required to be
    a real directory whose complete path has no symbolic-link component.
    """
    if not isinstance(manager, WorktreeManager) or not isinstance(handle, WorktreeHandle):
        raise WorktreeConflictError("a manager-owned worktree handle is required")
    if not manager.owns(handle):
        raise WorktreeConflictError("a manager-owned worktree handle is required")
    target = handle.target
    if not isinstance(target, Path) or not target.is_absolute() or not target.is_dir():
        raise WorktreeConflictError("manager-owned worktree target is unavailable")
    for component in (target, *target.parents):
        if component.is_symlink():
            raise WorktreeConflictError("manager-owned worktree target may not traverse a symlink")
    resolved = target.resolve(strict=True)
    if resolved != target:
        raise WorktreeConflictError("manager-owned worktree target is not canonical")
    return resolved


class GitWorktreeInspector:
    """Re-read a manager-owned generation worktree immediately before PR use.

    A caller supplies the opaque token from a ``WorktreeHandle``; this adapter
    never treats a path alone as evidence of generation ownership.
    """

    def __init__(self, handles: Sequence[WorktreeHandle], *, run: Runner = _subprocess_runner) -> None:
        self._run = run
        self._handles = {
            handle.ownership_token: handle
            for handle in handles
            if isinstance(handle, WorktreeHandle)
            and _OWNERSHIP_TOKEN_RE.fullmatch(handle.ownership_token)
        }

    def inspect_worktree(self, *, path: Path, ownership_handle: str):
        """Return bounded Git evidence only for one exact recorded worktree."""
        if not isinstance(path, Path) or not isinstance(ownership_handle, str):
            raise WorktreeConflictError("worktree ownership evidence is invalid")
        handle = self._handles.get(ownership_handle)
        if handle is None or handle.target.resolve() != path.resolve():
            raise WorktreeConflictError("worktree ownership evidence is invalid")
        if handle.branch is None or not _GENERATION_BRANCH_RE.fullmatch(handle.branch):
            raise WorktreeConflictError("generation branch is invalid")
        target = path.resolve()
        if not target.is_dir() or not (target / ".git").exists():
            raise WorktreeConflictError("generation worktree is unavailable")
        remote = self._run(("git", "-C", str(target), "remote", "get-url", "origin")).strip()
        repository = self._github_repository(remote)
        branch = self._run(("git", "-C", str(target), "branch", "--show-current")).strip()
        head = self._run(("git", "-C", str(target), "rev-parse", "HEAD")).strip()
        clean = not self._run(("git", "-C", str(target), "status", "--porcelain")).strip()
        if branch != handle.branch or not _SHA_RE.fullmatch(head):
            raise WorktreeConflictError("generation worktree Git state is invalid")
        # Import locally so the PR contract remains an adapter boundary rather
        # than making gitops a PR coordinator.
        from .prs import WorktreeInspection

        return WorktreeInspection(
            path=target,
            repository=repository,
            branch=branch,
            head_sha=head,
            clean=clean,
            ownership_handle=ownership_handle,
        )

    @staticmethod
    def _github_repository(remote: str) -> str:
        patterns = (
            re.compile(r"https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?"),
            re.compile(r"git@github\.com:([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?"),
            re.compile(r"ssh://git@github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?"),
        )
        for pattern in patterns:
            match = pattern.fullmatch(remote)
            if match:
                return f"{match.group(1)}/{match.group(2)}"
        raise WorktreeConflictError("generation worktree origin is not an exact GitHub repository")
