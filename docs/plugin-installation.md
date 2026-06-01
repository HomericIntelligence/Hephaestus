# ProjectHephaestus Claude Code Plugin — Installation Guide

ProjectHephaestus ships as a Claude Code plugin in addition to a Python package. Installing the plugin gives any repository in your ecosystem access to the `hephaestus` skill set.

> **Note on versions:** The plugin version (declared in
> [`.claude-plugin/plugin.json`](../.claude-plugin/plugin.json)) and the Python package
> version (tag-driven via hatch-vcs; see
> [latest release](https://github.com/HomericIntelligence/ProjectHephaestus/releases/latest))
> are **separate artifacts** with independent version numbers — they are not coupled and
> will not match. See
> [`COMPATIBILITY.md`](../COMPATIBILITY.md#versioning-python-package-vs-claude-code-plugin)
> for details.

## What the Plugin Provides

The plugin ships **23 skills**. The table is kept in sync with the
`skills/` directory by the `hephaestus-check-skill-catalog` pre-commit hook —
adding or removing a skill without updating this table will fail CI.

| Skill | Description |
|-------|-------------|
| advise | Search team knowledge before starting work. Use when starting experiments, debugging unfamiliar errors, or before implementing features with unknowns. |
| brainstorm | Use before any creative work — creating features, building components, adding functionality, or modifying behavior. Explores user intent and requirements before implementation. |
| code-review | Use when completing tasks, implementing major features, or before merging — dispatches a Sonnet code reviewer and guides reception of feedback with technical rigor |
| create-reusable-utilities | Port and generalize utility scripts from one project into ProjectHephaestus for cross-project reuse |
| finish-branch | Use when implementation is complete and all tests pass — guides branch completion by presenting structured options for merge, PR creation, or cleanup |
| git-worktrees | Use when starting feature work that needs isolation from current workspace — creates isolated git worktrees with safety verification |
| github-actions-python-cicd | Set up a comprehensive GitHub Actions CI/CD pipeline for a Python project with multi-version testing |
| learn | Save session learnings as a skill plugin — amends existing skills when the topic matches, creates new ones otherwise. Use after experiments, debugging sessions, or when you want to preserve team knowledge. |
| myrmidon-swarm | Summon the Myrmidon swarm — hierarchical agent delegation with Opus/Sonnet/Haiku model tiers for the HomericIntelligence ecosystem |
| python-repo-modernization | Bring a partially modernized Python repo to production-grade quality: fix bugs, restructure tests, enhance CI/pre-commit, prepare for PyPI publishing |
| repo-analyze | Performs comprehensive repository completeness and quality audit with grading across 15 dimensions |
| repo-analyze-full | Full-coverage repository audit — dispatches one Myrmidon swarm agent per audit section so EVERY file is analyzed with no sampling cap. Use when `repo-analyze` misses bugs by only reading sampled files. |
| repo-analyze-quick | Quick repository health check — catches showstoppers only, defaults to B, focuses on broken/dangerous/missing critical items |
| repo-analyze-quick-full | Quick repository health check with full file coverage — catches showstoppers in every file via per-section swarm agents. Same B-default philosophy as `repo-analyze-quick`, but no sampling cap. |
| repo-analyze-strict | Ruthlessly thorough repository audit with strict grading — starts at F, requires concrete evidence for every grade improvement |
| repo-analyze-strict-full | Ruthlessly thorough repository audit with strict grading AND full file coverage — dispatches one Myrmidon swarm agent per section so every file is read, then grades from F up with concrete evidence required. |
| review-pr-strict | Ruthlessly thorough PR alignment audit with strict grading AND full coverage — dispatches one Myrmidon swarm agent per audit dimension so every changed file, every linked issue, and every cited architecture document is examined, then grades from F up with concrete evidence required. |
| skill-advisor | Use when starting any task to determine which Hephaestus skill applies — routes tasks to the correct procedural skill before you begin |
| systematic-debugging | Use when encountering any bug, test failure, or unexpected behavior, before proposing fixes — requires root cause investigation before solutions |
| test-driven-development | Use when implementing any feature or bugfix, before writing implementation code — enforces RED-GREEN-REFACTOR cycle |
| tidy | Tidy local branches in the CURRENT repo. Runs `gh tidy --rebase-all --trunk <default>` interactively, then dispatches a Myrmidon swarm to finish any rebases that gh-tidy could not complete. The swarm NEVER deletes branches — only gh-tidy can, via its own y/N prompts. |
| verification | Use before claiming work is complete, fixed, or passing — requires running verification commands and confirming output before any success claims; evidence before assertions always |
| worktree-cleanup | Audit every git worktree, ensure all state is committed, then prune worktrees cleanly. NEVER deletes branches — that's `gh tidy`'s job. Use when `git worktree list` shows many entries after a parallel session, when you suspect uncommitted work in worktrees, or when you want to clean up before running `gh tidy`. |

## Installation

### From GitHub (recommended)

```bash
claude plugin install HomericIntelligence/ProjectHephaestus
```

### From a local clone

```bash
claude plugin install /path/to/ProjectHephaestus
```

## Enabling in a Project

After installing, enable the plugin in your project's `.claude/settings.json`:

```json
{
  "enabledPlugins": {
    "hephaestus@ProjectHephaestus": true
  }
}
```

## Verifying Installation

Check that the plugin appears in your project's enabled plugins:

```bash
cat .claude/settings.json
```

You should see `hephaestus@ProjectHephaestus` listed under `enabledPlugins`. Skills will then be available as both `/repo-analyze` and the fully-qualified `hephaestus:repo-analyze` form.

## Usage Examples

```
/advise implement retry logic with exponential backoff
/repo-analyze
/repo-analyze-strict
/repo-analyze-quick
/learn
/myrmidon-swarm refactor the authentication module
```

The fully-qualified form is useful when multiple plugins define a skill with the same name:

```
/hephaestus:repo-analyze
/hephaestus:advise implement a new config loader
```
