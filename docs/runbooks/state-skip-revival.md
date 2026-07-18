# Reviving a `state:skip`-labeled issue

## When to use

An issue was intentionally or accidentally labeled `state:skip` (operator
audit, manual triage, or a prior automated run applying it on review-budget
exhaustion — `state_labels.py` `is_skipped`) and you now want automation to
resume planning/implementing it.

## Background

`state:skip` is absolute and operator-only (#1576/#1584): the pipeline's
seeding classifier (`pipeline/seeding.py:239` `classify_issue`) excludes any
`state:skip`-labeled issue from the work queue entirely, before any other
state label is consulted. This is a *point-in-time* exclusion — if
`state:skip` is applied to an issue *after* automation has already queued and
started work on it (planned, or opened a PR), automation does not retroactively
abort the in-flight work; the exclusion only prevents *future* passes from
re-entering it. This is expected, not a bug (see the #1835 timeline audit:
11/11 reported "skip-labeled implementations" had `state:skip` applied strictly
after PR creation, never before).

## Diagnose

Check when `state:skip` was applied relative to PR creation:

    gh api repos/<owner>/<repo>/issues/<N>/timeline --paginate \
      --jq '.[] | select(.event=="labeled" or .event=="cross-referenced") | {event, created_at, label: .label.name, source_pr: .source.issue.number}'

- `state:skip` timestamp AFTER the PR's cross-reference timestamp → expected
  race (case a); the PR is real, already-committed work.
- `state:skip` timestamp AT OR BEFORE PR creation → investigate as a possible
  gate bypass (case b); file/reopen an automation bug.

## Fix — reviving intentionally

1. Decide the fate of any PR the pipeline already opened for the issue
   (`gh pr list --search "linked:<N>"` or check the issue's timeline
   cross-references). Close it or let it proceed to review manually — the
   pipeline will not resume driving it while `state:skip` remains.
2. Remove the label:

       gh issue edit <N> --remove-label state:skip

3. Leave any existing `state:plan-go`/`state:implementation-go` label as-is —
   removing `state:skip` alone is sufficient; the pipeline's seeding
   classifier will re-admit the issue at its current rank on the next pass
   (`pipeline/seeding.py` `classify_issue` — no `state:needs-plan` reset
   required).
4. Confirm re-admission: the next automation loop pass should pick the issue
   back up at planning or implementation depending on its current `state:*`
   rank (`docs/AUTOMATION_LOOP_ARCHITECTURE.md` classification table).

## Fix — confirming a skip was correct

If the skip was intentional and should stick, no action is needed — leave
`state:skip` in place. Automation will continue to exclude the issue and will
log a `state:skip AND state:plan-go` (or `implementation-go`) warning on any
stage that still encounters the contradictory label pair
(`pipeline/stages/planning.py`, `pipeline/stages/implementation.py`) —
this is informational only and requires no operator response unless the
warning is unexpected.

## See also

- [Automation loop crashed mid-issue](automation-loop-crash.md)
- [Drive-green stall](ci-driver-stall.md)
