# Runbook: Drive-Green Stall

Use this runbook when a PR carries loop-owned `state:implementation-go` and
remains blocked. `merge_wait` is the sole automatic armer and conditionally
arms the current head; CI/CD is outside the loop.

## Containment

If an open PR has `autoMergeRequest` present, disable it and verify the
read-back before doing any other automation work:

```bash
gh pr view <N> --json state,autoMergeRequest
gh pr merge <N> --disable-auto
gh pr view <N> --json state,autoMergeRequest
```

An unreadable state, a failed disable request, or a read-back that remains armed
is a blocking failure. Do not enable auto-merge manually.

## Resolution

Confirm the PR has `state:implementation-go`, then rerun the bounded
drive-green scope. It will either conditionally arm the current head or return
it to the loop's PR-review pass:

```bash
uv run hephaestus-automation-loop --prs <N> --phases drive-green --loops 1 --max-workers 1
```

## Follow-Up

If the label is absent, do not attempt to arm: the loop must complete its
current-head `$athena:pr-review` path first.

## See Also

- [Automation loop crashed mid-issue](automation-loop-crash.md)
- PR and state-label policy: [`../../CLAUDE.md`](../../CLAUDE.md)
