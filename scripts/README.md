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
- **`check_build_dir_untracked.py`** — Fail if anything becomes tracked under
  `build/` (sanctioned gitignored scratch dir; issue #1214).
- **`check_conventional_commit.py`** — Validate commit subjects against
  Conventional Commits (commit-msg hook + `pr-policy` CI).
- **`check_dco_signoff.py`** — Require a DCO `Signed-off-by` trailer on every
  commit message (commit-msg hook + `pr-policy` CI).
- **`check_license_compatibility.py`** — Fail CI when a distributed
  dependency's license is incompatible with BSD-3-Clause (see `NOTICE`).
- **`check_private_denylist.py`** — Reject strings from an operator-local
  `.heph-private-denylist` in tracked/staged files, without echoing values.
- **`check_security_policy_no_hardcoded_date.py`** — Reject hard-coded
  `As of YYYY-MM-DD` stamps in `SECURITY.md` (issue #730).

### Operations

- **`backup_state.py`** — Archive the tiered operational state (per the
  backup/disaster-recovery ADR) for restore; runs under bare `python3` so it
  works in the broken environment it recovers.

### Markdown

- **`fix_invalid_links.py`** — Fix invalid absolute-path links in markdown
  files (wraps `hephaestus.markdown.link_fixer`).

### Versioning

- **`update_version.py`** — Update secondary version files (`VERSION`,
  `__init__.py`) via `hephaestus.version.manager`. The canonical version comes
  from git tags via hatch-vcs — see [`../docs/RELEASING.md`](../docs/RELEASING.md).

### Scaffolding / automation introspection

- **`scaffold_subpackage.py`** — CLI shim for
  `hephaestus.scripts_lib.scaffold_subpackage`: scaffold a new `hephaestus`
  subpackage with matching test structure.
- **`show_prompt.py`** — Display the automation-pipeline agent prompt for a
  given GitHub issue and stage (planning, implementation, pr-review, …).

### Git / GitHub workflow helpers

- **`choose_merge_flag.sh`** — Sourceable `choose_merge_flag()` helper that
  picks a permitted manual merge strategy for a repo (rebase → squash → merge).
- **`shell/preflight_check.sh`** — Six pre-flight checks before starting work
  on a GitHub issue (closed issue, merged PR, worktree conflict, …).
- **`shell/cleanup-stale-worktrees.sh`** — Clean up git worktrees whose issue
  is closed or whose branch is merged into `main`.
- **`shell/drive_prs_green_ecosystem.sh`** — Drive failing PRs to green CI
  across every non-fork HomericIntelligence repo, with per-repo logs.

### Installation / environment

- **`shell/install.sh`** — HomericIntelligence ecosystem installer: check (and
  optionally install) all mesh dependencies by role
  (see `../docs/INSTALLER_ARCHITECTURE.md`).
- **`shell/lib/install_helpers.sh`** — Sourceable helper library (colors,
  counters, check helpers) shared by the installer scripts.
- **`shell/install_hooks.sh`** — Install this repo's git hooks via the
  `pre-commit` framework (wraps `uv run pre-commit install`).
- **`shell/setup_api_key.sh`** — Export `ANTHROPIC_API_KEY` from Claude CLI
  credentials for container execution.

### Disaster recovery

- **`backup_state.py`** — Backup, restore, and verify tier-3 operational
  state (`build/.issue_implementer/`); stdlib-only so it runs in a broken
  environment. See `../docs/adr/0012-backup-and-disaster-recovery-policy.md`
  and `../docs/runbooks/backup-restore.md`.

### Forensics / crash debugging

- **`shell/coredump-host-handler.sh`** — Pipe-mode `core_pattern` handler that
  captures container coredumps to a host-side directory.
- **`shell/run-under-gdb.sh`** — Run a command under gdb so fatal signals are
  caught before in-process handlers swallow them.

### Benchmarks / demos

- **`compare_benchmarks.py`** — Compare benchmark results across runs.
- **`demo_cli.py`** — Demo CLI functionality.
- **`example_usage.py`** — Usage examples.

### Pi smoke validation

- **`pi_smoke.py`** — Run a read-only Pi smoke prompt using
  `HEPH_PI_PROVIDER` and `HEPH_PI_MODEL` from the environment.
- **`pi_smoke_slurm.py`** — Submit `scripts/slurm/pi_smoke.sbatch` with
  `sbatch` while exporting only env var names, not alias values.
- **`slurm/pi_smoke.sbatch`** — Slurm batch template that invokes
  `pi_smoke.py` on a cluster node (copy and fill partition/account locally).

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
