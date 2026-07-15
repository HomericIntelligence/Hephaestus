# Runbook: CI-Driver Stall

This runbook is inactive under the current fail-closed merge gate. The queue
pipeline does not arm, retry, rebase, or address an open PR from `merge_wait`.
It verifies auto-merge is disabled, records `strict_gate_unavailable`, and
preserves post-merge learning only for a PR that was already merged.

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

## Activation condition

Reactivate or expand this recovery procedure only when the maintained pipeline
sources implement a head-bound strict-review proof and
`docs/ci/required-checks.md` permits queue-owned arming. That change must update
this runbook and its index entry in the same PR.

## See Also

- [Automation loop crashed mid-issue](automation-loop-crash.md)
- PR and state-label policy: [`../ci/required-checks.md`](../ci/required-checks.md)
