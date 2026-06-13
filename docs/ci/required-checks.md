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

Branch protection also has **`strict: true`** (a branch must be up to date with
`main` before it can merge), which prevents a stale PR from merging around a
gate that newer `main` would fail.

## Why a single aggregator gate

`_required.yml` defines ~18 jobs (`lint`, `pr-policy`, `unit-tests`,
`build`, the `security/*` scans, etc.). Enumerating each one individually in
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

Branch protection is stored on GitHub, not in git. To apply or restore the
required-checks contract above, an admin runs:

```bash
gh api -X PUT \
  repos/HomericIntelligence/ProjectHephaestus/branches/main/protection/required_status_checks \
  -f strict=true \
  -f 'checks[][context]=required-checks-gate' \
  -f 'checks[][context]=test (ubuntu-latest, 3.12, unit)' \
  -f 'checks[][context]=test (ubuntu-latest, 3.12, integration)'
```

Verify with:

```bash
gh api repos/HomericIntelligence/ProjectHephaestus/branches/main/protection/required_status_checks
```

Expect `strict: true` and exactly the three contexts above.

> **Order matters:** the `required-checks-gate` context only becomes selectable
> after the workflow containing it has run at least once on a commit. Land the
> workflow change first, let it report, then apply the protection PUT.

## Related

- `docs/DEFINITION_OF_DONE.md` — the full per-PR checklist and what enforces
  each item.
- `tests/unit/ci/test_required_checks_gate.py` — the invariant guard test.
