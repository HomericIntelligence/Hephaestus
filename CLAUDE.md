# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

ProjectHephaestus is the shared utilities and tooling repository of the HomericIntelligence ecosystem. Named after Hephaestus, the Greek god of craftsmanship, forging, and ingenious invention, this project provides the foundational scripts, helpers, and infrastructure that support development across all other repositories.

**Purpose**: Centralize and maintain Python utilities, helper functions, and common abstractions used throughout the HomericIntelligence suite.

**Role in Ecosystem**:

- ProjectOdyssey → Training and capability development
- ProjectKeystone → Automated task DAG execution
- ProjectScylla → Testing, measurement, and optimization
- ProjectMnemosyne → Knowledge, skills, and memory preservation
- ProjectHermes → Agent communication and message routing
- ProjectArgus → Observability, monitoring, and alerting
- ProjectProteus → Dynamic configuration and environment adaptation
- ProjectMyrmidons → Agent swarm coordination and task distribution
- ProjectAchaeanFleet → Multi-agent fleet orchestration
- **ProjectHephaestus → Shared utilities, tooling, and foundational components**

## Repository Structure

```text
ProjectHephaestus/
├── hephaestus/                 # Python source code (20 documented subpackages)
│   ├── agents/                 # Agent frontmatter + loader + runtime
│   ├── automation/             # Issue planning / implementation / PR review pipeline
│   ├── benchmarks/             # Benchmark comparison utilities
│   ├── ci/                     # CI helpers (precommit, workflows, docker timing)
│   ├── cli/                    # Command-line interface tools
│   ├── config/                 # Configuration management
│   ├── datasets/               # Dataset downloading utilities
│   ├── discovery/              # Discovery of agents, skills, and code blocks
│   ├── forensics/              # Coredump capture + gdb post-mortem runner
│   ├── github/                 # GitHub automation (PR merging, fleet sync, tidy, stats)
│   ├── io/                     # Input/output utilities
│   ├── logging/                # Logging utilities
│   ├── markdown/               # Markdown linting and link fixing
│   ├── nats/                   # NATS JetStream subscriber (event-driven workflows)
│   ├── resilience/             # Circuit breaker + retry + subprocess resilience
│   ├── system/                 # System information collection
│   ├── utils/                  # General utility functions (slugify, retry, subprocess)
│   ├── validation/             # README, schema, and structural validation
│   └── version/                # Version management
├── scripts/                    # Automation and maintenance scripts
├── skills/                     # Claude Code skill definitions (23 SKILL.md skills; kebab-case naming for plugin format)
├── tests/                      # Unit and integration tests
│   ├── unit/                   # Unit tests (mirror hephaestus/ subpackages; a small sanctioned set of extra dirs covers non-package targets — scripts/, docs/, shell, top-level modules)
│   └── integration/            # Integration tests
├── docs/                       # Documentation
└── .claude/                    # Claude Code configurations
```

Skill directories use kebab-case (`code-review`, `git-worktrees`) per the
Claude Code plugin format. All Python packages use lowercase_snake_case.

## Library vs product layer

`hephaestus/automation/` is a **product layer** (26.1k LoC, 53.9% of the
codebase) co-located with the utility library. It is gated behind the
`HomericIntelligence-Hephaestus[automation]` optional extra. The base
`import hephaestus` surface MUST NOT pull `curses`, `fcntl`, `pydantic`,
or any `hephaestus.automation.*` module. Enforced by
`tests/unit/test_import_surface.py` (subprocess) and
`tests/unit/test_automation_boundary.py` (static grep).

Library subpackages of `hephaestus` may not import from
`hephaestus.automation`. The dependency arrow points only one way:
automation → library. See `docs/adr/0001-automation-library-boundary.md`.

## Python Development Guidelines

### Language Preference

**Python 3.10+** is the implementation language for all ProjectHephaestus code:

- Shared utility scripts and helpers
- Configuration management tools
- Logging and monitoring utilities
- Cross-project abstraction layers
- Automation and maintenance scripts

### Key Principles

1. **Modularity**: Develop independent modules with well-defined interfaces
2. **Reusability**: Design components for use across multiple projects
3. **Consistency**: Follow established patterns and conventions
4. **Reliability**: Write robust, well-tested code with clear error handling
5. **Documentation**: Provide comprehensive docstrings and inline comments

### Python Standards

```python
#!/usr/bin/env python3

"""
Module description with purpose, usage, and examples.

Usage:
    python scripts/script_name.py [options]
"""

# Standard library imports first
import sys
import os
from typing import List, Dict, Optional

# Third-party imports next
# import requests
# import numpy as np

# Local imports last
# from hephaestus.utils.helpers import helper_function

def function_name(param: str, optional_param: Optional[int] = None) -> bool:
    """Clear docstring with purpose, parameters, and return value.

    Args:
        param: Description of parameter
        optional_param: Description of optional parameter

    Returns:
        Description of return value

    Raises:
        SpecificException: When something goes wrong
    """
    pass
```

### Requirements

- Python 3.10+
- Type hints required for all functions
- Clear docstrings for public functions and classes
- Comprehensive error handling
- Comprehensive test coverage (unit tests) — 83%+ test coverage enforced; target 90%
- Follow PEP 8 style guidelines

## Key Development Principles

1. **KISS** - *Keep It Simple, Stupid* → Don't add complexity when a simpler solution works
2. **YAGNI** - *You Ain't Gonna Need It* → Don't add things until they are required
3. **DRY** - *Don't Repeat Yourself* → Don't duplicate functionality, data structures, or algorithms
4. **SOLID** Principles:
   - Single Responsibility: Each module/class should have one reason to change
   - Open/Closed: Open for extension, closed for modification
   - Liskov Substitution: Subtypes must be substitutable for their base types
   - Interface Segregation: Clients should not be forced to depend on interfaces they don't use
   - Dependency Inversion: Depend on abstractions, not concretions
5. **Modularity** - Develop independent modules through well-defined interfaces
6. **POLA** - *Principle of Least Astonishment* - Create intuitive and predictable interfaces

## Security Configuration Guidelines

### Secrets Management

- **Never hardcode secrets** in source code
- Use environment variables for sensitive configuration
- Reference secret management systems when appropriate
- Document secret requirements in README, not code

### Input Validation

All utility functions accepting external input must:

1. Validate input types and ranges
2. Sanitize potentially malicious content
3. Handle encoding/decoding safely
4. Log suspicious inputs appropriately

### Secure Coding Practices

- Always use parameterized queries for database interactions
- Implement proper error handling without exposing sensitive information
- Follow principle of least privilege for file system access
- Validate and sanitize all external inputs

## Documentation Rules

### Code Documentation

- **Inline Comments**: Explain *why*, not *what*
- **Function Docstrings**: Follow Google Python Style Guide
- **Class Docstrings**: Describe purpose, attributes, and usage
- **Module Docstrings**: Explain module purpose and key components

### Technical Documentation

- Maintain README.md with setup and usage instructions
- Document API endpoints in OpenAPI format when applicable
- Reference external documentation rather than duplicating

**No CHANGELOG.md.** Do not create, edit, or file issues against `CHANGELOG.md`. Release notes are generated from commits at release time via `gh release create --generate-notes`. Audit reports MUST NOT flag missing/stale changelog entries.

## Claude Code Optimization

### When to Use Extended Thinking

Use Extended Thinking for:

- Designing new utility abstractions
- Analyzing complex cross-cutting concerns
- Planning refactoring of shared components
- Understanding dependency relationships
- Evaluating tradeoffs in utility design

Skip Extended Thinking for:

- Simple utility function implementation
- Straightforward bug fixes
- Boilerplate code generation
- Well-defined refactorings

### Automatic Skill Selection

Before beginning any substantive task, invoke `/hephaestus:skill-advisor` to determine if a structured
skill applies. Use `Skill(skill: "hephaestus:skill-advisor", args: "<task description>")`.

If you are a myrmidon-swarm subagent with a specific task prompt, skip this and follow your prompt directly.

### Skill Catalog

| Skill | When to Use |
|-------|-------------|
| `skill-advisor` | Before any task — routes to the correct skill |
| `advise` | Before starting work — search ProjectMnemosyne for prior learnings |
| `learn` | After completing work — capture session learnings in ProjectMnemosyne |
| `myrmidon-swarm` | Complex multi-step tasks requiring parallel agent coordination |
| `brainstorm` | Before implementing a new feature — design before code |
| `test-driven-development` | Before writing implementation code — RED-GREEN-REFACTOR |
| `systematic-debugging` | Before proposing fixes — root cause first |
| `verification` | Before claiming work is done — evidence before assertions |
| `git-worktrees` | When needing isolated branch workspace |
| `finish-branch` | When implementation is complete — branch completion workflow |
| `code-review` | After major feature completion — Sonnet reviewer + feedback reception |
| `repo-analyze` | Comprehensive 15-dimension repository audit |
| `repo-analyze-quick` | Quick repository health check |
| `repo-analyze-strict` | Ruthlessly thorough repository audit |
| `repo-analyze-full` | Full-coverage audit — one swarm agent per section, no sampling cap |
| `repo-analyze-quick-full` | Quick health check with full file coverage |
| `repo-analyze-strict-full` | Strict audit with full file coverage (swarm per section) |
| `review-pr-strict` | Ruthlessly thorough PR-alignment audit with full coverage |
| `worktree-cleanup` | Audit + prune git worktrees (never deletes branches) |
| `tidy` | Rebase all local branches with swarm conflict resolution |
| `create-reusable-utilities` | Port/generalize utility scripts for cross-project reuse |
| `github-actions-python-cicd` | Set up a Python GitHub Actions CI/CD pipeline |
| `python-repo-modernization` | Bring a Python repo to production-grade quality |

### Agent Skills vs Sub-Agents Decision Tree

```text
Is the task well-defined with predictable steps?
├─ YES → Use an Agent Skill (see catalog above)
│   ├─ Is it a new feature? → brainstorm → test-driven-development
│   ├─ Is it a bug? → systematic-debugging → test-driven-development
│   ├─ Is it ready to ship? → verification → finish-branch
│   ├─ Is it a CI/CD pipeline setup? → github-actions-python-cicd
│   ├─ Is it a repo audit? → repo-analyze (or its quick/strict/full variants)
│   └─ Is it a PR review? → code-review (or review-pr-strict for alignment audits)
│
└─ NO → Use a Sub-Agent
    ├─ Does it require exploration/discovery? → Use sub-agent
    ├─ Does it need adaptive decision-making? → Use sub-agent
    ├─ Is the workflow dynamic/context-dependent? → Use myrmidon-swarm
    └─ Does it need extended thinking? → Use sub-agent
```

### Output Style Guidelines

#### Code References

**DO**: Use repo-relative file paths with line numbers:

```markdown
Updated hephaestus/utils/helpers.py:45-52
```

#### GitHub Issue Integration

**DO**: Post implementation notes as GitHub issue comments:

```bash
gh issue comment <number> --body "Completed implementation of new logging utility"
```

## Working with GitHub

### Git Workflow

**IMPORTANT**: The `main` branch is protected. All changes must go through a pull request.

**PR policy (enforced by required CI gate `pr-policy` and by the PR reviewer):**

1. The PR body MUST contain the literal line `Closes #<issue-number>` (capital
   `C`, no colon, on its own line). `Fixes`, `Resolves`, `closes`, and
   `Closes:` are NOT accepted.
2. Auto-merge MUST stay disabled until implementation review applies
   `state:implementation-go`; then enable it with `gh pr merge --auto --squash`
   (squash-only; rebase is disabled).
3. Every commit MUST be cryptographically signed (`git commit -S`).

CI blocks PRs that fail any of these checks. No exceptions, including
Dependabot and dependency-bump PRs.

```bash
# 1. Create feature branch
git checkout -b <issue-number>-description

# 2. Make changes and commit (signed)
git add <files>
git commit -S -m "type(scope): description"
git log --show-signature -1   # verify the signature took

# 3. Push feature branch
git push -u origin <branch-name>

# 4. Create pull request
gh pr create \
  --title "[Type] Brief description" \
  --body "$(printf 'Summary of change.\n\nCloses #<issue-number>\n')"

# 5. After implementation review marks the PR `state:implementation-go`,
#    enable auto-merge (mandatory; squash-only — rebase is disabled)
gh pr merge --auto --squash
```

### Commit Message Format

Follow conventional commits:

```text
feat(utils): Add new configuration helper
fix(logging): Correct timestamp formatting
docs(readme): Update installation instructions
refactor(io): Simplify file handling logic
```

### Testing Strategy

All utility functions must include comprehensive test coverage:

1. **Unit Tests**: Test individual functions and classes
2. **Integration Tests**: Test component interactions
3. **Edge Cases**: Test boundary conditions and error scenarios
4. **Cross-platform**: Ensure compatibility across supported environments

```bash
# Run all unit tests
pixi run pytest tests/unit -v

# Run specific test file
pixi run pytest tests/unit/utils/test_general_utils.py -v

# Run with coverage
pixi run pytest tests/unit --cov=hephaestus --cov-report=html
```

## Environment Setup

This project uses [Pixi](https://pixi.sh) for environment management:

```bash
# Install dependencies and create environment
pixi install

# Run pre-commit hooks (formatting, linting, etc.)
pre-commit install
```

## Common Commands

### Development Workflows

```bash
# Run tests
pixi run pytest tests/unit

# Run linter
pixi run ruff check hephaestus/ tests/

# Run formatter
pixi run ruff format hephaestus/ tests/

# Run type checking
pixi run mypy
```

### Pre-commit Hooks

Pre-commit hooks automatically check code quality:

```bash
# Install pre-commit hooks (one-time setup)
pre-commit install

# Run hooks manually on all files
pre-commit run --all-files

# NEVER skip hooks with --no-verify
```

## Troubleshooting

### Common Issues

1. **Import Errors**: Check that `pixi install` has been run
2. **Dependency Conflicts**: Update `pixi.toml` and run `pixi install`
3. **Test Failures**: Run tests with verbose output for details
4. **Formatting Issues**: Run `pixi run ruff format hephaestus/ tests/`

### Getting Help

1. Check existing GitHub issues and discussions
2. Review documentation in docs/ directory
3. Post implementation questions as issue comments

## Key Files and Directories

- `hephaestus/utils/` - Core utility functions (slugify, retry, subprocess helpers)
- `hephaestus/config/` - Configuration loading (YAML, JSON, env vars)
- `hephaestus/io/` - File I/O (read, write, safe_write, load/save data)
- `hephaestus/logging/` - Enhanced logging (ContextLogger, setup_logging)
- `hephaestus/cli/` - CLI utilities (argument parsing, output formatting)
- `hephaestus/system/` - System information collection
- `hephaestus/github/` - GitHub automation (PR merging)
- `tests/unit/` - Unit test suite (mirrors hephaestus/ subpackages; sanctioned extra dirs in SANCTIONED_EXTRA_TEST_DIRS cover non-package targets like scripts/, docs/, shell installers, top-level modules)
- `tests/integration/` - Integration tests (package importability, smoke tests)
- `scripts/` - Automation and maintenance tools
- `docs/` - Documentation and guides
- `pyproject.toml` - Project metadata, dependencies, and tool configuration
- `pixi.toml` - Pixi environment and task definitions
- `.claude/` - Claude Code configuration and guidance

## Version Management

This project uses **hatch-vcs dynamic versioning** — the package version is derived
from git tags, not stored in any file.

- **Single source of truth**: the latest `vX.Y.Z` git tag. `pyproject.toml` declares
  `dynamic = ["version"]` with `[tool.hatch.version]` `source = "vcs"`; there is **no**
  static `[project].version` field.
- **`hephaestus/_version.py`** is generated at build time by the hatch-vcs build hook
  (`[tool.hatch.build.hooks.vcs]`, `version-file = "hephaestus/_version.py"`) and is not
  committed. At runtime, `hephaestus/__init__.py` reads `__version__` from installed
  package metadata via `importlib.metadata`.
- **`pixi.toml`** intentionally has no version field — do not add one.
- The `check-version-single-source` pre-commit hook enforces this invariant: it fails if
  a static `[project].version` is reintroduced, if `dynamic = ["version"]` or
  `[tool.hatch.version]` `source = "vcs"` is missing, or if `pixi.toml [workspace]` gains
  a `version` field.
- To cut a release you do **not** edit any version field — a signed `vX.Y.Z` git tag drives
  it. See `docs/RELEASING.md` and `CONTRIBUTING.md` for the workflow.

Make sure all temporary files are in the build/ directory
