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

Repository intake uses a separate detached worktree outside the caller
checkout. Each launch prepares its own worktree at
`.hephaestus-repo-intake/<digest>/launches/<launch-identity>/worktree` beside
the Git common directory. A durable registration and a per-launch receipt
record the ownership. The caller checkout can contain local work. Intake does
not switch, reset, clean, stash, or change its index.

## Locate a repository-intake worktree

Use the caller checkout to read the shared worktree registry. Do not remove an
intake path when its receipt is missing, its path does not match, or its state
is dirty. These conditions mean that ownership is not proven.

```bash
git -C <caller-repo> worktree list --porcelain
python3 - '<caller-repo>' <<'PY'
from pathlib import Path
import subprocess
import sys

common_dir_result = subprocess.run(
    [
        "git",
        "-C",
        sys.argv[1],
        "rev-parse",
        "--path-format=absolute",
        "--git-common-dir",
    ],
    check=True,
    capture_output=True,
    text=True,
)
common_dir = Path(common_dir_result.stdout.rstrip("\n")).resolve(strict=True)
state_root = common_dir.parent.parent / ".hephaestus-repo-intake"
if state_root.is_symlink() or state_root.is_file():
    print(state_root)
elif state_root.is_dir():
    subprocess.run(
        ["find", "-P", str(state_root), "-maxdepth", "4", "-print"],
        check=True,
    )
else:
    print(f"No repository-intake state exists at {state_root}")
PY
```

The Python command reads the absolute Git common directory. This method also
works from a linked worktree when Git stores a relative metadata path. The
command does not use shell substitution for the path.

If the intake receipt and registered path are both valid but the checkout is
dirty, preserve the path and inspect it before a later run. If the path is
foreign, symlinked, or ambiguously registered, stop and recover it manually.
The same rule applies when the intake `.git` pointer is a symlink, special file,
malformed pointer, or pointer to a different worktree admin directory. Run the
pointer proofs before you run Git from the intake path. The proofs are the
regular `.git` pointer, the admin `gitdir` back-pointer, and the admin
`commondir` pointer. The proofs must agree with the caller's selected Git
common directory.

An outdated intake also fails closed when a registered worktree is below its
path. Use `git -C <caller-repo> worktree list --porcelain` to identify the
descendant. Preserve clean and dirty descendants. Relocate or remove the
descendant through the verified recovery process, and then retry intake. Do not
delete the parent intake while the descendant is registered.

## Launch intake failure modes

Intake prepares one worktree per launch. The launch identity is the run
identity. A launch holds an exclusive owner lock at
`launches/<launch-identity>/owner.lock` below the state root while it uses its
worktree.

### Duplicate launch identity

A second process with the same launch identity cannot acquire the owner lock.
It fails closed with `repository_intake_launch_in_use` and tells the operator
to wait for the active run.

Wait for the active automation process to finish. Then, run the command again.
Do not delete `launches/<launch-identity>/owner.lock`. The kernel releases the
file lock when the process closes it or exits. If
`repository_intake_launch_in_use` continues, another live process still has
the lock. Stop that process through its normal shutdown path before you
continue.

### Retained path after cleanup or rollback

A preparation rollback failure or a cleanup failure preserves the launch tree
and reports the retained path:

```text
repository-intake launch preparation rollback failed; the launch tree is
preserved at <retained-path>; original error: <error>
```

```text
repository-intake launch cleanup preserved <retained-path>; recover with
docs/runbooks/corrupted-worktree.md: <error>
```

A preserved failure never changes the run exit code. The next launch's
abandoned-intake recovery sweep removes the leftover intake before it prepares
its own worktree. The sweep proves the ownership before any removal:

1. A nonblocking owner-lock acquisition proves that no live launch owns the
   tree.
2. The registration and receipt must match the tree path.
3. The checkout must be clean, detached, and without registered descendants.

### Crash recovery

After a SIGKILL or host failure, no local action is required. The kernel
releases every lock owner. The next launch's sweep performs the cleanup with
the same proof set.

If a sweep proof fails, the sweep preserves the tree and logs
`repository-intake recovery preserved <retained-path>: <error>`. Then inspect
the tree manually before the next run:

```bash
git -C <caller-repo> worktree list --porcelain
python3 -c "import json,sys; print(json.dumps(json.load(open(sys.argv[1])), indent=2))" \
  "<state-root>/launches/<launch-identity>/receipt.json"
```

If the checkout is dirty, preserve the path and inspect it before a later
run. If the path is foreign, symlinked, or ambiguously registered, stop and
recover it manually. Follow the preservation rules above. Do not remove a
receipt or worktree to bypass a failure.

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

If the recovered worktree (or its removal) touched `pyproject.toml`, the uv
environment may have re-solved and dropped the editable install, leaving
`hephaestus-*` console scripts dangling. Restore it:

```bash
uv sync
```

## See also

- [Automation loop crashed mid-issue](automation-loop-crash.md)
- `hephaestus:worktree-cleanup` skill — audit + prune git worktrees
  (never deletes branches).
- `hephaestus:tidy` skill — rebase all local branches.
