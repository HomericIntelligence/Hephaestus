---
name: worktree-cleanup
description: "Audit every git worktree, ensure all state is committed, then prune worktrees cleanly. NEVER deletes branches — that's `gh tidy`'s job. Use when: (1) `git worktree list` shows many entries after a parallel session, (2) you suspect uncommitted work in worktrees, (3) you want to clean up before running `gh tidy`."
argument-hint: "<optional: --dry-run>"
allowed-tools: [Bash, Read]
---

# /hephaestus:worktree-cleanup

Audit every git worktree for uncommitted state, classify each one, and prune those that are safe to remove.

> **Usage:** Run this from any directory in the repository. This skill audits ALL worktrees registered to the current repo.
>
> **Philosophy:** State preservation first. Every worktree is proven safe to remove before it is touched. This skill NEVER deletes branches — branch deletion is `gh tidy`'s exclusive responsibility. Pair this with `/hephaestus:tidy` for a complete cleanup flow.

## When to Use

- `git worktree list` shows many entries after a parallel agent session
- You suspect uncommitted work is stranded in one or more worktrees
- You want to clean up before running `/hephaestus:tidy`
- A prior `git worktree prune` left stale entries that won't remove cleanly
- You're onboarding onto a repo and want to understand its worktree state before touching anything

## What This Skill Does

### Step 1 — Inventory

Runs `git worktree list --porcelain` to enumerate every worktree registered to this repo.

### Step 2 — Per-worktree audit

For each non-main worktree:

```bash
# Uncommitted changes
git -C <path> status --short

# Commits not yet on remote
git -C <path> log origin/<branch>..HEAD --oneline 2>/dev/null || \
  git -C <path> log HEAD --oneline -5
```

### Step 3 — 3-method classification

Lifted verbatim from ProjectMnemosyne `git-worktree-cleanup-preservation-audit` v1.0.0:

| Method | Command | Positive Signal |
|--------|---------|-----------------|
| PR state | `gh pr list --head <branch> --state all --json number,state` | `MERGED` or `CLOSED` |
| Patch-ID | `git cherry origin/main <branch>` | All `-` prefix |
| Tree diff | `git diff <branch-tip-sha> origin/main --stat` | Main has net more insertions |

> **Squash-merge false positive warning:** `git cherry` always shows `+` for squash-merged branches. If cherry shows `+` on a MERGED PR, proceed to the message-search step:
>
> ```bash
> git log origin/main --oneline | grep -i "<first-4-words-of-commit-msg>"
> ```

### Step 4 — Classify each worktree

| State | Meaning | Skill Action |
|-------|---------|-------------|
| `CLEAN_PRUNE_OK` | No uncommitted changes; content provably on main | Run `git worktree remove <path>` + `git worktree prune` |
| `NEEDS_COMMIT` | Uncommitted non-artifact changes present | Auto-commit files clearly related to the branch's issue; ask user about ambiguous files; ignore artifacts |
| `NEEDS_PUSH` | Commits present but not on remote | Print `git push -u origin <branch>` for user to run |
| `NEEDS_PR` | Commits on remote but no open PR | Print `gh pr create` template for user |
| `KEEP` | Open PR exists or classification is ambiguous | Report and skip — do not touch |

### Step 5 — Safe removal only

For `CLEAN_PRUNE_OK` worktrees:

```bash
git worktree remove <path>   # no --force
git worktree prune
```

If `git worktree remove` fails because the worktree is **locked** (dead agent PID), check whether it is clean first:

```bash
git -C <path> status --short   # empty = clean
```

- **Locked + clean** → unlock and remove directly (no `--force` needed):

  ```bash
  git worktree unlock <path>
  git worktree remove <path>   # succeeds without --force on a clean worktree
  ```

- **Locked + dirty** → classify dirty files, auto-commit real work, ask user about ambiguous files, then unlock and remove:

  **Step 5a — Classify dirty files**

  ```bash
  git -C <path> status --short
  ```

  Artifact patterns — **always ignore, never commit**:

  ```
  __pycache__/  *.pyc  *.pyo  *.pyd
  .pytest_cache/  .mypy_cache/  .ruff_cache/
  build/  dist/  *.egg-info/  htmlcov/
  .coverage  .coverage.*
  .claude-prompt-*.md  .issue_implementer
  ```

  **Step 5b — Commit real work**

  For each non-artifact modified or untracked file, inspect the diff and infer which issue/branch it belongs to from the branch name and file content:

  ```bash
  git -C <path> diff HEAD -- <file>   # modified files
  git -C <path> diff -- <file>        # untracked: show content
  ```

  Auto-commit files that clearly relate to the branch's issue (same module, same feature area). Use `git add <specific-files>` — never `-A`:

  ```bash
  git -C <path> add <file1> <file2> ...
  git -C <path> commit -m "chore(worktree-cleanup): salvage uncommitted work on <branch>"
  git push -u origin <branch>
  gh pr create --head <branch> --base main \
    --title "chore(<scope>): salvage uncommitted work from worktree cleanup" \
    --body "Uncommitted changes recovered during worktree cleanup. Please review."
  ```

  For files that are ambiguous (unrelated module, unclear purpose), list them and ask the user:

  ```
  Worktree <path> (<branch>) has files I'm unsure about:
    - <file>: <one-line description of what it contains>

  Should I: (a) commit them to this branch, (b) skip them (leave on disk), or (c) discard them?
  ```

  Wait for user response before proceeding.

  **Step 5c — Unlock and remove**

  After all real work is committed (or the user has decided on ambiguous files):

  ```bash
  git worktree unlock <path>
  git worktree remove <path>
  ```

> **Why not `--force` on locked worktrees?** The Safety Net blocks `--force` and it is never needed once real work is committed. Unlock + clean remove is always the correct path.

### Step 6 — Print summary

List all worktrees with their classification and the actions taken or recommended.

## What This Skill Does NOT Do

- **Never `git branch -D` or `git branch -d`** — branch deletion is `gh tidy`'s exclusive responsibility.
- **Never `git push origin --delete <branch>`** — same reason.
- **Never `git worktree remove --force`** — locked + clean worktrees are unlocked then removed without `--force`; locked + dirty worktrees have real work committed first, then unlock + remove.
- **Never `git add -A` or `git add .`** — always adds specific files by name to avoid committing artifacts or secrets.
- **Never commits artifact files** (`__pycache__`, `*.pyc`, `.pytest_cache`, `.coverage`, `.mypy_cache`, etc.).
- **Never `git stash drop`** — stashes are listed and described but never dropped.
- **Never modifies the user's current branch or working directory**.

## Usage

```bash
# Standard: audit all worktrees, commit real work, ask about ambiguous files, prune safe ones
/hephaestus:worktree-cleanup

# Dry run: report only, no commits or removals at all
/hephaestus:worktree-cleanup --dry-run
```

## Recommended Workflow

Chain this with `/hephaestus:tidy` for a complete cleanup flow:

```
Step 1: /hephaestus:worktree-cleanup
        → Ensures all state is committed or documented
        → Prunes CLEAN_PRUNE_OK worktrees
        → Leaves branches intact for gh-tidy to decide

Step 2: /hephaestus:tidy
        → gh-tidy rebases all branches onto trunk
        → You answer its y/N prompts to delete stale branches
        → Swarm fixes any rebases that gh-tidy couldn't complete
```

## Safety Net Notes

The Safety Net blocks several operations that must be handed to the user manually:

| Blocked operation | Why blocked | Manual command |
|-------------------|-------------|----------------|
| `git stash drop` | Destructive, irreversible | `git stash drop 'stash@{N}'` |
| `git worktree remove --force` | Could discard uncommitted work | `git worktree unlock <path> && git worktree remove <path>` (clean) or resolve dirty state first |
| `git branch -D` | Permanent | `git branch -D <branch>` (only after audit proves safety) |
| `rm -rf .worktrees/` | Bulk deletion | Use the unlock loop above instead |

When this skill encounters any of these operations as necessary, it prints the exact command for the user to run rather than executing it.
