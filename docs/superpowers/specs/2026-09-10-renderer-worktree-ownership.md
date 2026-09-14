# Renderer worktree ownership gate

## Contract

Public backend, website, and operations renderers accept an exact
`WorktreeHandle` plus its `WorktreeManager`; they do not accept an arbitrary
output directory. Before any template, transaction-recovery, secret, or cleanup
write, the manager must confirm identity ownership of that exact handle.

Each renderer derives its write root from `handle.target`: backend artifacts use
`target / resources.folder_identity`, website artifacts use `target`, and
operations artifacts use `target / agents / resources.slug`.

The ownership gate rejects unowned, constructed, unavailable, relative, or
symlink-traversing targets before mutation. Every generated-path component and
cleanup target is checked for symlinks. Failure cleanup remains narrow and never
removes a primary checkout or a parent worktree.

## Test-only seam

`tests/_owned_worktree_fixture.py` is explicitly private and synthetic. It
records an identity-owned handle without invoking `WorktreeManager.create`, Git,
or any subprocess, and is therefore incapable of creating or mutating a real
checkout. Production code has no equivalent output-directory fallback.
