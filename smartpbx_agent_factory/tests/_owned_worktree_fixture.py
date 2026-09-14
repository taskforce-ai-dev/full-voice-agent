"""Private, synthetic ownership fixtures for renderer specifications.

This module deliberately never calls ``WorktreeManager.create`` or a subprocess.
It only installs an identity-owned handle into a manager for a directory supplied
by pytest's temporary-path fixture, so it cannot create or mutate a real checkout.
"""

from pathlib import Path

from smartpbx_agent_factory.gitops import WorktreeHandle, WorktreeManager


def fixture_owned_worktree(target: Path) -> tuple[WorktreeManager, WorktreeHandle]:
    """Return a manager-owned synthetic handle for one existing fixture directory."""
    target = Path(target).absolute()
    temporary_root = target.parent.absolute()
    manager = WorktreeManager(temporary_root, run=_forbidden_fixture_runner)
    handle = WorktreeHandle(
        primary=temporary_root / ".fixture-primary",
        target=target,
        revision="a" * 40,
        temporary_root=temporary_root,
        ownership_token="f" * 64,
        branch="smartpbx-agent-factory/fixture",
    )
    # This is a fixture-only identity record; no Git operation has occurred.
    manager._handles[id(handle)] = handle
    return manager, handle


def _forbidden_fixture_runner(_args: object) -> str:
    raise AssertionError("renderer ownership fixture must never invoke Git")
