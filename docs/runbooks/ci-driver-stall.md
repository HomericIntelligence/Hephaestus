# Runbook: Drive-Green Stall

Use this runbook when a PR carries loop-owned `state:implementation-go` and
remains blocked. The current queue verifies the label only with its
current-process reviewed-head proof, then `merge_wait` stands by; it does not
create, disable, adopt, or poll auto-merge pending the separately reviewed
#2419 conditional merge path. CI/CD is outside the loop.

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
so merge wait safely revokes the stale label only after a confirmed-unarmed
read and returns the PR to review. A proof created during the current review
cycle reaches safe standby rather than an auto-merge mutation:

```bash
uv run hephaestus-automation-loop --prs <N> --loops 1 --max-workers 1
```

## Follow-Up

If the label is absent, do not attempt to merge: the loop must complete its
fresh GitHub snapshot plus clean-checkout `$athena:pr-review` path first.

## See Also

- [Automation loop crashed mid-issue](automation-loop-crash.md)
- PR and state-label policy: [`../../AGENTS.md`](../../AGENTS.md)
