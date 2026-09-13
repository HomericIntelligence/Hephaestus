# ADR-0052: Verified default branch for merge admission

- Status: Accepted
- Date: 2026-09-13
- Tracks: #2837
- Supersedes: ADR-0016 branch-identity clause

## Context

ADR-0016 requires a final live admission before each server merge request. Its
branch-identity clause names `main`. GitHub repositories can use `master`,
`trunk`, or another default branch. A fixed branch name rejects a valid pull
request (PR) and cannot prove that an admitted base is the repository default.

The PR base and the repository default branch are mutable GitHub state. Local
checkout state, `origin/HEAD`, agent output, and labels do not prove this state.
The effective merge-policy read includes default-branch data, but it occurs
after initial admission and has a different policy purpose.

## Decision

Before initial merge admission, read `owner.login`, `name`, `nameWithOwner`,
and `defaultBranchRef.name` through the typed repository-scoped GraphQL
boundary. Require the response identity to equal the requested owner and
repository. Require a nonblank exact default-branch name. Do not use a local
branch source or a `main` fallback.

Bind the verified repository identity, default branch, PR base, and merge head
in one immutable admission snapshot. Require the PR base to equal the verified
default branch. Require the effective policy to use the same base and default
branch. Keep the open and unarmed PR, exclusive implementation-GO, exact-head,
review-thread, conversation-resolution, required-check, cancellation,
deadline, rebase-proof, and server merge-route gates.

After all policy, thread, readiness, and required-check reads, read the PR and
repository facts again as the final admission step before the server request.
Reject repository-identity, default-branch, PR-base, or head drift. Apply the
same comparison during uncertain-request reconciliation. A post-request read
does not authorize a second request.

If the repository metadata accessor fails, stop queue-driven merge requests.
Use the normal protected manual merge process until the accessor is repaired.
Do not restore a `main` fallback or use a state label as merge authority.

## Alternatives considered

- **Keep the literal `main` check.** Rejected: it excludes repositories that
  use another default-branch name.
- **Use `origin/HEAD` or checkout state.** Rejected: local state is not live
  repository metadata.
- **Use the PR base as its own authority.** Rejected: a release or stacked PR
  could satisfy that circular check.
- **Use the policy read as initial admission.** Rejected: it combines a later
  protection-policy purpose with the earlier repository-identity boundary.

## Consequences

Merge admission works with any verified default-branch spelling. It rejects a
non-default base and incomplete or changed repository metadata before a merge
request. Each request cycle adds two small repository metadata reads. The
GraphQL response validator and one pure snapshot validator keep identity and
branch checks consistent across the initial, final, and reconciliation paths.
