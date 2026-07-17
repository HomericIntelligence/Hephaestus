# Required status checks

This document records the CI contract and the last verified `main` protection
and ruleset configuration. GitHub policy can change outside git; use the audit
commands below before relying on this record for an operational merge decision.

## The contract

A PR can merge to `main` only when every enforced classic branch-protection
context and direct ruleset **required status check** is green.

After the #2055 bootstrap workflow has landed and its context is registered,
classic branch protection requires:

| Required context | Source |
|------------------|--------|
| `required-checks-gate` | `.github/workflows/_required.yml` |
| `test (ubuntu-latest, 3.12, unit)` | `.github/workflows/test.yml` |
| `test (ubuntu-latest, 3.12, integration)` | `.github/workflows/test.yml` |
| `strict-review-proof` | `.github/workflows/strict-review-proof.yml` |

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

## Merge queue readiness and activation boundary

The two workflows that supply required contexts, `_required.yml` and
`test.yml`, handle `merge_group: checks_requested` in addition to their existing
`pull_request` and `push` events. The queue therefore evaluates the same three
classic contexts and nine direct ruleset contexts listed above against the
synthetic merge-group commit. Context names, workflow permissions, and release
triggers are unchanged.

`pr-policy` is the repository-specific exception: PR body and commit-policy
checks run on the source `pull_request`, whose payload contains the PR metadata.
The merge-group payload has no `pull_request` object, so the job posts its same
successful context through a merge-group-only step while the code, build,
security, schema, and test jobs validate the synthetic commit.

Odysseus is the sole activation authority. This repository PR must not mutate
live rulesets or branch protection. After this workflow change merges and
receives human workflow review, the Odysseus operator may stage the repository
ruleset's `merge_queue` rule with the approved policy: `SQUASH`, `ALLGREEN`,
maximum 10 queue builds, maximum 5 merged entries per group, minimum 1 entry,
a 5-minute minimum wait, and a 60-minute check timeout. Activation remains
incomplete until the issue records the live read-back and a representative
queued smoke result; issue #2243 stays open for those post-merge steps.

## Aggregate workflow coverage

`_required.yml` defines ~20 code-validation jobs (`lint`, `pr-policy`, `unit-tests`,
`build`, the `security/*` scans, `license-scan`, etc.). Enumerating each one individually in
branch protection is brittle: renaming a job, adding a job, or splitting one
silently changes what's required, and nobody notices until something slips
through.

> This is exactly how `main` went red once: the `lint` job was **not** in the
> required list (only the two `test` contexts were), so a PR with red lint
> merged anyway. See issues #1313 / #1315.

`required-checks-gate` `needs:` every code-validation job and reports one
aggregate workflow result. It is a **classic branch-protection context**, not a
direct ruleset context. Keep its `needs:` list complete (enforced by a test —
see below): every included job participates in the classic protection gate.
The separate authorization proof is also a classic context because it cannot
run in the PR-code workflow without letting candidate code control merge
authority.

### How the gate works

```yaml
required-checks-gate:
  if: always()          # must run even when needed jobs skip
  needs: [ ...every gating job... ]
  steps:
    - # PASS when every needed job is `success` OR `skipped`;
      # FAIL on `failure` / `cancelled`.
```

`if: always()` is mandatory so an upstream failure produces a durable aggregate
result. This workflow accepts only code events: label and auto-merge events
never create a second run whose skipped heavy jobs could replace an in-flight
or failed code-validation context for the same commit.

`auto-merge-policy` is deliberately **excluded** from the gate: it is advisory
reporting only. The queue's head-bound `strict_review` and `merge_wait` stages
are the sole automatic arming authority.

### Commit-bound strict-review proof

`strict-review-proof` is a separate, directly required
`pull_request_target` workflow, restricted to the `main` base branch. It runs on every relevant pull-request event,
including `synchronize` and the label event emitted when the queue publishes a
strict GO, but checks out the immutable **base** revision rather than PR code.
This prevents a candidate PR from changing the verifier that grants it merge
authority. The trusted job reads the event's immutable head SHA and requires
an authenticated, elected v2 GO artifact for that exact SHA. It also treats an
authenticated exact-head v1 or v2 NOGO as terminal denial and verifies the
live PR head did not move while it was validating. A `pull_request_target`
job's native Actions check belongs to the base SHA, so the trusted workflow
explicitly posts its `strict-review-proof` success or failure commit status to
the event head using its narrowly scoped `statuses: write` token.

This closes the auto-merge timing gap: a push creates a new commit-bound proof
status, which cannot inherit the previous SHA's successful proof. GitHub therefore
cannot auto-merge the new head until the strict reviewer publishes a fresh GO
and the check succeeds for that head. Coordinator polling remains only a
defense-in-depth containment control. The workflow must first land on `main`,
then a subsequent PR event must emit its context before an operator adds
`strict-review-proof` to classic branch protection; #2055 is a manual-squash
bootstrap under the temporary merge policy.

Before enabling automatic arming, configure the public repository Actions
variable to the login used by the automation credential. The check fails closed
when it is unset; do not substitute the event actor or an arbitrary comment
author.

```bash
gh variable set HEPHAESTUS_AUTOMATION_LOGIN --repo HomericIntelligence/Hephaestus --body 'mvillmow'
```

## Adding a new gating job (runbook)

1. Add a code-validation job to `.github/workflows/_required.yml` as usual.
2. Add its job **key** to the `required-checks-gate` `needs:` list.
3. Put a trusted authorization job in a separate base-controlled workflow and
   add its context to protection only after that workflow has run on `main`.
4. Decide whether any job also needs an independently visible direct ruleset
   context. Update the policy only after auditing the existing bindings.

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
rules snapshot retains every ruleset check's `integration_id`. After the
trusted proof workflow has landed and emitted a context, the classic surface
requires the aggregate gate, two Python 3.12 matrix contexts, and the strict
proof; the single applicable status-check ruleset requires the nine direct
contexts listed above. Abort rather than rebuilding either array when the
response shape or effective contract is unexpected:

```bash
expected_classic='[
  "required-checks-gate",
  "test (ubuntu-latest, 3.12, unit)",
  "test (ubuntu-latest, 3.12, integration)",
  "strict-review-proof"
]'

expected_bootstrap_classic='[
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

jq -e --argjson expected_classic "$expected_classic" \
  --argjson expected_bootstrap_classic "$expected_bootstrap_classic" '
  ((.checks | type) == "array")
  and all(.checks[]; has("context") and has("app_id"))
  and (
    ([.checks[].context] | sort) == ($expected_classic | sort)
    or ([.checks[].context] | sort) == ($expected_bootstrap_classic | sort)
  )
' "$state_dir/branch.before.json"

# The trusted workflow publishes its status with the GitHub Actions app. Reuse
# the app binding already recorded for an existing Actions-only required check;
# an absent, wildcard, or conflicting id is not safe for this enrollment.
trusted_actions_app_id=$(jq -er '
  [.checks[] | select(.context == "required-checks-gate") | .app_id]
  | unique
  | if length == 1 and .[0] != null and .[0] != -1 then .[0]
    else error("required-checks-gate must have one concrete GitHub Actions app_id")
    end
' "$state_dir/branch.before.json")

# In the normal (already enrolled) path, reject an unbound or differently
# bound proof context before any repair is attempted. The bootstrap inventory
# legitimately has no proof context yet.
jq -e --argjson expected_bootstrap_classic "$expected_bootstrap_classic" \
  --argjson trusted_actions_app_id "$trusted_actions_app_id" '
  ([.checks[].context] | sort) == ($expected_bootstrap_classic | sort)
  or (
    [.checks[] | select(.context == "strict-review-proof") | .app_id]
    == [$trusted_actions_app_id]
  )
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

### One-time strict-review-proof enrollment

The #2055 bootstrap PR cannot enforce a workflow that does not yet exist on
`main`; it must receive its independent strict review and be manually squash
merged. After that merge, let an ordinary PR trigger the trusted workflow and
confirm that it publishes the `strict-review-proof` commit status on that PR's
head. Only when the initial snapshot has the three-context
`expected_bootstrap_classic` inventory (rather than the four-context normal
inventory) perform this source-pinned, administrator-authorized enrollment.
It preserves the read-back check list and its bindings exactly, adding only the
new context with the verified GitHub Actions app id; it never changes the
ruleset:

```bash
jq -e --argjson expected_bootstrap_classic "$expected_bootstrap_classic" '
  ((.checks | type) == "array")
  and all(.checks[]; has("context") and has("app_id"))
  and (([.checks[].context] | sort) == ($expected_bootstrap_classic | sort))
' "$state_dir/branch.before.json"

enrollment_payload=$(jq --argjson trusted_actions_app_id "$trusted_actions_app_id" '
  {
    strict: .strict,
    checks: (
      .checks + [{context: "strict-review-proof", app_id: $trusted_actions_app_id}]
    )
  }
' "$state_dir/branch.before.json")

printf '%s\n' "$enrollment_payload" > "$state_dir/strict-proof-enrollment-payload.json"

gh api -X PATCH \
  -H "Accept: application/vnd.github+json" \
  "repos/$repo/branches/$branch/protection/required_status_checks" \
  --input "$state_dir/strict-proof-enrollment-payload.json" \
  > "$state_dir/strict-proof-enrollment.json"

gh api \
  -H "Accept: application/vnd.github+json" \
  "repos/$repo/branches/$branch/protection/required_status_checks" \
  > "$state_dir/branch.enrolled.json"

jq -e --argjson expected_classic "$expected_classic" \
  --argjson trusted_actions_app_id "$trusted_actions_app_id" '
  ((.checks | type) == "array")
  and all(.checks[]; has("context") and has("app_id"))
  and (([.checks[].context] | sort) == ($expected_classic | sort))
  and (
    [.checks[] | select(.context == "strict-review-proof") | .app_id]
    == [$trusted_actions_app_id]
  )
' "$state_dir/branch.enrolled.json"

jq -S '.checks | map(select(.context != "strict-review-proof")) | sort_by(.context)' \
  "$state_dir/branch.before.json" > "$state_dir/checks.pre-enrollment.json"
jq -S '.checks | map(select(.context != "strict-review-proof")) | sort_by(.context)' \
  "$state_dir/branch.enrolled.json" > "$state_dir/checks.post-enrollment.json"
cmp -s "$state_dir/checks.pre-enrollment.json" "$state_dir/checks.post-enrollment.json"

# Use the completed enrollment as the baseline for the normal strict-mode
# repair and its no-context-drift comparisons below.
cp "$state_dir/branch.enrolled.json" "$state_dir/branch.before.json"
```

The final comparison proves the update preserved every existing context and
app binding, while the preceding equality check pins the proof to the trusted
GitHub Actions app. If either condition fails, stop and escalate it as a
branch-protection policy decision.

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
