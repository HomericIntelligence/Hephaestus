# Scripts Directory

Shell helpers and standalone maintenance scripts for Hephaestus. Most
Python command-line interfaces live in `hephaestus.*` modules and are exposed
through installed `hephaestus-*` console scripts.

## Available Scripts

> The former thin wrappers (`plan_issues.py`, `implement_issues.py`,
> `drive_prs_green.py`, `merge_prs.py`, `audit_doc_policy.py`,
> `check_tier_labels.py`, `check_cli_table_sync.py`,
> `check_python_version_consistency.py`, `check_version_single_source.py`)
> were removed in #1445. The former `check_unit_test_structure.py` wrapper was
> also removed — invoke the installed `hephaestus-*` console scripts or
> `python3 -m hephaestus.<module>` instead.

### Validation / pre-commit checks

- **`validate_readme_commands.py`** — Validate that commands shown in README
  code blocks actually run.
- **`check-symlinks.sh`** — Detect broken symlinks in the repo.

### Markdown

- **`fix_invalid_links.py`** — Fix invalid absolute-path links in markdown
  files (wraps `hephaestus.markdown.link_fixer`).

### Versioning

- **`update_version.py`** — Update secondary version files (`VERSION`,
  `__init__.py`) via `hephaestus.version.manager`. The canonical version comes
  from git tags via hatch-vcs — see [`../docs/RELEASING.md`](../docs/RELEASING.md).

### Release operations

- **`release_rollback.py`** — Fail-closed release preflight plus read-only
  inspection and confirmed withdrawal-advisory application for immutable
  releases. See [`../docs/RELEASING.md`](../docs/RELEASING.md).

### Benchmarks / demos

- **`compare_benchmarks.py`** — Compare benchmark results across runs.
- **`demo_cli.py`** — Demo CLI functionality.
- **`example_usage.py`** — Usage examples.

### Pi smoke validation

- **`pi_smoke.py`** — Run a read-only Pi smoke prompt using
  `HEPH_PI_PROVIDER` and `HEPH_PI_MODEL` from the environment.
- **`pi_smoke_slurm.py`** — Submit `scripts/slurm/pi_smoke.sbatch` with
  `sbatch` while exporting only env var names, not alias values.

## Usage

```bash
# Pre-commit-checked validators
hephaestus-check-test-structure
python3 -m hephaestus.scripts_lib.check_version_single_source
python3 -m hephaestus.scripts_lib.check_cli_table_sync

# Markdown link fixer
python3 scripts/fix_invalid_links.py .

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
