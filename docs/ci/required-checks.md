# Required status checks

This document records the CI contract and the last verified `main` protection
and ruleset configuration. GitHub policy can change outside git; audit live
state before relying on this record for a merge decision.

## CI is not automation-loop authorization

GitHub Actions validates repository code independently. Normal
`$athena:pr-review` may collect current check evidence and incorporate it into
its binary verdict, but the automation loop does not change checks, workflows,
statuses, artifacts, leases, or `pull_request_target` events. After the review
returns a current-head GO, the loop applies `state:implementation-go`;
`merge_wait` consumes that loop-owned label, while restarted labels re-enter
merge-wait. CI/CD never independently produces or validates authorization.

## Current required contexts

Classic branch protection requires:

| Required context | Source |
|------------------|--------|
| `required-checks-gate` | `.github/workflows/_required.yml` |
| `test (ubuntu-latest, 3.12, unit)` | `.github/workflows/test.yml` |
| `test (ubuntu-latest, 3.12, integration)` | `.github/workflows/test.yml` |

The active ruleset requires these direct contexts:

| Required context | Source |
|------------------|--------|
| `lint` | `.github/workflows/_required.yml` |
| `unit-tests` | `.github/workflows/_required.yml` |
| `integration-tests` | `.github/workflows/_required.yml` |
| `security/dependency-scan` | `.github/workflows/_required.yml` |
| `security/secrets-scan` | `.github/workflows/_required.yml` |
| `build` | `.github/workflows/_required.yml` |
| `schema-validation` | `.github/workflows/_required.yml` |
| `deps/version-sync` | `.github/workflows/_required.yml` |
| `pr-policy` | `.github/workflows/_required.yml` |

`auto-merge-policy` is advisory. It reports GitHub state but does not grant
automation authority and is not a required context.

## Aggregate workflow coverage

`required-checks-gate` depends on the code-validation jobs in
`_required.yml`, passing only when each needed job succeeds or is skipped. It
handles code events only; it must not gain label, review, or auto-merge event
triggers. The automation loop handles review labels and merge-state actions.

## Live audit

```bash
repo=HomericIntelligence/Hephaestus
branch=main
gh api "repos/$repo/branches/$branch/protection/required_status_checks"
gh api "repos/$repo/rulesets" --paginate
gh ruleset check --default --repo "$repo"
```

When changing required checks, first capture the complete live arrays and app
bindings. Modify only the named code-validation context, then read back the
arrays and prove every unrelated context and binding is unchanged. Do not add
the automation loop's label, a review result, or an internal loop artifact as
a GitHub Actions required check.
