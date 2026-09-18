# Runbook: Stale implementation label

Use this runbook when a pull request carries `state:implementation-go` but the
verdict bound to its current head is `NO-GO`.

`state:implementation-go` is loop-owned automated implementation eligibility. The
pipeline applies it exclusively, with a fresh readback, on its own write paths. A
verdict published *outside* the loop never reaches the label, so the label can
outlive the verdict that contradicts it.

The reconciliation pass reads the published review-exchange carrier and clears a
stale GO label when the verdict bound to the live head is `NO-GO`. It is one-way:
it can only remove eligibility. It never grants `state:implementation-go`.

## Confirm the state

Read the label set and the live head:

```bash
gh pr view <N> --repo OWNER/NAME --json headRefOid,labels
```

Read the published verdict carriers:

```bash
gh pr view <N> --repo OWNER/NAME --json reviews \
  --jq '.reviews[] | {oid: .commit.oid, submittedAt, body}'
```

A carrier is a `review-exchange:v1 kind=state` marker followed by a `json` block.
A verdict is usable only when the review's commit **and** the carrier's
`artifact_binding.revision` both equal the live head. An unbound, stale, or
ambiguous verdict is not usable, and the pass ignores it.

## Report first

Inspect without mutating:

```bash
uv run hephaestus-reconcile-implementation-labels --repo OWNER/NAME --dry-run
```

Scope the pass to one pull request, or sweep a whole org:

```bash
uv run hephaestus-reconcile-implementation-labels --repo OWNER/NAME --pr <N>
uv run hephaestus-reconcile-implementation-labels --org HomericIntelligence --dry-run
```

## Apply the correction

```bash
uv run hephaestus-reconcile-implementation-labels --repo OWNER/NAME --pr <N>
```

The pass performs one atomic label edit and then confirms the result with a fresh
exclusive readback. It aborts if the readback does not prove exactly
`state:implementation-no-go`. A lost proof means a concurrent actor may own the
state by then, so the pass does not attempt a compensating mutation.

## What this does not do

- It does not grant `state:implementation-go`. Only the reviewed-head GO proof in
  `pr_review` can do that.
- It does not resolve review threads, approve, merge, or re-review.
- It does not repair a `NO-GO` label that is missing. After a correction, the
  author addresses the required findings and requests a new review.

## See also

- [Queue merge stall](ci-driver-stall.md)
- [Queue architecture](../architecture.md)
- [PR and state-label policy](../../AGENTS.md)
