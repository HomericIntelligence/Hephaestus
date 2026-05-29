# Scripts Directory

CLI wrapper scripts and shell helpers for ProjectHephaestus. Most Python
scripts here are thin wrappers around the corresponding `hephaestus.*` module
exposed via a `[project.scripts]` entry — they exist for local invocation
without needing the console script on `$PATH`.

## Available Scripts

### Validation / pre-commit checks

- **`check_cli_table_sync.py`** — Verify the README CLI table documents every
  `[project.scripts]` entry. Wired into pre-commit.
- **`check_python_version_consistency.py`** — Check the Python version is
  consistent across pyproject.toml, pixi.toml, and CI configs.
- **`check_tier_labels.py`** — Check the `tier-X` issue/PR labels match the
  policy.
- **`check_unit_test_structure.py`** — Verify `tests/unit/` mirrors the
  `hephaestus/` subpackage layout. Wired into pre-commit.
- **`check_version_single_source.py`** — Validate the project has a single
  authoritative version source (hatch-vcs git tags) and `pixi.toml` has no
  version field. Wired into pre-commit.
- **`audit_doc_policy.py`** — Audit documentation against the CLAUDE.md doc
  policy (e.g. no CHANGELOG.md).
- **`validate_readme_commands.py`** — Validate that commands shown in README
  code blocks actually run.
- **`check-symlinks.sh`** — Detect broken symlinks in the repo.

### Markdown

- **`fix_invalid_links.py`** — Fix invalid absolute-path links in markdown
  files (wraps `hephaestus.markdown.link_fixer`).

### Automation pipeline (Claude/Codex agent orchestration)

Each of these is a tiny wrapper around the matching `hephaestus.automation.*`
module — most users invoke the `hephaestus-*` console scripts instead.

- **`plan_issues.py`** → `hephaestus-plan-issues` (bulk issue planning;
  the planner owns its plan-review loop internally).
- **`implement_issues.py`** → `hephaestus-implement-issues` (bulk issue
  implementation in parallel worktrees; absorbs PR-review +
  thread-addressing in-loop).
- **`drive_prs_green.py`** → drive open PRs to green CI.
- **`run_automation_loop.sh`** — Legacy bash glue script, superseded by the
  `hephaestus-automation-loop` console script
  (`hephaestus.automation.loop_runner`). Drives the 3-stage pipeline
  (plan → implement → drive-green).

### GitHub

- **`merge_prs.py`** → `hephaestus-merge-prs` (merge open PRs with green CI).

### Versioning

- **`update_version.py`** — Update secondary version files (`VERSION`,
  `__init__.py`) via `hephaestus.version.manager`. The canonical version comes
  from git tags via hatch-vcs — see [`../docs/RELEASING.md`](../docs/RELEASING.md).

### Benchmarks / demos

- **`compare_benchmarks.py`** — Compare benchmark results across runs.
- **`demo_cli.py`** — Demo CLI functionality.
- **`example_usage.py`** — Usage examples.

## Usage

```bash
# Pre-commit-checked validators
python3 scripts/check_unit_test_structure.py
python3 scripts/check_version_single_source.py
python3 scripts/check_cli_table_sync.py

# Markdown link fixer
python3 scripts/fix_invalid_links.py .

# Automation pipeline (shell glue)
scripts/run_automation_loop.sh

# Symlink check
scripts/check-symlinks.sh
```

## Design Principles

Following CLAUDE.md guidelines:

- **KISS** (Keep It Simple, Stupid) — Scripts are thin wrappers
- **DRY** (Don't Repeat Yourself) — Logic lives in `hephaestus.*` modules; the
  scripts here just expose CLI entry points or shell glue
- **YAGNI** (You Aren't Gonna Need It) — Only port what's reusable
- **Modularity** — Clear separation between CLI and core logic
