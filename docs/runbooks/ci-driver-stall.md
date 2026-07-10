# Runbook: CI-Driver Stall (Green-but-BLOCKED PR)

This runbook is dormant during #2054's fail-closed bootstrap: no PR may remain
armed for auto-merge. If `autoMergeRequest` is present, disable it and verify
the result before continuing. After #2055 restores a head-bound strict-review
gate, use this runbook when an independently gated PR is armed, green, and
still un-mergeable. The queue pipeline handles that path in
`hephaestus/automation/pipeline/stages/merge_wait.py`.

## Symptom

- After #2055, `=== Pipeline summary ===` shows the affected issue
  cycling through or ending around `merge_wait`, `ci`, or `pr_review`, but the
  PR remains open and armed.
- Pipeline logs show one of the `merge_wait` routes:
  - `merge_wait:N: PR #M still BLOCKED after address budget; regressing`
  - `merge_wait:N: PR #M genuinely stuck after address budget; skipping`
  - `merge_wait:N: PR #M still OPEN after ...; timing out`
- `mergeStateStatus` for the PR is `BLOCKED` even though every check is green
  and auto-merge is armed.

## Root cause

The merge classifier treats `mergeStateStatus == "BLOCKED"` as a merge gate
that cannot be satisfied by waiting alone. The common cause is
**required-context drift**: an org ruleset requires a check context that the
repo's CI never emits, so the PR is `BLOCKED` permanently. A second cause is an
unresolved review thread when the ruleset requires
`required_review_thread_resolution`.

In the queue pipeline, `merge_wait` addresses blocked threads while the
`blocked_address` budget remains. After that budget is exhausted, genuinely
stuck PRs receive `state:skip`; other blocked PRs regress to `pr_review` via
`blocked_exhausted`.

## Diagnose

```bash
gh pr view <N> --json mergeStateStatus,statusCheckRollup,autoMergeRequest
```

Armed (`autoMergeRequest` present) + all checks green + `mergeStateStatus`
`BLOCKED` = a gating condition CI cannot satisfy on its own. Then inspect what
is actually required:

```bash
# Org/repo ruleset: which check contexts are REQUIRED?
gh api repos/{owner}/{repo}/rulesets
gh api repos/{owner}/{repo}/rulesets/{id}

# Any unresolved review threads holding the merge?
gh pr view <N> --json reviewDecision,reviewRequests
```

## Fix

1. **Required-context drift** — reconcile the ruleset's required check contexts
   with the contexts CI actually emits. Either remove the obsolete required
   context from the ruleset, or add a CI job that emits it. A push to re-run CI
   then re-queues the (now-present) required context.
2. **Unresolved threads** — if the ruleset requires
   `required_review_thread_resolution`, resolve the lingering (often bot)
   review threads, then the armed auto-merge proceeds on its own.

After the gate is satisfied, the already-armed PR merges without re-running the
driver. This behavior is suspended by #2054 and resumes only after #2055.

## See also

- [Automation loop crashed mid-issue](automation-loop-crash.md)
- PR / state-label policy: [`../../CLAUDE.md`](../../CLAUDE.md)
