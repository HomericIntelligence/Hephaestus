# ADR-0039: Policy-selected server merge route

- Status: Accepted
- Date: 2026-09-05
- Tracks: #2965
- Amended: 2026-09-06 for #3015
- Amended: 2026-09-07 for #3023
- Supersedes: ADR-0038

## Context

ADR-0038 added an exact-head CI gate before a direct REST merge. It did not
model the full server policy. A ruleset can require the merge queue. An actor
can also bypass strict-update protection. In these cases, a direct REST request
does not prove that GitHub tested the reviewed head with the latest base.

The queue must keep the reviewed-head proof, exact-head status gate, and
SHA-conditional request contract. It must also follow the server merge mode.
It must not create or change native auto-merge.

## Decision

`merge_wait` uses these rules:

1. The effective policy is the stable union of classic protection and all
   applicable active rulesets. It includes required checks, conversation
   resolution, current-actor bypass facts, source-bound strict-update facts,
   and the required merge-queue method.
2. Required status evidence selects required Check Runs by name before it
   validates their application and result fields. An optional Check Run with a
   schema-valid null application cannot revoke merge eligibility. Complete page
   and run-ID validation still applies to the global response.
3. When an applicable ruleset requires the merge queue, the stage sends one
   `enqueuePullRequest` GraphQL mutation. It supplies the PR node ID and
   `expectedHeadOid`. These mutation inputs make GitHub apply the exact-head
   condition. The receipt must contain a queue entry ID, a valid queue state,
   and the request correlation value. GitHub defines the nested PR and base
   commit fields as nullable. The receipt does not require these fields.
4. The queue route is valid for an actor that can bypass the ruleset and for an
   actor that cannot bypass it. The explicit queue request does not bypass the
   queue. GitHub tests the queued change with the latest base and the merge-group
   checks before it merges.
5. After queue admission, the stage records the admitted head in the current
   work item and polls lifecycle state. It does not replay the mutation. If
   GitHub returns the typed exact already-enqueued rejection, the adapter can
   run one read-only query within the remaining operation deadline. The query
   must prove the same open pull request node, exact reviewed head, and valid
   queue entry. A matching entry is successful idempotent admission. A
   canceled, late, unavailable, malformed, absent, or mismatched readback is
   fail-closed. All other uncertain mutation outcomes are terminal for the
   request.
6. When no merge queue applies, a direct REST merge is valid only if effective
   strict-update protection applies and the current actor cannot bypass it. The
   request keeps `sha=<reviewed head>` and `merge_method=squash`.
7. Both routes repeat the open PR, `main` base, absent `autoMergeRequest`,
   exclusive implementation-GO label, exact reviewed head, thread, status, and
   stable policy checks before the request. A base advance cannot remove the
   server requirement to test the latest base.
8. No route calls `gh pr merge`, changes native auto-merge, or uses an
   administrator bypass.

## Alternatives considered

- **Always use direct REST merge.** Rejected. It does not follow a required
  merge queue and is unsafe when the actor can bypass strict-update protection.
- **Always use the merge queue.** Rejected. A repository without merge-queue
  policy does not supply that server contract.
- **Use `gh pr merge`.** Rejected. The command can create native auto-merge when
  the PR is not ready.
- **Reject every bypass-capable actor.** Rejected. Exact-head queue admission
  explicitly selects the server queue and does not use the bypass.

## Consequences

The current repository topology has an executable path for the current bypass
actor and for a non-bypass actor: both use exact-head merge-queue admission.
A base advance before admission remains safe because GitHub tests the queue
entry with the latest base. A repository without a merge queue can use direct
merge only with strict-update protection from a source that the current actor
cannot bypass.
Incomplete policy, queue entry, or required status evidence stops the merge
path. A missing nullable hydration field does not change an accepted queue
entry into an unknown mutation result.
