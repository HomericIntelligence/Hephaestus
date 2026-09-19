# ADR-0053: Focused local checks and CI-owned full validation

- Status: Partly superseded by ADR-0055 (manual rebase trigger only)
- Date: 2026-09-16
- Tracks: #3269

## Context

ADR-0051 made a complete normal local pytest run a prerequisite for manual PR
creation and later branch changes. This duplicates CI/CD work and requires
local tools and settings unrelated to the changed behavior. The maintainer
requires focused local checks and assigns full-suite execution to CI/CD.

## Decision

Supersede ADR-0051's complete local suite prerequisite. Before PR creation, run
focused checks for the affected behavior and every new or changed test. Use
locked pytest commands with explicit paths or node IDs. Clear the default fast
selection with `--override-ini="addopts="`. Confirm nonempty collection and
success. Documentation-only changes use applicable existing documentation
checks; they do not need artificial tests of prose wording.

Keep the canonical upstream fetch, signed final rebase, commit-signature checks,
and clean-head recording. Run the focused checks on the final source. Record
each command, source revision, result, and summary. After a branch change,
repeat this sequence and the focused checks for the resulting change. Keep
test-only delegation for agent-run verification and retain installed hooks.

ADR-0049 continues to define the schedule: required PR checks and pre-commit
use the shared fast selection; nightly CI/CD owns full suites, coverage, and
the separate package, shell, artifact, and Pi lanes. Required PR results must
identify the applicable head. A fast PR result is not full-suite evidence.
Do not require a complete local suite before PR creation or merge.

Source review is separate from test execution. The reviewer inspects source
and recorded evidence. Reviewer-run tests and a local review image are not
prerequisites. CI/CD evidence does not replace source review or create review
authority. Preserve review-thread resolution, exact-head checks, signatures,
and the policy-selected protected merge route.

This decision does not change CI workflows, coverage floors, test selections,
or branch protection. It does not alter the product's fixed `BuildTestJob`
commands or isolation contracts. The automation rebase policy remains ADR-0048.

## Alternatives considered

Keep the full local prerequisite: rejected because it duplicates CI/CD and
makes local host capabilities a gate for unrelated changes.

Remove all local checks: rejected because focused checks detect collection
errors and regressions in changed behavior before publication.

Run full suites on every PR: not selected. ADR-0049's fast PR and nightly
schedule remains in effect. A schedule change needs a separate decision.

## Consequences

Local verification is proportional to the change. CI/CD supplies full-suite
and coverage results. Nightly validation can find failures after merge; keep
the existing failure records and follow-up process. Never report a full-suite
pass from focused checks or reuse evidence from a different source revision.

The contribution guides and Definition of Done state one current policy.
ADR-0051 remains as history. Rollback requires an explicit policy decision;
this change introduces no runtime migration.
