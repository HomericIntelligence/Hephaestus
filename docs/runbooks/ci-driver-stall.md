# Runbook: Drive-Green Stall

Use this runbook when a PR carries loop-owned `state:implementation-go` and
remains blocked. The label is automated implementation eligibility. The current
queue verifies it with its current-process reviewed-head proof, no unresolved
review threads, and complete successful Check Runs for the exact head before
every attempt. `merge_wait` may make a bounded sequence (default: five) of
individual ordinary SHA-conditional REST squash-merge requests. Every request
has fresh open-`main`, unarmed, exclusive-label, and reviewed-head admission.
Before a request it makes a bounded, read-only operational readiness wait (15
minutes per fresh reviewed-head proof) without spending a merge attempt.
Readiness is not authorization: every actual request repeats the label, head,
thread, protection, and exact-head Check Runs gates. It does not invoke
`gh pr merge`, create, disable, adopt, or poll native auto-merge, manage a
merge queue, or use an administrator bypass.

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

Confirm the PR has `state:implementation-go` and inspect its current head:

```bash
gh pr view <N> --json state,headRefOid,baseRefName,autoMergeRequest
H1="$(gh pr view <N> --json headRefOid --jq '.headRefOid')"
REPOSITORY="$(gh repo view --json nameWithOwner --jq '.nameWithOwner')"
gh api "repos/${REPOSITORY}/commits/${H1}/check-runs?per_page=100" \
  --jq '{total_count, check_runs: [.check_runs[] | {name, head_sha, status, conclusion}]}'
uv run hephaestus-automation-loop --prs <N> --loops 1 --max-workers 1
```

The Check Runs request is scoped to `H1`. The queue also validates the
`head_sha` in every returned run, requires completed `success`, `neutral`, or
`skipped` conclusions, and rejects an empty or incomplete response. A second
user and a marked `APPROVED` review are not required. Do not substitute a later
head for `H1`: a concurrent push invalidates the current process proof and
routes the PR to fresh review.

A head race invalidates the reviewed-head proof. A direct run or restart has no
durable reviewed-head proof, so merge wait returns the PR to fresh automated
review without mutating labels. Missing, pending, failed, stale, or malformed
Check Runs stop the merge path without a merge request.

If unresolved threads remain, resolve them through the normal review process.
If an existing `autoMergeRequest` is present, treat it as external ownership
and do not change it. If the PR state, label read, thread read, protection
read, or Check Runs read is incomplete, stop and repair the source condition.

The bounded sequence of individually admitted SHA-conditional normal REST
merge requests is driven by:

```bash
uv run hephaestus-automation-loop --prs <N> --loops 1 --max-workers 1
```

## Follow-Up

If the label is absent, do not attempt to merge: the loop must complete its
fresh GitHub snapshot plus clean-checkout `$athena:pr-review` path first.

For PRs already carrying implementation eligibility during rollout, no label
or merge mutation occurs until the current-process reviewed-head proof and
green exact-head Check Runs exist. If either read is unavailable or defective,
stop queue-driven merging and use the normal branch-protected manual process;
do not restore label-only merging or manage native auto-merge.

## See Also

- [Automation loop crashed mid-issue](automation-loop-crash.md)
- PR and state-label policy: [`../../AGENTS.md`](../../AGENTS.md)
