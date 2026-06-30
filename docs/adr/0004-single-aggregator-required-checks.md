# ADR-0004: Single aggregated required-checks gate

- Status: Accepted
- Date: 2026-06-30
- Tracks: #1452

## Context

GitHub branch-protection required status checks live outside the git tree, in
repository settings. When branch protection enumerates each CI job by name, the
list drifts silently: adding or renaming a job, or changing the test matrix,
leaves protection referencing contexts the workflow no longer emits — PRs then
sit green-but-BLOCKED (or, worse, merge with a gating job no longer required).

## Decision

Use a **single aggregator** required status check instead of enumerating every
job:

1. One job, `required-checks-gate`
   (`.github/workflows/_required.yml:924`), fans in every gating job via its
   `needs:` list. It PASSES when every needed job is `success` or `skipped`
   (skipped = legitimately gated off) and FAILS on `failure`/`cancelled`.
2. Branch protection requires only that single context (alongside the two
   `test (...)` contexts from `test.yml`), with `strict: true`.
3. `if: always()` is mandatory so the gate reports a definite
   success/failure even when heavy jobs skip on label / auto-merge events.

`docs/ci/required-checks.md` is the operational source of truth for the gate;
this ADR records the *why*. The membership of the `needs:` list is itself
guarded by `tests/unit/ci/test_required_checks_gate.py`.

## Alternatives considered

- **List every job as a required context.** Rejected: brittle — every matrix
  change or new gating job requires a GitHub-side branch-protection edit, and
  forgetting one causes the silent required-context drift this ADR exists to
  prevent.
- **No required checks (trust auto-merge).** Rejected: provides no
  branch-protection floor and lets a red gating job merge.

## Consequences

- Adding a new gating job means adding it to the `required-checks-gate`
  `needs:` list — protection keeps working with no GitHub-side change.
- The advisory `auto-merge-policy` job is intentionally **excluded** from the
  `needs:` list; it must not gate merges.
- The gate's job list is kept honest by
  `tests/unit/ci/test_required_checks_gate.py`.
