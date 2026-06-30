# Runbook: CI-Driver Stall (Green-but-BLOCKED PR)

Use this when the CI driver keeps exiting cleanly each loop but PRs never
merge — they sit armed for auto-merge with all checks green, yet stay
un-mergeable. Grounded in `hephaestus/automation/ci_driver.py`.

## Symptom

- The drive-green stage exits `0` every loop, but open PRs pile up
  un-mergeable.
- The log shows (from `ci_driver.py`):

  > Issue #N: PR #M is BLOCKED by branch protection (unresolved conversations
  > or required review) — cannot auto-merge; leaving armed and exiting poll
  > early

- `mergeStateStatus` for the PR is `BLOCKED` even though every check is green
  and auto-merge is armed.

## Root cause

The driver treats `mergeStateStatus == "BLOCKED"` as failing-to-merge. The
common cause is **required-context drift**: an org ruleset requires a check
context that the repo's CI never emits, so the PR is `BLOCKED` permanently.
A second cause is an unresolved review thread when the ruleset requires
`required_review_thread_resolution`. The driver attempts to address
green-but-BLOCKED PRs at most `_BLOCKED_ADDRESS_MAX_ATTEMPTS = 2` times, then
**leaves the PR armed** and exits the poll early — it does not loop forever, so
the loop's clean exit hides the stall.

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
driver; re-run `hephaestus-automation-loop` only if you need to drive other
issues.

## See also

- [Automation loop crashed mid-issue](automation-loop-crash.md)
- PR / state-label policy: [`../../CLAUDE.md`](../../CLAUDE.md)
