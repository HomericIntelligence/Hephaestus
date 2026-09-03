# ADR-0034: Event-aware required-check results

- Status: Accepted
- Date: 2026-09-03
- Tracks: #2891
- Supersedes: The result policy in ADR-0004

## Context

ADR-0004 introduced the `required-checks-gate` aggregate. It accepted each
`skipped` dependency because the workflow then used events that could skip
validation jobs. ADR-0007 superseded the repository-policy parts of ADR-0004
and kept the aggregate. The workflow now uses only `pull_request`, `push`, and
`merge_group` events. The old broad skip rule can hide a required job that did
not run.

## Decision

Use an event-aware, fail-closed result policy for `required-checks-gate`:

1. Require `success` from each dependency by default.
2. Permit only `pr-policy=skipped`, and only on `push`. A push event has no pull
   request for `pr-policy` to inspect.
3. Require `success` from every dependency on `pull_request` and `merge_group`.
4. Reject unsupported events. Add an event only with an explicit reviewed
   result policy.
5. Keep an exact runtime job census. Reject missing jobs, unexpected jobs,
   malformed records, unknown results, and stale skip-policy entries.
6. Keep `changes-gate` unconditional. Require each heavy job to depend on its
   `code_event=true` output and use the common condition. Keep `pr-policy` as
   the explicit event-only job that has no `needs` dependency.

Do not change the live branch-protection or ruleset configuration as part of
this decision. ADR-0007 continues to define those two required-check surfaces.

## Alternatives considered

- **Continue to accept all skipped jobs.** Rejected: a condition bypass or a
  dependency skip cascade can make a required validation result appear green.
- **Remove the aggregate.** Rejected: the aggregate keeps all workflow jobs in
  the classic branch-protection gate.
- **Encode the policy only in tests or documentation.** Rejected: the workflow
  must enforce the policy at run time.

## Consequences

- A job-graph or supported-event change must update the workflow graph, the
  aggregate `needs` list, the runtime census, the skip policy, applicable job
  conditions, tests, and documentation in one reviewed change.
- A missing, malformed, cancelled, failed, unknown, or unallowlisted skipped
  result fails `required-checks-gate`.
- The only permitted skip is visible as a small event-and-job allowlist.
