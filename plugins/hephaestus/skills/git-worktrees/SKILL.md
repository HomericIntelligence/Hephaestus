---
name: git-worktrees
description: Use when starting feature work that needs isolation from current workspace — creates isolated git worktrees with safety verification
argument-hint: <branch-name or feature description>
allowed-tools: [Bash, Read]
---

# Using Git Worktrees

## Overview

Git worktrees create isolated workspaces sharing the same repository, allowing work on multiple branches simultaneously without switching.

**Core principle:** Systematic directory selection + safety verification = reliable isolation.

**When NOT to use this skill manually:** The `myrmidon-swarm` skill automatically provides worktree isolation for each agent via `isolation: "worktree"` on the Agent tool. Use this skill for your own manual development work, not when orchestrating a swarm.

## Directory Selection

Follow this priority order:

### 1. Check Existing Directories

```bash
ls -d .worktrees 2>/dev/null     # Preferred (hidden, project-local)
ls -d worktrees 2>/dev/null      # Alternative
```

If found: Use that directory. If both exist, `.worktrees` wins.

### 2. Check CLAUDE.md

```bash
grep -i "worktree" CLAUDE.md 2>/dev/null
```

If preference specified: Use it without asking.

### 3. Default for HomericIntelligence Repos

For all HomericIntelligence repos, worktrees go in `/tmp/<project>-<branch>` unless the repo has an existing `.worktrees/` directory. This avoids polluting the project directory.

```bash
project=$(basename "$(git rev-parse --show-toplevel)")
```

## Safety Verification

### For Project-Local Directories (.worktrees or worktrees)

**MUST verify directory is ignored before creating worktree:**

```bash
git check-ignore -q .worktrees 2>/dev/null || echo "NOT IGNORED - add to .gitignore first"
```

**If NOT ignored:**

1. Add `.worktrees/` to `.gitignore`
2. Commit the change
3. Then proceed with worktree creation

**Why critical:** Prevents accidentally committing worktree contents to repository.

### For /tmp Locations

No `.gitignore` verification needed — outside the project entirely.

## Creation Steps

```bash
# 1. Determine project name
project=$(basename "$(git rev-parse --show-toplevel)")

# 2. Create worktree with new branch
# Option A: /tmp location (default for HomericIntelligence)
git worktree add "/tmp/${project}-${BRANCH_NAME}" -b "${BRANCH_NAME}"
cd "/tmp/${project}-${BRANCH_NAME}"

# Option B: project-local (if .worktrees/ exists and is ignored)
git worktree add ".worktrees/${BRANCH_NAME}" -b "${BRANCH_NAME}"
cd ".worktrees/${BRANCH_NAME}"

# 3. Install dependencies
pixi install

# 4. Verify clean baseline
pixi run pytest tests/unit -v

# 5. Report status
echo "Worktree ready at $(pwd)"
```

**If tests fail:** Report failures, ask whether to proceed or investigate.

**If tests pass:** Report ready.

## Cleanup

```bash
# When work is done (or use finish-branch skill)
git worktree remove /tmp/${project}-${BRANCH_NAME}

# Prune stale worktree references
git worktree prune
```

**For Options merge/discard:** Clean up immediately.
**For Option keep/PR open:** Preserve the worktree.

## Quick Reference

| Situation | Action |
|-----------|--------|
| `.worktrees/` exists + ignored | Use it |
| Neither exists | Use `/tmp/<project>-<branch>` |
| Directory not ignored | Add to `.gitignore` + commit first |
| Tests fail at baseline | Report failures + ask before proceeding |

## Common Mistakes

- **Skipping ignore verification** for project-local worktrees → contents get tracked
- **Proceeding with failing baseline** → can't distinguish new bugs from pre-existing
- **Not cleaning up** → stale worktrees accumulate

## Integration

**Pairs with:**

- `/hephaestus:finish-branch` — REQUIRED for cleanup after work is complete
- `/hephaestus:verification` — run before finishing and cleaning up

---

_Adapted from [obra/superpowers](https://github.com/obra/superpowers) under the [MIT License](https://github.com/obra/superpowers/blob/main/LICENSE). Copyright (c) 2025 Jesse Vincent._
