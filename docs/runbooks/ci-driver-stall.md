# Runbook: CI-Driver Stall

This runbook is inactive during the #2054 fail-closed bootstrap. The queue
pipeline does not arm, retry, rebase, or address an open PR from `merge_wait`.
It verifies auto-merge is disabled, records `strict_gate_unavailable`, and
preserves post-merge learn only for a PR that was already merged.

## Containment

If an open PR has `autoMergeRequest` present, disable it and verify the
read-back before doing any other automation work:

```bash
gh pr view <N> --json state,autoMergeRequest
gh pr merge <N> --disable-auto
gh pr view <N> --json state,autoMergeRequest
```

An unreadable state, a failed disable request, or a read-back that remains armed
is a blocking failure. Do not retry the legacy CI-driver recovery flow and do
not enable auto-merge manually.

## Resolution

Obtain an unconditional independent strict-review GO and green required checks.
A maintainer may then perform a manual squash merge:

```bash
gh pr merge <N> --squash
```

## Follow-Up

Issue #2055 will add the head-bound strict-review proof and its single-authority
merge gate. It must provide a new runbook for any strict-gated recovery behavior
before this runbook is expanded again.

## See Also

- [Automation loop crashed mid-issue](automation-loop-crash.md)
- PR and state-label policy: [`../../CLAUDE.md`](../../CLAUDE.md)
