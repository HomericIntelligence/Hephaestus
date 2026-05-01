---
name: worktree-cleanup
description: Audit every git worktree, ensure all state is committed, then prune worktrees cleanly. NEVER deletes branches — that's `gh tidy`'s job. Use when: (1) `git worktree list` shows many entries after a parallel session, (2) you suspect uncommitted work in worktrees, (3) you want to clean up before running `gh tidy`.
argument-hint: <optional: --dry-run | --commit-untracked>
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
| `NEEDS_COMMIT` | Uncommitted changes present | Print exact `git add` + `git commit` commands; do NOT auto-commit unless `--commit-untracked` passed |
| `NEEDS_PUSH` | Commits present but not on remote | Print `git push -u origin <branch>` for user to run |
| `NEEDS_PR` | Commits on remote but no open PR | Print `gh pr create` template for user |
| `KEEP` | Open PR exists or classification is ambiguous | Report and skip — do not touch |

### Step 5 — Safe removal only

For `CLEAN_PRUNE_OK` worktrees:

```bash
git worktree remove <path>   # no --force
git worktree prune
```

If `git worktree remove` fails (locked worktree), classify as `KEEP` and print the manual unlock command for the user:

```bash
git worktree unlock <path>
git worktree remove --force <path>
```

### Step 6 — Print summary

List all worktrees with their classification and the actions taken or recommended.

## What This Skill Does NOT Do

- **Never `git branch -D` or `git branch -d`** — branch deletion is `gh tidy`'s exclusive responsibility.
- **Never `git push origin --delete <branch>`** — same reason.
- **Never `git worktree remove --force`** — locked worktrees are reported, not force-removed; the unlock + force-remove command is printed for the user to run manually.
- **Never `git stash drop`** — stashes are listed and described but never dropped.
- **Never modifies the user's current branch or working directory**.
- **Never auto-commits without explicit `--commit-untracked` flag** — default behavior is print-only.

## Usage

```bash
# Standard: audit all worktrees, print recommendations, prune safe ones
/hephaestus:worktree-cleanup

# Dry run: report only, no removals at all
/hephaestus:worktree-cleanup --dry-run

# Auto-commit dirty worktrees as wip snapshots (requires user confirmation per worktree)
/hephaestus:worktree-cleanup --commit-untracked
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
| `git worktree remove --force` | Could discard uncommitted work | `git worktree unlock <path> && git worktree remove --force <path>` |
| `git branch -D` | Permanent | `git branch -D <branch>` (only after audit proves safety) |
| `rm -rf .worktrees/` | Bulk deletion | Use the unlock loop above instead |

When this skill encounters any of these operations as necessary, it prints the exact command for the user to run rather than executing it.
