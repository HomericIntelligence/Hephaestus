# ADR-0047: Verified repository default branch for conditional merge admission

- Status: Accepted
- Date: 2026-09-02
- Tracks: #2837
- Clarifies: ADR-0014

## Context

ADR-0014 defines the conditional normal merge contract. Its admission text used
the name `main` for the PR base. GitHub repositories can use another default
branch name. A fixed name can reject a valid PR before the other merge gates
run.

The repository default branch is mutable GitHub state. Local checkout state,
`origin/HEAD`, and agent output do not prove the current repository default
branch. The merge contract also needs a fresh check before the conditional
request so a default-branch or PR-base change cannot pass an old admission.

## Decision

1. `merge_wait` resolves the default branch from one exact repository-scoped
   GitHub REST response at `repos/{owner}/{repo}`. The response must be a JSON
   object with a non-empty, exact `default_branch` string. Missing, malformed,
   unreadable, or failed metadata is a terminal admission failure. The code
   does not use local checkout state or fall back to `main`.
2. Worker admission uses one shared validation predicate for its initial and
   final reads. A PR is eligible only when its live `baseRefName`
   exactly equals the verified repository default branch. All existing
   open/unarmed, exclusive implementation-GO, reviewed-head,
   conversation-resolution, operator-authorization, readiness, and
   SHA-conditional squash-merge gates remain in force.
3. The active worker binds the initial verified repository default branch and
   PR base with the reviewed head for the admission cycle. It reads the
   repository default branch and PR base again immediately before the
   conditional request. A change in the default branch or PR base ends the
   cycle without a merge request.
4. Conversation-resolution protection receives the exact verified base branch.
   The branch-protection check therefore remains branch-specific and
   fail-closed.

## Alternatives considered

- **Keep the literal `main` check.** Rejected: valid repositories with a
  different default branch cannot reach merge admission.
- **Use local `origin/HEAD` or checkout state.** Rejected: local state is not
  authoritative for the live repository and can be stale or absent.
- **Use the PR base or fall back to `main` when metadata is unavailable.**
  Rejected: either choice guesses merge authority when the repository default
  branch is not verified.

## Consequences

Merge admission supports any valid repository default branch and rejects
release, stacked, and other non-default bases. A repository metadata failure
now stops admission before label or merge mutation. The active worker performs
one additional repository metadata read before the conditional request and
rejects default-branch or PR-base drift. The shared predicate keeps the initial
and final admission checks aligned.
