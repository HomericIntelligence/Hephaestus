# Runbook: Backup and disaster recovery

Use this to back up or restore Hephaestus tier-3 operational state
(`build/.issue_implementer`), or to rebuild a lost workstation end-to-end. The
recovery **policy** — what is durable, recreatable, or backup-required — is
[ADR-0013](../adr/0013-backup-and-disaster-recovery-policy.md); this runbook is
the operator procedure that executes it.

## Scope

Not all operational state is equal. Per ADR-0013, state is classified into three
tiers, and only tier 3 is archived:

| Tier | State | Recovery |
|------|-------|----------|
| 1 — durable upstream | issues, `state:*` labels, PRs, branches, signed `vX.Y.Z` tags | Re-derive from GitHub; never backed up locally |
| 2 — recreatable | `.venv`, pre-commit envs, `build/.worktrees/*`, coverage/lint caches | `uv sync` from committed `uv.lock` (ADR-0008); [corrupted-worktree runbook](corrupted-worktree.md) |
| 3 — local-only (backup-required) | `build/.issue_implementer/` (arming records, ci-fix markers, stage logs) | `scripts/backup_state.py` archive + restore |

The backup tool is stdlib-only and runs under bare `python3` (no `uv sync`
needed), because a DR tool must work in the broken environment it recovers.

## Taking a backup

```bash
python3 scripts/backup_state.py backup
# → Wrote backup: ~/.hephaestus-backups/hephaestus-state-<UTC-timestamp>.tar.gz
```

Backups default to `~/.hephaestus-backups/` — outside the repo, so the backup
survives deletion of the `build/` workspace it protects. Override with
`--output <dir>`.

Take a backup before any operator-initiated risky operation:

- before deleting `build/` or the whole checkout,
- before migrating to a new host,
- before bulk state surgery (e.g. the label edits in
  [state-skip-revival.md](state-skip-revival.md)).

## Restoring state

```bash
python3 scripts/backup_state.py restore ~/.hephaestus-backups/hephaestus-state-<ts>.tar.gz --force
```

Restore is fail-closed:

- Every member's SHA-256 digest is verified against the archive manifest
  **before** anything is written; a single mismatch aborts the restore with
  nothing written (fail-closed on digest mismatch).
- A non-empty target is refused unless you pass `--force`, so a restore never
  silently clobbers existing state.
- Archive members whose path escapes the repo root are rejected (path-traversal
  guard).

Exit codes: `0` success, `1` verify failure, `2` usage error or a refused
overwrite.

## Full workstation loss

Rebuild in this order (target RTO ≤ 1 hour, per ADR-0013):

1. `git clone` the repository.
2. `just bootstrap` — recreates tier-2 state (`.venv` from `uv.lock` per
   ADR-0008, pre-commit hooks).
3. `gh auth login` — restore GitHub credentials.
4. Import the GPG signing key and set `git config user.signingkey` — required
   for signed commits (see [`../../CLAUDE.md`](../../CLAUDE.md)).
5. Re-authenticate the Claude CLI.
6. Restore tier-3 state from your latest archive with `restore --force`.
7. Resume in-flight issue work via the
   [automation-loop-crash runbook](automation-loop-crash.md) (the loop re-seeds
   tier-1 state from GitHub labels and PR/worktree state).

## Verification drill

Run a read-only integrity drill against the latest archive at any time:

```bash
python3 scripts/backup_state.py verify ~/.hephaestus-backups/hephaestus-state-<ts>.tar.gz
```

`verify` extracts to a temp directory, recomputes every member digest, prints
per-member `PASS`/`FAIL`, and returns `0`/`1` — it never mutates the repo. The
same backup → destroy → restore round-trip and tamper checks run in CI via
`tests/unit/scripts/test_backup_state.py`, so the restore procedure is
continuously tested, not merely documented.

## What is never backed up

Credentials and secrets are **never** archived — GitHub auth (`gh auth`), the
GPG signing key, and Claude CLI auth are inventoried above and re-provisioned on
recovery, upholding the CLAUDE.md secrets policy. Tier-1 state (GitHub) and
tier-2 state (recreatable via `uv.lock`) are likewise out of scope for archival;
they are re-derived, not restored.

## See also

- Policy: [ADR-0013](../adr/0013-backup-and-disaster-recovery-policy.md)
- [Automation loop crashed mid-issue](automation-loop-crash.md)
- [Recover a corrupted worktree state](corrupted-worktree.md)
