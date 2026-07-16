# Required status checks

This document records the CI contract and the last verified `main` protection
and ruleset configuration. GitHub policy can change outside git; use the audit
commands below before relying on this record for an operational merge decision.

## The contract

A PR can merge to `main` only when every enforced classic branch-protection
context and direct ruleset **required status check** is green.

Classic branch protection currently requires:

| Required context | Source |
|------------------|--------|
| `required-checks-gate` | `.github/workflows/_required.yml` |
| `test (ubuntu-latest, 3.12, unit)` | `.github/workflows/test.yml` |
| `test (ubuntu-latest, 3.12, integration)` | `.github/workflows/test.yml` |

The active ruleset currently requires these direct contexts:

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

Branch protection uses **`strict: false`**, and every active
`required_status_checks` ruleset that applies to `main` must use
`strict_required_status_checks_policy: false`. The required checks (the
aggregator gate plus the two `test (...)` contexts) still gate every merge, but
the PR head is **not** required to be up to date with `main` before merging.

Requiring up-to-date (`strict: true`) forces every green PR to be rebased onto
the latest `main` before it can merge. With a fast-moving `main` (the automation
loop merges PRs continuously) this causes constant churn: mergeable PRs stall as
`BEHIND` and must be re-rebased every time `main` advances, often faster than
their CI can finish. The protection this bought — catching a semantic conflict
between two independently-green PRs — is rare and caught post-merge by CI on
`main`, so it does not justify the churn. `strict: false` lets a green PR merge
regardless of how far behind `main` it is.

## Aggregate workflow coverage

`_required.yml` defines ~19 jobs (`lint`, `pr-policy`, `unit-tests`,
`build`, the `security/*` scans, `license-scan`, etc.). Enumerating each one individually in
branch protection is brittle: renaming a job, adding a job, or splitting one
silently changes what's required, and nobody notices until something slips
through.

> This is exactly how `main` went red once: the `lint` job was **not** in the
> required list (only the two `test` contexts were), so a PR with red lint
> merged anyway. See issues #1313 / #1315.

`required-checks-gate` `needs:` every gating job and reports one aggregate
workflow result. It is a **classic branch-protection context**, not a direct
ruleset context. Keep its `needs:` list complete (enforced by a test — see
below): every included job participates in the classic protection gate. Make
an explicit GitHub-ruleset change only when a job also needs its own direct
ruleset context.

### How the gate works

```yaml
required-checks-gate:
  if: always()          # must run even when needed jobs skip
  needs: [ ...every gating job... ]
  steps:
    - # PASS when every needed job is `success` OR `skipped`;
      # FAIL on `failure` / `cancelled`.
```

`if: always()` is mandatory. Several heavy jobs gate on
`changes-gate.outputs.code_event` and **skip** on label / auto-merge PR events.
Without `always()` the gate would itself skip — reporting neither success nor
failure — and **deadlock** a required check. Treating `skipped` as acceptable
lets those legitimately-gated-off events pass while still failing on any real
job failure.

`auto-merge-policy` is deliberately **excluded** from the gate: it is advisory
reporting only. The queue's head-bound `strict_review` and `merge_wait` stages
are the sole automatic arming authority.

## Adding a new gating job (runbook)

1. Add the job to `.github/workflows/_required.yml` as usual.
2. Add its job **key** to the `required-checks-gate` `needs:` list.
3. Decide whether it also needs an independently visible direct ruleset context.
   If so, update the GitHub ruleset after auditing the existing bindings. The
   aggregate gate already makes all jobs in its `needs:` list block through
   classic branch protection.

The guard test `tests/unit/ci/test_required_checks_gate.py` fails if a job is
added to `_required.yml` without being wired into the gate (excepting the
advisory `auto-merge-policy` and the gate itself), so step 2 cannot be silently
forgotten.

## (Re-)applying branch protection

GitHub can combine classic branch protection with repository and inherited
organization rulesets. Inspect both before changing either policy surface.

```bash
set -euo pipefail

repo=HomericIntelligence/Hephaestus
branch=main
umask 077
state_dir=$(mktemp -d "${TMPDIR:-/tmp}/hephaestus-issue-2025.XXXXXX")

gh api \
  -H "Accept: application/vnd.github+json" \
  "repos/$repo/branches/$branch/protection/required_status_checks" \
  > "$state_dir/branch.before.json"

gh api --paginate --slurp \
  -H "Accept: application/vnd.github+json" \
  "repos/$repo/rulesets?includes_parents=true&targets=branch&per_page=100" \
  | jq 'add' > "$state_dir/rulesets.before.json"

gh api --paginate --slurp \
  -H "Accept: application/vnd.github+json" \
  "repos/$repo/rules/branches/$branch?per_page=100" \
  | jq 'add' > "$state_dir/rules.before.json"

gh ruleset check --default --repo "$repo"
```

The branch snapshot must expose every classic check's `app_id`. The applicable
rules snapshot retains every ruleset check's `integration_id`. The classic
surface requires the aggregate gate plus the two Python 3.12 matrix contexts;
the single applicable status-check ruleset requires the nine direct contexts
listed above. Abort rather than rebuilding either array when the response shape
or effective contract is unexpected:

```bash
expected_classic='[
  "required-checks-gate",
  "test (ubuntu-latest, 3.12, unit)",
  "test (ubuntu-latest, 3.12, integration)"
]'

expected_ruleset='[
  "lint",
  "unit-tests",
  "integration-tests",
  "security/dependency-scan",
  "security/secrets-scan",
  "build",
  "schema-validation",
  "deps/version-sync",
  "pr-policy"
]'

jq -e --argjson expected_classic "$expected_classic" '
  ((.checks | type) == "array")
  and all(.checks[]; has("context") and has("app_id"))
  and (([.checks[].context] | sort) == ($expected_classic | sort))
' "$state_dir/branch.before.json"

jq -e --argjson expected_ruleset "$expected_ruleset" '
  [.[] | select(.type == "required_status_checks")] as $status_rules
  | ($status_rules | length) == 1
    and ($status_rules[0].parameters.strict_required_status_checks_policy == false)
    and (($status_rules[0].parameters.required_status_checks | type) == "array")
    and all(
      $status_rules[0].parameters.required_status_checks[];
      has("context") and has("integration_id")
    )
    and (
      ($status_rules[0].parameters.required_status_checks | map(.context) | sort)
      == ($expected_ruleset | sort)
    )
' "$state_dir/rules.before.json"
```

Once both assertions pass, patch only the strict-mode field. Do not send
`contexts` or `checks`; omitting them preserves the existing context and
GitHub App bindings:

```bash
gh api -X PATCH \
  -H "Accept: application/vnd.github+json" \
  "repos/$repo/branches/$branch/protection/required_status_checks" \
  -F strict=false \
  > "$state_dir/branch.patch-response.json"
```

Read back every policy surface and prove that the repair changed no checks or
rulesets:

```bash
gh api \
  -H "Accept: application/vnd.github+json" \
  "repos/$repo/branches/$branch/protection/required_status_checks" \
  > "$state_dir/branch.after.json"

gh api --paginate --slurp \
  -H "Accept: application/vnd.github+json" \
  "repos/$repo/rulesets?includes_parents=true&targets=branch&per_page=100" \
  | jq 'add' > "$state_dir/rulesets.after.json"

gh api --paginate --slurp \
  -H "Accept: application/vnd.github+json" \
  "repos/$repo/rules/branches/$branch?per_page=100" \
  | jq 'add' > "$state_dir/rules.after.json"

jq -e '.strict == false' "$state_dir/branch.after.json"

jq -S '.checks | sort_by(.context)' \
  "$state_dir/branch.before.json" > "$state_dir/checks.before.json"
jq -S '.checks | sort_by(.context)' \
  "$state_dir/branch.after.json" > "$state_dir/checks.after.json"
cmp -s "$state_dir/checks.before.json" "$state_dir/checks.after.json"

jq -S 'sort_by(.ruleset_id, .type, .ruleset_source_type, .ruleset_source)' \
  "$state_dir/rules.before.json" > "$state_dir/applicable-rules.before.json"
jq -S 'sort_by(.ruleset_id, .type, .ruleset_source_type, .ruleset_source)' \
  "$state_dir/rules.after.json" > "$state_dir/applicable-rules.after.json"
cmp -s \
  "$state_dir/applicable-rules.before.json" \
  "$state_dir/applicable-rules.after.json"

jq -S 'sort_by(.id)' \
  "$state_dir/rulesets.before.json" > "$state_dir/rulesets-normalized.before.json"
jq -S 'sort_by(.id)' \
  "$state_dir/rulesets.after.json" > "$state_dir/rulesets-normalized.after.json"
cmp -s \
  "$state_dir/rulesets-normalized.before.json" \
  "$state_dir/rulesets-normalized.after.json"
```

Any failed assertion or comparison stops the operation. Do not weaken, replace,
or hand-reconstruct a check or inherited ruleset to make the command pass.

> **Order matters:** the `required-checks-gate` context only becomes selectable
> after the workflow containing it has run at least once on a commit. Land the
> workflow change first, let it report, then apply the protection patch.

## Related

- `docs/DEFINITION_OF_DONE.md` — the full per-PR checklist and what enforces
  each item.
- `tests/unit/ci/test_required_checks_gate.py` — the invariant guard test.
