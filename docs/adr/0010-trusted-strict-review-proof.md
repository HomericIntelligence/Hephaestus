# ADR-0010: Trusted strict-review proof context

- Status: Accepted
- Date: 2026-07-16
- Tracks: #2055
- Extends: ADR-0007

## Context

The strict-review verdict is merge authorization. A verifier executed from the
candidate pull-request checkout can be changed by that candidate, so a
head-bound check in the ordinary `pull_request` workflow cannot independently
attest the verdict. Label-triggered runs of the code-validation workflow also
create skipped heavy-job contexts, which can mask an in-flight or failed run.

## Decision

1. `strict-review-proof` lives in its own `pull_request_target` workflow,
   limited to the protected `main` base branch. It has `contents: read`,
   `pull-requests: read`, and the narrowly-scoped `statuses: write` needed to
   publish the authenticated proof.
2. It checks out `github.event.pull_request.base.sha` and runs only the trusted
   base revision's verifier while binding the result to the event head SHA.
   It explicitly publishes the result as the `strict-review-proof` commit
   status on that event head because its native Actions check belongs to the
   base SHA.
3. `_required.yml` accepts only code events, so its aggregate context cannot be
   replaced by label-event skipped jobs.
4. After the trusted workflow lands on `main`, let a subsequent PR event emit
   its status context, then add `strict-review-proof` to classic branch
   protection. The bootstrap PR remains an independently reviewed,
   manual-squash merge under #2054.

## Alternatives considered

- Run the verifier from the candidate checkout in `_required.yml`. Rejected:
  the candidate can alter the verifier that grants its own merge authority.
- Keep label events in `_required.yml` and distinguish the skipped jobs in the
  aggregate. Rejected: a separate proof context has a clearer authority
  boundary and cannot cancel or replace code-validation runs.
- Add the context before the workflow is on `main`. Rejected: GitHub cannot
  establish the bootstrap context from this candidate safely; the bootstrap
  remains a manual-squash exception until the trusted workflow is available.

## Consequences

- The proof is a direct classic branch-protection context rather than an
  aggregate dependency.
- GitHub-side protection changes remain explicit operations, audited against
  the exact context inventory in `docs/ci/required-checks.md`.
- The proof workflow never checks out or runs candidate PR code.
- The post-bootstrap enrollment records the returned publisher binding and
  preserves all existing classic-check bindings; an unexpected binding aborts
  the operation for an explicit policy decision.
