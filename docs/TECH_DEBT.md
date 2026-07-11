# Tech-Debt Tracking Convention

Tech debt in Hephaestus is tracked exclusively as GitHub issues
labelled `tech-debt`. We do not use undocumented "fix later" markers:
every `# TODO`, `# FIXME`, or `# HACK` comment in the source MUST reference
a tracking issue using the `# TODO(#N): explanation` form (for example,
`# TODO(#710): replace this dynamic test-seam with constructor injection.`).
Bare, unlinked markers are not allowed.

> Enforcement note: the `check-no-unlinked-todo` pre-commit hook
> (`hephaestus-check-unlinked-todo`, added in #1492) gates bare markers
> automatically across `hephaestus/` and `scripts/`. Reviewers additionally
> enforce the `# TODO(#N)` form in review.

## Filing

- Title: `[<SEVERITY>] <subsystem>: <one-line description>` where
  `<SEVERITY>` is one of `MAJOR`, `MINOR`, `NITPICK` (matches the
  `/hephaestus:repo-analyze-strict-full` audit output).
- Labels: `tech-debt` plus a dated audit label
  (e.g. `audit-2026-05-29`) when the item comes from a strict audit.
- Body: current behavior, desired behavior, blast radius, acceptance
  criteria, and (for audit children) the verification evidence proving
  the claim is still true against current `main`.

## Audit-driven debt

Findings from `/hephaestus:repo-analyze-strict-full` runs are filed as
child issues of a `[Tracker]` umbrella issue (see #708 for the
2026-05-29 audit, #381-style for prior audits). The tracker body
lists scorecard, findings, and verification-gate corrections.

## Triage cadence

- MAJOR: addressed before the next minor release.
- MINOR: addressed opportunistically when touching the affected module.
- NITPICK: addressed when convenient; eligible for `wontfix` if the
  fix breaks a public API with no in-repo beneficiary.

## Closing as `wontfix`

A child may be closed `wontfix` only when one of the following is true
and is quoted in the close comment:

1. The fix breaks a public `hephaestus.*` API and no caller in this
   repo would benefit.
2. The platform/OS prerequisite is explicitly documented as
   unsupported (see [`../COMPATIBILITY.md`](../COMPATIBILITY.md) at the
   repo root).
3. The cited code path has already been removed by an unrelated PR
   (verify with `git log -S<symbol>`).

## Audit history

| Audit label | Date | Tracker | Grade |
|-------------|------|---------|-------|
| `audit-2026-05-29` | 2026-05-29 | #708 (closed) | A- avg (C- → B+ planning corrected) |
