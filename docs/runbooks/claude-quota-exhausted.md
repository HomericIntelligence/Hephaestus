# Runbook: Claude Quota Exhausted (429)

Use this when a stage stops making progress because the Claude API session
limit / quota was hit (HTTP 429). Grounded in
`hephaestus/automation/claude_invoke.py` and
`hephaestus/github/rate_limit.py`.

## Symptom

- A stage reports an infrastructure failure and its stderr/output carries 429,
  quota, or rate-limit phrasing. This is not a review decision and cannot
  authorize or reject an implementation-state transition.
- The stage classifies the failure as a `ClaudeUsageCapError`.
- The issue is left **unlabeled and not skipped** — a quota cap is not a stuck
  work item, so the loop does not apply `state:skip` for it.

## A quota failure is not a review decision

A quota cap means the reviewer or implementer **never got to run**, not that
the change failed review. Do not apply `state:implementation-no-go` because of
the quota alone. There is nothing to fix in the code — only the quota to wait
out. Any later implementation-state transition still requires the normal
structural audit and fresh live GitHub head, thread, and label facts.

## Confirm quota was the cause

Look for `ClaudeUsageCapError` or 429 / rate-limit phrasing in the stage
output. When a reset time is present, `scan_quota_reset` (which delegates to
`resolve_quota_reset_epoch`) extracts the reset epoch from the error text; not
every 429 carries one.

## Do NOT delete sessions

The cap is enforced **API-side**, not session-side. Deleting Claude sessions
does not restore quota and only discards resumable context — leave sessions
intact.

## When to re-run

Wait for the Pacific-time session reset, then re-run the **same** invocation —
the issue was never mis-labeled, so no cleanup is needed:

```bash
# Check the current Pacific time to gauge the reset window:
TZ=America/Los_Angeles date

# After the reset, re-run the SAME issues — nothing else to clean up:
hephaestus-automation-loop --issues <N>
```

## See also

- [Automation loop crashed mid-issue](automation-loop-crash.md)
- [Drive-green stall](ci-driver-stall.md)
