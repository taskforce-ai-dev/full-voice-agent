from pathlib import Path

import pytest

from smartpbx_agent_factory.gitops import (
    DirtyWorktreeError,
    WorktreeHandle,
    WorktreeConflictError,
    WorktreeManager,
)


def test_worktree_manager_rejects_dirty_primary_without_fetching(tmp_path):
    primary = tmp_path / "primary"
    primary.mkdir()
    (primary / ".git").mkdir()
    calls: list[tuple[str, ...]] = []

    def run(args):
        calls.append(tuple(args))
        if args[-2:] == ("status", "--porcelain"):
            return " M server.py\n"
        raise AssertionError("must stop before mutation")

    manager = WorktreeManager(tmp_path / "generated", run=run)
    with pytest.raises(DirtyWorktreeError, match="dirty"):
        manager.create(
            primary=primary,
            remote="origin",
            revision="a" * 40,
            target=tmp_path / "generated" / "site",
        )
    assert not any("fetch" in call for call in calls)


def test_worktree_manager_rejects_target_outside_explicit_root(tmp_path):
    manager = WorktreeManager(tmp_path / "generated", run=lambda args: "")
    with pytest.raises(WorktreeConflictError, match="temporary root"):
        manager.create(
            primary=tmp_path / "primary",
            remote="origin",
            revision="a" * 40,
            target=tmp_path / "outside",
        )


def test_worktree_manager_pins_fetched_full_remote_sha(tmp_path):
    primary = tmp_path / "primary"
    primary.mkdir()
    (primary / ".git").mkdir()
    calls: list[tuple[str, ...]] = []

    def run(args):
        calls.append(tuple(args))
        if args[-2:] == ("status", "--porcelain"):
            return ""
        if args[-1] == "origin/main":
            return "a" * 40 + "\n"
        return ""

    target = tmp_path / "generated" / "site"
    manager = WorktreeManager(tmp_path / "generated", run=run)
    handle = manager.create(primary=primary, remote="origin", revision="a" * 40, target=target)
    assert handle.revision == "a" * 40
    assert calls[-1] == ("git", "-C", str(primary), "worktree", "add", "--detach", str(target), "a" * 40)


@pytest.mark.parametrize("remote", ("--upload-pack=evil", "https://example.invalid/repo.git", "origin/main"))
def test_worktree_manager_rejects_non_name_remote_before_any_git_call(tmp_path, remote):
    primary = tmp_path / "primary"
    primary.mkdir()
    (primary / ".git").mkdir()
    calls: list[tuple[str, ...]] = []
    manager = WorktreeManager(tmp_path / "generated", run=lambda args: calls.append(tuple(args)) or "")
    with pytest.raises(WorktreeConflictError, match="remote name"):
        manager.create(
            primary=primary,
            remote=remote,
            revision="a" * 40,
            target=tmp_path / "generated" / "site",
        )
    assert calls == []


def test_worktree_manager_rejects_constructed_handle_on_remove(tmp_path):
    primary = tmp_path / "primary"
    target = tmp_path / "generated" / "site"
    primary.mkdir()
    (primary / ".git").mkdir()
    target.mkdir(parents=True)
    manager = WorktreeManager(tmp_path / "generated", run=lambda args: "")
    constructed = WorktreeHandle(primary=primary, target=target, revision="a" * 40)
    with pytest.raises(WorktreeConflictError, match="created by this manager"):
        manager.remove(constructed)


def test_worktree_manager_rejects_a_value_equal_constructed_handle(tmp_path):
    primary = tmp_path / "primary"
    primary.mkdir()
    (primary / ".git").mkdir()
    target = tmp_path / "generated" / "site"

    def run(args):
        if args[-2:] == ("status", "--porcelain"):
            return ""
        if args[-1] == "origin/main":
            return "a" * 40
        return ""

    manager = WorktreeManager(tmp_path / "generated", run=run)
    handle = manager.create(primary=primary, remote="origin", revision="a" * 40, target=target)
    equal_but_constructed = WorktreeHandle(handle.primary, handle.target, handle.revision)
    assert equal_but_constructed == handle
    with pytest.raises(WorktreeConflictError, match="created by this manager"):
        manager.remove(equal_but_constructed)
