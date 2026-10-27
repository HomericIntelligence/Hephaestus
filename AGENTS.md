# AGENTS.md

This file is the single authoritative agent contract for Hephaestus. It is
self-contained: it holds the project overview, development rules (commit policy,
branch naming, version model), the skill catalog, and the AI-agent topology map
used by Hephaestus and the wider HomericIntelligence ecosystem. For enabled skill
plugins see [`.claude/settings.json`](.claude/settings.json).

## Goals

Hephaestus exists to get every change reviewed fully before it merges. These
are the outcomes we want. They are goals, not gates: the agents decide how to
reach them.

- **Fully reviewed pull requests.** A pull request merges only after a review
  has run and every review thread is answered and resolved.
- **GitHub is the source of truth.** Read labels, review threads, check runs,
  and merge state from GitHub. Do not keep a second record to validate them: no
  attestation, provenance, hash, nonce, or exact-head proof.
- **Agents handle the working tree.** A dirty worktree, a stale branch, a
  rebase, or a merge conflict is work for the agent, not a reason for the host
  to stop. The agent inspects what it finds, keeps its own changes, and
  continues. Use the `$athena:realign` skill when the plan and the architecture
  disagree.
- **Flexible over rigid.** Prefer a warning and a recovery over a refusal.
  Review comments are free-form text. A reviewer may add a grade and a
  per-comment severity. The host only shows them. Never gate on them, and do
  not add finding identities or format checks that can fail a review.
- **Small and simple.** Handle errors at module boundaries, test the core
  library, and delete code that guards a case no caller can reach.

## Project Overview

Hephaestus is the shared utilities and tooling repository of the
HomericIntelligence ecosystem. Named after Hephaestus, the Greek god of
craftsmanship, forging, and ingenious invention, this project provides the
foundational scripts, helpers, and infrastructure that support development across
all other repositories.

**Purpose**: Centralize and maintain Python utilities, helper functions, and
common abstractions used throughout the HomericIntelligence suite.

Hephaestus supports Linux and macOS, subject to each feature's host requirements.
Windows is unsupported for development and execution. Do not add Windows
compatibility paths or Windows CI jobs. Keep input-validation and isolation
checks that use Windows-style values on supported hosts.

**Role in Ecosystem**:

- Odyssey → Training and capability development
- Keystone → Automated task DAG execution
- Scylla → Testing, measurement, and optimization
- Mnemosyne → Knowledge, skills, and memory preservation
- Hermes → Agent communication and message routing
- Argus → Observability, monitoring, and alerting
- Proteus → Dynamic configuration and environment adaptation
- Myrmidons → Agent swarm coordination and task distribution
- AchaeanFleet → Multi-agent fleet orchestration
- **Hephaestus → Shared utilities, tooling, and foundational components**

## ASD-STE100 writing standard

Hephaestus MUST use ASD-STE100 Simplified Technical English for all English
technical prose that it owns and creates or revises. Hephaestus adopts Issue 9,
dated January 15, 2025. Use this issue until the project explicitly adopts a
later issue.

This rule applies to Hephaestus-owned skill text, agent directions, packaged
prompts, documentation, code comments, docstrings, user-interface text, and
issue or pull-request text. It also applies to new or revised text that an agent
creates when Hephaestus supplies an external skill. Do not change third-party
skill source text or versioned external contract fixtures.

The wording of the project principles is the only prose exception. This
exception applies only to sections or prompt blocks that explicitly declare
project principles, including specialized principle lists. Do not rewrite this
text to meet the writing standard. References to, explanations of, and
applications of a principle MUST follow the standard.

Follow the complete [Hephaestus ASD-STE100 policy](docs/asd-ste100.md). Use the
[official ASD-STE100 website](https://www.asd-ste100.org/) and
[request a free official copy](https://www.asd-ste100.org/STE_downloads.html)
when you need the standard. Do not copy its rules, dictionary, PDF, or logo
into this repository. This project policy does not state or imply that ASD or
STEMG approves, certifies, or endorses Hephaestus.

## Repository Structure

```text
Hephaestus/
├── hephaestus/                 # Python source packages
│   ├── agents/                 # Agent frontmatter + loader + runtime
│   ├── automation/             # Queue-based issue planning / implementation / PR review pipeline
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
│   ├── observability/          # Prometheus metrics, local health endpoint, and alert transitions
│   ├── prompts/                # Packaged Jinja templates and CLI-only override catalog
│   ├── resilience/             # Circuit breaker + retry + subprocess resilience
│   ├── scripts_lib/            # Standalone consistency-check scripts (CLI table, version)
│   ├── system/                 # System information collection
│   ├── utils/                  # General utility functions (slugify, retry, subprocess, git helpers)
│   ├── validation/             # README, schema, and structural validation
│   └── version/                # Version management
├── scripts/                    # Automation and maintenance scripts
├── tests/                      # Unit and integration tests
│   ├── unit/                   # Unit tests (mirror hephaestus/ subpackages; SANCTIONED_EXTRA_TEST_DIRS names the extra directories that have no source counterpart)
│   ├── integration/            # Integration tests
│   ├── performance/            # Load and regression benchmarks
│   ├── fixtures/               # Versioned test fixtures
│   └── shell/                  # Shell installer and justfile tests (bats)
├── docs/                       # Documentation
└── .claude/                    # Claude Code configurations
```

Agent skills are supplied by the Athena plugins enabled in
`.claude/settings.json`; plugin skill names use kebab-case
(`code-review`, `git-worktrees`). All Python packages use
lowercase_snake_case.

## Library vs product layer

`hephaestus/automation/` is an opt-in product layer co-located with the utility
library. It is gated behind the
`HomericIntelligence-Hephaestus[automation]` optional extra. The base
`import hephaestus` surface MUST NOT pull `curses`, `fcntl`, `pydantic`,
or any `hephaestus.automation.*` module. Enforced by
`tests/unit/validation/test_import_surface.py` (subprocess) and
`tests/unit/validation/test_automation_boundary.py` (static grep).

Library subpackages of `hephaestus` may not import from
`hephaestus.automation`. The dependency arrow points only one way:
automation → library. See `docs/adr/0001-automation-library-boundary.md`.

Significant architectural decisions are recorded as ADRs in `docs/adr/`; see
`docs/adr/README.md` for the enumerable index.

When a plan or review proposes a new ADR, refer to it as the "next available
ADR number." Do not reserve or hardcode a new ADR number in a plan, review,
prompt, or reusable direction. Immediately before the implementation creates
the ADR file, read the current `docs/adr/README.md` index and allocate the next
unused number. If concurrent work takes that number, read the index again and
use the new next unused number. References to existing ADRs must use their
exact assigned numbers.

### Coverage omit-list invariant

The Coverage.py gate measures the whole `hephaestus/` package. That includes the
opt-in `hephaestus/automation/` product layer. The product layer ships behind
the `HomericIntelligence-Hephaestus[automation]` extra, and the dependency
arrow points only from the product layer to the library. The extra controls
imports. It does not remove the layer from measurement.

The omit list holds exactly two entries: the test tree and the package
`__init__.py` files. Keep both generic exclusions. Do not add another entry
without a documented boundary and an explicit change to this invariant.
`tests/unit/validation/test_omit_allowlist.py` enforces exact membership, so a
whole `hephaestus/automation/*.py` module must stay measured.

`coverage.toml` holds the aggregate threshold and the per-module floors. The
retained floors are `validation/schema.py`, `automation/models.py`,
`automation/dependency_resolver.py`, and the line-coverage floors for
`automation/implementer.py`, `automation/planner.py`,
`automation/loop_runner.py`, `automation/loop_repo_manager.py`, and
`automation/pipeline_cli.py`. Remove a module floor only when its source is
deleted. Keep all other module floors.

`fail_under` in `[tool.coverage.report]` is 85. The gate measures the whole
package. Hermetic unit tests inject the agent, GitHub, Git, subprocess, clock,
and terminal seams, so live boundaries are exercised without a live transport.
Imports and test names do not prove coverage.

Measuring the product layer does not relax a safety boundary. The following
rules apply to every module in that layer, and a change to any of them needs
the same source review as a library change.

- **Prompt fencing stays.** Every untrusted GitHub value still passes through
  `fence_untrusted()` in `hephaestus/prompts/fencing.py` with a fresh nonce, and
  still carries the untrusted-content notice.
- **Transport size caps stay.** Review publication still caps the finding count,
  each finding path, body, and evidence field, the aggregate record bytes, the
  review body, the demoted-findings section, the summary, the raw feedback, the
  warning list, and the reported-finding text.
- **The sign-off gates stay fail closed.** The plan-review `GO` verdict and the
  implementation-review `GO` plus the `state:implementation-go` label proof
  still require the explicit reviewer verdict. A missing or malformed verdict
  invalidates the audit before any label write. Informational validation never
  relaxes this rule.

Change the omit list, a module floor, or `fail_under` only in a commit that
also updates this section.

## Python Development Guidelines

### Language Preference

**Python 3.13** is the implementation language for all Hephaestus code:

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

- Python 3.13
- Type hints required for all functions
- Clear docstrings for public functions and classes
- Error handling at module boundaries only. See
  [Error handling](#error-handling).
- Core-library test coverage — 85% test coverage for the whole `hephaestus/`
  package, enforced by the CI gate (`fail_under = 85`). See
  [Test policy](#test-policy).
- Follow PEP 8 style guidelines

## Error handling

Handle errors at module boundaries. A module boundary is a console-script
entry point, a public library API entry point, or a call that crosses a real
process, filesystem, or network boundary.

- **Inside a helper:** raise plainly. Do not catch an exception you cannot do
  something useful with. Do not add a guard for a state the caller already
  excludes.
- **At a boundary:** catch, and report. Use `hephaestus/errors.py` so the same
  failure produces the same exit code and the same stderr message every run.
- **Prefer a warning to a refusal.** When a condition degrades the result but
  does not make it unusable, log a warning that names the recovery step and
  continue. Refuse only when continuing would produce a wrong result.
- **Never silently swallow.** A caught exception with no log, no warning, and
  no re-raise is a defect.

## Test policy

Test the core library. A test must import and exercise a module under
`hephaestus/`. Behaviour tests belong here:

- A function or class that a caller uses.
- A real error case: bad input, a missing file, a failed subprocess, an
  unreachable network host, a timeout.

Do not add tests for:

- Documentation text, README content, or ADR files.
- Prompt strings, template text, or exact message wording.
- Repository configuration, hook wiring, or file layout.
- An impossible state that no caller can construct.
- A hypothetical input the type system or a caller already excludes.

Keep coverage honest: do not add a test to raise a number.

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
- Document secret requirements in README, not code

Do not add a provenance or attestation layer for ordinary inputs. Trust a value
that came from your own process. Validate it at the boundary where it enters.

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
- Follow the [ASD-STE100 writing standard](docs/asd-ste100.md)

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

Before a substantive task, select the applicable skills from the installed
skill catalog. Always use that catalog as the starting point. You may also use
`athena:skill-advisor` when it is available and useful. The advisor is optional;
its absence or failure does not block skill selection or the task.
Keep all applicable task, skill, and repository requirements.

For a subagent task, use the installed catalog for the assigned scope and
follow the task prompt.

### Verification

An agent verifies its own work. Run the checks you need for the change you
made, in this order:

1. Focused tests. Select the tests for the affected behaviour and each new or
   changed test. Use locked pytest commands with explicit paths or node IDs
   and `--override-ini="addopts="`. Confirm nonempty collection and success.
2. Lint and format: `uv run ruff check` and `uv run ruff format --check`.
3. Types: `uv run mypy` on the changed paths.

A complete local suite is not a prerequisite. Nightly CI/CD owns the full
suites and coverage. Let Git run its installed, mutating hooks during commit
and push; do not run `pre-commit run` separately.

Report each command you ran and its outcome. Do not claim a command ran when it
did not. If a check fails, fix it or report the failure with its exit status
and the evidence you collected.

Subagents are still useful for verification that would otherwise stall the main
agent. A test-only subagent runs checks and edits nothing. Give it the exact
commands it needs.

Source review is separate from test execution. The reviewer inspects source and
the results you report.

### Contribution sequence

1. Work on a feature branch. A dirty working tree is normal; do not reset,
   discard, or stash work you did not create.
2. Rebase only for an actual conflict or a necessary dependency. Rebase with
   `git rebase -S <target>`. Keep unrelated branches and worktrees unchanged.
3. Stage only the files you edited. Never use `git add -A`, `git add .`, or
   `git add --all`; they stage every dirty and untracked path in the tree.
4. Commit with `git commit -s -S`. Require exactly one `Signed-off-by` trailer
   that matches the commit author.
5. Push the branch and open the pull request with the required
   `Closes #<issue-number>` line.

Required CI/CD supplies the merge evidence. It does not authorize the
implementation GO state; the queue's `pr_review` stage does.

### Skill Catalog

Invoke an Athena skill with `Skill(skill: "athena:<name>", args: "<argument>")`, or
`/athena:<name> <argument>` interactively. The **Arguments** column mirrors the
Athena plugin's `argument-hint` frontmatter; `—` means the skill takes no
argument. `.claude/settings.json` is the
repository-local source of truth for which skill plugins are enabled.

| Skill | Arguments | When to Use |
|-------|-----------|-------------|
| `athena:skill-advisor` | `<task description>` | Optional aid after consulting the installed skill catalog |
| `athena:advise` | `<task description>` | Before starting work — search Mnemosyne for prior learnings |
| `athena:learn` | — | After completing work — capture session learnings in Mnemosyne |
| `athena:implement` | `<issue number>` | Implement an approved plan, handling a dirty worktree or stale branch yourself |
| `athena:address-review` | `<PR number>` | Reply to and resolve every review thread on a PR |
| `athena:realign` | `<plan or issue>` | When a plan conflicts with the architecture — bring the plan back into alignment |
| `athena:myrmidon-swarm` | `<task description>` | Complex multi-step tasks requiring parallel agent coordination |
| `athena:brainstorm` | `<idea or feature description>` | Before implementing a new feature — design before code |
| `athena:test-driven-development` | `<feature or bugfix description>` | Before writing implementation code — RED-GREEN-REFACTOR |
| `athena:systematic-debugging` | `<description of the bug or failure>` | Before proposing fixes — root cause first |
| `athena:verification` | `<what you are verifying>` | Before claiming work is done — evidence before assertions |
| `athena:git-worktrees` | `<branch-name or feature description>` | When needing isolated branch workspace |
| `athena:finish-branch` | `"<optional: base branch name>"` | When implementation is complete — branch completion workflow |
| `athena:code-review` | `<what was implemented>` | After major feature completion — Sonnet reviewer + feedback reception |
| `athena:repo-analyze` | — | Comprehensive 15-dimension repository audit |
| `athena:repo-analyze-quick` | — | Quick repository health check |
| `athena:repo-analyze-strict` | — | Ruthlessly thorough repository audit |
| `athena:repo-analyze-full` | — | Full-coverage audit — one swarm agent per section, no sampling cap |
| `athena:repo-analyze-quick-full` | — | Quick health check with full file coverage |
| `athena:repo-analyze-strict-full` | — | Strict audit with full file coverage (swarm per section) |
| `athena:pr-review` | — | Athena full-coverage pull-request review |
| `athena:worktree-cleanup` | `"<optional: --dry-run>"` | Audit + prune git worktrees (never deletes branches) |
| `athena:tidy` | `"<optional: --dry-run \| --no-swarm \| --trunk BRANCH \| --max-concurrent N>"` | Clean merged branches; select rebases only through an explicit operation |
| `athena:create-reusable-utilities` | — | Port/generalize utility scripts for cross-project reuse |
| `athena:github-actions-python-cicd` | — | Set up a Python GitHub Actions CI/CD pipeline |
| `athena:python-repo-modernization` | `<path to Python repo to modernize>` | Bring a Python repo to production-grade quality |

### Agent Skills vs Sub-Agents Decision Tree

```text
Is the task well-defined with predictable steps?
├─ YES → Use an Agent Skill (see catalog above)
│   ├─ Is it a new feature? → brainstorm → test-driven-development
│   ├─ Is it a bug? → systematic-debugging → test-driven-development
│   ├─ Is it ready to ship? → verification → finish-branch
│   ├─ Is it a CI/CD pipeline setup? → github-actions-python-cicd
│   ├─ Is it a repo audit? → repo-analyze (or its quick/strict/full variants)
│   └─ Is it a PR review? → `$athena:pr-review`
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

Hephaestus uses trunk-based development: create one short-lived feature
branch per issue, open a pull request, squash-merge it back to `main`, and cut
releases from signed `vX.Y.Z` tags; there are no release branches.

Branch naming is a recommendation, not a requirement. Prefer
`<issue-number>-<description>` for new branches. Other valid Git branch names
are permitted. Do not block review, CI/CD, or merge because a branch does not
use this format. Do not request a naming exception or require a branch rename.
Keep the exact branch-identity checks that protect automation ownership and
recovery.

#### Pull-request flow

1. The PR body contains `Closes #<issue-number>` on its own line.
2. Commit subjects follow Conventional Commits and carry a DCO `Signed-off-by`
   trailer. The active ruleset requires signed commits.
3. PR titles follow `type(scope)!: description` because the title becomes the
   squash-merge subject.

The `pr_review` stage runs `$athena:pr-review`. The reviewer posts ordinary
GitHub review comments. The author agent runs `$athena:address-review`, which
replies to every thread and resolves it.

When no review thread is unresolved, the reviewer applies
`state:implementation-go`. Hephaestus then runs `gh pr merge --squash --auto`
unless the agent already armed auto-merge. If `state:implementation-no-go` is
applied, Hephaestus runs `gh pr merge --disable-auto`. GitHub applies branch
protection, the required checks, and any merge queue. If the merge reports a
real conflict, the agent rebases and continues.

```bash
git checkout -b <issue-number>-description
git add <files>
git commit -s -S -m "type(scope): description"
git push -u origin <branch-name>
gh pr create --title "type(scope): description" \
  --body "$(printf 'Summary of change.\n\nCloses #<issue-number>\n')"
```

Branch naming is a recommendation. Prefer `<issue-number>-<description>`, and
never block work over a branch name.

### Commit Message Format

Follow conventional commits:

```text
feat(utils): Add new configuration helper
fix(logging): Correct timestamp formatting
docs(readme): Update installation instructions
refactor(io): Simplify file handling logic
```

### Testing Strategy

Follow the [test policy](#test-policy). A test must import and exercise a
module under `hephaestus/`. Cover the real error cases a caller can reach: bad
input, a missing file, a failed subprocess, an unreachable host, a timeout.
Do not add tests for documentation text, prompt strings, repository
configuration, exact message wording, or impossible states.

Verify your change with focused tests before you open the pull request. Full
suites and coverage run in CI/CD. The automation loop prepares the branch
before implementation and does not do a routine final rebase.

```bash
# Run an affected test file, including tests outside the default fast selection
uv run --locked pytest tests/unit/utils/test_general_utils.py \
  --override-ini="addopts=" -v --strict-markers
```

Use the paths and node IDs for the change. Full suites and coverage run in
CI/CD. A complete local run is not a PR-creation or merge prerequisite.

## Environment Setup

This project uses [uv](https://uv.sh) for environment management. The
one-command bootstrap (deps + editable install + pre-commit hooks) is
`just bootstrap`:

```bash
# Install deps, the editable hephaestus package, and pre-commit hooks
just bootstrap
```

`just bootstrap` wraps the two commands below. Run them manually if you do
not have [`just`](https://just.systems/) installed:

```bash
# 1. Install dependencies and create the environment
uv sync

# 2. Install the pre-commit hooks (uv-managed binary)
uv run pre-commit install
```

## Common Commands

### Development Workflows

```bash
# Run tests
uv run pytest tests/unit

# Run linter
uv run ruff check hephaestus/ tests/

# Check formatter
uv run ruff format --check hephaestus/ tests/

# Run type checking
uv run mypy hephaestus/ scripts/ tests/
```

### Pre-commit Hooks

Pre-commit hooks check code quality and run a fast test selection. Required
CI/CD supplies the full merge evidence. Nightly CI/CD owns the full-suite and
coverage results.

```bash
# Install pre-commit hooks (one-time setup)
pre-commit install

# Run hooks manually on all files
pre-commit run --all-files

# NEVER skip hooks with --no-verify
```

## Troubleshooting

### Common Issues

1. **Import Errors**: Check that `uv sync` has been run
2. **Dependency Conflicts**: Update `pyproject.toml` and run `uv sync`
3. **Test Failures**: Run tests with verbose output for details
4. **Formatting Issues**: Run `uv run ruff format hephaestus/ tests/`

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
- `tests/unit/` - Unit test suite. It mirrors the hephaestus/ subpackages.
  `SANCTIONED_EXTRA_TEST_DIRS` in `hephaestus/validation/test_layout.py` names
  the extra test directories that have no source counterpart.
- `tests/integration/` - Integration tests (package importability, smoke tests)
- `scripts/` - Automation and maintenance tools
- `docs/` - Documentation and guides
- `pyproject.toml` - Project metadata, dependencies, tool, and uv environment configuration
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
- **`pyproject.toml`** intentionally has no version field — do not add one.
- The `check-version-single-source` pre-commit hook enforces this invariant: it fails if
  a static `[project].version` is reintroduced, if `dynamic = ["version"]` or
  `[tool.hatch.version]` `source = "vcs"` is missing.
- To cut a release you do **not** edit any version field — a signed `vX.Y.Z` git tag drives
  it. See `docs/RELEASING.md` and `CONTRIBUTING.md` for the workflow.

Make sure all temporary files are in the build/ directory.

---

## AI-agent topology

The remainder of this document is a single-page map of the AI-agent topology and
conventions used by Hephaestus and the wider HomericIntelligence ecosystem.
The queue topology below is implemented as six main-lane queues and two
auxiliary queues.

## Agents the codebase orchestrates

The default `hephaestus-automation-loop` path is the queue-based in-process
pipeline in `hephaestus.automation.pipeline.coordinator`. The coordinator owns
eight bounded stage queues. A main worker pool runs ordinary work. A separate
host-only pool runs learning and terminal cleanup. Each agent job runs
**Claude Code**, **Codex**, **Pi** (admission-gated), or **OpenCode**, chosen via the
optional `--agent` CLI flag or auto-detected with a Claude preference when
omitted (see `hephaestus.agents.runtime.add_agent_argument`).
Pi is never auto-selected.

**Approval:** `pr_review` invokes `$athena:pr-review`. Its comments are
posted as written. A pull request with no unresolved review thread gets
`state:implementation-go`; otherwise it gets `state:implementation-no-go` and
the author agent addresses the threads. Any actor that follows this flow, human
or agent, may apply the label.

A reviewer can report that a fix must ship as its own child issue. The host
creates that child issue and the source PR stays in `state:implementation-no-go`
until the child merges.

**Error handling:** handle errors at module boundaries. Inside a helper, raise
plainly instead of catching and guarding. A dirty worktree is data, not a
fault: the agent classifies what it finds, stages only its own files, and
continues. Do not add a refusal for a dirty tree. Do not add a provenance,
attestation, or exact-identity check for a value your own process produced.

**Test policy:** test the core library. A test must import and exercise a
module under `hephaestus/`. Do not add tests for documentation text, prompt
strings, repository configuration, exact message wording, or impossible
states. Cover the real error cases a caller can reach.

| Queue stage | Module | Purpose |
|-------------|--------|---------|
| repo | `hephaestus.automation.pipeline.stages.repo` | Clone/discover, classify issues/PRs, and seed entry queues |
| planning | `hephaestus.automation.pipeline.stages.planning` | Advise and produce an implementation plan |
| plan_review | `hephaestus.automation.pipeline.stages.plan_review` | Strict plan review, amendment, and plan labels |
| implementation | `hephaestus.automation.pipeline.stages.implementation` | PR-writer worktree, rebase, implementation, tests, commit/push, and PR creation |
| pr_review | `hephaestus.automation.pipeline.stages.pr_review` | Detached read-only review with an informational grade and per-comment severity, one batched inline review, and implementation labels from the open threads |
| merge_wait | `hephaestus.automation.pipeline.stages.merge_wait` | Enables native auto-merge after GO approval, waits for the merge, and emits post-merge learning intent |
| learning | `hephaestus.automation.pipeline.stages.learning` | Claims durable intents and submits host-owned learning work |
| finished | `hephaestus.automation.pipeline.stages.finished` | Terminal ledger and auxiliary worktree cleanup/preservation |

`--learning-workers` and `--learning-queue-capacity` bound the auxiliary lane
independently. Both default to `1`. `--no-learn` prevents new learning work and
execution of learning intents.

Four console scripts use one parser and configuration builder in
`hephaestus.automation.pipeline_cli`. Each console script enters through a
base-importable shim in `hephaestus.cli.automation_entry`. The shim checks for
the `[automation]` extra: on a base install it reports a configuration error
with a recovery instruction instead of a raw `ModuleNotFoundError` traceback
(issue #3528). With the extra, the shim delegates to the automation module.

| Console script | Entry module | Main stage scope |
|----------------|--------------|------------------|
| `hephaestus-automation-loop` | `hephaestus.cli.automation_entry` → `hephaestus.automation.loop_runner` | All six main stages |
| `hephaestus-plan-issues` | `hephaestus.cli.automation_entry` → `hephaestus.automation.planner` | `planning → plan_review` |
| `hephaestus-implement-issues` | `hephaestus.cli.automation_entry` → `hephaestus.automation.implementer` | `implementation → pr_review → merge_wait` |
| `hephaestus-review-prs` | `hephaestus.cli.automation_entry` → `hephaestus.automation.pr_reviewer` | `pr_review` |

Learning and finished are implicit auxiliary stages for every scope. The full
command accepts `--stages` with contiguous main stage names in queue order.
Use `--merge-attempts` and `--max-workers`. Removed commands and option aliases
have no compatibility path.

The coordinator owns admission, routes, timers, permits, and completion.
Workers receive frozen requests and return one result for coordinator routing.
The accepted work item retains its repository-qualified file reservation
through implementation, review, and merge. Source jobs use an explicit
workspace binding. The worker checks source ownership under the workspace
lease before execution. One deadline covers lock waits, checks, and execution.

Current journals preserve plan pointers, learning claims, issue-wave
checkpoints, and source ownership. On restart, the coordinator reads GitHub for
the current labels and review threads and continues from there.

Stop old coordinators before cutover or rollback. Preserve uncertain effects,
local commits, worktrees, and current journals. Do not run old and new owners
against the same state directory. See
[ADR-0050](docs/adr/0050-queue-owned-automation-cutover.md).

## Agent runtime

`hephaestus.agents.runtime` is the thin layer that abstracts over Claude Code,
Codex, the currently fail-closed Pi provider boundary, and OpenCode. It provides:

- `add_agent_argument(parser)` — adds a uniform `--agent` flag to any CLI.
- `is_codex(agent_str)` / `is_pi(agent_str)` / `is_opencode(agent_str)` —
  provider-adapter branches kept inside the shared runtime.
- `run_codex_*` and `run_agent_*` text/session/resume helpers invoke direct
  providers through the neutral boundary. Public `run_pi_*` execution helpers
  also reject unadmitted automation; only `run_pi_smoke_session` is the fixed
  tool-free, non-interactive operator smoke seam. Normal Pi automation requires
  a reviewed external OS-isolation adapter. A fresh process loads only the
  explicitly selected `HEPH_PI_ISOLATION_ADAPTER` factory from the
  `hephaestus.pi_isolation_adapters` entry-point group; the base package ships
  no adapter and never auto-selects one.
  `hephaestus-install-pi-plugins` installs and preflights the catalog-pinned Pi
  CLI/package set, but passing preflight does not bypass external isolation,
  lifecycle, or role-scope admission gates. The smoke seam is not admission
  evidence. See ADR-0019, ADR-0020, ADR-0023, and ADR-0029.
- Claude is normally invoked via `hephaestus.automation.claude_invoke.invoke_claude_with_session`;
  the library-only fleet-sync conflict fallback uses `claude_code_sdk` with the scoped call-site
  controls below.

Athena `advise` and `learn` are host-owned operations, not agent-runtime
operations. `AthenaSkillJob` routes them only to the Mnemosyne host executor.
They do not invoke or validate Claude, Codex, Pi, or another harness. Provider
package, policy, and isolation checks apply only when a job executes through
that provider. Learning jobs carry a semantic intent; the host rebinds its
verified merged-PR evidence and reviewed candidate, prepares one bounded
validated skill change, then sends the closed request to the signed PR-delivery
service. Plan approval does not create learning work. See ADR-0025, ADR-0032,
and [learning evidence](docs/learning-evidence.md).

`hephaestus.automation.agent_config` supplies model, session, and timeout
defaults. The common parser exposes role timeout options, `--poll-max-wait`,
and `--git-message-timeout`. Host-owned advice and learning do not use agent
provider timeout options.

The automation loop selects tools and model strings independently. Use
`--planner-agent`, `--implementer-agent`, and `--reviewer-agent` to override
`--agent` for each role. Each role model overrides `--model`; a tool override
does not clear the global model. Omitted models use the selected tool default.
There is no model catalog or alias translation. Implementation helpers inherit
the implementation tool and model. Supply `--fallback-model` explicitly to
select a fallback. See ADR-0044.

The automation loop model options accept `MODEL[:EFFORT]`. The final nonempty
colon segment is a free-form effort. The runtime maps it to Codex
`model_reasoning_effort`, OpenCode `--variant`, or Pi `--thinking`. The value
`default` selects the applicable provider default. Claude uses the base model
without an effort selector. Codex retries one exact pre-work
unsupported-effort rejection without an explicit effort. OpenCode and Pi own
their native fallback behavior. OpenCode v1 applies only variants that its
resolved model configuration defines. It uses the base model options for an
unknown variant.

## Design Philosophy

The agent topology above is not accidental — it follows a small set of design
principles inherited from **ProjectOdyssey**, where the queue-based agent loop
and plan/review quality gates were first incubated before being generalized into
Hephaestus's shared tooling. Those principles, applied to agent design, are:

- **Simplicity first (KISS / YAGNI).** Each queue stage owns one responsibility
  and one reason to change; we do not add stages, providers, or abstractions
  until a concrete workflow needs them. The deferred `AgentProtocol` and
  resilience wiring (issues #468, #469) are intentionally *not* built yet.
- **One-way dependencies (DRY / boundaries).** The dependency arrow points only
  automation → library (see [Library vs product layer](#library-vs-product-layer)).
  Prompt construction lives in exactly one module (`hephaestus.automation.prompts`)
  so untrusted-content fencing is defined once, not per call site.
- **Substitutable providers (SOLID).** `hephaestus.agents.runtime` abstracts over
  Claude Code and Codex behind a uniform `--agent` flag so either provider is
  substitutable at a call site without changing orchestration logic.
- **Least privilege, least astonishment (POLA).** Every agent call site declares
  an explicit `--allowedTools` scope (see the permission-policy table below),
  runs in a scoped worktree, and defers irreversible actions to explicit
  repository policy gates.
- **Reviewed merges.** Every pull request is reviewed and its threads are
  resolved before it merges. GitHub branch protection and the required checks
  stay the merge contract.

For the full, non-agent-specific statement of these principles see
[Key Development Principles](#key-development-principles).

## Claude non-interactive permission policy

Claude invocations that pass `permission_mode="dontAsk"` are non-interactive
automation calls. They do not use `--dangerously-skip-permissions`, and
`hephaestus.automation.claude_invoke.invoke_claude_with_session` still forwards
the explicit `--allowedTools` scope. There is no OS-level seccomp, namespace, or chroot sandbox on this Claude path. The compensating controls are per-call tool
allowlists, cwd/worktree scoping, subprocess timeouts, prompt fencing for
untrusted GitHub content, secure logs, and GitHub branch protection plus the
required CI/CD checks.

`--allowedTools` supplies tool approvals. It does not restrict tool availability
by itself. The queue worker uses the explicit job scope when one is supplied.
Without an explicit scope, a read-only job uses `Read,Glob,Grep`. Other jobs use
the scope for their agent role. The worker forwards this scope and `dontAsk`
through the shared Claude invocation path.

| Call site | Tools | Scope / controls |
| --- | --- | --- |
| `pipeline/worker_pool.py:WorkerPool._invoke_agent` | Job scope, role scope, or `Read,Glob,Grep` | The worker applies the selected tool scope, `dontAsk`, the source lease, and the operation deadline. |
| `pipeline/stages/pr_review_jobs.py:PrReviewJobs._submit_review_job` | `Read,Glob,Grep,Bash,Skill,Agent,WebFetch` | The queue submits a read-only review job for the exact source head. The host owns review publication, labels, and merge admission. |
| `pipeline/stages/implementation.py` | `Read,Write,Edit,Glob,Grep,Bash` | The implementation job uses an isolated writer workspace. Host operations own Git publication and its policy checks. |
| `github/fleet_sync/conflict_resolver.py:_run_conflict_agent` | `none` | The conflict planner returns JSON edits from fenced input. The host validates paths and owns Git continuation, signing, and push. |

Fleet-sync `--dry-run` is a preview contract: GitHub reads and writes, Git subprocesses, pushes,
merges, and agent calls are suppressed or logged. The CLI may still allocate an ephemeral
temporary directory and pass Git actions through the dry-run logger so operators can see what
would run; no clone, worktree, rebase, or other Git mutation is executed.

## Prompt safety

`hephaestus.automation.prompts` builds every prompt the agents see. The module's
contract is that **all untrusted GitHub content**
(issue bodies, PR diffs, reviewer comments, plan text) is wrapped with
`_fence_untrusted()` using random nonces and accompanied by `_UNTRUSTED_NOTICE`.
This prevents a hostile issue body from forging a verdict line or injecting
instructions that bypass the PR review loop.
`hephaestus/prompts/fencing.py` holds the single definition of the fence.
`shared/untrusted_notice.j2` in the packaged catalog supplies the notice.

## Agent safety checkpoints

Several plugin-provided skills define workflow safety checks for agent actions.
These are workflow safety controls, not GitHub required approvals or merge
gates:

- `/athena:myrmidon-swarm`: If host policy or task scope requires approval,
  present the plan and ask for approval. Start safe work that is in scope only
  after you obtain all required approvals.
- `athena:skill-advisor` — optional aid when available and useful. Always
  consult the installed skill catalog first. Advisor failure does not block
  work. Routing does not grant permission for other actions.
- `/athena:finish-branch` and `/athena:code-review` — explicit confirm
  steps before tagging or force-pushing.

Every PR opened by the automation pipeline goes through GitHub's normal branch
protection and the `pr-policy` required-check gate
(see [PR policy](#pr-policy)); required CI/CD checks enforce the merge contract,
and the GitHub required-approving-review count remains intentionally zero for
this single-maintainer repository.

## Skill catalog (agent highlights)

The Athena plugins enabled in `.claude/settings.json` provide 23 reusable skills
the agents can invoke. See the [Skill Catalog](#skill-catalog) table above for
the full listing. Highlights:

- **Workflow**: `skill-advisor`, `advise`, `brainstorm`, `test-driven-development`,
  `systematic-debugging`, `verification`, `finish-branch`, `code-review`.
- **Repo audits**: `repo-analyze` and its `-quick`, `-strict`, `-full`, and
  `*-full` variants.
- **Worktrees**: `git-worktrees`, `worktree-cleanup`, `tidy`.
- **Orchestration**: `myrmidon-swarm` for hierarchical multi-agent fan-out.
- **Knowledge capture**: `learn` (writes back to the Mnemosyne marketplace).

## Configuration / boundaries

- Skill hooks, frontmatter, and per-skill `allowed-tools` are owned by the
  installed Athena plugins; `.claude/settings.json` is the repository-local
  source of truth for plugin enablement.
  Local skill copies, local skill symlink sets, and the skill-pin lock file
  are not repository source for Hephaestus.
- **MCP** (Model Context Protocol): `.mcp.json` is the version-controlled
  configuration surface for optional project-scoped agent tooling and remains
  intentionally empty. MCP is not a Hephaestus runtime API or ecosystem
  transport; package and automation operation must not depend on it.
  Plugin marketplaces, NATS JetStream, and HTTP REST remain the maintained
  integration contracts. See [`docs/mcp.md`](docs/mcp.md) and
  [ADR-0011](docs/adr/0011-mcp-integration-posture.md).
- The deferred follow-ups for cross-agent abstraction (a formal `AgentProtocol`)
  and for wiring `hephaestus.resilience` into the GitHub call path are tracked
  in issues #468 and #469.

## Canonical architecture reference

The **canonical unified reference** for the queue-pipeline, stage semantics, ROUTES table, scope trimming, durable journal, worker pool, and observability lives at [`docs/architecture.md`](docs/architecture.md). Update the doc (not this file) when the topology changes; this file remains the agent contract and agent-topology map.
