# Runbook: CI-Driver Stall

Use this runbook when a PR has completed independent `strict_review`, has a
current-head authenticated GO proof, and remains blocked after CI is green.
`merge_wait` is the sole automatic armer; it revalidates the proof and current
head immediately before and after its conditional arm request.

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

Confirm the exact current head has both `state:implementation-go` and a
strict-GO artifact, then rerun the bounded drive-green scope. It will either
conditionally arm the reviewed head or safely return it to `strict_review`:

```bash
uv run hephaestus-automation-loop --prs <N> --phases drive-green --loops 1 --max-workers 1
```

## Follow-Up

If the proof is absent, stale, NOGO, or the head differs, do not attempt to arm:
the pipeline must obtain a new strict review for the live head.

## See Also

- [Automation loop crashed mid-issue](automation-loop-crash.md)
- PR and state-label policy: [`../../CLAUDE.md`](../../CLAUDE.md)
