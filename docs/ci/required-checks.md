# Required status checks

This document records the CI contract and the last verified `main` protection
and ruleset configuration. GitHub policy can change outside git; audit live
state before relying on this record for a merge decision.

## CI is not automation-loop authorization

GitHub Actions validates repository code independently. Normal
`$athena:pr-review` may collect current check evidence as audit context, but
its prose, grades, and decision-shaped output do not authorize the loop. The
automation loop does not change checks, workflows, statuses, artifacts, leases,
or `pull_request_target` events. `pr_review` applies
`state:implementation-go` only after a structural audit and fresh live GitHub
facts confirm the exact open, unarmed reviewed head, complete thread state, and
exclusive label transition by readback. `merge_wait` consumes that loop-owned
label with its process-local reviewed-head proof and conditionally merges only
that SHA; restarted labels re-enter review. CI/CD never independently produces
or validates authorization.

## Queue pre-PR source checks

Before publishing a Hephaestus implementation, the queue runs the fixed command
`env HEPHAESTUS_CI_REBUILD=1 bash scripts/run_ci_local.sh all`. Rebuilding the
CI image prevents a prior checkout's dependency environment from weakening the
gate. For linked implementation worktrees, the runner mounts the shared Git
metadata read-only so hatch-vcs and Git-aware checks operate on the candidate
commit. The entry point mirrors the locally
executable source-validation jobs in `_required.yml`, including lint, unit and
integration tests, installed-CLI tests, artifact lifecycle validation,
security scans, schema and version checks, license policy, shell checks, and
repository structure checks. A failure returns to the bounded implementation
test-fix loop instead of publishing a knowingly red branch.

This local pass cannot run checks whose inputs do not exist until GitHub creates
the PR. `pr-policy` still validates the live PR body, title, commit subjects,
and DCO trailers in Actions. The classic matrix contexts and
`required-checks-gate` also remain authoritative merge requirements. The local
run is early failure feedback only; it does not grant
`state:implementation-go` and does not replace GitHub's exact-head checks.

## Current required contexts

Classic branch protection requires:

| Required context | Source |
|------------------|--------|
| `required-checks-gate` | `.github/workflows/_required.yml` |
| `test (ubuntu-latest, 3.13, unit)` | `.github/workflows/test.yml` |
| `test (ubuntu-latest, 3.13, integration)` | `.github/workflows/test.yml` |

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

The active `homeric-main-baseline` branch ruleset also applies
`required_signatures` to `main`. Signature enforcement is repository policy,
not a duplicate GitHub Actions context.

## Maintenance

- **Owner:** The `.github/` owner in
  [CODEOWNERS](../../.github/CODEOWNERS).
- **Versioned sources:** Maintain this document from the `jobs` mappings in
  [`_required.yml`](../../.github/workflows/_required.yml) and
  [`test.yml`](../../.github/workflows/test.yml); the latter defines the classic
  matrix-test contexts listed above.
- **External source:** The live branch-protection and ruleset output collected
  by the commands under [Live audit](#live-audit).
- **Trigger:** Reconcile this document whenever either workflow's jobs, matrix,
  or context names change (including a `test.yml` test-job rename), whenever a
  branch-protection rule or ruleset changes, and during the pre-release review.

## Aggregate workflow coverage

`required-checks-gate` depends on the code-validation jobs in
`_required.yml`, passing only when each needed job succeeds or is skipped. It
handles code events only; it must not gain label, review, or auto-merge event
triggers. The automation loop handles review labels and merge-state actions.

The gate fans in the `_required.yml` code-validation jobs: `lint`, `pr-policy`,
`unit-tests`, `build`, the `security/*` scans (including `security/workflow-scan`,
the zizmor GitHub Actions SAST gate added for issue #2151), `license-scan`, and
more. Enumerating each one individually in branch protection is brittle: renaming
a job, adding a job, or splitting one silently changes what's required, and nobody
notices until something slips through.

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
