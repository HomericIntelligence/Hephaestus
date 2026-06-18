# ProjectHephaestus — Codebuff Knowledge

## Project Overview

ProjectHephaestus is the shared utilities and tooling repository of the HomericIntelligence ecosystem.

**Language:** Python 3.10+ | **Package manager:** Pixi + justfile | **Linting:** ruff | **Type checking:** mypy | **Testing:** pytest

## Key Commands

```bash
pixi run pytest tests/unit -v    # Run unit tests
pixi run mypy hephaestus/        # Type check
pixi run ruff check hephaestus/ tests/  # Lint
pixi run ruff format --check hephaestus/ tests/  # Format check
pixi install                     # Install dependencies
just bootstrap                   # Full bootstrap
```

## Hephaestus Skills (23 Codebuff Agents)

Each agent in `.agents/hephaestus-*.ts` reads its `skills/<name>/SKILL.md` at runtime.

| Skill | Agent ID | When to Use |
|-------|----------|-------------|
| skill-advisor | `hephaestus:skill-advisor` | Before any task — routes to the correct skill |
| advise | `hephaestus:advise` | Before starting work — search ProjectMnemosyne |
| learn | `hephaestus:learn` | After completing work — capture learnings |
| myrmidon-swarm | `hephaestus:myrmidon-swarm` | Complex multi-step tasks requiring parallel agents |
| brainstorm | `hephaestus:brainstorm` | Before implementing a new feature |
| test-driven-development | `hephaestus:test-driven-development` | Before writing implementation code |
| systematic-debugging | `hephaestus:systematic-debugging` | Before proposing fixes |
| verification | `hephaestus:verification` | Before claiming work is done |
| git-worktrees | `hephaestus:git-worktrees` | When needing isolated branch workspace |
| finish-branch | `hephaestus:finish-branch` | When implementation is complete |
| code-review | `hephaestus:code-review` | After major feature completion |
| repo-analyze | `hephaestus:repo-analyze` | Comprehensive 15-dimension audit |
| repo-analyze-quick | `hephaestus:repo-analyze-quick` | Quick health check |
| repo-analyze-strict | `hephaestus:repo-analyze-strict` | Strict grading audit |
| repo-analyze-full | `hephaestus:repo-analyze-full` | Full-coverage audit (swarm per section) |
| repo-analyze-quick-full | `hephaestus:repo-analyze-quick-full` | Quick check with full file coverage |
| repo-analyze-strict-full | `hephaestus:repo-analyze-strict-full` | Strict audit with full file coverage |
| review-pr-strict | `hephaestus:review-pr-strict` | PR alignment audit |
| worktree-cleanup | `hephaestus:worktree-cleanup` | Audit + prune git worktrees |
| tidy | `hephaestus:tidy` | Rebase all local branches |
| create-reusable-utilities | `hephaestus:create-reusable-utilities` | Port/generalize utility scripts |
| github-actions-python-cicd | `hephaestus:github-actions-python-cicd` | Set up Python CI/CD pipeline |
| python-repo-modernization | `hephaestus:python-repo-modernization` | Bring Python repo to production-grade |

## Conventions

- **Commits:** Conventional commits — `type(scope): description`
- **Branches:** `<issue-number>-description`
- **PRs:** Must contain `Closes #<issue-number>`, auto-merge with squash
- **Versioning:** hatch-vcs (git tags, no static version field)
