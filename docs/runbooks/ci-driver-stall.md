# Runbook: Drive-Green Stall

Use this runbook when a PR carries loop-owned `state:implementation-go` and
remains blocked. The label is automated implementation eligibility. The current
queue verifies it with its current-process reviewed-head proof, no unresolved
review threads, and complete passing required status evidence for the exact
head before every attempt. By default, `merge_wait` can make five
policy-selected server merge requests. Every request has fresh open-`main`,
unarmed, exclusive-label, and reviewed-head admission.
Before a request, it makes a bounded, read-only operational readiness wait
without spending a merge attempt. The `--poll-max-wait` option controls this
wait. Its default is 1,200 seconds (20 minutes) for each fresh reviewed-head
proof.
Readiness is not authorization: every actual request repeats the label, head,
thread, protection, and exact-head required-status gates. A required merge
queue uses exact-head GraphQL admission. Otherwise, direct REST merge requires
strict-update protection from a source that the current actor cannot bypass.
The loop does not invoke `gh pr merge`, change native auto-merge, or use an
administrator bypass.

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
  --paginate \
  --jq '{total_count, check_runs: [.check_runs[] | {name, head_sha, status, conclusion}]}'
gh api "repos/${REPOSITORY}/commits/${H1}/status?per_page=100" \
  --paginate \
  --jq '{sha, total_count, statuses: [.statuses[] | {id, context, state}]}'
uv run hephaestus-automation-loop --prs <N> --loops 1 --max-workers 1
```

Both requests are scoped to `H1`. The queue validates the `head_sha` in each
required Check Run and the top-level `sha` in each combined commit-status
response. A Check Run must be complete and have a `success`, `neutral`, or
`skipped` conclusion. A commit status must have the `success` state. A commit
status can satisfy only an unbound required context. A positive GitHub App
binding requires a Check Run from that exact application. If both sources use
the same required context, both sources must pass. The queue reads and compares
two complete paginated snapshots from each source. One source can have no
entries when the other source proves all requirements. An incomplete, changed,
or malformed snapshot fails. The combined evidence must prove all required
contexts. A second user and a marked `APPROVED` review are not required. Do not
substitute a later head for `H1`. A concurrent push invalidates the current
process proof and routes the PR to fresh review.

A head race invalidates the reviewed-head proof. A direct run or restart has no
durable reviewed-head proof, so merge wait returns the PR to fresh automated
review without mutating labels. Missing, pending, failed, stale, or malformed
required status evidence stops the merge path without a merge request.

If unresolved threads remain, resolve them through the normal review process.
If an existing `autoMergeRequest` is present, treat it as external ownership
and do not change it. If the PR state, label read, thread read, protection
read, or required status read is incomplete, stop and repair the source
condition. If the effective ruleset requires a merge queue, the loop submits
the PR with its exact reviewed head and then waits for server lifecycle state.
This route is valid for bypass and non-bypass actors because the request
explicitly joins the queue. If no merge queue applies, direct merge requires a
strict-update source that the current actor cannot bypass.

The bounded sequence of individually admitted server merge requests is driven
by:

```bash
uv run hephaestus-automation-loop --prs <N> --loops 1 --max-workers 1
```

## Follow-Up

If the label is absent, do not attempt to merge: the loop must complete its
fresh GitHub snapshot plus clean-checkout `$athena:pr-review` path first.

For PRs already carrying implementation eligibility during rollout, no label
or merge mutation occurs until the current-process reviewed-head proof and
passing exact-head required status evidence exists. If either read is
unavailable or defective, stop queue-driven merging and use the normal
branch-protected manual process; do not restore label-only merging or change
native auto-merge.

## Linux review bootstrap for PR #3006

An unsupported host-verification boundary has two recovery paths. Use the
supported macOS host for ordinary immutable review, or use the narrow
[ADR-0046 protocol](../adr/0046-review-host-verification-bootstrap.md) for
PR #3006. Other PRs cannot use this exception.

1. Review and merge #3007 on the supported macOS host from clean, signed
   `main`. Do not use the new exception to review its own implementation.
2. Install that merged revision on the Linux review host.
3. Have the PR #3006 owner remove the two #3035 coverage deltas against a base
   with the coverage repair. The observed 32-record head
   `c81cc334232abebd6621915627a3fc5ab5734361` is not eligible. Require the exact
   30-record map, with every row present and no extra operations or paths.
4. Obtain a fresh review and a separate authenticated operator grant for the
   new head and checkout-derived branch point. The grant must use the exact
   marker and closed raw JSON schema in ADR-0046. Do not copy an old grant or
   treat this runbook, an observed diff, or a CI result as approval.
5. From the Hephaestus checkout, select the existing grant comment ID:

   ```bash
   uv run hephaestus-drive-prs-green --prs 3006 \
     --host-verification-bootstrap-comment <COMMENT_ID>
   ```

The ID must be positive. Do not combine this option with `--issues`, discovery,
or additional PR values. The pipeline authenticates the current actor and
comment association. It reads the grant again before source review, GO, and
each merge request. A Linux skip remains failed verification evidence; it
cannot claim that the fixed commands passed. Normal review, required checks,
and protected merge admission remain necessary.

To disable use, omit the option. To revoke the grant, its authenticated owner
sets `state` to `revoked` or deletes the comment. If GO already exists, the
pipeline must guard the exact open, unarmed head, apply NO-GO, and confirm GO
is absent before returning without a merge request. If these reads fail,
preserve the PR and repair the evidence source. A restart discards the local
proof and requires fresh grant validation and review. Revoke the grant after
PR #3006 merges.

## See Also

- [Automation loop crashed mid-issue](automation-loop-crash.md)
- PR and state-label policy: [`../../AGENTS.md`](../../AGENTS.md)
