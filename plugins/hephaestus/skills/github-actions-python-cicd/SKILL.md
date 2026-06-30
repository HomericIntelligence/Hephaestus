---
name: github-actions-python-cicd
description: Set up a GitHub Actions CI/CD pipeline for a Python project on the ProjectHephaestus reference stack (pixi + pyproject.toml + ruff + mypy + hatch-vcs), Python 3.10-3.13
allowed-tools: [Read, Write, Edit, Bash, Grep, Glob]
---

# GitHub Actions Python CI/CD Setup

## Source of Truth

The canonical stack parameters (Python floor, supported-version classifiers,
package layout, linter, env manager, versioning scheme) live in the
repository's `CLAUDE.md` and in `pyproject.toml`. If you find this skill
drifting from `CLAUDE.md` (e.g. the Python floor moves to 3.11, a new
classifier is added), update `CLAUDE.md` and `pyproject.toml` FIRST, then
propagate the change here. Snippets in this skill MUST NOT be treated as
the source of truth for stack choices — they are illustrative examples of
how to wire the canonical parameters into a workflow.

## Overview

| Attribute | Value |
|-----------|-------|
| **Stack** | pixi + pyproject.toml + hatch-vcs + ruff + mypy + yamllint + pytest |
| **Python matrix** | 3.10, 3.11, 3.12, 3.13 (mirrors `pyproject.toml` classifiers) |
| **Action pinning** | Digest-pinned with `# vX.Y.Z` comment, never bare `@v6` |
| **Workflow split** | Required gate (`_required.yml`) + cross-version matrix (`test.yml`) |
| **Audience** | HomericIntelligence-ecosystem repos on (or adopting) the ProjectHephaestus reference stack |
| **Single source of truth** | `CLAUDE.md` (§ Language Preference, § Python Development Guidelines, § Version Management) |

If `CLAUDE.md` changes the Python floor or swaps a tool, update `CLAUDE.md`
first and treat every value in this skill as a derived copy that must follow.

## When to Use

- A new HomericIntelligence repo needs CI from scratch.
- An existing repo is on flake8/black/`src/`/`requirements.txt` and is being
  brought onto the ruff + pixi + pyproject.toml reference stack.
- Adding a new supported Python version (must update matrix here AND
  classifiers in `pyproject.toml`; `hephaestus-check-python-version`
  enforces the invariant via pre-commit).

Do NOT use this skill for:

- Repos that have intentionally chosen a divergent stack (e.g. poetry, pdm,
  hatch envs without pixi). Pick the stack-appropriate skill instead.
- Setting `cache: true` on `prefix-dev/setup-pixi` (key on `pixi.lock` with
  `actions/cache` instead).
- Repos that use `requirements.txt` / `setup.py` (use a different skill).
- Python 3.8/3.9 — these are below the CLAUDE.md-mandated 3.10 floor.

## Stack Decisions

| Choice | Why | Anti-pattern |
|---|---|---|
| `pixi` for env management | Conda-forge availability + reproducible lock | `requirements.txt` / `requirements-dev.txt` / `setup.py` |
| `pyproject.toml` only | hatch-vcs dynamic version; no static version field | `setup.py`, `setup.cfg`, `[project].version =` |
| `ruff check` + `ruff format` | Subsumes flake8 + isort + black | flake8, black, isort |
| `mypy` via pixi `lint` env | Reproducible plugin/version set | ad-hoc `pip install mypy` |
| `yamllint` | Catches workflow drift | none |
| Flat top-level package (`hephaestus/`) | Hatchling auto-discovery; matches CLAUDE.md repo structure | `src/` layout |
| Python 3.10–3.13 matrix | Matches `pyproject.toml` classifiers + CLAUDE.md floor | 3.8, 3.9 |
| Digest-pinned `uses:` with version comment | Supply-chain hygiene, matches repo CI convention | bare `@v6` tags |

## Reference Workflow — multi-interpreter test matrix

### Two workflows, not one

The recommended structure is a split:

- **`_required.yml`** — the required gate. Runs lint (ruff/yamllint/mypy),
  unit + integration tests with coverage gating, build smoke test, and
  schema/pr-policy gates on a single canonical Python (3.12 in this repo).
- **`test.yml`** — cross-version compatibility matrix only. Proves the library
  imports and its tests pass on every supported Python. Does NOT re-run lint,
  coverage, or build (DRY).

Adding a new supported Python means updating the matrix in `test.yml` AND
the `Programming Language :: Python :: 3.x` classifiers in `pyproject.toml`.
A `check_python_version_consistency` pre-commit hook enforces the match.

### 1. Cross-version matrix workflow

`.github/workflows/test.yml`:

```yaml
name: Test

on:
  pull_request:
  push:
    branches: [main]

concurrency:
  group: ${{ github.workflow }}-${{ github.head_ref || github.sha }}
  cancel-in-progress: true

permissions:
  contents: read

jobs:
  test:
    runs-on: ${{ matrix.os }}
    timeout-minutes: 30
    strategy:
      fail-fast: false
      matrix:
        os: [ubuntu-latest]
        # MUST match every `Programming Language :: Python :: 3.x`
        # classifier in pyproject.toml. CLAUDE.md mandates 3.10+ floor.
        python-version: ["3.10", "3.11", "3.12", "3.13"]
        test-type: [unit, integration]
    defaults:
      run:
        shell: bash
    steps:
      # Pin third-party actions by commit SHA, with the version tag in a
      # trailing comment. Mirrors the rest of this repo's workflows.
      - uses: actions/checkout@de0fac2e4500dabe0009e67214ff5f5447ce83dd  # v6.0.2

      - name: Set up Python ${{ matrix.python-version }}
        uses: actions/setup-python@a309ff8b426b58ec0e2a45f0f869d46889d02405  # v6
        with:
          python-version: ${{ matrix.python-version }}

      - name: Cache pip packages
        uses: actions/cache@27d5ce7f107fe9357f9df03efb73ab90386fccae  # v5
        with:
          path: ~/.cache/pip
          # No requirements.txt or setup.py in a pixi/hatchling project —
          # key on pyproject.toml.
          key: pip-${{ runner.os }}-${{ matrix.python-version }}-${{ hashFiles('pyproject.toml') }}
          restore-keys: |
            pip-${{ runner.os }}-${{ matrix.python-version }}-

      - name: Install package
        run: pip install -e ".[dev,schema]"

      - name: Run unit tests
        if: matrix.test-type == 'unit'
        run: pytest tests/unit --override-ini="addopts=" -v --strict-markers

      - name: Check unit test structure
        if: matrix.test-type == 'unit'
        # Console script: hephaestus-check-test-structure =
        # "hephaestus.validation.test_structure:main" (see pyproject.toml).
        run: hephaestus-check-test-structure

      - name: Run integration tests
        if: matrix.test-type == 'integration'
        run: pytest tests/integration --override-ini="addopts=" -v --strict-markers
```

Adjust `[dev,schema]` to match the extras your `[project.optional-dependencies]`
exposes. If your repo has only `dev`, use `.[dev]`.

### 2. Required lint job (pixi-driven)

`.github/workflows/_required.yml` (excerpt):

```yaml
jobs:
  lint:
    name: lint
    runs-on: ubuntu-24.04
    timeout-minutes: 20
    steps:
      - uses: actions/checkout@de0fac2e4500dabe0009e67214ff5f5447ce83dd  # v6.0.2
      - name: Setup pixi env (lint)
        # Local composite action. If your repo lacks one, set up pixi directly
        # with prefix-dev/setup-pixi@<digest> # v0.X.Y instead.
        uses: ./.github/actions/setup-pixi-env
        with:
          environments: lint
          cache-key-prefix: pixi-lint
      - name: Ruff check
        run: pixi run --environment lint ruff check <pkg> scripts tests
      - name: Ruff format check
        run: pixi run --environment lint ruff format --check <pkg> scripts tests
      - name: yamllint
        run: pixi run --environment lint yamllint -c .yamllint.yaml .
      - name: mypy
        run: pixi run --environment lint mypy
```

Replace `<pkg>` with the flat top-level package directory (in
ProjectHephaestus: `hephaestus`). Never use `src/`.

## `pyproject.toml` essentials

```toml
[build-system]
requires = ["hatchling>=1.27.0,<2", "hatch-vcs>=0.4.0,<1"]
build-backend = "hatchling.build"

[project]
name = "<your-package>"
dynamic = ["version"]
requires-python = ">=3.10"
classifiers = [
    "Programming Language :: Python :: 3.10",
    "Programming Language :: Python :: 3.11",
    "Programming Language :: Python :: 3.12",
    "Programming Language :: Python :: 3.13",
]

[tool.hatch.version]
source = "vcs"

# REQUIRED — without this hook the build does not generate the version file.
[tool.hatch.build.hooks.vcs]
version-file = "<your-package>/_version.py"
```

Do NOT add a static `[project].version`. Do NOT add a `version` field to
`pixi.toml [workspace]`. See `CLAUDE.md` § Version Management.

## `pixi.toml` essentials

```toml
[workspace]
name = "<your-package>"
channels = ["conda-forge"]
platforms = ["linux-64"]
requires-pixi = ">=0.69.0"

[tasks]
test = "pytest"
lint = "ruff check <pkg> scripts tests"
format = "ruff format <pkg> scripts tests"
mypy = "mypy <pkg>/ scripts/ tests/"
dev-install = "pip install -e . --no-deps"

[dependencies]
python = ">=3.10"
pip = "*"
```

## Install Patterns — which to use when

| Context | Command | Why |
|---|---|---|
| Plain `actions/setup-python` + pip (matrix test job) | `pip install -e ".[dev,schema]"` | No pixi env present; pip resolves the closure once per matrix cell. |
| Inside a pixi-managed env (lint job, local dev) | `pip install -e . --no-deps` (via `pixi run dev-install`) | Pixi already resolved deps; `--no-deps` prevents pip from re-resolving and churning `pixi.lock`. |

Never use `pip install -e .[dev]` inside a pixi env — it causes the lockfile
churn documented in `pixi.toml` comments on the `dev-install` task.

## Common Pitfalls

- ❌ `flake8`, `black`, `isort` — ruff subsumes all three; do not re-introduce.
- ❌ `hashFiles('**/requirements*.txt', 'setup.py')` cache keys — neither file
  exists in this stack. Hash `pyproject.toml`.
- ❌ Scanning `src/` — flat package at repo root.
- ❌ Python 3.8/3.9 in the matrix — floor is 3.10 (CLAUDE.md § Language Preference).
- ❌ Adding a Python version to the matrix without the matching
  `Programming Language :: Python :: 3.x` classifier — `check_python_version_consistency`
  pre-commit hook fails the commit.
- ❌ Bare `@v6` / `@v5` action tags — repo CI convention is digest-pin + `# vX.Y.Z` comment.
- ❌ Declaring runtime deps (e.g. PyYAML) in the workflow's install step — they
  belong in `[project].dependencies`.
- ❌ `[project].version = "..."` or `[workspace].version =` in pixi.toml — the
  `check-version-single-source` pre-commit hook fails.
- ❌ Omitting `[tool.hatch.build.hooks.vcs]` — builds run but the version file
  is not generated; runtime `__version__` lookup falls back unexpectedly.

## Checklist

- [ ] `.github/workflows/test.yml` matrix is `["3.10", "3.11", "3.12", "3.13"]`
- [ ] Lint job runs `pixi run --environment lint ruff check / ruff format --check / yamllint / mypy`
- [ ] Cache key hashes `pyproject.toml` (pip cache) or `pixi.lock` (pixi env)
- [ ] No `flake8`, `black`, or `src/` references in `.github/workflows/`
- [ ] All `uses:` lines are digest-pinned with a `# vX.Y.Z` comment
- [ ] `pyproject.toml` has `dynamic = ["version"]`, `[tool.hatch.version] source = "vcs"`, AND `[tool.hatch.build.hooks.vcs]` with a `version-file =`
- [ ] `requires-python = ">=3.10"` and classifiers list 3.10–3.13
- [ ] `pixi.toml [workspace]` has no `version` field
- [ ] Workflow matrix and `Programming Language :: Python :: 3.x` classifiers match
- [ ] Required gate (`_required.yml`) separate from cross-version matrix (`test.yml`)

## Related Skills

- `renovate-pixi-conda-dependency-automation` — pixi-aware dependency automation
- `github-actions-workflow-required-checks` — branch-protection ruleset alignment
- `run-tests` — pytest execution patterns
- `python-repo-modernization` — bringing a legacy repo onto this stack;
  reference implementation for the patterns in this skill
- `create-reusable-utilities` — porting utilities across the HomericIntelligence ecosystem

## Cross-References (single source of truth)

When values in this skill drift, update the canonical source FIRST and re-derive:

- Python floor + supported versions → `CLAUDE.md` § Language Preference, § Python Development Guidelines
- Linter / formatter / type checker selection → `CLAUDE.md` § Common Commands, § Pre-commit Hooks
- Editable install + pixi rationale → `pixi.toml` comments on the `dev-install` task
- Version management (hatch-vcs, no static version) → `CLAUDE.md` § Version Management; `docs/RELEASING.md`
