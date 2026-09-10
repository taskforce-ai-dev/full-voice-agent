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
