# Required status checks

This document is the single source of truth for **which CI checks block merges
to `main`** and how that gating is wired. Branch protection itself lives on the
GitHub side (out of git), so this doc plus the workflow definition are the
in-repo record of the contract.

## The contract

A PR can merge to `main` only when these branch-protection **required status
checks** are green:

| Required context | Source |
|------------------|--------|
| `required-checks-gate` | `.github/workflows/_required.yml` (aggregator) |
| `test (ubuntu-latest, 3.12, unit)` | `.github/workflows/test.yml` matrix |
| `test (ubuntu-latest, 3.12, integration)` | `.github/workflows/test.yml` matrix |

Classic branch protection uses **`strict: true`**, and every active
`required_status_checks` ruleset that applies to `main` must use
`strict_required_status_checks_policy: true`. Requiring the PR head to be
tested with current `main` prevents a stale PR from merging around a gate that
newer `main` would fail.

## Why a single aggregator gate

`_required.yml` defines ~19 jobs (`lint`, `pr-policy`, `unit-tests`,
`build`, the `security/*` scans, `license-scan`, etc.). Enumerating each one individually in
branch protection is brittle: renaming a job, adding a job, or splitting one
silently changes what's required, and nobody notices until something slips
through.

> This is exactly how `main` went red once: the `lint` job was **not** in the
> required list (only the two `test` contexts were), so a PR with red lint
> merged anyway. See issues #1313 / #1315.

Instead, a single job — **`required-checks-gate`** — `needs:` every gating job
and reports one aggregated result. Branch protection requires only that one
context. Adding or renaming a gating job requires **no GitHub-side change**:
just keep the gate's `needs:` list complete (enforced by a test — see below).

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
(it checks that auto-merge arming matches the `state:implementation-go` label)
and must not block merges.

## Adding a new gating job (runbook)

1. Add the job to `.github/workflows/_required.yml` as usual.
2. Add its job **key** to the `required-checks-gate` `needs:` list.
3. That's it — no branch-protection change is needed.

The guard test `tests/unit/ci/test_required_checks_gate.py` fails if a job is
added to `_required.yml` without being wired into the gate (excepting the
advisory `auto-merge-policy` and the gate itself), so step 2 cannot be silently
forgotten.

## (Re-)applying branch protection

GitHub can combine classic branch protection with repository and inherited
organization rulesets. Inspect both before changing either policy surface.

```bash
set -euo pipefail

repo=HomericIntelligence/ProjectHephaestus
branch=main
state_dir=/tmp/projecthephaestus-issue-2025
install -d -m 700 "$state_dir"

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
rules snapshot retains every ruleset check's optional `integration_id`. Abort
rather than rebuilding either array when the response shape or effective
contract is unexpected:

```bash
expected='[
  "required-checks-gate",
  "test (ubuntu-latest, 3.12, unit)",
  "test (ubuntu-latest, 3.12, integration)"
]'

jq -e --argjson expected "$expected" '
  ((.checks | type) == "array")
  and all(.checks[]; has("context") and has("app_id"))
  and (([.checks[].context] | sort) == ($expected | sort))
' "$state_dir/branch.before.json"

jq -e --argjson expected "$expected" '
  [.[] | select(.type == "required_status_checks")] as $status_rules
  | all(
      $status_rules[];
      .parameters.strict_required_status_checks_policy == true
    )
    and (
      (
        $expected
        + [$status_rules[].parameters.required_status_checks[].context]
        | unique
        | sort
      ) == ($expected | sort)
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
  -F strict=true \
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

jq -e '.strict == true' "$state_dir/branch.after.json"

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
