# ADR-0024: Explicit operator authorization before queue merge

- Status: Accepted
- Date: 2026-08-08
- Tracks: #2368
- Supersedes in part: ADR-0016 merge-authorization boundary

## Context

The queue owns a SHA-conditional normal merge, while
`state:implementation-go` records automated implementation eligibility. The
label and the process-local reviewed-head proof do not establish that a human
operator authorized the irreversible merge. Native auto-merge arming is not a
valid successor: the queue must continue to leave native auto-merge untouched.

The authorization must survive a process restart without allowing an approval
for one repository, pull request, or commit to be reused elsewhere. It must
also fail closed when GitHub returns incomplete, unstable, or replayed review
data.

## Decision

Before each SHA-conditional merge request, `merge_wait` requires three
independent facts:

```text
state:implementation-go ─────► automated eligibility
current-process head proof ──► reviewed checkout identity
marked APPROVED review ──────► durable exact-head operator authorization
                               │
                               ▼
                    SHA-conditional squash merge
```

The durable artifact is a native GitHub `APPROVED` pull-request review whose
body is exactly:

```text
<!-- hephaestus-merge-authorization:v1 -->
```

The resolver binds the review to the repository-scoped pull request and its
`commit.oid`. Its immutable capability records the review node ID, nullable
`fullDatabaseId`, author login, normalized `WRITE` or `ADMIN` permission,
submission and update timestamps, edit metadata, and SHA-256 body digest.

Only a GitHub `User` with `viewerDidAuthor == false`, a login distinct from the
automation actor and not ending in `[bot]`, and current `write`, `maintain`,
or `admin` collaborator permission is trusted. `maintain` normalizes to
`WRITE`. This proves account provenance, not that an account is literally
operated by a person; repository administrators own that trust assumption and
must keep the automation identity separate.

The resolver is deterministic. Unmarked reviews are inert. Duplicate marked
review IDs in one snapshot, edited trusted current-head reviews, and malformed
trusted current-head metadata resolve to `REPLAYED`. Multiple valid active
trusted current-head approvals resolve to `AMBIGUOUS`; one resolves to
`AUTHORIZED`. In the absence of an active current approval, current dismissed
reviews resolve to `REVOKED`, trusted old-head reviews to `STALE`, current
untrusted reviews to `UNTRUSTED`, and no participating review to `ABSENT`.
Untrusted candidates never veto a valid trusted authorization.

Review traversal is repository-scoped, bounded to 100 pages and 10,000
reviews, and validates repository/PR identity, counts, cursors, and page
metadata. The complete snapshot is read twice and must be identical. Transport,
GraphQL, viewer-login, permission, pagination, or envelope failures become
`merge_authorization_unavailable`; they are not converted into an operator
correctable review state. Re-reading the same native review after restart is
normal durable recovery. The process-local reviewed-head proof is not durable,
so a restarted GO-labelled PR still needs fresh automated review before the
old exact-head authorization can be reused.

Authorization is resolved once after initial admission and before readiness,
then again after final live admission immediately before the PUT. The complete
immutable values must compare equal. The mutation adapter requires the final
capability and rejects repository, PR, or head mismatches before dry-run
logging or transport.

GitHub's merge endpoint supplies a head-SHA precondition but no review-ID or
review-state precondition. The final authorization read minimizes the window,
and the conditional PUT prevents a different head from being merged. A
dismissal racing after the final read cannot atomically cancel an in-flight
request; the authorization is therefore an issuance for an immutable head,
not a continuously revocable lease.

Existing GO-labelled PRs without an authorization block without changing the
label or issuing a merge or auto-merge mutation. If authorization validation
is defective, operators stop queue-driven merging and use the normal
branch-protected manual merge procedure. Rollback never restores label-only
automatic merging. PRs already in flight at rollout retain their labels but
must obtain the marked exact-head review before queue merging.

## Alternatives considered

- Treat the implementation-GO label as authorization. Rejected because it is
  an automated eligibility result and is not a human approval artifact.
- Arm native GitHub auto-merge after approval. Rejected because native
  auto-merge is outside queue ownership and has no exact authorization
  capability at the mutation boundary.
- Add a local signature or state store. Rejected because it would not prove
  GitHub provenance and would create a second durable authority.
- Require a continuously revocable review lease. Rejected because GitHub's
  merge API cannot atomically bind review state to the merge request.

## Consequences

Operators must submit the exact marked approval from a trusted identity and
rerun the loop after a head change. Restart recovery is safe: GitHub retains
the review, while the queue demands fresh process-local review proof. The
queue has explicit, auditable outcomes for absent, stale, ambiguous, replayed,
revoked, untrusted, unavailable, and changed authorization, and native
auto-merge remains unavailable to pipeline stages.
