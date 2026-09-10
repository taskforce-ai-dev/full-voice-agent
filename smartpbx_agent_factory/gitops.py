"""Isolated worktree operations with no reset, clean, or shell execution."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
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


Runner = Callable[[Sequence[str]], str]
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


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

    def create(
        self, *, primary: Path, remote: str, revision: str, target: Path
    ) -> WorktreeHandle:
        primary = self._validate_primary(primary)
        target = self._validate_target(target)
        if not isinstance(remote, str) or not remote:
            raise WorktreeConflictError("remote is required")
        if not isinstance(revision, str) or not _SHA_RE.fullmatch(revision):
            raise WorktreeConflictError("revision must be a full 40-hex SHA")

        status = self._run(("git", "-C", str(primary), "status", "--porcelain"))
        if status.strip():
            raise DirtyWorktreeError("primary worktree is dirty")
        self._run(("git", "-C", str(primary), "fetch", remote, "--prune"))
        resolved = self._run(("git", "-C", str(primary), "rev-parse", f"{remote}/main")).strip()
        if not _SHA_RE.fullmatch(resolved):
            raise WorktreeConflictError("remote main did not resolve to a full 40-hex SHA")
        if resolved != revision:
            raise WorktreeConflictError("requested revision does not match fetched remote main")
        self._run(("git", "-C", str(primary), "worktree", "add", "--detach", str(target), resolved))
        return WorktreeHandle(primary=primary, target=target, revision=resolved)

    def remove(self, handle: WorktreeHandle) -> None:
        if not isinstance(handle, WorktreeHandle):
            raise WorktreeConflictError("worktree handle is invalid")
        target = self._validate_target(handle.target, must_not_exist=False)
        if not target.exists():
            return
        self._run(("git", "-C", str(handle.primary), "worktree", "remove", str(target)))

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
