# ADR-0033: GraphQL response-error and mutation-proof contract

- Status: Accepted
- Date: 2026-08-10
- Tracks: #2393
- Amended: 2026-09-07 for #3023

## Context

Automation used several independent `gh api graphql` call sites. Their response
handling differed: some accepted partial envelopes, converted failures into
empty collections, retried mutations, or logged success before GitHub had
returned an identity-bound receipt. Those behaviors can create duplicate
comments and reviews, or silently lose review state.

## Decision

All automation GraphQL requests pass through
`hephaestus.automation.github_api.graphql.run_graphql`. A typed query or
mutation spec owns the operation document and its operation-specific validator.
The executor owns mutation variables that are security-sensitive, including a
fresh `clientMutationId`; mutation intents retain only target identifiers and
SHA-256 hashes of mutable content.

The executor performs one client attempt with the shared GitHub circuit breaker
and with rate-limit retry and throttling disabled for that attempt. It validates
the process status, JSON object envelope, `data` object, top-level errors, and
the exact operation payload. Query failures are classified as deterministic or
retryable. A mutation failure is outcome-unknown whenever dispatch or receipt
proof cannot be excluded. The sole mutation rejection that permits the
existing shadow-comment fallback is an exact `Body is not editable` response.

An exact merge-queue rejection permits one read-only reconciliation. The
executor raises `MergeQueueAlreadyEnqueuedError` only for an
`enqueuePullRequest` operation whose structured GraphQL response contains one
error. Its type must be `UNPROCESSABLE`, and its message must be
`Pull request is already in the queue`. Raw process text and responses with a
different operation, type, message, or additional error remain
outcome-unknown. After this typed rejection, the caller can read the queue
entry one time within the remaining operation deadline. It accepts the result
only for the same open pull request node and exact reviewed head. It does not
repeat the mutation.

All other outcome-unknown mutations are terminal for their intent: callers do
not replay them and do not issue compensating mutations. Only a proven
pre-dispatch failure may retry. Mutation success requires a correlation-bound
receipt, and reply or resolution success additionally requires the existing
exact-head and unchanged-conversation readback.

Journal-recovered implementation handoffs are reconciliation-only. They may
read marker-bound GitHub state to prove an earlier operation completed, but
they may not issue a mutation whose earlier dispatch cannot be excluded.

## Alternatives considered

- Keep per-call-site GraphQL parsing: rejected because response and mutation
  safety rules would continue to drift.
- Retry every transport error: rejected because a timeout or rate-limit response
  can follow a mutation that GitHub already applied.
- Treat a matching body or review state as mutation proof: rejected because
  those values are not correlation-bound and can describe an unrelated object.
- Add a second automation GraphQL client: rejected because it would bypass the
  shared circuit breaker and make the response contract unenforceable.

## Consequences

GraphQL failures now retain typed ownership and cannot masquerade as empty
state. Mutation callers must preserve intent, progress, receipts, and
readbacks, which makes the pipeline more explicit but prevents duplicate or
false-success review operations. The guarded package façade and AST tests make
new direct GraphQL execution sites fail during development. An authenticated,
read-only schema contract lane provides an opt-in check that the selected
fields still exist without executing a mutation.
