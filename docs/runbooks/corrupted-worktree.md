# Runbook: Recover a Corrupted Worktree State

Use this when an issue's worktree under `<repo>/build/.worktrees/issue-<N>` is
dirty, abandoned, or blocking a clean re-run. Recovery commands here come
directly from the worktree-manager module docstring
(`hephaestus/automation/worktree_manager.py`).

## Background

The automation pipeline creates one worktree per issue at
`<repo_root>/build/.worktrees/issue-{N}`. Worktrees live inside the repo (not in
`~/.tmp`) so an interrupted run leaves the worktree on disk for a later
invocation to resume or surface. A non-forced removal of a worktree with
uncommitted changes raises `WorktreeDirtyError`.

## Locate

```bash
git -C <repo> worktree list --porcelain
ls -la <repo>/build/.worktrees/issue-<N>
```

### Cross-repo hazard — check before any delete

A `build/.worktrees/` directory can belong to a **different** repository and be
invisible to this repo's `git worktree list`. Always confirm ownership before
removing anything:

```bash
git -C <repo>/build/.worktrees/issue-<N> remote get-url origin
```

If `origin` points at a repo other than the one you are recovering, **stop** —
that worktree belongs to another repo's automation.

## Inspect dirty state

```bash
git -C <repo>/build/.worktrees/issue-<N> status
git -C <repo>/build/.worktrees/issue-<N> diff --stat
```

Uncommitted changes are what cause a non-forced `worktree remove` to raise
`WorktreeDirtyError`. Decide whether the in-flight work is worth keeping before
you discard it.

## Remove cleanly

```bash
# Refuses if the worktree is dirty (preserves uncommitted work):
git -C <repo> worktree remove build/.worktrees/issue-<N>

# Discards uncommitted work and removes anyway:
git -C <repo> worktree remove --force build/.worktrees/issue-<N>
```

## Last resort

If `git worktree remove` itself fails (corrupted git metadata), delete the
directory and prune the stale administrative entry:

```bash
rm -rf <repo>/build/.worktrees/issue-<N>
git -C <repo> worktree prune
```

## After worktree churn

If the recovered worktree (or its removal) touched `pyproject.toml`, the pixi
environment may have re-solved and dropped the editable install, leaving
`hephaestus-*` console scripts dangling. Restore it:

```bash
pixi run dev-install
```

## See also

- [Automation loop crashed mid-issue](automation-loop-crash.md)
- `hephaestus:worktree-cleanup` skill — audit + prune git worktrees
  (never deletes branches).
- `hephaestus:tidy` skill — rebase all local branches.
