from pathlib import Path

import pytest

from smartpbx_agent_factory.gitops import (
    DirtyWorktreeError,
    GitWorktreeInspector,
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
    primary = tmp_path / "primary"
    primary.mkdir()
    (primary / ".git").mkdir()
    manager = WorktreeManager(tmp_path / "generated", run=lambda args: "")
    with pytest.raises(WorktreeConflictError, match="explicit temporary root"):
        manager.create(
            primary=primary,
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
    equal_but_constructed = WorktreeHandle(
        handle.primary,
        handle.target,
        handle.revision,
        handle.temporary_root,
        handle.ownership_token,
    )
    assert equal_but_constructed == handle
    with pytest.raises(WorktreeConflictError, match="created by this manager"):
        manager.remove(equal_but_constructed)


def test_generation_branch_starts_at_pinned_base_and_records_its_later_head(tmp_path):
    primary = tmp_path / "primary"
    primary.mkdir()
    (primary / ".git").mkdir()
    target = tmp_path / "generated" / "site"
    calls: list[tuple[str, ...]] = []

    def run(args):
        calls.append(tuple(args))
        if args[-2:] == ("status", "--porcelain"):
            return ""
        if args[-1] == "origin/main":
            return "a" * 40
        if args[-2:] == ("list", "--porcelain"):
            return f"worktree {target}\nHEAD {'b' * 40}\nbranch refs/heads/smartpbx-agent-factory/gen-001\n\n"
        if args[-2:] == ("rev-parse", "HEAD"):
            return "b" * 40
        return ""

    manager = WorktreeManager(tmp_path / "generated", run=run)
    handle = manager.create(
        primary=primary,
        remote="origin",
        revision="a" * 40,
        target=target,
        branch="smartpbx-agent-factory/gen-001",
    )
    assert handle.revision == "a" * 40
    assert handle.branch == "smartpbx-agent-factory/gen-001"
    assert ("git", "-C", str(primary), "worktree", "add", "-b", handle.branch, str(target), "a" * 40) in calls
    updated = manager.record_current_head(handle)
    assert updated.revision == "b" * 40
    manager.remove_recorded(updated)
    assert ("git", "-C", str(primary), "worktree", "remove", str(target)) in calls


def test_recorded_worktree_refuses_unknown_head_drift(tmp_path):
    primary = tmp_path / "primary"
    primary.mkdir()
    (primary / ".git").mkdir()
    target = tmp_path / "generated" / "site"

    def run(args):
        if args[-2:] == ("status", "--porcelain"):
            return ""
        if args[-1] == "origin/main":
            return "a" * 40
        if args[-2:] == ("list", "--porcelain"):
            return f"worktree {target}\nHEAD {'c' * 40}\nbranch refs/heads/smartpbx-agent-factory/gen-001\n\n"
        return ""

    manager = WorktreeManager(tmp_path / "generated", run=run)
    handle = manager.create(
        primary=primary,
        remote="origin",
        revision="a" * 40,
        target=target,
        branch="smartpbx-agent-factory/gen-001",
    )
    with pytest.raises(WorktreeConflictError, match="revision"):
        manager.remove_recorded(handle)


def test_real_inspector_requires_the_exact_recorded_handle_and_rechecks_git_state(tmp_path):
    primary = tmp_path / "primary"
    target = tmp_path / "generated" / "backend"
    primary.mkdir()
    (primary / ".git").mkdir()

    def run(args):
        if "worktree" in args and "add" in args:
            target.mkdir(parents=True)
            (target / ".git").write_text("gitdir: simulated\n", encoding="utf-8")
            return ""
        if args[-2:] == ("status", "--porcelain"):
            return ""
        if args[-1] == "origin/main":
            return "a" * 40
        if args[-2:] == ("get-url", "origin"):
            return "https://github.com/taskforce-ai-dev/full-voice-agent.git\n"
        if args[-2:] == ("branch", "--show-current"):
            return "smartpbx-agent-factory/gen-001\n"
        if args[-2:] == ("rev-parse", "HEAD"):
            return "b" * 40 + "\n"
        return ""

    manager = WorktreeManager(tmp_path / "generated", run=run)
    handle = manager.create(
        primary=primary,
        remote="origin",
        revision="a" * 40,
        target=target,
        branch="smartpbx-agent-factory/gen-001",
    )
    inspection = GitWorktreeInspector((handle,), run=run).inspect_worktree(
        path=target,
        ownership_handle=handle.ownership_token,
    )
    assert inspection.repository == "taskforce-ai-dev/full-voice-agent"
    assert inspection.branch == "smartpbx-agent-factory/gen-001"
    assert inspection.head_sha == "b" * 40
    assert inspection.clean is True

    with pytest.raises(WorktreeConflictError, match="ownership"):
        GitWorktreeInspector((handle,), run=run).inspect_worktree(path=target, ownership_handle="c" * 64)
