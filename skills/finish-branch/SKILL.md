---
name: finish-branch
description: Use when implementation is complete and all tests pass — guides branch completion by presenting structured options for merge, PR creation, or cleanup
argument-hint: <optional: base branch name>
allowed-tools: [Bash, Read]
---

# Finishing a Development Branch

## Overview

Guide completion of development work by verifying work is done, then presenting clear options.

**Core principle:** Verify tests → Run verification → Present options → Execute choice → Clean up.

**Prerequisite:** Run `/hephaestus:verification` BEFORE invoking this skill. If verification fails, fix the issues first.

## The Process

### Step 1: Full Verification

Run the complete Hephaestus verification suite:

```bash
pixi run pytest tests/unit -v
pixi run mypy hephaestus/
pixi run ruff check hephaestus/ tests/
pixi run ruff format --check hephaestus/ tests/
pre-commit run --files $(git diff --name-only origin/main)
```

**If ANY check fails:**

```
Verification failing — must fix before completing:

[Show failures]

Cannot proceed with merge/PR until all checks pass.
```

Stop. Don't proceed to Step 2.

**If all checks pass:** Continue.

### Step 2: Determine Base Branch

The `main` branch is protected in all HomericIntelligence repos. All merges go through PRs.

```bash
git log --oneline origin/main..HEAD
```

Verify the commit list looks correct before proceeding.

### Step 3: Present Options

Present exactly these options:

```
All checks pass. What would you like to do?

1. Push and create a Pull Request (recommended — main is protected)
2. Keep the branch as-is (I'll handle it later)
3. Discard this work

Which option?
```

**Note:** Direct merge to main is not an option — the `main` branch is protected.

### Step 4: Execute Choice

#### Option 1: Push and Create PR

```bash
# Push branch
git push -u origin <feature-branch>

# Create PR using conventional format
gh pr create \
  --title "feat(scope): description" \
  --body "$(cat <<'EOF'
## Summary
- <bullet: what changed>
- <bullet: why>

## Test Plan
- [ ] All unit tests pass
- [ ] Type check passes
- [ ] Linter clean
- [ ] Pre-commit hooks pass

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"

# Enable auto-merge
gh pr merge --auto --rebase
```

Then: Cleanup worktree (Step 5)

#### Option 2: Keep As-Is

Report: "Keeping branch `<name>`. Worktree preserved at `<path>`."

**Don't cleanup worktree.**

#### Option 3: Discard

**Confirm first:**

```
This will permanently delete:
- Branch <name>
- All commits: <commit-list>
- Worktree at <path> (if applicable)

Type 'discard' to confirm.
```

Wait for exact typed confirmation.

```bash
git checkout main
git branch -D <feature-branch>
```

Then: Cleanup worktree (Step 5)

### Step 5: Cleanup Worktree (Options 1 and 3 only)

```bash
# Check if in a worktree
git worktree list

# If yes, remove it
git worktree remove <worktree-path>
git worktree prune
```

## Commit Message Format

All commits must follow conventional commits:

```
feat(scope): add new capability
fix(scope): resolve specific issue
docs(scope): update documentation
refactor(scope): restructure without behavior change
test(scope): add/fix tests
chore(scope): maintenance task
```

Include `Co-Authored-By` trailer when AI-assisted:

```
Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
```

## Red Flags

**Never:**

- Proceed with failing checks
- Merge directly to main (protected branch)
- Force-push without explicit request
- Delete work without typed confirmation

**Always:**

- Run all 5 verification commands
- Present structured options
- Get typed confirmation for Option 3
- Clean up worktrees for Options 1 and 3

---

_Adapted from [obra/superpowers](https://github.com/obra/superpowers) under the [MIT License](https://github.com/obra/superpowers/blob/main/LICENSE). Copyright (c) 2025 Jesse Vincent._
