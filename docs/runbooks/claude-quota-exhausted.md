# Runbook: Claude Quota Exhausted (429)

Use this when a stage stops making progress because the Claude API session
limit / quota was hit (HTTP 429). Grounded in
`hephaestus/automation/claude_invoke.py` and
`hephaestus/github/rate_limit.py`.

## Symptom

- A stage emits `Verdict: ERROR` — an **infrastructure-failure** sentinel,
  **not** a `NOGO`. `claude_invoke.py` reserves `"ERROR"` for
  reviewer-infrastructure failures and keeps it *deliberately distinct from*
  `"NOGO"` so the loop does not mistake "the reviewer never ran" for "the code
  is bad."
- Stderr/output carries `429` / quota / rate-limit phrasing, wrapped as a
  `ClaudeUsageCapError`.
- The issue is left **unlabeled and not skipped** — a quota cap is not a stuck
  work item, so the loop does not apply `state:skip` for it.

## ERROR ≠ NOGO

This is the single most important distinction: a `Verdict: ERROR` from a quota
cap means the reviewer/implementer **never got to run**, not that your change
failed review. Do not treat it as a `state:implementation-no-go`. There is
nothing to fix in the code — only the quota to wait out.

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
- [CI-driver stall (green-but-BLOCKED)](ci-driver-stall.md)
