# ADR-0014: Confirmed implementation-state labels and merge-wait standby

- Status: Accepted
- Date: 2026-07-25
- Tracks: #2425
- Supersedes: ADR-0012

## Context

ADR-0012 retired the former CI proof workflow, status context, artifact, and
lease policy. That remains the correct historical decision, but the active
operator contract needs to say precisely what the implementation now enforces:
PR-review state is durable only after an exclusive GitHub label write is read
back, and merge-wait does not treat that label as standalone merge authority.

The current queue implementation has three separate facts:

1. `pr_review` creates a process-local reviewed-head proof from a GitHub PR
   snapshot and a clean checkout at the same head.
2. `pr_review` applies `state:implementation-go` or
   `state:implementation-no-go` only through helpers that read back the
   mutually exclusive implementation-state labels.
3. `merge_wait` accepts `state:implementation-go` only when the current work
   item still carries the matching process-local reviewed-head proof and the
   live PR is confirmed open and unarmed. A direct PR seed or restarted run has
   no durable proof to recover.

GitHub Actions required checks remain branch-protection and code-validation
signals. The advisory `auto-merge-policy` job may report observable PR state,
but no workflow, status, artifact, lease, or label-triggered event authorizes
the automation loop.

## Decision

1. The active durable implementation-review state is the confirmed exclusive
   PR label pair: exactly one of `state:implementation-go` or
   `state:implementation-no-go`, as read back from GitHub after the mutation.
   Review prose, audit summaries, and inline findings explain the decision but
   never replace the confirmed label.
2. `state:implementation-go` is not standalone merge authorization. It is one
   required input to merge-wait, which also requires the current process's
   reviewed-head proof and a confirmed open, unarmed live PR.
3. The reviewed-head proof is intentionally process-local. It is not persisted,
   backed up, published as an artifact, or reconstructed from comments after a
   restart.
4. Pending the separately reviewed #2419 conditional normal-merge path,
   `merge_wait` stands by after a matching proof. No queue stage creates,
   disables, adopts, or polls an auto-merge request. Existing or ambiguous
   merge ownership is external and left to operators.
5. Operator-facing contracts — `AGENTS.md`, `docs/architecture.md`,
   required-check documentation, state-label descriptions, and runbooks — must
   describe this confirmed-label plus process-local-proof contract rather than
   ADR-0012's older shorthand.

## Alternatives considered

- **Keep ADR-0012 as the active wording.** Rejected: it correctly removed
  CI/CD authorization, but its shorthand around "consuming" the label is too
  easy to read as durable merge authorization.
- **Persist the reviewed-head proof.** Rejected: it would create a new durable
  authorization artifact after the project deliberately removed artifacts,
  leases, and workflow-owned proof contexts.
- **Trust `state:implementation-go` alone.** Rejected: labels are not
  commit-bound and can outlive a process, a restart, or a PR head change.
- **Reintroduce auto-merge arming in merge-wait now.** Rejected: the current
  reviewed implementation stands by until #2419 supplies a separately reviewed
  conditional merge path.

## Consequences

- A restarted or direct PR run may seed a PR carrying
  `state:implementation-go` into merge-wait, but merge-wait must first verify
  a confirmed-unarmed PR and then return it to PR review after safe stale-label
  revocation when no matching proof exists.
- Operators must not manually enable auto-merge as a substitute for the
  reviewed-head proof.
- Backup and disaster-recovery procedures do not archive reviewed-head proof;
  they recover durable GitHub labels and PR state, then let the queue re-review
  when proof is absent.
- Required checks continue to block the GitHub merge button as code-validation
  policy, not as automation-loop authorization.
