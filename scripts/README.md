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

### Scaffolding / automation introspection

- **`show_prompt.py`** — Display the automation-pipeline agent prompt for a
  given GitHub issue and stage (planning, implementation, pr-review, …).
- **`compact_issue_timelines.py`** — Dry-run-first migration that reduces open
  and closed issue timelines to the latest actor-owned plan and plan review.
  See `../docs/runbooks/compact-issue-timelines.md`.

### Installation / environment

- **`shell/install.sh`** — HomericIntelligence ecosystem installer: check (and
  optionally install) all mesh dependencies by role
  (see `../docs/INSTALLER_ARCHITECTURE.md`).
- **`shell/lib/install_helpers.sh`** — Sourceable helper library (colors,
  counters, check helpers) shared by the installer scripts.

### Local CI

- **`run_ci_local.sh`** — Run the locally executable required source checks or
  a named subset. Its `build` subset runs the required artifact lifecycle lane
  rather than a separate package-build approximation. Project-toolchain
  commands use a Podman/Docker image that the runner builds automatically when
  absent. Set `HEPHAESTUS_CI_REBUILD=1` to rebuild the image from the current
  checkout; the autonomous queue always sets it. Linked-worktree Git metadata
  is mounted read-only so versioning and Git-aware scans inspect the candidate
  commit. `just`, ShellCheck, and Bats all run in the pinned CI image.

### Disaster recovery

- **`backup_state.py`** — Backup, restore, and verify tier-3 operational
  state (`build/.issue_implementer/`); stdlib-only so it runs in a broken
  environment. See `../docs/adr/0012-backup-and-disaster-recovery-policy.md`
  and `../docs/runbooks/backup-restore.md`.

### Pi smoke validation

- **`pi_smoke.py`** — Run a tool-free Pi smoke prompt using
  `HEPH_PI_PROVIDER` and `HEPH_PI_MODEL` from the environment.
- **`pi_smoke_slurm.py`** — Submit `scripts/slurm/pi_smoke.sbatch` with
  `sbatch` using a minimized environment and a fresh ACL-verified private
  scheduler run directory.
- **`slurm/pi_smoke.sbatch`** — Slurm batch template that invokes
  `pi_smoke.py` on a cluster node with a fixed export list and no shared
  scheduler artifact (copy and fill partition/account locally).

### Pi package acceptance

- **`pi_package_acceptance.py`** — Validate Athena's exact commit-pinned Pi
  package, upstream receipts, clean checkouts, archive boundary, and
  clean-install skill discovery; generate untracked evidence beneath
  `build/pi-acceptance/`.
- **`publish_pi_package_acceptance.py`** — Publish and exactly read back the
  actor-owned Athena `v0.4.0` acceptance comment on issue #2515.

### Pi end-to-end evidence

- **`pi_e2e_2519.py`** — Collect the live issue #2519 Pi/Codex evidence run,
  keep the private raw records under `build/pi-e2e-2519/<run-id>/`, and render
  the reproducible `docs/pi-e2e-2519-report.md` plus
  `docs/runbooks/pi-e2e-2519.md` artifacts.

The collector only performs GitHub readback. Commands passed to `capture` are
the normal planner and automation loop and retain their ordinary GitHub
mutation behavior. It enables their opt-in private receipt sink so provider
sessions, resolved policy scopes, and host-owned Athena results come from the
actual queue workers. Direct provider commands are rejected. See
`docs/runbooks/pi-e2e-2519.md` for the exact commands and evidence contract.

## Usage

```bash
# Pre-commit-checked validators
hephaestus-check-test-structure
python3 -m hephaestus.scripts_lib.check_version_single_source
python3 -m hephaestus.scripts_lib.check_cli_table_sync

# Symlink check
scripts/check-symlinks.sh
```

## Design Principles

Following AGENTS.md guidelines:

- **KISS** (Keep It Simple, Stupid) — Scripts are thin wrappers
- **DRY** (Don't Repeat Yourself) — Logic lives in `hephaestus.*` modules; the
  scripts here just expose CLI entry points or shell glue
- **YAGNI** (You Aren't Gonna Need It) — Only port what's reusable
- **Modularity** — Clear separation between CLI and core logic
