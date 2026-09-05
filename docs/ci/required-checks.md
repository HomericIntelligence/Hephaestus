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
label with its process-local reviewed-head proof and one trusted, unedited
marked `APPROVED` GitHub review for that exact SHA; restarted labels re-enter
review because the process-local proof is not durable. CI/CD never creates
operator authorization or independently produces the native review artifact.

## Queue pre-PR source checks

Before publishing a Hephaestus implementation, the queue runs the fixed command
`bash scripts/run_ci_local.sh all --rebuild`. Rebuilding the
CI image prevents a prior checkout's dependency environment from weakening the
gate. Each invocation builds from an explicit allowlisted context, captures its
own immutable image ID, and runs every container step against that ID; parallel
workers therefore cannot retag one another's dependencies, and ignored local
credentials, Git metadata, and unrelated artifacts never enter the build
context. The command runs once after each implementation or test-fix turn and
immediately before commit, push, and PR creation. A passing run advances
directly to publication; a failing run returns the item to the implementer and
must pass on the next attempt before publication. For linked implementation
worktrees, the runner mounts the shared Git
metadata read-only so hatch-vcs and Git-aware checks operate on the candidate
commit. Before lint and secret scanning, it also builds an alternate Git index
and read-only candidate tree from `HEAD` plus every non-ignored working-tree
change. This includes untracked source bytes without mutating the implementer's
real index; pre-commit reads the alternate index, while Gitleaks scans both Git
history and that exact candidate tree. The entry point mirrors the locally
executable source-validation jobs in `_required.yml`, including lint, unit and
integration tests, installed-CLI tests, artifact lifecycle validation,
security scans, schema and version checks, license policy, shell checks, and
repository structure checks. A failure returns to the bounded implementation
test-fix loop instead of publishing a knowingly red branch.

For each platform, the shell reports an approved runner-initialization failure.
Approved failures are an absent engine, an unavailable engine, and a failed
no-op container-start probe. The shell exits with code 75 and writes one exact
terminal protocol record. The queue validates the record. On macOS, the queue
runs `uv run pytest tests -q --tb=short`. On other platforms, the queue stops
with `pre_pr_runner_unavailable`. A failure in native verification still
returns the item to the bounded test-fix loop.

The queue reads the runner and its sourced helper through no-follow directory
descriptors. It compares both files with the immutable implementation-source
tree. It executes anonymous snapshots of the verified bytes. A path rename,
symlink change, or candidate marker cannot grant native-fallback authority.

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

## Merge queue execution

Both workflow sources of required contexts, `_required.yml` and `test.yml`,
run on `merge_group: checks_requested`. GitHub therefore evaluates the full
required suite against each synthetic `gh-readonly-queue/...` commit, including
the aggregate `required-checks-gate`, every direct ruleset context, and the two
classic matrix-test contexts. A separate smoke workflow cannot replace those
required names and is not part of the queue contract. Because the live queue's
`HEADGREEN` grouping strategy evaluates the synthetic group head, the
merge-group `pr-policy` job binds the queue ref to the live GraphQL merge-queue
entry and exact synthetic group head, enumerates every source PR represented by
that head, and requires a successful GitHub-Actions-owned `pr-policy` check on
each PR's exact current head SHA. Both queue entries and check runs are fully
paginated and cardinality-checked. Missing, malformed, stale, pending, failed,
or incomplete source-PR evidence fails closed.

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

`required-checks-gate` depends on all code-validation jobs in `_required.yml`.
Each dependency must succeed on pull-request and merge-group events. On a push,
only `pr-policy` can skip because no pull request exists. A failed, cancelled,
missing, unknown, or other skipped dependency fails the aggregate. The gate
also rejects a result census that differs from its complete expected-job list.

The unit guard requires exact equality between the top-level workflow jobs,
the aggregate `needs` list, and the runtime expected-job list. It also binds
the single push-only skip to the `pr-policy` condition. When you change the job
graph, update the top-level job, aggregate `needs`, runtime census, applicable
heavy-job set, runtime fixtures, structural tests, and hosted event evidence in
one reviewed change. When you change a supported event, also update the skip
policy and documentation. Add a skip exception only when the job has an exact
event condition and the unit guard enforces it.

The workflow handles code events only. It must not gain label, review, or
auto-merge event triggers. The automation loop handles review labels and
merge-state actions.

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
