# AGENTS.md

This file is the single authoritative agent contract for Hephaestus. It is
self-contained: it holds the project overview, development rules (commit policy,
branch naming, version model), the skill catalog, and the AI-agent topology map
used by Hephaestus and the wider HomericIntelligence ecosystem. For enabled skill
plugins see [`.claude/settings.json`](.claude/settings.json).

## Project Overview

Hephaestus is the shared utilities and tooling repository of the
HomericIntelligence ecosystem. Named after Hephaestus, the Greek god of
craftsmanship, forging, and ingenious invention, this project provides the
foundational scripts, helpers, and infrastructure that support development across
all other repositories.

**Purpose**: Centralize and maintain Python utilities, helper functions, and
common abstractions used throughout the HomericIntelligence suite.

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

## Repository Structure

```text
Hephaestus/
├── hephaestus/                 # Python source code (21 documented subpackages)
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
│   ├── unit/                   # Unit tests (mirror hephaestus/ subpackages; a small sanctioned set of extra dirs covers non-package targets — scripts/, docs/, shell, top-level modules)
│   └── integration/            # Integration tests
├── docs/                       # Documentation
└── .claude/                    # Claude Code configurations
```

Agent skills are supplied by the Athena plugins enabled in
`.claude/settings.json`; plugin skill names use kebab-case
(`code-review`, `git-worktrees`). All Python packages use
lowercase_snake_case.

The product layer resides in `hephaestus/automation/`, and its mirrored unit
tests reside in `tests/unit/automation/`.

## Library vs product layer

`hephaestus/automation/` is a **product layer** (26.1k LoC, 53.9% of the
codebase) co-located with the utility library. It is gated behind the
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

### Coverage omit-list invariant

A small set of `hephaestus/automation/*` orchestration modules whose loops
shell out to live `claude`/`gh` CLIs are excluded from coverage via
`[tool.coverage.run].omit`. The contract: an omitted module's pure-function
helpers MUST still be unit-tested in `tests/unit/automation/`. This is enforced
executably — `tests/unit/validation/test_omit_allowlist.py` freezes the list's
membership, and `tests/unit/validation/test_omit_justification.py` (using `ast`
import-parsing) fails CI if any omitted module lacks a backing unit-test suite.
That guard checks a *proxy* (a test file imports the module and defines a test),
not that every helper is asserted. Reducing the omit list (target: −50% over two
releases, issue #1422) means promoting a module's orchestration logic to
mocked-subprocess unit coverage and removing its `omit` entry. The
`hephaestus.automation.pipeline` package follows the same product-layer
boundary: adding or moving pipeline modules is not a reason to expand the omit
list; prefer coordinator/stage seams that remain unit-testable without live
agent or GitHub CLIs.

## Python Development Guidelines

### Language Preference

**Python 3.10+** is the implementation language for all Hephaestus code:

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

Before beginning any substantive task, invoke `/athena:skill-advisor` to determine if a structured
skill applies. Use `Skill(skill: "athena:skill-advisor", args: "<task description>")`.

If you are a myrmidon-swarm subagent with a specific task prompt, skip this and follow your prompt directly.

### Skill Catalog

Invoke an Athena skill with `Skill(skill: "athena:<name>", args: "<argument>")`, or
`/athena:<name> <argument>` interactively. The **Arguments** column mirrors the
Athena plugin's `argument-hint` frontmatter; `—` means the skill takes no
argument. `.claude/settings.json` is the
repository-local source of truth for which skill plugins are enabled.

| Skill | Arguments | When to Use |
|-------|-----------|-------------|
| `athena:skill-advisor` | `<task description>` | Before any task — routes to the correct skill |
| `athena:advise` | `<task description>` | Before starting work — search Mnemosyne for prior learnings |
| `athena:learn` | — | After completing work — capture session learnings in Mnemosyne |
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
| `athena:tidy` | `"<optional: --dry-run \| --no-swarm \| --trunk BRANCH \| --max-concurrent N>"` | Rebase all local branches with swarm conflict resolution |
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

#### PR policy

The required CI gate `pr-policy` and the PR reviewer enforce:

1. The PR body MUST contain the literal line `Closes #<issue-number>` (capital
   `C`, no colon, on its own line). `Fixes`, `Resolves`, `closes`, and
   `Closes:` are NOT accepted.
2. Every commit MUST be cryptographically signed (`git commit -S`) and carry a
   DCO `Signed-off-by` trailer.

`pr-policy` blocks PRs that fail those checks. The queue runs
`$athena:pr-review` in its normal default profile when available, then applies
`state:implementation-go` on GO; `merge_wait` is the sole automatic armer and
consumes that loop-owned label.
Normal review may collect CI/CD evidence and incorporate it into its binary
verdict, but the loop does not change CI/CD. CI workflows and external
artifacts never independently grant that authority. Branch protection and
required reviews still govern whether GitHub merges the PR.

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

# 5. Do not use --admin or bypass branch protection. Queue-owned auto-merge,
#    when eligible, is armed only by merge_wait after loop review applies its label.
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
uv run pytest tests/unit -v

# Run specific test file
uv run pytest tests/unit/utils/test_general_utils.py -v

# Run with coverage
uv run pytest tests/unit --cov=hephaestus --cov-report=html
```

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

# Run formatter
uv run ruff format hephaestus/ tests/

# Run type checking
uv run mypy hephaestus/ scripts/ tests/
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
- `tests/unit/` - Unit test suite (mirrors hephaestus/ subpackages; sanctioned extra dirs in SANCTIONED_EXTRA_TEST_DIRS cover non-package targets like scripts/, docs/, shell installers, top-level modules)
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

## Agents the codebase orchestrates

The default `hephaestus-automation-loop` path is the queue-based in-process
pipeline in `hephaestus.automation.pipeline.coordinator`. The coordinator owns
seven in-memory stage queues and dispatches agent/build/git jobs to a worker
pool. Each agent job runs either **Claude Code** or **Codex**, chosen via the
optional `--agent` CLI flag or auto-detected with a Claude preference when
omitted (see `hephaestus.agents.runtime.add_agent_argument`).

**Loop-owned approval policy:** `pr_review` invokes `$athena:pr-review` with
its normal default behavior when available, otherwise uses its inline-review
fallback. It posts inline findings and a final grade/GO-NOGO review; a GO
applies `state:implementation-go`. Normal review may collect CI/CD evidence
and incorporate it into its binary verdict, but the loop does not change CI/CD
and no workflow, status, artifact, or lease independently authorizes it.
`merge_wait` is the sole automatic armer and consumes that loop-owned label.

| Queue stage | Module | Purpose |
|-------------|--------|---------|
| repo | `hephaestus.automation.pipeline.stages.repo` | Clone/discover, classify issues/PRs, and seed entry queues |
| planning | `hephaestus.automation.pipeline.stages.planning` | Advise and produce an implementation plan |
| plan_review | `hephaestus.automation.pipeline.stages.plan_review` | Strict plan review, amendment, and plan labels |
| implementation | `hephaestus.automation.pipeline.stages.implementation` | Worktree creation, implementation, tests, commit/push, and PR creation |
| pr_review | `hephaestus.automation.pipeline.stages.pr_review` | Inline PR review, validation, comment addressing, and implementation labels |
| merge_wait | `hephaestus.automation.pipeline.stages.merge_wait` | Sole automatic armer for loop-approved PRs; preserves post-merge learn |
| finished | `hephaestus.automation.pipeline.stages.finished` | Terminal ledger and worktree cleanup/preservation |

Console scripts preserve their historical names. Stage-scoped wrappers are
thin queue-pipeline scoped entry points over the coordinator; manual commands
that do not map to a pipeline stage remain out-of-band tools:

| Console script | Current module | Purpose |
|----------------|----------------|---------|
| `hephaestus-plan-issues` | `hephaestus.automation.planner` | Thin queue-pipeline planning/plan_review wrapper |
| `hephaestus-implement-issues` | `hephaestus.automation.implementer` | Thin queue-pipeline implementation/pr_review/merge_wait wrapper |
| `hephaestus-merge-prs` | `hephaestus.github.pr_merge` | Manual merge-driving command outside the queue coordinator |
| `hephaestus-review-prs` | `hephaestus.automation.pr_reviewer` | Thin queue-pipeline pr_review wrapper |
| `hephaestus-agent-stage` | `hephaestus.automation.agent_stage` | One-off stage invocation |

## Agent runtime

`hephaestus.agents.runtime` is the thin layer that abstracts over Claude Code and
Codex. It provides:

- `add_agent_argument(parser)` — adds a uniform `--agent` flag to any CLI.
- `is_codex(agent_str)` — branches between the two providers.
- `run_codex_text(...)`, `run_codex_session(...)`, `resume_codex_session(...)` —
  invoke Codex.
- Claude is normally invoked via `hephaestus.automation.claude_invoke.invoke_claude_with_session`;
  the library-only fleet-sync conflict fallback uses `claude_code_sdk` with the scoped call-site
  controls below.

Per-agent model/session/timeout configuration is centralised in
`hephaestus.automation.agent_config`, all operator-tunable via explicit CLI flags
on each automation command (e.g., `--agent-timeout`, `--poll-max-wait`,
`--git-message-timeout`, etc.). Legacy `claude_models`, `claude_timeouts`, and
`session_naming` modules remain compatibility shims over `agent_config`.

The automation loop also accepts `--planner-reasoning-effort`,
`--implementer-reasoning-effort`, and `--reviewer-reasoning-effort` for Codex
roles. Values are `default`, `low`, `medium`, `high`, or `xhigh`; `default`
omits Codex's `model_reasoning_effort` setting. An omitted flag preserves the
selected model alias's established reasoning default.

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
  runs in a scoped worktree, and defers all irreversible actions (merge, tag,
  force-push) to human-gated checkpoints.
- **Human-in-the-loop by default.** Autonomy is bounded: skills that can act
  destructively stop for a human gate, and every automation PR still passes
  branch protection and the `pr-policy` check.

For the full, non-agent-specific statement of these principles see
[Key Development Principles](#key-development-principles).

## Claude non-interactive permission policy

Claude invocations that pass `permission_mode="dontAsk"` are non-interactive
automation calls. They do not use `--dangerously-skip-permissions`, and
`hephaestus.automation.claude_invoke.invoke_claude_with_session` still forwards
the explicit `--allowedTools` scope. There is no OS-level seccomp, namespace, or chroot sandbox on this Claude path. The compensating controls are per-call tool
allowlists, cwd/worktree scoping, subprocess timeouts, prompt fencing for
untrusted GitHub content, secure logs, and GitHub branch protection plus human
review before merge.

| Call site | Tools | Scope / controls |
| --- | --- | --- |
| `audit_reviewer.py:run_audit_coordinator` | `Read,Glob,Grep` | Repo-root audit analysis; no write tools; direct-runner parity uses `sandbox="read-only"`. |
| `review_validator.py:_run_validation_session` | `Read,Glob,Grep` | Worktree validation of prior review comments; no write tools; GitHub updates stay in orchestrator code. |
| `comment_difficulty.py:_run_classifier_session` | `Read,Glob,Grep` | Worktree comment classification; no write tools; result is parsed JSON only. |
| `pr_review_core.py:_invoke_and_parse_review_session` | `Read,Glob,Grep,Bash,Skill,Agent,WebFetch` | Worktree PR analysis invokes the normal read-only `$athena:pr-review` workflow when available (or its inline fallback); the agent does not post reviews or mutate CI/CD. |
| `pipeline/stages/pr_review.py:PrReviewStage._review_wait` | `Read,Glob,Grep,Bash,Skill,Agent,WebFetch` | The sole pipeline GO/NOGO review uses the read-only AgentJob policy and may invoke the normal read-only `$athena:pr-review` workflow; validation and difficulty jobs keep `Read,Glob,Grep`. |
| `_implement_phase.py:ImplementPhase._run_claude_impl_session` | `Read,Write,Edit,Glob,Grep,Bash` | Initial implementation runs in the isolated issue worktree and remains subject to review and branch protection. |
| `_review_phase.py:ReviewPhase._resume_impl_with_feedback` | `Read,Write,Edit,Glob,Grep,Bash` | Review-feedback fixes resume the implementer in the isolated issue worktree and cannot bypass PR review or merge gates. |
| `address_review_core.py:_invoke_address_fix_session` | `Read,Write,Edit,Glob,Grep,Bash,Task,Skill` | Review-thread fixes run in the isolated issue worktree; `Task`/`Skill` support per-comment sub-agents and skill-advisor routing. |
| `github/fleet_sync/conflict_resolver.py:_run_conflict_agent` | `none` | Claude-only conflict planner receives only nonce-fenced conflict text and returns JSON edits; direct runtimes are rejected because their tool surfaces cannot provide the zero-tool contract, no agent invocation occurs in `--dry-run`, and the host validates/writes only known paths, owns all Git continuation/signing/push, snapshots remote URLs, and pins the final lease to the discovered branch SHA. |

Fleet-sync `--dry-run` is a preview contract: GitHub reads and writes, Git subprocesses, pushes,
merges, and agent calls are suppressed or logged. The CLI may still allocate an ephemeral
temporary directory and pass Git actions through the dry-run logger so operators can see what
would run; no clone, worktree, rebase, or other Git mutation is executed.

## Prompt safety

`hephaestus.automation.prompts` builds every prompt the agents see. The module's
contract — enforced by the test suite — is that **all untrusted GitHub content**
(issue bodies, PR diffs, reviewer comments, plan text) is wrapped with
`_fence_untrusted()` using random nonces and accompanied by `_UNTRUSTED_NOTICE`.
This prevents a hostile issue body from forging a verdict line or injecting
instructions that bypass the PR review loop. See the tests in
`tests/unit/automation/test_prompts.py` for the regression coverage.

## Human-in-the-loop checkpoints

Several plugin-provided skills mandate human gates that the agents must wait on:

- `/athena:myrmidon-swarm` — explicit Phase 1 "STOP HERE. Ask the user…"
  before any swarm deploys.
- `/athena:skill-advisor` — invoked at the start of any substantive task
  with `allowed-tools: []`, so it can route but never act autonomously.
- `/athena:finish-branch` and `/athena:code-review` — explicit confirm
  steps before tagging, force-pushing, or merging.

Every PR opened by the automation pipeline goes through GitHub's normal branch
protection and the `pr-policy` required-check gate
(see [PR policy](#pr-policy)) — a human still reviews and merges.

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
