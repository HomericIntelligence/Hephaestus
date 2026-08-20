# Runbook: Drive-Green Stall

Use this runbook when a PR carries loop-owned `state:implementation-go` and
remains blocked. The label is automated implementation eligibility. The current
queue verifies it with its current-process reviewed-head proof and one trusted,
unedited marked `APPROVED` operator review for the exact head before every
attempt. `merge_wait` may make
a bounded sequence (default: five) of individual ordinary SHA-conditional REST
squash-merge requests. Every request has fresh open-`main`, unarmed,
exclusive-label, and reviewed-head admission. Before a request it makes a
bounded, read-only operational readiness wait (15 minutes per fresh reviewed-head proof)
without spending a merge attempt. Readiness is not authorization: every actual
request repeats the label, head, thread, and protection gates. It does not
invoke `gh pr merge`, create, disable, adopt, or poll native auto-merge, manage
a merge queue, or use an administrator bypass.

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

Confirm the PR has `state:implementation-go` and inspect its current head. The
operator must use a human GitHub `User` identity distinct from the automation
actor and submit the exact marked approval for that head:

```bash
gh pr view <N> --json state,headRefOid,baseRefName,autoMergeRequest
gh pr review <N> \
  --approve \
  --body '<!-- hephaestus-merge-authorization:v1 -->'
uv run hephaestus-automation-loop --prs <N> --loops 1 --max-workers 1
```

For every marked approval, merge wait independently reads GitHub's native
`GET /repos/{owner}/{repo}/pulls/{number}/reviews/{review_id}` record and
requires its `commit_id` to equal the reviewed head (and to agree with the
review snapshot). A review cannot be transferred across a pushed head.

A head race makes the review stale rather than transferable. A direct run or
restart has no durable reviewed-head proof, so merge wait returns the PR to
fresh automated review without mutating labels; the durable marked approval
can be reused only after that proof is recreated for the same head. Absent,
stale, ambiguous, replayed, revoked, or untrusted authorization blocks without
a merge request. Unavailable or changed authorization reads fail closed.

The bounded sequence of individually admitted SHA-conditional normal REST
merge requests is driven by:

```bash
uv run hephaestus-automation-loop --prs <N> --loops 1 --max-workers 1
```

## Follow-Up

If the label is absent, do not attempt to merge: the loop must complete its
fresh GitHub snapshot plus clean-checkout `$athena:pr-review` path first.

For PRs already carrying implementation eligibility during rollout, no label
or merge mutation occurs until the separate exact-head authorization exists. If
authorization validation is unavailable or defective, stop queue-driven merging
and use the normal branch-protected manual merge process; do not restore
label-only merging or manage native auto-merge.

## See Also

- [Automation loop crashed mid-issue](automation-loop-crash.md)
- PR and state-label policy: [`../../AGENTS.md`](../../AGENTS.md)
