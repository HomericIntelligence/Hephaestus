# ADR-0013: Tiered backup and disaster-recovery policy

- Status: Accepted
- Date: 2026-07-18
- Tracks: #2154

## Context

Hephaestus had no defined, *tested* backup and disaster-recovery (DR) policy
for its operational state and dependencies (Section 9 MAJOR, #2154). A recovery
plan that is written but never exercised is aspirational: it drifts from the
code, and its first real execution is during an outage.

The operational state of a Hephaestus workstation is not homogeneous. Most of
what the automation pipeline depends on is either durable somewhere else or
cheaply recreatable, and only a small residue is local-only. Backing up
everything is both wasteful and unsafe (it would sweep credentials into
archives), while backing up nothing loses the residue that cannot be
re-derived. The policy therefore has to name each kind of state and say how it
is recovered.

Concretely, the only durable local operational state the pipeline writes is the
automation state directory `DEFAULT_STATE_DIR = "build/.issue_implementer"`
(`hephaestus/automation/models.py:151`, created by `ensure_state_dir` at
`hephaestus/automation/_review_utils.py:206-210`). It holds arming records
`drive-green-armed-<issue>.json` (`hephaestus/automation/arming_state.py:67-68`)
and CI-fix markers `last-ci-fix-<pr>.json`
(`hephaestus/automation/drive_green_state.py:36-37`) plus per-stage logs.
Everything else is durable upstream on GitHub (issues, `state:*` labels, PRs,
branches, signed tags — the automation loop already re-seeds from these, see
[`../runbooks/automation-loop-crash.md`](../runbooks/automation-loop-crash.md))
or recreatable from committed sources (`.venv` via `uv sync` from `uv.lock` per
[ADR-0008](0008-uv-only-development-environment.md); worktrees per
[`../runbooks/corrupted-worktree.md`](../runbooks/corrupted-worktree.md)).

## Decision

1. **Classify all operational state into three tiers**, and back up only the
   local-only tier:

   | Tier | State | Recovery |
   |------|-------|----------|
   | 1 — durable upstream | issues, `state:*` labels, PRs, branches, signed `vX.Y.Z` tags | Re-derive from GitHub; never backed up locally |
   | 2 — recreatable | `.venv`, pre-commit envs, `build/.worktrees/*`, coverage/lint caches | `uv sync` from committed `uv.lock` (ADR-0008); worktree runbook |
   | 3 — local-only (backup-required) | `build/.issue_implementer/` (arming records, ci-fix markers, stage logs) | `scripts/backup_state.py` archive + restore |

2. **Credentials and secrets are never archived.** GitHub auth (`gh auth`), the
   GPG signing key, and Claude CLI auth are inventoried in the runbook and
   re-provisioned on recovery — never written into a backup archive. This
   upholds the AGENTS.md secrets policy (no secrets in artifacts).

3. **The restore procedure is continuously tested in CI, not documented in
   prose.** `tests/unit/scripts/test_backup_state.py` executes a
   backup → destroy → restore round-trip and fail-closed tamper checks on every
   run, so the procedure cannot silently rot.

4. **DR tooling is stdlib-only and runs under bare `python3`.** The tool that
   recovers a broken environment must not itself depend on that environment, so
   `scripts/backup_state.py` imports no `hephaestus` module and needs no
   `uv sync`. Placing it in `hephaestus/automation/` would gate it behind the
   `[automation]` extra and pydantic — exactly the surface a DR tool must avoid.

5. **Recovery objectives.** RPO: state loss is bounded by re-derivation from
   GitHub for tiers 1–2; for tier-3 state, take one backup per operator-initiated
   risky operation (deleting `build/`, host migration, bulk state surgery). RTO:
   a full workstation rebuild completes in ≤ 1 hour by following
   [`../runbooks/backup-restore.md`](../runbooks/backup-restore.md).

## Alternatives considered

- **Back up the whole workstation (or all of `build/`).** Rejected: it would
  archive recreatable tier-2 state and risk sweeping credentials into archives,
  violating the secrets policy, for no recovery benefit over re-derivation.
- **Snapshot the queue / persist a durable queue state.** Rejected: the pipeline
  deliberately keeps no persisted queue snapshot and re-seeds from GitHub labels
  and PR/worktree state (see the automation-loop-crash runbook). A snapshot would
  be a second source of truth that can disagree with GitHub.
- **A prose-only DR runbook with no executed test.** Rejected: that is exactly
  the untested-restore gap #2154 flags; a runbook whose steps are never run in CI
  drifts from the code.
- **Put the tool in `hephaestus/automation/`.** Rejected: it would inherit the
  `[automation]` extra and pydantic, so it could not run in the broken
  environment it is meant to recover.

## Consequences

- Operators have a single stdlib backup/restore/verify CLI
  (`scripts/backup_state.py`) and a DR runbook
  ([`../runbooks/backup-restore.md`](../runbooks/backup-restore.md)) wired into
  the runbook index.
- The restore path is regression-guarded: a change that breaks backup or restore
  fails `tests/unit/scripts/test_backup_state.py` in CI.
- Backups default to `~/.hephaestus-backups/` (outside the repo) so a backup
  survives deletion of the workspace it protects.
- Restore is destructive-safe: it refuses a non-empty target without `--force`,
  verifies every member's SHA-256 digest before writing (fail-closed on
  mismatch), and rejects archive members whose path escapes the repo root.
- Tier-1/2 state and all credentials are explicitly out of scope for archival;
  recovering them is a documented, non-automated step.
