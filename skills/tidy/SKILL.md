---
name: tidy
description: Tidy local branches in the CURRENT repo. Runs `gh tidy --rebase-all --trunk <default>` interactively, then dispatches a Myrmidon swarm to finish any rebases that gh-tidy could not complete. The swarm NEVER deletes branches — only gh-tidy can, via its own y/N prompts.
argument-hint: <optional: --dry-run | --no-swarm | --trunk BRANCH | --max-concurrent N>
allowed-tools: [Bash, Read]
---

# /hephaestus:tidy

Tidy local branches and fix failed rebases with a Myrmidon swarm.

## When to Use

- You want to rebase all local branches onto the repo's default trunk in one command.
- `gh tidy` aborted some rebases due to conflicts, and you want Claude to fix them.
- You're starting a new work session and want the local repo in a clean state.

## What This Skill Does

1. Verifies the working tree is clean (asks you to commit/stash first if not).
2. Runs `gh tidy --rebase-all --trunk <default_branch>` **interactively** — stdin
   passes through so you answer gh-tidy's own y/N delete prompts yourself.
   gh-tidy can delete branches if you say yes; that is expected and allowed.
3. Parses gh-tidy's output for `"WARNING: Unable to auto-rebase the following branches"`.
4. Spawns one Sonnet Myrmidon agent per failing branch (max 5 concurrent) to complete
   the rebase using semantic conflict resolution.
5. After each agent succeeds, re-arms `gh pr merge --auto --merge` on the branch's PR
   if one exists.
6. Prints a final summary: rebased / subsumed / still failing.

## What the Swarm Does NOT Do

- **Never deletes any branch** (local or remote). If a branch is already subsumed by
  trunk, the agent reports "subsumed" and stops — it does not delete the branch.
- **Never removes a worktree with `--force`**. Each agent creates a temporary worktree
  (`<repo>/.git/worktrees/tidy-<branch>`) for isolation and removes it cleanly when
  done. If cleanup fails, the worktree is left in place and reported.
- **Never touches pre-existing worktrees**. Only the worktree the agent itself created
  can be removed.

## Usage

```bash
# Standard: run gh-tidy interactively, then fix failed rebases with the swarm
hephaestus-tidy

# Preview what would happen without executing
hephaestus-tidy --dry-run

# Run gh-tidy, print failures, but do NOT spawn swarm (fix manually)
hephaestus-tidy --no-swarm

# Override the trunk branch (default: auto-detected from GitHub)
hephaestus-tidy --trunk main

# Limit swarm concurrency
hephaestus-tidy --max-concurrent 3
```

## Invocation via skill

```bash
# In Claude Code, this skill runs hephaestus-tidy with any arguments you provide:
/hephaestus:tidy
/hephaestus:tidy --dry-run
/hephaestus:tidy --no-swarm
```

## Implementation

The skill runs the CLI entry point `hephaestus-tidy` which lives in
`hephaestus/github/tidy.py`. The swarm uses `claude-code-sdk` to spawn agents.
Each agent prompt includes a verbatim "FORBIDDEN ACTIONS" block preventing
branch or worktree deletion.

## When the Skill Has Run

Read the summary at the end:

- **Rebased**: branches that now point to a new tip rebased onto trunk. Their PRs have
  auto-merge re-armed.
- **Subsumed**: branches whose commits were already on trunk after rebase — they still
  exist; you can delete them when you're ready.
- **Still failing**: branches the swarm could not fix. Fix these manually:

  ```bash
  git fetch origin
  git switch <branch>
  git rebase origin/<trunk>
  # resolve conflicts
  git push --force-with-lease --force-if-includes origin <branch>
  ```
