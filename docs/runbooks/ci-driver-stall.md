# Runbook: Drive-Green Stall

Use this runbook when a PR carries loop-owned `state:implementation-go` and
remains blocked. The current queue verifies the label only with its
current-process reviewed-head proof before every attempt. `merge_wait` may make
a bounded sequence (default: five) of individual ordinary SHA-conditional REST
squash-merge requests. Every request has fresh open-`main`, unarmed,
exclusive-label, and reviewed-head admission. Only classified retryable HTTP
405 readiness and unresolved transport ambiguity can timer-park another
request. It does not invoke `gh pr merge`, create, disable, adopt, or poll
native auto-merge, manage a merge queue, or use an administrator bypass. CI/CD
is outside the loop.

## Containment

If an open PR has `autoMergeRequest` present, treat it as externally owned and
inspect it without changing it:

```bash
gh pr view <N> --json state,autoMergeRequest
```

An unreadable or partial state is also blocking. The queue must not disable,
adopt, or re-arm an existing request; resolve ownership through the normal
maintainer process before rerunning automation. Do not enable auto-merge
manually as a substitute for the queue's review proof.

## Resolution

Confirm the PR has `state:implementation-go`, then rerun the bounded
drive-green scope. A direct run or restart has no durable reviewed-head proof,
so merge wait returns the PR to review without mutating labels. A proof
created during the current review cycle permits the bounded sequence of
individually admitted SHA-conditional normal REST merge requests rather than
an auto-merge mutation:

```bash
uv run hephaestus-automation-loop --prs <N> --loops 1 --max-workers 1
```

## Follow-Up

If the label is absent, do not attempt to merge: the loop must complete its
fresh GitHub snapshot plus clean-checkout `$athena:pr-review` path first.

## See Also

- [Automation loop crashed mid-issue](automation-loop-crash.md)
- PR and state-label policy: [`../../AGENTS.md`](../../AGENTS.md)
